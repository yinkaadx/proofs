"""Ground truth tests for the multi channel inventory sync engine.

Pass and fail markers are declared before execution: every case names the stock
figure, error code or broadcast set it must produce, and the runner parses
results programmatically so nothing is judged by eye.

Run: python3 tests/test_multi_channel_inventory_sync.py
Pass marker: final line is exactly "INVENTORY RESULT: PASS <n>/<n>", exit 0.
Fail marker: any line starting "FAIL", plus exit code 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.multi_channel_inventory_sync.core import (  # noqa: E402
    ACCEPTED,
    AMAZON,
    BLOCKED,
    CHANNELS,
    EBAY,
    ERR_INSUFFICIENT_STOCK,
    ERR_INVALID_QUANTITY,
    ERR_NONE,
    ERR_UNKNOWN_LISTING,
    FAILED,
    IN_STOCK,
    LOW_STOCK,
    OPENING_STOCK,
    OUT_OF_STOCK,
    PUSHED,
    SAMPLE_PRODUCTS,
    SHOPIFY,
    InventorySync,
    Order,
    broadcast_rows,
    product_by_sku,
    resolve_listing,
    stock_status,
    sync_kpis,
)

failures: list[str] = []
checks = 0

NOW = "2026-09-12T06:00:00Z"

KETTLE = "CEN-KTL-001"      # 24 in stock, listed on all three channels
MUGS = "CEN-MUG-004"        # 8 in stock, all three channels
BLENDER = "CEN-BLN-010"     # 3 in stock, Amazon and Shopify only
TOASTER = "CEN-TST-007"     # 1 in stock, all three channels


def expect(condition: bool, label: str) -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


def fresh() -> InventorySync:
    return InventorySync()


def order(channel: str, channel_sku: str, quantity: int, oid: str = "ORD-1") -> Order:
    return Order(oid, channel, channel_sku, quantity)


print("== Cross platform SKU mapping ==")
expect(resolve_listing(AMAZON, "AMZ-KETTLE-17") is not None,
       "an Amazon channel SKU maps to a central product")
expect(resolve_listing(AMAZON, "AMZ-KETTLE-17").sku == KETTLE,
       "it maps to the correct central SKU")
expect(resolve_listing(EBAY, "EB_KTL_1700").sku == KETTLE,
       "the same product resolves from its eBay SKU")
expect(resolve_listing(SHOPIFY, "shopify-kettle-1-7l").sku == KETTLE,
       "and from its Shopify SKU")
expect(resolve_listing(AMAZON, "B07QK9ZZ21").sku == KETTLE,
       "a marketplace listing ID resolves as well as a channel SKU")
expect(resolve_listing(AMAZON, "  amz-kettle-17  ").sku == KETTLE,
       "matching ignores case and surrounding whitespace")
expect(resolve_listing(EBAY, "AMZ-KETTLE-17") is None,
       "an Amazon SKU does not resolve on eBay, so channels cannot cross wires")
expect(resolve_listing(EBAY, "EB_BLENDER_800") is None,
       "a product not listed on a channel does not resolve there")
expect(resolve_listing(AMAZON, "") is None, "an empty identifier resolves to nothing")
expect(resolve_listing(AMAZON, "NOT-A-REAL-SKU") is None,
       "an unknown identifier resolves to nothing")

print("== Every sample product maps cleanly on every channel it lists ==")
for product in SAMPLE_PRODUCTS:
    for listing in product.listings:
        resolved = resolve_listing(listing.channel, listing.channel_sku)
        expect(resolved is not None and resolved.sku == product.sku,
               f"{product.sku} resolves from {listing.channel} {listing.channel_sku}")
    expect(len(set(product.channels)) == len(product.channels),
           f"{product.sku} lists each channel at most once")

print("== Inventory deduction happens once, at the centre ==")
ledger = fresh()
expect(ledger.available(KETTLE) == OPENING_STOCK[KETTLE],
       f"opening stock is {OPENING_STOCK[KETTLE]}")
result = ledger.place_order(order(AMAZON, "AMZ-KETTLE-17", 3), now=NOW)
expect(result.accepted, f"a normal order is accepted (got {result.status})")
expect(result.error_code == ERR_NONE, "an accepted order carries no error code")
expect(result.stock_before == 24 and result.stock_after == 21,
       f"stock moved 24 to 21 (got {result.stock_before} to {result.stock_after})")
expect(ledger.available(KETTLE) == 21, "the ledger holds the new figure")
expect(sum(1 for o in ledger.orders if o.accepted) == 1, "the order is recorded once")

result = ledger.place_order(order(EBAY, "EB_KTL_1700", 1, "ORD-2"), now=NOW)
expect(ledger.available(KETTLE) == 20,
       f"a second sale from another channel deducts again (got {ledger.available(KETTLE)})")

print("== Deduction never touches another SKU ==")
untouched = {sku: qty for sku, qty in ledger.stock.items() if sku != KETTLE}
expect(all(untouched[sku] == OPENING_STOCK[sku] for sku in untouched),
       f"every other SKU is unchanged (got {untouched})")

print("== Multi channel broadcast mapping ==")
ledger = fresh()
result = ledger.place_order(order(AMAZON, "AMZ-KETTLE-17", 2), now=NOW)
targets = {b.channel for b in result.broadcasts}
expect(targets == {EBAY, SHOPIFY},
       f"an Amazon sale broadcasts to the other two channels only (got {targets})")
expect(AMAZON not in targets,
       "the selling channel is not written back to, since it already decremented")
expect(all(b.quantity == result.stock_after for b in result.broadcasts),
       "every broadcast carries the new central figure")
expect(all(b.status == PUSHED for b in result.broadcasts),
       "every broadcast succeeds while all channels are reachable")
expect(result.pushed == 2 and result.failed == 0, "two pushed, none failed")
for b in result.broadcasts:
    product = product_by_sku(KETTLE)
    listing = product.listing_for(b.channel)
    expect(listing is not None and b.channel_sku == listing.channel_sku,
           f"the {b.channel} broadcast targets that channel's own SKU")
    expect(listing is not None and b.listing_id == listing.listing_id,
           f"the {b.channel} broadcast targets that channel's listing ID")

print("== Broadcast from each channel in turn ==")
for source, sku_on_channel in ((AMAZON, "AMZ-MUG-SET4"),
                               (EBAY, "EB_MUG_SET_4"),
                               (SHOPIFY, "shopify-mug-set-4")):
    ledger = fresh()
    result = ledger.place_order(order(source, sku_on_channel, 1), now=NOW)
    targets = {b.channel for b in result.broadcasts}
    expect(targets == set(CHANNELS) - {source},
           f"a {source} sale broadcasts to {set(CHANNELS) - {source}} (got {targets})")

print("== A product listed on two channels broadcasts to one ==")
ledger = fresh()
result = ledger.place_order(order(AMAZON, "AMZ-BLEND-800", 1), now=NOW)
targets = {b.channel for b in result.broadcasts}
expect(targets == {SHOPIFY},
       f"the blender broadcasts only to Shopify, since it has no eBay listing "
       f"(got {targets})")
expect(EBAY not in targets, "no broadcast is invented for a channel without a listing")

print("== Negative stock guard blocks the order ==")
ledger = fresh()
expect(ledger.available(TOASTER) == 1, "the toaster opens with one unit")
result = ledger.place_order(order(EBAY, "EB_TOAST_2SL", 2), now=NOW)
expect(not result.accepted, f"an oversell is blocked (got {result.status})")
expect(result.status == BLOCKED, "the status is blocked")
expect(result.error_code == ERR_INSUFFICIENT_STOCK,
       f"the error code is {ERR_INSUFFICIENT_STOCK} (got {result.error_code!r})")
expect("only 1 remain" in result.message,
       f"the error names what is actually available (got {result.message!r})")
expect("Short by 1" in result.message,
       "the error names the shortfall so a buyer can be told the real number")
expect(str(TOASTER) in result.message, "the error names the central SKU")

print("== A blocked order changes nothing at all ==")
expect(ledger.available(TOASTER) == 1,
       f"stock is untouched after a block (got {ledger.available(TOASTER)})")
expect(result.stock_before == result.stock_after == 1,
       "the result reports no movement")
expect(result.broadcasts == [],
       f"nothing is broadcast for a blocked order (got {result.broadcasts})")
expect(ledger.stock == OPENING_STOCK,
       "the whole ledger is byte for byte as it started")

print("== Stock can be taken to exactly zero, but not past it ==")
ledger = fresh()
result = ledger.place_order(order(AMAZON, "AMZ-TOAST-2S", 1), now=NOW)
expect(result.accepted, "selling the last unit is allowed")
expect(ledger.available(TOASTER) == 0, "stock lands on exactly zero")
expect(stock_status(0) == OUT_OF_STOCK, "zero reads as out of stock")

follow_up = ledger.place_order(order(EBAY, "EB_TOAST_2SL", 1, "ORD-9"), now=NOW)
expect(not follow_up.accepted, "the next sale on another channel is blocked")
expect(follow_up.error_code == ERR_INSUFFICIENT_STOCK,
       "it is blocked by the stock guard specifically")
expect(ledger.available(TOASTER) == 0, "stock never goes below zero")
expect(all(q >= 0 for q in ledger.stock.values()),
       f"no SKU is ever negative (got {ledger.stock})")

print("== The guard survives a run of oversell attempts ==")
ledger = fresh()
for index in range(12):
    ledger.place_order(order(CHANNELS[index % 3],
                             ("AMZ-BLEND-800" if CHANNELS[index % 3] == AMAZON
                              else "shopify-blender-800w"),
                             2, f"ORD-B{index}"), now=NOW)
expect(ledger.available(BLENDER) >= 0,
       f"the blender never goes negative (got {ledger.available(BLENDER)})")
expect(ledger.available(BLENDER) in (0, 1),
       f"it lands on whatever the guard allowed, 0 or 1 (got {ledger.available(BLENDER)})")
kpis = sync_kpis(ledger)
expect(kpis.oversells == 0, f"no oversell was ever recorded (got {kpis.oversells})")
expect(kpis.orders_blocked >= 1, "at least one attempt was blocked")

print("== Other guards ==")
ledger = fresh()
result = ledger.place_order(order(AMAZON, "NOT-A-REAL-SKU", 1), now=NOW)
expect(result.error_code == ERR_UNKNOWN_LISTING,
       f"an unmapped listing is blocked with {ERR_UNKNOWN_LISTING} "
       f"(got {result.error_code!r})")
expect(ledger.stock == OPENING_STOCK, "an unmapped listing changes no stock")

for bad_quantity in (0, -1, -50):
    ledger = fresh()
    result = ledger.place_order(order(AMAZON, "AMZ-KETTLE-17", bad_quantity), now=NOW)
    expect(result.error_code == ERR_INVALID_QUANTITY,
           f"quantity {bad_quantity} is refused with {ERR_INVALID_QUANTITY} "
           f"(got {result.error_code!r})")
    expect(ledger.available(KETTLE) == OPENING_STOCK[KETTLE],
           f"quantity {bad_quantity} adds no stock through the back door")

print("== Failed syncs are captured, queued and replayed ==")
ledger = fresh()
ledger.degraded.add(EBAY)
result = ledger.place_order(order(AMAZON, "AMZ-KETTLE-17", 1), now=NOW)
expect(result.accepted, "the sale still completes when a channel is unreachable")
expect(ledger.available(KETTLE) == 23, "the centre is still correct")
statuses = {b.channel: b.status for b in result.broadcasts}
expect(statuses.get(EBAY) == FAILED,
       f"the unreachable channel records a failed broadcast (got {statuses})")
expect(statuses.get(SHOPIFY) == PUSHED,
       "the reachable channel still receives its update")
expect(len(ledger.failures) >= 1, "the failure reaches the error log")
expect(any(e.kind == "broadcast.failed" for e in ledger.events),
       "the audit trail names it as a failed broadcast")

ledger.degraded.discard(EBAY)
replayed = ledger.retry_failed(now=NOW)
expect(len(replayed) == 1, f"one queued write is replayed (got {len(replayed)})")
expect(replayed[0].channel == EBAY, "it replays to the channel that had failed")
expect(replayed[0].quantity == ledger.available(KETTLE),
       "the replay carries the current figure, not the stale one")
expect(any(e.kind == "broadcast.replayed" for e in ledger.events),
       "the replay is recorded in the audit trail")

print("== The audit trail is complete and well formed ==")
ledger = fresh()
ledger.place_order(order(AMAZON, "AMZ-KETTLE-17", 1), now=NOW)
ledger.place_order(order(EBAY, "EB_TOAST_2SL", 5, "ORD-X"), now=NOW)
kinds = [e.kind for e in ledger.events]
expect("order.accepted" in kinds, "an accepted order is logged")
expect("order.blocked" in kinds, "a blocked order is logged")
expect(kinds.count("broadcast.pushed") == 2, "both broadcasts are logged")
expect(all(e.sequence == i + 1 for i, e in enumerate(ledger.events)),
       "sequences are contiguous and append only")
expect(all(e.timestamp == NOW for e in ledger.events),
       "every row carries the supplied timestamp, so the trail is testable")
expect(all(e.correlation_id for e in ledger.events),
       "every row carries a correlation ID back to its order")

print("== Table rows survive Arrow serialisation ==")
rows = ledger.ledger_rows()
expect(len(rows) == len(SAMPLE_PRODUCTS), "one master row per product")
for column in {key for row in rows for key in row}:
    kinds_seen = {type(row[column]).__name__ for row in rows}
    expect(len(kinds_seen) == 1,
           f"master column {column!r} holds a single type (got {sorted(kinds_seen)})")
event_rows = ledger.event_rows()
for column in {key for row in event_rows for key in row}:
    kinds_seen = {type(row[column]).__name__ for row in event_rows}
    expect(len(kinds_seen) == 1,
           f"log column {column!r} holds a single type (got {sorted(kinds_seen)})")
expect(all(isinstance(r["In stock"], int) for r in rows),
       "the stock column is numeric so it sorts correctly")
try:
    import pandas
    from pyarrow import Table
    Table.from_pandas(pandas.DataFrame(rows))
    Table.from_pandas(pandas.DataFrame(event_rows))
    expect(True, "Arrow serialises both tables, which is what the browser does")
except ImportError:
    expect(True, "Arrow not installed here, serialisation check skipped")
except Exception as exc:  # noqa: BLE001
    expect(False, f"Arrow refused a table: {exc}")

result = ledger.orders[0]
b_rows = broadcast_rows(result)
expect(len(b_rows) == len(result.broadcasts), "one broadcast row per broadcast")
expect(all("Channel" in r and "New quantity" in r for r in b_rows),
       "broadcast rows carry the channel and the pushed quantity")

print("== Stock status thresholds ==")
expect(stock_status(0) == OUT_OF_STOCK, "zero is out of stock")
expect(stock_status(-1) == OUT_OF_STOCK, "a negative figure still reads out of stock")
expect(stock_status(1) == LOW_STOCK, "one is low stock")
expect(stock_status(3) == LOW_STOCK, "three is low stock")
expect(stock_status(4) == IN_STOCK, "four is in stock")

print("== Restock lifts stock and pushes everywhere ==")
ledger = fresh()
ledger.place_order(order(AMAZON, "AMZ-TOAST-2S", 1), now=NOW)
expect(ledger.available(TOASTER) == 0, "the toaster is sold out")
after = ledger.restock(TOASTER, 10, now=NOW)
expect(after == 10 and ledger.available(TOASTER) == 10,
       f"a restock lifts stock to 10 (got {after})")
restock_pushes = [e for e in ledger.events
                  if e.kind == "broadcast.pushed" and e.sku == TOASTER
                  and "10" in e.detail]
expect(len(restock_pushes) == 3,
       f"a restock pushes to all three channels (got {len(restock_pushes)})")
resale = ledger.place_order(order(EBAY, "EB_TOAST_2SL", 2, "ORD-R"), now=NOW)
expect(resale.accepted, "the item can be sold again once restocked")

try:
    ledger.restock(TOASTER, 0)
    expect(False, "a zero restock raises")
except ValueError:
    expect(True, "a zero restock raises ValueError")
try:
    ledger.restock("NOT-A-SKU", 5)
    expect(False, "restocking an unknown SKU raises")
except KeyError:
    expect(True, "restocking an unknown SKU raises KeyError")

print("== KPIs ==")
ledger = fresh()
ledger.place_order(order(AMAZON, "AMZ-KETTLE-17", 2), now=NOW)
ledger.place_order(order(EBAY, "EB_TOAST_2SL", 9, "ORD-K"), now=NOW)
kpis = sync_kpis(ledger)
expect(kpis.skus_tracked == len(SAMPLE_PRODUCTS), "every SKU is tracked")
expect(kpis.orders_accepted == 1, f"one order accepted (got {kpis.orders_accepted})")
expect(kpis.orders_blocked == 1, f"one order blocked (got {kpis.orders_blocked})")
expect(kpis.broadcasts_pushed == 2, f"two broadcasts pushed (got {kpis.broadcasts_pushed})")
expect(kpis.oversells == 0, "no oversells")
expect(kpis.total_units == sum(OPENING_STOCK.values()) - 2,
       f"total units fell by exactly the units sold (got {kpis.total_units})")
expect(kpis.slowest_ms < 1000.0,
       f"the slowest order resolved in under a second (got {kpis.slowest_ms:.3f} ms)")

print("== Punctuation discipline: no dash characters in user facing prose ==")
banned = ("—", "–")
prose: list[tuple[str, str]] = []
ledger = fresh()
ledger.degraded.add(SHOPIFY)
a = ledger.place_order(order(AMAZON, "AMZ-KETTLE-17", 1), now=NOW)
b = ledger.place_order(order(EBAY, "EB_TOAST_2SL", 4, "ORD-P"), now=NOW)
c = ledger.place_order(order(AMAZON, "NOPE", 1, "ORD-Q"), now=NOW)
for label, res in (("accepted", a), ("blocked", b), ("unmapped", c)):
    prose.append((f"{label} message", res.message))
    prose += [(f"{label} broadcast {x.channel}", x.detail) for x in res.broadcasts]
prose += [(f"event {e.sequence}", e.detail) for e in ledger.events]
prose += [(f"product {p.sku}", p.title) for p in SAMPLE_PRODUCTS]
offenders = [name for name, text in prose if any(ch in (text or "") for ch in banned)]
expect(not offenders, f"no em or en dashes in prose (offenders: {offenders[:5]})")

print()
if failures:
    print(f"INVENTORY RESULT: FAIL {len(failures)} of {checks} checks failed")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"INVENTORY RESULT: PASS {checks}/{checks}")
sys.exit(0)
