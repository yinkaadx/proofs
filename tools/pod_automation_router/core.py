"""Print on Demand Automation Router engine.

An order arrives from WooCommerce, is translated into a Printful fulfilment
request, and the tracking number that comes back is pushed to WooCommerce and to
whichever marketplace the order originated on. When the blank garment is out of
stock the Printful error is caught and raised as an alert, because an order that
fails quietly is worse than one that fails loudly.

Pure logic, no Streamlit import, so this is unit testable on its own and can sit
behind the real webhook. Deterministic: the clock and the random source are
passed in, so a seeded run reproduces exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

ENGINE_VERSION = "1.0.0"

# Sales channels. WooCommerce is the shop of record; the others are listings
# that also need the tracking number or the buyer chases it.
WOOCOMMERCE = "WooCommerce"
ETSY = "Etsy"
EBAY = "eBay"
AMAZON = "Amazon"
CHANNELS: tuple[str, ...] = (WOOCOMMERCE, ETSY, EBAY, AMAZON)

# Tracking always goes back to WooCommerce, plus the marketplace the order came
# from. Pushing to a marketplace that never saw the order is not harmless: it
# raises an error on their API and pollutes the log with noise that hides real
# failures.
def sync_targets(origin: str) -> tuple[str, ...]:
    if origin == WOOCOMMERCE:
        return (WOOCOMMERCE,)
    return (WOOCOMMERCE, origin)


# Fulfilment outcomes
ROUTED = "routed"
REJECTED = "rejected"

# Error codes. A rejected order always carries exactly one, so a caller can
# branch on the code rather than parse the message.
ERR_NONE = ""
ERR_OUT_OF_STOCK = "BLANK_OUT_OF_STOCK"
ERR_UNMAPPED_VARIANT = "UNMAPPED_VARIANT"
ERR_EMPTY_ORDER = "EMPTY_ORDER"
ERR_INCOMPLETE_ADDRESS = "INCOMPLETE_ADDRESS"

ERROR_HEADLINE = {
    ERR_OUT_OF_STOCK: "Blank garment out of stock",
    ERR_UNMAPPED_VARIANT: "No Printful variant for that SKU and size",
    ERR_EMPTY_ORDER: "Order has no line items",
    ERR_INCOMPLETE_ADDRESS: "Shipping address is incomplete",
}

CARRIER = "Royal Mail Tracked 48"


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Variant:
    """One sellable size of one product, and the Printful blank behind it."""
    sku: str
    size: str
    printful_variant_id: int
    blank_stock: int


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    variants: tuple[Variant, ...]

    def variant(self, size: str) -> Variant | None:
        for candidate in self.variants:
            if candidate.size.upper() == str(size).strip().upper():
                return candidate
        return None

    @property
    def sizes(self) -> tuple[str, ...]:
        return tuple(v.size for v in self.variants)


CATALOGUE: tuple[Product, ...] = (
    Product("TEE-CLASSIC-BLK", "Classic tee, black", (
        Variant("TEE-CLASSIC-BLK", "S", 4011, 120),
        Variant("TEE-CLASSIC-BLK", "M", 4012, 64),
        Variant("TEE-CLASSIC-BLK", "L", 4013, 0),      # deliberately out of stock
        Variant("TEE-CLASSIC-BLK", "XL", 4014, 31),
    )),
    Product("HOOD-HEAVY-NVY", "Heavyweight hoodie, navy", (
        Variant("HOOD-HEAVY-NVY", "M", 5021, 18),
        Variant("HOOD-HEAVY-NVY", "L", 5022, 7),
        Variant("HOOD-HEAVY-NVY", "XL", 5023, 0),      # deliberately out of stock
    )),
    Product("MUG-ENAMEL-WHT", "Enamel mug, white", (
        Variant("MUG-ENAMEL-WHT", "One size", 6031, 240),
    )),
)

RETAIL_PRICE: dict[str, float] = {
    "TEE-CLASSIC-BLK": 22.00,
    "HOOD-HEAVY-NVY": 48.00,
    "MUG-ENAMEL-WHT": 14.50,
}


def product_by_sku(sku: str, catalogue=CATALOGUE) -> Product | None:
    for product in catalogue:
        if product.sku == sku:
            return product
    return None


def find_variant(sku: str, size: str, catalogue=CATALOGUE) -> Variant | None:
    product = product_by_sku(sku, catalogue)
    return product.variant(size) if product else None


# ---------------------------------------------------------------------------
# The incoming WooCommerce order
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LineItem:
    sku: str
    size: str
    quantity: int

    @property
    def unit_price(self) -> float:
        return RETAIL_PRICE.get(self.sku, 0.0)

    @property
    def total(self) -> float:
        return round(self.unit_price * self.quantity, 2)


@dataclass(frozen=True)
class Address:
    name: str
    address1: str
    city: str
    postcode: str
    country_code: str

    @property
    def complete(self) -> bool:
        return all(str(part).strip() for part in
                   (self.name, self.address1, self.city, self.postcode,
                    self.country_code))

    @property
    def missing(self) -> tuple[str, ...]:
        fields = {"name": self.name, "address line 1": self.address1,
                  "city": self.city, "postcode": self.postcode,
                  "country": self.country_code}
        return tuple(label for label, value in fields.items()
                     if not str(value).strip())


@dataclass(frozen=True)
class WooOrder:
    order_id: str
    origin: str
    email: str
    items: tuple[LineItem, ...]
    shipping: Address
    placed_at: str

    @property
    def order_total(self) -> float:
        return round(sum(item.total for item in self.items), 2)


SAMPLE_ADDRESSES: tuple[Address, ...] = (
    Address("Amara Okafor", "14 Bramble Way", "Leeds", "LS6 2QT", "GB"),
    Address("Tom Whitfield", "3 Harbour Court", "Bristol", "BS1 5TY", "GB"),
    Address("Ines Duarte", "77 Cedar Grove", "Manchester", "M14 5RN", "GB"),
)


def generate_order(rng, now: str, order_number: int = 1,
                   catalogue=CATALOGUE) -> WooOrder:
    """Mint a plausible incoming order.

    The random source is injected so a seeded run reproduces exactly, which is
    what lets a test assert on a generated payload at all.
    """
    product = rng.choice(list(catalogue))
    variant = rng.choice(list(product.variants))
    quantity = rng.randint(1, 3)
    origin = rng.choice(list(CHANNELS))
    address = rng.choice(list(SAMPLE_ADDRESSES))
    return WooOrder(
        order_id=f"WC-{order_number:05d}",
        origin=origin,
        email=f"{address.name.split()[0].lower()}@example.com",
        items=(LineItem(product.sku, variant.size, quantity),),
        shipping=address,
        placed_at=now,
    )


# ---------------------------------------------------------------------------
# Translation into a Printful request
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PrintfulItem:
    variant_id: int
    quantity: int
    retail_price: str     # Printful takes prices as strings
    name: str
    sku: str
    size: str


@dataclass(frozen=True)
class PrintfulRequest:
    external_id: str
    recipient: dict
    items: tuple[PrintfulItem, ...]

    def as_payload(self) -> dict:
        """The body that would be posted to Printful."""
        return {
            "external_id": self.external_id,
            "recipient": dict(self.recipient),
            "items": [
                {
                    "variant_id": item.variant_id,
                    "quantity": item.quantity,
                    "retail_price": item.retail_price,
                    "name": item.name,
                }
                for item in self.items
            ],
        }


class TranslationError(Exception):
    """Raised when a WooCommerce order cannot become a Printful request."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def translate(order: WooOrder, catalogue=CATALOGUE) -> PrintfulRequest:
    """Turn a WooCommerce order into a Printful request, or refuse it.

    Refusing is the important half. A silent partial translation would post an
    order missing a line, and the buyer would receive an incomplete parcel with
    nothing in the log to explain it.
    """
    if not order.items:
        raise TranslationError(
            ERR_EMPTY_ORDER,
            f"{order.order_id} arrived with no line items, so there is nothing "
            f"to fulfil. It is held rather than posted as an empty order.")

    if not order.shipping.complete:
        missing = ", ".join(order.shipping.missing)
        raise TranslationError(
            ERR_INCOMPLETE_ADDRESS,
            f"{order.order_id} is missing {missing}. Printful would accept the "
            f"order and the parcel would go nowhere, so it is held here instead.")

    items = []
    for line in order.items:
        variant = find_variant(line.sku, line.size, catalogue)
        if variant is None:
            product = product_by_sku(line.sku, catalogue)
            known = ", ".join(product.sizes) if product else "no sizes at all"
            raise TranslationError(
                ERR_UNMAPPED_VARIANT,
                f"{line.sku} in size {line.size} has no Printful variant. That "
                f"SKU offers {known}. The order is held rather than guessed, "
                f"because guessing a size ships the wrong garment.")
        product = product_by_sku(line.sku, catalogue)
        items.append(PrintfulItem(
            variant_id=variant.printful_variant_id,
            quantity=line.quantity,
            retail_price=f"{line.unit_price:.2f}",
            name=product.name if product else line.sku,
            sku=line.sku,
            size=line.size,
        ))

    return PrintfulRequest(
        external_id=order.order_id,
        recipient={
            "name": order.shipping.name,
            "address1": order.shipping.address1,
            "city": order.shipping.city,
            "zip": order.shipping.postcode,
            "country_code": order.shipping.country_code,
            "email": order.email,
        },
        items=tuple(items),
    )


