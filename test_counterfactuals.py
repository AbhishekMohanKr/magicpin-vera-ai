"""Counterfactual checks for planner/composer separation.

Run with: python3 test_counterfactuals.py
These are intentionally small, fixture-free checks: changing category, evidence,
consent or proposal state must change the decision or selected intervention.
"""

import asyncio
import unittest

from composer import VeraComposer
from planner import plan_intervention


MERCHANT = {
    "merchant_id": "m_cf",
    "category_slug": "dentists",
    "identity": {"name": "North Star Clinic", "locality": "Lajpat Nagar"},
    "performance": {"views": 1200, "calls": 24},
    "offers": [{"title": "Dental Cleaning", "status": "active"}],
}

CATEGORY_DENTIST = {"slug": "dentists", "voice": {"vocab_taboo": []}}
CATEGORY_GYM = {"slug": "gyms", "voice": {"vocab_taboo": []}}


class PlannerCounterfactualTests(unittest.TestCase):
    def test_category_changes_action_not_just_vocabulary(self):
        trigger = {
            "id": "cf_spike",
            "kind": "perf_spike",
            "merchant_id": "m_cf",
            "payload": {"metric": "calls", "delta_pct": 28, "window": "7d"},
        }
        dental = plan_intervention(CATEGORY_DENTIST, MERCHANT, trigger)
        gym = plan_intervention(CATEGORY_GYM, {**MERCHANT, "category_slug": "gyms"}, trigger)
        self.assertEqual(dental.decision, "send")
        self.assertEqual(gym.decision, "send")
        self.assertNotEqual(dental.objective, gym.objective)
        self.assertNotEqual(dental.category_action, gym.category_action)
        self.assertIn("clinical", dental.proposed_action)
        self.assertIn("member-facing", gym.proposed_action)

    def test_evidence_counterfactual_changes_send_decision(self):
        empty = {
            "id": "cf_unknown",
            "kind": "new_signal_not_in_playbook",
            "merchant_id": "m_cf",
            "payload": {},
        }
        grounded = {
            **empty,
            "payload": {"metric": "calls", "current": 18, "baseline": 10},
        }
        self.assertEqual(
            plan_intervention(CATEGORY_DENTIST, {"merchant_id": "m_cf", "category_slug": "dentists"}, empty).decision,
            "defer",
        )
        self.assertEqual(
            plan_intervention(CATEGORY_DENTIST, {"merchant_id": "m_cf", "category_slug": "dentists"}, grounded).decision,
            "send",
        )

    def test_consent_purpose_counterfactual(self):
        trigger = {
            "id": "cf_recall",
            "kind": "recall_due",
            "scope": "customer",
            "customer_id": "c_cf",
            "merchant_id": "m_cf",
            "payload": {"due_date": "2026-10-01", "consent_scope": "recall_reminders"},
        }
        base_customer = {
            "customer_id": "c_cf",
            "merchant_id": "m_cf",
            "preferences": {"reminder_opt_in": True},
            "consent": {"opted_in_at": "2026-01-01T00:00:00Z", "scope": ["promotional_offers"]},
        }
        recall_customer = {
            **base_customer,
            "consent": {"opted_in_at": "2026-01-01T00:00:00Z", "scope": ["recall_reminders"]},
        }
        self.assertEqual(plan_intervention(CATEGORY_DENTIST, MERCHANT, trigger, base_customer).reason_code, "consent_purpose_mismatch")
        self.assertEqual(plan_intervention(CATEGORY_DENTIST, MERCHANT, trigger, recall_customer).decision, "send")

    def test_reply_progresses_the_stored_proposal(self):
        async def run():
            composer = VeraComposer()
            plan = {
                "recipient_scope": "merchant",
                "pending_artifact": "planning_draft",
                "proposed_action": "turn the prior plan into a ready-to-send draft",
            }
            accepted = await composer.respond(
                [{"from": "vera", "body": "Want me to draft it?"}],
                MERCHANT,
                CATEGORY_DENTIST,
                "Yes, draft it",
                plan=plan,
            )
            completed = await composer.respond(
                [{"from": "vera", "body": "Want me to draft it?"}],
                MERCHANT,
                CATEGORY_DENTIST,
                "Thanks, done",
                plan=plan,
            )
            return accepted, completed

        accepted, completed = asyncio.run(run())
        self.assertEqual(accepted["reason_code"], "commitment_accepted")
        self.assertEqual(accepted["proposal_status"], "accepted")
        self.assertEqual(completed["reason_code"], "completed")
        self.assertIsNone(completed["opt_out_scope"])

    def test_review_composition_uses_planner_action(self):
        async def run():
            composer = VeraComposer()
            trigger = {
                "id": "cf_review",
                "kind": "review_theme_emerged",
                "merchant_id": "m_cf",
                "payload": {"theme": "delivery_late", "occurrences_30d": 4},
            }
            plan = plan_intervention(CATEGORY_DENTIST, MERCHANT, trigger).as_dict()
            plan["proposed_action"] = "prepare a review response and delivery recovery checklist"
            result = await composer.compose(
                CATEGORY_DENTIST,
                MERCHANT,
                trigger,
                plan=plan,
            )
            return result["body"]

        body = asyncio.run(run())
        self.assertIn("delivery recovery checklist", body)


if __name__ == "__main__":
    unittest.main()
