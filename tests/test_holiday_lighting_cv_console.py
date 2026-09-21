"""Tests for the Holiday Lighting CV Geometry Console.

The extraction tests check that the rakes are dropped whole rather than
trimmed, because their lower tips sit at eave height and a lowest pixel
filter keeps them. The fitting tests do not assert that RANSAC is better
than least squares, they measure both over the same pixels and compare.
"""

from __future__ import annotations

import math
import pathlib
import subprocess
import sys

import pytest

from tools.holiday_lighting_cv_console.core import (
    EDGE_EAVE,
    EDGE_RAKE,
    EDGE_RIDGE,
    EDGE_VERTICAL,
    ENGINE_VERSION,
    EXTRACTED,
    REFUSED,
    SAMPLE_GABLE,
    SAMPLE_HIP,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    calculate_bulb_spacing,
    classify_edge,
    extract_bottom_eave,
    fit_ransac_line,
    ordinary_least_squares,
    synthesise_eave_pixels,
)
from tools.registry import all_tools

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL_DIR = ROOT / "tools" / "holiday_lighting_cv_console"


# ---------------------------------------------------------------------------
# Eave extraction
# ---------------------------------------------------------------------------

def test_a_gable_mask_yields_the_bottom_edge_and_nothing_else():
    result = extract_bottom_eave(SAMPLE_GABLE)
    assert result.status == EXTRACTED
    assert result.points == ((120.0, 330.0), (680.0, 330.0))
    assert result.span_px == pytest.approx(560.0)


def test_both_rakes_are_dropped_whole_rather_than_trimmed():
    """Their lower tips sit at eave height, so a lowest pixel filter keeps
    them and pulls the fitted line up at both corners."""
    result = extract_bottom_eave(SAMPLE_GABLE)
    assert result.dropped_rake == 2
    peak = min(SAMPLE_GABLE, key=lambda p: p[1])
    assert peak not in result.points
    for tip in ((120.0, 300.0), (680.0, 300.0)):
        assert tip not in result.points


def test_the_wall_edges_are_dropped_as_vertical():
    result = extract_bottom_eave(SAMPLE_GABLE)
    assert result.dropped_vertical == 2


def test_a_hip_roof_mask_yields_its_own_eave_run():
    result = extract_bottom_eave(SAMPLE_HIP)
    assert result.status == EXTRACTED
    assert result.points == ((120.0, 350.0), (680.0, 350.0))


def test_the_extracted_eave_is_the_lowest_run_not_the_ridge():
    """Image coordinates run downward, so the eave carries the larger y."""
    result = extract_bottom_eave(SAMPLE_GABLE)
    eave_y = result.points[0][1]
    assert eave_y == max(p[1] for p in SAMPLE_GABLE)


def test_every_polygon_edge_is_accounted_for():
    result = extract_bottom_eave(SAMPLE_GABLE)
    total = (result.edges_kept + result.dropped_rake + result.dropped_ridge
             + result.dropped_vertical)
    assert result.edges_in == len(SAMPLE_GABLE)
    assert total == result.edges_in


def test_the_edge_classifier_names_each_kind():
    y_peak, y_base = 150.0, 330.0
    assert classify_edge((120.0, 330.0), (680.0, 330.0), y_peak,
                         y_base) == EDGE_EAVE
    assert classify_edge((120.0, 300.0), (400.0, 150.0), y_peak,
                         y_base) == EDGE_RAKE
    assert classify_edge((120.0, 300.0), (120.0, 330.0), y_peak,
                         y_base) == EDGE_VERTICAL
    assert classify_edge((300.0, 155.0), (500.0, 155.0), y_peak,
                         y_base) == EDGE_RIDGE


def test_a_steeply_viewed_mask_is_refused_rather_than_fitted():
    """A confident answer from geometry that is not recoverable is worse
    than a refusal."""
    steep = ((100.0, 400.0), (300.0, 100.0), (320.0, 120.0), (120.0, 420.0))
    result = extract_bottom_eave(steep, slope_tolerance=0.3)
    assert result.status == REFUSED
    assert result.points == ()
    assert "EAV-NONE" in {f.code for f in result.findings}


def test_a_degenerate_polygon_is_refused():
    result = extract_bottom_eave(((10.0, 10.0), (20.0, 20.0)))
    assert result.status == REFUSED
    assert any(f.severity == SEVERITY_CRITICAL for f in result.findings)


