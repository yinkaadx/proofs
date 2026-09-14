"""Airtable to Twilio WhatsApp automation engine.

A record changes in Airtable, Make picks it up, and a WhatsApp message goes out.
The three ways that pipeline goes wrong are always the same, and all three are
silent until somebody complains:

  Make retries a scenario that already succeeded, and the customer gets the
  same message twice. A phone number typed by a human is refused by Twilio,
  the module throws, and the scenario stops with the rest of the batch
  unsent. Or the message is sent outside the twenty four hour window without
  an approved template, which Twilio rejects with a code the scenario treats
  as a generic failure and retries forever.

So the ledger is keyed on what actually identifies a send, every phone number
is normalised before it reaches Twilio, and nothing in this module raises: a
record that cannot be sent produces an alert and the batch carries on.

Pure logic, no Streamlit import, so this is unit testable on its own and can sit
behind the real Make webhook. Deterministic: nothing here reads the clock or a
random source unless the caller passes one in.
"""

from __future__ import annotations

import hashlib
import json
import re
import string
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

# Twilio's own documented placeholder shape, not a hex string. A realistic
# looking AC followed by thirty two hex characters is exactly what a real
# Account SID is, and secret scanners refuse a push carrying one, correctly:
# nothing in a repository should look like a credential even when it is not.
TWILIO_ACCOUNT_SID = "AC" + "X" * 32
TWILIO_FROM = "whatsapp:+14155238886"
STATUS_CALLBACK = "https://hook.eu2.make.com/whatsapp-status"

# ---------------------------------------------------------------------------
# The Airtable record
# ---------------------------------------------------------------------------

STATUS_READY = "Ready to notify"
STATUS_NOTIFIED = "Notified"
STATUS_HOLD = "On hold"
STATUS_CANCELLED = "Cancelled"
STATUSES: tuple[str, ...] = (STATUS_READY, STATUS_NOTIFIED, STATUS_HOLD,
                             STATUS_CANCELLED)

# Only this one sends. Everything else is a record the automation should leave
# alone, and a scenario that ignores the status is how a cancelled booking gets
# a confirmation.
SENDING_STATUSES: tuple[str, ...] = (STATUS_READY,)

FIRST_NAMES = ("Amara", "Chidi", "Fatima", "Ope", "Zainab", "Tunde", "Nneka",
               "Yusuf")
LAST_NAMES = ("Okafor", "Balogun", "Adeyemi", "Danjuma", "Eze", "Sani",
              "Obi", "Lawal")

# The phone shapes a real Airtable base contains, because the field is free
# text and a person typed every one of them.
PHONE_CLEAN = "+2348031234567"
PHONE_LOCAL = "08031234567"
PHONE_SPACED = "+234 803 123 4567"
PHONE_BRACKETED = "(0803) 123-4567"
PHONE_DOUBLE_ZERO = "002348031234567"
PHONE_SHORT = "0803123"
PHONE_LETTERS = "0803 CALL ME"
PHONE_EMPTY = ""
PHONE_STYLES: tuple[str, ...] = (PHONE_CLEAN, PHONE_LOCAL, PHONE_SPACED,
                                 PHONE_BRACKETED, PHONE_DOUBLE_ZERO,
                                 PHONE_SHORT, PHONE_LETTERS, PHONE_EMPTY)

RECORD_ID_ALPHABET = string.ascii_letters + string.digits


@dataclass(frozen=True)
class AirtableRecord:
    record_id: str
    name: str
    phone: str
    status: str
    appointment: str
    created_time: str

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name.split() else "there"

    def as_payload(self) -> dict:
        """The shape Airtable posts to a Make webhook."""
        return {
            "id": self.record_id,
            "createdTime": self.created_time,
            "fields": {
                "Name": self.name,
                "Phone Number": self.phone,
                "Status": self.status,
                "Appointment": self.appointment,
            },
        }


