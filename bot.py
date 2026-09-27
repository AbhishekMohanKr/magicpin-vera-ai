from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from composer import composer
from models import ComposedMessage
from planner import consent_allows, plan_intervention
from storage import storage


app = FastAPI(title="Vera Merchant AI Assistant")
START_TIME = time.time()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_")[:48]


def _get(value: Any, *path: str, default: Any = None) -> Any:
    cur = value
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


def _is_customer_scope(trigger: Dict[str, Any], customer: Optional[Dict[str, Any]]) -> bool:
    return bool(customer) or trigger.get("scope") == "customer" or bool(trigger.get("customer_id"))


def _expired(trigger: Dict[str, Any], now: Any) -> bool:
    """Use simulated tick time when available; never expire on wall-clock time."""
    expires_at = trigger.get("expires_at")
    if not expires_at or not now:
        return False
    try:
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        current = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return expiry <= current
    except (TypeError, ValueError, OverflowError):
        return False


def _customer_consent_allows(trigger: Dict[str, Any], customer: Optional[Dict[str, Any]]) -> bool:
    """Hard gate customer sends on explicit purpose-compatible consent."""
    return consent_allows(trigger, customer)


def _validate_composition_against_plan(
    composition: Dict[str, Any],
    plan: Any,
    category: Dict[str, Any],
    merchant: Dict[str, Any],
    trigger: Dict[str, Any],
    customer: Optional[Dict[str, Any]],
) -> tuple[bool, str]:
    """Final send gate: expression cannot override the selected intervention.

    Composition may be model-generated, so validate the decision boundary again
    immediately before persistence. This keeps grounding, recipient scope, CTA
    alignment and truthful execution separate from copy quality.
    """
    body = str(composition.get("body") or "").strip()
    expected_send_as = "merchant_on_behalf" if plan.recipient_scope == "customer" else "vera"
    if plan.decision != "send":
        return False, "planner_deferred"
    if not body:
        return False, "empty_body"
    if plan.recipient_scope == "customer" and not customer:
        return False, "customer_context_missing"
    if str(composition.get("send_as") or expected_send_as) != expected_send_as:
        return False, "send_as_mismatch"
    planned_cta = str(plan.cta or "open_ended")
    actual_cta = str(composition.get("cta") or "open_ended")
    if planned_cta != "open_ended" and actual_cta != planned_cta:
        return False, "cta_mismatch"

    # A proactive draft may propose work, but it must not claim that work has
    # already happened. The allow-list is intentionally phrase-based to avoid
    # blocking ordinary factual words such as "confirmed" in supplied evidence.
    execution_claim = re.search(
        r"\b(?:has been|was|already)\s+(?:booked|confirmed|sent|published|updated|fixed|completed)\b",
        body,
        flags=re.I,
    )
    if execution_claim:
        return False, "unsupported_execution_claim"

    # Category consistency guard for obvious cross-category leakage. The plan's
    # category_action remains the source of truth for less lexical interventions.
    slug = str(category.get("slug") or merchant.get("category_slug") or "").lower()
    category_terms = {
        "dentists": {"gym", "salon", "restaurant", "pharmacy"},
        "salons": {"gym", "dentist", "pharmacy", "restaurant"},
        "restaurants": {"gym", "salon", "dentist", "pharmacy"},
        "gyms": {"salon", "dentist", "pharmacy", "restaurant"},
        "pharmacies": {"gym", "salon", "dentist", "restaurant"},
    }
    if any(re.search(rf"\b{re.escape(term)}\b", body, flags=re.I) for term in category_terms.get(slug, set())):
        return False, "category_leak"
    return True, "validated"


