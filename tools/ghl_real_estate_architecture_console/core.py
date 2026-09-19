"""GHL Real Estate Architecture Console: the engine.

One modelling decision decides whether a real estate CRM reports the truth,
and it is made in the first week and discovered in the first board pack.

A contact is a person. A property is the thing being transacted. A single
sale has a buyer, a seller, two agents and a lender attached to it, and if
the pipeline value lives on the contact then that one sale is counted once
per person who touched it. The overstatement is not a rounding issue. It is
the deal value multiplied by the number of people in the room.

Three parts, each holding one property that a test can check.

1. Address normalisation decides what counts as the same property. The unit
   number is the field that gets dropped, and dropping it merges two
   households into one record. The mirror case matters as much: two spellings
   of one address must merge, or the same property arrives twice and the
   double counting starts from a different direction.
2. A multi signer envelope is complete only when every signer has completed,
   and a single decline ends it whatever anybody else did. Marking an
   envelope complete on the first signature is the bug that moves a deal to
   under contract before it is.
3. The reporting ledger computes both models against the same data and shows
   the gap as arithmetic rather than as an argument.

Nothing here contacts a CRM, a USPS service or a signing provider. True CASS
certification and a real ZIP plus four assignment need a licensed USPS data
set, which this does not have and does not pretend to have: what it produces
is a Publication 28 style standardisation, which is the part that decides
deduplication.

No Streamlit import lives here on purpose, so the same engine could sit behind
a real intake worker without a line changing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

ENGINE_VERSION = "1.0.0"

TWO_PLACES = Decimal("0.01")

SEVERITY_OK = "OK"
SEVERITY_WARN = "WARN"
SEVERITY_CRITICAL = "CRITICAL"


def money(value) -> Decimal:
    """Currency, rounded once, at the point it becomes currency."""
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


def _worst(findings) -> str:
    for level in (SEVERITY_CRITICAL, SEVERITY_WARN):
        if any(f.severity == level for f in findings):
            return level
    return SEVERITY_OK


# ---------------------------------------------------------------------------
# 1. Address normalisation and deduplication
# ---------------------------------------------------------------------------

# Publication 28 street suffix abbreviations. The list is the common subset
# rather than the whole standard, and an unmatched suffix is left as typed
# rather than guessed at, because inventing an abbreviation is how two
# different streets become one record.
SUFFIXES: dict = {
    "STREET": "ST", "ST": "ST", "STR": "ST",
    "AVENUE": "AVE", "AVE": "AVE", "AV": "AVE",
    "ROAD": "RD", "RD": "RD",
    "BOULEVARD": "BLVD", "BLVD": "BLVD", "BOUL": "BLVD",
    "DRIVE": "DR", "DR": "DR",
    "LANE": "LN", "LN": "LN",
    "COURT": "CT", "CT": "CT",
    "CIRCLE": "CIR", "CIR": "CIR",
    "PLACE": "PL", "PL": "PL",
    "TERRACE": "TER", "TER": "TER",
    "PARKWAY": "PKWY", "PKWY": "PKWY",
    "HIGHWAY": "HWY", "HWY": "HWY",
    "SQUARE": "SQ", "SQ": "SQ",
    "TRAIL": "TRL", "TRL": "TRL",
    "WAY": "WAY",
}

DIRECTIONALS: dict = {
    "NORTH": "N", "N": "N", "SOUTH": "S", "S": "S",
    "EAST": "E", "E": "E", "WEST": "W", "W": "W",
    "NORTHEAST": "NE", "NE": "NE", "NORTHWEST": "NW", "NW": "NW",
    "SOUTHEAST": "SE", "SE": "SE", "SOUTHWEST": "SW", "SW": "SW",
}

# Secondary unit designators. The unit is the field whose loss merges two
# households, so it is parsed deliberately rather than stripped as noise.
UNIT_DESIGNATORS: dict = {
    "APARTMENT": "APT", "APT": "APT", "APARTMENTS": "APT",
    "SUITE": "STE", "STE": "STE", "SUIT": "STE",
    "UNIT": "UNIT",
    "BUILDING": "BLDG", "BLDG": "BLDG",
    "FLOOR": "FL", "FL": "FL",
    "ROOM": "RM", "RM": "RM",
    "NUMBER": "APT", "NO": "APT",
}

STATES: tuple[str, ...] = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
)

ZIP_PATTERN = re.compile(r"^(\d{5})(?:[-\s]?(\d{4}))?$")

DEDUPE_NEW = "NEW RECORD"
DEDUPE_DUPLICATE = "DUPLICATE OF AN EXISTING RECORD"
DEDUPE_REJECTED = "REJECTED"


@dataclass(frozen=True)
class NormalisedAddress:
    raw: str
    primary_number: str
    street: str
    unit_designator: str
    unit_number: str
    city: str
    state: str
    zip5: str
    zip4: str
    standardised: str
    dedupe_key: str
    dedupe_status: str
    matched_existing: str
    parsed: bool
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def has_unit(self) -> bool:
        return bool(self.unit_number)


def _token(word: str, table: dict) -> str:
    return table.get(word.upper(), "")


def normalize_and_dedupe_lead(raw_address: str,
                              existing_keys: tuple[str, ...] = ()
                              ) -> NormalisedAddress:
    """Standardise an address and say whether it is already on file.

    The unit number is parsed and kept in the key. Two flats in one building
    are two properties, and a normaliser that drops the unit reports them as
    one, which merges two households and then double counts the building.
    """
    raw = str(raw_address or "").strip()
    findings: list[Finding] = []

    if not raw:
        findings.append(Finding(
            code="ADR-EMPTY", severity=SEVERITY_CRITICAL,
            title="No address was supplied",
            detail=("An intake with no address cannot be keyed to a "
                    "property, so it becomes a contact with nothing attached "
                    "and falls out of every property report."),
            fix="Make the address required at the form rather than at the worker."))
        return NormalisedAddress(
            raw=raw, primary_number="", street="", unit_designator="",
            unit_number="", city="", state="", zip5="", zip4="",
            standardised="", dedupe_key="", dedupe_status=DEDUPE_REJECTED,
            matched_existing="", parsed=False, findings=tuple(findings))

    # Split on commas where present, which is how a form field usually
    # arrives, and fall back to whitespace parsing where it is not.
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    street_part = parts[0] if parts else raw
    city = parts[1].upper() if len(parts) > 1 else ""
    state = ""
    zip5 = ""
    zip4 = ""

    if len(parts) > 2:
        tail = parts[2].upper().replace(",", " ").split()
        for token in tail:
            if token in STATES and not state:
                state = token
                continue
            match = ZIP_PATTERN.match(token)
            if match and not zip5:
                zip5 = match.group(1)
                zip4 = match.group(2) or ""

    # Pull the unit out of the street line before the suffix is touched, so
    # a unit designator is never mistaken for a street suffix.
    unit_designator = ""
    unit_number = ""
    words = street_part.replace(".", "").replace(",", " ").split()
    cleaned: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word.startswith("#") and len(word) > 1:
            unit_designator, unit_number = "APT", word[1:].upper()
            index += 1
            continue
        if word == "#" and index + 1 < len(words):
            unit_designator, unit_number = "APT", words[index + 1].upper()
            index += 2
            continue
        designator = _token(word, UNIT_DESIGNATORS)
        if designator and index + 1 < len(words):
            unit_designator = designator
            unit_number = words[index + 1].upper()
            index += 2
            continue
        cleaned.append(word)
        index += 1

    primary_number = ""
    if cleaned and re.match(r"^\d+[A-Za-z]?$", cleaned[0]):
        primary_number = cleaned[0].upper()
        cleaned = cleaned[1:]

    street_words: list[str] = []
    for position, word in enumerate(cleaned):
        upper = word.upper()
        directional = _token(word, DIRECTIONALS)
        suffix = _token(word, SUFFIXES)
        is_edge = position == 0 or position == len(cleaned) - 1
        if directional and is_edge:
            street_words.append(directional)
        elif suffix and position == len(cleaned) - 1:
            street_words.append(suffix)
        else:
            street_words.append(upper)
    street = " ".join(street_words)

    pieces = [primary_number, street]
    if unit_number:
        pieces.append(f"{unit_designator} {unit_number}")
    line_one = " ".join(p for p in pieces if p).strip()
    # USPS puts the city on one line and the state and ZIP together on the
    # next, with no comma between the state and the ZIP. Writing it the
    # other way is the tell that an address was assembled rather than
    # standardised.
    zip_full = f"{zip5}-{zip4}" if zip4 else zip5
    last_line = " ".join(b for b in (state, zip_full) if b)
    standardised = ", ".join(
        part for part in (line_one, city, last_line) if part)

    # The key deliberately carries the unit. Two flats in one building are
    # two properties and one key would make them one.
    dedupe_key = "|".join([
        primary_number, street, unit_designator, unit_number, city, state,
        zip5])

    parsed = bool(primary_number and street)
    known = set(existing_keys)
    duplicate = dedupe_key in known

    if not parsed:
        findings.append(Finding(
            code="ADR-UNPARSED", severity=SEVERITY_CRITICAL,
            title="The street line did not parse into a number and a street",
            detail=("Without both, the key is not distinguishing and every "
                    "such record collides with every other one like it."),
            fix=("Send it to manual review rather than creating the record. "
                 "A bad key is worse than a missing record, because the bad "
                 "key merges things.")))

    if unit_number:
        findings.append(Finding(
            code="ADR-UNIT-KEPT", severity=SEVERITY_OK,
            title=f"Unit {unit_designator} {unit_number} is part of the key",
            detail=("Two flats in one building are two properties. A "
                    "normaliser that strips the unit reports them as one, "
                    "which merges two households and then counts the "
                    "building once when it should count twice."),
            fix=("Keep the unit in the key everywhere, including in any "
                 "spreadsheet import that bypasses this worker.")))
    else:
        findings.append(Finding(
            code="ADR-NO-UNIT", severity=SEVERITY_WARN,
            title="No unit was found in this address",
            detail=("Correct for a single family home and wrong for a flat "
                    "whose unit the lead did not type. The record will merge "
                    "with any other unitless lead at the same street "
                    "address."),
            fix=("Ask for the unit on the form when the property type is a "
                 "flat, rather than inferring it later from nothing.")))

    if not state:
        findings.append(Finding(
            code="ADR-NO-STATE", severity=SEVERITY_WARN,
            title="No state was recognised",
            detail=("The same street name and number occurs in many states, "
                    "so a key without one collides across the country."),
            fix="Take the state from a select rather than from free text."))
    if not zip5:
        findings.append(Finding(
            code="ADR-NO-ZIP", severity=SEVERITY_WARN,
            title="No ZIP Code was recognised",
            detail="The ZIP is the cheapest disambiguator in the whole address.",
            fix="Validate the ZIP at the form, where the lead can still fix it."))

    findings.append(Finding(
        code="ADR-NOT-CASS", severity=SEVERITY_WARN,
        title="This is a Publication 28 style standardisation and not CASS certified",
        detail=("True CASS certification and a real ZIP plus four assignment "
                "need a licensed USPS data set. This has neither and is not "
                "pretending to: what it does is the standardisation that "
                "decides deduplication."),
        fix=("Run a certified service before anything is mailed. Use this "
             "for keys, not for postage.")))

    if duplicate:
        findings.append(Finding(
            code="ADR-DUPLICATE", severity=SEVERITY_OK,
            title="This property is already on file",
            detail=(f"Key {dedupe_key} matches an existing Property record, "
                    f"so the lead is attached to that property rather than "
                    f"creating a second one."),
            fix=("Attach the person as a new related contact. Two people "
                 "enquiring about one property is normal and is exactly the "
                 "case that breaks contact based reporting.")))

    status = (DEDUPE_REJECTED if not parsed else
              DEDUPE_DUPLICATE if duplicate else DEDUPE_NEW)

    return NormalisedAddress(
        raw=raw, primary_number=primary_number, street=street,
        unit_designator=unit_designator, unit_number=unit_number, city=city,
        state=state, zip5=zip5, zip4=zip4, standardised=standardised,
        dedupe_key=dedupe_key, dedupe_status=status,
        matched_existing=dedupe_key if duplicate else "", parsed=parsed,
        findings=tuple(findings),
    )


SAMPLE_ADDRESSES: tuple[str, ...] = (
    "1428 north maple street apartment 3b, Austin, TX 78704",
    "1428 N Maple St Apt 3B, Austin, TX 78704",
    "1428 N Maple St Apt 4, Austin, TX 78704",
    "1428 N Maple St, Austin, TX 78704",
    "77 Lakeview Boulevard Suite 210, Denver, CO 80202",
)


# ---------------------------------------------------------------------------
# 2. Multi signer contract tracking
# ---------------------------------------------------------------------------

SIGNER_NOT_SENT = "Not Sent"
SIGNER_SENT = "Sent"
SIGNER_VIEWED = "Viewed"
SIGNER_COMPLETED = "Completed"
SIGNER_DECLINED = "Declined"

SIGNER_STATES: tuple[str, ...] = (SIGNER_NOT_SENT, SIGNER_SENT, SIGNER_VIEWED,
                                  SIGNER_COMPLETED, SIGNER_DECLINED)

ENVELOPE_NOT_SENT = "Not Sent"
ENVELOPE_IN_PROGRESS = "In Progress"
ENVELOPE_COMPLETED = "Completed"
ENVELOPE_DECLINED = "Declined"

# What the pipeline should read for each envelope state. The mapping is one
# way and total: every envelope state has exactly one stage and no stage is
# reachable from two states, so the pipeline cannot disagree with the
# envelope.
PIPELINE_STAGE: dict = {
    ENVELOPE_NOT_SENT: "Contract Prepared",
    ENVELOPE_IN_PROGRESS: "Awaiting Signatures",
    ENVELOPE_COMPLETED: "Under Contract",
    ENVELOPE_DECLINED: "Negotiation Reopened",
}


@dataclass(frozen=True)
class SignerStatus:
    ordinal: int
    name: str
    role: str
    status: str
    completed: bool
    blocking: bool


@dataclass(frozen=True)
class EnvelopeStatus:
    property_id: str
    signers: tuple[SignerStatus, ...]
    envelope_status: str
    pipeline_stage: str
    completed_count: int
    outstanding_count: int
    declined_count: int
    is_complete: bool
    is_terminal: bool
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def reconciles(self) -> bool:
        """Every signer is counted once and only once."""
        return (self.completed_count + self.outstanding_count
                + self.declined_count == len(self.signers))


def simulate_multi_signer_contract(property_id: str,
                                   signers_list) -> EnvelopeStatus:
    """Derive the envelope state from every signer, not from the first one.

    An envelope is complete only when every signer has completed. A single
    decline ends it whatever anybody else did, because a contract with one
    party refusing is not partly signed, it is not signed.
    """
    identifier = str(property_id or "").strip()
    if not identifier:
        raise ValueError("an envelope has to be attached to a property")

    rows: list[SignerStatus] = []
    for index, entry in enumerate(signers_list or (), start=1):
        if isinstance(entry, dict):
            name = str(entry.get("name", "")).strip()
            role = str(entry.get("role", "Signer")).strip()
            status = str(entry.get("status", SIGNER_NOT_SENT)).strip()
        else:
            name, role, status = (list(entry) + ["", "Signer",
                                                 SIGNER_NOT_SENT])[:3]
            name, role, status = str(name).strip(), str(role).strip(), str(status).strip()
        if status not in SIGNER_STATES:
            raise ValueError(f"unknown signer status: {status!r}")
        if not name:
            raise ValueError("every signer needs a name")
        rows.append(SignerStatus(
            ordinal=index, name=name, role=role or "Signer", status=status,
            completed=status == SIGNER_COMPLETED,
            blocking=status != SIGNER_COMPLETED))

    if not rows:
        raise ValueError("an envelope with no signers cannot be tracked")

    completed = sum(1 for r in rows if r.status == SIGNER_COMPLETED)
    declined = sum(1 for r in rows if r.status == SIGNER_DECLINED)
    outstanding = len(rows) - completed - declined

    findings: list[Finding] = []

    if declined:
        envelope = ENVELOPE_DECLINED
    elif completed == len(rows):
        envelope = ENVELOPE_COMPLETED
    elif all(r.status == SIGNER_NOT_SENT for r in rows):
        envelope = ENVELOPE_NOT_SENT
    else:
        envelope = ENVELOPE_IN_PROGRESS

    stage = PIPELINE_STAGE[envelope]
    complete = envelope == ENVELOPE_COMPLETED
    terminal = envelope in (ENVELOPE_COMPLETED, ENVELOPE_DECLINED)

    if declined:
        who = ", ".join(r.name for r in rows if r.status == SIGNER_DECLINED)
        findings.append(Finding(
            code="ENV-DECLINED", severity=SEVERITY_CRITICAL,
            title=f"{who} declined, so the envelope is dead",
            detail=("A contract with one party refusing is not partly "
                    "signed. Every other signature on it is now void and any "
                    "reissue is a new envelope rather than a resend."),
            fix=("Move the pipeline to the reopened stage and void the "
                 "envelope explicitly, so a stale link cannot still be "
                 "signed by somebody who has not heard.")))
    elif complete:
        findings.append(Finding(
            code="ENV-COMPLETE", severity=SEVERITY_OK,
            title=f"All {len(rows)} signers completed",
            detail="The envelope is complete and the deal is under contract.",
            fix=("Store the executed copy against the Property record rather "
                 "than against one of the contacts, or it is findable only "
                 "by whoever remembers whose record it went on.")))
    elif outstanding:
        waiting = ", ".join(f"{r.name} ({r.status.lower()})" for r in rows
                            if r.blocking)
        findings.append(Finding(
            code="ENV-WAITING", severity=SEVERITY_WARN,
            title=f"{outstanding} of {len(rows)} signers outstanding",
            detail=f"Still waiting on {waiting}.",
            fix=("Chase the earliest blocker rather than the whole list. A "
                 "reminder to somebody who already signed reads as a system "
                 "that is not paying attention.")))

    if completed and not complete and not declined:
        findings.append(Finding(
            code="ENV-PARTIAL", severity=SEVERITY_CRITICAL,
            title=f"{completed} signature(s) in and the envelope is not complete",
            detail=("This is the state where a pipeline gets moved to under "
                    "contract too early. A partly signed contract binds "
                    "nobody, and a forecast built on it is counting a deal "
                    "that can still evaporate."),
            fix=("Drive the stage from the envelope state rather than from "
                 "a signature webhook. One signature is an event, not a "
                 "state.")))

    findings.append(Finding(
        code="ENV-PROPERTY", severity=SEVERITY_WARN,
        title="The envelope belongs to the property, not to a contact",
        detail=(f"Four or five people are attached to {identifier}. Hanging "
                f"the envelope off one of them makes the other four unable "
                f"to find it and makes the deal count more than once in any "
                f"contact based report."),
        fix="Attach it to the Property custom object and relate the signers to it."))

    headline = (f"{identifier}: {completed} of {len(rows)} signed, "
                f"{outstanding} outstanding, {declined} declined, envelope "
                f"is {envelope} and the pipeline reads {stage}")

    return EnvelopeStatus(
        property_id=identifier, signers=tuple(rows), envelope_status=envelope,
        pipeline_stage=stage, completed_count=completed,
        outstanding_count=outstanding, declined_count=declined,
        is_complete=complete, is_terminal=terminal, headline=headline,
        findings=tuple(findings),
    )


SAMPLE_SIGNERS: tuple[dict, ...] = (
    {"name": "Dana Ruiz", "role": "Seller", "status": SIGNER_COMPLETED},
    {"name": "Marcus Bell", "role": "Buyer", "status": SIGNER_VIEWED},
    {"name": "Priya Nadar", "role": "Listing agent", "status": SIGNER_COMPLETED},
    {"name": "Owen Frank", "role": "Buyer agent", "status": SIGNER_SENT},
)


# ---------------------------------------------------------------------------
# 3. The reporting ledger
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LedgerRow:
    property_id: str
    address: str
    deal_value: Decimal
    related_contacts: tuple[str, ...]
    contact_model_value: Decimal
    property_model_value: Decimal

    @property
    def contact_count(self) -> int:
        return len(self.related_contacts)

    @property
    def overstatement(self) -> Decimal:
        return money(self.contact_model_value - self.property_model_value)


@dataclass(frozen=True)
class ReportingLedger:
    rows: tuple[LedgerRow, ...]
    property_count: int
    contact_count: int
    distinct_contact_count: int
    true_pipeline_value: Decimal
    contact_model_total: Decimal
    property_model_total: Decimal
    overstatement: Decimal
    overstatement_pct: float
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def reconciles(self) -> bool:
        """Both totals equal the sum of their own rows, and the gap adds up."""
        contact_rows = money(sum(r.contact_model_value for r in self.rows))
        property_rows = money(sum(r.property_model_value for r in self.rows))
        return (contact_rows == self.contact_model_total
                and property_rows == self.property_model_total
                and money(self.property_model_total + self.overstatement)
                == self.contact_model_total)


def generate_reporting_ledger(contacts, properties) -> ReportingLedger:
    """Compute both models against the same data and show the gap as arithmetic.

    Under the contact model the deal value rides on every related contact, so
    one sale with five people attached is reported five times. Under the
    property model it rides on the property and is reported once. The gap is
    the deal value times the number of related contacts less one, summed, and
    it is not an estimate.
    """
    contact_lookup = {str(c.get("id")): c for c in (contacts or [])}
    rows: list[LedgerRow] = []
    seen_contacts: set = set()

    for entry in properties or []:
        property_id = str(entry.get("id", "")).strip()
        if not property_id:
            raise ValueError("every property needs an id")
        value = money(entry.get("deal_value", 0))
        if value < 0:
            raise ValueError("a deal value cannot be negative")
        related = tuple(str(c) for c in entry.get("contacts", ()))
        for contact_id in related:
            if contact_id not in contact_lookup:
                raise ValueError(
                    f"{property_id} names contact {contact_id} which does not exist")
            seen_contacts.add(contact_id)
        rows.append(LedgerRow(
            property_id=property_id,
            address=str(entry.get("address", "")).strip(),
            deal_value=value, related_contacts=related,
            contact_model_value=money(value * len(related)),
            property_model_value=value))

    if not rows:
        raise ValueError("a ledger needs at least one property")

    true_value = money(sum(r.deal_value for r in rows))
    contact_total = money(sum(r.contact_model_value for r in rows))
    property_total = money(sum(r.property_model_value for r in rows))
    gap = money(contact_total - property_total)
    gap_pct = (float(gap / property_total * 100) if property_total else 0.0)

    findings: list[Finding] = []

    findings.append(Finding(
        code="LED-PROPERTY-TRUE", severity=SEVERITY_OK,
        title=f"The property model reports {property_total}, which is the truth",
        detail=(f"Each of the {len(rows)} properties contributes its own "
                f"value once, however many people are attached to it."),
        fix=("Put the pipeline value on the Property custom object and "
             "relate contacts to it. This is the whole architectural "
             "decision and it is made once.")))

    if gap > 0:
        worst = max(rows, key=lambda r: r.overstatement)
        findings.append(Finding(
            code="LED-DOUBLE-COUNT", severity=SEVERITY_CRITICAL,
            title=f"The contact model reports {contact_total}, overstating by {gap}",
            detail=(f"That is {gap_pct:.1f} percent, and it is not a "
                    f"rounding issue. Every deal is counted once per person "
                    f"attached to it. The worst single case here is "
                    f"{worst.property_id} at {worst.deal_value} with "
                    f"{worst.contact_count} contacts, reported as "
                    f"{worst.contact_model_value}."),
            fix=("Move the value field off the contact. Reporting on it from "
                 "the contact cannot be fixed with a filter, because the "
                 "duplication is in the data model rather than in the "
                 "query.")))
    else:
        findings.append(Finding(
            code="LED-NO-GAP", severity=SEVERITY_WARN,
            title="The two models agree on this data",
            detail=("Every property here has at most one related contact, "
                    "which is the only case where contact based reporting "
                    "is accurate. Real estate is not that case for long."),
            fix=("Add a second contact to any property and the gap appears "
                 "immediately. Build for that now rather than migrating "
                 "later.")))

    findings.append(Finding(
        code="LED-MIGRATION", severity=SEVERITY_WARN,
        title="Changing this later is a migration rather than a setting",
        detail=("Once value lives on contacts, moving it means rebuilding "
                "every pipeline, every automation that reads a value field "
                "and every report, against live data with deals in flight."),
        fix=("Decide it in week one. It is the cheapest decision in the "
             "build and the most expensive to reverse.")))

    headline = (f"{len(rows)} properties worth {property_total} report as "
                f"{contact_total} on the contact model, an overstatement of "
                f"{gap} or {gap_pct:.1f} percent")

    return ReportingLedger(
        rows=tuple(rows), property_count=len(rows),
        contact_count=len(contact_lookup),
        distinct_contact_count=len(seen_contacts),
        true_pipeline_value=true_value, contact_model_total=contact_total,
        property_model_total=property_total, overstatement=gap,
        overstatement_pct=round(gap_pct, 1), headline=headline,
        findings=tuple(findings),
    )


SAMPLE_CONTACTS: tuple[dict, ...] = (
    {"id": "C-1", "name": "Dana Ruiz", "role": "Seller"},
    {"id": "C-2", "name": "Marcus Bell", "role": "Buyer"},
    {"id": "C-3", "name": "Priya Nadar", "role": "Listing agent"},
    {"id": "C-4", "name": "Owen Frank", "role": "Buyer agent"},
    {"id": "C-5", "name": "Lena Osei", "role": "Lender"},
    {"id": "C-6", "name": "Tom Wexler", "role": "Seller"},
)

SAMPLE_PROPERTIES: tuple[dict, ...] = (
    {"id": "P-1001", "address": "1428 N MAPLE ST APT 3B, AUSTIN, TX 78704",
     "deal_value": "485000", "contacts": ("C-1", "C-2", "C-3", "C-4", "C-5")},
    {"id": "P-1002", "address": "77 LAKEVIEW BLVD STE 210, DENVER, CO 80202",
     "deal_value": "1250000", "contacts": ("C-3", "C-6")},
    {"id": "P-1003", "address": "912 ELM RD, AUSTIN, TX 78745",
     "deal_value": "329000", "contacts": ("C-2",)},
)