def generate_record(rng, sequence: int, now: str = "2026-09-13T09:00:00.000Z",
                    phone: str | None = None,
                    status: str | None = None) -> AirtableRecord:
    """Build a mock record. Seed the rng for a reproducible one.

    The record id follows Airtable's own shape, seventeen characters starting
    with rec, because the ledger is keyed on it and a shorter stand in would
    hide a collision that the real ids cannot have.
    """
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    suffix = "".join(rng.choice(RECORD_ID_ALPHABET) for _ in range(14))
    return AirtableRecord(
        record_id=f"rec{suffix}",
        name=f"{first} {last}",
        phone=phone if phone is not None else rng.choice(PHONE_STYLES),
        status=status or rng.choice(STATUSES),
        appointment=f"2026-09-{14 + (sequence % 10):02d} at 10:30",
        created_time=now,
    )


# ---------------------------------------------------------------------------
# Phone normalisation
# ---------------------------------------------------------------------------

DEFAULT_COUNTRY_CODE = "+234"

ERR_PHONE_EMPTY = "PHONE_EMPTY"
ERR_PHONE_LETTERS = "PHONE_NOT_NUMERIC"
ERR_PHONE_SHORT = "PHONE_TOO_SHORT"
ERR_PHONE_LONG = "PHONE_TOO_LONG"

E164_MIN_DIGITS = 8
E164_MAX_DIGITS = 15
# The shortest national significant number in practical use. Below this the
# number is truncated, whatever the country code in front of it says.
MIN_NATIONAL_DIGITS = 7


@dataclass(frozen=True)
class PhoneResult:
    ok: bool
    e164: str
    original: str
    error_code: str = ""
    message: str = ""
    changed: bool = False


def normalise_phone(raw: str,
                    country_code: str = DEFAULT_COUNTRY_CODE) -> PhoneResult:
    """Turn what a person typed into E.164, or refuse it with a reason.

    Twilio will not guess. A number in any of the shapes a free text field
    collects is rejected outright, so the normalisation has to happen here
    rather than being discovered as error 21211 on every single send.
    """
    original = str(raw)
    text = original.strip()
    if not text:
        return PhoneResult(False, "", original, ERR_PHONE_EMPTY,
                           "The Phone Number field is empty. Nothing can be "
                           "sent, and the record needs fixing in Airtable "
                           "rather than retried.")

    if re.search(r"[A-Za-z]", text):
        return PhoneResult(False, "", original, ERR_PHONE_LETTERS,
                           f"{original!r} contains letters, so it is not a "
                           f"phone number. Retrying this record will fail "
                           f"exactly the same way every time.")

    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = country_code.lstrip("+") + digits[1:]
    elif not digits.startswith(country_code.lstrip("+")):
        digits = country_code.lstrip("+") + digits

    if len(digits) < E164_MIN_DIGITS:
        return PhoneResult(False, "", original, ERR_PHONE_SHORT,
                           f"{original!r} normalises to {len(digits)} digits, "
                           f"below the {E164_MIN_DIGITS} an international "
                           f"number needs.")

    # A truncated local number would otherwise pass: prefixing the country code
    # pushes a six digit fragment over the global minimum. So when the country
    # code is one we recognise, the part after it is checked on its own.
    known_code = country_code.lstrip("+")
    if digits.startswith(known_code):
        national = digits[len(known_code):]
        if len(national) < MIN_NATIONAL_DIGITS:
            return PhoneResult(
                False, "", original, ERR_PHONE_SHORT,
                f"{original!r} leaves only {len(national)} digit(s) after the "
                f"{country_code} country code, and a national number needs at "
                f"least {MIN_NATIONAL_DIGITS}. This is a truncated number "
                f"rather than a foreign one.")
    if len(digits) > E164_MAX_DIGITS:
        return PhoneResult(False, "", original, ERR_PHONE_LONG,
                           f"{original!r} normalises to {len(digits)} digits, "
                           f"above the E.164 maximum of {E164_MAX_DIGITS}.")

    e164 = f"+{digits}"
    return PhoneResult(True, e164, original, changed=e164 != text)


# ---------------------------------------------------------------------------
# The Twilio request
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContentTemplate:
    """An approved WhatsApp template. Outside the twenty four hour window this
    is the only thing Twilio will deliver."""
    sid: str
    name: str
    body: str
    variables: tuple[str, ...]

    def render(self, values: dict[str, str]) -> str:
        rendered = self.body
        for index, variable in enumerate(self.variables, start=1):
            rendered = rendered.replace("{{" + str(index) + "}}",
                                        str(values.get(variable, "")))
        return rendered


