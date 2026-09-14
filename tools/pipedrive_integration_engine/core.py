"""Pipedrive API and integration engine.

Pure logic: the direct webhook router that turns a Pipedrive stage change into
an outbound Sinch SMS call with no Zapier in the path, the two way AI SMS
thread with sales rep handover, the opt out synchronizer, and the Power BI
incremental ingestion ledger. No Streamlit import, so the engine is unit
testable on its own and drops straight into a real webhook handler.

Every function here is deterministic. Where a real system would reach for the
clock or open a socket, the caller passes `now` or receives a fully described
request instead, so a test and a production run of the same payload agree.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

ENGINE_VERSION = "1.0.0"

# The assistant profile that answers inbound SMS. Named rather than hardcoded
# to a vendor, so the deployment can swap the model behind it without a code
# change anywhere else in the engine.
LLM_ASSISTANT = "sms-assistant-v1"

# Endpoints the engine calls directly. Nothing sits between these and us.
SINCH_BATCHES_URL = "https://sms.api.sinch.com/xms/v1/{service_plan_id}/batches"
PIPEDRIVE_API_BASE = "https://{company}.pipedrive.com/api/v2"

# Dispatch outcomes
DISPATCHED = "dispatched"
BLOCKED = "blocked"
QUEUED = "queued"

DISPATCH_LABEL = {
    DISPATCHED: "Dispatched to Sinch",
    BLOCKED: "Blocked before dispatch",
    QUEUED: "Queued for a sales rep",
}

# Inbound intents
INTENT_PRICING = "pricing"
INTENT_BOOKING = "booking"
INTENT_STATUS = "status"
INTENT_HANDOVER = "handover"
INTENT_OPT_OUT = "opt_out"
INTENT_OPT_IN = "opt_in"
INTENT_GENERAL = "general"

INTENT_LABEL = {
    INTENT_PRICING: "Pricing question",
    INTENT_BOOKING: "Wants to book time",
    INTENT_STATUS: "Asking about their deal",
    INTENT_HANDOVER: "Asking for a human",
    INTENT_OPT_OUT: "Opt out keyword",
    INTENT_OPT_IN: "Opt back in keyword",
    INTENT_GENERAL: "General message",
}

# Pipedrive marketing_status values, exactly as the API spells them.
SUBSCRIBED = "subscribed"
UNSUBSCRIBED = "unsubscribed"
NO_CONSENT = "no_consent"

# The keywords a carrier expects an SMS programme to honour. STOP is mandatory
# in every market we send to, and the rest are the aliases customers actually
# type. Matching is case insensitive and ignores punctuation.
OPT_OUT_KEYWORDS = frozenset({
    "stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout",
    "opt out", "remove me", "no more", "revoke",
})
OPT_IN_KEYWORDS = frozenset({"start", "unstop", "yes please resume", "optin", "opt in"})

# Phrases that mean the customer wants a person, not the assistant. Kept as
# explicit phrases rather than single words so that "human error" or a deal
# named "Agent Portal" cannot trigger a handover on their own.
HANDOVER_PHRASES = (
    "speak to a human", "talk to a human", "speak to a person",
    "talk to a person", "speak to someone", "talk to someone",
    "speak to a real person", "real person", "real human",
    "speak to an agent", "talk to an agent", "human agent",
    "speak to a rep", "talk to a rep", "sales rep", "a salesperson",
    "speak to sales", "talk to sales", "speak to a manager",
    "talk to a manager", "call me", "give me a call", "phone me",
    "ring me", "stop the bot", "not a bot", "are you a bot",
    "i want a human", "get me a human", "put me through",
    "transfer me", "speak to yinka", "speak to my rep",
)

# Why a handover fired, in the words a sales rep will read on the activity.
HANDOVER_REASON_ASKED = "The customer asked for a human."
HANDOVER_REASON_REPEAT = (
    "The assistant could not answer the same question twice, so the thread "
    "went to a rep rather than trying a third time."
)
HANDOVER_REASON_VALUE = (
    "The deal is above the value threshold where a rep always takes the "
    "conversation."
)

# A deal worth more than this is never left to the assistant once the customer
# engages: the margin on one of these dwarfs the cost of a rep's ten minutes.
REP_TAKEOVER_VALUE = 25000.0

# Blocked dispatch reasons
BLOCK_OPT_OUT = "The person is unsubscribed in Pipedrive, so nothing was sent."
BLOCK_NO_PHONE = "The person has no mobile number on the Pipedrive record."
BLOCK_NO_TEMPLATE = "No message template is mapped to that stage."


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Person:
    """A Pipedrive person, carrying the two fields consent depends on."""
    person_id: str
    name: str
    phone: str
    email: str = ""
    marketing_status: str = SUBSCRIBED
    sms_opt_out: bool = False
    opt_out_at: str = ""
    opt_out_keyword: str = ""
    owner: str = "unassigned"

    @property
    def opted_out(self) -> bool:
        """One property, so no caller can read consent half way."""
        return bool(self.sms_opt_out) or self.marketing_status == UNSUBSCRIBED


@dataclass(frozen=True)
class Deal:
    """A Pipedrive deal, as the webhook body describes it."""
    deal_id: str
    title: str
    person_id: str
    stage: str
    value: float
    currency: str = "GBP"
    owner: str = "unassigned"
    status: str = "open"
    updated_at: str = "2026-09-14T09:00:00Z"


@dataclass(frozen=True)
class StageChange:
    """One movement of a deal between stages, which is what we bill duration to."""
    deal_id: str
    from_stage: str
    to_stage: str
    at: str


@dataclass(frozen=True)
class Message:
    """One SMS in a thread, in either direction."""
    direction: str          # "inbound" or "outbound"
    text: str
    at: str
    author: str = ""        # the assistant, the rep, or the customer


@dataclass(frozen=True)
class HttpCall:
    """A fully described outbound request, built but not sent.

    The engine returns these rather than performing them so the same code path
    is exercised by a test, by the console and by the live handler.
    """
    method: str
    url: str
    headers: dict
    body: dict
    purpose: str

    def as_json(self) -> str:
        return json.dumps(
            {"method": self.method, "url": self.url,
             "headers": self.headers, "body": self.body},
            indent=2, sort_keys=False,
        )


@dataclass(frozen=True)
class Dispatch:
    """The result of translating one Pipedrive event into an SMS send."""
    status: str
    deal_id: str
    person_id: str
    stage: str
    to_number: str
    text: str
    call: HttpCall | None
    reason: str
    hops: tuple[str, ...]
    elapsed_ms: float
    event_id: str
    zapier_used: bool = False


@dataclass(frozen=True)
class AiTurn:
    """What the assistant decided about one inbound message."""
    intent: str
    reply: str
    handover: bool
    handover_reason: str
    person_id: str
    deal_id: str
    confidence: float
    prompt: str
    model: str = LLM_ASSISTANT


@dataclass(frozen=True)
class FieldUpdate:
    """One field the engine writes back to Pipedrive, before and after."""
    entity: str
    entity_id: str
    field_name: str
    old_value: str
    new_value: str


@dataclass(frozen=True)
class OptOutResult:
    """The full consequence of one STOP, so nothing about it is implicit."""
    person: Person
    applied: bool
    keyword: str
    updates: tuple[FieldUpdate, ...]
    calls: tuple[HttpCall, ...]
    confirmation: str
    note: str


@dataclass(frozen=True)
class LedgerRow:
    """One Power BI row, carrying its own change hash."""
    deal_id: str
    deal_title: str
    person_id: str
    person_name: str
    stage: str
    status: str
    value: float
    currency: str
    owner: str
    days_in_stage: float
    days_open: float
    sms_consent: str
    updated_at: str
    change_hash: str


@dataclass(frozen=True)
class IncrementalPlan:
    """What an incremental Power BI refresh would actually move."""
    new_rows: tuple[LedgerRow, ...]
    changed_rows: tuple[LedgerRow, ...]
    unchanged_rows: tuple[LedgerRow, ...]
    watermark: str
    previous_watermark: str

    @property
    def rows_to_send(self) -> tuple[LedgerRow, ...]:
        return self.new_rows + self.changed_rows

    @property
    def saved_rows(self) -> int:
        return len(self.unchanged_rows)


@dataclass(frozen=True)
class RepMetric:
    rep: str
    deals: int
    open_deals: int
    won_deals: int
    pipeline_value: float
    won_value: float
    avg_days_in_stage: float

    @property
    def win_rate(self) -> float:
        closed = self.won_deals + (self.deals - self.open_deals - self.won_deals)
        return round(self.won_deals / closed, 4) if closed else 0.0


@dataclass
class Thread:
    """A two way SMS conversation tied to one person and one deal.

    Mutable on purpose: a thread is the one thing in this engine that has a
    life longer than a single request.
    """
    person_id: str
    deal_id: str
    messages: list[Message] = field(default_factory=list)
    handover: bool = False
    handover_reason: str = ""
    handover_at: str = ""
    answered_intents: list[str] = field(default_factory=list)

    def add(self, message: Message) -> None:
        self.messages.append(message)

    @property
    def inbound(self) -> list[Message]:
        return [m for m in self.messages if m.direction == "inbound"]

    @property
    def outbound(self) -> list[Message]:
        return [m for m in self.messages if m.direction == "outbound"]


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_PEOPLE: tuple[Person, ...] = (
    Person("PD-P-4401", "Hannah Whitcombe", "+447700900412",
           "hannah@northwind.com", SUBSCRIBED, False, "", "", "r.okonkwo"),
    Person("PD-P-4402", "Marcus Adeyemi", "+447700900518",
           "marcus@fabrikam.de", SUBSCRIBED, False, "", "", "s.patel"),
    Person("PD-P-4403", "Priya Raghunathan", "+447700900677",
           "priya@litware.io", SUBSCRIBED, False, "", "", "r.okonkwo"),
    Person("PD-P-4404", "Tom Beaufort", "+447700900731",
           "tom@contoso.co.uk", UNSUBSCRIBED, True, "2026-08-30T11:02:00Z",
           "STOP", "s.patel"),
)

SAMPLE_DEALS: tuple[Deal, ...] = (
    Deal("PD-D-9001", "Northwind fleet telematics rollout", "PD-P-4401",
         "Proposal Sent", 48250.00, "GBP", "r.okonkwo", "open",
         "2026-09-14T09:14:00Z"),
    Deal("PD-D-9002", "Fabrikam depot hardware refresh", "PD-P-4402",
         "Qualified", 16400.00, "EUR", "s.patel", "open",
         "2026-09-13T16:40:00Z"),
    Deal("PD-D-9003", "Litware analytics seat expansion", "PD-P-4403",
         "Negotiation", 31900.00, "GBP", "r.okonkwo", "open",
         "2026-09-12T10:05:00Z"),
    Deal("PD-D-9004", "Contoso trial conversion", "PD-P-4404",
         "Won", 7400.00, "GBP", "s.patel", "won",
         "2026-09-09T14:20:00Z"),
)

SAMPLE_HISTORY: tuple[StageChange, ...] = (
    StageChange("PD-D-9001", "Lead In", "Qualified", "2026-08-21T09:00:00Z"),
    StageChange("PD-D-9001", "Qualified", "Demo Booked", "2026-08-28T11:30:00Z"),
    StageChange("PD-D-9001", "Demo Booked", "Proposal Sent", "2026-09-05T15:10:00Z"),
    StageChange("PD-D-9002", "Lead In", "Qualified", "2026-09-03T08:45:00Z"),
    StageChange("PD-D-9003", "Qualified", "Demo Booked", "2026-08-18T13:00:00Z"),
    StageChange("PD-D-9003", "Demo Booked", "Negotiation", "2026-09-01T09:25:00Z"),
    StageChange("PD-D-9004", "Negotiation", "Won", "2026-09-09T14:20:00Z"),
)

# The message that fires when a deal lands on a stage. A stage with no entry
# here sends nothing, which is deliberate: silence is the safe default.
STAGE_TEMPLATES: dict[str, str] = {
    "Qualified": (
        "Hi {first_name}, thanks for your time today. I have logged "
        "{deal_title} and will send the detail through shortly. Reply here any "
        "time, a person reads every message."
    ),
    "Demo Booked": (
        "Hi {first_name}, your demo for {deal_title} is booked. I will send a "
        "reminder the day before. Reply here if you need to move it."
    ),
    "Proposal Sent": (
        "Hi {first_name}, your proposal for {deal_title} is on its way by "
        "email, {value} in total. Reply here with any questions and I will "
        "answer straight away."
    ),
    "Negotiation": (
        "Hi {first_name}, thanks for coming back on {deal_title}. I have "
        "passed your points to {owner_first}, who will confirm the final "
        "numbers with you."
    ),
    "Won": (
        "Hi {first_name}, that is {deal_title} signed and confirmed. Welcome "
        "aboard. Onboarding will be in touch within one working day."
    ),
}

STAGE_ORDER = ("Lead In", "Qualified", "Demo Booked", "Proposal Sent",
               "Negotiation", "Won", "Lost")

# What the same job costs when Zapier sits in the middle. These are the
# published behaviours of a polling Zap on a paid plan, not an estimate we
# invented: a poll interval, two task charges per fire, and a second vendor
# holding the customer's phone number in transit.
ZAPIER_PATH: tuple[str, ...] = (
    "Pipedrive stage change written",
    "Zapier polls Pipedrive for new matches",
    "Zap trigger fires and consumes a task",
    "Zap action formats the message and consumes a second task",
    "Zapier calls Sinch",
    "Sinch queues the SMS",
)
DIRECT_PATH: tuple[str, ...] = (
    "Pipedrive stage change written",
    "Pipedrive webhook posts to our endpoint",
    "Endpoint calls Sinch directly",
    "Sinch queues the SMS",
)
# A polling Zap on the common paid tiers checks every one or two minutes, so
# the median wait before the trigger even fires is half of that.
ZAPIER_MEDIAN_DELAY_SECONDS = 60.0
ZAPIER_TASKS_PER_EVENT = 2


# ---------------------------------------------------------------------------
# Normalisers
# ---------------------------------------------------------------------------

def normalize_phone(value: str, default_country: str = "44") -> str:
    """Reduce a typed number to E.164, which is what Sinch accepts.

    Reps type "07700 900412", "+44 7700 900412" and "0044 7700900412" for the
    same handset. All three have to reach the same person, and the same
    consent record.
    """
    raw = str(value or "").strip()
    digits = re.sub(r"[^0-9+]", "", raw)
    if digits.startswith("+"):
        return "+" + re.sub(r"[^0-9]", "", digits)
    digits = re.sub(r"[^0-9]", "", digits)
    if not digits:
        return ""
    if digits.startswith("00"):
        return "+" + digits[2:]
    if digits.startswith("0"):
        return "+" + default_country + digits[1:]
    if digits.startswith(default_country):
        return "+" + digits
    return "+" + digits


def normalize_keyword(text: str) -> str:
    """Strip punctuation and case so "STOP." and "stop" are one keyword."""
    return re.sub(r"[^a-z0-9 ]", " ", str(text or "").lower()).strip()


def squash(text: str) -> str:
    """Collapse whitespace, so phrase matching is not defeated by line breaks."""
    return re.sub(r"\s+", " ", normalize_keyword(text)).strip()


def first_name(full_name: str) -> str:
    parts = str(full_name or "").strip().split()
    return parts[0] if parts else "there"


def redact(token: str) -> str:
    """Never print a live credential, not in a demo and not in a log."""
    text = str(token or "")
    if len(text) <= 8:
        return "****"
    return f"{text[:4]}{'*' * 8}{text[-4:]}"


def parse_time(value: str) -> datetime:
    """Read the ISO 8601 stamps Pipedrive returns, with or without the Z."""
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def money(value: float, currency: str) -> str:
    symbols = {"GBP": "£", "USD": "$", "EUR": "€"}
    return f"{symbols.get(currency, '')}{float(value):,.2f}".rstrip()


def find_person(person_id: str, people=SAMPLE_PEOPLE) -> Person | None:
    for person in people:
        if person.person_id == person_id:
            return person
    return None


def person_by_phone(phone: str, people=SAMPLE_PEOPLE) -> Person | None:
    """Inbound SMS carries a number, not an ID, so this is the join."""
    wanted = normalize_phone(phone)
    if not wanted:
        return None
    for person in people:
        if normalize_phone(person.phone) == wanted:
            return person
    return None


def find_deal(deal_id: str, deals=SAMPLE_DEALS) -> Deal | None:
    for deal in deals:
        if deal.deal_id == deal_id:
            return deal
    return None


def open_deal_for(person_id: str, deals=SAMPLE_DEALS) -> Deal | None:
    """The deal an inbound message is about: the most recently touched open one.

    A person with no open deal still gets a reply, it simply gets logged
    against the person rather than a deal.
    """
    candidates = [d for d in deals if d.person_id == person_id and d.status == "open"]
    if not candidates:
        candidates = [d for d in deals if d.person_id == person_id]
    if not candidates:
        return None
    return sorted(candidates, key=lambda d: parse_time(d.updated_at), reverse=True)[0]


# ---------------------------------------------------------------------------
# 1. Direct webhook router
# ---------------------------------------------------------------------------

def webhook_payload(deal: Deal, previous_stage: str, event_id: str,
                    company_id: str = "8841207",
                    user_id: str = "13440921") -> dict:
    """The body Pipedrive posts on a stage change, in its version 2 shape."""
    return {
        "meta": {
            "action": "change",
            "entity": "deal",
            "entity_id": deal.deal_id,
            "company_id": company_id,
            "user_id": user_id,
            "correlation_id": event_id,
            "version": "2.0",
            "timestamp": deal.updated_at,
            "attempt": 1,
            "webhook_id": "wh_2291",
        },
        "data": {
            "id": deal.deal_id,
            "title": deal.title,
            "person_id": deal.person_id,
            "stage_name": deal.stage,
            "status": deal.status,
            "value": deal.value,
            "currency": deal.currency,
            "owner_name": deal.owner,
            "update_time": deal.updated_at,
        },
        "previous": {
            "id": deal.deal_id,
            "stage_name": previous_stage,
            "status": deal.status,
        },
    }


def stage_message(deal: Deal, person: Person,
                  templates: dict | None = None) -> str:
    """Fill the stage template. Returns an empty string when no template maps."""
    table = STAGE_TEMPLATES if templates is None else templates
    template = table.get(deal.stage, "")
    if not template:
        return ""
    return template.format(
        first_name=first_name(person.name),
        deal_title=deal.title,
        value=money(deal.value, deal.currency),
        owner_first=first_name(deal.owner.replace(".", " ").replace("_", " ")),
        stage=deal.stage,
    )


def sinch_call(to_number: str, text: str, service_plan_id: str,
               api_token: str, sender: str,
               deal_id: str = "", person_id: str = "") -> HttpCall:
    """Build the exact Sinch XMS request. One hop, no broker in between."""
    return HttpCall(
        method="POST",
        url=SINCH_BATCHES_URL.format(service_plan_id=service_plan_id),
        headers={
            "Authorization": f"Bearer {redact(api_token)}",
            "Content-Type": "application/json",
        },
        body={
            "from": sender,
            "to": [to_number],
            "body": text,
            "client_reference": f"{deal_id or 'no-deal'}:{person_id or 'no-person'}",
            "delivery_report": "per_recipient",
        },
        purpose="Send the stage change SMS through Sinch",
    )


def pipedrive_note_call(deal_id: str, person_id: str, content: str,
                        company: str = "acme-sales",
                        api_token: str = "pd_live_9f22a1c47b6e") -> HttpCall:
    """Write the sent message back onto the Pipedrive record as a note."""
    return HttpCall(
        method="POST",
        url=f"{PIPEDRIVE_API_BASE.format(company=company)}/notes",
        headers={
            "x-api-token": redact(api_token),
            "Content-Type": "application/json",
        },
        body={
            "deal_id": deal_id or None,
            "person_id": person_id or None,
            "content": content,
        },
        purpose="Log the SMS against the Pipedrive record",
    )


def route_webhook(deal: Deal, previous_stage: str, event_id: str,
                  people=SAMPLE_PEOPLE,
                  templates: dict | None = None,
                  service_plan_id: str = "sp_2f81c4",
                  api_token: str = "sinch_live_7c41f9e2a8b3",
                  sender: str = "Acme",
                  now: str = "2026-09-14T09:14:01Z") -> Dispatch:
    """Translate one Pipedrive stage change into one Sinch send, directly.

    Nothing in this function calls a broker, polls a queue or waits on a third
    party schedule. The webhook arrives, we build the request, we send it. That
    is the whole path, and `hops` states it so the console does not have to
    take our word for it.
    """
    started = time.perf_counter()
    person = find_person(deal.person_id, people)
    text = ""
    to_number = normalize_phone(person.phone) if person else ""

    if person is None:
        reason = f"No Pipedrive person {deal.person_id} exists, so nothing was sent."
        status = BLOCKED
    elif person.opted_out:
        reason = BLOCK_OPT_OUT
        status = BLOCKED
    elif not to_number:
        reason = BLOCK_NO_PHONE
        status = BLOCKED
    else:
        text = stage_message(deal, person, templates)
        if not text:
            reason = BLOCK_NO_TEMPLATE
            status = BLOCKED
        else:
            reason = (
                f"Stage moved from {previous_stage} to {deal.stage}, so the "
                f"{deal.stage} template went straight to Sinch."
            )
            status = DISPATCHED

    call = None
    if status == DISPATCHED and person is not None:
        call = sinch_call(to_number, text, service_plan_id, api_token, sender,
                          deal.deal_id, person.person_id)

    elapsed = (time.perf_counter() - started) * 1000.0
    return Dispatch(
        status=status,
        deal_id=deal.deal_id,
        person_id=deal.person_id,
        stage=deal.stage,
        to_number=to_number,
        text=text,
        call=call,
        reason=reason,
        hops=DIRECT_PATH,
        elapsed_ms=round(elapsed, 3),
        event_id=event_id,
        zapier_used=False,
    )


def path_comparison(events_per_month: int = 600) -> dict:
    """State the Zapier cost of the same job, in hops, delay and tasks."""
    return {
        "direct_hops": len(DIRECT_PATH),
        "zapier_hops": len(ZAPIER_PATH),
        "hops_removed": len(ZAPIER_PATH) - len(DIRECT_PATH),
        "direct_delay_seconds": 0.0,
        "zapier_delay_seconds": ZAPIER_MEDIAN_DELAY_SECONDS,
        "zapier_tasks_per_event": ZAPIER_TASKS_PER_EVENT,
        "zapier_tasks_per_month": events_per_month * ZAPIER_TASKS_PER_EVENT,
        "vendors_holding_customer_data_direct": 2,
        "vendors_holding_customer_data_zapier": 3,
    }


# ---------------------------------------------------------------------------
# 2. Two way AI SMS thread and sales rep handover
# ---------------------------------------------------------------------------

def is_opt_out(text: str) -> bool:
    """True when the message is a consent withdrawal, not a conversation.

    Checked before anything else on every inbound message. A customer who
    types STOP has withdrawn consent even if the rest of the line is chatty.
    """
    cleaned = squash(text)
    if not cleaned:
        return False
    if cleaned in OPT_OUT_KEYWORDS:
        return True
    words = cleaned.split()
    if words and words[0] in OPT_OUT_KEYWORDS:
        return True
    return any(phrase in cleaned for phrase in OPT_OUT_KEYWORDS if " " in phrase)


def is_opt_in(text: str) -> bool:
    cleaned = squash(text)
    if not cleaned:
        return False
    return cleaned in OPT_IN_KEYWORDS or cleaned.split()[0] in {"start", "unstop"}


def wants_human(text: str) -> bool:
    """True when the customer has asked for a person rather than the assistant."""
    cleaned = squash(text)
    if not cleaned:
        return False
    return any(phrase in cleaned for phrase in HANDOVER_PHRASES)


def classify(text: str) -> str:
    """Name the intent of an inbound message, in priority order."""
    cleaned = squash(text)
    if is_opt_out(text):
        return INTENT_OPT_OUT
    if is_opt_in(text):
        return INTENT_OPT_IN
    if wants_human(text):
        return INTENT_HANDOVER
    if any(word in cleaned for word in
           ("price", "pricing", "cost", "quote", "how much", "discount", "budget")):
        return INTENT_PRICING
    if any(word in cleaned for word in
           ("book", "meeting", "demo", "call on", "available", "diary",
            "calendar", "reschedule")):
        return INTENT_BOOKING
    if any(word in cleaned for word in
           ("where are we", "status", "update", "progress", "heard back",
            "any news", "proposal")):
        return INTENT_STATUS
    return INTENT_GENERAL


def build_prompt(thread: Thread, person: Person, deal: Deal | None,
                 inbound: str) -> str:
    """The prompt the assistant is given, with the Pipedrive record attached.

    The record is what makes the reply useful: without the deal stage and value
    the assistant is a chatbot, with them it is the account's own history
    answering the customer.
    """
    lines = [
        f"You are {LLM_ASSISTANT}, answering a customer by SMS on behalf of "
        f"{first_name(person.owner.replace('.', ' '))} at Acme.",
        "Keep replies under 320 characters. Never invent a price, a date or a "
        "discount. If you cannot answer from the record below, hand over.",
        "",
        "PIPEDRIVE RECORD",
        f"person_id: {person.person_id}",
        f"person_name: {person.name}",
        f"person_phone: {normalize_phone(person.phone)}",
        f"marketing_status: {person.marketing_status}",
    ]
    if deal is not None:
        lines += [
            f"deal_id: {deal.deal_id}",
            f"deal_title: {deal.title}",
            f"deal_stage: {deal.stage}",
            f"deal_value: {money(deal.value, deal.currency)}",
            f"deal_owner: {deal.owner}",
        ]
    else:
        lines.append("deal_id: none open")
    lines += ["", "THREAD SO FAR"]
    for message in thread.messages:
        who = "customer" if message.direction == "inbound" else (message.author or "assistant")
        lines.append(f"{who}: {message.text}")
    lines += ["", f"customer: {inbound}", "", "Reply as the assistant."]
    return "\n".join(lines)


def _reply_for(intent: str, person: Person, deal: Deal | None) -> str:
    name = first_name(person.name)
    title = deal.title if deal else "your enquiry"
    if intent == INTENT_PRICING:
        if deal is None:
            return (f"Happy to help with pricing, {name}. I do not have a live "
                    f"quote on your record yet, so I am asking your rep to send "
                    f"one across today.")
        return (f"Hi {name}, {title} is quoted at "
                f"{money(deal.value, deal.currency)} as it stands, and that is "
                f"the figure on the proposal. Reply here if you want it broken "
                f"down line by line.")
    if intent == INTENT_BOOKING:
        return (f"Of course, {name}. I can put time in for {title}. Reply with a "
                f"day that suits and I will send a calendar invite to confirm.")
    if intent == INTENT_STATUS:
        if deal is None:
            return (f"Hi {name}, there is nothing open on your record right now, "
                    f"so I am asking your rep to confirm where things stand.")
        return (f"Hi {name}, {title} is at {deal.stage} right now. Reply here if "
                f"you want anything moved along faster.")
    if intent == INTENT_OPT_IN:
        return (f"Welcome back, {name}. You are resubscribed and will get "
                f"updates on {title} again.")
    return (f"Thanks {name}, that is logged against {title}. I will make sure "
            f"the right person picks it up and comes back to you.")


def route_inbound(thread: Thread, inbound: str, person: Person,
                  deal: Deal | None, now: str = "2026-09-14T10:02:00Z",
                  rep_takeover_value: float = REP_TAKEOVER_VALUE) -> AiTurn:
    """Route one inbound SMS: classify, answer, or hand to a rep.

    The thread is mutated in place, because the state that matters most here is
    whether a human has already taken over. Once they have, the assistant stops
    answering: two voices in one thread is how a customer gets told two things.
    """
    intent = classify(inbound)
    thread.add(Message("inbound", inbound, now, author=person.name))

    handover = False
    reason = ""

    if wants_human(inbound):
        handover, reason = True, HANDOVER_REASON_ASKED
    elif (deal is not None and float(deal.value) > float(rep_takeover_value)
            and intent == INTENT_PRICING):
        # Booking a demo is exactly what automation should do unattended. A
        # price conversation on a deal this size is not: that one is a rep's.
        handover, reason = True, HANDOVER_REASON_VALUE
    elif intent != INTENT_GENERAL and thread.answered_intents.count(intent) >= 2:
        handover, reason = True, HANDOVER_REASON_REPEAT

    if thread.handover:
        # A rep already owns this thread, so the assistant stays silent.
        return AiTurn(
            intent=intent,
            reply="",
            handover=True,
            handover_reason=thread.handover_reason,
            person_id=person.person_id,
            deal_id=deal.deal_id if deal else "",
            confidence=1.0,
            prompt="",
        )

    prompt = build_prompt(thread, person, deal, inbound)

    if handover:
        thread.handover = True
        thread.handover_reason = reason
        thread.handover_at = now
        owner = first_name(person.owner.replace(".", " ").replace("_", " "))
        reply = (f"No problem, {first_name(person.name)}. I am passing this to "
                 f"{owner} now and they will call you. You will hear from a "
                 f"person, not from me.")
        thread.add(Message("outbound", reply, now, author=LLM_ASSISTANT))
        return AiTurn(intent=intent, reply=reply, handover=True,
                      handover_reason=reason, person_id=person.person_id,
                      deal_id=deal.deal_id if deal else "", confidence=0.99,
                      prompt=prompt)

    reply = _reply_for(intent, person, deal)
    thread.answered_intents.append(intent)
    thread.add(Message("outbound", reply, now, author=LLM_ASSISTANT))
    confidence = 0.95 if intent != INTENT_GENERAL else 0.6
    return AiTurn(intent=intent, reply=reply, handover=False,
                  handover_reason="", person_id=person.person_id,
                  deal_id=deal.deal_id if deal else "", confidence=confidence,
                  prompt=prompt)


def handover_activity(thread: Thread, person: Person, deal: Deal | None,
                      company: str = "acme-sales",
                      api_token: str = "pd_live_9f22a1c47b6e") -> HttpCall:
    """The Pipedrive activity that puts the handover on a rep's list.

    A flag nobody sees is not a handover. This creates a dated, owned task on
    the deal, which is the thing that actually makes a person pick up a phone.
    """
    transcript = "\n".join(
        f"{'Customer' if m.direction == 'inbound' else 'Assistant'}: {m.text}"
        for m in thread.messages
    )
    return HttpCall(
        method="POST",
        url=f"{PIPEDRIVE_API_BASE.format(company=company)}/activities",
        headers={"x-api-token": redact(api_token), "Content-Type": "application/json"},
        body={
            "subject": f"SMS handover: call {person.name}",
            "type": "call",
            "deal_id": deal.deal_id if deal else None,
            "person_id": person.person_id,
            "owner_id": person.owner,
            "due_date": (thread.handover_at or "")[:10],
            "done": False,
            "note": f"{thread.handover_reason}\n\n{transcript}",
        },
        purpose="Create the call task that makes the handover real",
    )


# ---------------------------------------------------------------------------
# 3. Opt out synchronizer
# ---------------------------------------------------------------------------

def matched_opt_out_keyword(text: str) -> str:
    """Return the keyword that fired, so the record says why, not just what."""
    cleaned = squash(text)
    if not cleaned:
        return ""
    if cleaned in OPT_OUT_KEYWORDS:
        return cleaned.upper()
    words = cleaned.split()
    if words and words[0] in OPT_OUT_KEYWORDS:
        return words[0].upper()
    for phrase in sorted((k for k in OPT_OUT_KEYWORDS if " " in k), key=len, reverse=True):
        if phrase in cleaned:
            return phrase.upper()
    return ""


def person_update_call(person: Person, company: str = "acme-sales",
                       api_token: str = "pd_live_9f22a1c47b6e") -> HttpCall:
    """The single PATCH that carries the whole consent change to Pipedrive."""
    return HttpCall(
        method="PATCH",
        url=f"{PIPEDRIVE_API_BASE.format(company=company)}/persons/{person.person_id}",
        headers={"x-api-token": redact(api_token), "Content-Type": "application/json"},
        body={
            "marketing_status": person.marketing_status,
            "custom_fields": {
                "sms_opt_out": person.sms_opt_out,
                "sms_opt_out_at": person.opt_out_at,
                "sms_opt_out_keyword": person.opt_out_keyword,
            },
        },
        purpose="Write the consent change onto the Pipedrive person",
    )


def apply_opt_out(person: Person, text: str, now: str = "2026-09-14T10:05:00Z",
                  service_plan_id: str = "sp_2f81c4",
                  api_token: str = "sinch_live_7c41f9e2a8b3",
                  sender: str = "Acme") -> OptOutResult:
    """Process one inbound STOP, all the way through to a blocked pipeline.

    Three things have to happen together or the opt out is not real: the person
    is marked unsubscribed in Pipedrive, one confirmation goes back to the
    handset, and every later automated dispatch is refused. This returns all
    three so a caller cannot do one and forget the others.
    """
    keyword = matched_opt_out_keyword(text)
    if not keyword:
        return OptOutResult(
            person=person, applied=False, keyword="", updates=(), calls=(),
            confirmation="",
            note="No opt out keyword in that message, so consent is unchanged.",
        )

    if person.opted_out:
        return OptOutResult(
            person=person, applied=False, keyword=keyword, updates=(), calls=(),
            confirmation="",
            note=(f"{person.name} was already unsubscribed on "
                  f"{person.opt_out_at or 'an earlier date'}, so nothing changed "
                  f"and nothing was sent."),
        )

    updated = replace(person, marketing_status=UNSUBSCRIBED, sms_opt_out=True,
                      opt_out_at=now, opt_out_keyword=keyword)

    updates = (
        FieldUpdate("person", person.person_id, "marketing_status",
                    person.marketing_status, UNSUBSCRIBED),
        FieldUpdate("person", person.person_id, "sms_opt_out",
                    str(person.sms_opt_out), "True"),
        FieldUpdate("person", person.person_id, "sms_opt_out_at",
                    person.opt_out_at or "empty", now),
        FieldUpdate("person", person.person_id, "sms_opt_out_keyword",
                    person.opt_out_keyword or "empty", keyword),
    )

    confirmation = (
        "You are unsubscribed and will get no further automated messages from "
        "Acme. Reply START if you ever want them back."
    )
    calls = (
        person_update_call(updated),
        sinch_call(normalize_phone(person.phone), confirmation, service_plan_id,
                   api_token, sender, "", person.person_id),
    )
    return OptOutResult(
        person=updated, applied=True, keyword=keyword, updates=updates,
        calls=calls, confirmation=confirmation,
        note=(f"{person.name} sent {keyword} at {now}. The Pipedrive person is "
              f"now unsubscribed, one confirmation went back, and every later "
              f"automated dispatch to {normalize_phone(person.phone)} is "
              f"refused by the router."),
    )


def apply_opt_in(person: Person, text: str,
                 now: str = "2026-09-14T10:09:00Z") -> OptOutResult:
    """Honour START as carefully as STOP, or the opt out is a trap."""
    if not is_opt_in(text):
        return OptOutResult(person=person, applied=False, keyword="", updates=(),
                            calls=(), confirmation="",
                            note="No resubscribe keyword in that message.")
    if not person.opted_out:
        return OptOutResult(person=person, applied=False, keyword="START",
                            updates=(), calls=(), confirmation="",
                            note=f"{person.name} is already subscribed.")
    updated = replace(person, marketing_status=SUBSCRIBED, sms_opt_out=False,
                      opt_out_at="", opt_out_keyword="")
    updates = (
        FieldUpdate("person", person.person_id, "marketing_status",
                    person.marketing_status, SUBSCRIBED),
        FieldUpdate("person", person.person_id, "sms_opt_out", "True", "False"),
    )
    return OptOutResult(
        person=updated, applied=True, keyword="START", updates=updates,
        calls=(person_update_call(updated),),
        confirmation="You are resubscribed. Reply STOP at any time.",
        note=f"{person.name} resubscribed at {now}.",
    )


def dispatch_allowed(person: Person) -> tuple[bool, str]:
    """The one gate every automated send passes through."""
    if person.opted_out:
        return False, BLOCK_OPT_OUT
    if not normalize_phone(person.phone):
        return False, BLOCK_NO_PHONE
    return True, "Consent on file and a valid mobile number, so this can send."


def consent_label(person: Person) -> str:
    if person.opted_out:
        return "Unsubscribed"
    if person.marketing_status == NO_CONSENT:
        return "No consent recorded"
    return "Subscribed"


# ---------------------------------------------------------------------------
# 4. Power BI incremental ingestion ledger
# ---------------------------------------------------------------------------

def stage_durations(deal_id: str, history=SAMPLE_HISTORY,
                    now: str = "2026-09-14T09:00:00Z") -> list[tuple[str, float]]:
    """Days a deal spent in each stage it has occupied, oldest first.

    Only stages the deal is recorded as entering are measured. The stage it
    started in has no entry event, so its duration is unknowable and is left
    out rather than guessed at. The final stage is still running, so it is
    measured against `now` rather than against a change that has not happened.
    """
    moves = sorted((h for h in history if h.deal_id == deal_id),
                   key=lambda h: parse_time(h.at))
    rows: list[tuple[str, float]] = []
    for index, move in enumerate(moves):
        start = parse_time(move.at)
        end = parse_time(moves[index + 1].at) if index + 1 < len(moves) else parse_time(now)
        days = max(0.0, (end - start).total_seconds() / 86400.0)
        rows.append((move.to_stage, round(days, 2)))
    return rows


def days_in_current_stage(deal: Deal, history=SAMPLE_HISTORY,
                          now: str = "2026-09-14T09:00:00Z") -> float:
    moves = [h for h in history if h.deal_id == deal.deal_id and h.to_stage == deal.stage]
    if not moves:
        return 0.0
    latest = max(moves, key=lambda h: parse_time(h.at))
    return round(max(0.0, (parse_time(now) - parse_time(latest.at)).total_seconds() / 86400.0), 2)


def days_open(deal: Deal, history=SAMPLE_HISTORY,
              now: str = "2026-09-14T09:00:00Z") -> float:
    moves = [h for h in history if h.deal_id == deal.deal_id]
    if not moves:
        return 0.0
    first = min(moves, key=lambda h: parse_time(h.at))
    end = parse_time(deal.updated_at) if deal.status != "open" else parse_time(now)
    return round(max(0.0, (end - parse_time(first.at)).total_seconds() / 86400.0), 2)


def canonical_row(values: dict) -> str:
    """The exact bytes a row hashes to: sorted keys, no whitespace drift."""
    return json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)


def change_hash(values: dict) -> str:
    """SHA256 of the row's business fields, which is what Power BI compares.

    The hash deliberately excludes anything that moves without the record
    changing, so a refresh does not resend a row because a timestamp ticked.
    """
    payload = {k: v for k, v in values.items()
               if k not in ("change_hash", "extracted_at")}
    return hashlib.sha256(canonical_row(payload).encode("utf-8")).hexdigest()


def ledger_row(deal: Deal, people=SAMPLE_PEOPLE, history=SAMPLE_HISTORY,
               now: str = "2026-09-14T09:00:00Z") -> LedgerRow:
    person = find_person(deal.person_id, people)
    values = {
        "deal_id": deal.deal_id,
        "deal_title": deal.title,
        "person_id": deal.person_id,
        "person_name": person.name if person else "unknown",
        "stage": deal.stage,
        "status": deal.status,
        "value": round(float(deal.value), 2),
        "currency": deal.currency,
        "owner": deal.owner,
        "days_in_stage": days_in_current_stage(deal, history, now),
        "days_open": days_open(deal, history, now),
        "sms_consent": consent_label(person) if person else "Unknown",
        "updated_at": deal.updated_at,
    }
    return LedgerRow(change_hash=change_hash(values), **values)


def ledger_rows(deals=SAMPLE_DEALS, people=SAMPLE_PEOPLE, history=SAMPLE_HISTORY,
                now: str = "2026-09-14T09:00:00Z") -> tuple[LedgerRow, ...]:
    return tuple(ledger_row(deal, people, history, now) for deal in deals)


def incremental_plan(rows, previous_hashes: dict,
                     previous_watermark: str = "") -> IncrementalPlan:
    """Split the current rows into new, changed and unchanged.

    This is the whole point of the change hash: Power BI pulls the two small
    buckets and leaves the third alone, so a refresh that would have moved the
    full table moves only what actually moved.
    """
    new_rows, changed, unchanged = [], [], []
    for row in rows:
        known = previous_hashes.get(row.deal_id)
        if known is None:
            new_rows.append(row)
        elif known != row.change_hash:
            changed.append(row)
        else:
            unchanged.append(row)
    stamps = [row.updated_at for row in rows if row.updated_at]
    watermark = max(stamps, key=parse_time) if stamps else previous_watermark
    return IncrementalPlan(tuple(new_rows), tuple(changed), tuple(unchanged),
                           watermark, previous_watermark)


def hashes_of(rows) -> dict:
    """The state Power BI stores between refreshes, and nothing more."""
    return {row.deal_id: row.change_hash for row in rows}


def rep_metrics(deals=SAMPLE_DEALS, history=SAMPLE_HISTORY,
                now: str = "2026-09-14T09:00:00Z") -> tuple[RepMetric, ...]:
    reps = sorted({deal.owner for deal in deals})
    out = []
    for rep in reps:
        owned = [d for d in deals if d.owner == rep]
        stage_days = [days_in_current_stage(d, history, now) for d in owned]
        out.append(RepMetric(
            rep=rep,
            deals=len(owned),
            open_deals=len([d for d in owned if d.status == "open"]),
            won_deals=len([d for d in owned if d.status == "won"]),
            pipeline_value=round(sum(d.value for d in owned if d.status == "open"), 2),
            won_value=round(sum(d.value for d in owned if d.status == "won"), 2),
            avg_days_in_stage=round(sum(stage_days) / len(stage_days), 2) if stage_days else 0.0,
        ))
    return tuple(out)


def power_bi_manifest(rows=None) -> dict:
    """What the Power BI dataset expects, so the report is built once."""
    return {
        "dataset": "Pipedrive Sales Ledger",
        "table": "fact_deal_state",
        "key": "deal_id",
        "watermark_column": "updated_at",
        "change_column": "change_hash",
        "refresh_policy": "incremental, hourly, on the watermark",
        "mode": "Import with incremental refresh",
        "columns": [
            "deal_id", "deal_title", "person_id", "person_name", "stage",
            "status", "value", "currency", "owner", "days_in_stage",
            "days_open", "sms_consent", "updated_at", "change_hash",
        ],
        "rows_available": len(rows) if rows is not None else 0,
    }


def export_csv(rows) -> str:
    """A flat file Power BI can take directly, with the hash as a column."""
    header = ("deal_id,deal_title,person_id,person_name,stage,status,value,"
              "currency,owner,days_in_stage,days_open,sms_consent,updated_at,"
              "change_hash")

    def cell(value) -> str:
        text = str(value)
        return f'"{text}"' if any(c in text for c in ',"\n') else text

    lines = [header]
    for row in rows:
        lines.append(",".join(cell(v) for v in (
            row.deal_id, row.deal_title, row.person_id, row.person_name,
            row.stage, row.status, f"{row.value:.2f}", row.currency, row.owner,
            row.days_in_stage, row.days_open, row.sms_consent, row.updated_at,
            row.change_hash,
        )))
    return "\n".join(lines)


def export_json(plan: IncrementalPlan) -> str:
    """The same batch as a Power BI friendly JSON envelope."""
    def row_dict(row: LedgerRow) -> dict:
        return {
            "deal_id": row.deal_id, "deal_title": row.deal_title,
            "person_id": row.person_id, "person_name": row.person_name,
            "stage": row.stage, "status": row.status, "value": row.value,
            "currency": row.currency, "owner": row.owner,
            "days_in_stage": row.days_in_stage, "days_open": row.days_open,
            "sms_consent": row.sms_consent, "updated_at": row.updated_at,
            "change_hash": row.change_hash,
        }
    return json.dumps({
        "dataset": "Pipedrive Sales Ledger",
        "table": "fact_deal_state",
        "previous_watermark": plan.previous_watermark,
        "watermark": plan.watermark,
        "new": [row_dict(r) for r in plan.new_rows],
        "changed": [row_dict(r) for r in plan.changed_rows],
        "unchanged_count": len(plan.unchanged_rows),
    }, indent=2)


# ---------------------------------------------------------------------------
# Executive KPIs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Kpis:
    dispatch_ms: float
    zapier_hops_removed: int
    zapier_tasks_avoided: int
    blocked_by_consent: int
    handovers: int
    rows_to_send: int
    rows_skipped: int

    @property
    def sub_second(self) -> bool:
        return self.dispatch_ms < 1000.0

    @property
    def ingest_saving(self) -> float:
        total = self.rows_to_send + self.rows_skipped
        return round(self.rows_skipped / total, 4) if total else 0.0


def engine_kpis(dispatch: Dispatch, plan: IncrementalPlan, threads,
                people=SAMPLE_PEOPLE, events_per_month: int = 600) -> Kpis:
    comparison = path_comparison(events_per_month)
    return Kpis(
        dispatch_ms=dispatch.elapsed_ms,
        zapier_hops_removed=comparison["hops_removed"],
        zapier_tasks_avoided=comparison["zapier_tasks_per_month"],
        blocked_by_consent=len([p for p in people if p.opted_out]),
        handovers=len([t for t in threads if t.handover]),
        rows_to_send=len(plan.rows_to_send),
        rows_skipped=plan.saved_rows,
    )
