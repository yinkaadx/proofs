"""Tests for the Shopify Preorder Architecture Console.

Two things are worth proving. The router must never lose or invent a unit,
whatever the stock position, because a unit that vanishes between the cart
and the plan reaches a customer rather than a log. And the inventory policy
must be right for every product type at every stock level, including the two
misconfigurations that produce no error and no visible change on the
storefront, which is exactly why they survive.

Pass marker: pytest reports all tests passed, exit 0.
Fail marker: any failure or error line, exit non zero.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from itertools import product as combinations
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.shopify_preorder_architecture_console.core import (  # noqa: E402
    ADMIN_LABEL,
    AUDIT_CORRECT,
    AUDIT_DEAD_BUY_BUTTON,
    AUDIT_SILENT_OVERSELL,
    AUTHORIZATION_DAYS,
    CAPTURE_METHODS,
    CAPTURE_UPFRONT,
    CAPTURE_VAULT,
    DEFAULT_FIXED_FEE,
    DEFAULT_RATE,
    ENGINE_VERSION,
    POLICY_CONTINUE,
    POLICY_DENY,
    PRODUCT_TYPES,
    ROUTE_DISPATCH_NOW,
    ROUTE_HOLD_FOR_PREORDER,
    SAMPLE_CART,
    SECOND_PARCEL_COST,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    TYPE_BACKORDER,
    TYPE_DISCONTINUED,
    TYPE_IN_STOCK,
    TYPE_MADE_TO_ORDER,
    TYPE_PREORDER,
    CartItem,
    audit_current_setting,
    capture_comparison,
    evaluate_inventory_policy,
    money,
    simulate_payment_capture,
    simulate_split_cart,
)

STOCK_LEVELS = (-120, -1, 0, 1, 25)


# ---------------------------------------------------------------------------
# Split cart routing
# ---------------------------------------------------------------------------


def test_a_cart_that_is_fully_in_stock_ships_once():
    plan = simulate_split_cart((CartItem("A", "Item A", 2, 5),
                                CartItem("B", "Item B", 1, 1)))
    assert plan.shipment_count == 1
    assert not plan.is_split
    assert plan.units_held == 0
    assert plan.units_dispatched_now == 3
    assert plan.extra_shipping_cost == money(0)
    assert plan.shipments[0].route == ROUTE_DISPATCH_NOW


def test_a_line_is_split_rather_than_moved_whole():
    """Three ordered against one on hand sends one and holds two. Holding all
    three is a choice the customer did not make."""
    plan = simulate_split_cart((CartItem("A", "Item A", 3, 1, "2026-11-14"),))
    assert plan.units_dispatched_now == 1
    assert plan.units_held == 2
    assert plan.shipment_count == 2
    routes = [s.route for s in plan.shipments]
    assert routes == [ROUTE_DISPATCH_NOW, ROUTE_HOLD_FOR_PREORDER]


def test_no_unit_is_lost_or_invented_at_any_stock_position():
    """The invariant, checked across the grid rather than at one point."""
    for ordered in range(1, 7):
        for on_hand in range(0, 8):
            plan = simulate_split_cart(
                (CartItem("A", "Item A", ordered, on_hand, "2026-12-01"),))
            assert plan.units_requested == ordered, (ordered, on_hand)
            assert plan.units_dispatched_now + plan.units_held == ordered
            assert plan.units_dispatched_now == min(ordered, on_hand)
            shipped = sum(line.quantity for shipment in plan.shipments
                          for line in shipment.lines)
            assert shipped == ordered, (ordered, on_hand)


def test_a_cart_with_nothing_available_is_one_held_shipment_not_a_split():
    plan = simulate_split_cart((CartItem("A", "Item A", 2, 0, "2026-12-01"),))
    assert plan.shipment_count == 1
    assert plan.units_dispatched_now == 0
    assert plan.units_held == 2
    assert plan.extra_shipping_cost == money(0)
    assert any("nothing is gained by splitting" in note
               for note in plan.findings)


def test_the_extra_shipping_cost_follows_the_shipment_count():
    plan = simulate_split_cart(SAMPLE_CART)
    assert plan.shipment_count == 3
    assert plan.extra_shipping_cost == money(SECOND_PARCEL_COST * 2)
    single = simulate_split_cart((CartItem("A", "Item A", 1, 1),))
    assert single.extra_shipping_cost == money(0)


def test_a_held_line_with_no_ship_date_is_called_out():
    plan = simulate_split_cart((CartItem("A", "Item A", 2, 0, ""),))
    assert plan.latest_ship_date == "date not published"
    assert any("no published ship date" in note for note in plan.findings)
    dated = simulate_split_cart((CartItem("A", "Item A", 2, 0, "2026-11-14"),))
    assert dated.latest_ship_date == "2026-11-14"
    assert not any("no published ship date" in note for note in dated.findings)


def test_the_latest_ship_date_is_the_furthest_out_one():
    plan = simulate_split_cart((CartItem("A", "A", 1, 0, "2026-11-14"),
                               CartItem("B", "B", 1, 0, "2026-12-02")))
    assert plan.latest_ship_date == "2026-12-02"


def test_a_mixed_cart_warns_about_money_held_for_goods_that_have_not_moved():
    plan = simulate_split_cart((CartItem("A", "A", 1, 1),
                               CartItem("B", "B", 1, 0, "2026-12-02")))
    assert any("chargeback clock" in note for note in plan.findings)


def test_an_impossible_cart_raises_rather_than_routing_it():
    with pytest.raises(ValueError):
        simulate_split_cart(())
    with pytest.raises(ValueError):
        simulate_split_cart((CartItem("A", "A", 0, 1),))
    with pytest.raises(ValueError):
        simulate_split_cart((CartItem("A", "A", 1, -1),))


# ---------------------------------------------------------------------------
# Inventory policy
# ---------------------------------------------------------------------------


def test_a_preorder_must_continue_selling_and_must_keep_tracking():
    for stock in STOCK_LEVELS:
        policy = evaluate_inventory_policy(TYPE_PREORDER, stock)
        assert policy.inventory_policy == POLICY_CONTINUE, stock
        assert policy.continue_selling_when_out_of_stock
        assert policy.track_quantity, (
            "the negative count is the order book, so tracking stays on")
        assert policy.buy_button_live


def test_an_in_stock_product_must_stop_at_zero():
    for stock in STOCK_LEVELS:
        policy = evaluate_inventory_policy(TYPE_IN_STOCK, stock)
        assert policy.inventory_policy == POLICY_DENY, stock
        assert not policy.continue_selling_when_out_of_stock
        assert policy.buy_button_live == (stock > 0)


def test_made_to_order_continues_selling_and_stops_tracking():
    policy = evaluate_inventory_policy(TYPE_MADE_TO_ORDER, 0)
    assert policy.inventory_policy == POLICY_CONTINUE
    assert not policy.track_quantity, (
        "a tracked count on a made to order item is wrong forever")
    assert policy.buy_button_live


def test_a_backorder_continues_selling_and_keeps_the_queue():
    policy = evaluate_inventory_policy(TYPE_BACKORDER, -30)
    assert policy.inventory_policy == POLICY_CONTINUE
    assert policy.track_quantity
    assert "Cap it" in policy.fix


def test_discontinued_never_continues_selling():
    for stock in STOCK_LEVELS:
        policy = evaluate_inventory_policy(TYPE_DISCONTINUED, stock)
        assert policy.inventory_policy == POLICY_DENY, stock
        assert policy.buy_button_live == (stock > 0)


def test_the_policy_is_a_function_of_the_type_alone():
    """Stock moves the warnings and the buy button. It never moves the
    policy, because the policy is a property of what the product is."""
    for kind in PRODUCT_TYPES:
        policies = {evaluate_inventory_policy(kind, stock).inventory_policy
                    for stock in STOCK_LEVELS}
        assert len(policies) == 1, kind


def test_every_answer_names_the_checkbox_the_merchant_actually_sees():
    for kind in PRODUCT_TYPES:
        policy = evaluate_inventory_policy(kind, 0)
        assert policy.admin_label == ADMIN_LABEL[policy.inventory_policy]
        assert "Continue selling when out of stock" in policy.admin_label
        expected = "ticked" if policy.continue_selling_when_out_of_stock else "unticked"
        assert policy.admin_label.endswith(expected), kind


def test_a_preorder_holding_real_stock_is_flagged():
    flagged = evaluate_inventory_policy(TYPE_PREORDER, 12)
    assert any("marked preorder while 12 units" in note
               for note in flagged.findings)
    clean = evaluate_inventory_policy(TYPE_PREORDER, -12)
    assert any("12 unit(s) are already" in note for note in clean.findings)


def test_a_preorder_always_says_the_setting_alone_is_not_enough():
    for stock in STOCK_LEVELS:
        policy = evaluate_inventory_policy(TYPE_PREORDER, stock)
        assert any("indistinguishable from overselling" in note
                   for note in policy.findings), stock


def test_an_unknown_product_type_raises():
    with pytest.raises(ValueError):
        evaluate_inventory_policy("Mystery Box", 4)


# ---------------------------------------------------------------------------
# The audit of what the store is actually set to
# ---------------------------------------------------------------------------


def test_a_matching_setting_is_reported_correct_for_every_type():
    for kind, stock in combinations(PRODUCT_TYPES, STOCK_LEVELS):
        required = evaluate_inventory_policy(kind, stock)
        audit = audit_current_setting(
            kind, stock, required.continue_selling_when_out_of_stock)
        assert audit.verdict == AUDIT_CORRECT, (kind, stock)
        assert audit.severity == SEVERITY_OK


def test_every_mismatch_is_critical_and_named_for_what_it_does():
    """Two failures, neither of which shows on the storefront."""
    for kind, stock in combinations(PRODUCT_TYPES, STOCK_LEVELS):
        required = evaluate_inventory_policy(kind, stock)
        wrong = not required.continue_selling_when_out_of_stock
        audit = audit_current_setting(kind, stock, wrong)
        assert audit.severity == SEVERITY_CRITICAL, (kind, stock)
        expected = (AUDIT_SILENT_OVERSELL if wrong else AUDIT_DEAD_BUY_BUTTON)
        assert audit.verdict == expected, (kind, stock)
        assert audit.current_policy != audit.required_policy


def test_an_in_stock_product_set_to_continue_is_a_silent_oversell():
    audit = audit_current_setting(TYPE_IN_STOCK, 0, True)
    assert audit.verdict == AUDIT_SILENT_OVERSELL
    assert audit.required_policy == POLICY_DENY
    assert audit.current_policy == POLICY_CONTINUE
    assert "says nothing at all" in audit.headline


def test_a_preorder_set_to_deny_loses_its_buy_button():
    audit = audit_current_setting(TYPE_PREORDER, -5, False)
    assert audit.verdict == AUDIT_DEAD_BUY_BUTTON
    assert audit.required_policy == POLICY_CONTINUE
    assert "nothing to click" in audit.headline


# ---------------------------------------------------------------------------
# Payment capture
# ---------------------------------------------------------------------------


def test_an_upfront_charge_moves_cash_and_creates_a_liability():
    ledger = simulate_payment_capture(CAPTURE_UPFRONT, "249.00")
    assert ledger.cash_today == money("249.00")
    assert ledger.deferred_revenue == money("249.00")
    assert ledger.revenue_recognised_today == money(0), (
        "revenue belongs to the period the goods move")
    expected_fee = money(Decimal("249.00") * DEFAULT_RATE + DEFAULT_FIXED_FEE)
    assert ledger.processing_fee_today == expected_fee
    assert ledger.net_cash_today == money(Decimal("249.00") - expected_fee)


def test_a_vault_authorization_moves_nothing_today():
    ledger = simulate_payment_capture(CAPTURE_VAULT, "249.00")
    assert ledger.cash_today == money(0)
    assert ledger.deferred_revenue == money(0)
    assert ledger.processing_fee_today == money(0)
    assert ledger.revenue_recognised_today == money(0)
    assert ledger.authorization_expires_in_days == AUTHORIZATION_DAYS


def test_neither_method_recognises_revenue_on_the_day_of_the_order():
    for method in CAPTURE_METHODS:
        for amount in ("1.00", "249.00", "8999.99"):
            ledger = simulate_payment_capture(method, amount)
            assert ledger.revenue_recognised_today == money(0), (method, amount)


def test_the_journal_balances_for_every_method_and_amount():
    """Double entry, checked rather than claimed, and the totals are the sum
    of the rows they print."""
    for method in CAPTURE_METHODS:
        for amount in ("0.01", "1.00", "37.49", "249.00", "12500.00"):
            ledger = simulate_payment_capture(method, amount)
            assert ledger.balances, (method, amount)
            assert ledger.total_debits == money(
                sum(entry.debit for entry in ledger.entries))
            assert ledger.total_credits == money(
                sum(entry.credit for entry in ledger.entries))
            for entry in ledger.entries:
                assert not (entry.debit and entry.credit), (
                    "a row is one side or the other, never both")


def test_the_net_cash_is_the_amount_less_the_fee_and_nothing_else():
    for amount in ("10.00", "100.00", "999.99"):
        ledger = simulate_payment_capture(CAPTURE_UPFRONT, amount)
        assert ledger.net_cash_today == money(
            ledger.cash_today - ledger.processing_fee_today), amount


def test_the_chargeback_clock_starts_at_different_moments():
    upfront = simulate_payment_capture(CAPTURE_UPFRONT, "100.00")
    vault = simulate_payment_capture(CAPTURE_VAULT, "100.00")
    assert "today" in upfront.chargeback_clock_starts
    assert "fulfillment" in vault.chargeback_clock_starts
    assert upfront.chargeback_clock_starts != vault.chargeback_clock_starts


def test_the_comparison_returns_both_methods_on_the_same_amount():
    rows = capture_comparison("500.00")
    assert len(rows) == len(CAPTURE_METHODS)
    assert {row.capture_method for row in rows} == set(CAPTURE_METHODS)
    assert all(row.amount == money("500.00") for row in rows)
    assert all(row.balances for row in rows)


def test_an_impossible_capture_raises():
    with pytest.raises(ValueError):
        simulate_payment_capture("Cheque in the post", "100.00")
    with pytest.raises(ValueError):
        simulate_payment_capture(CAPTURE_UPFRONT, "0.00")
    with pytest.raises(ValueError):
        simulate_payment_capture(CAPTURE_UPFRONT, "-5.00")


def test_money_rounds_once_at_the_point_it_becomes_currency():
    assert money("0.005") == Decimal("0.01")
    assert money("2.344") == Decimal("2.34")
    assert money("2.345") == Decimal("2.35")


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "shopify_preorder_architecture_console"
              / "core.py").read_text()
    assert "import streamlit" not in source
    probe = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.shopify_preorder_architecture_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT)
    )
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [p.headline + p.fix + " ".join(p.findings)
         for p in (simulate_split_cart(SAMPLE_CART),
                   simulate_split_cart((CartItem("A", "A", 2, 0),)),
                   simulate_split_cart((CartItem("A", "A", 2, 2),)))]
        + [evaluate_inventory_policy(kind, stock).headline
           + evaluate_inventory_policy(kind, stock).fix
           + " ".join(evaluate_inventory_policy(kind, stock).findings)
           for kind, stock in combinations(PRODUCT_TYPES, STOCK_LEVELS)]
        + [audit_current_setting(kind, stock, flag).headline
           + audit_current_setting(kind, stock, flag).fix
           for kind, stock in combinations(PRODUCT_TYPES, STOCK_LEVELS)
           for flag in (True, False)]
        + [ledger.headline + ledger.fix + " ".join(ledger.findings)
           for ledger in capture_comparison("249.00")]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools
    tools = all_tools()
    entry = next(t for t in tools
                 if t.key == "shopify-preorder-architecture-console")
    assert entry.title == "Shopify Preorder Architecture Console"
    icons = [t.icon for t in tools]
    assert icons.count(entry.icon) == 1, "this icon is already used by another tool"
    assert len(entry.tagline) > 30
