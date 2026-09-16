"""Tests for the Core Web Vitals & Speed Optimizer Console.

The scoring assertions are not self referential. The log normal curve, the
inverse erfc constant, the erf polynomial, the per metric p10 and median
values and the category weights were all read from the Lighthouse source
before the engine was written. The curve is defined by two points, and the
first two tests below check them: a metric at its p10 scores exactly 90 and a
metric at its median scores exactly 50. An engine that got the constant wrong
fails there rather than agreeing with itself.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tools.core_web_vitals_optimizer_console.core import (
    AFTER,
    AFTER_CARELESS_FONT,
    BEFORE,
    CORE_WEB_VITALS,
    ENGINE_VERSION,
    FIELD_PERCENTILE,
    FIELD_WINDOW_DAYS,
    GOOD,
    INVERSE_ERFC_ONE_FIFTH,
    LAB_METRICS,
    LAB_METRIC_BY_KEY,
    NEEDS_IMPROVEMENT,
    POOR,
    SEVERITY_CRIT,
    SEVERITY_OK,
    SEVERITY_WARN,
    THRESHOLDS,
    THRESHOLD_BY_KEY,
    TOTAL_WEIGHT,
    UNWEIGHTED_METRICS,
    WORDPRESS_STEPS,
    erf,
    evaluate_web_vitals,
    get_wordpress_remediation_steps,
    lighthouse_score,
    log_normal_score,
    metric_score_rows,
    remediation_rows,
    score_sensitivity,
    simulate_score_jump,
    steps_that_move,
    threshold_rows,
)

ROOT = Path(__file__).resolve().parents[1]

PASSING = dict(lcp_sec=2.1, inp_ms=180, cls_score=0.04, ttfb_ms=420)


# ---------------------------------------------------------------------------
# The curve, checked at the two points that define it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("metric", LAB_METRICS, ids=lambda m: m.acronym)
def test_a_metric_at_its_p10_scores_exactly_ninety(metric):
    score = log_normal_score(metric.p10, metric.p10, metric.median)
    assert score * 100 == pytest.approx(90.0, abs=0.01)


@pytest.mark.parametrize("metric", LAB_METRICS, ids=lambda m: m.acronym)
def test_a_metric_at_its_median_scores_exactly_fifty(metric):
    score = log_normal_score(metric.median, metric.p10, metric.median)
    assert score * 100 == pytest.approx(50.0, abs=0.01)


def test_the_inverse_erfc_constant_is_the_lighthouse_one():
    assert INVERSE_ERFC_ONE_FIFTH == 0.9061938024368232


def test_the_erf_approximation_matches_its_known_values():
    assert erf(0) == pytest.approx(0.0, abs=1e-9)
    assert erf(1) == pytest.approx(0.8427007929, abs=1e-6)
    assert erf(-1) == pytest.approx(-0.8427007929, abs=1e-6)
    assert erf(3) == pytest.approx(0.9999779095, abs=1e-6)


def test_the_curve_is_monotonically_worse_as_the_metric_grows():
    previous = 1.1
    for value in range(100, 8000, 100):
        score = log_normal_score(value, 2500, 4000)
        assert score <= previous
        previous = score


def test_a_non_positive_metric_is_a_perfect_score():
    assert log_normal_score(0, 2500, 4000) == 1.0
    assert log_normal_score(-5, 2500, 4000) == 1.0


def test_the_curve_rejects_impossible_parameters():
    with pytest.raises(ValueError, match="median must be greater than zero"):
        log_normal_score(100, 50, 0)
    with pytest.raises(ValueError, match="p10 must be greater than zero"):
        log_normal_score(100, 0, 500)
    with pytest.raises(ValueError, match="p10 must be less than the median"):
        log_normal_score(100, 600, 500)


def test_every_score_stays_inside_zero_and_one():
    for metric in LAB_METRICS:
        for factor in (0.01, 0.5, 1, 2, 10, 100):
            score = log_normal_score(metric.median * factor, metric.p10,
                                     metric.median)
            assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# The weights, straight from the Lighthouse default config
# ---------------------------------------------------------------------------


def test_the_weights_are_the_published_ones():
    assert {m.acronym: m.weight for m in LAB_METRICS} == {
        "FCP": 10, "SI": 10, "LCP": 25, "TBT": 30, "CLS": 25}


def test_the_weights_total_one_hundred():
    assert TOTAL_WEIGHT == 100


def test_inp_and_ttfb_carry_no_weight_at_all():
    """The whole point of the tool. Lighthouse lists INP with weight 0."""
    assert set(UNWEIGHTED_METRICS) == {"inp", "ttfb"}
    assert "inp" not in LAB_METRIC_BY_KEY
    assert "ttfb" not in LAB_METRIC_BY_KEY


def test_the_mobile_curves_are_the_published_ones():
    curves = {m.acronym: (m.p10, m.median) for m in LAB_METRICS}
    assert curves == {
        "FCP": (1800, 3000), "SI": (3387, 5800), "LCP": (2500, 4000),
        "TBT": (200, 600), "CLS": (0.1, 0.25)}


def test_a_page_at_every_p10_scores_ninety():
    assert lighthouse_score({m.key: m.p10 for m in LAB_METRICS}) == 90


def test_a_page_at_every_median_scores_fifty():
    assert lighthouse_score({m.key: m.median for m in LAB_METRICS}) == 50


def test_a_missing_lab_metric_is_rejected():
    metrics = {m.key: m.p10 for m in LAB_METRICS}
    del metrics["tbt"]
    with pytest.raises(ValueError, match="missing lab metrics: tbt"):
        lighthouse_score(metrics)


def test_supplying_inp_cannot_change_the_score():
    base = {m.key: m.p10 for m in LAB_METRICS}
    assert lighthouse_score(base) == lighthouse_score({**base, "inp": 2000})


def test_tbt_is_the_heaviest_single_metric():
    heaviest = max(LAB_METRICS, key=lambda m: m.weight)
    assert heaviest.acronym == "TBT"
    assert heaviest.weight == 30


# ---------------------------------------------------------------------------
# Thresholds and the assessment
# ---------------------------------------------------------------------------


def test_the_thresholds_are_the_published_ones():
    assert THRESHOLD_BY_KEY["lcp"].good_at_or_below == 2.5
    assert THRESHOLD_BY_KEY["lcp"].poor_above == 4.0
    assert THRESHOLD_BY_KEY["inp"].good_at_or_below == 200.0
    assert THRESHOLD_BY_KEY["inp"].poor_above == 500.0
    assert THRESHOLD_BY_KEY["cls"].good_at_or_below == 0.1
    assert THRESHOLD_BY_KEY["cls"].poor_above == 0.25
    assert THRESHOLD_BY_KEY["ttfb"].good_at_or_below == 800.0
    assert THRESHOLD_BY_KEY["ttfb"].poor_above == 1800.0


def test_exactly_three_metrics_are_core_web_vitals():
    assert CORE_WEB_VITALS == ("lcp", "inp", "cls")
    assert THRESHOLD_BY_KEY["ttfb"].is_core_web_vital is False


@pytest.mark.parametrize("key,value,expected", [
    ("lcp", 2.5, GOOD), ("lcp", 2.51, NEEDS_IMPROVEMENT),
    ("lcp", 4.0, NEEDS_IMPROVEMENT), ("lcp", 4.01, POOR),
    ("inp", 200, GOOD), ("inp", 201, NEEDS_IMPROVEMENT),
    ("inp", 500, NEEDS_IMPROVEMENT), ("inp", 501, POOR),
    ("cls", 0.1, GOOD), ("cls", 0.11, NEEDS_IMPROVEMENT),
    ("cls", 0.25, NEEDS_IMPROVEMENT), ("cls", 0.26, POOR),
    ("ttfb", 800, GOOD), ("ttfb", 1800, NEEDS_IMPROVEMENT),
    ("ttfb", 1801, POOR),
])
def test_the_boundaries_are_inclusive_on_the_good_side(key, value, expected):
    assert THRESHOLD_BY_KEY[key].status(value) == expected


def test_all_three_good_is_a_pass():
    assessment = evaluate_web_vitals(**PASSING)
    assert assessment.passes_core_web_vitals is True
    assert assessment.seo_indicator == "PASS"
    assert assessment.worst_status == GOOD


def test_two_of_three_good_is_still_a_fail():
    """There is no partial credit in the assessment."""
    assessment = evaluate_web_vitals(lcp_sec=2.1, inp_ms=260, cls_score=0.04,
                                     ttfb_ms=420)
    assert assessment.passes_core_web_vitals is False
    assert assessment.seo_indicator == "FAIL"
    assert "CWV-ONE-AWAY" in [f.code for f in assessment.findings]


@pytest.mark.parametrize("key", CORE_WEB_VITALS)
def test_any_single_core_web_vital_can_fail_the_url_alone(key):
    values = dict(PASSING)
    argument = {"lcp": "lcp_sec", "inp": "inp_ms", "cls": "cls_score"}[key]
    values[argument] = THRESHOLD_BY_KEY[key].poor_above + 1
    assert evaluate_web_vitals(**values).passes_core_web_vitals is False


def test_a_terrible_ttfb_cannot_fail_the_assessment():
    """TTFB is diagnostic. Google does not assess it."""
    assessment = evaluate_web_vitals(lcp_sec=2.1, inp_ms=180, cls_score=0.04,
                                     ttfb_ms=2900)
    assert assessment.by_key("ttfb").status == POOR
    assert assessment.passes_core_web_vitals is True
    assert "CWV-TTFB-DIAGNOSTIC" in [f.code for f in assessment.findings]


def test_a_negative_metric_is_rejected():
    for argument in ("lcp_sec", "inp_ms", "cls_score", "ttfb_ms"):
        values = dict(PASSING)
        values[argument] = -1
        with pytest.raises(ValueError, match="cannot be negative"):
            evaluate_web_vitals(**values)


def test_a_perfect_zero_page_passes():
    assessment = evaluate_web_vitals(0, 0, 0, 0)
    assert assessment.passes_core_web_vitals is True
    assert all(v.status == GOOD for v in assessment.verdicts)


def test_the_assessment_always_names_the_field_caveat():
    for values in (PASSING, dict(lcp_sec=9, inp_ms=900, cls_score=0.6,
                                 ttfb_ms=2500)):
        codes = [f.code for f in evaluate_web_vitals(**values).findings]
        assert "CWV-FIELD-ONLY" in codes


def test_a_failing_inp_always_raises_the_unscored_finding():
    assessment = evaluate_web_vitals(lcp_sec=2.1, inp_ms=520, cls_score=0.04,
                                     ttfb_ms=420)
    finding = next(f for f in assessment.findings
                   if f.code == "CWV-INP-UNSCORED")
    assert finding.severity == SEVERITY_CRIT
    assert "weight 0" in finding.detail


def test_multiple_failures_are_reported_together():
    assessment = evaluate_web_vitals(lcp_sec=6, inp_ms=700, cls_score=0.4,
                                     ttfb_ms=2200)
    codes = [f.code for f in assessment.findings]
    assert "CWV-MULTI" in codes
    assert "CWV-ONE-AWAY" not in codes


def test_a_pass_is_reported_as_a_pass():
    codes = [f.code for f in evaluate_web_vitals(**PASSING).findings]
    assert "CWV-PASS" in codes
    assert "CWV-ONE-AWAY" not in codes
    assert "CWV-MULTI" not in codes


def test_the_distance_from_good_is_reported_for_a_failing_metric():
    assessment = evaluate_web_vitals(lcp_sec=4.0, inp_ms=180, cls_score=0.04,
                                     ttfb_ms=420)
    assert assessment.by_key("lcp").distance == "1.50 s over"
    assert assessment.by_key("inp").distance == "already Good"


def test_the_assessment_is_deterministic():
    first = evaluate_web_vitals(**PASSING)
    second = evaluate_web_vitals(**PASSING)
    assert first.rows() == second.rows()
    assert [f.code for f in first.findings] == [f.code for f in second.findings]


# ---------------------------------------------------------------------------
# Before and after
# ---------------------------------------------------------------------------


def test_the_before_and_after_scores_are_computed_not_written_down():
    jump = simulate_score_jump()
    assert jump.before_score == lighthouse_score(BEFORE.lab_metrics())
    assert jump.after_score == lighthouse_score(AFTER.lab_metrics())


def test_the_worked_example_runs_forty_two_to_ninety_eight():
    jump = simulate_score_jump()
    assert jump.before_score == 42
    assert jump.after_score == 98
    assert jump.points_gained == 56


def test_the_ninety_eight_still_fails_the_assessment():
    """The headline of the whole tool. INP has weight 0, so the two numbers
    are free to disagree, and on this page they do."""
    jump = simulate_score_jump()
    assert jump.after_score >= 90
    assert jump.after_passes is False
    assert jump.lab_field_gap is True
    assert "CWV-LAB-FIELD-GAP" in [f.code for f in jump.findings]


def test_the_failing_metric_after_the_work_is_inp():
    after = AFTER.assessment()
    assert after.by_key("lcp").status == GOOD
    assert after.by_key("cls").status == GOOD
    assert after.by_key("inp").status == NEEDS_IMPROVEMENT


def test_nothing_applied_means_nothing_gained():
    jump = simulate_score_jump(False)
    assert jump.points_gained == 0
    assert jump.bytes_saved == 0
    assert jump.main_thread_saved_ms == 0
    assert [f.code for f in jump.findings] == ["CWV-NO-CHANGE"]


def test_the_careless_font_fix_costs_more_score_than_it_buys():
    careful = simulate_score_jump(True, AFTER)
    careless = simulate_score_jump(True, AFTER_CARELESS_FONT)
    assert careless.after_score < careful.after_score
    assert "CWV-CLS-TRADE" in [f.code for f in careless.findings]


def test_the_careless_font_fix_breaks_a_core_web_vital():
    assert AFTER.assessment().by_key("cls").status == GOOD
    assert AFTER_CARELESS_FONT.assessment().by_key("cls").status \
        == NEEDS_IMPROVEMENT


def test_the_only_difference_between_the_two_after_pages_is_layout_shift():
    careful = AFTER.lab_metrics()
    careless = AFTER_CARELESS_FONT.lab_metrics()
    differing = [k for k in careful if careful[k] != careless[k]]
    assert differing == ["cls"]


def test_the_payload_savings_add_up():
    jump = simulate_score_jump()
    assert jump.bytes_saved == BEFORE.total_bytes - AFTER.total_bytes
    assert jump.bytes_saved > 0
    parts = sum(
        getattr(BEFORE, a) - getattr(AFTER, a)
        for a in ("js_bytes", "css_bytes", "font_bytes", "image_bytes"))
    assert parts == jump.bytes_saved


def test_the_total_row_equals_the_sum_of_its_rows():
    rows = simulate_score_jump().payload_rows()
    total = rows[-1]
    assert total["Asset"] == "Total"
    for column in ("Before", "After", "Saved"):
        parts = sum(float(r[column].replace(" kB", "").replace(",", ""))
                    for r in rows[:-1])
        assert float(total[column].replace(" kB", "").replace(",", "")) \
            == pytest.approx(parts, abs=1.0)


def test_main_thread_time_falls():
    jump = simulate_score_jump()
    assert jump.main_thread_saved_ms > 0
    assert jump.main_thread_saved_ms == (BEFORE.main_thread_ms
                                         - AFTER.main_thread_ms)


def test_every_jump_warns_about_the_field_window():
    for applied in (True, False):
        codes = [f.code for f in simulate_score_jump(applied).findings]
        if applied:
            assert "CWV-WINDOW" in codes


def test_the_window_finding_names_the_lag():
    finding = next(f for f in simulate_score_jump().findings
                   if f.code == "CWV-WINDOW")
    assert str(FIELD_WINDOW_DAYS) in finding.title
    assert finding.severity == SEVERITY_WARN


def test_the_before_page_fails_on_more_than_one_vital():
    before = BEFORE.assessment()
    failing = [v for v in before.verdicts
               if v.is_core_web_vital and v.status != GOOD]
    assert len(failing) >= 2


# ---------------------------------------------------------------------------
# Remediation
# ---------------------------------------------------------------------------


def test_five_steps_are_returned_in_priority_order():
    steps = get_wordpress_remediation_steps()
    assert len(steps) == 5
    assert [s.rank for s in steps] == [1, 2, 3, 4, 5]


def test_the_brief_asked_for_four_categories_and_all_four_are_covered():
    text = " ".join(s.title + s.action for s in WORDPRESS_STEPS).lower()
    for needle in ("render blocking", "unused css", "font-display",
                   "caching"):
        assert needle in text


def test_every_step_says_what_it_does_not_move():
    for step in WORDPRESS_STEPS:
        assert step.moves
        assert step.does_not_move
        assert not set(step.moves) & set(step.does_not_move)


def test_exactly_one_step_moves_inp():
    movers = steps_that_move("INP")
    assert len(movers) == 1
    assert "JavaScript" in movers[0].title


def test_caching_moves_ttfb_and_not_a_single_core_web_vital_directly():
    caching = next(s for s in WORDPRESS_STEPS if "caching" in s.title.lower())
    assert "TTFB" in caching.moves
    assert "INP" in caching.does_not_move
    assert "CLS" in caching.does_not_move


def test_the_font_step_warns_that_it_can_make_cls_worse():
    font = next(s for s in WORDPRESS_STEPS if "font-display" in s.title)
    assert "CLS" in font.risk
    assert "size-adjust" in font.action


def test_every_step_names_an_effort_a_risk_and_a_wordpress_note():
    for step in WORDPRESS_STEPS:
        assert step.effort in ("Low", "Medium", "High")
        assert step.risk.strip()
        assert step.wordpress_note.strip()
        assert step.action.strip()


def test_a_metric_no_step_moves_returns_an_empty_list():
    assert steps_that_move("Time to Interactive") == []


# ---------------------------------------------------------------------------
# Sensitivity
# ---------------------------------------------------------------------------


def test_the_sensitivity_table_covers_every_scored_metric():
    rows = score_sensitivity()
    assert len(rows) == len(LAB_METRICS)


def test_the_sensitivity_table_is_ordered_by_points_gained():
    gains = [int(r["Points gained"].lstrip("+")) for r in score_sensitivity()]
    assert gains == sorted(gains, reverse=True)


def test_perfecting_one_metric_never_lowers_the_score():
    baseline = lighthouse_score(BEFORE.lab_metrics())
    for row in score_sensitivity():
        assert int(row["Score if this alone reached its Good boundary"]) \
            >= baseline


def test_no_single_metric_alone_reaches_ninety():
    """The score is a composite. One metric cannot carry it."""
    for row in score_sensitivity():
        assert int(row["Score if this alone reached its Good boundary"]) < 90


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


def test_assessment_rows_are_arrow_safe():
    _assert_all_strings(evaluate_web_vitals(**PASSING).rows())


def test_threshold_rows_are_arrow_safe():
    _assert_all_strings(threshold_rows())


def test_metric_rows_are_arrow_safe():
    _assert_all_strings(simulate_score_jump().metric_rows())


def test_payload_rows_are_arrow_safe():
    _assert_all_strings(simulate_score_jump().payload_rows())


def test_remediation_rows_are_arrow_safe():
    _assert_all_strings(remediation_rows())


def test_score_breakdown_rows_are_arrow_safe():
    _assert_all_strings(metric_score_rows(BEFORE.lab_metrics()))


def test_sensitivity_rows_are_arrow_safe():
    _assert_all_strings(score_sensitivity())


def test_the_threshold_table_marks_the_lighthouse_weights():
    rows = {r["Metric"]: r for r in threshold_rows()}
    assert rows["Interaction to Next Paint"]["Lighthouse weight"] == "0%"
    assert rows["Time to First Byte"]["Lighthouse weight"] == "0%"
    assert rows["Largest Contentful Paint"]["Lighthouse weight"] == "25%"


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "core_web_vitals_optimizer_console"
              / "core.py").read_text()
    assert "import streamlit" not in source
    assert "from streamlit" not in source


def test_the_core_is_importable_without_streamlit_installed():
    code = (
        "import sys;"
        "sys.modules['streamlit'] = None;"
        "from tools.core_web_vitals_optimizer_console import core;"
        "print(core.simulate_score_jump().after_score)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "98"


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [t.note for t in THRESHOLDS]
        + [s.action + s.risk + s.wordpress_note for s in WORDPRESS_STEPS]
        + [f.detail + f.fix + f.title
           for values in (PASSING,
                          dict(lcp_sec=6, inp_ms=700, cls_score=0.4,
                               ttfb_ms=2200),
                          dict(lcp_sec=2.1, inp_ms=260, cls_score=0.04,
                               ttfb_ms=420))
           for f in evaluate_web_vitals(**values).findings]
        + [f.detail + f.fix + f.title
           for after in (AFTER, AFTER_CARELESS_FONT)
           for f in simulate_score_jump(True, after).findings]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_field_percentile_is_the_published_one():
    assert FIELD_PERCENTILE == 75
    assert FIELD_WINDOW_DAYS == 28


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools

    tools = all_tools()
    entry = next(t for t in tools
                 if t.key == "core-web-vitals-optimizer-console")
    assert entry.title == "Core Web Vitals & Speed Optimizer Console"
    assert len(entry.tagline) > 30
    icons = [t.icon for t in tools if not t.key.startswith("synthetic-")]
    assert icons.count(entry.icon) == 1
