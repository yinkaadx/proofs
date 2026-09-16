"""Engine tests for the Sports Betting Algorithm and Edge Console.

Written for pytest. Deterministic: nothing in the engine reads the clock or a
random source, so a backtest run twice gives the same answer twice, which is
the minimum a backtest has to do before anybody believes one.

The central tests are the ones that separate a disagreement from an edge. A
model can sit above the de vigged market and still lose money on every bet,
and an engine that reports only the first number is the thing this suite
exists to prevent.

Run: pytest tests/test_sports_betting_algo_console.py -q
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.sports_betting_algo_console.core import (  # noqa: E402
    ALPHA,
    BACKTEST_BETS,
    DEFAULT_KELLY_FRACTION,
    DISCLAIMER,
    ENGINE_VERSION,
    REFEREE_POOL,
    REFEREE_SAMPLES,
    SIG_NOMINAL,
    SIG_NONE,
    SIG_STRONG,
    SPORT_BASKETBALL,
    SPORT_FOOTBALL,
    SPORTS,
    analyze_referee_factor,
    bankroll_curve,
    calculate_ensemble_edge,
    console_summary,
    devig,
    fair_odds,
    implied_probability,
    kelly_fraction,
    kelly_ladder,
    normal_two_tail_p,
    overround,
    referee_rows,
    simulate_kelly_backtest,
)

EVEN = 1.91


# ---------------------------------------------------------------------------
# Odds and the margin
# ---------------------------------------------------------------------------

def test_implied_probability_is_the_reciprocal():
    assert implied_probability(2.0) == pytest.approx(0.5)
    assert implied_probability(EVEN) == pytest.approx(1 / EVEN)


def test_odds_at_or_below_one_are_refused():
    """Decimal odds include the stake, so they are always above 1.0."""
    for bad in (1.0, 0.5, 0.0, -2.0):
        with pytest.raises(ValueError):
            implied_probability(bad)


def test_a_real_two_way_market_sums_to_more_than_one():
    total = overround((EVEN, EVEN))
    assert total > 1.0
    assert total == pytest.approx(2 / EVEN, abs=1e-9)


def test_a_fair_market_sums_to_exactly_one():
    assert overround((2.0, 2.0)) == pytest.approx(1.0)


def test_de_vigging_makes_the_probabilities_sum_to_one():
    for market in ((EVEN, EVEN), (1.5, 2.6), (1.2, 4.5), (3.1, 1.45)):
        assert sum(devig(market)) == pytest.approx(1.0, abs=1e-9)


def test_de_vigging_always_lowers_every_raw_probability():
    """Because the margin is removed from each side proportionally."""
    market = (1.5, 2.6)
    for raw, true in zip((implied_probability(o) for o in market),
                         devig(market)):
        assert true < raw


def test_a_fair_market_is_unchanged_by_de_vigging():
    assert devig((2.0, 2.0)) == pytest.approx([0.5, 0.5])


def test_fair_odds_invert_a_probability():
    assert fair_odds(0.5) == pytest.approx(2.0)
    assert fair_odds(0.25) == pytest.approx(4.0)
    for bad in (0.0, 1.0, -0.2, 1.5):
        with pytest.raises(ValueError):
            fair_odds(bad)


# ---------------------------------------------------------------------------
# The margin illusion, which is what this engine exists for
# ---------------------------------------------------------------------------

def test_a_model_above_the_market_can_still_lose_money():
    """The single most common way a betting dashboard shows green while the
    bankroll falls."""
    # True market on 1.91 each side is 0.500. A model at 0.515 is three
    # percent better and still below the 0.5236 break even.
    edge = calculate_ensemble_edge((0.515,), EVEN, EVEN)
    assert edge.probability_edge_percent > 0
    assert edge.expected_value_percent < 0
    assert edge.margin_illusion is True
    assert edge.profitable is False


def test_break_even_is_one_over_the_odds_not_the_de_vigged_market():
    edge = calculate_ensemble_edge((0.55,), EVEN, EVEN)
    assert edge.break_even_probability == pytest.approx(1 / EVEN, abs=1e-5)
    assert edge.market_probability == pytest.approx(0.5, abs=1e-5)
    assert edge.break_even_probability > edge.market_probability


def test_a_model_exactly_at_break_even_has_no_expected_value():
    """And is still flagged, which took a moment to accept as correct.

    The engine rounds its reported figures for display, so the tolerance is
    that rounding rather than float precision. The second assertion is the
    interesting one. At exactly break even the model sits 4.7 percent above
    the de vigged market and makes nothing at all, which is precisely the
    illusion the flag exists to name. Break even is not profitable, so a
    disagreement that buys nothing is reported as buying nothing.
    """
    edge = calculate_ensemble_edge((1 / EVEN,), EVEN, EVEN)
    assert edge.expected_value_percent == pytest.approx(0.0, abs=0.0001)
    assert edge.probability_edge_percent > 0
    assert not edge.profitable, "break even is not profit"
    assert edge.margin_illusion, (
        "a positive disagreement worth zero money is the illusion itself")


def test_the_hurdle_is_how_far_above_the_market_break_even_sits():
    edge = calculate_ensemble_edge((0.55,), EVEN, EVEN)
    expected = ((edge.break_even_probability - edge.market_probability)
                / edge.market_probability) * 100
    assert edge.margin_hurdle_percent == pytest.approx(expected, abs=0.001)
    assert edge.margin_hurdle_percent > 0


def test_a_model_matching_the_de_vigged_market_exactly_loses_the_margin():
    edge = calculate_ensemble_edge((0.5,), EVEN, EVEN)
    assert edge.probability_edge_percent == pytest.approx(0.0, abs=0.0001)
    assert edge.expected_value_percent < 0
    assert edge.expected_value_percent == pytest.approx(
        (0.5 * EVEN - 1) * 100, abs=0.0001)


def test_a_genuinely_strong_model_clears_the_hurdle():
    edge = calculate_ensemble_edge((0.58, 0.54, 0.61), EVEN, EVEN)
    assert edge.profitable
    assert not edge.margin_illusion
    assert edge.probability_edge_percent > edge.margin_hurdle_percent


def test_expected_value_matches_the_arithmetic_by_hand():
    edge = calculate_ensemble_edge((0.58, 0.54, 0.61), EVEN, EVEN)
    assert edge.expected_value_percent == pytest.approx(
        (edge.model_probability * EVEN - 1) * 100, abs=0.001)


def test_a_wider_margin_raises_the_hurdle():
    tight = calculate_ensemble_edge((0.55,), 1.98, 1.98)
    wide = calculate_ensemble_edge((0.55,), 1.80, 1.80)
    assert wide.margin_hurdle_percent > tight.margin_hurdle_percent
    assert wide.expected_value_percent < tight.expected_value_percent


# ---------------------------------------------------------------------------
# The ensemble
# ---------------------------------------------------------------------------

def test_equal_weights_average_the_submodels():
    edge = calculate_ensemble_edge((0.5, 0.6, 0.7), EVEN, EVEN)
    assert edge.model_probability == pytest.approx(0.6, abs=1e-9)


def test_weights_are_normalised_so_they_need_not_sum_to_one():
    even = calculate_ensemble_edge((0.5, 0.7), EVEN, EVEN, weights=(1, 1))
    scaled = calculate_ensemble_edge((0.5, 0.7), EVEN, EVEN, weights=(5, 5))
    assert even.model_probability == pytest.approx(scaled.model_probability)


def test_weighting_shifts_the_composite_toward_the_heavier_submodel():
    edge = calculate_ensemble_edge((0.5, 0.7), EVEN, EVEN, weights=(1, 3))
    assert edge.model_probability == pytest.approx(0.65, abs=1e-9)


def test_disagreement_is_the_spread_between_submodels():
    edge = calculate_ensemble_edge((0.5, 0.6, 0.7), EVEN, EVEN)
    assert edge.disagreement == pytest.approx(0.2, abs=1e-9)
    assert calculate_ensemble_edge((0.6, 0.6), EVEN, EVEN).disagreement == 0


def test_an_empty_ensemble_is_refused():
    with pytest.raises(ValueError):
        calculate_ensemble_edge((), EVEN, EVEN)


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.4])
def test_an_impossible_submodel_probability_is_refused(bad):
    with pytest.raises(ValueError):
        calculate_ensemble_edge((bad,), EVEN, EVEN)


def test_mismatched_weights_are_refused():
    with pytest.raises(ValueError):
        calculate_ensemble_edge((0.5, 0.6), EVEN, EVEN, weights=(1,))


def test_zero_total_weight_is_refused():
    with pytest.raises(ValueError):
        calculate_ensemble_edge((0.5, 0.6), EVEN, EVEN, weights=(0, 0))


def test_the_edge_rows_are_arrow_safe():
    for row in calculate_ensemble_edge().rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# Referee factor and the multiple comparisons problem
# ---------------------------------------------------------------------------

def test_the_p_value_is_a_real_two_tailed_normal_tail():
    assert normal_two_tail_p(0.0) == pytest.approx(1.0)
    assert normal_two_tail_p(1.96) == pytest.approx(0.05, abs=0.001)
    assert normal_two_tail_p(-1.96) == pytest.approx(0.05, abs=0.001)


def test_searching_more_referees_makes_the_threshold_stricter():
    """Which is the whole point of correcting for the search."""
    few = analyze_referee_factor("K. Anderson", SPORT_FOOTBALL, 1)
    many = analyze_referee_factor("K. Anderson", SPORT_FOOTBALL, 200)
    assert many.bonferroni_alpha < few.bonferroni_alpha
    assert few.p_value == many.p_value, "the data did not change"


def test_a_finding_can_survive_one_test_and_die_under_the_full_search():
    referee = "K. Anderson"
    alone = analyze_referee_factor(referee, SPORT_FOOTBALL, 1)
    searched = analyze_referee_factor(referee, SPORT_FOOTBALL, 500)
    assert alone.significance == SIG_STRONG
    assert searched.significance == SIG_NOMINAL
    assert not searched.significant


def test_the_corrected_threshold_is_alpha_over_the_pool():
    factor = analyze_referee_factor("M. Oliveira", SPORT_FOOTBALL, 30)
    assert factor.bonferroni_alpha == pytest.approx(ALPHA / 30, abs=1e-6)


def test_a_large_sample_with_a_large_deviation_survives_correction():
    factor = analyze_referee_factor("M. Oliveira", SPORT_FOOTBALL,
                                    REFEREE_POOL)
    assert factor.significance == SIG_STRONG
    assert factor.significant
    assert factor.matches > 100


def test_a_referee_below_the_league_mean_gives_a_negative_deviation():
    factor = analyze_referee_factor("S. Petrov", SPORT_BASKETBALL)
    assert factor.foul_deviation < 0
    assert factor.over_bias < 0


def test_the_nominal_band_says_why_it_is_the_least_safe_place_to_stake():
    factor = analyze_referee_factor("K. Anderson", SPORT_FOOTBALL, 500)
    assert "chance" in factor.note
    assert "least safe" in factor.note


def test_an_unknown_sport_or_referee_is_refused():
    with pytest.raises(ValueError):
        analyze_referee_factor("M. Oliveira", "Cricket")
    with pytest.raises(ValueError):
        analyze_referee_factor("Nobody At All", SPORT_FOOTBALL)


def test_a_referee_is_not_found_under_the_wrong_sport():
    with pytest.raises(ValueError):
        analyze_referee_factor("M. Oliveira", SPORT_BASKETBALL)


@pytest.mark.parametrize("sample", REFEREE_SAMPLES)
def test_every_sample_analyses_without_error(sample):
    factor = analyze_referee_factor(sample.name, sample.sport)
    assert factor.significance in (SIG_STRONG, SIG_NOMINAL, SIG_NONE)
    assert 0 <= factor.p_value <= 1
    for row in factor.rows():
        assert all(isinstance(v, str) for v in row.values()), row


def test_the_referee_rows_cover_one_sport_only():
    rows = referee_rows(SPORT_FOOTBALL)
    assert len(rows) == len([s for s in REFEREE_SAMPLES
                             if s.sport == SPORT_FOOTBALL])
    for row in rows:
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# Kelly
# ---------------------------------------------------------------------------

def test_kelly_matches_the_formula_by_hand():
    """f = (bp - q) / b."""
    p, odds = 0.55, EVEN
    b = odds - 1
    expected = (b * p - (1 - p)) / b
    assert kelly_fraction(p, odds) == pytest.approx(expected, abs=1e-9)


def test_kelly_is_zero_rather_than_negative_when_there_is_no_edge():
    """A negative Kelly means bet the other side, and returning it silently is
    how a backtest ends up staking against its own model."""
    assert kelly_fraction(0.40, EVEN) == 0.0
    assert kelly_fraction(0.5, EVEN) == 0.0


def test_kelly_rises_with_the_win_rate():
    fractions = [kelly_fraction(p, EVEN) for p in (0.52, 0.55, 0.60, 0.70)]
    assert fractions == sorted(fractions)


def test_kelly_at_even_money_is_the_classic_two_p_minus_one():
    assert kelly_fraction(0.60, 2.0) == pytest.approx(0.2, abs=1e-9)


def test_an_impossible_win_probability_or_odds_is_refused():
    with pytest.raises(ValueError):
        kelly_fraction(1.4, EVEN)
    with pytest.raises(ValueError):
        kelly_fraction(0.55, 1.0)


def test_fractional_kelly_stakes_the_stated_share_of_full_kelly():
    run = simulate_kelly_backtest(3.0, 0.55, 10_000.0, EVEN, 0.25)
    assert run.staked_fraction == pytest.approx(run.full_kelly * 0.25,
                                                abs=1e-6)


def test_a_smaller_fraction_lowers_both_return_and_drawdown():
    big = simulate_kelly_backtest(3.0, 0.55, 10_000.0, EVEN, 1.0)
    small = simulate_kelly_backtest(3.0, 0.55, 10_000.0, EVEN, 0.125)
    assert small.roi_percent < big.roi_percent
    assert small.worst_case_drawdown_percent < big.worst_case_drawdown_percent


def test_the_worst_case_drawdown_is_never_smaller_than_the_observed_one():
    """The bettor has to survive the bad ordering, not the average one."""
    for fraction in (0.125, 0.25, 0.5, 1.0):
        run = simulate_kelly_backtest(3.0, 0.55, 10_000.0, EVEN, fraction)
        assert run.worst_case_drawdown_percent >= run.max_drawdown_percent


def test_a_losing_win_rate_stakes_nothing_at_all():
    run = simulate_kelly_backtest(-2.0, 0.45, 10_000.0, EVEN)
    assert run.full_kelly == 0.0
    assert run.staked_fraction == 0.0
    assert run.final_bankroll == run.bankroll
    assert run.roi_percent == 0.0
    assert "no edge" in run.ruin_risk


def test_a_winning_model_grows_the_bankroll():
    run = simulate_kelly_backtest(3.0, 0.60, 10_000.0, EVEN)
    assert run.profitable
    assert run.final_bankroll > 10_000.0


def test_the_backtest_is_reproducible():
    first = simulate_kelly_backtest(3.0, 0.55, 10_000.0, EVEN)
    second = simulate_kelly_backtest(3.0, 0.55, 10_000.0, EVEN)
    assert first.final_bankroll == second.final_bankroll
    assert first.max_drawdown_percent == second.max_drawdown_percent


def test_the_win_count_matches_the_requested_rate():
    run = simulate_kelly_backtest(3.0, 0.55, 10_000.0, EVEN, bets=100)
    assert run.wins == 55
    assert run.bets == 100


def test_invalid_backtest_inputs_are_refused():
    with pytest.raises(ValueError):
        simulate_kelly_backtest(3.0, 1.4, 10_000.0)
    with pytest.raises(ValueError):
        simulate_kelly_backtest(3.0, 0.55, 0.0)
    with pytest.raises(ValueError):
        simulate_kelly_backtest(3.0, 0.55, 10_000.0, EVEN, 0.0)
    with pytest.raises(ValueError):
        simulate_kelly_backtest(3.0, 0.55, 10_000.0, EVEN, 1.5)


def test_full_kelly_carries_a_drawdown_worth_warning_about():
    run = simulate_kelly_backtest(3.0, 0.55, 10_000.0, EVEN, 1.0)
    assert run.worst_case_drawdown_percent > 30
    assert run.ruin_risk != "Contained at this fraction"


def test_the_bankroll_curve_agrees_with_the_headline_figure():
    """The curve and the summary cannot be allowed to disagree."""
    run = simulate_kelly_backtest(3.0, 0.55, 10_000.0, EVEN, 0.25)
    curve = bankroll_curve(0.55, EVEN, 10_000.0, 0.25)
    assert len(curve) == run.bets + 1
    assert curve[0] == 10_000.0
    assert curve[-1] == pytest.approx(run.final_bankroll, abs=0.01)


def test_the_ladder_shows_aggression_costing_drawdown():
    rows = kelly_ladder(0.55, EVEN, 10_000.0)
    assert len(rows) == 4
    worst = [float(row["Worst case drawdown"].rstrip("%")) for row in rows]
    assert worst == sorted(worst, reverse=True)
    for row in rows:
        assert all(isinstance(v, str) for v in row.values()), row


def test_the_backtest_rows_are_arrow_safe():
    for row in simulate_kelly_backtest().rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# Summary and house rules
# ---------------------------------------------------------------------------

def test_the_summary_agrees_with_the_parts_it_counts():
    summary = console_summary()
    edge = calculate_ensemble_edge()
    assert summary["overround"] == edge.overround
    assert summary["expected_value"] == edge.expected_value_percent
    assert summary["referees"] == len(REFEREE_SAMPLES)
    assert summary["corrected_alpha"] == pytest.approx(ALPHA / REFEREE_POOL,
                                                       abs=1e-6)


def test_it_never_presents_itself_as_betting_advice():
    assert "Not betting advice" in DISCLAIMER
    assert "no guarantee" in DISCLAIMER


def test_the_defaults_are_fractional_rather_than_full_kelly():
    assert 0 < DEFAULT_KELLY_FRACTION < 1
    assert simulate_kelly_backtest().kelly_fraction_used == DEFAULT_KELLY_FRACTION


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"
    assert BACKTEST_BETS == 100


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "sports_betting_algo_console"
              / "core.py").read_text()
    assert "streamlit" not in source


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [DISCLAIMER, simulate_kelly_backtest().ruin_risk]
        + [analyze_referee_factor(s.name, s.sport).note
           for s in REFEREE_SAMPLES])
    assert "—" not in text
    assert "–" not in text
