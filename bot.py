
import os
import re
import time
import hashlib
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Vera Merchant AI Assistant", version="1.0.0")
START_TIME = time.time()

# In-memory state is accepted for the challenge as long as the service is not restarted during test.
contexts: dict[tuple[str, str], dict[str, Any]] = {}
conversations: dict[str, list[dict[str, Any]]] = {}
sent_suppression_keys: set[str] = set()
closed_conversations: set[str] = set()

TEAM_NAME = os.getenv("TEAM_NAME", "Shashwat Vera Bot")
TEAM_MEMBERS = [m.strip() for m in os.getenv("TEAM_MEMBERS", "Shashwat Prajapati").split(",") if m.strip()]
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "your-email@example.com")
MODEL_NAME = os.getenv("MODEL_NAME", "deterministic-context-composer-v1")

VALID_SCOPES = {"category", "merchant", "customer", "trigger"}
AUTO_REPLY_PATTERNS = [
    r"thank you for contacting",
    r"thanks for contacting",
    r"we will respond shortly",
    r"our team will respond",
    r"automated assistant",
    r"auto[- ]?reply",
    r"business hours",
    r"aapki .{0,40}jaankari",
    r"team tak pahuncha",
]
STOP_PATTERNS = [r"\bstop\b", r"not interested", r"don't message", r"do not message", r"unsubscribe", r"band karo", r"mat bhejo"]
YES_PATTERNS = [r"\byes\b", r"go ahead", r"let'?s do", r"do it", r"confirm", r"send", r"please", r"haan", r"kar do", r"chalo", r"ok"]
OUT_OF_SCOPE_PATTERNS = [r"\bgst\b", r"tax filing", r"income tax", r"legal notice", r"personal loan"]

class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = []

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: str
    turn_number: int

@app.get("/v1/healthz")
async def healthz():
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid), _record in contexts.items():
        counts[scope] = counts.get(scope, 0) + 1
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": counts,
    }

@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": TEAM_NAME,
        "team_members": TEAM_MEMBERS,
        "model": MODEL_NAME,
        "approach": "Deterministic routing composer using category, merchant, trigger and optional customer context; includes auto-reply, stop-intent and action-intent handling.",
        "contact_email": CONTACT_EMAIL,
        "version": "1.0.0",
        "submitted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

