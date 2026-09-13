"""WasteTab logistics and financial engine.

A waste enquiry arrives from a WordPress form, a local carrier is found, a price
is quoted, and a payment link is sent. The money part is where this kind of
platform quietly loses margin, and the operations part is where it quietly
loses customers.

  A payment link built from the price rather than grossed up for the
  processor's fee arrives short: charge 130 and 126.71 lands, so the margin
  pays the fee on every single job. And a skip loaded with something other
  than what was quoted has to stop the flow, not be absorbed, because a carrier
  turning up to a different job either refuses it or eats the difference.

So every amount here is integer pence, the gross up is verified against the
processor's own rounding rather than trusted to a formula, and the state machine
refuses a transition it does not recognise instead of writing it anyway.

Pure logic, no Streamlit import, so this is unit testable on its own and can sit
behind the real WordPress webhook. Deterministic: nothing here reads the clock
or a random source unless the caller passes one in.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

ENGINE_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

# Every amount in this module is an integer number of pence. Floats are the
# reason a gross up lands a penny short: 0.1 + 0.2 is not 0.3, and a payment
# processor does not round the way a float does.
def pounds(pence: int) -> str:
    sign = "-" if pence < 0 else ""
    pence = abs(int(pence))
    return f"{sign}£{pence // 100}.{pence % 100:02d}"


def to_pence(amount: float | str) -> int:
    """Read pounds as a person types them, and refuse what is not money."""
    text = str(amount).strip().replace("£", "").replace(",", "")
    if not text:
        raise ValueError("Enter an amount in pounds.")
    try:
        value = Decimal(text)
    except Exception:  # noqa: BLE001
        raise ValueError(f"{amount!r} is not an amount of money.") from None
    if value < 0:
        raise ValueError("An amount cannot be negative.")
    return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# GoDaddy Payments card rate on the account this was built for.
FEE_PERCENT = Decimal("0.023")
FEE_FIXED_PENCE = 30

DEFAULT_MARGIN_PERCENT = Decimal("30")


def processor_fee(gross_pence: int) -> int:
    """What the processor actually deducts from a given charge.

    The percentage part is rounded to the penny by the processor before the
    fixed part is added, which is exactly the step a one line gross up formula
    misses and why the result can land a penny short.
    """
    if gross_pence < 0:
        raise ValueError("A charge cannot be negative.")
    percentage = (Decimal(gross_pence) * FEE_PERCENT).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP)
    return int(percentage) + FEE_FIXED_PENCE


def net_after_fee(gross_pence: int) -> int:
    return gross_pence - processor_fee(gross_pence)


def gross_up(net_pence: int) -> int:
    """The smallest charge that still leaves the target net after the fee.

    The formula gives the starting point. The loop is what makes it true: the
    processor rounds its percentage to the penny, so the algebraic answer can
    land a penny either side. Rather than trust it, this checks the result
    against the same arithmetic the processor uses and walks to the smallest
    charge that clears the target.
    """
    if net_pence < 0:
        raise ValueError("A net amount cannot be negative.")
    if net_pence == 0:
        return 0

    estimate = ((Decimal(net_pence) + FEE_FIXED_PENCE) / (1 - FEE_PERCENT))
    gross = int(estimate.quantize(Decimal("1"), rounding=ROUND_CEILING))

    while net_after_fee(gross) < net_pence:
        gross += 1
    while gross > 0 and net_after_fee(gross - 1) >= net_pence:
        gross -= 1
    return gross


@dataclass(frozen=True)
class Quote:
    carrier_bid_pence: int
    margin_percent: Decimal
    margin_pence: int
    net_target_pence: int
    charge_pence: int
    fee_pence: int

    @property
    def net_received_pence(self) -> int:
        return net_after_fee(self.charge_pence)

    @property
    def surplus_pence(self) -> int:
        """Anything above the target, which is the rounding penny at most."""
        return self.net_received_pence - self.net_target_pence

    @property
    def naive_charge_pence(self) -> int:
        """What a platform that forgot to gross up would have charged."""
        return self.net_target_pence

    @property
    def naive_shortfall_pence(self) -> int:
        return self.net_target_pence - net_after_fee(self.naive_charge_pence)

    def rows(self) -> list[dict]:
        return [
            {"Line": "Carrier bid", "Amount": pounds(self.carrier_bid_pence)},
            {"Line": f"Margin at {self.margin_percent:g} percent",
             "Amount": pounds(self.margin_pence)},
            {"Line": "Net needed before payout",
             "Amount": pounds(self.net_target_pence)},
            {"Line": "Processor fee at 2.3 percent plus 30p",
             "Amount": pounds(self.fee_pence)},
            {"Line": "Payment link amount",
             "Amount": pounds(self.charge_pence)},
            {"Line": "Net actually received",
             "Amount": pounds(self.net_received_pence)},
        ]


def build_quote(carrier_bid_pence: int,
                margin_percent: Decimal = DEFAULT_MARGIN_PERCENT) -> Quote:
    """Bid, margin, then gross up. In that order, because the fee is charged on
    the amount the customer pays rather than on the margin."""
    if carrier_bid_pence < 0:
        raise ValueError("A carrier bid cannot be negative.")
    if margin_percent < 0:
        raise ValueError("A margin cannot be negative.")

    margin = int((Decimal(carrier_bid_pence) * Decimal(margin_percent) / 100)
                 .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    net_target = carrier_bid_pence + margin
    charge = gross_up(net_target)
    return Quote(carrier_bid_pence=carrier_bid_pence,
                 margin_percent=Decimal(margin_percent), margin_pence=margin,
                 net_target_pence=net_target, charge_pence=charge,
                 fee_pence=processor_fee(charge))


# ---------------------------------------------------------------------------
# Postcodes and carriers
# ---------------------------------------------------------------------------

# The published UK postcode pattern. Kept strict on purpose: a form that accepts
# anything sends a van to a place that does not exist.
POSTCODE_PATTERN = re.compile(
    r"^([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})$", re.IGNORECASE)

ERR_POSTCODE_EMPTY = "POSTCODE_EMPTY"
ERR_POSTCODE_SHAPE = "POSTCODE_NOT_RECOGNISED"


@dataclass(frozen=True)
class PostcodeResult:
    ok: bool
    formatted: str = ""
    outward: str = ""
    area: str = ""
    error_code: str = ""
    message: str = ""


def parse_postcode(raw: str) -> PostcodeResult:
    """Normalise a UK postcode, or refuse it with the reason.

    The outward code is what routing runs on. The inward half identifies the
    street and tells a dispatcher nothing about which depot is nearest.
    """
    text = str(raw).strip().upper().replace("-", " ")
    if not text:
        return PostcodeResult(False, error_code=ERR_POSTCODE_EMPTY,
                              message="Enter a postcode. Dispatch cannot "
                                      "guess a region from a town name that "
                                      "appears in four counties.")
    collapsed = re.sub(r"\s+", "", text)
    found = POSTCODE_PATTERN.match(collapsed)
    if not found:
        return PostcodeResult(
            False, error_code=ERR_POSTCODE_SHAPE,
            message=f"{raw!r} is not a UK postcode. The enquiry is held for a "
                    f"human rather than dispatched to whoever happens to be "
                    f"first in the list.")
    outward, inward = found.group(1).upper(), found.group(2).upper()
    area = re.match(r"^[A-Z]{1,2}", outward).group(0)
    return PostcodeResult(True, f"{outward} {inward}", outward, area)


SKIP_4 = "4 yard skip"
SKIP_8 = "8 yard skip"
SKIP_12 = "12 yard skip"
GRAB = "Grab lorry"
SERVICES: tuple[str, ...] = (SKIP_4, SKIP_8, SKIP_12, GRAB)

WASTE_GENERAL = "General household"
WASTE_CONSTRUCTION = "Construction and rubble"
WASTE_GREEN = "Green waste"
WASTE_HAZARDOUS = "Hazardous"
WASTE_TYPES: tuple[str, ...] = (WASTE_GENERAL, WASTE_CONSTRUCTION,
                                WASTE_GREEN, WASTE_HAZARDOUS)


@dataclass(frozen=True)
class Carrier:
    code: str
    name: str
    areas: tuple[str, ...]
    services: tuple[str, ...]
    waste_types: tuple[str, ...]
    bid_pence: int
    rating: float
    licence: str
    active: bool = True


CARRIERS: tuple[Carrier, ...] = (
    Carrier("CAR-01", "Thameside Clearance", ("SE", "SW", "CR"),
            (SKIP_4, SKIP_8, GRAB),
            (WASTE_GENERAL, WASTE_CONSTRUCTION, WASTE_GREEN), 9800, 4.7,
            "CBDU183422"),
    Carrier("CAR-02", "Meridian Waste", ("SE", "BR", "DA"),
            (SKIP_8, SKIP_12),
            (WASTE_GENERAL, WASTE_CONSTRUCTION), 10400, 4.4, "CBDU209117"),
    Carrier("CAR-03", "Pennine Skips", ("LS", "BD", "HX"),
            (SKIP_4, SKIP_8, SKIP_12),
            (WASTE_GENERAL, WASTE_CONSTRUCTION, WASTE_GREEN), 8600, 4.8,
            "CBDU771903"),
    Carrier("CAR-04", "Clyde Environmental", ("G", "PA", "ML"),
            (SKIP_8, GRAB),
            (WASTE_GENERAL, WASTE_CONSTRUCTION, WASTE_HAZARDOUS), 11200, 4.2,
            "SEPA/WCL/4471"),
    Carrier("CAR-05", "Severn Haulage", ("BS", "BA", "GL"),
            (SKIP_4, SKIP_8, SKIP_12, GRAB),
            (WASTE_GENERAL, WASTE_GREEN), 9100, 4.5, "CBDU556201"),
    Carrier("CAR-06", "Dormant Skips", ("SE", "SW"),
            (SKIP_4, SKIP_8), (WASTE_GENERAL,), 7400, 3.1, "CBDU110044",
            active=False),
)

NO_MATCH_AREA = "No carrier covers that area"
NO_MATCH_SERVICE = "No local carrier offers that service"
NO_MATCH_WASTE = "No local carrier is licensed for that waste"


@dataclass(frozen=True)
class DispatchResult:
    postcode: PostcodeResult
    matches: list
    reason: str

    @property
    def matched(self) -> bool:
        return bool(self.matches)

    @property
    def best(self) -> Carrier | None:
        return self.matches[0] if self.matches else None

    def rows(self) -> list[dict]:
        return [
            {"Carrier": carrier.name, "Code": carrier.code,
             "Areas": ", ".join(carrier.areas),
             "Bid": pounds(carrier.bid_pence),
             "Rating": f"{carrier.rating:.1f}",
             "Licence": carrier.licence}
            for carrier in self.matches
        ]


def dispatch(postcode: str, service: str = SKIP_8,
             waste_type: str = WASTE_GENERAL,
             carriers=CARRIERS) -> DispatchResult:
    """Find the carriers that can actually take this job.

    Ordered by bid, then by rating. Cheapest first is what the margin depends
    on, and rating breaks the tie because two carriers at the same price are
    not the same carrier.
    """
    parsed = parse_postcode(postcode)
    if not parsed.ok:
        return DispatchResult(parsed, [], parsed.message)

    live = [carrier for carrier in carriers if carrier.active]
    in_area = [carrier for carrier in live if parsed.area in carrier.areas]
    if not in_area:
        return DispatchResult(
            parsed, [],
            f"{NO_MATCH_AREA}. Nothing covers {parsed.area}, so the enquiry "
            f"goes to the partner desk rather than to a carrier who would "
            f"quote a distance they will not drive.")

    with_service = [carrier for carrier in in_area
                    if service in carrier.services]
    if not with_service:
        return DispatchResult(
            parsed, [],
            f"{NO_MATCH_SERVICE}. {len(in_area)} carrier(s) cover "
            f"{parsed.area} and none of them runs a {service.lower()}.")

    licensed = [carrier for carrier in with_service
                if waste_type in carrier.waste_types]
    if not licensed:
        return DispatchResult(
            parsed, [],
            f"{NO_MATCH_WASTE}. {waste_type} needs a licence none of the "
            f"{len(with_service)} local carrier(s) holds, and sending it "
            f"anyway is the load that comes back.")

    ranked = sorted(licensed, key=lambda carrier: (carrier.bid_pence,
                                                   -carrier.rating))
    return DispatchResult(
        parsed, ranked,
        f"{len(ranked)} carrier(s) cover {parsed.area} for a "
        f"{service.lower()} of {waste_type.lower()}. Cheapest first, rating "
        f"breaking the tie.")


# ---------------------------------------------------------------------------
# The job state machine
# ---------------------------------------------------------------------------

NEW_ENQUIRY = "NEW ENQUIRY"
QUOTE_SENT = "QUOTE SENT"
BOOKED = "BOOKED"
ON_HOLD = "ON HOLD, WASTE DIFFERS"
COMPLETED = "COMPLETED"
CANCELLED = "CANCELLED"

STATES: tuple[str, ...] = (NEW_ENQUIRY, QUOTE_SENT, BOOKED, ON_HOLD, COMPLETED,
                           CANCELLED)

# What may follow what. A transition not on this map is refused rather than
# written, because a job that reaches COMPLETED without passing BOOKED is a job
# nobody was paid for.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    NEW_ENQUIRY: (QUOTE_SENT, CANCELLED),
    QUOTE_SENT: (BOOKED, CANCELLED),
    BOOKED: (ON_HOLD, COMPLETED, CANCELLED),
    ON_HOLD: (BOOKED, CANCELLED),
    COMPLETED: (),
    CANCELLED: (),
}

TERMINAL: tuple[str, ...] = (COMPLETED, CANCELLED)


@dataclass(frozen=True)
class LedgerEntry:
    sequence: int
    at: str
    state_from: str
    state_to: str
    actor: str
    detail: str
    charge_pence: int = 0


@dataclass(frozen=True)
class Transition:
    ok: bool
    state: str
    message: str
    entry: LedgerEntry | None = None


@dataclass
class Job:
    reference: str
    postcode: str
    service: str
    waste_type: str
    state: str = NEW_ENQUIRY
    quote: Quote | None = None
    carrier: Carrier | None = None
    entries: list = field(default_factory=list)
    links: list = field(default_factory=list)

    @property
    def charged_pence(self) -> int:
        return sum(entry.charge_pence for entry in self.entries)

    @property
    def net_pence(self) -> int:
        return sum(net_after_fee(entry.charge_pence) for entry in self.entries
                   if entry.charge_pence)

    def rows(self) -> list[dict]:
        return [
            {"#": entry.sequence, "At": entry.at, "From": entry.state_from,
             "To": entry.state_to, "By": entry.actor,
             "Charged": pounds(entry.charge_pence) if entry.charge_pence
                        else "none",
             "Detail": entry.detail}
            for entry in self.entries
        ]


def move(job: Job, to_state: str, actor: str = "system", detail: str = "",
         at: str = "2026-09-14T09:00:00", charge_pence: int = 0) -> Transition:
    """Advance the job, or refuse the move and say why.

    Refusing is the point. A quote marked BOOKED without a payment, or a job
    completed from an enquiry, is an invoice nobody can explain later.
    """
    if to_state not in STATES:
        return Transition(False, job.state,
                          f"{to_state!r} is not a state this job has.")
    if job.state in TERMINAL:
        return Transition(False, job.state,
                          f"{job.reference} is already {job.state}, and a "
                          f"finished job does not move again.")
    if to_state not in TRANSITIONS[job.state]:
        allowed = ", ".join(TRANSITIONS[job.state]) or "nothing"
        return Transition(False, job.state,
                          f"{job.state} cannot go straight to {to_state}. "
                          f"From here the job may only go to {allowed}.")

    entry = LedgerEntry(len(job.entries) + 1, at, job.state, to_state, actor,
                        detail or f"Moved to {to_state}.", charge_pence)
    job.entries.append(entry)
    job.state = to_state
    return Transition(True, to_state,
                      f"{job.reference} moved to {to_state}.", entry)


# ---------------------------------------------------------------------------
# Payment links
# ---------------------------------------------------------------------------

LINK_QUOTE = "Quote"
LINK_SURCHARGE = "Surcharge"

PAY_BASE = "https://pay.wastetab.example/l"


@dataclass(frozen=True)
class PaymentLink:
    reference: str
    kind: str
    amount_pence: int
    net_pence: int
    fee_pence: int
    url: str
    description: str


def payment_link(job: Job, amount_pence: int, kind: str = LINK_QUOTE,
                 description: str = "") -> PaymentLink:
    """Build the link the customer is sent.

    The reference is derived from the job and the amount, so the same request
    produces the same link and a duplicated send cannot create a second charge
    for the same money.
    """
    if amount_pence <= 0:
        raise ValueError("A payment link needs an amount above zero.")
    digest = hashlib.sha256(
        f"{job.reference}|{kind}|{amount_pence}".encode("utf-8")).hexdigest()
    reference = f"wt_{digest[:20]}"
    link = PaymentLink(
        reference=reference, kind=kind, amount_pence=amount_pence,
        net_pence=net_after_fee(amount_pence),
        fee_pence=processor_fee(amount_pence),
        url=f"{PAY_BASE}/{reference}",
        description=description or f"{kind} for {job.reference}, "
                                   f"{job.service} at {job.postcode}")
    job.links.append(link)
    return link


def link_rows(job: Job) -> list[dict]:
    return [
        {"Reference": link.reference, "Kind": link.kind,
         "Charged": pounds(link.amount_pence),
         "Fee": pounds(link.fee_pence),
         "Net": pounds(link.net_pence),
         "Link": link.url}
        for link in job.links
    ]


# ---------------------------------------------------------------------------
# The safety valve
# ---------------------------------------------------------------------------

# What the carrier finds that differs from what was quoted, and what it adds.
SURCHARGE_BANDS: dict[str, int] = {
    WASTE_CONSTRUCTION: 6500,
    WASTE_HAZARDOUS: 18500,
    WASTE_GREEN: 2500,
    WASTE_GENERAL: 0,
}

VALVE_NO_CHANGE = "Nothing materially different"
VALVE_PAUSED = "Paused, waiting on a surcharge"
VALVE_REFUSED = "Job cancelled, surcharge declined"
VALVE_RESUMED = "Surcharge paid, job resumed"


@dataclass(frozen=True)
class ValveResult:
    triggered: bool
    outcome: str
    message: str
    surcharge: PaymentLink | None = None
    found_waste: str = ""


def trigger_safety_valve(job: Job, found_waste: str,
                         margin_percent: Decimal = DEFAULT_MARGIN_PERCENT,
                         at: str = "2026-09-14T11:20:00") -> ValveResult:
    """Stop the flow when the load is not what was quoted.

    The alternative is absorbing it, which means the carrier is paid for a job
    they did not agree to do or the platform pays the difference itself. Both
    happen quietly, once per job, until somebody reads the margin at the end of
    the quarter.
    """
    if job.state != BOOKED:
        return ValveResult(False, VALVE_NO_CHANGE,
                           f"The valve only fires on a booked job, and "
                           f"{job.reference} is {job.state}.")
    if found_waste == job.waste_type:
        return ValveResult(False, VALVE_NO_CHANGE,
                           f"The load is the {job.waste_type.lower()} that was "
                           f"quoted, so nothing is paused and the job runs.")

    uplift = SURCHARGE_BANDS.get(found_waste, 0)
    if uplift <= 0:
        return ValveResult(
            False, VALVE_NO_CHANGE,
            f"{found_waste} carries no uplift over {job.waste_type.lower()}, "
            f"so the difference is absorbed rather than billed. Stopping a van "
            f"over nothing costs more than the difference.")

    quote = build_quote(uplift, margin_percent)
    move(job, ON_HOLD, actor="carrier",
         detail=f"Carrier found {found_waste.lower()} against "
                f"{job.waste_type.lower()} quoted. Collection paused.", at=at)
    link = payment_link(job, quote.charge_pence, LINK_SURCHARGE,
                        f"Supplementary charge for {found_waste.lower()} "
                        f"found on site at {job.postcode}")
    return ValveResult(
        True, VALVE_PAUSED,
        f"{found_waste} found against {job.waste_type.lower()} quoted. The "
        f"collection is paused and a supplementary link for "
        f"{pounds(quote.charge_pence)} has been sent, which leaves "
        f"{pounds(quote.net_target_pence)} after the fee.",
        surcharge=link, found_waste=found_waste)


def resolve_safety_valve(job: Job, paid: bool,
                         at: str = "2026-09-14T11:45:00") -> ValveResult:
    """Either the surcharge is paid and the van carries on, or it is not."""
    if job.state != ON_HOLD:
        return ValveResult(False, VALVE_NO_CHANGE,
                           f"{job.reference} is {job.state}, so there is no "
                           f"paused collection to resolve.")
    surcharge = next((link for link in reversed(job.links)
                      if link.kind == LINK_SURCHARGE), None)
    if paid:
        move(job, BOOKED, actor="customer",
             detail=f"Surcharge {surcharge.reference if surcharge else ''} "
                    f"paid. Collection resumed.",
             at=at,
             charge_pence=surcharge.amount_pence if surcharge else 0)
        return ValveResult(True, VALVE_RESUMED,
                           f"Surcharge paid. {job.reference} is booked again "
                           f"and the carrier has been released.",
                           surcharge=surcharge)
    move(job, CANCELLED, actor="customer",
         detail="Surcharge declined. Collection cancelled on site.", at=at)
    return ValveResult(True, VALVE_REFUSED,
                       f"Surcharge declined, so {job.reference} is cancelled "
                       f"rather than run at a loss. The carrier is paid the "
                       f"call out and the customer keeps the waste.",
                       surcharge=surcharge)


# ---------------------------------------------------------------------------
# The WordPress enquiry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Enquiry:
    reference: str
    name: str
    email: str
    phone: str
    postcode: str
    service: str
    waste_type: str
    notes: str
    submitted_at: str

    def as_payload(self) -> dict:
        """The shape the WordPress form plugin posts to the webhook."""
        return {
            "form_id": "wastetab-enquiry",
            "entry_id": self.reference,
            "submitted_at": self.submitted_at,
            "fields": {
                "name": self.name,
                "email": self.email,
                "phone": self.phone,
                "postcode": self.postcode,
                "service": self.service,
                "waste_type": self.waste_type,
                "notes": self.notes,
            },
        }


def sample_enquiry(postcode: str = "SE15 4RT", service: str = SKIP_8,
                   waste_type: str = WASTE_GENERAL,
                   reference: str = "WT-20260914-0031") -> Enquiry:
    return Enquiry(
        reference=reference, name="Amara Okafor",
        email="amara.okafor@example.com", phone="07700 900431",
        postcode=postcode, service=service, waste_type=waste_type,
        notes="Rear access through the side gate, dropped kerb available.",
        submitted_at="2026-09-14T08:42:00")


def open_job(enquiry: Enquiry) -> Job:
    return Job(reference=enquiry.reference, postcode=enquiry.postcode,
               service=enquiry.service, waste_type=enquiry.waste_type)


def kpi_counts(job: Job) -> dict:
    return {
        "state": job.state,
        "entries": len(job.entries),
        "charged": job.charged_pence,
        "net": job.net_pence,
        "links": len(job.links),
    }