def test_a_tilted_eave_is_flagged_as_an_unrectified_camera():
    tilted = ((120.0, 300.0), (400.0, 150.0), (680.0, 300.0),
              (680.0, 390.0), (120.0, 330.0))
    result = extract_bottom_eave(tilted, slope_tolerance=0.3)
    assert result.status == EXTRACTED
    assert result.mean_slope == pytest.approx(60.0 / 560.0, abs=1e-9)
    assert "EAV-TILT" in {f.code for f in result.findings}


def test_a_gently_tilted_eave_below_the_threshold_is_not_flagged():
    """The threshold has to mean something, so a slope under it must not
    fire the warning."""
    gentle = ((120.0, 300.0), (400.0, 150.0), (680.0, 300.0),
              (680.0, 350.0), (120.0, 330.0))
    result = extract_bottom_eave(gentle, slope_tolerance=0.3)
    assert result.status == EXTRACTED
    assert abs(result.mean_slope) < 0.05
    assert "EAV-TILT" not in {f.code for f in result.findings}


def test_the_extraction_always_states_that_pixels_carry_no_scale():
    result = extract_bottom_eave(SAMPLE_GABLE)
    assert "EAV-SCALE" in {f.code for f in result.findings}


# ---------------------------------------------------------------------------
# RANSAC line fitting
# ---------------------------------------------------------------------------

def test_a_clean_run_is_fitted_level():
    pixels = synthesise_eave_pixels(occlusion_severity=0.0)
    fit = fit_ransac_line(pixels, 0.0)
    assert abs(fit.slope) < 0.005
    assert fit.inlier_ratio > 0.9
    assert fit.confidence > 0.9


def test_ransac_beats_least_squares_on_the_same_occluded_pixels():
    """The claim the whole stage rests on, measured rather than asserted.
    The true eave is level, so the correct slope is zero."""
    for severity in (0.4, 0.6, 0.8, 0.9):
        pixels = synthesise_eave_pixels(occlusion_severity=severity)
        fit = fit_ransac_line(pixels, severity)
        ols_slope, _ = ordinary_least_squares(pixels)
        assert abs(fit.slope) < abs(ols_slope), severity
        assert abs(fit.slope) < 0.005, severity


def test_least_squares_is_dragged_far_off_by_the_branch_pixels():
    """Stated explicitly so the comparison above cannot pass by both fits
    being equally good."""
    pixels = synthesise_eave_pixels(occlusion_severity=0.9)
    ols_slope, _ = ordinary_least_squares(pixels)
    assert abs(ols_slope) > 0.02


def test_the_fit_is_deterministic_for_the_same_pixels():
    """A seeded fit, because a roofline that moves between two runs of the
    same photograph cannot be quoted to a customer."""
    pixels = synthesise_eave_pixels(occlusion_severity=0.7)
    first = fit_ransac_line(pixels, 0.7)
    second = fit_ransac_line(pixels, 0.7)
    assert first.slope == second.slope
    assert first.intercept == second.intercept
    assert first.confidence == second.confidence


