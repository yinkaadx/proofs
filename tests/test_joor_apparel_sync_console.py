"""Tests for the JOOR, ApparelMagic and Extensiv Sync Console.

The ingestion tests are about refusal, because a wholesale order accepted
short ships short and a short shipment is a chargeback rather than a
backorder. The inventory tests are about the identity that has to hold
when available to sell goes negative, since that is the case a clamp
hides.

The last test in this file measures the shipped colour palette against the
WCAG contrast floor on both themes, per Rule 33.
"""

from __future__ import annotations

import itertools
import pathlib
import re
import subprocess
import sys
from decimal import Decimal

import pytest

from tools.joor_apparel_sync_console.core import (
    ACCEPTED,
    CARRIER_DHL,
    CARRIER_FEDEX,
    CARRIER_UNKNOWN,
    CARRIER_UPS,
    CARRIER_USPS,
    ENGINE_VERSION,
    LINESHEET_HIDE,
    LINESHEET_PUBLISH,
    LINESHEET_PULL,
    REJECTED,
    SAMPLE_MATRIX_BROKEN,
    SAMPLE_MATRIX_CLEAN,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SIZE_SCALES,
    STATE_BLOCKED,
    STATE_COMPLETE,
    STYLE_CATALOGUE,
    build_sku,
    detect_carrier,
    process_fulfillment_tracking,
    reconcile_extensiv_inventory,
    simulate_joor_order_ingestion,
)
from tools.registry import all_tools

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL_DIR = ROOT / "tools" / "joor_apparel_sync_console"

KNIT = "SS26-KNT-014"
DRESS = "SS26-DRS-221"


# ---------------------------------------------------------------------------
# Order ingestion
# ---------------------------------------------------------------------------

def test_a_clean_matrix_maps_every_submitted_unit():
    payload = simulate_joor_order_ingestion("PO-44812", KNIT,
                                            SAMPLE_MATRIX_CLEAN)
    assert payload.status == ACCEPTED
    assert payload.units_submitted == 72
    assert payload.units_mapped == 72
    assert payload.units_rejected == 0
    assert len(payload.lines) == 8


def test_the_order_value_equals_the_sum_of_its_lines():
    payload = simulate_joor_order_ingestion("PO-44812", KNIT,
                                            SAMPLE_MATRIX_CLEAN)
    total = sum((line.extended_price for line in payload.lines),
                Decimal("0.00"))
    assert payload.order_value == total
    assert payload.order_value == Decimal("4896.00")


def test_the_size_and_colour_breakdowns_sum_to_the_mapped_units():
    payload = simulate_joor_order_ingestion("PO-44812", KNIT,
                                            SAMPLE_MATRIX_CLEAN)
    assert sum(payload.size_breakdown.values()) == payload.units_mapped
    assert sum(payload.color_breakdown.values()) == payload.units_mapped


def test_a_size_the_style_does_not_carry_rejects_the_whole_order():
    """An order accepted short ships short, and a short wholesale shipment
    is a chargeback."""
    payload = simulate_joor_order_ingestion("PO-44813", KNIT,
                                            SAMPLE_MATRIX_BROKEN)
    assert payload.status == REJECTED
    assert "XXXL" in payload.unmapped_sizes
    assert "Cobalt" in payload.unmapped_colors


def test_a_rejected_order_emits_no_lines_at_all():
    """Returning the successfully mapped lines beside a rejected status
    invites a consumer to iterate them, which is the partial order this
    stage exists to prevent."""
    payload = simulate_joor_order_ingestion("PO-44813", KNIT,
                                            SAMPLE_MATRIX_BROKEN)
    assert payload.lines == ()
    assert payload.order_value == Decimal("0.00")
    assert payload.units_mapped == 0
    assert payload.units_rejected == payload.units_submitted


