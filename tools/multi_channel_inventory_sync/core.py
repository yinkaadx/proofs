"""Multi channel inventory sync engine.

One central stock figure per SKU, with each marketplace holding its own listing
identifier for the same physical product. A sale on any channel deducts once
from the centre and broadcasts the new figure to every other channel that
carries the SKU, so the platforms cannot drift apart and oversell.

Pure logic: no Streamlit import, so the engine is unit testable on its own and
can sit behind a real webhook handler. Deterministic throughout: where a live
system would read the clock, the caller passes `now`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

AMAZON = "Amazon"
EBAY = "eBay"
SHOPIFY = "Shopify"
CHANNELS: tuple[str, ...] = (AMAZON, EBAY, SHOPIFY)

# Order outcomes
ACCEPTED = "accepted"
BLOCKED = "blocked"

# Error codes. A blocked order always carries exactly one of these, so a caller
# can branch on the code rather than parsing the message.
ERR_NONE = ""
ERR_UNKNOWN_LISTING = "UNKNOWN_LISTING"
ERR_INVALID_QUANTITY = "INVALID_QUANTITY"
ERR_INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"

# Broadcast outcomes
PUSHED = "pushed"
FAILED = "failed"
QUEUED = "queued"

BROADCAST_LABEL = {
    PUSHED: "Pushed",
    FAILED: "Failed",
    QUEUED: "Queued for retry",
}


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Listing:
    """How one marketplace refers to a central SKU."""
    channel: str
    listing_id: str
    channel_sku: str


@dataclass(frozen=True)
class Product:
    sku: str
    title: str
    listings: tuple[Listing, ...]

    def listing_for(self, channel: str) -> Listing | None:
        for listing in self.listings:
            if listing.channel == channel:
                return listing
        return None

    @property
    def channels(self) -> tuple[str, ...]:
        return tuple(listing.channel for listing in self.listings)


SAMPLE_PRODUCTS: tuple[Product, ...] = (
    Product(
        "CEN-KTL-001", "Stainless steel kettle, 1.7 litre",
        (
            Listing(AMAZON, "B07QK9ZZ21", "AMZ-KETTLE-17"),
            Listing(EBAY, "185532004411", "EB_KTL_1700"),
            Listing(SHOPIFY, "gid://shopify/Variant/44012", "shopify-kettle-1-7l"),
        ),
    ),
    Product(
        "CEN-MUG-004", "Ceramic mug, set of four",
        (
            Listing(AMAZON, "B08LMN4471", "AMZ-MUG-SET4"),
            Listing(EBAY, "185532009822", "EB_MUG_SET_4"),
            Listing(SHOPIFY, "gid://shopify/Variant/44029", "shopify-mug-set-4"),
        ),
    ),
    Product(
        "CEN-BLN-010", "Hand blender, 800 watt",
        (
            Listing(AMAZON, "B09TTY5580", "AMZ-BLEND-800"),
            Listing(SHOPIFY, "gid://shopify/Variant/44055", "shopify-blender-800w"),
        ),
    ),
    Product(
        "CEN-TST-007", "Two slice toaster, brushed",
        (
            Listing(AMAZON, "B06XWQ1133", "AMZ-TOAST-2S"),
            Listing(EBAY, "185532011903", "EB_TOAST_2SL"),
            Listing(SHOPIFY, "gid://shopify/Variant/44071", "shopify-toaster-2slice"),
        ),
    ),
)

OPENING_STOCK: dict[str, int] = {
    "CEN-KTL-001": 24,
    "CEN-MUG-004": 8,
    "CEN-BLN-010": 3,
    "CEN-TST-007": 1,
}


def product_by_sku(sku: str, products=SAMPLE_PRODUCTS) -> Product | None:
    for product in products:
        if product.sku == sku:
            return product
    return None


def resolve_listing(channel: str, channel_sku: str,
                    products=SAMPLE_PRODUCTS) -> Product | None:
    """Cross platform SKU mapping: a marketplace identifier to the central SKU.

    Marketplaces each invent their own code for the same physical item, and the
    seller rarely types them consistently, so matching ignores case and
    surrounding whitespace. A listing ID is accepted as well as a channel SKU,
    because webhooks quote whichever they hold.
    """
    needle = str(channel_sku or "").strip().lower()
    if not needle:
        return None
    for product in products:
        for listing in product.listings:
            if listing.channel != channel:
                continue
            if needle in (listing.channel_sku.lower(), listing.listing_id.lower()):
                return product
    return None


# ---------------------------------------------------------------------------
# Orders, broadcasts and the audit trail
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Order:
    order_id: str
    channel: str
    channel_sku: str
    quantity: int
    buyer: str = "marketplace buyer"


@dataclass(frozen=True)
class Broadcast:
    channel: str
    listing_id: str
    channel_sku: str
    quantity: int
    status: str
    detail: str


@dataclass(frozen=True)
class SyncEvent:
    sequence: int
    timestamp: str
    kind: str
    channel: str
    sku: str
    status: str
    detail: str
    correlation_id: str


@dataclass
class OrderResult:
    status: str
    order: Order
    sku: str
    title: str
    stock_before: int
    stock_after: int
    error_code: str
    message: str
    broadcasts: list[Broadcast] = field(default_factory=list)
    elapsed_ms: float = 0.0

    @property
    def accepted(self) -> bool:
        return self.status == ACCEPTED

    @property
    def pushed(self) -> int:
        return sum(1 for b in self.broadcasts if b.status == PUSHED)

    @property
    def failed(self) -> int:
        return sum(1 for b in self.broadcasts if b.status != PUSHED)


def _stamp(now: str | None, fallback_sequence: int) -> str:
    """Timestamps are supplied by the caller so results stay reproducible."""
    if now:
        return now
    return f"seq-{fallback_sequence:04d}"


@dataclass
class InventorySync:
    """The central ledger, its broadcast engine and its audit trail.

    Stock lives here rather than on the product, because the catalogue is a
    fixed description of what is sold and the stock is the thing that moves.
    """
    stock: dict[str, int] = field(default_factory=lambda: dict(OPENING_STOCK))
    products: tuple[Product, ...] = SAMPLE_PRODUCTS
    events: list[SyncEvent] = field(default_factory=list)
    orders: list[OrderResult] = field(default_factory=list)
    # Channels the integration currently cannot reach. A broadcast to one of
    # these fails and is queued, which is what produces the error log.
    degraded: set[str] = field(default_factory=set)

    # -- audit -------------------------------------------------------------
    def _record(self, kind: str, channel: str, sku: str, status: str,
                detail: str, correlation_id: str, now: str | None) -> SyncEvent:
        sequence = len(self.events) + 1
        event = SyncEvent(sequence, _stamp(now, sequence), kind, channel, sku,
                          status, detail, correlation_id)
        self.events.append(event)
        return event

    # -- reads -------------------------------------------------------------
    def available(self, sku: str) -> int:
        return int(self.stock.get(sku, 0))

    def ledger_rows(self) -> list[dict]:
        """Master table rows. Every column holds one type, because the table
        widget serialises through Arrow and Arrow refuses a mixed column."""
        rows = []
        for product in self.products:
            row = {
                "Central SKU": product.sku,
                "Product": product.title,
                "In stock": self.available(product.sku),
            }
            for channel in CHANNELS:
                listing = product.listing_for(channel)
                row[channel] = listing.channel_sku if listing else "not listed"
            row["Status"] = stock_status(self.available(product.sku))
            rows.append(row)
        return rows

    def event_rows(self) -> list[dict]:
        return [
            {
                "#": e.sequence,
                "Timestamp": e.timestamp,
                "Event": e.kind,
                "Channel": e.channel,
                "SKU": e.sku,
                "Status": e.status,
                "Detail": e.detail,
                "Correlation": e.correlation_id,
            }
            for e in self.events
        ]

    @property
    def failures(self) -> list[SyncEvent]:
        return [e for e in self.events if e.status in (FAILED, BLOCKED)]

    # -- writes ------------------------------------------------------------
    def place_order(self, order: Order, now: str | None = None) -> OrderResult:
        """Guard, deduct once, then broadcast to every other channel.

        The guard runs before anything is written, so a blocked order leaves
        the ledger byte for byte as it was. That ordering is the whole point:
        a partially applied order is worse than a rejected one, because the
        centre and the channels would disagree about reality.
        """
        started = time.perf_counter()
        correlation = f"{order.order_id}"

        product = resolve_listing(order.channel, order.channel_sku, self.products)
        if product is None:
            message = (
                f"{order.channel} quoted listing {order.channel_sku!r}, which maps "
                f"to no central SKU. The order is held rather than guessed, because "
                f"deducting from the wrong SKU would oversell a different product.")
            self._record("order.blocked", order.channel, "unmapped", BLOCKED,
                         message, correlation, now)
            return OrderResult(BLOCKED, order, "", "", 0, 0,
                               ERR_UNKNOWN_LISTING, message,
                               elapsed_ms=(time.perf_counter() - started) * 1000)

        before = self.available(product.sku)

        if order.quantity <= 0:
            message = (
                f"Quantity {order.quantity} is not a sale. Only a positive quantity "
                f"can deduct stock, so the order is refused rather than silently "
                f"adding inventory.")
            self._record("order.blocked", order.channel, product.sku, BLOCKED,
                         message, correlation, now)
            return OrderResult(BLOCKED, order, product.sku, product.title,
                               before, before, ERR_INVALID_QUANTITY, message,
                               elapsed_ms=(time.perf_counter() - started) * 1000)

        # Negative stock guard.
        if order.quantity > before:
            shortfall = order.quantity - before
            message = (
                f"Blocked: {order.channel} asked for {order.quantity} of "
                f"{product.sku} but only {before} remain. Filling it would leave "
                f"{before - order.quantity}, and stock below zero is not a number "
                f"a warehouse can honour. Short by {shortfall}.")
            self._record("order.blocked", order.channel, product.sku, BLOCKED,
                         message, correlation, now)
            result = OrderResult(BLOCKED, order, product.sku, product.title,
                                 before, before, ERR_INSUFFICIENT_STOCK, message,
                                 elapsed_ms=(time.perf_counter() - started) * 1000)
            self.orders.append(result)
            return result

        # Deduct once, at the centre.
        after = before - order.quantity
        self.stock[product.sku] = after
        accept_detail = (
            f"{order.channel} sold {order.quantity} of {product.sku}. Central stock "
            f"moved from {before} to {after}.")
        self._record("order.accepted", order.channel, product.sku, ACCEPTED,
                     accept_detail, correlation, now)

        broadcasts = self._broadcast(product, after, order.channel, correlation, now)

        result = OrderResult(ACCEPTED, order, product.sku, product.title,
                             before, after, ERR_NONE, accept_detail,
                             broadcasts=broadcasts,
                             elapsed_ms=(time.perf_counter() - started) * 1000)
        self.orders.append(result)
        return result

    def _broadcast(self, product: Product, quantity: int, source_channel: str,
                   correlation: str, now: str | None) -> list[Broadcast]:
        """Push the new figure to every channel except the one that sold it.

        The selling channel already decremented its own copy when it took the
        order, so pushing back to it would be a redundant write and, on some
        marketplaces, a needless rate limit cost.
        """
        broadcasts: list[Broadcast] = []
        for listing in product.listings:
            if listing.channel == source_channel:
                continue
            if listing.channel in self.degraded:
                detail = (
                    f"{listing.channel} rejected the stock update for "
                    f"{listing.channel_sku}. The channel is marked unreachable, so "
                    f"the write is queued and will replay when it recovers. Until "
                    f"then {listing.channel} still advertises the old figure.")
                broadcasts.append(Broadcast(listing.channel, listing.listing_id,
                                            listing.channel_sku, quantity,
                                            FAILED, detail))
                self._record("broadcast.failed", listing.channel, product.sku,
                             FAILED, detail, correlation, now)
                continue
            detail = (
                f"{listing.channel} listing {listing.channel_sku} set to "
                f"{quantity}.")
            broadcasts.append(Broadcast(listing.channel, listing.listing_id,
                                        listing.channel_sku, quantity,
                                        PUSHED, detail))
            self._record("broadcast.pushed", listing.channel, product.sku,
                         PUSHED, detail, correlation, now)
        return broadcasts

    def retry_failed(self, now: str | None = None) -> list[Broadcast]:
        """Replay every queued write for channels that are reachable again."""
        replayed: list[Broadcast] = []
        outstanding = [e for e in self.events
                       if e.kind == "broadcast.failed" and e.channel not in self.degraded]
        # One replay per channel and SKU, carrying the current figure rather
        # than the one that failed, because the truth may have moved on since.
        seen: set[tuple[str, str]] = set()
        for event in outstanding:
            key = (event.channel, event.sku)
            if key in seen:
                continue
            seen.add(key)
            product = product_by_sku(event.sku, self.products)
            if product is None:
                continue
            listing = product.listing_for(event.channel)
            if listing is None:
                continue
            quantity = self.available(event.sku)
            detail = (
                f"Replayed after {event.channel} recovered. Listing "
                f"{listing.channel_sku} set to {quantity}, which is the current "
                f"central figure rather than the one that failed.")
            replayed.append(Broadcast(event.channel, listing.listing_id,
                                      listing.channel_sku, quantity, PUSHED, detail))
            self._record("broadcast.replayed", event.channel, event.sku, PUSHED,
                         detail, event.correlation_id, now)
        return replayed

    def restock(self, sku: str, quantity: int, now: str | None = None) -> int:
        """Receive stock in and broadcast the new figure everywhere."""
        if quantity <= 0:
            raise ValueError("A restock must be a positive quantity.")
        product = product_by_sku(sku, self.products)
        if product is None:
            raise KeyError(f"No product with central SKU {sku!r}.")
        before = self.available(sku)
        after = before + quantity
        self.stock[sku] = after
        self._record("stock.received", "Warehouse", sku, ACCEPTED,
                     f"Received {quantity}. Central stock moved from {before} to "
                     f"{after}.", f"restock-{sku}", now)
        self._broadcast(product, after, "Warehouse", f"restock-{sku}", now)
        return after


# ---------------------------------------------------------------------------
# Presentation helpers, kept here so they are testable without Streamlit
# ---------------------------------------------------------------------------

OUT_OF_STOCK = "Out of stock"
LOW_STOCK = "Low stock"
IN_STOCK = "In stock"

LOW_STOCK_THRESHOLD = 3


def stock_status(quantity: int) -> str:
    if quantity <= 0:
        return OUT_OF_STOCK
    if quantity <= LOW_STOCK_THRESHOLD:
        return LOW_STOCK
    return IN_STOCK


def broadcast_rows(result: OrderResult) -> list[dict]:
    return [
        {
            "Channel": b.channel,
            "Listing": b.channel_sku,
            "New quantity": b.quantity,
            "Status": BROADCAST_LABEL.get(b.status, b.status),
            "Detail": b.detail,
        }
        for b in result.broadcasts
    ]


@dataclass
class SyncKpis:
    total_units: int
    skus_tracked: int
    orders_accepted: int
    orders_blocked: int
    broadcasts_pushed: int
    broadcasts_failed: int
    oversells: int
    slowest_ms: float


def sync_kpis(ledger: InventorySync) -> SyncKpis:
    pushed = sum(1 for e in ledger.events if e.kind in ("broadcast.pushed",
                                                        "broadcast.replayed"))
    failed = sum(1 for e in ledger.events if e.kind == "broadcast.failed")
    accepted = sum(1 for o in ledger.orders if o.accepted)
    blocked = sum(1 for o in ledger.orders if not o.accepted)
    # An oversell is any SKU that ever went below zero. The guard makes this
    # unreachable, and the metric exists to prove it stayed unreachable.
    oversells = sum(1 for quantity in ledger.stock.values() if quantity < 0)
    slowest = max((o.elapsed_ms for o in ledger.orders), default=0.0)
    return SyncKpis(
        total_units=sum(ledger.stock.values()),
        skus_tracked=len(ledger.products),
        orders_accepted=accepted,
        orders_blocked=blocked,
        broadcasts_pushed=pushed,
        broadcasts_failed=failed,
        oversells=oversells,
        slowest_ms=slowest,
    )