@app.post("/v1/context")
async def push_context(body: ContextBody):
    if body.scope not in VALID_SCOPES:
        return {"accepted": False, "reason": "invalid_scope", "details": f"scope must be one of {sorted(VALID_SCOPES)}"}
    if body.version < 0:
        return {"accepted": False, "reason": "invalid_version", "details": "version must be non-negative"}
    key = (body.scope, body.context_id)
    current = contexts.get(key)
    if current and current["version"] >= body.version:
        return {"accepted": False, "reason": "stale_version", "current_version": current["version"]}
    contexts[key] = {"version": body.version, "payload": body.payload, "delivered_at": body.delivered_at}
    return {
        "accepted": True,
        "ack_id": f"ack_{safe_id(body.context_id)}_v{body.version}",
        "stored_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []
    for trigger_id in body.available_triggers[:20]:
        trigger = get_payload("trigger", trigger_id)
        if not trigger:
            continue
        suppression_key = trigger.get("suppression_key") or f"trigger:{trigger_id}"
        if suppression_key in sent_suppression_keys:
            continue
        merchant_id = trigger.get("merchant_id")
        merchant = get_payload("merchant", merchant_id) if merchant_id else None
        if not merchant:
            continue
        category = get_payload("category", merchant.get("category_slug"))
        if not category:
            continue
        customer = None
        customer_id = trigger.get("customer_id")
        if customer_id:
            customer = get_payload("customer", customer_id)
        composed = compose(category, merchant, trigger, customer)
        conv_id = conversation_id(merchant_id, trigger_id, customer_id)
        action = {
            "conversation_id": conv_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "send_as": composed["send_as"],
            "trigger_id": trigger_id,
            "template_name": template_name(trigger, customer),
            "template_params": build_template_params(composed["body"]),
            "body": composed["body"],
            "cta": composed["cta"],
            "suppression_key": composed["suppression_key"],
            "rationale": composed["rationale"],
        }
        actions.append(action)
        conversations.setdefault(conv_id, []).append({"from": "bot", "body": composed["body"], "trigger_id": trigger_id, "ts": body.now})
        sent_suppression_keys.add(suppression_key)
    return {"actions": actions}

@app.post("/v1/reply")
async def reply(body: ReplyBody):
    if body.conversation_id in closed_conversations:
        return {"action": "end", "rationale": "Conversation was already closed; not re-engaging."}
    msg = (body.message or "").strip()
    turns = conversations.setdefault(body.conversation_id, [])
    turns.append({"from": body.from_role, "body": msg, "ts": body.received_at, "turn_number": body.turn_number})

    if is_stop(msg):
        closed_conversations.add(body.conversation_id)
        return {"action": "end", "rationale": "Merchant/customer explicitly asked to stop or showed no interest; closing gracefully."}

    auto_count = count_recent_auto_replies(turns)
    if is_auto_reply(msg):
        if auto_count >= 3:
            closed_conversations.add(body.conversation_id)
            return {"action": "end", "rationale": "Detected repeated WhatsApp Business auto-replies three times; closing to avoid wasting turns."}
        if auto_count >= 2:
            return {"action": "wait", "wait_seconds": 86400, "rationale": "Same auto-reply repeated; waiting before retrying."}
        return {
            "action": "send",
            "body": "Looks like an auto-reply. When the owner/manager sees this, they can reply YES and I’ll continue from here.",
            "cta": "binary_yes_no",
            "rationale": "Detected canned WhatsApp Business auto-reply; one light prompt for a real decision-maker.",
        }

    if is_out_of_scope(msg):
        return {
            "action": "send",
            "body": "That part is outside what I can handle directly. Coming back to this task — reply YES and I’ll prepare the next draft/action from the context I already have.",
            "cta": "binary_yes_no",
            "rationale": "Politely declined unrelated request and redirected to the active Vera workflow.",
        }

    if is_yes(msg):
        return action_reply(body.conversation_id, body.merchant_id, body.customer_id)

    return {
        "action": "send",
        "body": "Got it. Share one detail and I’ll keep it simple: should I prepare the draft/action now? Reply YES to proceed or STOP to close.",
        "cta": "binary_yes_stop",
        "rationale": "Ambiguous but engaged reply; asking one binary next step without adding more qualification.",
    }

@app.post("/v1/teardown")
async def teardown():
    contexts.clear()
    conversations.clear()
    sent_suppression_keys.clear()
    closed_conversations.clear()
    return {"accepted": True, "reason": "state_cleared"}

# ----------------------------- composer -----------------------------
def compose(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: Optional[dict[str, Any]] = None) -> dict[str, str]:
    kind = trigger.get("kind", "unknown")
    if customer or trigger.get("scope") == "customer":
        body, cta = compose_customer(category, merchant, trigger, customer or {})
        send_as = "merchant_on_behalf"
    else:
        body, cta = compose_merchant(category, merchant, trigger)
        send_as = "vera"
    return {
        "body": clean(body),
        "cta": cta,
        "send_as": send_as,
        "suppression_key": trigger.get("suppression_key", f"{kind}:{merchant.get('merchant_id', 'unknown')}") or "",
        "rationale": rationale_for(kind, merchant, trigger, customer),
    }

def compose_merchant(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    ident = merchant.get("identity", {})
    first = ident.get("owner_first_name") or short_business_name(ident.get("name", "there"))
    name = ident.get("name", "your business")
    locality = ident.get("locality", "your locality")
    perf = merchant.get("performance", {})
    payload = trigger.get("payload", {}) or {}
    active_offer = first_active_offer(merchant) or category_offer(category)

    if kind in {"research_digest", "cde_opportunity", "regulation_change"}:
        item = find_digest_item(category, payload.get("top_item_id") or payload.get("digest_item_id"))
        return message_digest(first, merchant, category, trigger, item), "open_ended"

    if kind in {"perf_dip", "seasonal_perf_dip"}:
        metric = payload.get("metric", "performance")
        delta = fmt_pct(payload.get("delta_pct") or perf.get("delta_7d", {}).get(f"{metric}_pct"))
        views = perf.get("views")
        calls = perf.get("calls")
        seasonal = payload.get("is_expected_seasonal")
        if seasonal:
            body = f"{first}, {metric} is down {delta or 'this week'} — but this looks seasonal, not a panic signal. Current 30d base: {views} views and {calls} calls. Better move: protect retention now, not over-spend on ads. Want me to draft a low-effort customer retention nudge?"
        else:
            body = f"{first}, quick flag: {metric} dropped {delta or 'sharply'} in the last {payload.get('window', '7d')}. Your 30d base is {views} views, {calls} calls, CTR {fmt_pct(perf.get('ctr'))}. Want me to draft one specific fix using your current offer{(': ' + active_offer) if active_offer else ''}?"
        return body, "binary_yes_no"

    if kind in {"perf_spike", "milestone_reached"}:
        if kind == "milestone_reached":
            body = f"{first}, you are close to {payload.get('milestone_value', 'the next')} {payload.get('metric', 'milestone')} — current value is {payload.get('value_now', 'updated')}. Good time to turn this into social proof. Want me to draft a short Google post for {name}?"
        else:
            body = f"{first}, {payload.get('metric', 'performance')} is up {fmt_pct(payload.get('delta_pct')) or 'this week'} vs baseline. Likely driver: {payload.get('likely_driver', 'recent profile activity')}. Want me to turn this into a follow-up post while momentum is fresh?"
        return body, "binary_yes_no"

    if kind in {"review_theme_emerged"}:
    quote = payload.get("common_quote")
    q = f'Common line: "{quote}". ' if quote else ""
    body = (
        f"{first}, review pattern spotted: "
        f"{payload.get('occurrences_30d', 'multiple')} recent reviews mention "
        f"{payload.get('theme', 'one repeated theme')}. "
        f"{q}Want me to draft a polite public reply + one operational fix note?"
    )
    return body, "binary_yes_no"

    if kind in {"competitor_opened"}:
        body = f"{first}, new competitor signal near {locality}: {payload.get('competitor_name', 'a similar business')} opened {payload.get('distance_km', '?')} km away with {payload.get('their_offer', 'a visible offer')}. Want me to draft a sharper Google post using your own offer{(': ' + active_offer) if active_offer else ''}?"
        return body, "binary_yes_no"

    if kind in {"festival_upcoming", "ipl_match_today", "category_seasonal"}:
        if kind == "ipl_match_today":
            body = f"{first}, {payload.get('match', 'match')} is today at {readable_time(payload.get('match_time_iso'))}. Your active offer is {active_offer or 'ready to use'}. Want me to draft a match-day delivery post with one clean CTA?"
        elif kind == "category_seasonal":
            trends = ', '.join(payload.get('trends', [])[:3])
            body = f"{first}, seasonal demand shift for {payload.get('season', 'this period')}: {trends}. This is useful for shelf/profile planning. Want me to draft a counter display + WhatsApp note?"
        else:
            body = f"{first}, {payload.get('festival', 'festival')} is coming on {payload.get('date', 'the calendar')} — this is early enough to prepare, not rush. Want me to draft a category-fit campaign using {active_offer or 'your strongest service'}?"
        return body, "binary_yes_no"

    if kind in {"renewal_due", "winback_eligible", "dormant_with_vera", "gbp_unverified"}:
        if kind == "renewal_due":
            body = f"{first}, your {payload.get('plan', merchant.get('subscription', {}).get('plan', 'plan'))} renewal has {payload.get('days_remaining', merchant.get('subscription', {}).get('days_remaining', 'few'))} days left. Before renewing, I can show what changed in the last 30 days: {perf.get('views')} views, {perf.get('calls')} calls, {perf.get('directions')} direction requests. Want the 3-line summary?"
        elif kind == "gbp_unverified":
            body = f"{first}, {name} is still unverified on Google. Verification can unlock profile edits and reduce update delays. Path shown: {payload.get('verification_path', 'standard verification')}. Want me to give the exact checklist?"
        elif kind == "winback_eligible":
            body = f"{first}, since expiry, performance is down {fmt_pct(payload.get('perf_dip_pct')) or 'noticeably'} and {payload.get('lapsed_customers_added_since_expiry', 'some')} customers moved into lapsed status. Want me to draft a winback message before the gap widens?"
        else:
            body = f"{first}, it has been {payload.get('days_since_last_merchant_message', 'a while')} days since your last Vera conversation. I can do one useful check only: profile, offer, or customer recall draft. Reply YES and I’ll pick the highest-impact one from current data."
        return body, "binary_yes_no"

    if kind in {"curious_ask_due"}:
        body = f"Hi {first}, quick check — what service/product has been most asked-for this week at {name}? I’ll turn your answer into a Google post + a 4-line WhatsApp reply. Reply with the service name."
        return body, "open_ended"

    if kind in {"active_planning_intent"}:
        topic = payload.get("intent_topic", "your plan").replace("_", " ")
        last = payload.get("merchant_last_message")
        body = f"{first}, continuing from your message{(': ' + last) if last else ''}. Here is a simple first draft for {topic}: 1 clear offer, 1 target audience, 1 WhatsApp CTA. Want me to prepare the exact customer-facing copy now?"
        return body, "binary_yes_no"

    if kind in {"supply_alert"}:
        batches = ', '.join(payload.get('affected_batches', []))
        body = f"{first}, urgent supply alert: {payload.get('molecule', 'medicine')} batch(es) {batches or 'listed'} from {payload.get('manufacturer', 'manufacturer')} need attention. Want me to draft the customer notice + replacement workflow?"
        return body, "binary_yes_no"

    return f"{first}, I found a timely update for {name} based on current context. Want me to draft the next best message/action now?", "binary_yes_no"

def compose_customer(category: dict[str, Any], merchant: dict[str, Any], trigger: dict[str, Any], customer: dict[str, Any]) -> tuple[str, str]:
    kind = trigger.get("kind", "")
    m_ident = merchant.get("identity", {})
    c_ident = customer.get("identity", {})
    cust_name = c_ident.get("name", "there")
    merchant_name = m_ident.get("name", "our team")
    owner = m_ident.get("owner_first_name") or merchant_name
    payload = trigger.get("payload", {}) or {}
    active_offer = first_active_offer(merchant) or category_offer(category)
    lang = c_ident.get("language_pref", "")
    hi_mix = "hi" in str(lang).lower()

    if kind == "recall_due":
        slots = payload.get("available_slots", [])
        slot_text = " ya ".join([s.get("label", "available slot") for s in slots[:2]]) or "a convenient slot"
        if hi_mix:
            body = f"Hi {cust_name}, {merchant_name} here. It has been some time since your last visit — your {payload.get('service_due', 'recall').replace('_', ' ')} is due. Apke liye slots ready hain: {slot_text}. {active_offer or 'Consultation available'}. Reply 1/2 for a slot, or share a time that works."
        else:
            body = f"Hi {cust_name}, {merchant_name} here. Your {payload.get('service_due', 'recall').replace('_', ' ')} is due based on your last visit. Available slots: {slot_text}. {active_offer or 'Consultation available'}. Reply 1/2 for a slot, or share a time that works."
        return body, "multi_choice_slot"

    if kind in {"customer_lapsed_soft", "customer_lapsed_hard"}:
        focus = payload.get("previous_focus")
        days = payload.get("days_since_last_visit")
        body = f"Hi {cust_name}, {owner} from {merchant_name} here. It has been {days or 'a while'} days since your last visit — no pressure. {('Your earlier focus was ' + focus + '. ') if focus else ''}Want me to hold one no-commitment slot/trial for you this week? Reply YES."
        return body, "binary_yes_no"

    if kind in {"appointment_tomorrow", "trial_followup"}:
        options = payload.get("next_session_options", []) or payload.get("available_slots", [])
        slot = options[0].get("label") if options else "the next available slot"
        body = f"Hi {cust_name}, {merchant_name} here. Quick follow-up — your next option is {slot}. Want us to block it for you? Reply YES to confirm or suggest another time."
        return body, "binary_yes_no"

    if kind in {"chronic_refill_due"}:
        meds = ", ".join(payload.get("molecule_list", []))
        date = readable_date(payload.get("stock_runs_out_iso")) or "soon"
        delivery = " Free home delivery is available to your saved address." if payload.get("delivery_address_saved") else ""
        body = f"Namaste {cust_name}, {merchant_name} here. Your regular medicines ({meds}) are expected to run out by {date}.{delivery} Reply CONFIRM to dispatch, or tell us if any dosage changed."
        return body, "binary_confirm_cancel"

    if kind in {"wedding_package_followup"}:
        body = f"Hi {cust_name}, {merchant_name} here. Your wedding date {payload.get('wedding_date', '')} is on our note, and this is the right window for {payload.get('next_step_window_open', 'the next prep step').replace('_', ' ')}. Want us to block your preferred slot for the first session?"
        return body, "binary_yes_no"

    return f"Hi {cust_name}, {merchant_name} here. We have a relevant update based on your last interaction. Reply YES if you want us to help with the next step.", "binary_yes_no"

# ----------------------------- helpers -----------------------------
def get_payload(scope: str, context_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not context_id:
        return None
    record = contexts.get((scope, context_id))
    return record.get("payload") if record else None

def clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:1200]

def safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", str(value))[:80]

def conversation_id(merchant_id: str, trigger_id: str, customer_id: Optional[str]) -> str:
    base = f"{merchant_id}_{customer_id or 'merchant'}_{trigger_id}"
    digest = hashlib.sha1(base.encode()).hexdigest()[:8]
    return f"conv_{safe_id(merchant_id)}_{safe_id(trigger_id)}_{digest}"[:180]

def template_name(trigger: dict[str, Any], customer: Optional[dict[str, Any]]) -> str:
    kind = safe_id(trigger.get("kind", "generic"))
    return f"{'merchant' if customer else 'vera'}_{kind}_v1"

def build_template_params(body: str) -> list[str]:
    if len(body) <= 240:
        return [body]
    return [body[:240], body[240:480], body[480:720]]

def short_business_name(name: str) -> str:
    if not name:
        return "there"
    return str(name).split()[0].strip(",")

def first_active_offer(merchant: dict[str, Any]) -> Optional[str]:
    for offer in merchant.get("offers", []) or []:
        if offer.get("status") == "active" and offer.get("title"):
            return offer["title"]
    return None

def category_offer(category: dict[str, Any]) -> Optional[str]:
    for offer in category.get("offer_catalog", []) or []:
        if offer.get("type") == "service_at_price" and offer.get("title"):
            return offer["title"]
    offers = category.get("offer_catalog", []) or []
    return offers[0].get("title") if offers else None

def find_digest_item(category: dict[str, Any], digest_id: Optional[str]) -> Optional[dict[str, Any]]:
    digest = category.get("digest", []) or []
    if digest_id:
        for item in digest:
            if item.get("id") == digest_id:
                return item
    return digest[0] if digest else None

def message_digest(first: str, merchant: dict[str, Any], category: dict[str, Any], trigger: dict[str, Any], item: Optional[dict[str, Any]]) -> str:
    if not item:
        return f"{first}, a new {category.get('display_name', category.get('slug', 'category'))} update landed and it matches your current context. Want me to summarize it and draft a WhatsApp-ready note?"
    title = item.get("title", "new update")
    source = item.get("source")
    trial = item.get("trial_n")
    segment = item.get("patient_segment") or item.get("kind")
    evidence = []
    if trial:
        evidence.append(f"{trial:,}-person")
    if segment:
        evidence.append(str(segment).replace("_", " "))
    merchant_signal = ""
    signals = " ".join(merchant.get("signals", []) or [])
    if "high_risk" in signals or merchant.get("customer_aggregate", {}).get("high_risk_adult_count"):
        merchant_signal = " relevant to your high-risk adult cohort"
    return f"{first}, {source + ' — ' if source else ''}{title}.{merchant_signal} Worth a quick look. Want me to pull the key points and draft a customer-friendly WhatsApp you can share?"

def fmt_pct(value: Any) -> Optional[str]:
    try:
        value = float(value)
        if abs(value) <= 1:
            return f"{round(value*100):g}%"
        return f"{round(value):g}%"
    except Exception:
        return None

def readable_time(iso: Optional[str]) -> str:
    if not iso:
        return "the scheduled time"
    try:
        return iso.split("T")[1][:5]
    except Exception:
        return iso

def readable_date(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    return iso.split("T")[0]

def rationale_for(kind: str, merchant: dict[str, Any], trigger: dict[str, Any], customer: Optional[dict[str, Any]]) -> str:
    scope = "customer-facing" if customer else "merchant-facing"
    return f"{scope} {kind} message grounded in trigger payload, merchant context, category voice and available offer/data; uses one clear CTA and avoids unsupported claims."

def is_auto_reply(message: str) -> bool:
    text = message.lower()
    return any(re.search(p, text) for p in AUTO_REPLY_PATTERNS)

def is_stop(message: str) -> bool:
    text = message.lower()
    return any(re.search(p, text) for p in STOP_PATTERNS)

def is_yes(message: str) -> bool:
    text = message.lower()
    return any(re.search(p, text) for p in YES_PATTERNS)

def is_out_of_scope(message: str) -> bool:
    text = message.lower()
    return any(re.search(p, text) for p in OUT_OF_SCOPE_PATTERNS)

def count_recent_auto_replies(turns: list[dict[str, Any]]) -> int:
    count = 0
    for t in reversed(turns):
        if t.get("from") == "bot":
            continue
        if is_auto_reply(t.get("body", "")):
            count += 1
        else:
            break
    return count

def action_reply(conversation_id: str, merchant_id: Optional[str], customer_id: Optional[str]) -> dict[str, Any]:
    history = conversations.get(conversation_id, [])
    last_bot = next((t for t in reversed(history) if t.get("from") == "bot"), {})
    trigger_id = last_bot.get("trigger_id")
    trigger = get_payload("trigger", trigger_id) if trigger_id else None
    kind = trigger.get("kind") if trigger else "action"
    if customer_id:
        return {
            "action": "send",
            "body": "Confirmed. I’ll keep the next step simple and aligned with the selected option. If anything changes, reply with the preferred time/details.",
            "cta": "none",
            "rationale": "Customer accepted the offer/slot; acknowledging without adding complexity.",
        }
    if kind in {"research_digest", "cde_opportunity", "regulation_change"}:
        body = "Great. I’ll prepare a concise summary plus a WhatsApp-ready draft. First draft: one concrete fact, one simple explanation, one reply CTA — so it is easy for customers to act. Reply CONFIRM if you want this sent/scheduled."
    elif kind in {"active_planning_intent", "curious_ask_due"}:
        body = "Great — moving to action. I’ll draft the exact customer-facing copy now with one offer, one audience, and one CTA. Reply CONFIRM to approve the draft."
    else:
        body = "Great — I’ll prepare the next action from the current context and keep it short. Reply CONFIRM to proceed, or STOP to close."
    return {"action": "send", "body": body, "cta": "binary_confirm_cancel", "rationale": "Merchant showed explicit intent; switching from qualification to action immediately."}
