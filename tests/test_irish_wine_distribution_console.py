"""Tests for the Irish Wine Wholesale Operating Console.

The duty assertions are anchored on two figures that are quoted publicly and
independently of this engine: Irish excise on a standard 75cl bottle is 3.19
euro for still wine in the 5.5 to 15 band and 6.37 euro for sparkling wine
over 5.5. Both are reproduced here from the per hectolitre rates rather than
written in as constants, so a wrong rate in the table fails these tests rather
than agreeing with itself.

Money is integer cents everywhere. Any test that compares a euro string is
comparing a rendered value, and any test that compares arithmetic is comparing
cents.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from tools.irish_wine_distribution_console.core import (
    BOTTLE_SIZES_ML,
    BOTTLE_TYPES,
    CATALOGUE,
    CUSTOMER_TIERS,
    CUSTOMER_TYPES,
    DUTY_BANDS,
    ENGINE_VERSION,
    HECTOLITRE_ML,
    PRODUCT_BY_ID,
    SAMPLE_ORDERS,
    SEVERITY_CRIT,
    SEVERITY_OK,
    SEVERITY_WARN,
    SPARKLING,
    STILL,
    TIER_BY_NAME,
    TIER_CHAIN,
    TIER_INDEPENDENT,
    TIER_RESTAURANT,
    VAT_RATE,
    WAREHOUSE_BONDED,
    WAREHOUSE_DUTY_PAID,
    WAREHOUSE_TYPES,
    band_cliffs,
    band_for,
    calculate_irish_wine_duty,
    dispatch_summary_rows,
    duty_band_rows,
    duty_is_flat_within_a_band,
    duty_per_bottle_cents,
    euros,
    generate_3pl_dispatch_payload,
    get_customer_tier_price,
    margin_erosion,
    tier_rows,
    to_cents,
)

ROOT = Path(__file__).resolve().parents[1]

STANDARD = 750


# ---------------------------------------------------------------------------
# The two independently quoted anchors
# ---------------------------------------------------------------------------


def test_a_standard_still_wine_bottle_carries_three_nineteen():
    """The figure quoted publicly for Ireland, reproduced from the rate."""
    assert duty_per_bottle_cents(STILL, "13.0", STANDARD) == 319


def test_a_standard_sparkling_bottle_carries_six_thirty_seven():
    assert duty_per_bottle_cents(SPARKLING, "11.0", STANDARD) == 637


def test_sparkling_is_almost_exactly_double_still_at_the_same_strength():
    still = duty_per_bottle_cents(STILL, "12.0", STANDARD)
    sparkling = duty_per_bottle_cents(SPARKLING, "12.0", STANDARD)
    assert sparkling == 2 * still - 1   # 637 against 638, one cent of rounding


def test_a_fortified_still_wine_carries_four_sixty_two():
    assert duty_per_bottle_cents(STILL, "19.5", STANDARD) == 462


def test_a_low_strength_wine_carries_one_oh_six():
    for bottle_type in BOTTLE_TYPES:
        assert duty_per_bottle_cents(bottle_type, "4.0", STANDARD) == 106


# ---------------------------------------------------------------------------
# The band table
# ---------------------------------------------------------------------------


def test_five_bands_cover_still_and_sparkling():
    assert len(DUTY_BANDS) == 5
    assert sum(1 for b in DUTY_BANDS if b.bottle_type == STILL) == 3
    assert sum(1 for b in DUTY_BANDS if b.bottle_type == SPARKLING) == 2


def test_the_rates_are_the_published_ones():
    rates = {(b.bottle_type, b.label): b.rate_per_hl_cents for b in DUTY_BANDS}
    assert set(rates.values()) == {
        to_cents("141.57"), to_cents("424.84"), to_cents("616.45"),
        to_cents("849.68")}


def test_the_low_strength_rate_is_the_same_for_both_product_types():
    low = [b for b in DUTY_BANDS if b.abv_up_to == Decimal("5.5")]
    assert len(low) == 2
    assert len({b.rate_per_hl_cents for b in low}) == 1


@pytest.mark.parametrize("bottle_type,abv,expected_rate", [
    (STILL, "0.5", "141.57"), (STILL, "5.5", "141.57"),
    (STILL, "5.6", "424.84"), (STILL, "15.0", "424.84"),
    (STILL, "15.1", "616.45"), (STILL, "22.0", "616.45"),
    (SPARKLING, "5.5", "141.57"), (SPARKLING, "5.6", "849.68"),
    (SPARKLING, "12.0", "849.68"),
])
def test_the_right_band_is_selected_at_every_boundary(bottle_type, abv,
                                                      expected_rate):
    assert band_for(bottle_type, abv).rate_per_hl_cents == to_cents(
        expected_rate)


def test_the_upper_boundary_of_a_band_is_inclusive():
    assert band_for(STILL, "15.0").abv_up_to == Decimal("15")
    assert band_for(STILL, "15.0").rate_per_hl_cents == to_cents("424.84")


def test_above_twenty_two_percent_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="dutied as spirits"):
        band_for(STILL, "22.5")


def test_a_negative_strength_is_refused():
    with pytest.raises(ValueError, match="abv cannot be negative"):
        band_for(STILL, "-1")


def test_an_unknown_product_type_is_refused():
    with pytest.raises(ValueError, match="bottle_type must be one of"):
        band_for("Fortified", "18.0")


def test_a_non_positive_bottle_size_is_refused():
    with pytest.raises(ValueError, match="size_ml must be positive"):
        duty_per_bottle_cents(STILL, "13.0", 0)


# ---------------------------------------------------------------------------
# Duty is per hectolitre of product, not of alcohol
# ---------------------------------------------------------------------------


def test_strength_does_not_scale_duty_inside_a_band():
    """The property most often assumed false. Beer and spirits scale; wine
    selects a band and stops."""
    assert duty_is_flat_within_a_band(STILL, "5.6", "15.0") is True
    assert duty_is_flat_within_a_band(SPARKLING, "5.6", "22.0") is True


def test_every_strength_inside_the_main_still_band_pays_the_same():
    reference = duty_per_bottle_cents(STILL, "5.6", STANDARD)
    step = Decimal("5.6")
    while step <= Decimal("15.0"):
        assert duty_per_bottle_cents(STILL, step, STANDARD) == reference
        step += Decimal("0.1")


def test_duty_scales_with_volume_and_nothing_else():
    half = duty_per_bottle_cents(STILL, "13.0", 375)
    standard = duty_per_bottle_cents(STILL, "13.0", 750)
    magnum = duty_per_bottle_cents(STILL, "13.0", 1500)
    assert standard == pytest.approx(2 * half, abs=1)
    assert magnum == pytest.approx(2 * standard, abs=1)


def test_a_bag_in_box_pays_four_standard_bottles_of_duty():
    box = duty_per_bottle_cents(STILL, "13.0", BOTTLE_SIZES_ML["Bag in box 300cl"])
    bottle = duty_per_bottle_cents(STILL, "13.0", STANDARD)
    assert box == pytest.approx(4 * bottle, abs=1)


def test_the_rate_is_applied_per_hectolitre():
    band = band_for(STILL, "13.0")
    expected = round(band.rate_per_hl_cents * STANDARD / HECTOLITRE_ML)
    assert duty_per_bottle_cents(STILL, "13.0", STANDARD) == expected


# ---------------------------------------------------------------------------
# The cliffs
# ---------------------------------------------------------------------------


def test_three_cliffs_are_reported():
    assert len(band_cliffs()) == 3


def test_the_fifteen_percent_cliff_costs_a_euro_forty_three():
    cliff = next(r for r in band_cliffs()
                 if r["Product"] == STILL and r["At or below"] == "15%")
    assert cliff["Step"] == "€1.43"
    assert cliff["Per case of 12"] == "€17.16"


def test_the_sparkling_cliff_is_the_steepest():
    steps = {(r["Product"], r["At or below"]):
             float(r["Step"].lstrip("€")) for r in band_cliffs()}
    assert steps[(SPARKLING, "5.5%")] == max(steps.values())


def test_every_cliff_is_a_step_upward():
    for row in band_cliffs():
        assert float(row["Step"].lstrip("€")) > 0


def test_a_tenth_of_a_percent_can_change_the_duty():
    assert duty_per_bottle_cents(STILL, "15.0") != \
        duty_per_bottle_cents(STILL, "15.1")


# ---------------------------------------------------------------------------
# The invoice
# ---------------------------------------------------------------------------


def test_vat_is_charged_on_goods_plus_duty():
    calc = calculate_irish_wine_duty(STILL, "13.0", 12, "5.20")
    assert calc.duty_suspended_cents == 6240
    assert calc.duty_cents == 12 * 319
    assert calc.net_of_vat_cents == 6240 + 12 * 319
    expected_vat = round(calc.net_of_vat_cents * 23 / 100)
    assert calc.vat_cents == expected_vat


def test_the_shortcut_of_charging_vat_on_goods_alone_understates_the_line():
    calc = calculate_irish_wine_duty(STILL, "13.0", 12, "5.20")
    assert calc.vat_on_goods_only_cents < calc.vat_cents
    assert calc.understatement_cents > 0


def test_the_understatement_is_the_vat_on_the_duty_give_or_take_a_cent():
    """Rounding is not distributive.

    The understatement is the difference of two independently rounded VAT
    figures, which is not the same as rounding 23 percent of the duty. On this
    line they are 881 and 880. The engine is right and the tidy identity is
    wrong, which is the sort of cent that turns up in a reconciliation and
    gets blamed on the wrong thing.
    """
    calc = calculate_irish_wine_duty(STILL, "13.0", 12, "5.20")
    naive = round(calc.duty_cents * 23 / 100)
    assert calc.understatement_cents == 881
    assert naive == 880
    assert abs(calc.understatement_cents - naive) <= 1


def test_the_rounding_gap_never_exceeds_a_cent_across_the_catalogue():
    for product in CATALOGUE:
        for quantity in (1, 6, 12, 60, 480):
            calc = calculate_irish_wine_duty(
                product.bottle_type, product.abv, quantity,
                product.cost_cents / 100, product.size_ml)
            naive = round(calc.duty_cents * 23 / 100)
            assert abs(calc.understatement_cents - naive) <= 1


def test_the_understatement_is_seventy_three_cent_a_bottle_on_still_wine():
    calc = calculate_irish_wine_duty(STILL, "13.0", 1, "5.20")
    assert calc.understatement_cents == 73


def test_the_total_is_the_sum_of_its_parts():
    calc = calculate_irish_wine_duty(SPARKLING, "11.0", 60, "5.10")
    assert calc.total_cents == (calc.duty_suspended_cents + calc.duty_cents
                                + calc.vat_cents)


def test_the_invoice_lines_foot_to_the_total():
    calc = calculate_irish_wine_duty(STILL, "14.0", 24, "6.40")
    rows = {r["Line"]: r["Amount"] for r in calc.rows()}
    goods = float(rows[[k for k in rows if k.startswith("Goods")][0]]
                  .lstrip("€").replace(",", ""))
    duty = float(rows[[k for k in rows if k.startswith("Alcohol")][0]]
                 .lstrip("€").replace(",", ""))
    vat = float(rows[[k for k in rows if k.startswith("VAT")][0]]
                .lstrip("€").replace(",", ""))
    total = float(rows["Total invoice price"].lstrip("€").replace(",", ""))
    assert goods + duty + vat == pytest.approx(total, abs=0.01)


def test_the_vat_rate_is_the_irish_standard_rate():
    assert VAT_RATE == Decimal("0.23")


def test_a_zero_quantity_is_refused():
    with pytest.raises(ValueError, match="quantity must be positive"):
        calculate_irish_wine_duty(STILL, "13.0", 0, "5.20")


def test_a_negative_price_is_refused():
    with pytest.raises(ValueError, match="cannot be negative"):
        calculate_irish_wine_duty(STILL, "13.0", 12, "-1.00")


def test_a_free_of_charge_line_still_carries_duty():
    """A sample case is not a duty free case."""
    calc = calculate_irish_wine_duty(STILL, "13.0", 12, "0.00")
    assert calc.duty_suspended_cents == 0
    assert calc.duty_cents == 12 * 319
    assert calc.total_cents > 0


def test_money_never_becomes_a_float_in_the_result():
    calc = calculate_irish_wine_duty(STILL, "13.0", 7, "5.33")
    for value in (calc.duty_suspended_cents, calc.duty_cents, calc.vat_cents,
                  calc.total_cents, calc.duty_per_bottle_cents):
        assert isinstance(value, int)


def test_a_thousand_bottles_do_not_drift_a_cent():
    one = calculate_irish_wine_duty(STILL, "13.0", 1, "5.20")
    many = calculate_irish_wine_duty(STILL, "13.0", 1000, "5.20")
    assert many.duty_cents == 1000 * one.duty_cents
    assert many.duty_suspended_cents == 1000 * one.duty_suspended_cents


def test_the_vat_finding_always_fires():
    for bottle_type in BOTTLE_TYPES:
        calc = calculate_irish_wine_duty(bottle_type, "12.0", 6, "8.00")
        assert "IE-VAT-ON-DUTY" in [f.code for f in calc.findings]


def test_the_cliff_finding_fires_near_fifteen_percent():
    calc = calculate_irish_wine_duty(STILL, "14.8", 12, "6.40")
    finding = next(f for f in calc.findings if f.code == "IE-ABV-CLIFF")
    assert finding.severity == SEVERITY_CRIT


def test_the_cliff_finding_stays_quiet_away_from_the_boundary():
    calc = calculate_irish_wine_duty(STILL, "12.0", 12, "6.40")
    assert "IE-ABV-CLIFF" not in [f.code for f in calc.findings]


def test_the_sparkling_premium_finding_names_the_gap():
    calc = calculate_irish_wine_duty(SPARKLING, "11.0", 12, "5.10")
    finding = next(f for f in calc.findings
                   if f.code == "IE-SPARKLING-PREMIUM")
    assert "€3.18" in finding.detail or "€3.19" in finding.detail


def test_duty_exceeding_the_goods_is_flagged():
    calc = calculate_irish_wine_duty(STILL, "13.0", 12, "2.60")
    assert calc.duty_cents > calc.duty_suspended_cents
    assert "IE-DUTY-EXCEEDS-GOODS" in [f.code for f in calc.findings]


def test_the_calculation_is_deterministic():
    first = calculate_irish_wine_duty(STILL, "13.0", 12, "5.20")
    second = calculate_irish_wine_duty(STILL, "13.0", 12, "5.20")
    assert first.rows() == second.rows()
    assert [f.code for f in first.findings] == [f.code for f in second.findings]


# ---------------------------------------------------------------------------
# Tier pricing
# ---------------------------------------------------------------------------


def test_three_account_types_exist():
    assert CUSTOMER_TYPES == (TIER_INDEPENDENT, TIER_RESTAURANT, TIER_CHAIN)
    assert len(CUSTOMER_TIERS) == 3


def test_the_independent_merchant_pays_list():
    price = get_customer_tier_price("IWD-520", TIER_INDEPENDENT)
    assert price.discount_cents == 0
    assert price.applied_price_cents == price.base_price_cents


def test_each_tier_discount_is_applied_to_the_base_price():
    for tier in CUSTOMER_TIERS:
        price = get_customer_tier_price("IWD-101", tier.name)
        expected = round(price.base_price_cents * float(tier.discount_off_list))
        assert price.discount_cents == pytest.approx(expected, abs=1)
        assert price.applied_price_cents == (price.base_price_cents
                                             - price.discount_cents)


def test_the_discounts_are_ordered_as_the_tiers_are():
    prices = [get_customer_tier_price("IWD-101", t).applied_price_cents
              for t in CUSTOMER_TYPES]
    assert prices == sorted(prices, reverse=True)


def test_an_override_base_price_is_honoured():
    price = get_customer_tier_price("IWD-101", TIER_CHAIN, "20.00")
    assert price.base_price_cents == 2000
    assert price.applied_price_cents == 1600


def test_an_unknown_product_is_refused():
    with pytest.raises(ValueError, match="unknown product"):
        get_customer_tier_price("IWD-999", TIER_CHAIN)


def test_an_unknown_account_type_is_refused():
    with pytest.raises(ValueError, match="customer_type must be one of"):
        get_customer_tier_price("IWD-101", "Wholesaler")


def test_a_negative_base_price_is_refused():
    with pytest.raises(ValueError, match="cannot be negative"):
        get_customer_tier_price("IWD-101", TIER_CHAIN, "-5.00")


def test_a_discount_takes_more_margin_than_its_headline():
    """The commercial point of the whole tab. Duty does not discount."""
    price = get_customer_tier_price("IWD-520", TIER_CHAIN)
    assert price.headline_discount == Decimal("0.20")
    assert price.margin_erosion > price.headline_discount


def test_the_twenty_percent_chain_discount_takes_most_of_the_margin():
    price = get_customer_tier_price("IWD-520", TIER_CHAIN)
    assert price.margin_erosion > Decimal("0.7")


def test_the_erosion_is_worse_on_a_cheaper_wine():
    """More of a cheap bottle's price is fixed cost, so less survives."""
    cheap = get_customer_tier_price("IWD-520", TIER_CHAIN).margin_erosion
    dear = get_customer_tier_price("IWD-415", TIER_CHAIN).margin_erosion
    assert cheap > dear