APPOINTMENT_TEMPLATE = ContentTemplate(
    sid="HX" + "X" * 32,
    name="appointment_reminder_v3",
    body="Hi {{1}}, this is a reminder about your appointment on {{2}}. "
         "Reply STOP to opt out.",
    variables=("first_name", "appointment"),
)


@dataclass(frozen=True)
class TwilioRequest:
    to: str
    from_: str
    body: str
    account_sid: str
    status_callback: str
    content_sid: str = ""
    content_variables: dict[str, str] = field(default_factory=dict)
    within_window: bool = True

    @property
    def url(self) -> str:
        return (f"https://api.twilio.com/2010-04-01/Accounts/"
                f"{self.account_sid}/Messages.json")

    def as_form(self) -> dict:
        """The form encoded parameters Twilio actually accepts.

        Inside the window a free form Body is allowed. Outside it, only an
        approved template is, so the request carries ContentSid and
        ContentVariables instead and Body is omitted rather than sent and
        ignored.
        """
        form = {"To": self.to, "From": self.from_,
                "StatusCallback": self.status_callback}
        if self.within_window:
            form["Body"] = self.body
        else:
            form["ContentSid"] = self.content_sid
            form["ContentVariables"] = json.dumps(self.content_variables,
                                                  sort_keys=True)
        return form

    def as_curl(self) -> str:
        lines = [f"curl -X POST '{self.url}' \\"]
        for key, value in self.as_form().items():
            lines.append(f"  --data-urlencode '{key}={value}' \\")
        lines.append("  -u \"$TWILIO_ACCOUNT_SID:$TWILIO_AUTH_TOKEN\"")
        return "\n".join(lines)


def build_request(record: AirtableRecord, phone: PhoneResult,
                  within_window: bool = True,
                  template: ContentTemplate = APPOINTMENT_TEMPLATE,
                  account_sid: str = TWILIO_ACCOUNT_SID,
                  from_number: str = TWILIO_FROM) -> TwilioRequest:
    """Format one record into a Twilio WhatsApp request.

    Both channel addresses carry the whatsapp: prefix. Leaving it off the To
    address is the mistake that silently sends an SMS instead, at a different
    price and to a phone that may not be the one the customer uses for
    WhatsApp.
    """
    if not phone.ok:
        raise ValueError("A request cannot be built from a phone number that "
                         "did not normalise.")
    values = {"first_name": record.first_name, "appointment": record.appointment}
    return TwilioRequest(
        to=f"whatsapp:{phone.e164}",
        from_=from_number,
        body=template.render(values),
        account_sid=account_sid,
        status_callback=STATUS_CALLBACK,
        content_sid=template.sid,
        content_variables={"1": values["first_name"], "2": values["appointment"]},
        within_window=within_window,
    )


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def idempotency_key(record: AirtableRecord, request: TwilioRequest) -> str:
    """What makes a send unique.

    The record id alone is not enough: a later, genuinely different message to
    the same record would be suppressed as a duplicate. The message body is not
    enough either, because two records can carry the same text. So the key is
    the record id and a digest of the message together, which suppresses a
    retry and allows a real second message.
    """
    digest = hashlib.sha256(
        f"{request.to}|{request.body}".encode("utf-8")).hexdigest()[:16]
    return f"{record.record_id}:{digest}"


# ---------------------------------------------------------------------------
# Twilio errors
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TwilioError:
    code: int
    label: str
    retryable: bool
    meaning: str
    action: str


ERROR_NONE = 0