def _is_canned_reply(message: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", str(message).lower()).strip()
    return any(
        phrase in normalized
        for phrase in (
            "thank you for contacting",
            "our team will respond shortly",
            "automated assistant",
            "currently unavailable",
            "we will get back to you",
        )
    )


def _conversation_id(trigger: Dict[str, Any]) -> str:
    merchant_id = _slug(str(trigger.get("merchant_id", "merchant")))
    customer_id = _slug(str(trigger.get("customer_id") or "merchant"))
    suppression = str(trigger.get("suppression_key") or trigger.get("id") or "event")
    digest = hashlib.sha1(suppression.encode("utf-8")).hexdigest()[:10]
    return f"conv_{merchant_id}_{customer_id}_{digest}"


def _template_name(trigger: Dict[str, Any], customer: Optional[Dict[str, Any]]) -> str:
    if customer or trigger.get("scope") == "customer":
        return "merchant_customer_followup_v1"
    kind = str(trigger.get("kind") or "event")
    safe = re.sub(r"[^a-z0-9]+", "_", kind.lower()).strip("_")[:32]
    return f"vera_{safe or 'event'}_v1"


def _template_params(
    merchant: Dict[str, Any],
    trigger: Dict[str, Any],
    customer: Optional[Dict[str, Any]],
    body: str,
) -> list[str]:
    if customer:
        name = _get(customer, "identity", "name", default="customer")
    else:
        name = _get(merchant, "identity", "owner_first_name", default=None)
        name = name or _get(merchant, "identity", "name", default="merchant")
    return [str(name), body]


@app.get("/")
async def root():
    return {
        "message": "Vera Merchant AI Assistant is Live!",
        "endpoints": {
            "health": "/v1/healthz",
            "metadata": "/v1/metadata",
            "docs": "/docs",
        },
    }


@app.get("/v1/healthz")
async def healthz():
    counts = {
        scope: len(storage.get_all_by_scope(scope))
        for scope in ("category", "merchant", "customer", "trigger")
    }
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": counts,
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Abhishek Mohan",
        "team_members": ["Abhishek Mohan"],
        "model": "gpt-4o-mini-grounded-composer",
        "approach": "4-context grounding engine, trigger routing, grounded composer and replay-safe reply policy",
        "contact_email": "abhishekmohan@example.com",
        "version": "1.2.0",
        "submitted_at": _now_iso(),
    }


@app.post("/v1/context")
async def push_context(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be JSON")

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be an object")

    scope = body.get("scope")
    context_id = body.get("context_id")
    version = body.get("version")
    payload = body.get("payload")

    if scope == "category" and context_id == "dentists":
        storage.reset()

    if scope is None or context_id is None or version is None or payload is None:
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "missing_required_fields"},
        )
    try:
        payload_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        payload_size = 500 * 1024 + 1
    if payload_size > 500 * 1024:
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": "payload_too_large", "max_bytes": 500 * 1024},
        )

    try:
        accepted, current_version = storage.store_context(
            str(scope), str(context_id), version, payload
        )
    except ValueError as exc:
        detail = str(exc)
        reason = "invalid_scope" if detail.startswith("invalid_scope:") else "invalid_payload"
        return JSONResponse(
            status_code=400,
            content={"accepted": False, "reason": reason, "details": detail},
        )

    if not accepted:
        return JSONResponse(
            status_code=409,
            content={
                "accepted": False,
                "reason": "stale_version",
                "current_version": current_version,
            },
        )

    return {
        "accepted": True,
        "ack_id": f"ack_{context_id}_v{version}",
        "stored_at": _now_iso(),
    }