def test_erosion_is_zero_where_there_is_no_discount():
    assert get_customer_tier_price("IWD-101",
                                   TIER_INDEPENDENT).margin_erosion == 0


def test_contribution_is_price_less_duty_less_cost():
    price = get_customer_tier_price("IWD-204", TIER_RESTAURANT)
    product = PRODUCT_BY_ID["IWD-204"]
    assert price.contribution_cents == (price.applied_price_cents
                                        - price.duty_per_bottle_cents
                                        - product.cost_cents)


def test_every_catalogue_product_prices_at_every_tier():
    for product in CATALOGUE:
        for tier in CUSTOMER_TYPES:
            price = get_customer_tier_price(product.product_id, tier)
            assert price.applied_price_cents > 0


def test_every_tier_states_terms_and_a_minimum():
    for tier in CUSTOMER_TIERS:
        assert tier.minimum_cases >= 1
        assert tier.payment_days > 0
        assert tier.note.strip()


def test_the_chain_is_cheapest_to_serve_and_slowest_to_pay():
    chain = TIER_BY_NAME[TIER_CHAIN]
    assert chain.service_cost_cents == min(
        t.service_cost_cents for t in CUSTOMER_TIERS)
    assert chain.payment_days == max(t.payment_days for t in CUSTOMER_TIERS)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_a_picking_slip_names_the_order_and_every_line():
    payload = generate_3pl_dispatch_payload("SO-24118", WAREHOUSE_BONDED)
    assert "SO-24118" in payload.picking_slip
    assert "Blackrock Cellars" in payload.picking_slip
    for line in SAMPLE_ORDERS[0].lines:
        assert line.product_id in payload.picking_slip


