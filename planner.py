from __future__ import annotations

"""Evidence-first intervention planning for Vera.

The planner decides *whether* a proactive action is justified and what kind of
intervention is appropriate.  The composer is deliberately kept separate: it
only turns the selected plan into merchant/customer wording.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


CUSTOMER_KINDS = {
    "appointment_tomorrow",
    "recall_due",
    "trial_followup",
    "wedding_package_followup",
    "customer_lapsed_soft",
    "customer_lapsed_hard",
    "winback_eligible",
    "chronic_refill_due",
    "renewal_due",
}

KIND_PRIORITY = {
    "supply_alert": 10,
    "regulation_change": 9,
    "appointment_tomorrow": 9,
    "chronic_refill_due": 9,
    "recall_due": 8,
    "perf_dip": 8,
    "seasonal_perf_dip": 8,
    "competitor_opened": 7,
    "gbp_unverified": 7,
    "renewal_due": 7,
    "research_digest": 6,
    "cde_opportunity": 6,
    "review_theme_emerged": 6,
    "winback_eligible": 6,
    "customer_lapsed_hard": 6,
    "customer_lapsed_soft": 5,
    "perf_spike": 5,
    "ipl_match_today": 5,
    "festival_upcoming": 4,
    "category_seasonal": 4,
    "active_planning_intent": 8,
    "dormant_with_vera": 3,
    "curious_ask_due": 3,
}

CATEGORY_ACTIONS = {
    "dentists": {
        "perf_dip": ("protect_recall_utilisation", "audit recall/listing and draft a patient-safe recovery plan"),
        "perf_spike": ("compound_patient_discovery", "turn the winning signal into a clinical Google listing post"),
        "seasonal_perf_dip": ("retain_recall_patients", "draft a seasonal recall education plan for current patients"),
        "category_seasonal": ("educate_and_retain", "draft a recall education post without discounting treatment"),
        "review_theme_emerged": ("repair_patient_trust", "draft a review response and a three-step service fix"),
    },
    "salons": {
        "perf_dip": ("recover_rebooking", "draft a rebooking and listing-recovery plan"),
        "perf_spike": ("compound_rebooking", "turn the winning service signal into a rebooking post"),
        "seasonal_perf_dip": ("protect_rebooking", "draft a seasonal rebooking plan for existing clients"),
        "category_seasonal": ("fill_evidenced_demand", "draft a seasonal service post using only live offers"),
        "review_theme_emerged": ("repair_service_experience", "draft a review response and a practical service fix"),
    },
    "restaurants": {
        "perf_dip": ("recover_repeat_orders", "test an off-peak or repeat-order campaign using an existing offer"),
        "perf_spike": ("compound_repeat_orders", "turn the winning demand signal into a repeat-order post"),
        "seasonal_perf_dip": ("protect_repeat_orders", "draft a seasonal repeat-order plan using an active offer"),
        "category_seasonal": ("capture_meal_demand", "draft a seasonal menu or offer post from supplied items"),
        "review_theme_emerged": ("repair_dining_experience", "draft a review response and an operational service fix"),
    },
    "gyms": {
        "perf_dip": ("recover_attendance", "draft an attendance reactivation plan without transformation claims"),
        "perf_spike": ("compound_attendance", "turn the winning class signal into a member-facing post"),
        "seasonal_perf_dip": ("retain_members", "draft a seasonal attendance challenge for current members"),
        "category_seasonal": ("retain_members", "draft a seasonal attendance challenge for current members"),
        "review_theme_emerged": ("repair_member_experience", "draft a review response and a practical coaching fix"),
    },
    "pharmacies": {
        "perf_dip": ("recover_profile_discovery", "audit listing and refill visibility without treatment claims"),
        "perf_spike": ("compound_refill_visibility", "turn the winning demand signal into a precise listing post"),
        "seasonal_perf_dip": ("protect_verified_demand", "draft a seasonal listing update using verified demand"),
        "category_seasonal": ("capture_verified_demand", "draft a seasonal health-pack listing post from supplied demand data"),
        "review_theme_emerged": ("repair_service_trust", "draft a review response and a practical service fix"),
    },
}

CONSENT_SCOPES = {
    "appointment_tomorrow": {"appointment_reminders"},
    "recall_due": {"recall_reminders", "appointment_reminders"},
    "trial_followup": {"appointment_reminders", "bridal_package_followup", "kids_program_updates", "program_updates"},
    "wedding_package_followup": {"bridal_package_followup", "appointment_reminders"},
    "renewal_due": {"renewal_reminders", "program_updates"},
    "customer_lapsed_soft": {"promotional_offers", "winback_offers"},
    "customer_lapsed_hard": {"promotional_offers", "winback_offers"},
    "winback_eligible": {"promotional_offers", "winback_offers"},
    "chronic_refill_due": {"refill_reminders", "delivery_notifications", "recall_alerts"},
}


@dataclass
class InterventionPlan:
    decision: str
    recipient_scope: str
    send_as: str
    objective: str = ""
    intervention: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)
    proposed_action: str = ""
    pending_artifact: str = ""
    cta: str = "open_ended"
    constraints: list[str] = field(default_factory=list)
    reason_code: str = ""
    priority: float = 0.0
    trigger_id: str = ""
    category_action: str = ""
    required_consent_scopes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _get(value: Any, *path: str, default: Any = None) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _is_customer_scope(trigger: dict, customer: Optional[dict]) -> bool:
    return bool(customer) or trigger.get("scope") == "customer" or bool(trigger.get("customer_id"))


def _expired(trigger: dict, now: Any) -> bool:
    if not trigger.get("expires_at") or not now:
        return False
    try:
        expiry = datetime.fromisoformat(_text(trigger["expires_at"]).replace("Z", "+00:00"))
        current = datetime.fromisoformat(_text(now).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return expiry <= current
    except (TypeError, ValueError, OverflowError):
        return False


def consent_allows(trigger: dict, customer: Optional[dict]) -> bool:
    """Require opt-in and purpose-compatible consent for customer outreach."""
    if not customer:
        return False
    preferences = customer.get("preferences") or {}
    if preferences.get("reminder_opt_in") is not True:
        return False
    consent = customer.get("consent") or {}
    if not isinstance(consent, dict) or not consent.get("opted_in_at"):
        return False
    scopes = consent.get("scope")
    if not isinstance(scopes, list) or not scopes:
        return False
    raw_required = (
        _get(trigger, "payload", "consent_scope", default=None)
        or trigger.get("consent_scope")
        or CONSENT_SCOPES.get(_text(trigger.get("kind")), set())
    )
    required = {raw_required} if isinstance(raw_required, str) else set(raw_required or set())
    # A customer send must name a purpose and that purpose must intersect the
    # customer's granted scope. Unknown purposes fail closed rather than
    # treating a generic channel opt-in as consent for an unbounded campaign.
    granted = {str(scope) for scope in scopes}
    return bool(required) and bool(required.intersection(granted))


def _evidence(category: dict, merchant: dict, trigger: dict, customer: Optional[dict]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    payload = trigger.get("payload") or {}
    for key, value in payload.items():
        if key in {"placeholder", "ask_template", "merchant_id", "customer_id", "category"}:
            continue
        if value in (None, "", [], {}, False):
            continue
        if isinstance(value, (str, int, float)) or (isinstance(value, list) and value):
            evidence.append({"source_scope": "trigger", "source_path": f"payload.{key}", "value": value})
    performance = merchant.get("performance") or {}
    for key in ("views", "calls", "directions", "ctr", "leads"):
        if performance.get(key) is not None:
            evidence.append({"source_scope": "merchant", "source_path": f"performance.{key}", "value": performance[key]})
    offers = [offer.get("title") for offer in merchant.get("offers", []) or [] if offer.get("status") == "active" and offer.get("title")]
    if offers:
        evidence.append({"source_scope": "merchant", "source_path": "offers.active", "value": offers[:3]})
    signals = merchant.get("signals") or []
    if signals:
        evidence.append({"source_scope": "merchant", "source_path": "signals", "value": signals[:4]})
    if customer:
        relationship = customer.get("relationship") or {}
        for key in ("last_visit", "visits_total", "services_received"):
            if relationship.get(key) not in (None, "", [], {}):
                evidence.append({"source_scope": "customer", "source_path": f"relationship.{key}", "value": relationship[key]})
    digest = category.get("digest") or []
    if digest:
        evidence.append({"source_scope": "category", "source_path": "digest", "value": "available"})
    return evidence[:10]


def _category_action(slug: str, kind: str) -> tuple[str, str]:
    return CATEGORY_ACTIONS.get(slug, {}).get(kind, ("merchant_growth", "prepare one grounded next step"))


def _objective_and_artifact(kind: str, customer: Optional[dict]) -> tuple[str, str, str]:
    if customer:
        mapping = {
            "appointment_tomorrow": ("protect_appointment", "confirm the existing appointment", "appointment_confirmation"),
            "recall_due": ("recover_recall", "draft a consented recall reminder", "recall_reminder"),
            "trial_followup": ("convert_trial", "draft the next-session follow-up", "trial_followup"),
            "wedding_package_followup": ("convert_bridal_trial", "draft the next bridal step", "bridal_followup"),
            "chronic_refill_due": ("protect_refill_continuity", "prepare a verified refill reminder", "refill_reminder"),
            "customer_lapsed_soft": ("recover_relationship", "draft a no-pressure return note", "winback_note"),
            "customer_lapsed_hard": ("recover_relationship", "draft a no-pressure return note", "winback_note"),
            "winback_eligible": ("recover_relationship", "draft a consented win-back note", "winback_note"),
        }
        return mapping.get(kind, ("customer_followup", "draft the relevant customer follow-up", "customer_followup"))
    mapping = {
        "research_digest": ("share_relevant_evidence", "draft a short education or customer-facing post", "education_draft"),
        "regulation_change": ("meet_compliance_change", "draft an implementation checklist", "compliance_checklist"),
        "supply_alert": ("protect_supply_continuity", "draft the customer note and replacement workflow", "replacement_workflow"),
        "cde_opportunity": ("capture_professional_learning", "prepare registration and calendar details", "registration_details"),
        "gbp_unverified": ("remove_profile_friction", "prepare the verification walkthrough", "verification_steps"),
        "active_planning_intent": ("advance_stated_intent", "turn the prior plan into a ready-to-send draft", "planning_draft"),
        "dormant_with_vera": ("restart_useful_dialogue", "identify one blocker and shape a seven-day plan", "seven_day_plan"),
        "milestone_reached": ("compound_social_proof", "draft a milestone celebration post", "milestone_post"),
    }
    return mapping.get(kind, ("merchant_growth", "prepare one grounded next step", "next_step_draft"))


def plan_intervention(
    category: dict,
    merchant: dict,
    trigger: dict,
    customer: Optional[dict] = None,
    *,
    now: Any = None,
    conversation: Optional[dict] = None,
) -> InterventionPlan:
    kind = _text(trigger.get("kind") or "event")
    recipient_scope = "customer" if _is_customer_scope(trigger, customer) else "merchant"
    send_as = "merchant_on_behalf" if recipient_scope == "customer" else "vera"
    trigger_id = _text(trigger.get("id"))
    base = dict(
        decision="send",
        recipient_scope=recipient_scope,
        send_as=send_as,
        trigger_id=trigger_id,
    )

    if trigger.get("scope") == "customer" and not trigger.get("customer_id"):
        return InterventionPlan(**{**base, "decision": "defer"}, reason_code="missing_customer_reference")
    if recipient_scope == "customer" and not customer:
        return InterventionPlan(**{**base, "decision": "defer"}, reason_code="missing_customer_context")
    if _expired(trigger, now):
        return InterventionPlan(**{**base, "decision": "defer"}, reason_code="expired_trigger")
    if recipient_scope == "customer" and not consent_allows(trigger, customer):
        return InterventionPlan(**{**base, "decision": "defer"}, reason_code="consent_purpose_mismatch")
    if conversation and conversation.get("status") == "ended":
        return InterventionPlan(**{**base, "decision": "defer"}, reason_code="conversation_closed")

    objective, artifact, pending_artifact = _objective_and_artifact(kind, customer)
    category_slug = _text(category.get("slug") or merchant.get("category_slug"))
    category_objective, category_action = _category_action(category_slug, kind)
    # For category-specific plays, the planner's objective changes too; this is
    # deliberately more than swapping nouns in an otherwise generic message.
    if category_objective != "merchant_growth":
        objective = category_objective
    evidence = _evidence(category, merchant, trigger, customer)
    payload = trigger.get("payload") or {}
    has_trigger_fact = any(
        key not in {"placeholder", "ask_template", "metric_or_topic", "category"}
        and value not in (None, "", [], {}, False)
        for key, value in payload.items()
    )
    merchant_support = bool(evidence)
    if kind not in KIND_PRIORITY and not has_trigger_fact and not merchant_support:
        return InterventionPlan(**{**base, "decision": "defer"}, reason_code="insufficient_evidence")

    continuity = 3 if kind == "active_planning_intent" or payload.get("merchant_last_message") else 0
    urgency = max(0, min(10, int(trigger.get("urgency", 0) or 0)))
    evidence_score = min(8, len(evidence))
    timing = 2 if any(key in payload for key in ("deadline_iso", "due_date", "expires_at", "match_time_iso", "days_until")) else 0
    fatigue = 0
    if conversation and conversation.get("metadata", {}).get("objective") == objective:
        fatigue = 2
    priority = KIND_PRIORITY.get(kind, 2) + urgency * 0.8 + evidence_score * 0.7 + timing + continuity - fatigue

    constraints = [
        "Use only evidence_refs in the received contexts",
        "Do not invent availability, prices, offers, diagnoses, or execution results",
        "Use one CTA aligned with the selected intervention",
        "Explain why this matters now before asking for the next step",
        "Prefer one reversible, low-effort action over a broad campaign ask",
        "Use a single reply keyword, binary choice, or supplied slot choice; avoid multiple questions",
        "Structure the message as observation, implication, then one CTA; do not merely repeat the metric",
        "Prefer two short sentences and name what Vera will produce after the reply",
        "Classify the trigger as a problem, opportunity, information-only update, or urgent issue before choosing an action",
        "Do not turn a positive metric into a campaign unless a supplied driver, offer, or category action makes the opportunity concrete",
    ]
    if recipient_scope == "customer":
        constraints.append("Send only within the matched consent purpose")
    if category_slug in {"dentists", "pharmacies"}:
        constraints.append("Avoid treatment or clinical outcome claims")

    expected_cta = "open_ended"
    if kind in {"appointment_tomorrow", "chronic_refill_due"}:
        expected_cta = "binary_confirm_cancel"
    elif recipient_scope == "customer" and kind in {"trial_followup", "wedding_package_followup", "customer_lapsed_soft", "customer_lapsed_hard", "winback_eligible"}:
        expected_cta = "binary_yes_no"
    elif kind == "recall_due":
        slots = payload.get("available_slots") or payload.get("next_session_options") or []
        expected_cta = "multi_choice_slot" if slots else "binary_confirm_cancel"
    return InterventionPlan(
        **base,
        objective=objective,
        intervention=category_action if category_action != "prepare one grounded next step" else artifact,
        evidence=evidence,
        proposed_action=category_action if category_action != "prepare one grounded next step" else artifact,
        pending_artifact=pending_artifact,
        cta=expected_cta,
        constraints=constraints,
        reason_code=f"supported_{category_objective}",
        priority=priority,
        category_action=category_action,
        required_consent_scopes=sorted(CONSENT_SCOPES.get(kind, set())),
    )