def test_units_always_tie_whatever_the_outcome():
    matrices = (SAMPLE_MATRIX_CLEAN, SAMPLE_MATRIX_BROKEN,
                {"Ink": {"S": 0, "M": 0}}, {})
    for matrix in matrices:
        payload = simulate_joor_order_ingestion("PO-1", KNIT, matrix)
        assert payload.units_mapped + payload.units_rejected == \
            payload.units_submitted, matrix


def test_a_numeric_size_is_never_accepted_onto_an_alpha_style():
    """This is position matching's failure dressed as a normal order: the
    alpha run and the numeric run line up for six labels."""
    payload = simulate_joor_order_ingestion(
        "PO-44814", KNIT, {"Ink": {"0": 6, "2": 6, "4": 6}})
    assert payload.status == REJECTED
    assert set(payload.unmapped_sizes) == {"0", "2", "4"}
    assert payload.lines == ()


def test_the_same_labels_are_accepted_on_the_style_that_carries_them():
    """The proof that the refusal above is about the scale and not about
    the labels being unusable anywhere."""
    payload = simulate_joor_order_ingestion(
        "PO-44815", DRESS, {"Bone": {"0": 6, "2": 6, "4": 6}})
    assert payload.status == ACCEPTED
    assert payload.units_mapped == 18


def test_position_matching_would_have_silently_succeeded():
    """Stated explicitly so the label rule cannot be removed without a
    failing test explaining why it existed."""
    alpha = SIZE_SCALES["ALPHA"]
    numeric = SIZE_SCALES["NUMERIC_EVEN"]
    assert len(alpha) <= len(numeric)
    paired = list(zip(alpha, numeric))
    assert paired[3] == ("L", "6")
    assert not set(alpha) & set(numeric)


def test_a_zero_quantity_size_creates_no_line_and_no_rejection():
    payload = simulate_joor_order_ingestion(
        "PO-44816", KNIT, {"Ink": {"S": 0, "M": 12}})
    assert payload.status == ACCEPTED
    assert len(payload.lines) == 1
    assert payload.units_submitted == 12


def test_every_sku_is_unique_within_an_accepted_order():
    payload = simulate_joor_order_ingestion("PO-44812", KNIT,
                                            SAMPLE_MATRIX_CLEAN)
    skus = [line.sku for line in payload.lines]
    assert len(skus) == len(set(skus))
    assert skus[0] == build_sku(KNIT, "OAT", "XS")


def test_every_style_in_the_catalogue_maps_its_own_full_scale():
    for style in STYLE_CATALOGUE:
        colour = style.colors[0][0]
        matrix = {colour: {size: 2 for size in style.sizes}}
        payload = simulate_joor_order_ingestion("PO-X", style.style_code,
                                                matrix)
        assert payload.status == ACCEPTED, style.style_code
        assert payload.units_mapped == 2 * len(style.sizes)


def test_a_missing_po_or_unknown_style_raises():
    with pytest.raises(ValueError):
        simulate_joor_order_ingestion("", KNIT, SAMPLE_MATRIX_CLEAN)
    with pytest.raises(ValueError):
        simulate_joor_order_ingestion("PO-1", "NOT-A-STYLE",
                                      SAMPLE_MATRIX_CLEAN)


def test_a_negative_size_run_raises():
    with pytest.raises(ValueError):
        simulate_joor_order_ingestion("PO-1", KNIT, {"Ink": {"M": -4}})


# ---------------------------------------------------------------------------
# Available to sell
# ---------------------------------------------------------------------------

def test_available_to_sell_is_physical_less_allocated():
    position = reconcile_extensiv_inventory("SKU-1", 140, 96)
    assert position.available_to_sell == 44
    assert position.oversold_units == 0
    assert position.linesheet_status == LINESHEET_PUBLISH


def test_an_exactly_allocated_sku_is_hidden_not_pulled():
    position = reconcile_extensiv_inventory("SKU-2", 32, 32)
    assert position.available_to_sell == 0
    assert position.oversold_units == 0
    assert position.linesheet_status == LINESHEET_HIDE
    assert not position.oversold