def test_the_slip_totals_match_the_order():
    payload = generate_3pl_dispatch_payload("SO-24120", WAREHOUSE_BONDED)
    order = next(o for o in SAMPLE_ORDERS if o.order_id == "SO-24120")
    assert payload.total_cases == sum(l.cases for l in order.lines)
    assert payload.total_bottles == sum(l.cases * l.bottles_per_case
                                        for l in order.lines)


def test_bonded_release_creates_a_duty_liability():
    payload = generate_3pl_dispatch_payload("SO-24120", WAREHOUSE_BONDED)
    finding = next(f for f in payload.findings if f.code == "IE-BOND-RELEASE")
    assert finding.severity == SEVERITY_CRIT
    assert payload.duty_cents > 0


def test_duty_paid_dispatch_creates_no_fresh_liability():
    payload = generate_3pl_dispatch_payload("SO-24120", WAREHOUSE_DUTY_PAID)
    codes = [f.code for f in payload.findings]
    assert "IE-DUTY-PAID" in codes
    assert "IE-BOND-RELEASE" not in codes


def test_the_duty_on_a_consignment_is_the_same_whatever_the_warehouse():
    """The amount does not change. When it is payable does."""
    bonded = generate_3pl_dispatch_payload("SO-24119", WAREHOUSE_BONDED)
    paid = generate_3pl_dispatch_payload("SO-24119", WAREHOUSE_DUTY_PAID)
    assert bonded.duty_cents == paid.duty_cents
    assert bonded.duty_status != paid.duty_status


