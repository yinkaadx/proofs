"""Tests for the CAT Psychometric and IRT Audit Console.

The two things worth proving are the two closed forms, and they are proved
against brute force rather than against themselves. The maximum information
formula is checked by searching the actual information curve on a fine grid
for every combination of discrimination and guessing, so a mistyped exponent
in the 3PL result cannot pass. The EAP posterior is checked against the
precision weighted average computed independently, and against the analytic
consequence that bias is exactly one minus the data weight times the distance
from the prior mean.

Pass marker: pytest reports all tests passed, exit 0.
Fail marker: any failure or error line, exit non zero.
"""

from __future__ import annotations

import math
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.cat_psychometric_audit_console.core import (  # noqa: E402
    D_SCALING,
    ENGINE_VERSION,
    EXPOSURE_CAP,
    INFORMATION_PER_ITEM,
    MODEL_1PL,
    MODEL_2PL,
    MODEL_3PL,
    MODELS,
    RISK_CERTAIN,
    RISK_LOW,
    UTILISATION_CEILING,
    UTILISATION_UNCONTROLLED,
    calculate_information_bounds,
    item_information,
    max_item_information,
    probability_correct,
    simulate_bank_exhaustion,
    simulate_eap_shrinkage,
    theta_of_max_information,
)


def _brute_force_peak(a: float, c: float, span: float = 8.0,
                      steps: int = 200_001) -> tuple[float, float]:
    """The maximum of the real information curve, found by looking at it."""
    best_theta, best_info = -span, -1.0
    width = 2 * span / (steps - 1)
    for index in range(steps):
        theta = -span + index * width
        info = item_information(theta, a, 0.0, c)
        if info > best_info:
            best_info, best_theta = info, theta
    return best_theta, best_info


# ---------------------------------------------------------------------------
# The response function
# ---------------------------------------------------------------------------


def test_the_response_function_has_the_shape_the_model_promises():
    assert probability_correct(0.0, 1.0, 0.0, 0.0) == pytest.approx(0.5)
    assert probability_correct(-9.0, 1.0, 0.0, 0.25) == pytest.approx(0.25, abs=1e-6)
    assert probability_correct(9.0, 1.0, 0.0, 0.25) == pytest.approx(1.0, abs=1e-6)
    assert probability_correct(0.0, 1.0, 0.0, 0.20) == pytest.approx(0.60)
    rising = [probability_correct(t / 4, 1.2, 0.0, 0.1) for t in range(-12, 13)]
    assert rising == sorted(rising), "the curve must be monotonic in ability"


# ---------------------------------------------------------------------------
# Fisher information
# ---------------------------------------------------------------------------


def test_the_two_parameter_maximum_is_d_squared_a_squared_over_four():
    for a in (0.4, 0.8, 1.0, 1.5, 2.2):
        expected = (D_SCALING ** 2) * (a ** 2) / 4
        assert max_item_information(MODEL_2PL, a) == pytest.approx(expected)
    assert max_item_information(MODEL_1PL, 99.0) == pytest.approx(
        (D_SCALING ** 2) / 4), "the 1PL fixes discrimination at one"


def test_the_three_parameter_formula_collapses_to_the_two_parameter_one():
    for a in (0.5, 1.0, 1.8):
        assert max_item_information(MODEL_3PL, a, 0.0) == pytest.approx(
            max_item_information(MODEL_2PL, a))


def test_the_closed_form_maximum_matches_a_brute_force_search():
    """The formula is checked against the curve it claims to describe, for
    every combination, so a mistyped exponent cannot survive."""
    for a in (0.5, 1.0, 1.6):
        for c in (0.0, 0.10, 0.20, 0.25, 0.33):
            model = MODEL_2PL if c == 0 else MODEL_3PL
            _, searched = _brute_force_peak(a, c)
            closed = max_item_information(model, a, c)
            assert closed == pytest.approx(searched, rel=1e-4), (a, c)


def test_the_peak_location_matches_a_brute_force_search():
    for a in (0.6, 1.0, 1.5):
        for c in (0.0, 0.15, 0.30):
            model = MODEL_2PL if c == 0 else MODEL_3PL
            searched_theta, _ = _brute_force_peak(a, c)
            assert theta_of_max_information(model, a, 0.0, c) == pytest.approx(
                searched_theta, abs=1e-3), (a, c)


def test_guessing_lowers_information_and_pushes_the_peak_up_the_scale():
    """A guesser gets easy items right without telling you anything, so the
    item stops being informative where it used to be."""
    previous_info = max_item_information(MODEL_2PL, 1.0)
    previous_peak = theta_of_max_information(MODEL_2PL, 1.0)
    for c in (0.10, 0.20, 0.25, 0.33):
        info = max_item_information(MODEL_3PL, 1.0, c)
        peak = theta_of_max_information(MODEL_3PL, 1.0, 0.0, c)
        assert info < previous_info, c
        assert peak > previous_peak, c
        previous_info, previous_peak = info, peak


