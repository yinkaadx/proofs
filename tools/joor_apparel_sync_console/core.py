"""JOOR, ApparelMagic, and Extensiv synchronisation engine.

No Streamlit import lives in this file, and money is Decimal throughout.

Wholesale apparel integrations fail quietly in three specific places, and
each stage here is built to make the quiet version impossible:

* a size run is a list of labels, and two systems rarely agree on the
  list. Mapping a buyer's size run onto a style's scale by position
  instead of by label turns size L into size 6 without any error, so this
  engine maps by label only and an order carrying a size the style does
  not have is refused whole rather than created short;
* available to sell is on hand minus allocated, and when that goes
  negative the units are already promised to someone. Clamping it to zero
  and publishing that is how a linesheet oversells, so a negative result
  is published as zero and reported as an oversold quantity at the same
  time;
* a wholesale order is completed after it is invoiced, and it is invoiced
  after it ships. Completing on a tracking number that was never
  validated closes the order in the buyer's portal while the box is still
  on the packing bench.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping, Sequence

ENGINE_VERSION = "1.0.0"

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"

_SEVERITY_ORDER = {SEVERITY_OK: 0, SEVERITY_WARN: 1, SEVERITY_CRITICAL: 2}

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


def _worst(findings: Sequence[Finding]) -> str:
    if not findings:
        return SEVERITY_OK
    return max((f.severity for f in findings), key=lambda s: _SEVERITY_ORDER[s])


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# 1. JOOR order ingestion into an ApparelMagic payload
# ---------------------------------------------------------------------------

SCALE_ALPHA = "ALPHA"
SCALE_NUMERIC = "NUMERIC_EVEN"
SCALE_WAIST = "WAIST"
SCALE_ONE_SIZE = "ONE_SIZE"

SIZE_SCALES: dict[str, tuple[str, ...]] = {
    SCALE_ALPHA: ("XS", "S", "M", "L", "XL", "XXL"),
    SCALE_NUMERIC: ("0", "2", "4", "6", "8", "10", "12", "14"),
    SCALE_WAIST: ("28", "30", "32", "34", "36", "38"),
    SCALE_ONE_SIZE: ("OS",),
}

ACCEPTED = "Accepted into ApparelMagic"
REJECTED = "Rejected, nothing was created"


@dataclass(frozen=True)
class Style:
    style_code: str
    description: str
    scale: str
    colors: tuple[tuple[str, str], ...]
    wholesale_price: Decimal

    @property
    def sizes(self) -> tuple[str, ...]:
        return SIZE_SCALES[self.scale]

    @property
    def color_codes(self) -> dict[str, str]:
        return {name: code for name, code in self.colors}


STYLE_CATALOGUE: tuple[Style, ...] = (
    Style("SS26-KNT-014", "Merino crew neck knit", SCALE_ALPHA,
          (("Oatmeal", "OAT"), ("Ink", "INK"), ("Moss", "MOS")),
          money("68.00")),
    Style("SS26-DRS-221", "Bias cut midi dress", SCALE_NUMERIC,
          (("Bone", "BON"), ("Clay", "CLY")),
          money("104.00")),
    Style("SS26-TRS-108", "Pleated wide leg trouser", SCALE_WAIST,
          (("Navy", "NVY"), ("Stone", "STN")),
          money("92.50")),
    Style("SS26-SCF-003", "Silk twill scarf", SCALE_ONE_SIZE,
          (("Poppy", "POP"),),
          money("46.00")),
)

_STYLES = {style.style_code: style for style in STYLE_CATALOGUE}


@dataclass(frozen=True)
class OrderLine:
    sku: str
    style_code: str
    color_name: str
    color_code: str
    size: str
    quantity: int
    unit_price: Decimal

    @property
    def extended_price(self) -> Decimal:
        return money(self.unit_price * self.quantity)


@dataclass(frozen=True)
class OrderPayload:
    po_number: str
    style_code: str
    scale: str
    lines: tuple[OrderLine, ...]
    units_submitted: int
    units_mapped: int
    units_rejected: int
    unmapped_sizes: tuple[str, ...]
    unmapped_colors: tuple[str, ...]
    order_value: Decimal
    status: str
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def accepted(self) -> bool:
        return self.status == ACCEPTED

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def size_breakdown(self) -> dict[str, int]:
        breakdown: dict[str, int] = {}
        for line in self.lines:
            breakdown[line.size] = breakdown.get(line.size, 0) + line.quantity
        return breakdown

    @property
    def color_breakdown(self) -> dict[str, int]:
        breakdown: dict[str, int] = {}
        for line in self.lines:
            breakdown[line.color_name] = (breakdown.get(line.color_name, 0)
                                          + line.quantity)
        return breakdown


def build_sku(style_code: str, color_code: str, size: str) -> str:
    return f"{style_code}-{color_code}-{size}"


def simulate_joor_order_ingestion(po_number: str, style_code: str,
                                  size_matrix: Mapping) -> OrderPayload:
    """Translate a JOOR size matrix into an ApparelMagic order payload.

    Sizes are matched by label and never by position. Position matching is
    the defect that hides: an alpha run of six labels lines up neatly
    against a numeric run of eight, so size L silently becomes size 6, the
    order passes validation, and the mistake is discovered when the buyer
    receives the wrong garments.

    Nothing is created if any size or colour fails to map. A wholesale
    order accepted short ships short, and a short shipment on a wholesale
    PO is a chargeback rather than a backorder.
    """
    po = str(po_number or "").strip()
    code = str(style_code or "").strip()
    findings: list[Finding] = []

    if not po:
        raise ValueError("a purchase order number is required")
    if code not in _STYLES:
        raise ValueError(f"unknown style {style_code!r}")

    style = _STYLES[code]
    valid_sizes = set(style.sizes)
    valid_colors = style.color_codes

    lines: list[OrderLine] = []
    submitted = 0
    rejected = 0
    unmapped_sizes: list[str] = []
    unmapped_colors: list[str] = []

    for color_name, runs in (size_matrix or {}).items():
        colour = str(color_name).strip()
        for size_label, quantity in (runs or {}).items():
            label = str(size_label).strip()
            units = int(quantity)
            if units < 0:
                raise ValueError("a size run cannot carry negative units")
            submitted += units
            if units == 0:
                continue

            colour_ok = colour in valid_colors
            size_ok = label in valid_sizes

            if not colour_ok and colour not in unmapped_colors:
                unmapped_colors.append(colour)
            if not size_ok and label not in unmapped_sizes:
                unmapped_sizes.append(label)

            if not (colour_ok and size_ok):
                rejected += units
                continue

            lines.append(OrderLine(
                sku=build_sku(code, valid_colors[colour], label),
                style_code=code, color_name=colour,
                color_code=valid_colors[colour], size=label,
                quantity=units, unit_price=style.wholesale_price))

    mapped = sum(line.quantity for line in lines)
    order_value = sum((line.extended_price for line in lines), ZERO)

    skus = [line.sku for line in lines]
    if len(skus) != len(set(skus)):
        findings.append(Finding(
            code="ORD-DUPSKU", severity=SEVERITY_CRITICAL,
            title="Two lines resolved to the same SKU",
            detail=("A colour code collision merges two buyer lines into "
                    "one warehouse item, and the second quantity overwrites "
                    "the first in most import routines rather than adding "
                    "to it."),
            fix="Make the colour code unique inside the style, not globally."))

    if unmapped_sizes or unmapped_colors:
        status = REJECTED
        findings.append(Finding(
            code="ORD-SCALE", severity=SEVERITY_CRITICAL,
            title=(f"{len(unmapped_sizes)} size(s) and "
                   f"{len(unmapped_colors)} colour(s) do not exist on "
                   f"{code}"),
            detail=(f"The style runs on the {style.scale} scale "
                    f"({', '.join(style.sizes)}). Sizes offered that the "
                    f"style does not carry: "
                    f"{', '.join(unmapped_sizes) or 'none'}. Colours: "
                    f"{', '.join(unmapped_colors) or 'none'}. Nothing was "
                    f"created, because an order accepted short ships short "
                    f"and a short wholesale shipment is a chargeback."),
            fix=("Fix the linesheet size run in JOOR so the buyer cannot "
                 "offer a size that does not exist, then resubmit the PO.")))
        findings.append(Finding(
            code="ORD-POSITION", severity=SEVERITY_CRITICAL,
            title="Never resolve this by matching on position",
            detail=("A six label alpha run lines up against the first six "
                    "of an eight label numeric run without complaint, so "
                    "position matching turns L into 6 and passes every "
                    "validation the import has."),
            fix="Map on the label, and refuse when the label is absent."))
    else:
        status = ACCEPTED
        findings.append(Finding(
            code="ORD-MAPPED", severity=SEVERITY_OK,
            title=(f"{mapped} unit(s) across {len(lines)} SKU(s) on the "
                   f"{style.scale} scale"),
            detail=("Every size and colour on the PO exists on the style, "
                    "matched by label. The unit count on the payload equals "
                    "the unit count on the purchase order."),
            fix="Reconcile units on the payload against the PO before send."))

    # A rejected order creates nothing, so it carries no lines. Returning
    # the successfully mapped ones beside a rejected status invites a
    # consumer to iterate them, which is exactly the partial order this
    # stage exists to prevent.
    if status == REJECTED:
        lines = []
        order_value = ZERO
        mapped = 0
        rejected = submitted

    findings.append(Finding(
        code="ORD-TIE",
        severity=(SEVERITY_OK if mapped + rejected == submitted
                  else SEVERITY_CRITICAL),
        title=(f"{mapped} mapped plus {rejected} rejected equals "
               f"{submitted} submitted"),
        detail=("Nothing is dropped silently. A unit that could not be "
                "mapped is counted as rejected, so the difference between "
                "the purchase order and the payload is always visible as a "
                "number rather than as a missing row."),
        fix="Alert on any rejected count above zero, not on a failed import."))

    headline = (f"{po} on {code}: {mapped} of {submitted} unit(s) mapped, "
                f"{len(lines)} SKU(s)")

    return OrderPayload(
        po_number=po, style_code=code, scale=style.scale,
        lines=tuple(lines), units_submitted=submitted, units_mapped=mapped,
        units_rejected=rejected, unmapped_sizes=tuple(unmapped_sizes),
        unmapped_colors=tuple(unmapped_colors), order_value=order_value,
        status=status, headline=headline, findings=tuple(findings))


SAMPLE_MATRIX_CLEAN: dict = {
    "Oatmeal": {"XS": 4, "S": 10, "M": 14, "L": 10, "XL": 6},
    "Ink": {"S": 8, "M": 12, "L": 8},
}

SAMPLE_MATRIX_BROKEN: dict = {
    "Oatmeal": {"XS": 4, "S": 10, "M": 14, "L": 10, "XXXL": 6},
    "Cobalt": {"S": 8, "M": 12},
}


# ---------------------------------------------------------------------------
# 2. Extensiv inventory reconciliation and available to sell
# ---------------------------------------------------------------------------

LINESHEET_PUBLISH = "Publish on the linesheet"
LINESHEET_HIDE = "Hide, nothing available"
LINESHEET_PULL = "Pull immediately and alert sales"

ATS_FLOOR = 0


@dataclass(frozen=True)
class InventoryPosition:
    sku: str
    physical_count: int
    allocated_orders: int
    available_to_sell: int
    oversold_units: int
    linesheet_status: str
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def oversold(self) -> bool:
        return self.oversold_units > 0

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def reconcile_extensiv_inventory(sku: str, physical_count: int,
                                 allocated_orders: int) -> InventoryPosition:
    """Compute available to sell, and never hide an oversold position.

    Available to sell is the physical count at the third party warehouse
    less the units already promised on open orders. When that arithmetic
    goes negative the units are not merely unavailable, they are already
    sold to somebody, and that is a different problem from being out of
    stock.

    Publishing a clamped zero is how a linesheet oversells: the sales team
    sees an item that is simply sold out, nobody investigates, and the
    shortfall surfaces at allocation. This returns the clamped zero for
    the linesheet and the oversold quantity alongside it, so the second
    number cannot be lost.
    """
    item = str(sku or "").strip()
    physical = int(physical_count)
    allocated = int(allocated_orders)
    findings: list[Finding] = []

    if not item:
        raise ValueError("a SKU is required")
    if physical < 0:
        raise ValueError("a physical count cannot be negative")
    if allocated < 0:
        raise ValueError("an allocation cannot be negative")

    raw = physical - allocated
    available = max(ATS_FLOOR, raw)
    oversold = max(0, -raw)

    if oversold:
        status = LINESHEET_PULL
        findings.append(Finding(
            code="INV-OVERSOLD", severity=SEVERITY_CRITICAL,
            title=f"{oversold} unit(s) are promised and do not exist",
            detail=(f"The warehouse holds {physical} and {allocated} are "
                    f"already on open orders. These units are sold, not "
                    f"merely unavailable, so somebody is going to receive a "
                    f"short shipment unless the allocation is changed "
                    f"first."),
            fix=("Pull the SKU from the linesheet now, then decide which "
                 "orders get cut. Deciding at pick time means the decision "
                 "is made by whoever picks first.")))
    elif available == 0:
        status = LINESHEET_HIDE
        findings.append(Finding(
            code="INV-EMPTY", severity=SEVERITY_WARN,
            title="Exactly nothing left to sell",
            detail=("Physical and allocated are equal. The position is "
                    "clean, and it is one return or one cancellation away "
                    "from being sellable again."),
            fix="Hide rather than delete, so a return republishes it."))
    else:
        status = LINESHEET_PUBLISH
        findings.append(Finding(
            code="INV-ATS", severity=SEVERITY_OK,
            title=f"{available} unit(s) available to sell",
            detail=(f"{physical} physical less {allocated} allocated. The "
                    f"physical count is the third party warehouse position "
                    f"and the allocation is the order book position, so "
                    f"this number is only true if both were read at the "
                    f"same moment."),
            fix=("Stamp both reads with the same timestamp and refuse the "
                 "reconciliation when they differ by more than a cycle.")))

    findings.append(Finding(
        code="INV-CLAMP", severity=SEVERITY_WARN,
        title="The linesheet gets zero, the report gets the true shortfall",
        detail=("A negative available to sell cannot be published, so it is "
                "clamped. Clamping without reporting the oversold quantity "
                "beside it is the defect: the linesheet then shows sold out, "
                "which reads as normal, and nothing tells anyone that units "
                "were promised twice."),
        fix="Alert on the oversold count, never on the clamped count."))

    findings.append(Finding(
        code="INV-TIE", severity=SEVERITY_OK,
        title=f"{allocated} allocated equals {min(physical, allocated)} "
              f"covered plus {oversold} oversold",
        detail=("Every allocated unit is either covered by stock in the "
                "warehouse or is oversold. There is no third category, and "
                "the two numbers always sum back to the allocation."),
        fix="Reconcile this identity nightly, not at month end."))

    headline = (f"{item}: {available} available to sell"
                + (f", {oversold} oversold" if oversold else ""))

    return InventoryPosition(
        sku=item, physical_count=physical, allocated_orders=allocated,
        available_to_sell=available, oversold_units=oversold,
        linesheet_status=status, headline=headline,
        findings=tuple(findings))


SAMPLE_INVENTORY: tuple[tuple[str, int, int], ...] = (
    ("SS26-KNT-014-OAT-M", 140, 96),
    ("SS26-KNT-014-INK-L", 32, 32),
    ("SS26-DRS-221-BON-4", 18, 44),
    ("SS26-TRS-108-NVY-32", 210, 12),
)


# ---------------------------------------------------------------------------
# 3. Fulfillment tracking, invoicing, and order completion
# ---------------------------------------------------------------------------

CARRIER_UPS = "UPS"
CARRIER_FEDEX = "FedEx"
CARRIER_USPS = "USPS"
CARRIER_DHL = "DHL Express"
CARRIER_UNKNOWN = "Unrecognised"

# Public tracking number shapes. Format detection is a heuristic and only
# the carrier API confirms a number exists, which is stated on every result.
_CARRIER_PATTERNS: tuple[tuple[str, str], ...] = (
    (CARRIER_UPS, r"1Z[0-9A-Z]{16}"),
    (CARRIER_USPS, r"[A-Z]{2}\d{9}US"),
    (CARRIER_USPS, r"9\d{21}"),
    (CARRIER_USPS, r"9\d{19}"),
    (CARRIER_FEDEX, r"\d{12}"),
    (CARRIER_FEDEX, r"\d{15}"),
    (CARRIER_DHL, r"\d{10}"),
)

STAGE_SHIPPED = "Shipment confirmed"
STAGE_INVOICED = "ApparelMagic invoice raised"
STAGE_COMPLETED = "JOOR wholesale order completed"

STATE_BLOCKED = "Blocked at shipment, nothing advanced"
STATE_IN_FLIGHT = "Shipped and invoiced, awaiting completion"
STATE_COMPLETE = "Complete"


@dataclass(frozen=True)
class Stage:
    name: str
    reached: bool
    detail: str


@dataclass(frozen=True)
class FulfillmentResult:
    order_id: str
    tracking_number: str
    carrier: str
    tracking_valid: bool
    stages: tuple[Stage, ...]
    invoice_reference: str
    joor_status: str
    state: str
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def completed(self) -> bool:
        return self.state == STATE_COMPLETE

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def detect_carrier(tracking_number: str) -> str:
    """Name the carrier from the number shape, or say it is unrecognised."""
    candidate = re.sub(r"\s+", "", str(tracking_number or "")).upper()
    if not candidate:
        return CARRIER_UNKNOWN
    for carrier, pattern in _CARRIER_PATTERNS:
        if re.fullmatch(pattern, candidate):
            return carrier
    return CARRIER_UNKNOWN


def process_fulfillment_tracking(order_id: str,
                                 tracking_number: str) -> FulfillmentResult:
    """Advance the order only in the order the money actually moves.

    A wholesale order is completed after it is invoiced, and it is
    invoiced after it ships. Completing on a tracking number that was
    never validated closes the order in the buyer's portal while the box
    is still on the bench, which removes the one signal the buyer's
    merchandiser uses to chase it.

    No stage is reached out of order here, and a stage that is not reached
    emits no reference rather than a provisional one, because a draft
    invoice number in a completed order field is indistinguishable from a
    real one.
    """
    order = str(order_id or "").strip()
    tracking = re.sub(r"\s+", "", str(tracking_number or "")).upper()
    findings: list[Finding] = []

    if not order:
        raise ValueError("an order identifier is required")

    carrier = detect_carrier(tracking)
    valid = carrier != CARRIER_UNKNOWN

    shipped = valid
    invoiced = shipped
    completed = invoiced

    invoice_reference = f"AM-INV-{order}" if invoiced else ""

    stages = (
        Stage(STAGE_SHIPPED, shipped,
              (f"{carrier} number {tracking} matched a known shape."
               if shipped else
               "No tracking number matched a carrier shape, so the "
               "shipment is not confirmed and nothing downstream runs.")),
        Stage(STAGE_INVOICED, invoiced,
              (f"Invoice {invoice_reference} raised against the shipment."
               if invoiced else
               "No invoice was raised. An invoice against an unconfirmed "
               "shipment is a receivable with nothing behind it.")),
        Stage(STAGE_COMPLETED, completed,
              ("The order is closed in the buyer's portal, after the "
               "invoice, after the shipment." if completed else
               "The order stays open. An order closed early stops the "
               "buyer's merchandiser chasing it.")),
    )

    if not valid:
        state = STATE_BLOCKED
        joor_status = "Open, awaiting shipment"
        findings.append(Finding(
            code="FUL-TRACK", severity=SEVERITY_CRITICAL,
            title=(f"{tracking!r} does not match any carrier shape"
                   if tracking else "No tracking number was supplied"),
            detail=("Nothing advanced. A completion driven by an "
                    "unvalidated tracking number closes the order in the "
                    "buyer's portal while the box is still on the packing "
                    "bench, and it is the merchandiser chasing that order "
                    "who would otherwise have caught it."),
            fix=("Block completion on a tracking number the carrier has "
                 "not acknowledged, not merely on a field being non "
                 "empty.")))
    else:
        state = STATE_COMPLETE
        joor_status = "Completed"
        findings.append(Finding(
            code="FUL-ORDER", severity=SEVERITY_OK,
            title=f"{carrier} shipment, then invoice, then completion",
            detail=("The three stages ran in the order the money moves. "
                    "Invoicing before the shipment confirms creates a "
                    "receivable with nothing behind it, and completing "
                    "before the invoice loses the link between the order "
                    "and the money."),
            fix="Keep the sequence enforced in code, not in a runbook."))

    findings.append(Finding(
        code="FUL-HEURISTIC", severity=SEVERITY_WARN,
        title="A matching shape is not a real shipment",
        detail=("Carrier detection here is a format match. A number of the "
                "right shape that the carrier has never seen passes this "
                "check and fails at the first tracking scan, so the format "
                "test is a filter and not a confirmation."),
        fix=("Call the carrier tracking API before completion, and treat "
             "this check as the cheap first gate.")))

    if not invoiced:
        findings.append(Finding(
            code="FUL-NOREF", severity=SEVERITY_OK,
            title="No invoice reference was emitted",
            detail=("A stage that was not reached emits nothing rather than "
                    "a provisional reference, because a draft number "
                    "sitting in a completed order field is "
                    "indistinguishable from a real one."),
            fix="Keep the field empty until the invoice exists."))

    reached = sum(1 for stage in stages if stage.reached)
    headline = f"{order}: {reached} of {len(stages)} stage(s) reached"

    return FulfillmentResult(
        order_id=order, tracking_number=tracking, carrier=carrier,
        tracking_valid=valid, stages=stages,
        invoice_reference=invoice_reference, joor_status=joor_status,
        state=state, headline=headline, findings=tuple(findings))


SAMPLE_TRACKING: tuple[tuple[str, str], ...] = (
    ("UPS ground", "1Z999AA10123456784"),
    ("FedEx twelve digit", "123456789012"),
    ("USPS S10 international", "LN123456789US"),
    ("DHL Express", "1234567890"),
    ("Typed into the wrong field", "SHIPPED TODAY"),
    ("Left empty by the warehouse", ""),
)