# ---------------------------------------------------------------------------
# Fulfilment, including the stock exception guard
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fulfilment:
    status: str
    order_id: str
    printful_order_id: str
    error_code: str
    message: str
    request: PrintfulRequest | None = None

    @property
    def routed(self) -> bool:
        return self.status == ROUTED


@dataclass(frozen=True)
class RouterEvent:
    sequence: int
    timestamp: str
    stage: str
    status: str
    order_id: str
    detail: str


@dataclass
class RouterLog:
    events: list[RouterEvent] = field(default_factory=list)

    def record(self, stage: str, status: str, order_id: str, detail: str,
               now: str) -> RouterEvent:
        event = RouterEvent(len(self.events) + 1, now, stage, status, order_id,
                            detail)
        self.events.append(event)
        return event

    @property
    def alerts(self) -> list[RouterEvent]:
        return [e for e in self.events if e.status == "alert"]

    def rows(self) -> list[dict]:
        return [
            {
                "#": e.sequence,
                "Timestamp": e.timestamp,
                "Stage": e.stage,
                "Status": e.status,
                "Order": e.order_id,
                "Detail": e.detail,
            }
            for e in self.events
        ]


def route_order(order: WooOrder, log: RouterLog, now: str,
                catalogue=CATALOGUE) -> Fulfilment:
    """Translate and submit, catching every refusal as a logged alert.

    The stock guard is the point of the exercise. Printful answers an out of
    stock blank with an error, and the naive integration swallows it: the shop
    shows the order as processing, nothing is printed, and the first person to
    notice is the buyer. Here it becomes an alert with the SKU, the size and
    what to do about it.
    """
    log.record("receive", "ok", order.order_id,
               f"Order received from {order.origin}, {len(order.items)} line(s), "
               f"{order.order_total:.2f} total.", now)

    try:
        request = translate(order, catalogue)
    except TranslationError as exc:
        log.record("translate", "alert", order.order_id,
                   f"{exc.code}: {exc.message}", now)
        return Fulfilment(REJECTED, order.order_id, "", exc.code, exc.message)

    log.record("translate", "ok", order.order_id,
               f"Translated to a Printful request with "
               f"{len(request.items)} item(s): "
               f"{', '.join(str(i.variant_id) for i in request.items)}.", now)

    # Printful's answer. Out of stock is the failure this guard exists for.
    for item in request.items:
        variant = find_variant(item.sku, item.size, catalogue)
        available = variant.blank_stock if variant else 0
        if available < item.quantity:
            message = (
                f"Printful rejected {order.order_id}: the blank for {item.sku} "
                f"in size {item.size} has {available} in stock and the order "
                f"needs {item.quantity}. Nothing was submitted. Hold the order, "
                f"tell the buyer, or substitute a size that is in stock.")
            log.record("fulfil", "alert", order.order_id,
                       f"{ERR_OUT_OF_STOCK}: {message}", now)
            return Fulfilment(REJECTED, order.order_id, "", ERR_OUT_OF_STOCK,
                              message, request)

    printful_order_id = f"PF-{abs(hash(order.order_id)) % 900000 + 100000}"
    log.record("fulfil", "ok", order.order_id,
               f"Printful accepted the order as {printful_order_id}.", now)
    return Fulfilment(ROUTED, order.order_id, printful_order_id, ERR_NONE,
                      f"Submitted to Printful as {printful_order_id}.", request)