def test_information_is_zero_far_from_the_difficulty():
    assert item_information(-12.0, 1.0, 0.0, 0.0) == pytest.approx(0.0, abs=1e-6)
    assert item_information(12.0, 1.0, 0.0, 0.0) == pytest.approx(0.0, abs=1e-6)


def test_an_impossible_item_raises_rather_than_returning_a_number():
    with pytest.raises(ValueError):
        max_item_information(MODEL_2PL, 0.0)
    with pytest.raises(ValueError):
        max_item_information(MODEL_2PL, -1.0)
    with pytest.raises(ValueError):
        max_item_information(MODEL_3PL, 1.0, 1.0)


# ---------------------------------------------------------------------------
# Information bounds and the standard error floor
# ---------------------------------------------------------------------------


def test_the_standard_error_floor_is_one_over_the_root_of_test_information():
    bounds = calculate_information_bounds(MODEL_2PL, 1.0, 30, 0.20)
    assert bounds.max_test_information == pytest.approx(
        30 * max_item_information(MODEL_2PL, 1.0))
    assert bounds.min_standard_error == pytest.approx(
        1 / math.sqrt(bounds.max_test_information))


def test_the_item_count_a_target_demands_is_derived_not_asserted():
    for a in (0.7, 1.0, 1.4):
        for target in (0.15, 0.20, 0.30):
            bounds = calculate_information_bounds(MODEL_2PL, a, 40, target)
            per_item = max_item_information(MODEL_2PL, a)
            assert bounds.items_needed_for_target == math.ceil(
                1 / (target ** 2 * per_item))
            # The count it names must actually reach the target.
            reached = 1 / math.sqrt(bounds.items_needed_for_target * per_item)
            assert reached <= target + 1e-12, (a, target)
            # And one item fewer must not, or the count is padded.
            if bounds.items_needed_for_target > 1:
                short = 1 / math.sqrt(
                    (bounds.items_needed_for_target - 1) * per_item)
                assert short > target, (a, target)


def test_reachability_agrees_with_the_item_count_it_reports():
    for items in range(5, 61, 5):
        for target in (0.18, 0.22, 0.28):
            bounds = calculate_information_bounds(MODEL_2PL, 1.0, items, target)
            assert bounds.target_reachable == (
                bounds.items_needed_for_target <= items), (items, target)
            assert bounds.target_reachable == (
                bounds.min_standard_error <= target)


def test_a_target_out_of_reach_reports_the_shortfall_and_names_the_levers():
    bounds = calculate_information_bounds(MODEL_2PL, 1.0, 20, 0.15)
    assert not bounds.target_reachable
    assert bounds.shortfall_items == bounds.items_needed_for_target - 20
    assert bounds.shortfall_items > 0
    assert "Raise the item limit" in bounds.fix
    reached = calculate_information_bounds(MODEL_2PL, 1.0, 60, 0.30)
    assert reached.target_reachable
    assert reached.shortfall_items == 0


def test_a_weaker_bank_needs_more_items_for_the_same_target():
    strong = calculate_information_bounds(MODEL_2PL, 1.6, 60, 0.20)
    weak = calculate_information_bounds(MODEL_2PL, 0.6, 60, 0.20)
    assert weak.items_needed_for_target > strong.items_needed_for_target
    assert weak.min_standard_error > strong.min_standard_error


def test_the_three_models_rank_the_way_the_mathematics_says():
    """At the same discrimination, guessing can only cost information."""
    two = calculate_information_bounds(MODEL_2PL, 1.0, 30, 0.20)
    three = calculate_information_bounds(MODEL_3PL, 1.0, 30, 0.20, c_param=0.25)
    assert three.max_item_information < two.max_item_information
    assert three.min_standard_error > two.min_standard_error
    assert three.items_needed_for_target > two.items_needed_for_target


def test_impossible_bounds_inputs_raise():
    with pytest.raises(ValueError):
        calculate_information_bounds("4PL", 1.0, 30, 0.20)
    with pytest.raises(ValueError):
        calculate_information_bounds(MODEL_2PL, 1.0, 0, 0.20)
    with pytest.raises(ValueError):
        calculate_information_bounds(MODEL_2PL, 1.0, 30, 0.0)


# ---------------------------------------------------------------------------
# EAP shrinkage
# ---------------------------------------------------------------------------


