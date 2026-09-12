"""Engine tests for the Print on Demand Automation Router.

Written for pytest. Deterministic throughout: the clock and the random source
are injected, so a seeded run reproduces exactly.

Run: pytest tests/test_pod_automation_router.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.pod_automation_router.core import (  # noqa: E402
    AMAZON,
    CATALOGUE,
    CHANNELS,
    EBAY,
    ERR_EMPTY_ORDER,
    ERR_INCOMPLETE_ADDRESS,
    ERR_NONE,
    ERR_OUT_OF_STOCK,
    ERR_UNMAPPED_VARIANT,
    ETSY,
    REJECTED,
    ROUTED,
    WOOCOMMERCE,
    Address,
    LineItem,
    RouterLog,
    TranslationError,
    WooOrder,
    catalogue_rows,
    find_variant,
    generate_order,
    generate_tracking_number,
    product_by_sku,
    route_order,
    router_kpis,
    sync_targets,
    sync_tracking,
    tracking_rows,
    translate,
)

NOW = "2026-09-12T12:00:00Z"

IN_STOCK_SKU = "TEE-CLASSIC-BLK"
IN_STOCK_SIZE = "M"
OUT_OF_STOCK_SIZE = "L"          # seeded with zero blank stock
GOOD_ADDRESS = Address("Amara Okafor", "14 Bramble Way", "Leeds", "LS6 2QT", "GB")


def order(sku: str = IN_STOCK_SKU, size: str = IN_STOCK_SIZE, quantity: int = 1,
          origin: str = WOOCOMMERCE, address: Address = GOOD_ADDRESS,
          items: tuple | None = None, order_id: str = "WC-00001") -> WooOrder:
    lines = items if items is not None else (LineItem(sku, size, quantity),)
    return WooOrder(order_id=order_id, origin=origin, email="buyer@example.com",
                    items=lines, shipping=address, placed_at=NOW)


# ---------------------------------------------------------------------------
# Payload translation
# ---------------------------------------------------------------------------

def test_a_clean_order_translates_to_a_printful_request():
    request = translate(order())
    assert request.external_id == "WC-00001"
    assert len(request.items) == 1
    assert request.items[0].variant_id == find_variant(IN_STOCK_SKU,
                                                       IN_STOCK_SIZE).printful_variant_id


def test_translation_maps_sku_and_size_to_the_numeric_variant():
    for product in CATALOGUE:
        for variant in product.variants:
            request = translate(order(product.sku, variant.size))
            assert request.items[0].variant_id == variant.printful_variant_id


def test_the_recipient_is_carried_across_into_printful_field_names():
    request = translate(order())
    recipient = request.recipient
    assert recipient["name"] == GOOD_ADDRESS.name
    assert recipient["address1"] == GOOD_ADDRESS.address1
    assert recipient["city"] == GOOD_ADDRESS.city
    # Printful calls it zip, WooCommerce calls it postcode. The translation is
    # the only place that difference should exist.
    assert recipient["zip"] == GOOD_ADDRESS.postcode
    assert recipient["country_code"] == GOOD_ADDRESS.country_code
    assert "postcode" not in recipient


def test_retail_price_is_serialised_as_a_string_with_two_decimals():
    request = translate(order())
    price = request.items[0].retail_price
    assert isinstance(price, str)
    assert price == "22.00"


def test_quantity_survives_translation():
    request = translate(order(quantity=3))
    assert request.items[0].quantity == 3


def test_multiple_lines_all_translate():
    lines = (LineItem(IN_STOCK_SKU, "S", 1), LineItem("MUG-ENAMEL-WHT", "One size", 2))
    request = translate(order(items=lines))
    assert len(request.items) == 2
    assert {i.quantity for i in request.items} == {1, 2}


def test_the_payload_is_shaped_the_way_printful_expects():
    payload = translate(order()).as_payload()
    assert set(payload) == {"external_id", "recipient", "items"}
    assert isinstance(payload["items"], list)
    assert set(payload["items"][0]) == {"variant_id", "quantity", "retail_price",
                                        "name"}


def test_an_unmapped_size_is_refused_rather_than_guessed():
    with pytest.raises(TranslationError) as raised:
        translate(order(size="XXXL"))
    assert raised.value.code == ERR_UNMAPPED_VARIANT
    assert "ships the wrong garment" in raised.value.message


def test_an_unmapped_size_names_the_sizes_that_do_exist():
    with pytest.raises(TranslationError) as raised:
        translate(order(size="XXXL"))
    for size in product_by_sku(IN_STOCK_SKU).sizes:
        assert size in raised.value.message


def test_an_empty_order_is_refused():
    with pytest.raises(TranslationError) as raised:
        translate(order(items=()))
    assert raised.value.code == ERR_EMPTY_ORDER


@pytest.mark.parametrize("field,value", [
    ("name", ""), ("address1", ""), ("city", ""), ("postcode", ""),
    ("country_code", ""),
])
def test_an_incomplete_address_is_refused_before_submission(field, value):
    broken = Address(**{**GOOD_ADDRESS.__dict__, field: value})
    with pytest.raises(TranslationError) as raised:
        translate(order(address=broken))
    assert raised.value.code == ERR_INCOMPLETE_ADDRESS


def test_the_incomplete_address_error_names_what_is_missing():
    broken = Address("Amara Okafor", "14 Bramble Way", "", "LS6 2QT", "GB")
    with pytest.raises(TranslationError) as raised:
        translate(order(address=broken))
    assert "city" in raised.value.message
    assert broken.missing == ("city",)


def test_a_complete_address_reports_nothing_missing():
    assert GOOD_ADDRESS.complete is True
    assert GOOD_ADDRESS.missing == ()


# ---------------------------------------------------------------------------
# Stock exception guard
# ---------------------------------------------------------------------------

def test_an_out_of_stock_blank_is_caught_and_not_submitted():
    log = RouterLog()
    result = route_order(order(size=OUT_OF_STOCK_SIZE), log, NOW)
    assert result.status == REJECTED
    assert result.error_code == ERR_OUT_OF_STOCK
    assert result.printful_order_id == ""


def test_the_out_of_stock_alert_names_the_sku_the_size_and_the_shortfall():
    log = RouterLog()
    result = route_order(order(size=OUT_OF_STOCK_SIZE, quantity=2), log, NOW)
    assert IN_STOCK_SKU in result.message
    assert OUT_OF_STOCK_SIZE in result.message
    assert "has 0 in stock" in result.message
    assert "needs 2" in result.message


def test_the_out_of_stock_failure_is_logged_as_an_alert_not_swallowed():
    log = RouterLog()
    route_order(order(size=OUT_OF_STOCK_SIZE), log, NOW)
    assert len(log.alerts) == 1
    alert = log.alerts[0]
    assert alert.stage == "fulfil"
    assert ERR_OUT_OF_STOCK in alert.detail


def test_an_order_needing_more_than_the_blank_stock_is_refused():
    variant = find_variant("HOOD-HEAVY-NVY", "L")
    assert variant.blank_stock == 7
    log = RouterLog()
    result = route_order(order("HOOD-HEAVY-NVY", "L", variant.blank_stock + 1),
                         log, NOW)
    assert result.error_code == ERR_OUT_OF_STOCK


def test_an_order_equal_to_the_blank_stock_is_accepted():
    variant = find_variant("HOOD-HEAVY-NVY", "L")
    log = RouterLog()
    result = route_order(order("HOOD-HEAVY-NVY", "L", variant.blank_stock),
                         log, NOW)
    assert result.status == ROUTED


def test_every_refusal_reaches_the_log_as_an_alert():
    cases = [
        order(size="XXXL"),
        order(items=()),
        order(address=Address("", "", "", "", "")),
        order(size=OUT_OF_STOCK_SIZE),
    ]
    for index, candidate in enumerate(cases):
        log = RouterLog()
        result = route_order(candidate, log, NOW)
        assert result.status == REJECTED, f"case {index} should be refused"
        assert log.alerts, f"case {index} left no alert in the log"


def test_a_routed_order_carries_no_error_code_and_a_printful_id():
    log = RouterLog()
    result = route_order(order(), log, NOW)
    assert result.status == ROUTED
    assert result.error_code == ERR_NONE
    assert result.printful_order_id.startswith("PF-")
    assert not log.alerts


def test_the_log_is_append_only_and_contiguous():
    log = RouterLog()
    route_order(order(order_id="WC-1"), log, NOW)
    route_order(order(order_id="WC-2", size=OUT_OF_STOCK_SIZE), log, NOW)
    assert [e.sequence for e in log.events] == list(range(1, len(log.events) + 1))
    assert all(e.timestamp == NOW for e in log.events)


def test_router_kpis_count_what_happened():
    log = RouterLog()
    route_order(order(order_id="WC-1"), log, NOW)
    route_order(order(order_id="WC-2", size=OUT_OF_STOCK_SIZE), log, NOW)
    kpis = router_kpis(log)
    assert kpis.received == 2
    assert kpis.routed == 1
    assert kpis.alerts == 1


# ---------------------------------------------------------------------------
# Tracking synchronisation
# ---------------------------------------------------------------------------

def test_a_woocommerce_order_syncs_only_to_woocommerce():
    assert sync_targets(WOOCOMMERCE) == (WOOCOMMERCE,)


@pytest.mark.parametrize("marketplace", [ETSY, EBAY, AMAZON])
def test_a_marketplace_order_syncs_to_the_shop_and_that_marketplace(marketplace):
    targets = sync_targets(marketplace)
    assert WOOCOMMERCE in targets
    assert marketplace in targets
    assert len(targets) == 2


@pytest.mark.parametrize("marketplace", [ETSY, EBAY, AMAZON])
def test_tracking_never_reaches_a_channel_that_never_saw_the_order(marketplace):
    targets = set(sync_targets(marketplace))
    strangers = set(CHANNELS) - {WOOCOMMERCE, marketplace}
    assert targets.isdisjoint(strangers)


def test_tracking_sync_pushes_to_every_target_for_the_origin():
    log = RouterLog()
    placed = order(origin=ETSY)
    fulfilment = route_order(placed, log, NOW)
    update = sync_tracking(fulfilment, placed, log, random.Random(7), NOW)
    assert set(update.channels) == set(sync_targets(ETSY))
    assert all(p.status == "pushed" for p in update.pushes)


def test_the_tracking_number_is_carrier_shaped_and_reproducible():
    first = generate_tracking_number(random.Random(11))
    second = generate_tracking_number(random.Random(11))
    assert first == second
    assert first.startswith("RM") and first.endswith("GB")
    assert len(first) == 13
    assert first[2:11].isdigit()


def test_the_tracking_url_contains_the_tracking_number():
    log = RouterLog()
    placed = order()
    fulfilment = route_order(placed, log, NOW)
    update = sync_tracking(fulfilment, placed, log, random.Random(3), NOW)
    assert update.tracking_number in update.tracking_url


def test_tracking_is_refused_for_an_order_that_was_never_submitted():
    log = RouterLog()
    placed = order(size=OUT_OF_STOCK_SIZE)
    fulfilment = route_order(placed, log, NOW)
    with pytest.raises(ValueError):
        sync_tracking(fulfilment, placed, log, random.Random(1), NOW)


def test_the_shipment_time_is_after_the_order_time():
    log = RouterLog()
    placed = order()
    fulfilment = route_order(placed, log, NOW)
    update = sync_tracking(fulfilment, placed, log, random.Random(1), NOW,
                           ship_after_minutes=90)
    assert update.shipped_at == "2026-09-12T13:30:00Z"


def test_tracking_rows_are_uniform_for_the_table():
    log = RouterLog()
    placed = order(origin=AMAZON)
    fulfilment = route_order(placed, log, NOW)
    rows = tracking_rows(sync_tracking(fulfilment, placed, log,
                                       random.Random(5), NOW))
    assert len(rows) == 2
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"column {column!r} mixes types: {sorted(kinds)}"


def test_the_tracking_push_is_recorded_in_the_log():
    log = RouterLog()
    placed = order(origin=EBAY)
    fulfilment = route_order(placed, log, NOW)
    update = sync_tracking(fulfilment, placed, log, random.Random(2), NOW)
    pushes = [e for e in log.events if e.stage == "track"]
    assert len(pushes) == 1
    assert update.tracking_number in pushes[0].detail


# ---------------------------------------------------------------------------
# Order generation and tables
# ---------------------------------------------------------------------------

def test_a_generated_order_is_reproducible_for_a_given_seed():
    first = generate_order(random.Random(42), NOW)
    second = generate_order(random.Random(42), NOW)
    assert first == second


def test_a_generated_order_is_internally_consistent():
    for seed in range(25):
        generated = generate_order(random.Random(seed), NOW, order_number=seed + 1)
        assert generated.items, "a generated order must have a line"
        line = generated.items[0]
        assert find_variant(line.sku, line.size) is not None, (
            "a generated order must reference a size that exists")
        assert generated.origin in CHANNELS
        assert generated.shipping.complete


def test_catalogue_rows_are_uniform_and_flag_the_out_of_stock_blanks():
    rows = catalogue_rows()
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"column {column!r} mixes types: {sorted(kinds)}"
    out_of_stock = [r for r in rows if r["Status"] == "Out of stock"]
    assert out_of_stock, "the fixture should include an out of stock blank"
    assert all(r["Blank stock"] == 0 for r in out_of_stock)


def test_log_rows_are_uniform_for_the_table():
    log = RouterLog()
    route_order(order(order_id="WC-1"), log, NOW)
    route_order(order(order_id="WC-2", size=OUT_OF_STOCK_SIZE), log, NOW)
    rows = log.rows()
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"column {column!r} mixes types: {sorted(kinds)}"


# ---------------------------------------------------------------------------
# House style
# ---------------------------------------------------------------------------

def test_no_em_or_en_dashes_in_user_facing_prose():
    banned = ("—", "–")
    log = RouterLog()
    prose: list[str] = []
    placed = order()
    fulfilment = route_order(placed, log, NOW)
    prose.append(fulfilment.message)
    update = sync_tracking(fulfilment, placed, log, random.Random(1), NOW)
    prose += [p.detail for p in update.pushes]
    for bad in (order(size="XXXL"), order(items=()),
                order(size=OUT_OF_STOCK_SIZE),
                order(address=Address("", "", "", "", ""))):
        prose.append(route_order(bad, log, NOW).message)
    prose += [e.detail for e in log.events]
    prose += [p.name for p in CATALOGUE]
    offenders = [p for p in prose if any(b in p for b in banned)]
    assert not offenders, f"em or en dashes found: {offenders[:5]}"