TWILIO_ERRORS: tuple[TwilioError, ...] = (
    TwilioError(
        21211, "Invalid To number", False,
        "Twilio could not parse the destination as a phone number.",
        "Fix the record in Airtable. Retrying fails identically every time and "
        "burns an operation on each attempt.",
    ),
    TwilioError(
        63003, "Channel could not find the To address", False,
        "The number is valid but has no WhatsApp account.",
        "Fall back to SMS or email for this record and mark the field in "
        "Airtable, because the number will never resolve on WhatsApp.",
    ),
    TwilioError(
        63016, "Free form message outside the allowed window", False,
        "More than twenty four hours have passed since the customer last "
        "messaged, so only an approved template may be sent.",
        "Send the approved template through ContentSid instead of a Body. The "
        "router does this already when the window is marked closed.",
    ),
    TwilioError(
        21610, "Recipient has opted out", False,
        "The customer replied STOP, and Twilio blocks further messages.",
        "Set the record to Cancelled in Airtable. Sending again is a "
        "compliance problem, not a delivery problem.",
    ),
    TwilioError(
        20429, "Too many requests", True,
        "The account is sending faster than its allowed rate.",
        "Back off and retry. This is the one failure worth retrying, and the "
        "only one the scenario should queue rather than alert on.",
    ),
)


def error_by_code(code: int) -> TwilioError | None:
    for error in TWILIO_ERRORS:
        if error.code == code:
            return error
    return None


# ---------------------------------------------------------------------------
# Processing one record
# ---------------------------------------------------------------------------

SENT = "Sent"
DUPLICATE = "Duplicate suppressed"
SKIPPED = "Skipped, status not ready"
BLOCKED = "Blocked before Twilio"
FAILED = "Twilio rejected it"
QUEUED = "Queued for retry"

OUTCOME_ORDER: tuple[str, ...] = (SENT, DUPLICATE, SKIPPED, BLOCKED, FAILED,
                                  QUEUED)


@dataclass(frozen=True)
class RunResult:
    record: AirtableRecord
    outcome: str
    detail: str
    message_sid: str = ""
    request: TwilioRequest | None = None
    error: TwilioError | None = None
    phone: PhoneResult | None = None
    key: str = ""

    @property
    def alert(self) -> bool:
        return self.outcome in (BLOCKED, FAILED)


@dataclass
class AutomationLedger:
    """What the Make scenario has to keep between runs. In production this is
    a table in Airtable or a data store; the shape is the same."""
    sent_keys: dict = field(default_factory=dict)
    results: list = field(default_factory=list)

    def has(self, key: str) -> bool:
        return key in self.sent_keys

    def count(self, outcome: str) -> int:
        return sum(1 for result in self.results if result.outcome == outcome)

    @property
    def alerts(self) -> list:
        return [result for result in self.results if result.alert]

    @property
    def queued(self) -> list:
        """Records held for a retry. Not alerts: a rate limit is the one
        failure that clears itself, so it needs a queue rather than a person."""
        return [result for result in self.results if result.outcome == QUEUED]

    def rows(self) -> list[dict]:
        return [
            {"Record": result.record.record_id,
             "Name": result.record.name,
             "Status": result.record.status,
             "Outcome": result.outcome,
             "Message SID": result.message_sid or "none",
             "Detail": result.detail}
            for result in self.results
        ]

    def alert_rows(self) -> list[dict]:
        return [
            {"Record": result.record.record_id,
             "Phone as typed": result.record.phone or "empty",
             "Error": (str(result.error.code) if result.error
                       else (result.phone.error_code if result.phone else "")),
             "Retryable": ("Yes" if result.error and result.error.retryable
                           else "No"),
             "What to do": (result.error.action if result.error
                            else (result.phone.message if result.phone else ""))}
            for result in self.alerts
        ]