# ---------------------------------------------------------------------------
# Tracking synchronisation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrackingPush:
    channel: str
    status: str
    detail: str


@dataclass(frozen=True)
class TrackingUpdate:
    order_id: str
    printful_order_id: str
    tracking_number: str
    carrier: str
    tracking_url: str
    shipped_at: str
    pushes: tuple[TrackingPush, ...]

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(p.channel for p in self.pushes)


def generate_tracking_number(rng) -> str:
    """A Royal Mail style reference: two letters, nine digits, two letters."""
    digits = "".join(str(rng.randrange(10)) for _ in range(9))
    return f"RM{digits}GB"


def sync_tracking(fulfilment: Fulfilment, order: WooOrder, log: RouterLog,
                  rng, now: str, ship_after_minutes: int = 90) -> TrackingUpdate:
    """Take the shipment webhook and push the tracking number outward.

    WooCommerce is the shop of record and always receives it. The originating
    marketplace receives it too, because that is where the buyer will look. No
    other channel is touched: pushing to a marketplace that never saw the order
    fails on their API and buries real errors in noise.
    """
    if not fulfilment.routed:
        raise ValueError(
            "Tracking cannot be synchronised for an order that was never "
            "submitted to Printful.")

    number = generate_tracking_number(rng)
    shipped = _parse(now) + timedelta(minutes=ship_after_minutes)
    pushes = tuple(
        TrackingPush(channel, "pushed",
                     f"{channel} order {order.order_id} marked shipped with "
                     f"{number}.")
        for channel in sync_targets(order.origin)
    )
    update = TrackingUpdate(
        order_id=order.order_id,
        printful_order_id=fulfilment.printful_order_id,
        tracking_number=number,
        carrier=CARRIER,
        tracking_url=f"https://www.royalmail.com/track-your-item#/tracking-results/{number}",
        shipped_at=_format(shipped),
        pushes=pushes,
    )
    log.record("track", "ok", order.order_id,
               f"Tracking {number} pushed to {', '.join(update.channels)}.", now)
    return update


