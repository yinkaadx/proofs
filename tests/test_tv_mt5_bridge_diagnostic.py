"""Engine tests for the TradingView MT5 Bridge Diagnostic Console.

Written for pytest. Deterministic throughout: the clock is injected, so every
run reproduces exactly.

Run: pytest tests/test_tv_mt5_bridge_diagnostic.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.tv_mt5_bridge_diagnostic.core import (  # noqa: E402
    BUY,
    CAUSE_BOTH,
    CAUSE_GENUINE,
    CAUSE_HEADLINE,
    CAUSE_LATENCY,
    CAUSE_NONE,
    CAUSE_SPREAD,
    ERR_BAD_ACTION,
    ERR_BAD_NUMBER,
    ERR_ILLOGICAL_LEVELS,
    ERR_INVALID_JSON,
    ERR_MISSING_FIELD,
    ERROR_HEADLINE,
    EURUSD,
    REQUIRED_FIELDS,
    SAMPLE_PAYLOAD,
    SELL,
    XAUUSD,
    AlertParseError,
    Latency,
    TvAlert,
    build_trail,
    diagnose_stop_out,
    parse_alert,
    quote_from_spread,
    simulate_execution,
    symbol_by_name,
    trail_rows,
)

NOW = "2026-09-12T08:30:00Z"

LONG_BODY = {
    "action": "buy", "ticker": "EURUSD", "price": 1.08540,
    "sl": 1.08340, "tp": 1.08940, "contracts": 1.0,
    "strategy": "London breakout v3", "time": NOW,
}
SHORT_BODY = {
    "action": "sell", "ticker": "EURUSD", "price": 1.08540,
    "sl": 1.08740, "tp": 1.08340, "contracts": 1.0,
    "strategy": "London fade", "time": NOW,
}

QUIET = Latency(webhook_ms=120, bridge_ms=30, broker_ms=60)
SLOW = Latency(webhook_ms=420, bridge_ms=95, broker_ms=310)


def body(**overrides) -> str:
    merged = dict(LONG_BODY)
    merged.update(overrides)
    return json.dumps(merged)


def long_alert() -> TvAlert:
    return parse_alert(json.dumps(LONG_BODY), now=NOW)


def short_alert() -> TvAlert:
    return parse_alert(json.dumps(SHORT_BODY), now=NOW)


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

def test_sample_payload_parses_into_the_expected_alert():
    alert = parse_alert(SAMPLE_PAYLOAD, now=NOW)
    assert alert.action == BUY
    assert alert.ticker == "EURUSD"
    assert alert.price == pytest.approx(1.08540)
    assert alert.stop_loss == pytest.approx(1.08340)
    assert alert.take_profit == pytest.approx(1.08940)
    assert alert.is_long is True
    assert alert.risk_price() == pytest.approx(0.00200)


def test_action_is_normalised_to_upper_case():
    assert parse_alert(body(action="  Buy "), now=NOW).action == BUY
    assert parse_alert(json.dumps(SHORT_BODY), now=NOW).action == SELL


def test_ticker_is_normalised_to_upper_case():
    assert parse_alert(body(ticker=" eurusd "), now=NOW).ticker == "EURUSD"


def test_numbers_sent_as_strings_are_accepted():
    """TradingView sends numbers as numbers or as strings depending on how the
    alert message was written. Both have to work."""
    alert = parse_alert(body(price="1.08540", sl="1.08340", tp="1.08940"),
                        now=NOW)
    assert alert.price == pytest.approx(1.08540)
    assert alert.stop_loss == pytest.approx(1.08340)


def test_contracts_defaults_to_one_when_absent():
    payload = {k: v for k, v in LONG_BODY.items() if k != "contracts"}
    assert parse_alert(json.dumps(payload), now=NOW).contracts == 1.0


def test_time_falls_back_to_the_injected_now():
    payload = {k: v for k, v in LONG_BODY.items() if k != "time"}
    assert parse_alert(json.dumps(payload), now=NOW).fired_at == NOW


def test_malformed_json_is_refused_with_invalid_json():
    with pytest.raises(AlertParseError) as caught:
        parse_alert('{"action": "buy", "ticker": "EURUSD",}', now=NOW)
    assert caught.value.code == ERR_INVALID_JSON


def test_a_json_array_is_refused_because_the_bridge_needs_an_object():
    with pytest.raises(AlertParseError) as caught:
        parse_alert("[1, 2, 3]", now=NOW)
    assert caught.value.code == ERR_INVALID_JSON


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_every_required_field_is_enforced(field):
    payload = {k: v for k, v in LONG_BODY.items() if k != field}
    with pytest.raises(AlertParseError) as caught:
        parse_alert(json.dumps(payload), now=NOW)
    assert caught.value.code == ERR_MISSING_FIELD
    assert field in caught.value.message


def test_an_unsubstituted_placeholder_is_refused_as_a_bad_number():
    """The commonest real failure: {{close}} left in the alert message."""
    with pytest.raises(AlertParseError) as caught:
        parse_alert(body(price="{{close}}"), now=NOW)
    assert caught.value.code == ERR_BAD_NUMBER


def test_a_boolean_price_is_refused_rather_than_coerced_to_one():
    with pytest.raises(AlertParseError) as caught:
        parse_alert(body(price=True), now=NOW)
    assert caught.value.code == ERR_BAD_NUMBER


def test_a_nan_price_is_refused():
    with pytest.raises(AlertParseError) as caught:
        parse_alert('{"action":"buy","ticker":"EURUSD","price":NaN,'
                    '"sl":1.0,"tp":1.1}', now=NOW)
    assert caught.value.code == ERR_BAD_NUMBER


def test_an_unknown_action_is_refused():
    with pytest.raises(AlertParseError) as caught:
        parse_alert(body(action="hold"), now=NOW)
    assert caught.value.code == ERR_BAD_ACTION


def test_a_buy_with_its_stop_above_entry_is_refused():
    with pytest.raises(AlertParseError) as caught:
        parse_alert(body(sl=1.08740), now=NOW)
    assert caught.value.code == ERR_ILLOGICAL_LEVELS


def test_a_sell_with_its_stop_below_entry_is_refused():
    payload = dict(SHORT_BODY, sl=1.08340, tp=1.08740)
    with pytest.raises(AlertParseError) as caught:
        parse_alert(json.dumps(payload), now=NOW)
    assert caught.value.code == ERR_ILLOGICAL_LEVELS


def test_every_error_code_has_a_headline():
    for code in (ERR_INVALID_JSON, ERR_MISSING_FIELD, ERR_BAD_NUMBER,
                 ERR_BAD_ACTION, ERR_ILLOGICAL_LEVELS):
        assert ERROR_HEADLINE[code]


def test_symbol_lookup_is_case_insensitive_and_returns_none_when_unknown():
    assert symbol_by_name(" eurusd ") is EURUSD
    assert symbol_by_name("XAUUSD") is XAUUSD
    assert symbol_by_name("NOTATHING") is None


# ---------------------------------------------------------------------------
# Spread injection
# ---------------------------------------------------------------------------

def test_the_quote_is_built_symmetrically_around_the_chart_mid():
    quote = quote_from_spread(1.08540, 12, EURUSD)
    assert quote.bid == pytest.approx(1.08534)
    assert quote.ask == pytest.approx(1.08546)
    assert quote.ask - quote.bid == pytest.approx(0.00012)


def test_a_buy_pays_the_ask_and_a_sell_receives_the_bid():
    quote = quote_from_spread(1.08540, 12, EURUSD)
    assert quote.side_price(BUY) == pytest.approx(quote.ask)
    assert quote.side_price(SELL) == pytest.approx(quote.bid)


def test_a_long_fills_above_the_chart_by_the_half_spread():
    execution = simulate_execution(long_alert(), EURUSD, spread_points=12,
                                   latency=QUIET, market_move_points=0)
    assert execution.fill_price == pytest.approx(1.08546)
    assert execution.spread_points == pytest.approx(12.0)
    assert execution.drift_points == pytest.approx(6.0)
    assert execution.adverse is True


def test_a_short_fills_below_the_chart_by_the_half_spread():
    execution = simulate_execution(short_alert(), EURUSD, spread_points=12,
                                   latency=QUIET, market_move_points=0)
    assert execution.fill_price == pytest.approx(1.08534)
    assert execution.drift_points == pytest.approx(-6.0)
    # Worse for a seller, so the slippage reads positive on both sides.
    assert execution.slippage_points == pytest.approx(6.0)
    assert execution.adverse is True


def test_a_wider_spread_costs_proportionally_more():
    narrow = simulate_execution(long_alert(), EURUSD, spread_points=6,
                                latency=QUIET)
    wide = simulate_execution(long_alert(), EURUSD, spread_points=30,
                              latency=QUIET)
    assert narrow.slippage_points == pytest.approx(3.0)
    assert wide.slippage_points == pytest.approx(15.0)


def test_a_zero_spread_fills_exactly_on_the_chart_price():
    execution = simulate_execution(long_alert(), EURUSD, spread_points=0,
                                   latency=QUIET, market_move_points=0)
    assert execution.fill_price == pytest.approx(1.08540)
    assert execution.slippage_points == pytest.approx(0.0)
    assert execution.adverse is False


def test_a_negative_spread_is_refused_rather_than_simulated():
    with pytest.raises(ValueError):
        simulate_execution(long_alert(), EURUSD, spread_points=-1,
                           latency=QUIET)


def test_the_spread_is_read_in_the_instruments_own_points():
    """XAUUSD has a 0.01 point, so the same point count is a different price."""
    alert = TvAlert(action=BUY, ticker="XAUUSD", price=2380.00, stop_loss=2375.00,
                    take_profit=2390.00, contracts=1.0, strategy="gold",
                    fired_at=NOW)
    execution = simulate_execution(alert, XAUUSD, spread_points=30,
                                   latency=QUIET, market_move_points=0)
    assert execution.fill_price == pytest.approx(2380.15)
    assert execution.spread_points == pytest.approx(30.0)


def test_spread_and_latency_are_reported_as_separate_costs():
    execution = simulate_execution(long_alert(), EURUSD, spread_points=12,
                                   latency=SLOW, market_move_points=8)
    assert EURUSD.to_points(execution.spread_component_price) == pytest.approx(6.0)
    assert EURUSD.to_points(execution.latency_component_price) == pytest.approx(8.0)
    assert execution.drift_points == pytest.approx(14.0)


def test_a_move_in_the_traders_favour_reduces_the_drift():
    execution = simulate_execution(long_alert(), EURUSD, spread_points=12,
                                   latency=SLOW, market_move_points=-8)
    assert execution.drift_points == pytest.approx(-2.0)
    assert execution.adverse is False


# ---------------------------------------------------------------------------
# Latency
# ---------------------------------------------------------------------------

def test_latency_totals_its_three_stages():
    assert SLOW.total_ms == 825
    assert Latency(0, 0, 0).total_ms == 0


def test_the_worst_stage_is_named_so_a_fix_has_a_target():
    assert SLOW.worst_stage == "webhook"
    assert Latency(10, 900, 10).worst_stage == "bridge"
    assert Latency(10, 10, 900).worst_stage == "broker"


def test_latency_shares_sum_to_one_hundred_percent():
    rows = SLOW.rows()
    assert len(rows) == 3
    assert sum(int(row["Share"].rstrip("%")) for row in rows) == 100
    assert sum(row["Milliseconds"] for row in rows) == SLOW.total_ms


@pytest.mark.parametrize("stages", [
    (420, 95, 310), (1, 1, 1), (7, 11, 13), (1000, 1, 1), (333, 333, 334),
    (0, 5, 0), (17, 0, 83),
])
def test_latency_shares_always_total_one_hundred(stages):
    rows = Latency(*stages).rows()
    assert sum(int(row["Share"].rstrip("%")) for row in rows) == 100


def test_latency_rows_survive_a_zero_total_without_dividing_by_zero():
    rows = Latency(0, 0, 0).rows()
    assert all(row["Share"] == "0%" for row in rows)


def test_the_fill_timestamp_is_the_alert_time_plus_the_total_latency():
    execution = simulate_execution(long_alert(), EURUSD, spread_points=12,
                                   latency=SLOW)
    assert execution.executed_at == "2026-09-12T08:30:00.825Z"


def test_a_faster_bridge_fills_earlier():
    quick = simulate_execution(long_alert(), EURUSD, 12, QUIET)
    slow = simulate_execution(long_alert(), EURUSD, 12, SLOW)
    assert quick.executed_at < slow.executed_at


# ---------------------------------------------------------------------------
# Stop out diagnosis
# ---------------------------------------------------------------------------

def diagnosis_for(chart_extreme: float, spread_points: float = 12,
                  market_move_points: float = 0.0, short: bool = False):
    alert = short_alert() if short else long_alert()
    execution = simulate_execution(alert, EURUSD, spread_points, SLOW,
                                   market_move_points=market_move_points)
    return diagnose_stop_out(execution, chart_extreme)


def test_a_chart_that_stayed_clear_of_the_stop_is_not_stopped_out():
    diagnosis = diagnosis_for(1.08400)
    assert diagnosis.stopped_out is False
    assert diagnosis.cause == CAUSE_NONE


def test_the_trigger_sits_half_a_spread_above_the_stop_for_a_long():
    diagnosis = diagnosis_for(1.08400, spread_points=12)
    assert diagnosis.trigger_price == pytest.approx(1.08346)
    assert diagnosis.spread_gap_points == pytest.approx(6.0)


def test_with_no_spread_the_trigger_is_the_stop_itself():
    diagnosis = diagnosis_for(1.08400, spread_points=0)
    assert diagnosis.trigger_price == pytest.approx(1.08340)
    assert diagnosis.spread_gap_points == pytest.approx(0.0)


def test_a_chart_that_never_reached_the_stop_is_blamed_on_the_spread():
    """The headline case: stopped at a price that never printed."""
    diagnosis = diagnosis_for(1.08343, spread_points=12, market_move_points=0)
    assert diagnosis.stopped_out is True
    assert diagnosis.cause == CAUSE_SPREAD
    assert "never reached" in diagnosis.explanation


def test_a_wider_spread_stops_a_trade_the_narrow_one_survives():
    assert diagnosis_for(1.08343, spread_points=2).stopped_out is False
    assert diagnosis_for(1.08343, spread_points=12).stopped_out is True


def test_a_chart_that_reached_the_stop_is_a_genuine_stop_out():
    diagnosis = diagnosis_for(1.08330, spread_points=12, market_move_points=0)
    assert diagnosis.stopped_out is True
    assert diagnosis.cause == CAUSE_GENUINE
    assert "made no difference" in diagnosis.explanation


def test_a_genuine_stop_out_with_adverse_latency_is_attributed_to_latency():
    diagnosis = diagnosis_for(1.08330, spread_points=12, market_move_points=8)
    assert diagnosis.cause == CAUSE_LATENCY
    assert diagnosis.latency_cost_points == pytest.approx(8.0)


def test_latency_larger_than_the_spread_gap_is_named_alongside_it():
    diagnosis = diagnosis_for(1.08343, spread_points=12, market_move_points=8)
    assert diagnosis.cause == CAUSE_BOTH
    assert diagnosis.latency_cost_points >= diagnosis.spread_gap_points


def test_latency_smaller_than_the_spread_gap_leaves_the_spread_to_blame():
    diagnosis = diagnosis_for(1.08343, spread_points=20, market_move_points=3)
    assert diagnosis.cause == CAUSE_SPREAD


def test_a_short_is_stopped_on_the_ask_so_the_trigger_sits_below_its_stop():
    diagnosis = diagnosis_for(1.08737, spread_points=12, short=True)
    assert diagnosis.trigger_price == pytest.approx(1.08734)
    assert diagnosis.stopped_out is True
    assert diagnosis.cause == CAUSE_SPREAD


def test_a_short_clear_of_its_trigger_is_not_stopped_out():
    diagnosis = diagnosis_for(1.08700, spread_points=12, short=True)
    assert diagnosis.stopped_out is False


def test_every_cause_has_a_headline_and_the_diagnosis_carries_it():
    for cause in (CAUSE_SPREAD, CAUSE_LATENCY, CAUSE_BOTH, CAUSE_GENUINE,
                  CAUSE_NONE):
        assert CAUSE_HEADLINE[cause]
    diagnosis = diagnosis_for(1.08343)
    assert diagnosis.headline == CAUSE_HEADLINE[diagnosis.cause]


def test_the_verdict_table_shows_the_arithmetic_rather_than_asserting_it():
    diagnosis = diagnosis_for(1.08343)
    measures = [row["Measure"] for row in diagnosis.verdict_rows]
    assert "Chart price that actually triggers the stop" in measures
    assert "Half spread inside the chart" in measures
    assert "Latency in flight" in measures


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def test_the_trail_runs_from_the_alert_to_the_fill_in_order():
    execution = simulate_execution(long_alert(), EURUSD, 12, SLOW)
    trail = build_trail(execution)
    assert [e.sequence for e in trail] == [1, 2, 3, 4]
    assert [e.elapsed_ms for e in trail] == [0, 420, 515, 825]
    assert trail[0].timestamp == "2026-09-12T08:30:00.000Z"
    assert trail[-1].timestamp == execution.executed_at


def test_the_trail_gains_a_verdict_entry_once_a_diagnosis_exists():
    execution = simulate_execution(long_alert(), EURUSD, 12, SLOW)
    diagnosis = diagnose_stop_out(execution, 1.08343)
    trail = build_trail(execution, diagnosis)
    assert len(trail) == 5
    assert diagnosis.headline in trail[-1].detail


def test_trail_rows_hold_one_type_per_column_so_arrow_can_serialise_them():
    """An Arrow table rejects a mixed column. AppTest does not catch this; a
    real browser does, which is how it was found the first time."""
    rows = trail_rows(build_trail(simulate_execution(long_alert(), EURUSD, 12,
                                                     SLOW)))
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"column {column!r} mixes {sorted(kinds)}"


def test_the_trail_names_the_symbol_the_side_and_the_fill():
    execution = simulate_execution(long_alert(), EURUSD, 12, SLOW)
    detail = " ".join(e.detail for e in build_trail(execution))
    assert "EURUSD" in detail
    assert BUY in detail
    assert "1.08546" in detail


# ---------------------------------------------------------------------------
# Prose discipline
# ---------------------------------------------------------------------------

def test_no_dash_characters_in_any_user_facing_prose():
    execution = simulate_execution(long_alert(), EURUSD, 12, SLOW,
                                   market_move_points=8)
    prose = "\n".join([
        *ERROR_HEADLINE.values(),
        *CAUSE_HEADLINE.values(),
        *(e.detail for e in build_trail(execution,
                                        diagnose_stop_out(execution, 1.08343))),
        diagnose_stop_out(execution, 1.08343).explanation,
        diagnose_stop_out(execution, 1.08400).explanation,
        diagnose_stop_out(execution, 1.08330).explanation,
    ])
    assert "—" not in prose
    assert "–" not in prose