def test_an_oversold_sku_reports_the_shortfall_beside_the_clamped_zero():
    """Publishing the clamped zero alone reads as sold out, which reads as
    normal, and nobody investigates until allocation."""
    position = reconcile_extensiv_inventory("SKU-3", 18, 44)
    assert position.available_to_sell == 0
    assert position.oversold_units == 26
    assert position.oversold
    assert position.linesheet_status == LINESHEET_PULL
    assert "INV-OVERSOLD" in {f.code for f in position.findings}
    assert position.severity == SEVERITY_CRITICAL


def test_available_to_sell_is_never_negative_and_never_lost():
    """The identity that has to hold in every cell: an allocated unit is
    either covered by stock or oversold, and there is no third case."""
    for physical, allocated in itertools.product(range(0, 60, 7),
                                                 range(0, 60, 5)):
        position = reconcile_extensiv_inventory("SKU", physical, allocated)
        assert position.available_to_sell >= 0
        assert position.oversold_units >= 0
        covered = min(physical, allocated)
        assert covered + position.oversold_units == allocated
        assert position.available_to_sell == physical - covered


def test_oversold_and_available_are_never_both_positive():
    for physical, allocated in itertools.product(range(0, 40, 3),
                                                 range(0, 40, 3)):
        position = reconcile_extensiv_inventory("SKU", physical, allocated)
        assert not (position.available_to_sell > 0
                    and position.oversold_units > 0)


def test_the_linesheet_status_follows_the_numbers_and_nothing_else():
    for physical, allocated in itertools.product(range(0, 30, 4),
                                                 range(0, 30, 4)):
        position = reconcile_extensiv_inventory("SKU", physical, allocated)
        if position.oversold_units:
            expected = LINESHEET_PULL
        elif position.available_to_sell == 0:
            expected = LINESHEET_HIDE
        else:
            expected = LINESHEET_PUBLISH
        assert position.linesheet_status == expected


def test_every_position_warns_that_the_two_reads_must_share_a_timestamp():
    position = reconcile_extensiv_inventory("SKU-4", 210, 12)
    assert "INV-CLAMP" in {f.code for f in position.findings}


def test_a_missing_sku_or_negative_input_raises():
    with pytest.raises(ValueError):
        reconcile_extensiv_inventory("", 10, 2)
    with pytest.raises(ValueError):
        reconcile_extensiv_inventory("SKU", -1, 2)
    with pytest.raises(ValueError):
        reconcile_extensiv_inventory("SKU", 10, -2)


# ---------------------------------------------------------------------------
# Fulfillment
# ---------------------------------------------------------------------------

def test_each_carrier_shape_is_recognised():
    assert detect_carrier("1Z999AA10123456784") == CARRIER_UPS
    assert detect_carrier("123456789012") == CARRIER_FEDEX
    assert detect_carrier("123456789012345") == CARRIER_FEDEX
    assert detect_carrier("LN123456789US") == CARRIER_USPS
    assert detect_carrier("1234567890") == CARRIER_DHL


def test_junk_in_the_tracking_field_is_not_a_carrier():
    for junk in ("SHIPPED TODAY", "", "   ", "N/A", "1Z", "12345"):
        assert detect_carrier(junk) == CARRIER_UNKNOWN, junk


def test_an_unvalidated_tracking_number_advances_nothing():
    """A completion driven by an unvalidated number closes the order in the
    buyer's portal while the box is still on the bench."""
    result = process_fulfillment_tracking("JO-99120", "SHIPPED TODAY")
    assert result.state == STATE_BLOCKED
    assert not result.completed
    assert all(not stage.reached for stage in result.stages)
    assert result.invoice_reference == ""
    assert "FUL-TRACK" in {f.code for f in result.findings}


def test_a_blocked_order_emits_no_provisional_invoice_reference():
    """A draft number sitting in a completed order field is
    indistinguishable from a real one."""
    for junk in ("", "N/A", "pending", "tbc"):
        result = process_fulfillment_tracking("JO-1", junk)
        assert result.invoice_reference == "", junk


