"""Tests for the Python Algorithmic Trading Console.

Three properties carry this file, and every one of them is attacked rather
than demonstrated, because a trading bug costs money directly and a happy
path test finds none of them.

Position risk must never exceed the hard cap, for any combination of equity,
requested percent and grade. Checked across the grid, including requests ten
times the cap.

The protected floor must never move backward. Checked against sequences built
to break it, including a loss larger than every gain that preceded it, since
a rising equity curve makes any implementation look correct.

Exactly one cell in the whole mode, environment and arm token cross product
may reach a broker. The test enumerates all of it and counts.

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

from tools.python_algo_trading_console.core import (  # noqa: E402
    ADVERSARIAL_DAYS,
    ARM_TOKEN,
    ENGINE_VERSION,
    ENVIRONMENTS,
    ENV_LIVE,
    ENV_PAPER,
    MAX_EXPOSURE_PERCENT,
    MAX_RISK_PERCENT,
    MILESTONE_TIERS,
    MODES,
    MODE_LIVE,
    MODE_PAPER,
    SAMPLE_ORDER,
    SETUP_QUALITIES,
    SEVERITY_CRITICAL,
    STATUS_BLOCKED,
    STATUS_ROUTED_LIVE,
    STATUS_SIMULATED,
    TIER_BY_NAME,
    TIER_NAMES,
    calculate_dynamic_position_size,
    money,
    replay_ratchet,
    route_broker_execution,
    simulate_profit_ratchet,
)

EQUITIES = ("0", "1000", "50000", "250000", "1000000")
REQUESTS = ("0", "0.25", "1", "2", "5", "20", "100")
GRADES = tuple(name for name, _m, _n in SETUP_QUALITIES)


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


def test_no_grade_multiplier_is_above_one():
    """The single property that keeps the risk ceiling a ceiling."""
    for name, multiplier, _note in SETUP_QUALITIES:
        assert Decimal("0") <= multiplier <= Decimal("1"), name


def test_risk_never_exceeds_the_hard_cap_anywhere_on_the_grid():
    for equity, request, grade in combinations(EQUITIES, REQUESTS, GRADES):
        sizing = calculate_dynamic_position_size(equity, request, grade)
        assert sizing.within_hard_cap, (equity, request, grade)
        ceiling = money(Decimal(equity) * MAX_RISK_PERCENT / 100)
        assert sizing.risk_amount <= ceiling, (equity, request, grade)
        assert sizing.effective_risk_percent <= MAX_RISK_PERCENT, (
            equity, request, grade)


def test_a_request_above_the_cap_is_clamped_and_reported_as_critical():
    sizing = calculate_dynamic_position_size("50000", "25", "A plus")
    assert sizing.risk_was_clamped
    assert sizing.applied_risk_percent == MAX_RISK_PERCENT
    assert sizing.applied_risk_percent < sizing.requested_risk_percent
    assert sizing.severity == SEVERITY_CRITICAL
    assert any(f.code == "SIZE-CLAMPED" for f in sizing.findings)


def test_a_request_inside_the_cap_is_honoured_exactly():
    sizing = calculate_dynamic_position_size("50000", "1", "A plus")
    assert not sizing.risk_was_clamped
    assert sizing.applied_risk_percent == Decimal("1")
    assert sizing.risk_amount == money("500")


def test_the_grade_only_ever_scales_the_size_down():
    full = calculate_dynamic_position_size("100000", "1", "A plus")
    for name, multiplier, _note in SETUP_QUALITIES:
        row = calculate_dynamic_position_size("100000", "1", name)
        assert row.risk_amount <= full.risk_amount, name
        assert row.risk_amount == money(full.base_risk_amount * multiplier), name


def test_the_grades_are_ordered_and_no_trade_sizes_to_zero():
    sizes = [calculate_dynamic_position_size("100000", "1", name).risk_amount
             for name, _m, _n in SETUP_QUALITIES]
    assert sizes == sorted(sizes, reverse=True)
    no_trade = calculate_dynamic_position_size("100000", "1", "No trade")
    assert no_trade.risk_amount == money(0)
    assert not no_trade.tradeable


def test_sizing_is_monotonic_in_equity():
    previous = money("-1")
    for equity in ("1000", "10000", "50000", "500000"):
        row = calculate_dynamic_position_size(equity, "1", "A")
        assert row.risk_amount > previous, equity
        previous = row.risk_amount


def test_a_dead_account_sizes_nothing_and_says_so():
    for equity in ("0", "-500"):
        row = calculate_dynamic_position_size(equity, "1", "A plus")
        assert row.risk_amount == money(0), equity
        assert not row.tradeable, equity
        assert row.within_hard_cap, equity
        assert any(f.code == "SIZE-NO-EQUITY" for f in row.findings), equity


def test_zero_requested_risk_sizes_nothing():
    row = calculate_dynamic_position_size("50000", "0", "A plus")
    assert row.risk_amount == money(0)
    assert not row.tradeable


def test_the_exposure_cap_is_separate_from_the_risk_cap():
    row = calculate_dynamic_position_size("50000", "1", "A plus")
    assert row.max_exposure_amount == money(
        Decimal("50000") * MAX_EXPOSURE_PERCENT / 100)
    assert row.max_exposure_amount > row.risk_amount
    assert any(f.code == "SIZE-EXPOSURE" for f in row.findings)


def test_every_sizing_says_it_is_not_advice():
    for grade in GRADES:
        row = calculate_dynamic_position_size("50000", "1", grade)
        assert any(f.code == "SIZE-NOT-ADVICE" for f in row.findings), grade


def test_impossible_sizing_inputs_raise():
    with pytest.raises(ValueError):
        calculate_dynamic_position_size("50000", "1", "A plus plus")
    with pytest.raises(ValueError):
        calculate_dynamic_position_size("50000", "-1", "A")


# ---------------------------------------------------------------------------
# The profit ratchet
# ---------------------------------------------------------------------------


def test_no_lock_ratio_reaches_one():
    """A floor equal to the balance is a system that cannot trade."""
    for name, _threshold, lock in MILESTONE_TIERS:
        assert Decimal("0") <= lock < Decimal("1"), name


def test_the_floor_never_moves_backward_on_a_single_step():
    for tier in TIER_NAMES:
        for pnl in ("-100000", "-5000", "-1", "0", "1", "5000", "100000"):
            for floor in ("0", "500", "5000", "50000"):
                state = simulate_profit_ratchet(pnl, tier, floor, "2000",
                                                "4000")
                assert state.protected_floor >= money(floor), (tier, pnl, floor)
                assert not state.floor_moved_backward, (tier, pnl, floor)


def test_a_catastrophic_loss_cannot_touch_the_floor():
    """The attack: give back more than was ever made."""
    state = simulate_profit_ratchet("-999999", "Tier 4", "7500", "10000",
                                    "10000")
    assert state.protected_floor == money("7500")
    assert state.cumulative_profit < 0
    assert not state.floor_moved_backward
    assert any(f.code == "RTC-LOSS-SAFE" for f in state.findings)


def test_the_floor_is_monotonic_across_the_adversarial_sequence():
    for tier in TIER_NAMES:
        ledger = replay_ratchet(ADVERSARIAL_DAYS, tier)
        assert ledger["monotonic"], tier
        assert not ledger["ever_moved_backward"], tier
        floors = ledger["floors"]
        assert all(b >= a for a, b in zip(floors, floors[1:])), tier


def test_the_adversarial_sequence_really_does_end_underwater():
    """A sequence that never goes negative would not test anything."""
    ledger = replay_ratchet(ADVERSARIAL_DAYS, "Tier 1")
    assert ledger["final_cumulative"] < 0
    assert ledger["peak"] > 0
    assert ledger["final_floor"] > 0, (
        "the floor survives a run that ends underwater, which is the point")
    assert ledger["final_floor"] > ledger["final_cumulative"]


def test_reversing_the_sequence_still_never_lowers_the_floor():
    reversed_days = tuple(reversed(ADVERSARIAL_DAYS))
    for tier in TIER_NAMES:
        ledger = replay_ratchet(reversed_days, tier)
        assert ledger["monotonic"], tier
        assert not ledger["ever_moved_backward"], tier


def test_a_tier_locks_only_once_the_peak_passes_its_threshold():
    threshold, lock = TIER_BY_NAME["Tier 2"]
    under = simulate_profit_ratchet("0", "Tier 2", "0",
                                    str(threshold - 1), str(threshold - 1))
    assert under.protected_floor == money(0)
    assert any(f.code == "RTC-BELOW-TIER" for f in under.findings)
    at = simulate_profit_ratchet("0", "Tier 2", "0", str(threshold),
                                 str(threshold))
    assert at.protected_floor == money(threshold * lock)
    assert at.floor_moved


def test_the_lock_is_taken_against_the_peak_not_the_current_total():
    """Locking against the running total lets a drawdown lower the lock,
    which is the same bug wearing a different hat."""
    state = simulate_profit_ratchet("-3000", "Tier 3", "0", "6000", "6000")
    threshold, lock = TIER_BY_NAME["Tier 3"]
    assert state.peak_profit == money("6000")
    assert state.cumulative_profit == money("3000")
    assert state.protected_floor == money(Decimal("6000") * lock)
    assert state.protected_floor > state.cumulative_profit


def test_a_higher_tier_locks_more_of_the_same_peak():
    locks = []
    for name, threshold, lock in MILESTONE_TIERS:
        if not lock:
            continue
        state = simulate_profit_ratchet("0", name, "0", "20000", "20000")
        locks.append(state.protected_floor)
    assert locks == sorted(locks)


def test_every_state_says_the_floor_has_to_be_enforced_elsewhere():
    for tier in TIER_NAMES:
        state = simulate_profit_ratchet("100", tier, "0", "5000", "5000")
        assert any(f.code == "RTC-ENFORCE" for f in state.findings), tier


def test_impossible_ratchet_inputs_raise():
    with pytest.raises(ValueError):
        simulate_profit_ratchet("100", "Tier 9")
    with pytest.raises(ValueError):
        simulate_profit_ratchet("100", "Tier 1", "-500")


# ---------------------------------------------------------------------------
# Execution routing
# ---------------------------------------------------------------------------


def test_exactly_one_cell_of_the_whole_cross_product_reaches_a_broker():
    """The crown jewel. Enumerate everything and count."""
    reached = []
    for mode, env, armed in combinations(MODES, ENVIRONMENTS, (False, True)):
        result = route_broker_execution(mode, SAMPLE_ORDER, env,
                                        ARM_TOKEN if armed else "")
        if result.reached_broker:
            reached.append((mode, env, armed))
    assert reached == [(MODE_LIVE, ENV_LIVE, True)], reached
    assert len(reached) == 1


def test_live_mode_in_a_paper_environment_is_always_blocked():
    for armed in (False, True):
        result = route_broker_execution(MODE_LIVE, SAMPLE_ORDER, ENV_PAPER,
                                        ARM_TOKEN if armed else "")
        assert result.status == STATUS_BLOCKED, armed
        assert not result.reached_broker, armed
        assert any(f.code == "EXE-PAPER-ENV" for f in result.findings), armed


def test_live_without_the_arm_token_is_blocked_even_in_a_live_environment():
    result = route_broker_execution(MODE_LIVE, SAMPLE_ORDER, ENV_LIVE, "")
    assert result.status == STATUS_BLOCKED
    assert not result.reached_broker
    assert any(f.code == "EXE-NOT-ARMED" for f in result.findings)
    wrong = route_broker_execution(MODE_LIVE, SAMPLE_ORDER, ENV_LIVE,
                                   "arm-live-execution")
    assert not wrong.reached_broker, "the token comparison must be exact"


def test_paper_mode_never_reaches_a_broker_in_any_environment():
    for env, armed in combinations(ENVIRONMENTS, (False, True)):
        result = route_broker_execution(MODE_PAPER, SAMPLE_ORDER, env,
                                        ARM_TOKEN if armed else "")
        assert result.status == STATUS_SIMULATED, (env, armed)
        assert not result.reached_broker, (env, armed)


def test_an_unknown_mode_or_environment_fails_closed():
    """Failing open on an unrecognised value is how a staging deploy sends
    real orders on its first run."""
    for bad_mode in ("", "  ", "LIVE", "live", "production", "Bogus"):
        result = route_broker_execution(bad_mode, SAMPLE_ORDER, ENV_LIVE,
                                        ARM_TOKEN)
        assert result.status == STATUS_BLOCKED, bad_mode
        assert not result.reached_broker, bad_mode
    for bad_env in ("", "  ", "prod", "LIVE", "staging"):
        result = route_broker_execution(MODE_LIVE, SAMPLE_ORDER, bad_env,
                                        ARM_TOKEN)
        assert result.status == STATUS_BLOCKED, bad_env
        assert not result.reached_broker, bad_env


def test_a_malformed_order_is_blocked_in_both_modes():
    bad_payloads = (
        {},
        {"symbol": "MSFT", "side": "buy"},
        {"symbol": "", "side": "buy", "quantity": "10"},
        {"symbol": "MSFT", "side": "hold", "quantity": "10"},
        {"symbol": "MSFT", "side": "buy", "quantity": "0"},
        {"symbol": "MSFT", "side": "buy", "quantity": "-5"},
        {"symbol": "MSFT", "side": "buy", "quantity": "lots"},
    )
    for payload in bad_payloads:
        for mode, env in combinations(MODES, ENVIRONMENTS):
            result = route_broker_execution(mode, payload, env, ARM_TOKEN)
            assert result.status == STATUS_BLOCKED, (payload, mode, env)
            assert not result.reached_broker, (payload, mode, env)


def test_the_same_validation_runs_in_both_modes():
    """Paper that is more forgiving than live teaches the wrong lesson."""
    bad = {"symbol": "MSFT", "side": "buy", "quantity": "0"}
    paper = route_broker_execution(MODE_PAPER, bad, ENV_PAPER)
    live = route_broker_execution(MODE_LIVE, bad, ENV_LIVE, ARM_TOKEN)
    assert paper.status == live.status == STATUS_BLOCKED
    assert paper.reason == live.reason


def test_a_live_route_is_reported_as_critical_and_audited():
    result = route_broker_execution(MODE_LIVE, SAMPLE_ORDER, ENV_LIVE,
                                    ARM_TOKEN)
    assert result.status == STATUS_ROUTED_LIVE
    assert result.reached_broker
    assert result.severity == SEVERITY_CRITICAL
    assert any(f.code == "EXE-LIVE" for f in result.findings)
    assert "reached_broker=true" in result.audit_line
    assert str(SAMPLE_ORDER["symbol"]) in result.audit_line


def test_every_blocked_route_writes_an_audit_line_saying_so():
    for mode, env, armed in combinations(MODES, ENVIRONMENTS, (False, True)):
        result = route_broker_execution(mode, SAMPLE_ORDER, env,
                                        ARM_TOKEN if armed else "")
        if result.reached_broker:
            continue
        assert "reached_broker=false" in result.audit_line, (mode, env, armed)


def test_paper_mode_inside_a_live_environment_is_noticed():
    result = route_broker_execution(MODE_PAPER, SAMPLE_ORDER, ENV_LIVE)
    assert result.status == STATUS_SIMULATED
    assert any(f.code == "EXE-PAPER-IN-LIVE" for f in result.findings)


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"
    assert MAX_RISK_PERCENT == Decimal("2.0")


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "python_algo_trading_console" / "core.py").read_text()
    assert "import streamlit" not in source
    probe = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.python_algo_trading_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT)
    )
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [note for _n, _m, note in SETUP_QUALITIES]
        + [row.headline + " ".join(f.title + f.detail + f.fix
                                   for f in row.findings)
           for grade in GRADES
           for row in (calculate_dynamic_position_size("50000", "5", grade),)]
        + [state.headline + " ".join(f.title + f.detail + f.fix
                                     for f in state.findings)
           for tier in TIER_NAMES
           for state in (simulate_profit_ratchet("-4500", tier, "1500",
                                                 "3000", "3000"),)]
        + [result.reason + " ".join(f.title + f.detail + f.fix
                                    for f in result.findings)
           for mode, env, armed in combinations(MODES, ENVIRONMENTS,
                                                (False, True))
           for result in (route_broker_execution(
               mode, SAMPLE_ORDER, env, ARM_TOKEN if armed else ""),)]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools
    tools = all_tools()
    entry = next(t for t in tools if t.key == "python-algo-trading-console")
    assert entry.title == "Python Algorithmic Trading Console"
    icons = [t.icon for t in tools]
    assert icons.count(entry.icon) == 1, "this icon is already used by another tool"
    assert len(entry.tagline) > 30