def test_bonded_instructions_require_a_movement_reference():
    payload = generate_3pl_dispatch_payload("SO-24118", WAREHOUSE_BONDED)
    joined = " ".join(payload.instructions)
    assert "movement reference" in joined
    assert "payable on release" in joined


def test_duty_paid_instructions_do_not_ask_for_a_movement_document():
    payload = generate_3pl_dispatch_payload("SO-24118", WAREHOUSE_DUTY_PAID)
    joined = " ".join(payload.instructions)
    assert "movement reference" not in joined


def test_a_restaurant_order_carries_a_cellar_delivery_instruction():
    payload = generate_3pl_dispatch_payload("SO-24119", WAREHOUSE_BONDED)
    assert any("cellar" in i for i in payload.instructions)


def test_a_chain_order_carries_a_booking_slot_instruction():
    payload = generate_3pl_dispatch_payload("SO-24120", WAREHOUSE_BONDED)
    assert any("Booking slot" in i for i in payload.instructions)


def test_an_order_below_its_tier_minimum_is_flagged():
    payload = generate_3pl_dispatch_payload("SO-24119", WAREHOUSE_BONDED)
    # Four cases against a three case restaurant minimum: not flagged.
    assert "IE-BELOW-MINIMUM" not in [f.code for f in payload.findings]


