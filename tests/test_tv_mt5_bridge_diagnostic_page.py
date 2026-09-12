"""Page tests for the TradingView MT5 Bridge Diagnostic Console, via AppTest.

Written for pytest.

Run: pytest tests/test_tv_mt5_bridge_diagnostic_page.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.tv_mt5_bridge_diagnostic.core import (  # noqa: E402
    CAUSE_HEADLINE,
    CAUSE_SPREAD,
    ERR_BAD_NUMBER,
    ERR_MISSING_FIELD,
)
from tools.tv_mt5_bridge_diagnostic.page import BROKEN_PAYLOADS  # noqa: E402

HARNESS = Path(__file__).resolve().parent / "_page_harness_tv_mt5_bridge_diagnostic.py"

VALID = "Valid alert"
PLACEHOLDER = "Unsubstituted placeholder"
MISSING = "Missing stop loss"


def text_of(at: AppTest) -> str:
    parts: list[str] = []
    for attr in ("markdown", "text", "info", "success", "warning", "error",
                 "caption", "code", "header", "subheader", "title"):
        try:
            parts += [str(getattr(el, "value", "")) for el in getattr(at, attr)]
        except Exception:
            pass
    try:
        for frame in at.get("dataframe"):
            value = getattr(frame, "value", None)
            if value is not None and hasattr(value, "to_numpy"):
                parts += [str(cell) for cell in value.to_numpy().ravel()]
                parts += [str(col) for col in value.columns]
    except Exception:
        pass
    return "\n".join(parts)


def run(timeout: int = 120) -> AppTest:
    at = AppTest.from_file(str(HARNESS), default_timeout=timeout)
    at.run()
    return at


def widget(at: AppTest, kind: str, label: str):
    for el in getattr(at, kind):
        if el.label == label:
            return el
    raise AssertionError(f"no {kind} labelled {label!r}; "
                         f"have {[el.label for el in getattr(at, kind)]}")


def press(at: AppTest, label: str) -> AppTest:
    for button in at.button:
        if button.label == label:
            return button.click().run()
    raise AssertionError(f"no button labelled {label!r}; "
                         f"have {[b.label for b in at.button]}")


def parsed(preset: str = VALID, body: str | None = None) -> AppTest:
    """Take the page as far as a parsed alert, the way a user would."""
    at = run()
    if preset != VALID:
        widget(at, "selectbox", "Preset payload").set_value(preset).run()
    if body is not None:
        widget(at, "text_area", "Webhook body").set_value(body).run()
    return press(at, "Parse payload")


def executed(**overrides) -> AppTest:
    at = parsed()
    for label, value in overrides.items():
        widget(at, "number_input", label).set_value(value).run()
    return press(at, "Execute on MT5")


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------

def test_the_page_renders_with_no_exception():
    at = run()
    assert not at.exception, [str(e.value) for e in at.exception]


def test_the_hero_and_the_four_tabs_are_present():
    at = run()
    body = text_of(at)
    assert "TradingView MT5 Bridge Diagnostic Console" in body
    labels = [tab.label for tab in at.tabs]
    for expected in ("Webhook Payload", "Broker Execution",
                     "Latency and Drift", "Audit Trail"):
        assert expected in labels


def test_nothing_downstream_is_offered_before_a_payload_is_parsed():
    at = run()
    body = text_of(at)
    assert "No alert parsed yet" in body
    assert "Parse a payload on the first tab" in body
    assert [b.label for b in at.button] == ["Parse payload"]


def test_no_two_widgets_of_a_kind_share_a_label():
    at = executed()
    for kind in ("selectbox", "number_input", "text_area", "button"):
        labels = [el.label for el in getattr(at, kind)]
        assert len(labels) == len(set(labels)), f"{kind}: {labels}"


# ---------------------------------------------------------------------------
# Payload tab
# ---------------------------------------------------------------------------

def test_a_valid_payload_parses_and_reports_the_levels():
    at = parsed()
    body = text_of(at)
    assert "Parsed" in body
    assert "1.0854" in body
    assert "London breakout v3" in body


def test_choosing_a_preset_loads_that_payload_into_the_box():
    at = run()
    widget(at, "selectbox", "Preset payload").set_value(MISSING).run()
    assert widget(at, "text_area", "Webhook body").value == BROKEN_PAYLOADS[MISSING]


def test_the_placeholder_preset_is_refused_with_its_own_code():
    at = parsed(PLACEHOLDER)
    body = text_of(at)
    assert ERR_BAD_NUMBER in body
    assert "Nothing was sent to MT5" in body


def test_the_missing_field_preset_names_the_field_it_is_missing():
    at = parsed(MISSING)
    body = text_of(at)
    assert ERR_MISSING_FIELD in body
    assert "sl" in body


def test_every_preset_parses_or_refuses_without_crashing_the_page():
    for preset in BROKEN_PAYLOADS:
        at = parsed(preset)
        assert not at.exception, f"{preset}: {[str(e.value) for e in at.exception]}"


def test_a_refused_payload_offers_no_execution_controls():
    at = parsed(PLACEHOLDER)
    assert "Parse a payload on the first tab" in text_of(at)
    assert "Execute on MT5" not in [b.label for b in at.button]


def test_edited_text_is_parsed_rather_than_the_value_from_the_last_render():
    """A callback that reads its argument instead of live widget state parses
    the previous text. This is that regression, pinned."""
    edited = json.dumps({"action": "sell", "ticker": "XAUUSD", "price": 2380.0,
                         "sl": 2385.0, "tp": 2370.0, "contracts": 2.0,
                         "strategy": "Gold reversal", "time": "2026-09-12T08:30:00Z"})
    body = text_of(parsed(body=edited))
    assert "XAUUSD" in body
    assert "Gold reversal" in body
    assert "London breakout v3" not in body


def test_markup_pasted_into_the_payload_is_escaped_not_executed():
    hostile = json.dumps({"action": "buy", "ticker": "EURUSD", "price": 1.08540,
                          "sl": 1.08340, "tp": 1.08940, "contracts": 1.0,
                          "strategy": "<img src=x onerror=alert(1)>",
                          "time": "2026-09-12T08:30:00Z"})
    body = text_of(parsed(body=hostile))
    assert "<img src=x onerror=alert(1)>" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body


# ---------------------------------------------------------------------------
# Execution tab
# ---------------------------------------------------------------------------

def test_executing_reports_the_fill_the_quote_and_the_drift():
    at = executed()
    body = text_of(at)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "filled at" in body
    assert "spread" in body
    assert "TradingView to bridge" in body


def test_the_fill_moves_with_the_spread():
    narrow = text_of(executed(**{"Spread in points": 2.0,
                                 "Market move while in flight, in points": 0.0}))
    wide = text_of(executed(**{"Spread in points": 40.0,
                               "Market move while in flight, in points": 0.0}))
    assert "1.08541" in narrow
    assert "1.0856" in wide


def test_a_slower_stage_is_named_as_the_one_worth_fixing():
    body = text_of(executed(**{"Bridge processing, ms": 4000}))
    assert "slowest stage is bridge" in body


def test_a_favourable_move_is_not_reported_as_an_adverse_fill():
    body = text_of(executed(**{"Market move while in flight, in points": -40.0}))
    assert "Fill in favour" in body
    assert "Adverse fill" not in body


# ---------------------------------------------------------------------------
# Drift tab and audit trail
# ---------------------------------------------------------------------------

def test_the_diagnosis_explains_a_stop_the_chart_never_reached():
    at = executed(**{"Spread in points": 12.0,
                     "Market move while in flight, in points": 0.0})
    widget(at, "number_input", "Worst price the chart printed").set_value(1.08343).run()
    at = press(at, "Diagnose")
    body = text_of(at)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert CAUSE_HEADLINE[CAUSE_SPREAD] in body
    assert "never reached" in body
    assert "Chart price that actually triggers the stop" in body


def test_the_audit_trail_holds_every_stage_once_an_order_is_executed():
    body = text_of(executed())
    assert "TradingView alert" in body
    assert "Bridge received" in body
    assert "Bridge processed" in body
    assert "MT5 execution" in body


def test_the_trail_is_empty_until_an_order_is_executed():
    assert "The trail is written when an order is executed" in text_of(parsed())


def test_the_kpi_strip_counts_the_milliseconds_in_flight():
    body = text_of(executed(**{"TradingView to bridge, ms": 500,
                               "Bridge processing, ms": 100,
                               "Bridge to MT5 fill, ms": 400}))
    assert "1000" in body
    assert "Milliseconds in flight" in body


@pytest.mark.parametrize("preset", list(BROKEN_PAYLOADS))
def test_no_dash_characters_reach_the_screen(preset):
    body = text_of(parsed(preset))
    assert "—" not in body
    assert "–" not in body