def test_confidence_falls_as_the_occlusion_grows():
    scores = [fit_ransac_line(
        synthesise_eave_pixels(occlusion_severity=s), s).confidence
        for s in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[-1]


def test_a_bridged_gap_is_named_as_an_assumption_not_a_measurement():
    """A bay window or a dormer inside the gap would look identical to a
    perfect fit."""
    pixels = synthesise_eave_pixels(occlusion_severity=0.8)
    fit = fit_ransac_line(pixels, 0.8)
    assert fit.bridged
    assert fit.largest_gap_px > 0.0
    assert "FIT-BRIDGE" in {f.code for f in fit.findings}


def test_a_clean_run_claims_no_bridge():
    fit = fit_ransac_line(synthesise_eave_pixels(occlusion_severity=0.0), 0.0)
    assert not fit.bridged
    assert "FIT-BRIDGE" not in {f.code for f in fit.findings}


def test_confidence_stays_inside_zero_and_one():
    for severity in (0.0, 0.25, 0.5, 0.75, 1.0):
        fit = fit_ransac_line(
            synthesise_eave_pixels(occlusion_severity=severity), severity)
        assert 0.0 <= fit.confidence <= 1.0, severity
        assert 0.0 <= fit.inlier_ratio <= 1.0, severity


def test_the_direction_vector_is_a_unit_vector():
    fit = fit_ransac_line(synthesise_eave_pixels(occlusion_severity=0.5), 0.5)
    dx, dy = fit.direction
    assert math.hypot(dx, dy) == pytest.approx(1.0, abs=1e-9)


def test_the_fit_recovers_a_known_sloped_line_through_outliers():
    """Ground truth chosen in advance, then buried in outliers."""
    truth = [(float(x), 0.5 * x + 20.0) for x in range(0, 300, 3)]
    outliers = [(float(x), 400.0 - x) for x in range(120, 200, 4)]
    fit = fit_ransac_line(truth + outliers, 0.3)
    assert fit.slope == pytest.approx(0.5, abs=0.01)
    assert fit.intercept == pytest.approx(20.0, abs=1.0)


def test_a_fit_with_fewer_than_two_pixels_raises():
    with pytest.raises(ValueError):
        fit_ransac_line([(1.0, 1.0)], 0.0)


# ---------------------------------------------------------------------------
# Bulb spacing
# ---------------------------------------------------------------------------

def test_the_first_and_last_bulbs_land_on_the_ends_exactly():
    run = calculate_bulb_spacing((120.0, 330.0), (680.0, 330.0), 40.0)
    assert run.coordinates[0] == (120.0, 330.0)
    assert run.coordinates[-1] == (680.0, 330.0)


def test_every_gap_is_identical():
    run = calculate_bulb_spacing((100.0, 200.0), (700.0, 260.0), 37.0)
    gaps = [math.dist(a, b)
            for a, b in zip(run.coordinates, run.coordinates[1:])]
    for gap in gaps:
        assert gap == pytest.approx(gaps[0], abs=1e-9)


def test_no_gap_is_ever_wider_than_the_pitch_that_was_asked_for():
    """Rounding the count down would stretch the spacing past spec, which
    shows on the house as a dark gap and shows in no number."""
    for length in (100.0, 137.0, 240.0, 560.0, 999.0):
        for pitch in (7.0, 18.5, 40.0, 63.0):
            run = calculate_bulb_spacing((0.0, 0.0), (length, 0.0), pitch)
            assert run.actual_pitch_px <= pitch + 1e-9, (length, pitch)


def test_an_exact_division_keeps_the_requested_pitch():
    run = calculate_bulb_spacing((0.0, 0.0), (560.0, 0.0), 40.0)
    assert run.actual_pitch_px == pytest.approx(40.0, abs=1e-9)
    assert run.count == 15


def test_the_count_matches_the_gaps_plus_one():
    for pitch in (9.0, 25.0, 40.0, 91.0):
        run = calculate_bulb_spacing((120.0, 330.0), (680.0, 330.0), pitch)
        gaps = run.run_length_px / run.actual_pitch_px
        assert run.count == pytest.approx(gaps + 1, abs=1e-6)


def test_a_diagonal_run_is_spaced_along_its_own_length():
    run = calculate_bulb_spacing((0.0, 0.0), (300.0, 400.0), 50.0)
    assert run.run_length_px == pytest.approx(500.0)
    assert run.count == 11
    assert run.actual_pitch_px == pytest.approx(50.0)


def test_a_run_shorter_than_the_pitch_still_lights_both_ends():
    run = calculate_bulb_spacing((0.0, 0.0), (10.0, 0.0), 40.0)
    assert run.count == 2
    assert run.coordinates == ((0.0, 0.0), (10.0, 0.0))


def test_a_zero_length_run_returns_one_coordinate_not_a_division_by_zero():
    run = calculate_bulb_spacing((5.0, 5.0), (5.0, 5.0), 40.0)
    assert run.count == 1
    assert any(f.severity == SEVERITY_CRITICAL for f in run.findings)


def test_a_non_positive_pitch_raises():
    for pitch in (0.0, -12.0):
        with pytest.raises(ValueError):
            calculate_bulb_spacing((0.0, 0.0), (100.0, 0.0), pitch)


def test_the_spacing_always_states_that_a_pixel_pitch_is_not_a_spacing():
    run = calculate_bulb_spacing((0.0, 0.0), (100.0, 0.0), 20.0)
    assert "BLB-SCALE" in {f.code for f in run.findings}


def test_the_three_stages_chain_end_to_end():
    extraction = extract_bottom_eave(SAMPLE_GABLE)
    pixels = synthesise_eave_pixels(
        start=extraction.points[0], end=extraction.points[-1],
        occlusion_severity=0.5)
    fit = fit_ransac_line(pixels, 0.5)
    left = extraction.points[0][0]
    right = extraction.points[-1][0]
    run = calculate_bulb_spacing((left, fit.y_at(left)),
                                 (right, fit.y_at(right)), 40.0)
    assert run.count > 10
    assert run.coordinates[0][0] == pytest.approx(left)
    assert run.coordinates[-1][0] == pytest.approx(right)


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
        "import tools.holiday_lighting_cv_console.core as c; "
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
    assert "holiday-lighting-cv-console" in keys
    icons = [t.icon for t in tools]
    assert len(icons) == len(set(icons))
    assert SEVERITY_OK != SEVERITY_CRITICAL