def test_every_order_dispatches_from_both_warehouse_types():
    for order in SAMPLE_ORDERS:
        for warehouse in WAREHOUSE_TYPES:
            payload = generate_3pl_dispatch_payload(order.order_id, warehouse)
            assert payload.picking_slip
            assert payload.instructions
            assert payload.findings


def test_an_unknown_order_is_refused():
    with pytest.raises(ValueError, match="unknown order"):
        generate_3pl_dispatch_payload("SO-00000", WAREHOUSE_BONDED)


def test_an_unknown_warehouse_type_is_refused():
    with pytest.raises(ValueError, match="warehouse_type must be one of"):
        generate_3pl_dispatch_payload("SO-24118", "Third party")


def test_the_consignment_duty_is_the_sum_of_its_lines():
    payload = generate_3pl_dispatch_payload("SO-24120", WAREHOUSE_BONDED)
    total = sum(
        int(r["Duty on line"].lstrip("€").replace(",", "").replace(".", ""))
        for r in payload.line_rows())
    assert total == payload.duty_cents


# ---------------------------------------------------------------------------
# Rows reaching the screen must be Arrow safe
# ---------------------------------------------------------------------------


def _assert_all_strings(rows):
    assert rows
    keys = set(rows[0])
    for row in rows:
        assert set(row) == keys, "ragged rows break the Arrow conversion"
        for value in row.values():
            assert isinstance(value, str)