def tracking_rows(update: TrackingUpdate) -> list[dict]:
    return [
        {
            "Channel": push.channel,
            "Order": update.order_id,
            "Tracking number": update.tracking_number,
            "Carrier": update.carrier,
            "Status": push.status,
            "Detail": push.detail,
        }
        for push in update.pushes
    ]


def _parse(moment: str) -> datetime:
    text = str(moment).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def catalogue_rows(catalogue=CATALOGUE) -> list[dict]:
    rows = []
    for product in catalogue:
        for variant in product.variants:
            rows.append({
                "SKU": product.sku,
                "Product": product.name,
                "Size": variant.size,
                "Printful variant": variant.printful_variant_id,
                "Blank stock": variant.blank_stock,
                "Status": "Out of stock" if variant.blank_stock == 0 else "In stock",
            })
    return rows


@dataclass
class RouterKpis:
    received: int
    routed: int
    rejected: int
    alerts: int
    tracked: int


def router_kpis(log: RouterLog) -> RouterKpis:
    return RouterKpis(
        received=sum(1 for e in log.events if e.stage == "receive"),
        routed=sum(1 for e in log.events if e.stage == "fulfil" and e.status == "ok"),
        rejected=sum(1 for e in log.events if e.status == "alert"),
        alerts=len(log.alerts),
        tracked=sum(1 for e in log.events if e.stage == "track"),
    )