def message_sid(key: str) -> str:
    """A stable stand in for the SID Twilio returns.

    Derived from the idempotency key so a given send always reports the same
    SID, which is what makes the ledger readable in a test and on screen. A
    real SID comes back from the API and is not predictable.
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return f"SM{digest}"


def process_record(ledger: AutomationLedger, record: AirtableRecord,
                   within_window: bool = True, failure_code: int = ERROR_NONE,
                   template: ContentTemplate = APPOINTMENT_TEMPLATE
                   ) -> RunResult:
    """Take one record all the way to a result, and never raise.

    That last part is the whole guard. A Make scenario that throws stops the
    run, and the records after this one are never processed at all. So every
    path here returns a result, including the path where something unforeseen
    goes wrong inside the formatter.
    """
    try:
        if record.status not in SENDING_STATUSES:
            return _record(ledger, RunResult(
                record, SKIPPED,
                f"Status is {record.status}, and only {STATUS_READY} sends. "
                f"A scenario that ignores the status is how a cancelled "
                f"booking gets a confirmation."))

        phone = normalise_phone(record.phone)
        if not phone.ok:
            return _record(ledger, RunResult(
                record, BLOCKED, phone.message, phone=phone,
                error=error_by_code(21211)))

        request = build_request(record, phone, within_window, template)
        key = idempotency_key(record, request)

        if ledger.has(key):
            return _record(ledger, RunResult(
                record, DUPLICATE,
                f"This exact message was already sent to {phone.e164} as "
                f"{ledger.sent_keys[key]}. Make retries a scenario that timed "
                f"out after succeeding, and without this the customer gets it "
                f"twice.",
                message_sid=ledger.sent_keys[key], request=request, phone=phone,
                key=key))

        error = error_by_code(failure_code) if failure_code else None
        if error is not None:
            outcome = QUEUED if error.retryable else FAILED
            return _record(ledger, RunResult(
                record, outcome,
                f"Twilio returned {error.code}, {error.label}. {error.meaning}",
                request=request, error=error, phone=phone, key=key))

        sid = message_sid(key)
        ledger.sent_keys[key] = sid
        return _record(ledger, RunResult(
            record, SENT,
            f"Delivered to {phone.e164} "
            f"{'as a free form message' if within_window else 'as template ' + template.name}.",
            message_sid=sid, request=request, phone=phone, key=key))

    except Exception as exc:  # noqa: BLE001
        # Deliberately broad. An unexpected exception here would stop the whole
        # scenario, so it becomes an alert on this record and the batch carries
        # on with the next one.
        return _record(ledger, RunResult(
            record, BLOCKED,
            f"The automation hit an unexpected error on this record and "
            f"skipped it rather than stopping the run: {type(exc).__name__}: "
            f"{exc}"))


def _record(ledger: AutomationLedger, result: RunResult) -> RunResult:
    ledger.results.append(result)
    return result


def run_batch(records: list[AirtableRecord],
              ledger: AutomationLedger | None = None,
              within_window: bool = True,
              failure_code: int = ERROR_NONE) -> AutomationLedger:
    target = ledger or AutomationLedger()
    for record in records:
        process_record(target, record, within_window, failure_code)
    return target


def sample_batch(now: str = "2026-09-13T09:00:00.000Z"
                 ) -> list[AirtableRecord]:
    """A batch with every case the guard exists for, built by hand so the run
    is identical on every render."""
    return [
        AirtableRecord("recA1b2C3d4E5f6G", "Amara Okafor", PHONE_LOCAL,
                       STATUS_READY, "2026-09-15 at 10:30", now),
        AirtableRecord("recB2c3D4e5F6g7H", "Chidi Balogun", PHONE_SPACED,
                       STATUS_READY, "2026-09-15 at 11:00", now),
        AirtableRecord("recC3d4E5f6G7h8I", "Fatima Danjuma", PHONE_LETTERS,
                       STATUS_READY, "2026-09-16 at 09:15", now),
        AirtableRecord("recD4e5F6g7H8i9J", "Ope Adeyemi", PHONE_EMPTY,
                       STATUS_READY, "2026-09-16 at 14:00", now),
        AirtableRecord("recE5f6G7h8I9j0K", "Zainab Sani", PHONE_CLEAN,
                       STATUS_CANCELLED, "2026-09-17 at 12:00", now),
        AirtableRecord("recF6g7H8i9J0k1L", "Tunde Eze", PHONE_BRACKETED,
                       STATUS_HOLD, "2026-09-17 at 16:30", now),
        # The same record Make sends again after a timeout.
        AirtableRecord("recA1b2C3d4E5f6G", "Amara Okafor", PHONE_LOCAL,
                       STATUS_READY, "2026-09-15 at 10:30", now),
    ]


def kpi_counts(ledger: AutomationLedger) -> dict[str, int]:
    return {outcome: ledger.count(outcome) for outcome in OUTCOME_ORDER}