def test_invoice_rows_are_arrow_safe():
    _assert_all_strings(calculate_irish_wine_duty(STILL, "13.0", 12,
                                                  "5.20").rows())


def test_band_rows_are_arrow_safe():
    _assert_all_strings(duty_band_rows())


def test_cliff_rows_are_arrow_safe():
    _assert_all_strings(band_cliffs())


def test_tier_rows_are_arrow_safe():
    _assert_all_strings(tier_rows("IWD-101"))


def test_erosion_rows_are_arrow_safe():
    _assert_all_strings(margin_erosion("IWD-520"))


def test_dispatch_line_rows_are_arrow_safe():
    _assert_all_strings(generate_3pl_dispatch_payload(
        "SO-24120", WAREHOUSE_BONDED).line_rows())


def test_dispatch_summary_rows_are_arrow_safe():
    _assert_all_strings(dispatch_summary_rows())


def test_the_summary_covers_every_order_and_warehouse():
    assert len(dispatch_summary_rows()) == len(SAMPLE_ORDERS) * len(
        WAREHOUSE_TYPES)


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_money_rounds_half_up():
    assert to_cents("0.005") == 1
    assert to_cents("0.015") == 2
    assert to_cents("1.005") == 101


def test_euros_renders_two_places_with_a_thousands_separator():
    assert euros(123456) == "€1,234.56"
    assert euros(0) == "€0.00"


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "irish_wine_distribution_console"
              / "core.py").read_text()
    assert "import streamlit" not in source
    assert "from streamlit" not in source


def test_the_core_is_importable_without_streamlit_installed():
    code = (
        "import sys;"
        "sys.modules['streamlit'] = None;"
        "from tools.irish_wine_distribution_console import core;"
        "print(core.duty_per_bottle_cents('Still', '13.0', 750))"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "319"


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [t.note for t in CUSTOMER_TIERS]
        + [b.label for b in DUTY_BANDS]
        + [f.detail + f.fix + f.title
           for args in ((STILL, "14.8", 12, "2.60"),
                        (SPARKLING, "11.0", 12, "5.10"),
                        (STILL, "12.0", 12, "9.00"))
           for f in calculate_irish_wine_duty(*args).findings]
        + [f.detail + f.fix + f.title
           for order in SAMPLE_ORDERS
           for warehouse in WAREHOUSE_TYPES
           for f in generate_3pl_dispatch_payload(order.order_id,
                                                  warehouse).findings]
        + [i for order in SAMPLE_ORDERS
           for warehouse in WAREHOUSE_TYPES
           for i in generate_3pl_dispatch_payload(order.order_id,
                                                  warehouse).instructions]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools

    tools = all_tools()
    entry = next(t for t in tools if t.key == "irish-wine-distribution-console")
    assert entry.title == "Irish Wine Wholesale Operating Console"
    assert len(entry.tagline) > 30
    icons = [t.icon for t in tools if not t.key.startswith("synthetic-")]
    assert icons.count(entry.icon) == 1
