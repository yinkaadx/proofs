"""Shopify Preorder Architecture Console: the engine.

A preorder breaks three assumptions a normal Shopify store is built on, and
each break has a configuration answer rather than an opinion.

1. One order, one shipment. A cart holding an item in stock and an item that
   ships in six weeks has to become two shipments or one late one, and the
   store has already quoted a single shipping charge for both.
2. Stock at zero means stop selling. A preorder needs the opposite, so the
   variant has to be set to continue selling when out of stock. That same
   setting, on a product that is not a preorder, is silent overselling: the
   customer is told nothing and expects a parcel this week.
3. Payment equals revenue. Money taken before the goods move is a liability,
   not a sale, and the chargeback clock starts the day it is taken rather
   than the day the item ships.

Every figure returned here is derived from the inputs. The ledger is double
entry and the tests check that it balances rather than trusting the totals as
typed.

No Streamlit import lives here on purpose, so the same engine could sit behind
a real Shopify app without a line changing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

ENGINE_VERSION = "1.0.0"

TWO_PLACES = Decimal("0.01")


def money(value) -> Decimal:
    """Currency, rounded once, at the point it becomes currency."""
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# 1. Split cart fulfillment routing
# ---------------------------------------------------------------------------

ROUTE_DISPATCH_NOW = "DISPATCH NOW"
ROUTE_HOLD_FOR_PREORDER = "HOLD FOR PREORDER"

# What a second parcel costs the merchant when the customer was charged once.
# This is the assumption this router prices splitting with, named here so a
# merchant can replace it with their own carrier rate.
SECOND_PARCEL_COST = Decimal("6.50")


@dataclass(frozen=True)
class CartItem:
    sku: str
    title: str
    quantity: int
    available_now: int
    ship_date: str = ""

    @property
    def is_preorder(self) -> bool:
        """A line is a preorder when the shop cannot ship all of it today."""
        return self.available_now < self.quantity


@dataclass(frozen=True)
class ShipmentLine:
    sku: str
    title: str
    quantity: int
    route: str
    ship_date: str


@dataclass(frozen=True)
class Shipment:
    label: str
    route: str
    ship_date: str
    lines: tuple[ShipmentLine, ...]

    @property
    def units(self) -> int:
        return sum(line.quantity for line in self.lines)


@dataclass(frozen=True)
class RoutingPlan:
    shipments: tuple[Shipment, ...]
    units_requested: int
    units_dispatched_now: int
    units_held: int
    shipment_count: int
    extra_shipping_cost: Decimal
    latest_ship_date: str
    headline: str
    fix: str
    findings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_split(self) -> bool:
        return self.shipment_count > 1


def simulate_split_cart(cart_items: tuple[CartItem, ...]) -> RoutingPlan:
    """Route one cart into what leaves today and what waits.

    A line is split rather than moved whole. Three units ordered against one
    in stock sends one today and holds two, because sending nothing until the
    stock lands is a choice the customer did not make and sending all three
    is not possible.

    The plan counts units on both sides and the tests check they add back to
    what was asked for, because a router that loses a unit somewhere in the
    middle is the failure that reaches a customer rather than a log.
    """
    items = tuple(cart_items)
    if not items:
        raise ValueError("an empty cart has nothing to route")
    for item in items:
        if item.quantity <= 0:
            raise ValueError(f"{item.sku} has no quantity to ship")
        if item.available_now < 0:
            raise ValueError(f"{item.sku} cannot have negative stock")

    now_lines: list[ShipmentLine] = []
    held_lines: list[ShipmentLine] = []
    for item in items:
        shippable = min(item.quantity, item.available_now)
        held = item.quantity - shippable
        if shippable:
            now_lines.append(ShipmentLine(
                sku=item.sku, title=item.title, quantity=shippable,
                route=ROUTE_DISPATCH_NOW, ship_date="today"))
        if held:
            held_lines.append(ShipmentLine(
                sku=item.sku, title=item.title, quantity=held,
                route=ROUTE_HOLD_FOR_PREORDER,
                ship_date=item.ship_date or "date not published"))

    shipments: list[Shipment] = []
    if now_lines:
        shipments.append(Shipment(
            label="Shipment 1 of the order", route=ROUTE_DISPATCH_NOW,
            ship_date="today", lines=tuple(now_lines)))
    for index, line in enumerate(held_lines, start=len(shipments) + 1):
        shipments.append(Shipment(
            label=f"Shipment {index} of the order",
            route=ROUTE_HOLD_FOR_PREORDER, ship_date=line.ship_date,
            lines=(line,)))

    requested = sum(item.quantity for item in items)
    dispatched = sum(line.quantity for line in now_lines)
    held_units = sum(line.quantity for line in held_lines)
    count = len(shipments)
    extra = money(SECOND_PARCEL_COST * (count - 1)) if count > 1 else money(0)

    dates = [line.ship_date for line in held_lines]
    latest = "today" if not dates else (
        "date not published" if any(d == "date not published" for d in dates)
        else max(dates))

    findings: list[str] = []
    if any(item.is_preorder and not item.ship_date for item in items):
        findings.append(
            "At least one held line has no published ship date. The customer "
            "is being asked to wait for a date nobody has told them, which is "
            "the single largest driver of preorder support tickets and of "
            "chargebacks raised as goods not received.")
    if count > 1:
        findings.append(
            f"This order becomes {count} shipments and {count} fulfillment "
            f"notifications. The customer was quoted one shipping charge, so "
            f"the extra {extra} comes out of margin unless the theme says at "
            f"the cart that the order will arrive separately.")
    if now_lines and held_lines:
        findings.append(
            "Mixed carts are where partial capture matters. If the whole "
            "amount was taken at checkout, the merchant is holding money for "
            "goods that have not moved, and the chargeback clock on all of it "
            "started today.")
    if not now_lines:
        findings.append(
            "Nothing in this cart can ship today, so nothing is gained by "
            "splitting it. One shipment, one notification, one ship date.")

    if count > 1:
        headline = (f"{requested} units become {count} shipments: "
                    f"{dispatched} out today and {held_units} held until "
                    f"{latest}")
        fix = ("Turn on separate fulfillments and say so at the cart, not at "
               "the confirmation email. A customer who reads it before paying "
               "asks no question; a customer who reads it afterwards opens a "
               "ticket.")
    elif held_units:
        headline = (f"All {requested} units are held until {latest}, so this "
                    f"is a preorder rather than a split order")
        fix = ("Charge at fulfillment rather than at checkout, or say plainly "
               "on the product page that the card is charged today for a "
               "later delivery.")
    else:
        headline = f"All {requested} units ship today in one shipment"
        fix = "Nothing to route. This cart holds no preorder line."

    return RoutingPlan(
        shipments=tuple(shipments), units_requested=requested,
        units_dispatched_now=dispatched, units_held=held_units,
        shipment_count=count, extra_shipping_cost=extra,
        latest_ship_date=latest, headline=headline, fix=fix,
        findings=tuple(findings),
    )


SAMPLE_CART: tuple[CartItem, ...] = (
    CartItem("TEE-BLK-M", "Heavyweight tee, black, medium", 2, 2, ""),
    CartItem("HOOD-NVY-L", "Zip hoodie, navy, large", 3, 1, "2026-11-14"),
    CartItem("CAP-SS27", "Season 27 cap", 1, 0, "2026-12-02"),
)


# ---------------------------------------------------------------------------
# 2. Inventory policy
# ---------------------------------------------------------------------------

POLICY_DENY = "DENY"
POLICY_CONTINUE = "CONTINUE"

# The label the merchant actually sees on the variant, so the answer can be
# acted on without a translation step.
ADMIN_LABEL = {
    POLICY_DENY: "Continue selling when out of stock: unticked",
    POLICY_CONTINUE: "Continue selling when out of stock: ticked",
}

TYPE_IN_STOCK = "In Stock"
TYPE_PREORDER = "Preorder"
TYPE_MADE_TO_ORDER = "Made to Order"
TYPE_BACKORDER = "Backorder"
TYPE_DISCONTINUED = "Discontinued"

PRODUCT_TYPES: tuple[str, ...] = (
    TYPE_IN_STOCK, TYPE_PREORDER, TYPE_MADE_TO_ORDER, TYPE_BACKORDER,
    TYPE_DISCONTINUED)

SEVERITY_OK = "OK"
SEVERITY_WARN = "WARN"
SEVERITY_CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class InventoryPolicy:
    product_type: str
    stock_level: int
    inventory_policy: str
    continue_selling_when_out_of_stock: bool
    track_quantity: bool
    admin_label: str
    buy_button_live: bool
    oversell_risk: str
    severity: str
    headline: str
    fix: str
    findings: tuple[str, ...] = field(default_factory=tuple)


def evaluate_inventory_policy(product_type: str,
                              stock_level: int) -> InventoryPolicy:
    """The variant configuration this product type requires, and why.

    Two settings do all the work. Continue selling when out of stock decides
    whether the buy button survives zero, and tracking quantity decides
    whether the shop knows how much it has committed to. A preorder needs the
    first one on, which is the same switch that makes an ordinary product
    oversell in silence, so the setting is never the whole answer: what the
    customer is told alongside it is the rest.
    """
    kind = str(product_type or "").strip()
    if kind not in PRODUCT_TYPES:
        raise ValueError(f"unknown product type: {product_type!r}")
    stock = int(stock_level)

    findings: list[str] = []

    if kind == TYPE_PREORDER:
        policy, track = POLICY_CONTINUE, True
        severity = SEVERITY_OK if stock <= 0 else SEVERITY_WARN
        headline = ("A preorder has to keep selling past zero, so continue "
                    "selling is on and quantity stays tracked")
        fix = ("Keep tracking quantity even though selling continues. The "
               "count goes negative and that negative number is the order "
               "book: it is how many units are owed before the next batch "
               "lands.")
        if stock > 0:
            findings.append(
                f"This variant is marked preorder while {stock} units are on "
                f"hand. Those units will be sold as preorders and shipped "
                f"today, which reads as a broken promise in the other "
                f"direction. Sell the stock as in stock and switch to "
                f"preorder when it reaches zero.")
        else:
            findings.append(
                f"The count is {stock}, so {abs(stock)} unit(s) are already "
                f"owed. That figure is the number to reconcile against the "
                f"purchase order, not a data error.")
        findings.append(
            "Continue selling alone tells the customer nothing. Pair it with "
            "a preorder badge on the product card, a published ship date and "
            "the same date in the cart, or the setting is indistinguishable "
            "from overselling.")
    elif kind == TYPE_MADE_TO_ORDER:
        policy, track = POLICY_CONTINUE, False
        severity = SEVERITY_OK
        headline = ("Made to order has no finite stock, so selling continues "
                    "and quantity is not tracked at all")
        fix = ("Untick tracking rather than setting a large number. A tracked "
               "count on a made to order item is a number that will be wrong "
               "in one direction forever.")
        findings.append(
            "Publish the build time on the product page. Made to order "
            "without a stated lead time is a preorder the customer did not "
            "know they were agreeing to.")
    elif kind == TYPE_BACKORDER:
        policy, track = POLICY_CONTINUE, True
        severity = SEVERITY_WARN
        headline = ("A backorder keeps selling past zero and keeps tracking, "
                    "so the negative count is the queue")
        fix = ("Cap it. A backorder with no ceiling takes orders the supplier "
               "cannot cover, and the refunds land months after the money was "
               "spent on something else.")
        findings.append(
            f"The count is {stock}. Every unit below zero is an order already "
            f"promised against stock that does not exist yet.")
    elif kind == TYPE_DISCONTINUED:
        policy, track = POLICY_DENY, True
        severity = SEVERITY_OK if stock <= 0 else SEVERITY_WARN
        headline = ("Discontinued stops at zero and never continues, because "
                    "there is no next batch to fill an oversell from")
        fix = ("Let the remaining units sell down and then let the page go "
               "sold out. Unpublishing it early throws away the search "
               "ranking that the replacement product could inherit.")
        if stock > 0:
            findings.append(
                f"{stock} unit(s) remain. Keep the page live until they clear.")
    else:
        policy, track = POLICY_DENY, True
        severity = SEVERITY_OK if stock > 0 else SEVERITY_WARN
        headline = ("In stock stops selling at zero, which is the setting "
                    "that prevents an oversell")
        fix = ("Leave continue selling off. Ticking it on an in stock product "
               "is the single most common cause of an order the shop cannot "
               "fill, because nothing on the page changes when it happens.")
        if stock <= 0:
            findings.append(
                f"The count is {stock} and the policy denies, so the buy "
                f"button is already off and the page reads sold out. If this "
                f"product should still be sellable, it is a preorder or a "
                f"backorder and should be configured as one.")

    # The policy above is what this product type requires, so a silent
    # oversell cannot appear in it: that failure is a store whose current
    # setting disagrees with the requirement, which audit_current_setting
    # below is what compares. Keeping an unreachable branch here would have
    # read as a check that was running when it never could.
    continue_selling = policy == POLICY_CONTINUE
    buy_button = continue_selling or stock > 0
    if continue_selling and stock <= 0:
        oversell = "DISCLOSED, sold against future stock"
    elif continue_selling:
        oversell = "NONE while stock remains"
    else:
        oversell = "NONE, the policy stops at zero"

    return InventoryPolicy(
        product_type=kind, stock_level=stock, inventory_policy=policy,
        continue_selling_when_out_of_stock=continue_selling,
        track_quantity=track, admin_label=ADMIN_LABEL[policy],
        buy_button_live=buy_button, oversell_risk=oversell, severity=severity,
        headline=headline, fix=fix, findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# 3. Payment capture and the ledger
# ---------------------------------------------------------------------------

CAPTURE_UPFRONT = "Upfront Charge"
CAPTURE_VAULT = "Vault Authorization"
CAPTURE_METHODS: tuple[str, ...] = (CAPTURE_UPFRONT, CAPTURE_VAULT)

# The processing rate this simulator prices with. Rates differ by plan and by
# country, so this is the figure to replace with the merchant's own before any
# number here is quoted to them.
DEFAULT_RATE = Decimal("0.029")
DEFAULT_FIXED_FEE = Decimal("0.30")

# An authorization is not indefinite. Seven days is the ordinary window before
# a hold has to be captured or re taken, and eligible Shopify Payments
# accounts can run longer, so confirm the merchant's own gateway setting
# rather than assuming this number covers a six week preorder.
AUTHORIZATION_DAYS = 7

# The card networks measure a dispute window from the transaction, which is
# why taking money months before delivery moves the risk rather than removing
# it. Confirm the exact window with the acquirer.
CHARGEBACK_WINDOW_DAYS = 120


@dataclass(frozen=True)
class LedgerEntry:
    account: str
    debit: Decimal
    credit: Decimal


@dataclass(frozen=True)
class CaptureLedger:
    capture_method: str
    amount: Decimal
    cash_today: Decimal
    processing_fee_today: Decimal
    net_cash_today: Decimal
    deferred_revenue: Decimal
    revenue_recognised_today: Decimal
    entries: tuple[LedgerEntry, ...]
    total_debits: Decimal
    total_credits: Decimal
    authorization_expires_in_days: int
    chargeback_clock_starts: str
    working_capital_effect: str
    headline: str
    fix: str
    findings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def balances(self) -> bool:
        """Double entry, checked rather than claimed."""
        return self.total_debits == self.total_credits


def simulate_payment_capture(capture_method: str, amount,
                             rate: Decimal = DEFAULT_RATE,
                             fixed_fee: Decimal = DEFAULT_FIXED_FEE
                             ) -> CaptureLedger:
    """The ledger position the day a preorder is placed, under each method.

    Upfront charge moves cash today and creates a liability of the same size,
    because revenue belongs to the period the goods move and not the period
    the card cleared. Vault authorization moves no cash, creates no liability
    and carries a different risk: the card has to still work weeks later.

    The entries are real double entry rows and the totals are the sum of them,
    so the tests check the ledger balances rather than reading a headline.
    """
    method = str(capture_method or "").strip()
    if method not in CAPTURE_METHODS:
        raise ValueError(f"unknown capture method: {capture_method!r}")
    value = money(amount)
    if value <= 0:
        raise ValueError("a capture must be for a positive amount")

    findings: list[str] = []

    if method == CAPTURE_UPFRONT:
        cash = value
        fee = money(value * rate + fixed_fee)
        net = money(cash - fee)
        entries = (
            LedgerEntry("Cash at bank", net, money(0)),
            LedgerEntry("Payment processing fees", fee, money(0)),
            LedgerEntry("Deferred revenue, customer deposits", money(0), value),
        )
        headline = (f"{value} taken today, held as deferred revenue of "
                    f"{value}, and {net} of it reaches the bank after "
                    f"{fee} of fees")
        fix = ("Treat this money as the customer's until the parcel moves. It "
               "funds nothing, it is refundable in full, and spending it on "
               "the stock that fills the order is the cash flow trap that "
               "ends most preorder campaigns.")
        findings.append(
            f"The dispute window runs roughly {CHARGEBACK_WINDOW_DAYS} days "
            f"from today rather than from delivery, so a long preorder can "
            f"ship after the safest part of that window has already passed. "
            f"Confirm the exact window with the acquirer.")
        findings.append(
            f"The processing fee of {fee} is spent now. Refunding this order "
            f"later returns {value} to the customer and the fee is not always "
            f"returned to the merchant, so a cancelled preorder can cost more "
            f"than it earned.")
        working = f"Positive {net} today, fully refundable"
        clock = "today, the day the card was charged"
        expires = 0
    else:
        cash = money(0)
        fee = money(0)
        net = money(0)
        entries = (
            LedgerEntry("Memorandum, authorized not captured", value, money(0)),
            LedgerEntry("Contingent consideration, customer commitment",
                        money(0), value),
        )
        headline = (f"{value} authorized and not captured: no cash, no "
                    f"liability, and nothing recognised until the goods move")
        fix = ("Re authorize before the hold lapses, or vault the payment "
               "method and charge at fulfillment through a selling plan. An "
               "authorization left to expire fails quietly and the first "
               "anyone hears of it is a fulfillment that cannot be paid for.")
        findings.append(
            f"An ordinary authorization lasts about {AUTHORIZATION_DAYS} days, "
            f"which does not cover a six week preorder. Check the merchant's "
            f"own gateway setting before promising a ship date beyond it.")
        findings.append(
            "Charging at fulfillment moves the failure from the merchant to "
            "the checkout: some cards will decline weeks later, so build the "
            "expected decline rate into the forecast rather than counting "
            "every authorization as a sale.")
        working = "Neutral today, no customer money held"
        clock = "the day the card is actually charged, at fulfillment"
        expires = AUTHORIZATION_DAYS

    total_debits = money(sum(entry.debit for entry in entries))
    total_credits = money(sum(entry.credit for entry in entries))

    return CaptureLedger(
        capture_method=method, amount=value, cash_today=cash,
        processing_fee_today=fee, net_cash_today=net,
        deferred_revenue=value if method == CAPTURE_UPFRONT else money(0),
        revenue_recognised_today=money(0), entries=entries,
        total_debits=total_debits, total_credits=total_credits,
        authorization_expires_in_days=expires, chargeback_clock_starts=clock,
        working_capital_effect=working, headline=headline, fix=fix,
        findings=tuple(findings),
    )


AUDIT_CORRECT = "CORRECT"
AUDIT_SILENT_OVERSELL = "SILENT OVERSELL"
AUDIT_DEAD_BUY_BUTTON = "DEAD BUY BUTTON"


@dataclass(frozen=True)
class PolicyAudit:
    product_type: str
    stock_level: int
    required_policy: str
    current_policy: str
    verdict: str
    severity: str
    headline: str
    fix: str


def audit_current_setting(product_type: str, stock_level: int,
                          continue_selling_now: bool) -> PolicyAudit:
    """Compare what a variant is set to against what its type requires.

    This is where the two real failures live, and neither one shows on the
    storefront. A product set to continue selling that should not be sells
    stock the shop does not have, and nothing on the page tells the customer.
    A preorder set to deny loses its buy button the moment the count reaches
    zero, and the campaign goes quiet without an error anywhere.
    """
    required = evaluate_inventory_policy(product_type, stock_level)
    current = POLICY_CONTINUE if continue_selling_now else POLICY_DENY
    stock = int(stock_level)

    if current == required.inventory_policy:
        return PolicyAudit(
            product_type=required.product_type, stock_level=stock,
            required_policy=required.inventory_policy, current_policy=current,
            verdict=AUDIT_CORRECT, severity=SEVERITY_OK,
            headline=(f"{required.product_type} is set correctly: "
                      f"{ADMIN_LABEL[current]}"),
            fix=required.fix)

    if current == POLICY_CONTINUE:
        return PolicyAudit(
            product_type=required.product_type, stock_level=stock,
            required_policy=required.inventory_policy, current_policy=current,
            verdict=AUDIT_SILENT_OVERSELL, severity=SEVERITY_CRITICAL,
            headline=(f"{required.product_type} is set to continue selling "
                      f"when it should stop at zero, so with a count of "
                      f"{stock} the shop takes orders it cannot fill and the "
                      f"page says nothing at all"),
            fix=("Untick continue selling on this variant. If the product is "
                 "meant to keep selling, it is a preorder or a backorder and "
                 "needs the badge and the ship date that go with one, not "
                 "this setting on its own."))

    return PolicyAudit(
        product_type=required.product_type, stock_level=stock,
        required_policy=required.inventory_policy, current_policy=current,
        verdict=AUDIT_DEAD_BUY_BUTTON, severity=SEVERITY_CRITICAL,
        headline=(f"{required.product_type} is set to stop at zero, so at a "
                  f"count of {stock} the buy button is gone and the campaign "
                  f"is live with nothing to click"),
        fix=("Tick continue selling on this variant. This failure produces no "
             "error and no alert: the page simply reads sold out while the "
             "advertising keeps running."))


def capture_comparison(amount) -> tuple[CaptureLedger, ...]:
    """Both methods on the same amount, so the choice is read side by side."""
    return tuple(simulate_payment_capture(method, amount)
                 for method in CAPTURE_METHODS)