def test_a_valid_number_runs_ship_then_invoice_then_complete():
    result = process_fulfillment_tracking("JO-99120", "1Z999AA10123456784")
    assert result.state == STATE_COMPLETE
    assert [stage.reached for stage in result.stages] == [True, True, True]
    assert result.invoice_reference == "AM-INV-JO-99120"
    assert result.joor_status == "Completed"


def test_no_stage_is_ever_reached_out_of_order():
    """Invoicing before the shipment confirms creates a receivable with
    nothing behind it."""
    numbers = ("1Z999AA10123456784", "123456789012", "LN123456789US",
               "1234567890", "SHIPPED TODAY", "", "1Z")
    for number in numbers:
        reached = [s.reached for s in
                   process_fulfillment_tracking("JO-1", number).stages]
        assert reached == sorted(reached, reverse=True), number


def test_completion_always_implies_an_invoice_and_a_shipment():
    numbers = ("1Z999AA10123456784", "123456789012", "bad", "")
    for number in numbers:
        result = process_fulfillment_tracking("JO-1", number)
        if result.completed:
            assert result.tracking_valid, number
            assert result.invoice_reference, number


def test_whitespace_in_the_tracking_field_is_tolerated():
    result = process_fulfillment_tracking("JO-1", " 1z999aa10123456784 ")
    assert result.carrier == CARRIER_UPS
    assert result.completed


def test_every_result_says_a_matching_shape_is_not_a_real_shipment():
    """Format detection is a filter, not a confirmation."""
    result = process_fulfillment_tracking("JO-1", "1Z999AA10123456784")
    assert "FUL-HEURISTIC" in {f.code for f in result.findings}


def test_a_missing_order_identifier_raises():
    with pytest.raises(ValueError):
        process_fulfillment_tracking("", "1Z999AA10123456784")


# ---------------------------------------------------------------------------
# House invariants
# ---------------------------------------------------------------------------

def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    """The engine has to run outside the app, or it is not an engine."""
    script = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.joor_apparel_sync_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT))
    done = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    for name in ("core.py", "page.py"):
        text = (TOOL_DIR / name).read_text(encoding="utf-8")
        assert "—" not in text, name
        assert "–" not in text, name


def test_the_tool_is_registered_with_a_unique_icon():
    tools = [t for t in all_tools() if not t.key.startswith("synthetic-")]
    keys = [t.key for t in tools]
    assert "joor-apparel-sync-console" in keys
    icons = [t.icon for t in tools]
    assert len(icons) == len(set(icons))
    assert SEVERITY_OK != SEVERITY_CRITICAL


def _relative_luminance(hex_colour: str) -> float:
    raw = hex_colour.lstrip("#")
    channels = [int(raw[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
              for c in channels]
    return (0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2])


def _contrast(foreground: str, background: str) -> float:
    first = _relative_luminance(foreground)
    second = _relative_luminance(background)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def test_every_text_colour_clears_the_contrast_floor_in_both_themes():
    """Rule 33: measured on the palette actually shipped, in light and in
    dark, against both surfaces text is drawn on."""
    theme = (ROOT / "shared" / "theme.py").read_text(encoding="utf-8")
    pairs = re.findall(
        r"--app-(\w+): light-dark\((#[0-9a-fA-F]{6}), (#[0-9a-fA-F]{6})\)",
        theme)
    tokens = {name: (light, dark) for name, light, dark in pairs}
    assert {"ink", "muted", "accent", "crit", "warn", "ok", "card",
            "soft"} <= set(tokens)

    text_tokens = ("ink", "muted", "accent", "crit", "warn", "ok")
    for index, mode in ((0, "light"), (1, "dark")):
        for surface in ("card", "soft"):
            background = tokens[surface][index]
            for name in text_tokens:
                ratio = _contrast(tokens[name][index], background)
                assert ratio >= 4.5, (mode, surface, name, round(ratio, 2))