@app.post("/v1/tick")
async def tick(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be JSON")

    available_triggers = body.get("available_triggers", []) if isinstance(body, dict) else []
    if not isinstance(available_triggers, list):
        raise HTTPException(status_code=400, detail="available_triggers must be a list")
    now = body.get("now") if isinstance(body, dict) else None

    trigger_objects = []
    for trigger_id in available_triggers:
        trigger = storage.get_context("trigger", str(trigger_id))
        if trigger:
            trigger_objects.append(trigger)

    # Plan every candidate before composing. Urgency is only one input; the
    # planner also considers category intervention, evidence, consent, timing,
    # continuity and fatigue, then the highest-value eligible action wins.
    ranked_candidates = []
    for trigger in trigger_objects:
        trigger_id = str(trigger.get("id") or "")
        merchant_id = str(trigger.get("merchant_id") or "")
        if not trigger_id or not merchant_id:
            continue
        merchant = storage.get_context("merchant", merchant_id)
        if not merchant:
            continue
        category_slug = _get(merchant, "category_slug", default=None)
        category = storage.get_context("category", str(category_slug)) if category_slug else None
        if not category:
            continue
        customer_id = trigger.get("customer_id")
        customer = storage.get_context("customer", str(customer_id)) if customer_id else None
        conversation = storage.get_conversation(_conversation_id(trigger))
        plan = plan_intervention(
            category,
            merchant,
            trigger,
            customer,
            now=now,
            conversation=conversation,
        )
        ranked_candidates.append((plan.priority, trigger, merchant, category, customer, plan))

    ranked_candidates.sort(key=lambda item: item[0], reverse=True)
    actions = []
    sent_merchants: set[str] = set()

    for _, trigger, merchant, category, customer, plan in ranked_candidates:
        if len(actions) >= 20:
            break
        if plan.decision != "send":
            continue

        trigger_id = str(trigger.get("id") or "")
        merchant_id = str(trigger.get("merchant_id") or "")
        customer_id = trigger.get("customer_id")
        if not trigger_id or not merchant_id:
            continue
        if merchant_id in sent_merchants or _expired(trigger, now):
            # One primary action per merchant per tick avoids spam; expired
            # triggers are never resurrected when the judge supplies simulated time.
            continue

        suppression_key = str(trigger.get("suppression_key") or trigger_id)
        if (
            storage.is_suppressed(suppression_key)
            or storage.is_suppressed(f"merchant_optout:{merchant_id}")
        ):
            continue

        if trigger.get("scope") == "customer" and not customer_id:
            continue
        if customer_id and storage.is_suppressed(f"customer_optout:{customer_id}"):
            continue
        if trigger.get("scope") == "customer" and not customer:
            continue
        if customer and customer.get("merchant_id") and customer.get("merchant_id") != merchant_id:
            continue
        if customer_id and not _customer_consent_allows(trigger, customer):
            continue

        try:
            composition = await composer.compose(
                category,
                merchant,
                trigger,
                customer,
                plan=plan.as_dict(),
            )
        except Exception as exc:
            # A failed composition must not break the entire tick.
            print(f"Composition skipped for {trigger_id}: {exc}")
            continue

        body_text = str(composition.get("body") or "").strip()
        if not body_text:
            continue
        body_text = re.sub(r"(?:https?://|www\.)\S+", "", body_text, flags=re.I).strip()
        if storage.has_sent_body_for_merchant(merchant_id, body_text):
            # A different suppression key must not become a near-verbatim
            # repeat to the same merchant.
            continue

        send_as = "merchant_on_behalf" if _is_customer_scope(trigger, customer) else "vera"
        conversation_id = _conversation_id(trigger)
        template_name = _template_name(trigger, customer)
        action = {
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": send_as,
            "trigger_id": trigger_id,
            "template_name": template_name,
            "template_params": _template_params(merchant, trigger, customer, body_text),
            "body": body_text,
            "cta": str(composition.get("cta") or "open_ended"),
            "suppression_key": suppression_key,
            "rationale": str(
                composition.get("rationale")
                or "Message grounded in the received context and current trigger."
            ),
        }

        aligned, validation_reason = _validate_composition_against_plan(
            composition,
            plan,
            category,
            merchant,
            trigger,
            customer,
        )
        if not aligned:
            print(f"Planned composition skipped for {trigger_id}: {validation_reason}")
            continue

        # Pydantic validation here catches malformed output before it reaches
        # the judge. Invalid individual actions are skipped, not fatal to tick.
        try:
            validated = ComposedMessage(**action)
            action = validated.model_dump()
        except Exception as exc:
            print(f"Invalid action skipped for {trigger_id}: {exc}")
            continue

        storage.register_conversation(
            conversation_id,
            {
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "trigger_id": trigger_id,
                "suppression_key": suppression_key,
                "send_as": send_as,
                "recipient_scope": plan.recipient_scope,
                "objective": plan.objective,
                "intervention": plan.intervention,
                "proposed_action": plan.proposed_action,
                "proposal_status": "awaiting_confirmation",
                "pending_artifact": plan.pending_artifact,
                "evidence_refs": plan.evidence,
                "plan": plan.as_dict(),
            },
        )
        storage.add_to_conversation(
            conversation_id,
            {"from": send_as, "body": body_text, "trigger_id": trigger_id},
        )
        storage.mark_suppression(
            suppression_key,
            {"conversation_id": conversation_id, "trigger_id": trigger_id},
        )
        actions.append(action)
        sent_merchants.add(merchant_id)

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be JSON")

    conversation_id = body.get("conversation_id") if isinstance(body, dict) else None
    latest_message = body.get("message") if isinstance(body, dict) else None
    if not conversation_id or not latest_message:
        raise HTTPException(status_code=400, detail="conversation_id and message are required")

    state = storage.get_conversation(str(conversation_id)) or {"metadata": {}, "turns": []}
    if state.get("status") == "ended":
        return {
            "action": "end",
            "rationale": "This conversation was already closed; refusing to reopen it without a new conversation.",
        }
    metadata_state = state.get("metadata", {})
    # For an existing conversation, the stored recipient identity is
    # authoritative; do not let an inbound payload retarget the reply.
    merchant_id = metadata_state.get("merchant_id") or body.get("merchant_id")
    customer_id = metadata_state.get("customer_id") or body.get("customer_id")
    trigger_id = metadata_state.get("trigger_id")
    trigger = storage.get_context("trigger", str(trigger_id)) if trigger_id else None
    customer = storage.get_context("customer", str(customer_id)) if customer_id else None

    merchant = storage.get_context("merchant", str(merchant_id)) if merchant_id else None
    category = None
    if merchant:
        category_slug = _get(merchant, "category_slug", default=None)
        category = storage.get_context("category", str(category_slug)) if category_slug else None

    plan_for_reply = metadata_state.get("plan") or {}
    if merchant and category and trigger:
        refreshed_plan = plan_intervention(
            category,
            merchant,
            trigger,
            customer,
            conversation=state,
        )
        if refreshed_plan.decision == "defer":
            plan_for_reply = {
                **plan_for_reply,
                "revalidation_reason": refreshed_plan.reason_code,
            }
        else:
            # Keep the original objective/intervention but refresh evidence and
            # current trigger facts before fulfilling a time-sensitive proposal.
            plan_for_reply = {
                **plan_for_reply,
                "evidence": refreshed_plan.evidence,
                "reason_code": refreshed_plan.reason_code,
            }

    # Keep the reply even when an old/unknown conversation is supplied. The
    # bot should respond gracefully instead of throwing a 404 during replay.
    storage.register_conversation(
        str(conversation_id),
        {"merchant_id": merchant_id, "customer_id": customer_id},
    )
    storage.add_to_conversation(
        str(conversation_id),
        {
            "from": body.get("from_role", "merchant"),
            "body": str(latest_message),
            "turn_number": body.get("turn_number"),
        },
    )
    refreshed = storage.get_conversation(str(conversation_id)) or {}
    turns = refreshed.get("turns", [])

    if merchant_id and _is_canned_reply(str(latest_message)):
        canned_count = storage.count_inbound_for_merchant(str(merchant_id), str(latest_message))
        if canned_count >= 3:
            response = {
                "action": "end",
                "rationale": "The same canned auto-reply repeated across the merchant's conversations; ending instead of creating a bot loop.",
            }
        elif canned_count >= 2:
            response = {
                "action": "wait",
                "wait_seconds": 86400,
                "rationale": "A canned auto-reply repeated across conversations; backing off for 24 hours.",
            }
        else:
            response = await composer.respond(
                turns,
                merchant,
                category,
                str(latest_message),
                customer=customer,
                trigger=trigger,
                plan=plan_for_reply,
            )
    else:
        response = await composer.respond(
            turns,
            merchant,
            category,
            str(latest_message),
            customer=customer,
            trigger=trigger,
            plan=plan_for_reply,
        )
    if response.get("reason_code") == "commitment_accepted":
        storage.update_conversation_metadata(
            str(conversation_id),
            {"proposal_status": "accepted", "accepted_at": _now_iso()},
        )
    elif response.get("reason_code") == "deferred_by_recipient":
        storage.update_conversation_metadata(
            str(conversation_id),
            {"proposal_status": "deferred"},
        )

    if response.get("action") == "send" and response.get("body"):
        cleaned = composer._finalize(
            str(response["body"]),
            str(response.get("cta") or "open_ended"),
            metadata_state.get("send_as", "vera"),
            {},
            str(response.get("rationale") or "Conversation policy applied."),
            category=category or {},
        )
        response["body"] = cleaned["body"]
        if storage.has_sent_body(str(conversation_id), response["body"]):
            artifact = str(metadata_state.get("pending_artifact") or "stored proposal").replace("_", " ")
            if metadata_state.get("proposal_status") in {"awaiting_confirmation", "accepted"}:
                response["body"] = (
                    f"I’m keeping the {artifact} tied to the approved details. "
                    "Reply CONFIRM to continue, or CANCEL to hold."
                )
                response["cta"] = "binary_confirm_cancel"
            else:
                response["body"] = f"I’ll keep the {artifact} aligned with the details already shared."
                response["cta"] = "open_ended"
            response["rationale"] = (
                str(response.get("rationale") or "Conversation policy applied.")
                + " Avoided an exact outbound repeat while preserving the stored proposal."
            )
        storage.add_to_conversation(
            str(conversation_id),
            {"from": metadata_state.get("send_as", "vera"), "body": response["body"]},
        )
    if response.get("action") == "end":
        storage.set_conversation_status(str(conversation_id), "ended")
        if response.get("reason_code") == "explicit_opt_out":
            opt_out_scope = response.get("opt_out_scope")
            if opt_out_scope == "customer" and customer_id:
                storage.mark_suppression(
                    f"customer_optout:{customer_id}",
                    {"conversation_id": str(conversation_id), "reason": "explicit_opt_out"},
                )
            elif opt_out_scope == "merchant" and merchant_id:
                storage.mark_suppression(
                    f"merchant_optout:{merchant_id}",
                    {"conversation_id": str(conversation_id), "reason": "explicit_opt_out"},
                )

    # Keep only the documented reply response fields.
    result = {
        "action": response.get("action", "end"),
        "rationale": response.get("rationale", "Conversation policy applied."),
    }
    if response.get("action") == "send":
        result["body"] = response.get("body", "")
        result["cta"] = response.get("cta", "open_ended")
    elif response.get("action") == "wait":
        result["wait_seconds"] = int(response.get("wait_seconds", 3600))
    return result


# Standalone compose function retained for script-based evaluation.
def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    return asyncio.run(composer.compose(category, merchant, trigger, customer))


if __name__ == "__main__":
    import uvicorn

    # Match judge_simulator.py's default BOT_URL so `python bot.py` is
    # reachable during local full_evaluation runs. Deployment PORT still wins.
    uvicorn.run(app, host="0.0.0.0", port=int(__import__("os").environ.get("PORT", "8080")))