def test_the_posterior_mean_is_the_precision_weighted_average():
    estimate = simulate_eap_shrinkage(2.0, 0.0, 1.0, 20)
    info = INFORMATION_PER_ITEM * 20
    prior_precision = 1 / 1.0 ** 2
    expected = (info * 2.0 + prior_precision * 0.0) / (info + prior_precision)
    assert estimate.estimated_theta == pytest.approx(expected)
    assert estimate.test_information == pytest.approx(info)
    assert estimate.posterior_sd == pytest.approx(
        1 / math.sqrt(info + prior_precision))


def test_bias_is_exactly_one_minus_the_data_weight_times_the_distance():
    """The analytic consequence, checked across the grid rather than at one
    convenient point."""
    for theta in (-2.5, -1.0, 0.0, 0.5, 2.0, 3.0):
        for prior_mean in (-1.0, 0.0, 0.75):
            for length in (5, 20, 45):
                estimate = simulate_eap_shrinkage(theta, prior_mean, 1.0, length)
                expected = (1 - estimate.data_weight) * (prior_mean - theta)
                assert estimate.shrinkage_bias == pytest.approx(expected)
                assert estimate.shrinkage_bias_magnitude == pytest.approx(
                    abs(expected))


def test_a_student_on_the_prior_mean_is_not_shrunk_at_all():
    for prior_mean in (-1.5, 0.0, 1.2):
        estimate = simulate_eap_shrinkage(prior_mean, prior_mean, 1.0, 12)
        assert estimate.shrinkage_bias == pytest.approx(0.0, abs=1e-12)
        assert estimate.estimated_theta == pytest.approx(prior_mean)


def test_the_estimate_always_lands_between_the_student_and_the_prior_mean():
    for theta in (-3.0, -0.4, 1.1, 2.8):
        for prior_mean in (-1.0, 0.0, 1.0):
            estimate = simulate_eap_shrinkage(theta, prior_mean, 1.0, 20)
            low, high = sorted((theta, prior_mean))
            assert low - 1e-12 <= estimate.estimated_theta <= high + 1e-12


def test_bias_grows_with_distance_from_the_prior_mean():
    previous = -1.0
    for theta in (0.0, 0.5, 1.0, 2.0, 3.0):
        magnitude = simulate_eap_shrinkage(theta, 0.0, 1.0, 20).shrinkage_bias_magnitude
        assert magnitude > previous, theta
        previous = magnitude


def test_a_longer_test_shrinks_less_and_a_wider_prior_shrinks_less():
    previous = simulate_eap_shrinkage(2.0, 0.0, 1.0, 5)
    for length in (10, 20, 40, 60):
        current = simulate_eap_shrinkage(2.0, 0.0, 1.0, length)
        assert current.shrinkage_bias_magnitude < previous.shrinkage_bias_magnitude
        assert current.data_weight > previous.data_weight
        assert current.posterior_sd < previous.posterior_sd
        previous = current
    tight = simulate_eap_shrinkage(2.0, 0.0, 0.5, 20)
    wide = simulate_eap_shrinkage(2.0, 0.0, 2.0, 20)
    assert wide.shrinkage_bias_magnitude < tight.shrinkage_bias_magnitude


def test_the_data_weight_stays_a_proportion():
    for length in (1, 5, 20, 60):
        for prior_sd in (0.3, 1.0, 2.0):
            weight = simulate_eap_shrinkage(1.0, 0.0, prior_sd, length).data_weight
            assert 0.0 < weight < 1.0, (length, prior_sd)


def test_the_interval_is_symmetric_around_the_estimate():
    estimate = simulate_eap_shrinkage(1.5, 0.0, 1.0, 25)
    assert estimate.ci_high - estimate.estimated_theta == pytest.approx(
        estimate.estimated_theta - estimate.ci_low)
    assert estimate.ci_high - estimate.ci_low == pytest.approx(
        2 * 1.96 * estimate.posterior_sd)
    assert estimate.true_theta_inside_ci == (
        estimate.ci_low <= 1.5 <= estimate.ci_high)


def test_a_far_out_student_on_a_short_test_is_reported_outside_the_interval():
    """The audit finding stated as a test: the shrinkage can be large enough
    that the reported interval does not contain the student at all."""
    estimate = simulate_eap_shrinkage(3.0, 0.0, 0.5, 5)
    assert not estimate.true_theta_inside_ci
    assert estimate.findings


def test_impossible_shrinkage_inputs_raise():
    with pytest.raises(ValueError):
        simulate_eap_shrinkage(1.0, 0.0, 0.0, 20)
    with pytest.raises(ValueError):
        simulate_eap_shrinkage(1.0, 0.0, -1.0, 20)
    with pytest.raises(ValueError):
        simulate_eap_shrinkage(1.0, 0.0, 1.0, 0)


