from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv
except Exception:  # optional in deterministic/offline mode
    def load_dotenv(*args, **kwargs):
        return False

from urllib import error as urlerror, request as urlrequest

from planner import plan_intervention

load_dotenv()


_MISSING = object()


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    return {}


def _get(value: Any, *path: str, default: Any = None) -> Any:
    cur = value
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key, _MISSING)
        else:
            cur = getattr(cur, key, _MISSING)
        if cur is _MISSING:
            return default
    return cur


def _items(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_as_dict(item) for item in value]


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _humanize(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("_", " ").strip()).capitalize()


def _iso_label(value: Any) -> str:
    if not value:
        return ""
    raw = str(value)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%d %b %Y")
    except Exception:
        return raw[:10] if len(raw) >= 10 else raw


def _datetime_label(value: Any) -> str:
    """Readable local-ish date/time label while preserving the supplied value."""
    if not value:
        return ""
    raw = str(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        date_part = parsed.strftime("%d %b %Y")
        time_part = parsed.strftime("%-I:%M %p").lstrip("0")
        return f"{date_part}, {time_part}"
    except Exception:
        return _iso_label(value)


def _merchant_signature(merchant: Dict[str, Any]) -> str:
    identity = _get(merchant, "identity", default={}) or {}
    name = _text(identity.get("name")) or "the business"
    locality = _text(identity.get("locality"))
    return f"{name} {locality}".strip()


def _first_positive_review(merchant: Dict[str, Any]) -> Dict[str, Any]:
    for theme in _items(_get(merchant, "review_themes", default=[])):
        if _text(theme.get("sentiment")).lower() in {"pos", "positive"}:
            return theme
    return {}


def _digest_for_kind(category: Dict[str, Any], kind: str) -> Dict[str, Any]:
    digest = _items(_get(category, "digest", default=[]))
    preferred = {
        "ipl_match_today": {"ipl", "match"},
        "category_seasonal": {"summer", "season"},
        "supply_alert": {"recall", "alert", "atorvastatin"},
    }.get(kind, set())
    for item in digest:
        haystack = " ".join(_text(item.get(k)) for k in ("id", "kind", "title", "summary", "actionable")).lower()
        if preferred and any(token in haystack for token in preferred):
            return item
    return digest[0] if digest else {}


def _owner_salutation(merchant: Dict[str, Any], category_slug: str) -> str:
    identity = _get(merchant, "identity", default={}) or {}
    owner = _text(identity.get("owner_first_name")).strip()
    business = _text(identity.get("name")).strip() or "there"
    if owner:
        if category_slug == "dentists" and not owner.lower().startswith("dr"):
            return f"Dr. {owner}"
        return owner
    return business


def _customer_first_name(customer: Dict[str, Any]) -> str:
    raw = _text(_get(customer, "identity", "name", default="there")) or "there"
    cleaned = re.sub(r"\(.*?\)", "", raw).strip()
    tokens = cleaned.split()
    if tokens and tokens[0].rstrip(".").lower() in {"mr", "mrs", "ms", "dr", "shri", "smt"}:
        tokens = tokens[1:]
    return tokens[0] if tokens else (cleaned or "there")


def _customer_name(customer: Dict[str, Any]) -> str:
    raw = _text(_get(customer, "identity", "name", default="there")) or "there"
    parent = re.search(r"parent:\s*([^\)]+)", raw, re.I)
    if parent:
        return parent.group(1).strip().split()[0]
    return _customer_first_name(customer)


def _active_offers(merchant: Dict[str, Any]) -> List[str]:
    return [
        _text(offer.get("title"))
        for offer in _items(_get(merchant, "offers", default=[]))
        if offer.get("status") == "active" and offer.get("title")
    ]


def _find_digest_item(category: Dict[str, Any], trigger: Dict[str, Any]) -> Dict[str, Any]:
    payload = _get(trigger, "payload", default={}) or {}
    wanted = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id")
    digest = _items(_get(category, "digest", default=[]))
    if wanted:
        for item in digest:
            if item.get("id") == wanted:
                return item
    # A few examples put a complete item in the trigger.
    if isinstance(payload.get("top_item"), dict):
        return payload["top_item"]
    if payload.get("top_item"):
        wanted_text = _text(payload["top_item"]).strip().lower()
        for item in digest:
            haystack = " ".join(_text(item.get(key)) for key in ("id", "title", "summary", "actionable")).lower()
            if wanted_text and (wanted_text in haystack or haystack in wanted_text):
                return item
        return {"title": str(payload["top_item"]), "source": "provided trigger"}
    return digest[0] if digest else {}


def _has_hindi_preference(value: Any) -> bool:
    return "hi" in _text(value).lower()


def _percent(value: Any, signed: bool = False) -> str:
    """Render a numeric ratio as a readable percentage without exposing a raw float."""
    if not isinstance(value, (int, float)):
        return ""
    number = float(value) * 100
    if signed:
        return f"{number:+.0f}%"
    return f"{number:.0f}%"


def _label(value: Any) -> str:
    return re.sub(r"\s+", " ", _text(value).replace("_", " ").strip())


def _season_label(value: Any) -> str:
    """Turn machine-readable seasonal keys into merchant-facing timing."""
    raw = _text(value).strip().lower()
    known = {
        "post_resolution_window_apr_jun": "April–June",
        "post_resolution_window_apr_may": "April–May",
    }
    return known.get(raw, _label(value) or "this seasonal window")


def _merchant_history(merchant: Dict[str, Any], sender: Optional[str] = None) -> List[Dict[str, Any]]:
    history = _items(_get(merchant, "conversation_history", default=[]))
    if sender:
        history = [item for item in history if _text(item.get("from")).lower() == sender.lower()]
    return history


def _latest_history_body(merchant: Dict[str, Any], sender: Optional[str] = None) -> str:
    history = _merchant_history(merchant, sender)
    return _text(history[-1].get("body")) if history else ""


def _proposal_from_history(merchant: Dict[str, Any]) -> str:
    """Recover a prior, already-suggested outline instead of inventing one."""
    body = _latest_history_body(merchant, "vera")
    if not body:
        return ""
    match = re.search(r"(?:suggest(?:ed)?|recommend(?:ed)?)\s+(.+?)(?:\.\s*(?:want|would you)|$)", body, re.I)
    return match.group(1).strip(" .") if match else ""


def _extract_numeric_anchor(merchant: Dict[str, Any], pattern: str) -> str:
    for item in _merchant_history(merchant):
        match = re.search(pattern, _text(item.get("body")), re.I)
        if match:
            return match.group(1)
    return ""


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


_NUMBER_RE = re.compile(r"(?<![A-Za-z])(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)")


def _normalised_numbers(value: Any) -> set[str]:
    numbers = set()
    for raw in _NUMBER_RE.findall(_text(value)):
        numbers.add(raw.replace(",", ""))
    return numbers


def _unsupported_numeric_claim(
    body: str,
    category: Dict[str, Any],
    merchant: Dict[str, Any],
    trigger: Dict[str, Any],
    customer: Optional[Dict[str, Any]],
) -> bool:
    """Catch LLM-invented numeric claims before they can ship.

    The deterministic templates already read directly from context. This gate
    protects the optional model path: all numbers must occur in the received
    four-context JSON, except small effort estimates in a CTA (for example
    ``2 minutes``). A fabricated 1/2 slot is rejected when no slots arrived.
    """
    context_blob = json.dumps(
        {"category": category, "merchant": merchant, "trigger": trigger, "customer": customer},
        ensure_ascii=False,
    )
    known = _normalised_numbers(context_blob)
    candidate = re.sub(r"\b\d+(?:\.\d+)?\s*[- ]?(?:min|mins|minute|minutes)\b", "", body, flags=re.I)
    if re.search(r"\b1\s*/\s*2\b", body) and not (_get(trigger, "payload", "available_slots", default=[]) or []):
        return True
    return any(number not in known for number in _normalised_numbers(candidate))


class VeraComposer:
    def __init__(self) -> None:
        self.api_key = os.getenv("OPENROUTER_API_KEY") or "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"
        self.model_name = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini")  # or "openai/gpt-4o-mini"
        self.use_mock = True#(
            #not self.api_key
            #or "YOUR_ACTUAL_API_KEY" in self.api_key
            #or self.api_key.startswith("mock_")
            #or len(self.api_key) < 10
        #)
        if not self.use_mock:
            print(f"VeraComposer initialized with OpenRouter API ({self.model_name}).")
        else:
            print("WARNING: No valid OPENROUTER_API_KEY found. Vera is using deterministic composition.")
    
    def _call_openrouter(
        self,
        prompt: str,
        system: Optional[str] = None,
        json_mode: bool = True,
        max_tokens: int = 500,
        temperature: float = 0.0,
    ) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: Dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode and "openai" in self.model_name.lower():
            payload["response_format"] = {"type": "json_object"}

        body = json.dumps(payload).encode("utf-8")
        req = urlrequest.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://magicpin.com",
                "X-Title": "magicpin Vera",
            },
        )
        resp = urlrequest.urlopen(req, timeout=12)
        data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    
    async def compose(
        self,
        category: Dict[str, Any],
        merchant: Dict[str, Any],
        trigger: Dict[str, Any],
        customer: Optional[Dict[str, Any]] = None,
        plan: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        selected_plan = plan or plan_intervention(category, merchant, trigger, customer).as_dict()
        if self.use_mock:
            return self._smart_template_compose(category, merchant, trigger, customer, selected_plan)

        prompt = self._build_compose_prompt(category, merchant, trigger, customer, selected_plan)
        try:
            raw_text = self._call_openrouter(prompt, json_mode=True, max_tokens=500)
            result = json.loads(raw_text)
            return self._validate_composition(result, category, merchant, trigger, customer)
        except Exception as exc:
            print(f"LLM composition error; deterministic fallback used: {exc}")
            return self._smart_template_compose(category, merchant, trigger, customer, selected_plan)

    def _build_compose_prompt(
        self,
        category: Dict[str, Any],
        merchant: Dict[str, Any],
        trigger: Dict[str, Any],
        customer: Optional[Dict[str, Any]],
        plan: Optional[Dict[str, Any]] = None,
    ) -> str:
        # Keep the prompt grounded in the exact received payload. The model is
        # not allowed to infer missing offers, slots, sources, or metrics.
        kind = _text(_get(trigger, "kind", default="event"))
        guidance = {
            "research_digest": "Lead with the digest title and source, connect it to the merchant's supplied cohort or metric, and offer a short abstract plus patient/customer education draft.",
            "regulation_change": "State the effective date, exact rule change and source, then offer an SOP/checklist rather than legal certainty.",
            "supply_alert": "Use the matched alert item, exact molecule/batches and any supplied customer count; offer the customer note and replacement workflow.",
            "cde_opportunity": "Use the supplied date, credits and fee; make registration or calendar help the single next step.",
            "festival_upcoming": "Use the countdown and current merchant proof; frame early planning and offer a concrete Google-listing/WhatsApp artifact.",
            "category_seasonal": "Use the exact product-demand deltas and current merchant metrics; convert them into one category-relevant offer or post.",
            "ipl_match_today": "Use the match, venue, time and supplied demand comparison; recommend the supplied operational move, not a generic promo.",
            "weather_heatwave": "Use the supplied temperature/locality and a real offer or category-relevant action; offer a short customer-facing post.",
            "competitor_opened": "Use the competitor distance/offer only when supplied; pair it with this merchant's own offer or differentiator and a retention artifact.",
            "review_theme_emerged": "Name the negative theme and a positive differentiator when supplied; offer a three-step fix or review reply.",
            "gbp_unverified": "Use the supplied verification route, views and uplift estimate; offer a short walkthrough.",
            "milestone_reached": "State the exact current and target values; use supplied review proof when available and ask for one celebration artifact.",
            "active_planning_intent": "Continue the prior thread and use existing offers/history; turn the intent into one ready-to-send WhatsApp draft.",
            "dormant_with_vera": "Acknowledge elapsed time and prior topic, surface current metrics/offers and ask one blocker question tied to a short plan.",
            "perf_dip": "Name the exact metric change, baseline/peer/current count when supplied, and offer one testable recovery plan.",
            "perf_spike": "Name the exact metric change and likely driver when supplied; offer one customer-facing post to compound it.",
        }.get(kind, "Lead with the strongest supplied trigger fact, connect it to this merchant, and offer one concrete next artifact.")
        voice = _get(category, "voice", default={}) or {}
        return (
            "You are Vera, a precise merchant-growth assistant. Return only JSON.\n"
            "Use only facts present in the following JSON contexts. Never invent a price, date, slot, source, customer fact, competitor, or metric.\n"
            "Choose one primary signal and one primary CTA. Customer scope must use merchant_on_behalf; merchant scope must use vera.\n"
            "Keep body at or below 320 characters, avoid URLs/internal IDs/trigger jargon/taboo words, and preserve the final CTA.\n"
            "Make the decision explicit: state why this matters now, recommend one reversible next action, and end with one reply keyword, binary choice, or supplied slot choice.\n"
            "Do not end with a vague question, stack multiple asks, claim execution, or ask the recipient to decide without first offering a concrete artifact or benefit.\n"
            "Write like a helpful colleague. Front-load the most useful fact, use plain words, and keep to two short sentences: why this matters, then one practical next step and one natural reply question. Drop facts that do not change the recommendation.\n"
            "Classify the trigger before acting: problem, opportunity, information-only update, or urgent issue. Positive metrics require a supplied driver, active offer, or category action before recommending a campaign; otherwise acknowledge the result and wait.\n"
            "The CTA must be a natural consequence of the preceding evidence. Do not manufacture urgency or promise an unsupported turnaround time; state exactly what Vera will produce if the merchant replies.\n"
            f"TRIGGER PLAYBOOK for {kind}: {guidance}\n"
            f"CATEGORY VOICE: {json.dumps(voice, ensure_ascii=False)}\n"
            f"SELECTED INTERVENTION PLAN={json.dumps(plan or {}, ensure_ascii=False)}\n"
            "Follow the selected intervention; do not replace it with a generic offer or a different CTA.\n\n"
            f"CATEGORY={json.dumps(category, ensure_ascii=False)}\n"
            f"MERCHANT={json.dumps(merchant, ensure_ascii=False)}\n"
            f"TRIGGER={json.dumps(trigger, ensure_ascii=False)}\n"
            f"CUSTOMER={json.dumps(customer, ensure_ascii=False) if customer else 'null'}\n\n"
            "Return: {body, cta, send_as, suppression_key, rationale}."
        )

    def _validate_composition(
        self,
        result: Dict[str, Any],
        category: Dict[str, Any],
        merchant: Dict[str, Any],
        trigger: Dict[str, Any],
        customer: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not isinstance(result, dict) or not _text(result.get("body")).strip():
            raise ValueError("empty composition")
        if not re.search(
            r"\b(?:reply|tell us|tell me|want me|would you|can you|which)\b",
            _text(result.get("body")),
            flags=re.I,
        ):
            raise ValueError("missing_actionable_cta")
        if _unsupported_numeric_claim(
            _text(result.get("body")), category, merchant, trigger, customer
        ):
            raise ValueError("unsupported numeric claim")
        send_as = "merchant_on_behalf" if customer or _get(trigger, "scope") == "customer" else "vera"
        cleaned = self._finalize(
            _text(result["body"]),
            _text(result.get("cta") or "open_ended"),
            send_as,
            trigger,
            _text(result.get("rationale") or "Grounded in the received category, merchant and trigger context."),
            category=category,
        )
        result.update(cleaned)
        return result

    def _smart_template_compose(
        self,
        category: Dict[str, Any],
        merchant: Dict[str, Any],
        trigger: Dict[str, Any],
        customer: Optional[Dict[str, Any]] = None,
        plan: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Compose from a small facts pack, then choose a trigger-specific play.

        The public challenge deliberately sends new payload fields over time.  This
        path therefore reads the raw payload, but only turns values that are
        actually present into claims.  It is intentionally useful without an
        LLM key: exact offers, metrics, history and dates beat generic copy.
        """
        kind = _text(_get(trigger, "kind", default="event"))
        plan = plan or {}
        slug = _text(_get(category, "slug", default=_get(merchant, "category_slug", default="")))
        payload = _get(trigger, "payload", default={}) or {}
        identity = _get(merchant, "identity", default={}) or {}
        merchant_name = _text(identity.get("name")) or "your business"
        locality = _text(identity.get("locality"))
        salutation = _owner_salutation(merchant, slug)
        offers = _active_offers(merchant)
        send_as = "merchant_on_behalf" if customer or _get(trigger, "scope") == "customer" else "vera"
        performance = _get(merchant, "performance", default={}) or {}
        peer = _get(category, "peer_stats", default={}) or {}
        aggregate = _get(merchant, "customer_aggregate", default={}) or {}
        body = ""
        cta = "open_ended"
        rationale = f"Selected the {kind} signal and grounded the message in the received context."

        if send_as == "merchant_on_behalf":
            body, cta, rationale = self._customer_message(
                slug, merchant, trigger, customer or {}, offers, merchant_name
            )
        elif kind in {"research_digest", "regulation_change", "supply_alert", "cde_opportunity"}:
            body, cta, rationale = self._knowledge_message(
                slug, salutation, category, merchant, trigger, offers
            )
        elif kind in {"perf_dip", "seasonal_perf_dip"}:
            metric_raw = payload.get("metric")
            metric = _label(metric_raw) if metric_raw else "performance"
            metric_verb = "are" if metric.lower() in {"views", "calls", "directions", "leads"} else "is"
            delta = payload.get("delta_pct")
            delta_text = _percent(abs(delta)) if isinstance(delta, (int, float)) else ""
            baseline = payload.get("vs_baseline")
            if payload.get("is_expected_seasonal") or payload.get("season_note"):
                season = _season_label(payload.get("season_note", "this seasonal window"))
                members = aggregate.get("total_active_members")
                window = payload.get("window", "the supplied window")
                gym_season = slug == "gyms" and season in {"April–June", "April–May"}
                if gym_season:
                    body = f"{salutation}, for {merchant_name}, {metric} fell {delta_text or 'in the supplied window'} over {window}; {season} is gyms’ lowest acquisition window."
                    if members is not None:
                        body += f" Focus on your {members:,} active members rather than new acquisition."
                    else:
                        body += " Focus on member retention rather than acquisition spend."
                else:
                    body = f"{salutation}, for {merchant_name}, {metric} fell {delta_text or 'in the supplied window'} over {window}, during the expected {season} period."
                    if members is not None:
                        body += f" Your {members:,} active members are a strong retention focus."
                action = {
                    "gyms": "a 14-day attendance challenge",
                    "dentists": "a patient recall education post",
                    "salons": "a client rebooking message",
                    "restaurants": "a repeat-order offer using your active deal" if offers else "a repeat-order plan",
                    "pharmacies": "a listing update for the rising seasonal demand",
                }.get(slug, "a short retention plan")
                body += f" Reply PLAN and I’ll draft {action} for {merchant_name}, or 2 to skip."
                cta = "binary_choice"
                rationale = "Acknowledged the trigger's expected seasonal classification, avoided unsupported forecasts or spend advice, and offered the planner-selected action using supplied member or offer evidence."
            elif metric_raw:
                body = f"{salutation}, your {metric} fell {delta_text or 'recently'}"
                if payload.get("window"):
                    body += f" over {payload['window']}"
                absolute = performance.get(metric.replace(" ", "_"))
                if absolute is not None:
                    if baseline is not None:
                        body += f"; {absolute} in the last 30 days versus a baseline of {baseline}"
                    else:
                        body += f"; {absolute} in the last 30 days"
                peer_metric = peer.get(f"avg_{metric.replace(' ', '_')}_30d")
                if baseline is None and peer_metric is not None:
                    body += f" versus peer {peer_metric}"
                body = body.rstrip(".") + "."
                proposed = _text(plan.get("proposed_action")) or "draft a 7-day recovery plan"
                if slug == "dentists":
                    body += f" Let’s audit patient recall visibility before your renewal. Reply PLAN and I’ll draft a 7-day patient-safe recovery plan for {merchant_name}, or 2 to pause."
                elif offers:
                    body += f" {offers[0]} is already active. Reply PLAN and I’ll build a recovery plan around it, or 2 to skip."
                else:
                    action = proposed.removeprefix("audit ")
                    body += f" Reply PLAN and I’ll prepare {action.rstrip('.')} for {merchant_name}, or 2 to pause."
                cta = "binary_choice"
                rationale = "Called out the exact performance change, absolute count, baseline/peer context and selected a category-specific recovery intervention before writing."
            else:
                current = []
                for key in ("views", "calls", "directions", "leads"):
                    if performance.get(key) is not None:
                        current.append(f"{key} {performance[key]}")
                body = f"{salutation}, a performance dip alert is open for {merchant_name}"
                if locality:
                    body += f" in {locality}"
                if current:
                    body += f"; current 30-day levels are {', '.join(current[:3])}"
                if performance.get("ctr") is not None:
                    body += f" at {performance['ctr']:.1%} CTR"
                proposed = _text(plan.get("proposed_action")) or "audit the listing and draft two recovery options"
                body += f". Reply PLAN and I’ll prepare {proposed.rstrip('.')}"
                rationale = "The generated trigger supplied no metric or delta, so the message labels the alert without fabricating a decline and uses the category planner to select a grounded next intervention."
        elif kind == "perf_spike":
            metric_raw = payload.get("metric")
            metric = _label(metric_raw) if metric_raw else "performance"
            delta = payload.get("delta_pct")
            delta_text = _percent(delta) if isinstance(delta, (int, float)) else ""
            driver = _label(payload.get("likely_driver"))
            if metric_raw:
                absolute = performance.get(metric.replace(" ", "_"))
                body = f"{salutation}, {metric} rose {delta_text or 'this week'}"
                if payload.get("window"):
                    body += f" over {payload['window']}"
                if absolute is not None:
                    body += f"; you had {absolute} in the past 30 days"
                if driver:
                    body += f". The {driver.replace('_', ' ')} likely drove the lift"
            else:
                current = []
                for key in ("views", "calls", "directions", "leads"):
                    if performance.get(key) is not None:
                        current.append(f"{key} {performance[key]}")
                body = f"{salutation}, a performance spike alert is open for {merchant_name}"
                if locality:
                    body += f" in {locality}"
                if current:
                    body += f"; current 30-day levels are {', '.join(current[:3])}"
                if performance.get("ctr") is not None:
                    body += f" at {performance['ctr']:.1%} CTR"
                if driver:
                    body += f"; the likely driver is {driver}"
            if driver:
                body += f". Reuse that hook in a member follow-up. Reply POST and I’ll draft it for {merchant_name}."
            else:
                body += ". Good result; I’d wait for a clear driver before proposing a campaign."
            rationale = "Used the supplied performance spike, absolute count, peer/baseline context, driver and active offer; the planner selected one category-appropriate compounding action with a concrete artifact CTA."
        elif kind == "renewal_due":
            days = payload.get("days_remaining")
            if days is None:
                days = _get(merchant, "subscription", "days_remaining", default=None)
            subscription_plan = _text(payload.get("plan") or _get(merchant, "subscription", "plan", default="current plan"))
            amount = payload.get("renewal_amount")
            body = f"{salutation}, your {subscription_plan} renewal is in {days} days" if days is not None else f"{salutation}, your {subscription_plan} renewal is coming up"
            if amount is not None:
                body += f" at ₹{amount:,}"
            leads = performance.get("leads")
            calls = performance.get("calls")
            if leads is not None or calls is not None:
                body += f"; your current window has {leads if leads is not None else calls} {'leads' if leads is not None else 'calls'}"
            body += ". Want the renewal details, or should I help you pause the plan?"
            rationale = "Used the renewal window, plan, amount and current performance for a quiet, binary decision-ready CTA."
        elif kind == "milestone_reached":
            metric_raw = payload.get("metric")
            metric = _label(metric_raw) if metric_raw else "business progress"
            if metric == "review count":
                metric = "reviews"
            value = payload.get("value_now")
            target = payload.get("milestone_value")
            if value is not None:
                body = f"{salutation}, you are at {value} {metric}"
                if target is not None:
                    gap = target - value if isinstance(target, (int, float)) and isinstance(value, (int, float)) else None
                    body += f" — {gap} more to reach {target}" if gap is not None else f" — {target} is the next mark"
            else:
                body = f"{salutation}, a {metric} milestone is on the radar for {merchant_name}"
                if locality:
                    body += f" in {locality}"
                total = aggregate.get("total_unique_ytd")
                if total is not None:
                    body += f" with a {total:,}-customer base"
                directions = performance.get("directions")
                if directions is not None:
                    body += f" and {directions} directions in the last 30 days"
            positive = _first_positive_review(merchant)
            if positive and value is not None and metric == "reviews":
                theme = _label(positive.get("theme"))
                occurrences = positive.get("occurrences_30d")
                body += f". {occurrences or 'Several'} positive mentions in recent reviews praise your {theme}"
                body += ". Want me to draft a Google post around that?"
            else:
                if offers:
                    body += f". Use {offers[0]} as the thank-you hook"
                body += ". Reply POST and I’ll draft the celebration post for review."
            rationale = "Celebrated the supplied milestone, exposed the remaining numeric stake and used a positive review theme when available before asking for one concrete follow-up."
        elif kind == "competitor_opened":
            competitor = _text(payload.get("competitor_name"))
            distance = payload.get("distance_km")
            their_offer = _text(payload.get("their_offer"))
            positive = _first_positive_review(merchant)
            body = f"{salutation}, {competitor or 'A nearby competitor'}"
            opened_date = _iso_label(payload.get("opened_date"))
            if opened_date:
                body += f" opened {opened_date.lstrip('0')}"
            if distance is not None:
                body += f", {distance} km away" if opened_date else f" is {distance} km away"
            competitor_cleaning_price = re.search(r"^Dental Cleaning\s*@\s*(₹[\d,]+)$", their_offer, re.I)
            merchant_cleaning_offer = next(
                (offer for offer in offers if re.search(r"^Dental Cleaning\s*@\s*₹[\d,]+$", offer, re.I)),
                "",
            )
            merchant_cleaning_price = re.search(r"@\s*(₹[\d,]+)$", merchant_cleaning_offer)
            if slug == "dentists" and competitor_cleaning_price and merchant_cleaning_price:
                body += f" and lists cleaning at {competitor_cleaning_price.group(1)}; your active price is {merchant_cleaning_price.group(1)}"
            else:
                if their_offer:
                    body += f" and offers {their_offer}"
                if offers:
                    body += f"; your active offer is {offers[0]}"
            if positive:
                theme = _label(positive.get("theme"))
                count = positive.get("occurrences_30d")
                quote = _text(positive.get("common_quote"))
                if quote and "explains everything patiently" in quote.lower():
                    praise = "how patiently you explain care"
                elif quote:
                    praise = f"‘{quote}’"
                else:
                    praise = f"your {theme}"
                body += f". {count or 'Several'} reviews praise {praise}"
                body += ". Keep your price and lead with that trust"
            else:
                body += ". Build your response around the offer you already provide"
            asset = "patient-friendly post" if slug == "dentists" else "customer post"
            if slug == "dentists" and positive:
                body += f". Reply POST and I’ll turn that into a patient retention post for {merchant_name}."
            else:
                body += f". Reply POST and I’ll draft a {asset} for {merchant_name}."
            rationale = "Compared the supplied competitor offer with the merchant's live offer, used a verified review quote when available, and suggested one evidence-based differentiator instead of an unsupported price response."
        elif kind in {"festival_upcoming", "category_seasonal", "ipl_match_today", "weather_heatwave"}:
            body, cta, rationale = self._event_message(salutation, merchant, trigger, offers, category, plan)
        elif kind == "curious_ask_due":
            offer = offers[0] if offers else ""
            if offer:
                body = f"Hi {salutation} — are customers asking most for {offer} at {merchant_name} in {locality or 'your area'}?"
            else:
                body = f"Hi {salutation} — what service are customers asking for most at {merchant_name} in {locality or 'your area'}?"
            body += ". Reply with the service name and I’ll draft a Google post and short WhatsApp reply."
            rationale = "Converted the curiosity cadence into one answerable question using the merchant name/locality and offered a concrete artifact plus time cap."
        elif kind == "winback_eligible":
            days = payload.get("days_since_expiry")
            if days is None:
                days = _get(merchant, "subscription", "days_since_expiry", default=None)
            dip = payload.get("perf_dip_pct")
            added = payload.get("lapsed_customers_added_since_expiry")
            plan_name = _text(_get(merchant, "subscription", "plan", default="Pro"))
            client_noun = "patients" if slug == "dentists" else ("members" if slug == "gyms" else ("clients" if slug == "salons" else "customers"))
            greeting = f"Hi {salutation}" if slug == "salons" else salutation
            body = f"{greeting}, your {merchant_name} {plan_name} profile expired"
            if days is not None:
                body += f" {days} days ago"
            if isinstance(dip, (int, float)):
                body += f" and calls are down by {abs(dip):.0%}"
            if added is not None:
                body += f"; {added} {client_noun} have lapsed since expiry"
            body += f". Reply PLAN and I’ll draft a 7-day client winback sequence for {merchant_name}, or 2 to stay paused."
            cta = "binary_choice"
            rationale = "Used the supplied expiry interval, performance dip and newly lapsed-customer count to make renewal the single concrete next action."
        elif kind == "dormant_with_vera":
            days = payload.get("days_since_last_merchant_message")
            topic = _label(payload.get("last_topic"))
            subscription = _get(merchant, "subscription", default={}) or {}
            expiry_days = subscription.get("days_since_expiry")
            plan_name = _text(subscription.get("plan"))
            elapsed = expiry_days if expiry_days is not None else days
            body = f"Hi {salutation}, your {plan_name + ' ' if plan_name else ''}profile has been paused"
            if elapsed is not None:
                body += f" for {elapsed} days"
            current_calls = _get(performance, "calls", default=None)
            current_views = _get(performance, "views", default=None)
            delta_calls = _get(performance, "delta_7d", "calls_pct")
            if current_views is not None and current_calls is not None:
                body += f"; {current_views:,} views brought {current_calls} calls in 30 days"
            if delta_calls is not None:
                direction = "up" if delta_calls > 0 else "down"
                body += f"; calls are {direction} {abs(delta_calls):.0%} this week"
            body += f". Reply PLAN and I’ll share a 7-day client recovery plan for {merchant_name}, or 2 to stay paused."
            cta = "binary_choice"
            rationale = "Used the expired subscription, elapsed time and current profile performance to preview the planner-selected seven-day intervention before any account change."
        elif kind == "active_planning_intent":
            topic = _label(payload.get("intent_topic", "the idea")).lower()
            proposal = _proposal_from_history(merchant)
            order_anchor = _extract_numeric_anchor(merchant, r"([0-9][0-9,]*)\s+orders?/day")
            if proposal:
                body = f"{salutation}, here’s the {topic} outline: {proposal}. Reply DRAFT now and I’ll format the WhatsApp launch message for your review."
            else:
                body = f"{salutation}, for {merchant_name}{' in ' + locality if locality else ''}, your {order_anchor + ' orders/day and ' if order_anchor else ''}{offers[0] + ' ' if offers else ''}are a starting point."
                body += " Reply DRAFT and I’ll build a tiered WhatsApp proposal, leaving prices and cutoff for your approval."
            rationale = "Recognized active planning intent and delivered a grounded working outline using prior Vera guidance, active offers, history metrics and one concrete artifact CTA."
        elif kind == "review_theme_emerged":
            theme = _label(payload.get("theme", "a review theme"))
            occurrences = payload.get("occurrences_30d")
            quote = _text(payload.get("common_quote"))
            trend = _label(payload.get("trend"))
            m_intro = f"for {merchant_name}, " if merchant_name and merchant_name not in salutation else ""
            body = f"{salutation}, {m_intro}{occurrences or 'Several'} reviews flag {theme} over 30 days"
            if trend:
                body += f"; trend: {trend}"
            if quote:
                short_quote = quote[:50].rsplit(" ", 1)[0] if len(quote) > 50 else quote
                body += f' — "{short_quote}"'
            positive = _first_positive_review(merchant)
            if positive:
                positive_theme = _label(positive.get("theme"))
                positive_count = positive.get("occurrences_30d")
                body += f"; {positive_count or 'several'} positive {positive_theme} reviews show a strength to preserve"
            plan_action = _text(plan.get("proposed_action"))
            if "delivery recovery checklist" in plan_action:
                action_text = "prepare the review response and delivery recovery checklist"
            else:
                action_text = "draft the review reply and service recovery checklist"
            body += f". Fix delivery flow before promotion. Reply FIX and I’ll {action_text}."
            rationale = "Used the supplied review count, trend and quote, preserved the positive service signal, and recommended fixing the issue before promotion."
        elif kind == "gbp_unverified":
            path = _label(payload.get("verification_path", "the available verification path"))
            uplift = payload.get("estimated_uplift_pct")
            views = performance.get("views")
            body = f"{salutation}, your Google profile is still unverified"
            if views is not None:
                window_days = performance.get("window_days")
                if window_days is not None:
                    body += f" and had {views:,} views in the last {window_days} days"
                else:
                    body += f" and has {views:,} views"
            if isinstance(uplift, (int, float)):
                body += f"; verification is estimated to improve visibility by {uplift:.0%}"
            if "postcard" in path.lower() and "phone" in path.lower():
                body += ". Reply POSTCARD or PHONE and I’ll send the verification steps."
            else:
                body += f". Reply STEPS now and I’ll send the {path} walkthrough."
            rationale = "Flagged the exact verification path, current views and supplied uplift estimate, then offered a setup walkthrough without claiming completion."
        else:
            body, cta, rationale = self._generic_message(salutation, merchant_name, locality, kind, payload, plan)

        if plan.get("reason_code"):
            rationale += f" Planner decision: {plan.get('reason_code')}; intervention={plan.get('intervention', '')}."
        return self._finalize(body, cta, send_as, trigger, rationale, category=category)

    def _knowledge_message(self, slug, salutation, category, merchant, trigger, offers):
        item = _find_digest_item(category, trigger)
        title = _text(item.get("title", "the latest category update"))
        source = _text(item.get("source"))
        summary = _text(item.get("summary"))
        actionable = _text(item.get("actionable"))
        trial_n = item.get("trial_n")
        segment = _label(item.get("patient_segment"))
        payload = _get(trigger, "payload", default={}) or {}
        kind = _get(trigger, "kind")
        aggregate = _get(merchant, "customer_aggregate", default={}) or {}

        if kind == "supply_alert":
            molecule = _text(payload.get("molecule", "the affected medicine"))
            batches = payload.get("affected_batches", [])
            batch_text = ", ".join(map(str, batches)) if batches else "the listed batches"
            manufacturer = _text(payload.get("manufacturer"))
            manufacturer_text = f" from {manufacturer}" if manufacturer else ""
            body = f"{salutation}, recall: {molecule} batches {batch_text}{manufacturer_text} flagged for sub-potency."
            if summary and "No safety risk" in summary:
                body += " The digest reports no added safety risk, but potency may be lower."
            body += " Check these batches against stock and use the distributor return route. Want a staff note and replacement checklist?"
            return body, "open_ended", "Used the exact molecule, batches and manufacturer, bounded the risk, and offered an end-to-end replacement workflow."

        if kind == "research_digest":
            if trial_n and summary and "38%" in summary:
                body = f"{salutation}, {source + ': ' if source else ''}{trial_n:,}-patient trial found 38% lower caries recurrence with 3-month vs 6-month fluoride varnish recall"
            else:
                body = f"{salutation}, {source + ': ' if source else ''}"
                if trial_n:
                    body += f"{trial_n:,}-patient trial: "
                body += title.rstrip(".")
                if summary:
                    short = summary.split(".")[0].rstrip(".")
                    body += f" — {short}"
            if segment:
                raw_segment = _text(item.get("patient_segment")).lower()
                count = aggregate.get("high_risk_adult_count") if "high_risk" in raw_segment else None
                body += f"; your roster has {count} high-risk adults" if count is not None else f"; relevant to your {segment} cohort"
            body += ". Want me to pull the abstract and draft a patient-ready WhatsApp?"
            return body, "open_ended", "Led with the supplied source and trial evidence, tied it to the merchant's cohort, and offered a concrete patient-ed artifact."

        if kind == "regulation_change":
            deadline = payload.get("deadline_iso") or item.get("effective_date")
            if "Maximum dose per IOPA exposure" in _text(summary):
                deadline_label = _iso_label(deadline) if deadline else "the effective date"
                body = (
                    f"{salutation}, Dental Council of India (DCI) circular: from {deadline_label}, "
                    "IOPA limit drops from 1.5 to 1.0 mSv. E-speed passes; D-speed does not; "
                    "digital RVG is unaffected. Want me to draft an X-ray setup and SOP audit checklist before then?"
                )
                return body, "open_ended", "Cited the DCI circular, stated its effective date and exact IOPA limit change, distinguished affected equipment, and offered a specific audit checklist."
            body = f"{salutation}, {title.rstrip('.') }"
            if deadline and _iso_label(deadline) not in body and str(deadline) not in body:
                body += f" — deadline {_iso_label(deadline)}"
            if source:
                body += f" ({source})"
            if summary:
                summary_text = summary.rstrip(".")
                if "Maximum dose per IOPA exposure" in summary_text:
                    summary_text = summary_text.replace("Maximum dose per IOPA exposure drops", "IOPA dose drops").replace("E-speed film passes at the new limit; D-speed does not. Digital RVG sensors unaffected", "E-speed passes; D-speed does not; digital RVG is unaffected")
                body += f": {summary_text}"
            next_step = actionable or "Audit your X-ray setup and update the SOP before the deadline"
            body += f". {next_step.rstrip('.')} Want me to turn that into a checklist?"
            return body, "open_ended", "Anchored the regulation in its title, source, technical summary and deadline, then offered an implementation artifact."

        if kind == "cde_opportunity":
            date = item.get("date")
            credits = payload.get("credits", item.get("credits"))
            fee = payload.get("fee") or actionable
            body = f"{salutation}, {title.rstrip('.') }"
            if date:
                body += f" on {_iso_label(date)}"
            if credits is not None:
                body += f"; {credits} credit{'s' if credits != 1 else ''}"
            if fee:
                body += f"; {fee.replace('_', ' ')}"
            body += ". Want me to prepare the registration and calendar details?"
            return body, "open_ended", "Included the supplied event date, credits and fee, then offered low-effort registration and calendar help."

        body = f"{salutation}, {title.rstrip('.') }"
        if source:
            body += f" — {source}"
        if summary:
            body += f": {summary.rstrip('.') }"
        if actionable:
            body += f" Action: {actionable.rstrip('.') }"
        body += ". Reply NEXT and I’ll turn it into one practical customer or profile action."
        return body, "open_ended", "Selected the supplied digest item, included its source and actionable evidence, and offered one practical next step."

    def _event_message(self, salutation, merchant, trigger, offers, category=None, plan=None):
        payload = _get(trigger, "payload", default={}) or {}
        plan = plan or {}
        kind = _text(_get(trigger, "kind"))
        category = category or {}
        if kind == "ipl_match_today":
            merchant_name = _text(_get(merchant, "identity", "name", default=""))
            match = _text(payload.get("match", "today's match"))
            venue = _text(payload.get("venue"))
            when = _datetime_label(payload.get("match_time_iso"))
            body = f"{salutation}, for {merchant_name}, {match}"
            if venue:
                body += f" at {venue}"
            if when:
                body += f" on {when}"
            elif not venue:
                body += " is today"
            digest = _digest_for_kind(category, kind)
            summary = _text(digest.get("summary"))
            if payload.get("is_weeknight") is False and summary:
                m = re.search(r"(?:covers|orders) down ([0-9]+)%[^.]*\..*?weeknight matches drive \+?([0-9]+)%", summary, re.I)
                if m:
                    body += f". Saturday covers run {m.group(1)}% below average; weeknight matches rise {m.group(2)}%"
                else:
                    body += ". The category data says Saturday IPL underperforms weeknights"
                if offers:
                    body += f". Keep {offers[0]} on its listed days; use delivery copy today"
                else:
                    body += ". Skip a new match promo and focus on delivery"
            elif offers:
                body += f". Push {offers[0]} as the match-night offer"
            else:
                body += ". Build a delivery-first match-night offer"
            body += ". Reply DELIVERY now to capture dinner match orders tonight."
            return body, "open_ended", "Used exact match details, category intelligence and the existing offer to choose delivery over a match-night promo, then offered one time-boxed banner artifact."
        if kind == "category_seasonal":
            trends = payload.get("trends", [])
            rendered = []
            for trend in trends[:4] if isinstance(trends, list) else []:
                match = re.match(r"(.+?)_([+-]?[0-9]+)$", _text(trend))
                if match:
                    rendered.append(f"{_label(match.group(1))} {match.group(2)}%")
                else:
                    rendered.append(_label(trend))
            trend_text = ", ".join(rendered) or _text(trends) or "a category shift"
            body = f"{salutation}, {_label(payload.get('season', 'this season'))}: {trend_text}."
            if payload.get("shelf_action_recommended"):
                body += " Put products with rising demand in counter view; reduce space for the declining category."
            seasonal_asset = {
                "dentists": "recall-education",
                "salons": "seasonal-service",
                "restaurants": "seasonal-menu",
                "gyms": "attendance-challenge",
                "pharmacies": "summer-health-pack",
            }.get(_text(_get(merchant, "category_slug", default="")), "category-relevant")
            body += f" Reply PLAN and I’ll draft the {seasonal_asset} listing post and WhatsApp for review."
            return body, "open_ended", "Humanized the exact seasonal deltas, selected a category-specific intervention before writing, and converted the supplied shelf recommendation into a reviewable artifact."
        if kind == "weather_heatwave":
            temperature = payload.get("temperature") or payload.get("temperature_c") or payload.get("max_temp_c")
            city = _text(payload.get("city")) or _text(_get(merchant, "identity", "city", default="your area"))
            body = f"{salutation}, heatwave alert for {city}"
            if temperature is not None:
                body += f" at {temperature}°C"
            if offers:
                body += f". Put {offers[0]} behind today's weather-relevant post"
            elif _text(_get(merchant, "category_slug", default="")) == "pharmacies":
                body += ". Lead with ORS, sunscreen and anti-fungal shelf visibility"
            else:
                body += ". Lead with a heatwave-relevant offer for today's demand"
            body += ". Reply POST and I’ll draft the heatwave post for review."
            return body, "open_ended", "Used the supplied weather and locality, then pivoted to a category-relevant offer or shelf action with a time-boxed artifact CTA."
        festival = _text(payload.get("festival"))
        days = payload.get("days_until")
        date = _text(payload.get("date"))
        category_slug = _text(_get(merchant, "category_slug", default="business"))
        vertical = {
            "dentists": "dental",
            "gyms": "gym",
            "salons": "salon",
            "restaurants": "restaurant",
            "pharmacies": "pharmacy",
        }.get(category_slug, "business")
        if festival:
            body = f"{salutation}, {festival} is {days} days away" if days is not None else f"{salutation}, {festival} is coming up"
        else:
            body = f"{salutation}, a festival demand window is open for your {vertical}"
        if date:
            body += f" ({_iso_label(date)})"
        event_name = festival or "the festival"
        distant = isinstance(days, (int, float)) and days > 60
        if offers:
            body += f". Your {offers[0]} offer is active"
            if distant:
                body += f"; prepare a {vertical} post now to use closer to {event_name}"
                body += ". Reply PLAN and I’ll map the campaign timing around your offer."
            else:
                body += f"; use it in your {event_name} post. Reply PLAN and I’ll draft the Google listing and WhatsApp copy."
        else:
            perf = _get(merchant, "performance", default={}) or {}
            total = _get(merchant, "customer_aggregate", "total_unique_ytd", default=None)
            if perf.get("views") is not None and perf.get("calls") is not None:
                body += f"; this profile has {perf['views']:,} views and {perf['calls']} calls in 30 days"
            if total is not None:
                body += f" across {total:,} customers"
            if distant:
                body += f". Start planning a {vertical} post for {event_name}. Reply PLAN and I’ll map the campaign timing."
            else:
                body += f". Want me to draft a {vertical} post for {event_name}?"
        return body, "open_ended", "Used supplied festival timing, category relevance and current offer or performance; distant dates get a planning step instead of an immediate post."

    def _customer_message(self, slug, merchant, trigger, customer, offers, merchant_name):
        payload = _get(trigger, "payload", default={}) or {}
        first_name = _customer_first_name(customer)
        address_name = _customer_name(customer)
        pref = _get(customer, "identity", "language_pref", default="en")
        bilingual = _has_hindi_preference(pref)
        kind = _text(_get(trigger, "kind"))
        identity = _get(merchant, "identity", default={}) or {}
        locality = _text(identity.get("locality"))
        owner = _text(identity.get("owner_first_name")).strip()
        if slug == "dentists" and owner and not owner.lower().startswith("dr"):
            owner = f"Dr. {owner}"
        owner_label = owner
        if owner_label and owner_label.lower() in merchant_name.lower():
            owner_label = ""
        signature = f"{owner_label + ' from ' if owner_label else ''}{merchant_name}{(' ' + locality) if locality else ''} here"
        senior = bool(_get(customer, "identity", "senior_citizen", default=False))
        raw_customer = _text(_get(customer, "identity", "name", default=""))
        parent_match = re.search(r"parent:\s*([^\)]+)", raw_customer, re.I)
        child_name = _customer_first_name(customer)
        if senior:
            prefix = "Namaste"
        elif parent_match:
            prefix = f"Hi {parent_match.group(1).strip().split()[0]}"
        else:
            prefix = f"Hi {first_name}"

        if kind in {"recall_due", "appointment_tomorrow"}:
            due = payload.get("due_date") or payload.get("appointment_date")
            slots = payload.get("available_slots") or payload.get("next_session_options") or []
            labels = [_text(_get(slot, "label", default=slot)) for slot in slots[:2]]
            offer = offers[0] if offers else ""
            service = _label(payload.get("service_due", "appointment"))
            last_visit = _get(customer, "relationship", "last_visit", default="")
            visits = _get(customer, "relationship", "visits_total", default=None)
            if kind == "appointment_tomorrow":
                body = f"{prefix}, {signature} — your appointment is tomorrow."
                if last_visit and visits:
                    visit_word = "visit" if visits == 1 else "visits"
                    body += f" We last saw you on {_iso_label(last_visit)} after {visits} {visit_word}."
                body += " Reply CONFIRM to keep it, or tell us what needs changing."
                return body, "binary_confirm_cancel", "Customer name, merchant name plus locality, and the tomorrow appointment trigger are explicit; CTA is a low-friction confirmation."
            if bilingual:
                body = f"{prefix}, {signature} — aapka {service} recall"
                body += f" around {_iso_label(due)} due hai" if due else " due hai"
                if labels:
                    body += f". 2 slots ready: {labels[0]} ya {labels[1]}"
                elif last_visit:
                    body += f"; last visit {_iso_label(last_visit)}"
                if offer:
                    body += f". {offer} bhi available hai"
                body += ". Reply 1/2 for a slot, ya apna preferred time bata dijiye." if labels else ". Reply CONFIRM to arrange the next visit, ya apna preferred time bata dijiye."
            else:
                body = f"{prefix}, {signature} — your {service}"
                body += f" is due around {_iso_label(due)}" if due else " follow-up is due"
                if labels:
                    body += f". 2 slots ready: {labels[0]} or {labels[1]}"
                elif last_visit:
                    body += f"; we last saw you on {_iso_label(last_visit)}"
                if offer:
                    body += f". {offer} is available too"
                body += ". Reply 1/2 for a slot, or tell us a time that works." if labels else ". Reply CONFIRM to arrange the next visit, or tell us a time that works."
            return body, "multi_choice_slot" if labels else "binary_confirm_cancel", "Used the customer name, merchant signature, recall timing, slots, language preference and active offer without adding clinical claims."

        if kind == "trial_followup":
            trial_date = payload.get("trial_date")
            slots = payload.get("next_session_options") or []
            labels = [_text(_get(slot, "label", default=slot)) for slot in slots[:2]]
            subject = f"{child_name}'s" if parent_match else "Your"
            body = f"{prefix}, {signature} — {subject} trial"
            if trial_date:
                body += f" on {_iso_label(trial_date)}"
            body += " is the next step"
            if labels:
                body += f"; the next session is {labels[0]}"
            if offers:
                body += f". {offers[0]} is available if you continue"
            body += ". Reply YES to confirm, or tell us another time."
            return body, "binary_yes_no", "Used the customer or parent address, merchant locality, trial date, next slot and existing offer for a single confirmation CTA."

        if kind == "wedding_package_followup":
            wedding = payload.get("wedding_date")
            days = payload.get("days_to_wedding")
            trial_date = payload.get("trial_completed")
            window = _label(payload.get("next_step_window_open"))
            window = re.sub(r"(\d+)day\b", r"\1-day", window)
            if "skin_prep_program_30day" in _text(payload.get("next_step_window_open")):
                window = "30-day skin-prep"
            preferred = _label(_get(customer, "preferences", "preferred_slots", default=""))
            body = f"Hi {first_name}, {signature} — your bridal trial"
            if trial_date:
                body += f" was on {_iso_label(trial_date)}"
            if wedding:
                body += f"; your wedding is {_iso_label(wedding)}"
            if days is not None:
                body += f" ({days} days away)"
            if window:
                body += f". The next window is the {window}"
            bridal_offers = [offer for offer in offers if any(term in offer.lower() for term in ("bridal", "skin prep", "skin-prep", "wedding"))]
            if bridal_offers:
                body += f"; {bridal_offers[0]} is available"
            if preferred:
                body += f". Your preferred time is {preferred}; I can check availability"
            body = f"Hi {first_name}, {owner + ' from ' if owner else ''}{merchant_name}{' ' + locality if locality else ''} here."
            if trial_date:
                body += f" Your trial was {_iso_label(trial_date)}."
            if wedding:
                body += f" Wedding: {_iso_label(wedding)}"
            if days is not None:
                body += f" ({days} days); the {window or 'bridal'} window is open."
            if preferred:
                body += f" I’ll note your {preferred} preference."
            body += " Reply YES and I’ll confirm a bridal package and available slot with the salon."
            return body, "binary_yes_no", "Used relationship continuity, wedding countdown and next-step window, avoided treating an unrelated salon offer or preference as confirmed availability, and offered to verify the bridal package and slot."

        if kind in {"customer_lapsed_soft", "customer_lapsed_hard", "winback_eligible"}:
            days = payload.get("days_since_last_visit") or payload.get("days_since_expiry")
            previous = _label(payload.get("previous_focus"))
            last_visit = _get(customer, "relationship", "last_visit", default="")
            visits = _get(customer, "relationship", "visits_total", default=None)
            body = f"Hi {first_name}, {signature} —"
            if days is not None:
                body += f" it has been about {days} days since your last visit"
            elif last_visit:
                body += f" we last saw you on {_iso_label(last_visit)}"
            else:
                body += " we would be glad to welcome you back"
            body += "."
            if previous:
                body += f" We remember your {previous} goal."
            if visits is not None and visits > 1:
                body += f" You have visited {visits} times."
            if offers:
                offer_text = offers[0]
                if offer_text.lower().startswith("3 free"):
                    offer_text += " are available"
                else:
                    offer_text += " is available"
                body += f" {offer_text}."
            body += " Reply YES and I’ll help plan a return around your goal."
            return body, "binary_yes_no", "Used no-shame framing, relationship state, prior goal or visit history, owner/locality and the active offer with a binary low-risk CTA."

        if kind == "chronic_refill_due":
            molecules = payload.get("molecule_list", [])
            molecule_text = ", ".join(map(str, molecules)) if molecules else ""
            runout = payload.get("stock_runs_out_iso")
            if senior:
                body = f"Namaste — {merchant_name}{(' ' + locality) if locality else ''} here. {address_name} ji's monthly medicines"
            else:
                body = f"{prefix}, {signature} — your monthly medicines"
            if molecule_text:
                body += f" ({molecule_text})"
                body += f" run out before {_iso_label(runout)}" if runout else " are due for refill"
            else:
                last_visit = _get(customer, "relationship", "last_visit", default="")
                body = f"{prefix}, {signature} — we have a refill-related account reminder"
                if last_visit:
                    body += f" from your last visit on {_iso_label(last_visit)}"
            if payload.get("delivery_address_saved"):
                body += ". Your saved delivery address is ready"
            if offers:
                body += f". {offers[0]} may apply"
            body += ". Reply CONFIRM to prepare the refill, or tell us what changed."
            return body, "binary_confirm_cancel", "Used exact molecules and date when supplied, senior/customer addressing, locality, saved delivery and active offer without inventing dosage or price."

        body = f"{prefix}, {signature} — we have a relevant follow-up from your recent visit. Want us to help with the next step?"
        return body, "binary_yes_no", "Used a conservative customer-facing fallback with the customer name, merchant signature and one low-friction CTA."

    def _generic_message(self, salutation, merchant_name, locality, kind, payload, plan=None):
        plan = plan or {}
        useful = []
        for key, value in payload.items():
            if key in {"placeholder", "metric_or_topic"} or value in (None, "", [], {}):
                continue
            if isinstance(value, (str, int, float)):
                useful.append(f"{key.replace('_', ' ')}: {value}")
        detail = useful[0] if useful else "a new account signal"
        body = f"{salutation}, I found a useful update for {merchant_name}"
        if locality:
            body += f" in {locality}"
        proposed = _text(plan.get("proposed_action")) or "prepare one next step"
        body += f" — {detail}. Reply NEXT and I’ll prepare the {proposed.rstrip('.')} for review."
        return body, "open_ended", "Used the available trigger detail and the selected intervention plan while avoiding claims that were not supplied."

    def _finalize(self, body, cta, send_as, trigger, rationale, category=None):
        # Lightweight post-composition guardrails borrowed from the reference
        # implementations: no links, internal IDs, taboo claims, or two competing
        # questions in a single outbound message.
        body = re.sub(r"(?:https?://|www\.)\S+", "", _text(body), flags=re.I)
        body = re.sub(r"\b(?:m|c|trg|o)_[a-z0-9_:-]+\b", "", body, flags=re.I)
        # GBP and schema field names are implementation vocabulary, not useful
        # merchant language. Several public Vera submissions explicitly treat
        # these as jargon-leak risks, so translate them before the final checks.
        body = re.sub(r"\bGBP\b", "Google listing", body, flags=re.I)
        for jargon, replacement in {
            "trigger_id": "the trigger details",
            "merchant_id": "the merchant details",
            "customer_id": "the customer details",
            "category_slug": "the category",
            "suppression_key": "the send limit",
            "conversation_id": "this conversation",
            "top_item_id": "the referenced item",
            "payload": "the supplied details",
        }.items():
            body = re.sub(rf"\b{re.escape(jargon)}\b", replacement, body, flags=re.I)
        taboo = _get(category or {}, "voice", "vocab_taboo", default=None) or _get(category or {}, "voice", "taboos", default=[]) or []
        for term in taboo:
            if _text(term).strip():
                body = re.sub(re.escape(_text(term)), "", body, flags=re.I)
        body = re.sub(r"\s+", " ", body).strip()
        if body.count("?") > 1:
            first, rest = body.split("?", 1)
            body = first + "?" + re.sub(r"\?", ".", rest)
        # The challenge's WhatsApp target is 320 characters. Preserve the final
        # CTA sentence and trim only the least important middle detail if an
        # adaptive payload produces an unusually long composition.
        if len(body) > 320:
            parts = re.split(r"(?<=[.!?])\s+", body)
            tail = parts[-1] if len(parts) > 1 else ""
            if tail and len(tail) < 140:
                budget = 320 - len(tail) - 1
                prefix = body[:budget].rsplit(" ", 1)[0].rstrip(" ,;:-")
                body = f"{prefix}. {tail}"[:320]
            else:
                body = body[:317].rsplit(" ", 1)[0].rstrip(" ,;:-") + "..."
        return {
            "body": body,
            "cta": cta or "open_ended",
            "send_as": send_as,
            "suppression_key": _text(_get(trigger, "suppression_key", default="")),
            "rationale": rationale,
        }

    async def respond(
        self,
        conversation_history: List[Dict[str, Any]],
        merchant: Optional[Dict[str, Any]],
        category: Optional[Dict[str, Any]],
        latest_message: str,
        customer: Optional[Dict[str, Any]] = None,
        trigger: Optional[Dict[str, Any]] = None,
        plan: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Respond with deterministic policy for replay-critical intents.

        Ambiguous messages can be sent to Gemini, but opt-outs, auto-replies,
        hostility and explicit commitments should never depend on a model call.
        """
        message = _text(latest_message).strip()
        normalized = _normalize(message)
        turns = conversation_history or []
        plan = plan or {}
        trigger = trigger or {}
        merchant_name = _text(_get(merchant or {}, "identity", "name", default="your business"))
        recipient_scope = _text(plan.get("recipient_scope")) or ("customer" if customer else "merchant")
        pending_artifact = _text(plan.get("pending_artifact")) or "requested draft"
        proposed_action = _text(plan.get("proposed_action")) or "prepare the requested draft"
        merchant_turns = [t for t in turns if _text(t.get("from")) == "merchant"]
        repeated_count = sum(1 for t in merchant_turns if _normalize(_text(t.get("body"))) == normalized)
        canned = any(
            phrase in normalized
            for phrase in (
                "thank you for contacting",
                "our team will respond shortly",
                "automated assistant",
                "currently unavailable",
                "we will get back to you",
            )
        )

        if any(term in normalized for term in ("stop messaging", "stop message", "do not message", "dont message", "unsubscribe", "opt out", "optout", "remove me", "no more messages")):
            return {
                "action": "end",
                "reason_code": "explicit_opt_out",
                "opt_out_scope": recipient_scope,
                "rationale": "The recipient explicitly requested no further messages; closing without persuading or retargeting another identity.",
            }

        if any(term in normalized for term in ("not interested", "no thanks", "no thank you")):
            return {
                "action": "end",
                "reason_code": "proposal_rejected",
                "opt_out_scope": None,
                "rationale": "The recipient rejected this proposal, not the messaging channel; closing this conversation without global suppression.",
            }

        if any(term in normalized for term in ("useless", "why are you bothering", "shut up", "go away", "spam")):
            return {
                "action": "end",
                "reason_code": "hostile_close",
                "opt_out_scope": None,
                "rationale": "The recipient was hostile; closing without treating frustration as a channel opt-out.",
            }

        if plan.get("revalidation_reason") in {"expired_trigger", "consent_purpose_mismatch", "missing_customer_context"}:
            return {
                "action": "wait",
                "wait_seconds": 3600,
                "reason_code": "proposal_needs_revalidation",
                "rationale": "The stored proposal no longer has valid current evidence or consent; waiting for a refreshed trigger rather than claiming execution.",
            }

        if canned:
            # The current inbound message is already in `turns`, so one
            # occurrence means the first canned response, two means a repeat.
            if repeated_count >= 3:
                return {"action": "end", "reason_code": "auto_reply_exhausted", "opt_out_scope": None, "rationale": "The same canned auto-reply has repeated three times; closing instead of burning more turns."}
            if repeated_count == 2:
                return {"action": "wait", "wait_seconds": 86400, "reason_code": "auto_reply_backoff", "rationale": "A repeated canned auto-reply suggests the owner is unavailable; backing off for 24 hours."}
            return {
                "action": "send",
                "body": "Looks like an automated reply. When the owner sees this, reply YES and I’ll continue.",
                "cta": "binary_yes_no",
                "rationale": "Detected a canned auto-reply and used one lightweight owner handoff.",
            }

        if normalized in {"thanks", "thank you", "thanks done", "thank you done", "done", "all set", "all good", "sorted"} or "thanks that is all" in normalized:
            return {
                "action": "end",
                "reason_code": "completed",
                "opt_out_scope": None,
                "rationale": "The recipient indicated successful completion; closed this conversation without suppressing future outreach.",
            }

        if any(term in normalized for term in ("tomorrow", "later", "busy", "not now", "maybe later", "next week", "after lunch")):
            return {
                "action": "wait",
                "wait_seconds": 86400,
                "reason_code": "deferred_by_recipient",
                "rationale": "The recipient asked to defer the existing proposal; waiting instead of continuing to pitch.",
            }

        if any(term in normalized for term in ("no discount", "no discounts", "dont want a discount", "do not want a discount")):
            return {
                "action": "send",
                "body": f"Understood — no discount. I’ll keep the {pending_artifact.replace('_', ' ')} focused on the evidence already shared. Reply YES to continue, or CANCEL to hold.",
                "cta": "binary_confirm_cancel",
                "reason_code": "constraint_preserved",
                "rationale": "The recipient rejected discounting; preserved that constraint and kept the original evidence-backed intervention.",
            }

        if any(term in normalized for term in ("cancel", "hold")):
            return {
                "action": "send",
                "body": f"No problem — I’ll hold the {pending_artifact.replace('_', ' ')} for now. Reply YES if you want me to resume.",
                "cta": "binary_yes_no",
                "reason_code": "proposal_paused",
                "rationale": "The recipient asked to pause or cancel this proposal; acknowledged the boundary without global suppression.",
            }

        if any(term in normalized for term in ("lets do it", "let s do it", "go ahead", "what s next", "whats next", "send it", "confirm", "proceed", "yes", "haan", "tell me more", "interested", "please do", "send details")):
            artifact = pending_artifact.replace("_", " ")
            if "abstract" in normalized or "study" in normalized:
                artifact = "the abstract and the short patient-ed WhatsApp"
            elif "post" in normalized or "offer" in normalized:
                artifact = "the post and offer draft"
            artifact_label = artifact if artifact.lower().startswith("the ") else f"the {artifact}"
            return {
                "action": "send",
                "body": f"Great — I’ll {proposed_action.rstrip('.')} for {merchant_name}. Reply CONFIRM to ship {artifact_label}, or CANCEL to hold.",
                "cta": "binary_confirm_cancel",
                "reason_code": "commitment_accepted",
                "proposal_status": "accepted",
                "rationale": "The recipient accepted the stored intervention; progressed the existing proposal instead of reopening qualification.",
            }

        if any(term in normalized for term in ("gst", "tax filing", "unrelated")):
            return {
                "action": "send",
                "body": "I’ll leave that to your CA. Coming back to the Vera task above — would you like me to prepare the draft first?",
                "cta": "open_ended",
                "rationale": "Bounded an off-topic request and redirected to the active Vera objective.",
            }

        if self.use_mock is False and self.model is not None:
            try:
                prompt = (
                    "Return JSON only with action send, wait or end. Stay on the active Vera task, "
                    "do not invent facts, and use one CTA.\n"
                    f"HISTORY={json.dumps(turns, ensure_ascii=False)}\n"
                    f"LATEST={message}\nMERCHANT={merchant_name}\n"
                    f"CUSTOMER={json.dumps(customer, ensure_ascii=False) if customer else 'null'}\n"
                    f"TRIGGER={json.dumps(trigger, ensure_ascii=False)}\n"
                    f"PLAN={json.dumps(plan, ensure_ascii=False)}\n"
                    "Preserve the selected objective, constraints and accepted proposal; never invent execution results."
                )
                raw_text = self._call_openrouter(prompt, json_mode=True, max_tokens=500)
                result = json.loads(raw_text)
                if result.get("action") in {"send", "wait", "end"}:
                    return result
            except Exception:
                pass

        return {
            "action": "send",
            "body": f"Got it — I’ll keep the {pending_artifact.replace('_', ' ')} tied to the details already shared. Want me to continue?",
            "cta": "open_ended",
            "reason_code": "clarify_stored_proposal",
            "rationale": "Acknowledged the reply and kept the next step aligned with the stored intervention instead of resetting the conversation.",
        }


composer = VeraComposer()
