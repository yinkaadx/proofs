"""Irish wine wholesale engine: duty, VAT, tier pricing and dispatch.

No Streamlit import lives here, so the same engine could sit behind an
invoicing job or a pricing API.

Money is integer cents throughout, with Decimal and ROUND_HALF_UP at the one
place rounding happens. Float euros would drift a cent on a pallet and an
invoice that does not foot is a credit note plus a phone call.

Four things here are deliberately not what a first pass would write.

1. VAT is charged on the duty inclusive amount. Excise is part of the
   consideration for the supply, so the 23 percent applies to goods plus duty,
   not to goods alone. Computing VAT on the ex duty price understates a 75cl
   still wine invoice by 73 cent a bottle before anything else has gone wrong,
   and the error scales with the pallet.

2. Irish wine duty is charged per hectolitre of product, not per hectolitre of
   pure alcohol. Beer and spirits work the other way. So a 12.0 percent wine
   and a 14.5 percent wine in the same band pay exactly the same duty, and ABV
   only selects which band applies. A duty model that multiplies by strength is
   wrong in a way that looks careful.

3. Because the bands are selected rather than scaled, they are cliffs. A still
   wine at 15.0 percent pays the 5.5 to 15 rate; at 15.1 percent it pays the
   over 15 rate, which is 1.43 euro a bottle more, 17.16 euro a case, on a 0.1
   point difference that a label rounds. band_cliffs() computes every one of
   those steps rather than describing them.

4. A trade discount is taken off a price that already contains duty, and duty
   does not discount. Twenty percent off a duty paid case price is far more
   than twenty percent off the margin, because the fixed cost inside the price
   goes out at full value. margin_erosion() reports the real number, which is
   the one a sales manager needs before agreeing a chain listing.

Rates. The Alcohol Products Tax rates below are the published Irish rates for
wine and other fermented beverages, and the VAT rate is the Irish standard
rate. They are held in one table with an effective date so that a Budget change
is a one line edit rather than a search. Two of the bands are independently
checkable against the figures quoted publicly for a standard 75cl bottle:
3.19 euro for still wine in the 5.5 to 15 band and 6.37 euro for sparkling wine
over 5.5, and the test suite reproduces both from the per hectolitre rates
rather than hardcoding them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping, Sequence

ENGINE_VERSION = "1.0.0"

CURRENCY = "EUR"
RATES_EFFECTIVE = "Irish Alcohol Products Tax rates for wine, as published"
RATES_SOURCE = (
    "Revenue Alcohol Products Tax rate table for wine and other fermented "
    "beverages. Cross checked against the publicly quoted per bottle figures "
    "of 3.19 euro still and 6.37 euro sparkling on a 75cl bottle."
)

# Irish standard rate of VAT.
VAT_RATE = Decimal("0.23")

HECTOLITRE_ML = 100_000

CENT = Decimal("0.01")


def to_cents(euros: Decimal | str | int | float) -> int:
    """One place where money becomes an integer, and it rounds half up."""
    return int((Decimal(str(euros)) * 100).quantize(Decimal("1"),
                                                    rounding=ROUND_HALF_UP))


def euros(cents: int) -> str:
    return f"€{cents / 100:,.2f}"


# ---------------------------------------------------------------------------
# Duty bands
# ---------------------------------------------------------------------------

STILL = "Still"
SPARKLING = "Sparkling"
BOTTLE_TYPES = (STILL, SPARKLING)


@dataclass(frozen=True)
class DutyBand:
    """One Alcohol Products Tax rate and the range of products it covers."""

    bottle_type: str
    label: str
    # Inclusive lower bound, exclusive of the band below.
    abv_above: Decimal
    # Inclusive upper bound. None means no upper bound.
    abv_up_to: Decimal | None
    rate_per_hl_cents: int

    def covers(self, bottle_type: str, abv: Decimal) -> bool:
        if bottle_type != self.bottle_type:
            return False
        if abv <= self.abv_above:
            return False
        return self.abv_up_to is None or abv <= self.abv_up_to


# Rates in euro per hectolitre of product. Held as cents so nothing downstream
# has to touch a float.
DUTY_BANDS: tuple[DutyBand, ...] = (
    DutyBand(STILL, "Still wine not exceeding 5.5% vol",
             Decimal("0"), Decimal("5.5"), to_cents("141.57")),
    DutyBand(STILL, "Still wine exceeding 5.5% but not exceeding 15% vol",
             Decimal("5.5"), Decimal("15"), to_cents("424.84")),
    DutyBand(STILL, "Still wine exceeding 15% vol",
             Decimal("15"), None, to_cents("616.45")),
    DutyBand(SPARKLING, "Sparkling wine not exceeding 5.5% vol",
             Decimal("0"), Decimal("5.5"), to_cents("141.57")),
    DutyBand(SPARKLING, "Sparkling wine exceeding 5.5% vol",
             Decimal("5.5"), None, to_cents("849.68")),
)

# The trade sizes a wholesaler actually moves.
BOTTLE_SIZES_ML: Mapping[str, int] = {
    "Piccolo 20cl": 200,
    "Half bottle 37.5cl": 375,
    "Standard 75cl": 750,
    "Magnum 150cl": 1500,
    "Bag in box 300cl": 3000,
}
DEFAULT_SIZE = "Standard 75cl"


def band_for(bottle_type: str, abv: Decimal | float | str,
             ) -> DutyBand:
    """The single band that applies, or a refusal naming what was wrong."""
    if bottle_type not in BOTTLE_TYPES:
        raise ValueError(
            f"bottle_type must be one of {BOTTLE_TYPES}, got {bottle_type!r}")
    strength = Decimal(str(abv))
    if strength < 0:
        raise ValueError(f"abv cannot be negative, got {strength}")
    if strength > Decimal("22"):
        raise ValueError(
            f"{strength}% is above the 22% ceiling for wine and other "
            f"fermented beverages. Above that the product is dutied as "
            f"spirits, which this engine does not price.")
    for band in DUTY_BANDS:
        if band.covers(bottle_type, strength):
            return band
    raise ValueError(f"no duty band covers {bottle_type} at {strength}%")


def duty_per_bottle_cents(bottle_type: str, abv: Decimal | float | str,
                          size_ml: int = 750) -> int:
    """Duty on one bottle.

    The rate is per hectolitre of product, so the only thing strength does is
    choose the band. Volume does the rest.
    """
    if size_ml <= 0:
        raise ValueError(f"size_ml must be positive, got {size_ml}")
    band = band_for(bottle_type, abv)
    exact = (Decimal(band.rate_per_hl_cents) * Decimal(size_ml)
             / Decimal(HECTOLITRE_ML))
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# The invoice
# ---------------------------------------------------------------------------

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_CRIT = "crit"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


@dataclass(frozen=True)
class DutyCalculation:
    bottle_type: str
    abv: Decimal
    quantity: int
    size_ml: int
    band: DutyBand
    unit_price_cents: int
    duty_suspended_cents: int
    duty_cents: int
    duty_per_bottle_cents: int
    vat_cents: int
    total_cents: int
    findings: Sequence[Finding] = field(default_factory=tuple)

    @property
    def net_of_vat_cents(self) -> int:
        """Goods plus duty, which is the amount VAT is charged on."""
        return self.duty_suspended_cents + self.duty_cents

    @property
    def vat_on_goods_only_cents(self) -> int:
        """The wrong answer, kept so the gap can be shown rather than claimed."""
        return int((Decimal(self.duty_suspended_cents) * VAT_RATE)
                   .quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @property
    def understatement_cents(self) -> int:
        return self.vat_cents - self.vat_on_goods_only_cents

    def rows(self) -> list[dict[str, str]]:
        """Arrow safe: every cell a string, and the lines sum to the total."""
        return [
            {"Line": f"Goods, {self.quantity} x "
                     f"{euros(self.unit_price_cents)} duty suspended",
             "Amount": euros(self.duty_suspended_cents)},
            {"Line": f"Alcohol Products Tax, {self.quantity} x "
                     f"{euros(self.duty_per_bottle_cents)}",
             "Amount": euros(self.duty_cents)},
            {"Line": "Subtotal, the amount VAT is charged on",
             "Amount": euros(self.net_of_vat_cents)},
            {"Line": f"VAT at {VAT_RATE * 100:.0f}%",
             "Amount": euros(self.vat_cents)},
            {"Line": "Total invoice price",
             "Amount": euros(self.total_cents)},
        ]


def calculate_irish_wine_duty(bottle_type: str, abv: float | str | Decimal,
                              quantity: int, unit_price_eur: float | str,
                              size_ml: int = 750) -> DutyCalculation:
    """Duty suspended cost, excise, VAT and the total, for one line.

    unit_price_eur is the duty suspended unit price, which is what a bonded
    supplier quotes and what sits on the stock ledger while the wine is in
    bond.
    """
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    unit_cents = to_cents(unit_price_eur)
    if unit_cents < 0:
        raise ValueError("unit_price_eur cannot be negative")

    band = band_for(bottle_type, abv)
    per_bottle = duty_per_bottle_cents(bottle_type, abv, size_ml)

    suspended = unit_cents * quantity
    duty = per_bottle * quantity
    # VAT is charged on the duty inclusive amount, not on the goods alone.
    net = suspended + duty
    vat = int((Decimal(net) * VAT_RATE).quantize(Decimal("1"),
                                                 rounding=ROUND_HALF_UP))

    calculation = DutyCalculation(
        bottle_type=bottle_type, abv=Decimal(str(abv)), quantity=quantity,
        size_ml=size_ml, band=band, unit_price_cents=unit_cents,
        duty_suspended_cents=suspended, duty_cents=duty,
        duty_per_bottle_cents=per_bottle, vat_cents=vat,
        total_cents=net + vat,
    )
    return DutyCalculation(
        bottle_type=calculation.bottle_type, abv=calculation.abv,
        quantity=calculation.quantity, size_ml=calculation.size_ml,
        band=calculation.band, unit_price_cents=calculation.unit_price_cents,
        duty_suspended_cents=calculation.duty_suspended_cents,
        duty_cents=calculation.duty_cents,
        duty_per_bottle_cents=calculation.duty_per_bottle_cents,
        vat_cents=calculation.vat_cents, total_cents=calculation.total_cents,
        findings=tuple(audit_calculation(calculation)),
    )


def audit_calculation(calc: DutyCalculation) -> list[Finding]:
    findings: list[Finding] = []

    findings.append(Finding(
        code="IE-VAT-ON-DUTY",
        severity=SEVERITY_WARN,
        title="VAT is charged on the duty inclusive amount",
        detail=(
            f"VAT of {euros(calc.vat_cents)} is charged on "
            f"{euros(calc.net_of_vat_cents)}, which is goods plus excise. "
            f"Charged on the goods alone it would be "
            f"{euros(calc.vat_on_goods_only_cents)}, so the common shortcut "
            f"understates this line by {euros(calc.understatement_cents)}."
        ),
        fix=("Add duty to the net before applying the rate. Excise forms part "
             "of the consideration for the supply."),
    ))

    strength = calc.abv
    if calc.bottle_type == STILL and Decimal("14.5") <= strength <= Decimal("15.5"):
        other = Decimal("15.1") if strength <= Decimal("15") else Decimal("15")
        here = duty_per_bottle_cents(STILL, strength, calc.size_ml)
        there = duty_per_bottle_cents(STILL, other, calc.size_ml)
        findings.append(Finding(
            code="IE-ABV-CLIFF",
            severity=SEVERITY_CRIT,
            title="This wine is sitting on a duty cliff",
            detail=(
                f"At {strength}% the duty is {euros(here)} a bottle. At "
                f"{other}% it is {euros(there)}, a difference of "
                f"{euros(abs(there - here))} on a strength a label rounds. "
                f"Across {calc.quantity} bottles that is "
                f"{euros(abs(there - here) * calc.quantity)}."
            ),
            fix=("Take the strength from the producer's analysis certificate, "
                 "not from the front label, before the stock is released."),
        ))

    if calc.bottle_type == SPARKLING and strength > Decimal("5.5"):
        still_equivalent = duty_per_bottle_cents(STILL, strength, calc.size_ml)
        findings.append(Finding(
            code="IE-SPARKLING-PREMIUM",
            severity=SEVERITY_WARN,
            title="Sparkling carries double the duty of still at this strength",
            detail=(
                f"Duty here is {euros(calc.duty_per_bottle_cents)} a bottle. "
                f"The same strength as a still wine would be "
                f"{euros(still_equivalent)}. The bubbles cost "
                f"{euros(calc.duty_per_bottle_cents - still_equivalent)} a "
                f"bottle before the wine does."
            ),
            fix=("Price sparkling from its own duty line. A single blended "
                 "duty assumption across the range loses money on one half of "
                 "it and prices the other half out."),
        ))

    if calc.duty_cents > calc.duty_suspended_cents:
        findings.append(Finding(
            code="IE-DUTY-EXCEEDS-GOODS",
            severity=SEVERITY_CRIT,
            title="Duty is larger than the wine",
            detail=(
                f"Excise of {euros(calc.duty_cents)} against goods of "
                f"{euros(calc.duty_suspended_cents)}. At this buying price the "
                f"tax, not the supplier, is the main cost in the case."
            ),
            fix=("Entry level listings live or die on duty. A ten cent buying "
                 "improvement is worth less here than one place on the shelf."),
        ))

    return findings


def band_cliffs(size_ml: int = 750) -> list[dict[str, str]]:
    """Every point where 0.1 percent of strength changes the duty.

    The bands are selected rather than scaled, so the boundaries are steps.
    This computes them rather than describing them.
    """
    rows: list[dict[str, str]] = []
    for bottle_type in BOTTLE_TYPES:
        boundaries = sorted({b.abv_up_to for b in DUTY_BANDS
                             if b.bottle_type == bottle_type
                             and b.abv_up_to is not None})
        for boundary in boundaries:
            below = duty_per_bottle_cents(bottle_type, boundary, size_ml)
            above = duty_per_bottle_cents(
                bottle_type, boundary + Decimal("0.1"), size_ml)
            rows.append({
                "Product": bottle_type,
                "At or below": f"{boundary}%",
                "Duty per bottle": euros(below),
                "Just above": f"{boundary + Decimal('0.1')}%",
                "Duty per bottle above": euros(above),
                "Step": euros(above - below),
                "Per case of 12": euros((above - below) * 12),
            })
    return rows


def duty_is_flat_within_a_band(bottle_type: str, low: str, high: str,
                               size_ml: int = 750) -> bool:
    """True when two strengths in the same band pay the same duty.

    Kept as a function because it is the property most often assumed false.
    """
    return (duty_per_bottle_cents(bottle_type, low, size_ml)
            == duty_per_bottle_cents(bottle_type, high, size_ml))


def duty_band_rows() -> list[dict[str, str]]:
    return [{
        "Product": band.bottle_type,
        "Band": band.label,
        "Rate per hectolitre": euros(band.rate_per_hl_cents),
        "Per 75cl bottle": euros(
            int((Decimal(band.rate_per_hl_cents) * 750 / HECTOLITRE_ML)
                .quantize(Decimal("1"), rounding=ROUND_HALF_UP))),
        "Per case of 12": euros(
            int((Decimal(band.rate_per_hl_cents) * 750 / HECTOLITRE_ML)
                .quantize(Decimal("1"), rounding=ROUND_HALF_UP)) * 12),
    } for band in DUTY_BANDS]


# ---------------------------------------------------------------------------
# Customer tiers
# ---------------------------------------------------------------------------

TIER_INDEPENDENT = "Independent Merchant"
TIER_RESTAURANT = "Restaurant"
TIER_CHAIN = "Multi Store Chain"
CUSTOMER_TYPES = (TIER_INDEPENDENT, TIER_RESTAURANT, TIER_CHAIN)


@dataclass(frozen=True)
class CustomerTier:
    name: str
    discount_off_list: Decimal
    minimum_cases: int
    payment_days: int
    # What the tier costs to serve beyond the discount, per case.
    service_cost_cents: int
    note: str


CUSTOMER_TIERS: tuple[CustomerTier, ...] = (
    CustomerTier(
        name=TIER_INDEPENDENT,
        discount_off_list=Decimal("0.00"),
        minimum_cases=1,
        payment_days=30,
        service_cost_cents=to_cents("2.50"),
        note=("Buys list, buys small, pays on time. The mixed case picking "
              "costs more per case than a chain pallet does."),
    ),
    CustomerTier(
        name=TIER_RESTAURANT,
        discount_off_list=Decimal("0.08"),
        minimum_cases=3,
        payment_days=45,
        service_cost_cents=to_cents("4.00"),
        note=("Small drops, often by hand into a cellar, and the longest "
              "average delay between the invoice and the money."),
    ),
    CustomerTier(
        name=TIER_CHAIN,
        discount_off_list=Decimal("0.20"),
        minimum_cases=40,
        payment_days=60,
        service_cost_cents=to_cents("0.80"),
        note=("Cheapest to serve per case and the most expensive to win. The "
              "discount is taken off a price that already contains duty."),
    ),
)

TIER_BY_NAME = {t.name: t for t in CUSTOMER_TIERS}


@dataclass(frozen=True)
class Product:
    product_id: str
    name: str
    bottle_type: str
    abv: str
    size_ml: int
    list_price_cents: int
    cost_cents: int


CATALOGUE: tuple[Product, ...] = (
    Product("IWD-101", "Muscadet Sevre et Maine", STILL, "12.0", 750,
            to_cents("13.50"), to_cents("5.20")),
    Product("IWD-204", "Rioja Crianza", STILL, "14.0", 750,
            to_cents("16.90"), to_cents("6.40")),
    Product("IWD-310", "Prosecco DOC Brut", SPARKLING, "11.0", 750,
            to_cents("15.40"), to_cents("5.10")),
    Product("IWD-415", "Tawny Port", STILL, "19.5", 750,
            to_cents("24.00"), to_cents("9.80")),
    Product("IWD-520", "Entry level Chilean Merlot", STILL, "12.5", 750,
            to_cents("7.95"), to_cents("2.60")),
)

PRODUCT_BY_ID = {p.product_id: p for p in CATALOGUE}


@dataclass(frozen=True)
class TierPrice:
    product_id: str
    customer_type: str
    tier: CustomerTier
    base_price_cents: int
    discount_cents: int
    applied_price_cents: int
    duty_per_bottle_cents: int
    # What is left once duty and the cost of the wine come out.
    contribution_cents: int
    contribution_at_list_cents: int

    @property
    def headline_discount(self) -> Decimal:
        return self.tier.discount_off_list

    @property
    def margin_erosion(self) -> Decimal:
        """The share of contribution the discount actually took.

        Always larger than the headline discount whenever the price contains a
        fixed cost, which for wine it always does.
        """
        if self.contribution_at_list_cents <= 0:
            return Decimal("0")
        lost = self.contribution_at_list_cents - self.contribution_cents
        return (Decimal(lost) / Decimal(self.contribution_at_list_cents))


def get_customer_tier_price(product_id: str, customer_type: str,
                            base_price_eur: float | str | None = None
                            ) -> TierPrice:
    """The applied price for one account type on one product.

    base_price_eur overrides the catalogue list price. Leaving it out uses the
    catalogue, which is what a live system would do.
    """
    if product_id not in PRODUCT_BY_ID:
        raise ValueError(f"unknown product {product_id!r}")
    if customer_type not in TIER_BY_NAME:
        raise ValueError(
            f"customer_type must be one of {CUSTOMER_TYPES}, "
            f"got {customer_type!r}")
    product = PRODUCT_BY_ID[product_id]
    tier = TIER_BY_NAME[customer_type]

    base = (product.list_price_cents if base_price_eur is None
            else to_cents(base_price_eur))
    if base < 0:
        raise ValueError("base_price_eur cannot be negative")

    discount = int((Decimal(base) * tier.discount_off_list)
                   .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    applied = base - discount
    duty = duty_per_bottle_cents(product.bottle_type, product.abv,
                                 product.size_ml)

    contribution = applied - duty - product.cost_cents
    at_list = base - duty - product.cost_cents

    return TierPrice(
        product_id=product_id, customer_type=customer_type, tier=tier,
        base_price_cents=base, discount_cents=discount,
        applied_price_cents=applied, duty_per_bottle_cents=duty,
        contribution_cents=contribution, contribution_at_list_cents=at_list,
    )


def margin_erosion(product_id: str) -> list[dict[str, str]]:
    """Headline discount against what the discount actually cost.

    The gap is the whole argument. A twenty percent chain discount on a wine
    whose price is mostly duty and cost can take well over half the margin.
    """
    rows: list[dict[str, str]] = []
    for tier in CUSTOMER_TIERS:
        price = get_customer_tier_price(product_id, tier.name)
        rows.append({
            "Account type": tier.name,
            "Headline discount": f"{price.headline_discount * 100:.0f}%",
            "Price per bottle": euros(price.applied_price_cents),
            "Duty inside that price": euros(price.duty_per_bottle_cents),
            "Contribution per bottle": euros(price.contribution_cents),
            "Share of margin given away":
                f"{price.margin_erosion * 100:.0f}%",
        })
    return rows


def tier_rows(product_id: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for tier in CUSTOMER_TIERS:
        price = get_customer_tier_price(product_id, tier.name)
        rows.append({
            "Account type": tier.name,
            "Applied price": euros(price.applied_price_cents),
            "Minimum order": f"{tier.minimum_cases} cases",
            "Payment terms": f"{tier.payment_days} days",
            "Service cost per case": euros(tier.service_cost_cents),
            "Note": tier.note,
        })
    return rows


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

WAREHOUSE_BONDED = "Bonded"
WAREHOUSE_DUTY_PAID = "Duty paid"
WAREHOUSE_TYPES = (WAREHOUSE_BONDED, WAREHOUSE_DUTY_PAID)


@dataclass(frozen=True)
class OrderLine:
    product_id: str
    cases: int
    bottles_per_case: int = 12


@dataclass(frozen=True)
class Order:
    order_id: str
    customer: str
    customer_type: str
    delivery: str
    lines: tuple[OrderLine, ...]


SAMPLE_ORDERS: tuple[Order, ...] = (
    Order("SO-24118", "Blackrock Cellars", TIER_INDEPENDENT,
          "Main Street, Blackrock, Co Dublin",
          (OrderLine("IWD-101", 2), OrderLine("IWD-310", 1))),
    Order("SO-24119", "Harbour Grill", TIER_RESTAURANT,
          "Harbour Road, Howth, Co Dublin",
          (OrderLine("IWD-204", 3), OrderLine("IWD-415", 1))),
    Order("SO-24120", "Nationwide Off Licence Group", TIER_CHAIN,
          "Regional Depot, Ballymount, Dublin 12",
          (OrderLine("IWD-520", 40), OrderLine("IWD-101", 20))),
)

ORDER_BY_ID = {o.order_id: o for o in SAMPLE_ORDERS}


@dataclass(frozen=True)
class DispatchPayload:
    order_id: str
    warehouse_type: str
    picking_slip: str
    instructions: tuple[str, ...]
    total_bottles: int
    total_cases: int
    duty_cents: int
    duty_status: str
    findings: Sequence[Finding] = field(default_factory=tuple)

    def line_rows(self) -> list[dict[str, str]]:
        order = ORDER_BY_ID[self.order_id]
        rows: list[dict[str, str]] = []
        for line in order.lines:
            product = PRODUCT_BY_ID[line.product_id]
            bottles = line.cases * line.bottles_per_case
            per_bottle = duty_per_bottle_cents(product.bottle_type,
                                               product.abv, product.size_ml)
            rows.append({
                "SKU": product.product_id,
                "Description": product.name,
                "Cases": str(line.cases),
                "Bottles": str(bottles),
                "ABV": f"{product.abv}%",
                "Duty per bottle": euros(per_bottle),
                "Duty on line": euros(per_bottle * bottles),
            })
        return rows


def generate_3pl_dispatch_payload(order_id: str, warehouse_type: str
                                  ) -> DispatchPayload:
    """A picking slip and dispatch instructions for an outsourced warehouse.

    The warehouse type is not cosmetic. Goods leaving a bonded warehouse are
    being released for consumption, which is the moment the duty becomes
    payable and the moment a movement document is required. Goods leaving a
    duty paid warehouse are just goods.
    """
    if order_id not in ORDER_BY_ID:
        raise ValueError(f"unknown order {order_id!r}")
    if warehouse_type not in WAREHOUSE_TYPES:
        raise ValueError(
            f"warehouse_type must be one of {WAREHOUSE_TYPES}, "
            f"got {warehouse_type!r}")

    order = ORDER_BY_ID[order_id]
    bottles = sum(l.cases * l.bottles_per_case for l in order.lines)
    cases = sum(l.cases for l in order.lines)
    duty = 0
    for line in order.lines:
        product = PRODUCT_BY_ID[line.product_id]
        duty += (duty_per_bottle_cents(product.bottle_type, product.abv,
                                       product.size_ml)
                 * line.cases * line.bottles_per_case)

    bonded = warehouse_type == WAREHOUSE_BONDED
    status = ("Duty becomes payable on release"
              if bonded else "Duty already accounted for")

    header = [
        f"PICKING SLIP  {order.order_id}",
        f"Customer      {order.customer} ({order.customer_type})",
        f"Deliver to    {order.delivery}",
        f"Warehouse     {warehouse_type}",
        f"Duty status   {status}",
        "",
        f"{'SKU':<10}{'Description':<32}{'Cases':>6}{'Bottles':>9}",
        "-" * 57,
    ]
    for line in order.lines:
        product = PRODUCT_BY_ID[line.product_id]
        header.append(
            f"{product.product_id:<10}{product.name[:31]:<32}"
            f"{line.cases:>6}{line.cases * line.bottles_per_case:>9}")
    header += [
        "-" * 57,
        f"{'TOTAL':<42}{cases:>6}{bottles:>9}",
        "",
        f"Excise on this consignment: {euros(duty)}",
    ]

    instructions: list[str] = []
    if bonded:
        instructions += [
            "Release from bond. Do not load until the movement reference is "
            "recorded against this order.",
            "Excise becomes payable on release, not on delivery and not on "
            "payment of the invoice.",
            "Pick whole cases from bonded locations only. A mixed pick that "
            "touches duty paid stock breaks the audit trail on both.",
        ]
    else:
        instructions += [
            "Duty paid stock. No movement document is required for a domestic "
            "delivery.",
            "Pick from duty paid locations only.",
        ]
    instructions += [
        f"Stand pallets upright. {cases} cases, {bottles} bottles.",
        "Photograph the loaded pallet before it leaves the bay. A breakage "
        "claim without a load photograph is settled against the sender.",
    ]
    if order.customer_type == TIER_RESTAURANT:
        instructions.append(
            "Restaurant delivery. Call thirty minutes ahead and deliver to "
            "the cellar door, not the front of house.")
    if order.customer_type == TIER_CHAIN:
        instructions.append(
            "Depot delivery. Booking slot required. A turn away is charged to "
            "the sender whatever the reason.")

    payload = DispatchPayload(
        order_id=order_id, warehouse_type=warehouse_type,
        picking_slip="\n".join(header), instructions=tuple(instructions),
        total_bottles=bottles, total_cases=cases, duty_cents=duty,
        duty_status=status,
    )
    return DispatchPayload(
        order_id=payload.order_id, warehouse_type=payload.warehouse_type,
        picking_slip=payload.picking_slip, instructions=payload.instructions,
        total_bottles=payload.total_bottles, total_cases=payload.total_cases,
        duty_cents=payload.duty_cents, duty_status=payload.duty_status,
        findings=tuple(audit_dispatch(payload)),
    )


def audit_dispatch(payload: DispatchPayload) -> list[Finding]:
    findings: list[Finding] = []
    order = ORDER_BY_ID[payload.order_id]

    if payload.warehouse_type == WAREHOUSE_BONDED:
        findings.append(Finding(
            code="IE-BOND-RELEASE",
            severity=SEVERITY_CRIT,
            title="Releasing this order creates a duty liability today",
            detail=(
                f"{euros(payload.duty_cents)} of excise becomes payable when "
                f"these {payload.total_bottles} bottles leave bond. The "
                f"customer is on {TIER_BY_NAME[order.customer_type].payment_days} "
                f"day terms, so the duty is funded by the business for that "
                f"period whatever the invoice says."
            ),
            fix=("Release against firm orders rather than to smooth picking. "
                 "Stock released early is cash paid early."),
        ))
    else:
        findings.append(Finding(
            code="IE-DUTY-PAID",
            severity=SEVERITY_OK,
            title="Duty on this stock was already accounted for",
            detail=(f"{euros(payload.duty_cents)} of excise sits in the "
                    f"carrying value of these {payload.total_cases} cases and "
                    f"is not a fresh liability."),
            fix="Dispatch normally.",
        ))

    tier = TIER_BY_NAME[order.customer_type]
    if payload.total_cases < tier.minimum_cases:
        findings.append(Finding(
            code="IE-BELOW-MINIMUM",
            severity=SEVERITY_WARN,
            title=f"Below the {tier.name} minimum",
            detail=(f"{payload.total_cases} cases against a stated minimum of "
                    f"{tier.minimum_cases}. The tier price assumes the volume "
                    f"that is not here."),
            fix=("Either hold for consolidation or reprice the line. Shipping "
                 "a chain price on an independent quantity is a decision, and "
                 "it should be one somebody made."),
        ))

    return findings


def dispatch_summary_rows() -> list[dict[str, str]]:
    """Every sample order out of both warehouse types."""
    rows: list[dict[str, str]] = []
    for order in SAMPLE_ORDERS:
        for warehouse in WAREHOUSE_TYPES:
            payload = generate_3pl_dispatch_payload(order.order_id, warehouse)
            rows.append({
                "Order": order.order_id,
                "Customer": order.customer,
                "Warehouse": warehouse,
                "Cases": str(payload.total_cases),
                "Bottles": str(payload.total_bottles),
                "Excise on consignment": euros(payload.duty_cents),
                "Liability created today": (
                    euros(payload.duty_cents)
                    if warehouse == WAREHOUSE_BONDED else euros(0)),
            })
    return rows