# ---------------------------------------------------------------------------
# Bank exhaustion, a model that states its assumptions
# ---------------------------------------------------------------------------


def test_controls_raise_utilisation_and_never_lower_it():
    base = simulate_bank_exhaustion(300, 30, 3, False, False)
    strat = simulate_bank_exhaustion(300, 30, 3, True, False)
    hetter = simulate_bank_exhaustion(300, 30, 3, False, True)
    both = simulate_bank_exhaustion(300, 30, 3, True, True)
    assert base.utilisation == UTILISATION_UNCONTROLLED
    assert strat.utilisation > base.utilisation
    assert hetter.utilisation > base.utilisation
    assert both.utilisation >= max(strat.utilisation, hetter.utilisation)
    assert both.utilisation <= UTILISATION_CEILING
    for row in (base, strat, hetter, both):
        assert row.effective_pool == pytest.approx(300 * row.utilisation)


def test_a_pool_smaller_than_the_test_runs_out_for_certain():
    forecast = simulate_bank_exhaustion(40, 30, 1, False, False)
    assert forecast.effective_pool < 30
    assert forecast.crash_probability == 1.0
    assert forecast.exhaustion_risk == RISK_CERTAIN
    assert "runs out" in forecast.headline


def test_a_deep_bank_is_low_risk_and_inside_the_exposure_cap():
    forecast = simulate_bank_exhaustion(600, 20, 2, True, True)
    assert forecast.exhaustion_risk == RISK_LOW
    assert forecast.crash_probability == 0.0
    assert forecast.exposure_rate <= EXPOSURE_CAP
    assert not forecast.overexposed


def test_repeat_exposure_rises_with_sittings_and_is_zero_on_the_first():
    first = simulate_bank_exhaustion(400, 25, 1, True, True)
    assert first.duplicate_exposure_rate == pytest.approx(0.0)
    previous = 0.0
    for sittings in (2, 3, 4, 6):
        rate = simulate_bank_exhaustion(400, 25, sittings, True,
                                        True).duplicate_exposure_rate
        assert rate > previous, sittings
        assert 0.0 <= rate <= 1.0
        previous = rate


def test_every_rate_the_model_reports_stays_a_proportion():
    for band in (25, 120, 500):
        for length in (10, 30, 60):
            for sittings in (1, 3, 6):
                row = simulate_bank_exhaustion(band, length, sittings,
                                               False, True)
                assert 0.0 <= row.exposure_rate <= 1.0
                assert 0.0 <= row.duplicate_exposure_rate <= 1.0
                assert 0.0 <= row.crash_probability <= 1.0


def test_the_model_ships_its_assumptions_with_every_answer():
    forecast = simulate_bank_exhaustion(200, 30, 3, False, False)
    assert len(forecast.assumptions) >= 4
    assert any("not measured constants" in line for line in forecast.assumptions)


def test_impossible_bank_inputs_raise():
    with pytest.raises(ValueError):
        simulate_bank_exhaustion(0, 30, 3, False, False)
    with pytest.raises(ValueError):
        simulate_bank_exhaustion(200, 0, 3, False, False)
    with pytest.raises(ValueError):
        simulate_bank_exhaustion(200, 30, 0, False, False)


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"
    assert MODELS == (MODEL_1PL, MODEL_2PL, MODEL_3PL)


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "cat_psychometric_audit_console" / "core.py").read_text()
    assert "import streamlit" not in source
    probe = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.cat_psychometric_audit_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT)
    )
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [calculate_information_bounds(model, 1.0, items, 0.20).headline
         + calculate_information_bounds(model, 1.0, items, 0.20).fix
         for model in MODELS for items in (10, 45)]
        + [simulate_eap_shrinkage(theta, 0.0, 1.0, 15).headline
           + simulate_eap_shrinkage(theta, 0.0, 1.0, 15).fix
           + " ".join(simulate_eap_shrinkage(theta, 0.0, 1.0, 15).findings)
           for theta in (0.0, 2.5, -2.5)]
        + [simulate_bank_exhaustion(band, 30, 3, strat, False).headline
           + simulate_bank_exhaustion(band, 30, 3, strat, False).fix
           + " ".join(simulate_bank_exhaustion(band, 30, 3, strat,
                                               False).assumptions)
           for band in (40, 200, 600) for strat in (True, False)]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools
    tools = all_tools()
    entry = next(t for t in tools if t.key == "cat-psychometric-audit-console")
    assert entry.title == "CAT Psychometric & IRT Audit Console"
    icons = [t.icon for t in tools]
    assert icons.count(entry.icon) == 1, "this icon is already used by another tool"
    assert len(entry.tagline) > 30
