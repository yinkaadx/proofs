"""Engine tests for the Supply Chain QA and Regression Console.

Written for pytest. Deterministic: nothing in the engine reads the clock or a
random source, so the expected figure in a generated ticket is the figure an
engineer reproduces.

Run: pytest tests/test_supply_chain_qa_console.py -q
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.supply_chain_qa_console.core import (  # noqa: E402
    DEFAULT_SERVICE,
    ENGINE_VERSION,
    MODULE_LEAD_TIME,
    MODULE_PLANNING,
    MODULE_REPLENISH,
    MODULE_SAFETY_STOCK,
    MODULES,
    REGRESSION_AT_RISK,
    REGRESSION_CLEAN,
    REGRESSION_FAILED,
    SERVICE_LEVELS,
    SEV_BLOCKER,
    SEV_MAJOR,
    SEV_MINOR,
    SEVERITIES,
    STABLE_SUPPLIER,
    VOLATILE_SUPPLIER,
    ExecutionCycle,
    PlanningScenario,
    console_summary,
    defect_backlog,
    execution_rows,
    generate_defect_report,
    get_execution_metrics,
    get_supply_chain_test_cases,
    reorder_point,
    safety_stock_demand_only,
    safety_stock_full,
    severity_rank,
    case_matrix_rows,
    z_for,
)


# ---------------------------------------------------------------------------
# The planning arithmetic
# ---------------------------------------------------------------------------

def test_the_two_formulas_agree_when_lead_time_never_varies():
    """Which is exactly why a suite built on a reliable supplier stays green
    while the bug is live."""
    assert STABLE_SUPPLIER.lead_time_sigma == 0
    assert STABLE_SUPPLIER.naive_safety_stock == \
        STABLE_SUPPLIER.correct_safety_stock
    assert STABLE_SUPPLIER.shortfall == 0


def test_the_formulas_diverge_the_moment_lead_time_varies():
    assert VOLATILE_SUPPLIER.lead_time_sigma > 0
    assert VOLATILE_SUPPLIER.correct_safety_stock > \
        VOLATILE_SUPPLIER.naive_safety_stock
    assert VOLATILE_SUPPLIER.shortfall > 0


def test_the_naive_formula_always_understates_never_overstates():
    """It drops a term that can only add variance, so the error has a sign."""
    for sigma in (0.0, 0.5, 1.0, 2.0, 4.5, 8.0):
        scenario = PlanningScenario("SKU-T", 120.0, 18.0, 14.0, sigma)
        assert scenario.correct_safety_stock >= scenario.naive_safety_stock
        assert scenario.shortfall >= 0


def test_the_gap_grows_with_lead_time_variance():
    gaps = [PlanningScenario("SKU-T", 120.0, 18.0, 14.0, s).shortfall
            for s in (0.0, 1.0, 2.0, 4.5, 8.0)]
    assert gaps == sorted(gaps)
    assert gaps[0] == 0


def test_kings_formula_matches_the_arithmetic_by_hand():
    case = VOLATILE_SUPPLIER
    expected = z_for(case.service_level) * math.sqrt(
        case.lead_time_days * case.demand_sigma ** 2
        + case.demand_mean ** 2 * case.lead_time_sigma ** 2)
    assert case.correct_safety_stock == pytest.approx(expected, abs=0.01)


def test_the_demand_only_formula_matches_the_arithmetic_by_hand():
    case = VOLATILE_SUPPLIER
    expected = (z_for(case.service_level) * case.demand_sigma
                * math.sqrt(case.lead_time_days))
    assert case.naive_safety_stock == pytest.approx(expected, abs=0.01)


def test_zero_demand_variance_still_produces_a_lead_time_buffer():
    """A zero here would mean the variances are multiplied rather than added,
    which leaves a perfectly predictable product stocked out."""
    scenario = PlanningScenario("SKU-T", 120.0, 0.0, 14.0, 4.5)
    assert scenario.naive_safety_stock == 0
    assert scenario.correct_safety_stock > 0


def test_zero_variance_everywhere_gives_no_buffer_at_all():
    scenario = PlanningScenario("SKU-T", 120.0, 0.0, 14.0, 0.0)
    assert scenario.correct_safety_stock == 0


def test_a_negative_or_missing_lead_time_does_not_produce_a_complex_result():
    scenario = PlanningScenario("SKU-T", 120.0, 18.0, -5.0, 0.0)
    assert scenario.correct_safety_stock >= 0
    assert not math.isnan(scenario.correct_safety_stock)
    assert safety_stock_demand_only(18.0, -5.0) == 0


def test_raising_the_service_level_raises_the_buffer_monotonically():
    buffers = [PlanningScenario("SKU-T", 120.0, 18.0, 14.0, 4.5, level)
               .correct_safety_stock
               for level in sorted(SERVICE_LEVELS)]
    assert buffers == sorted(buffers)
    assert len(set(buffers)) == len(buffers)


def test_an_undefined_service_level_is_refused_rather_than_interpolated():
    """Silently rounding to the nearest rung changes the buffer without
    telling anyone."""
    with pytest.raises(ValueError) as caught:
        z_for(0.965)
    assert "0.95" in str(caught.value)


def test_the_defined_z_scores_are_the_standard_normal_quantiles():
    assert z_for(0.95) == pytest.approx(1.645, abs=0.001)
    assert z_for(0.99) == pytest.approx(2.326, abs=0.001)
    assert SERVICE_LEVELS[DEFAULT_SERVICE] == z_for(DEFAULT_SERVICE)


def test_the_reorder_point_is_demand_over_lead_time_plus_the_buffer():
    case = VOLATILE_SUPPLIER
    assert case.correct_reorder_point == pytest.approx(
        case.demand_mean * case.lead_time_days + case.correct_safety_stock,
        abs=0.01)


def test_omitting_the_buffer_makes_the_reorder_point_fire_too_late():
    case = VOLATILE_SUPPLIER
    bare = reorder_point(case.demand_mean, case.lead_time_days, 0)
    assert bare < case.correct_reorder_point
    assert case.correct_reorder_point - bare == pytest.approx(
        case.correct_safety_stock, abs=0.01)


# ---------------------------------------------------------------------------
# Test case matrix
# ---------------------------------------------------------------------------

def test_the_matrix_covers_every_module():
    covered = {case.module for case in get_supply_chain_test_cases()}
    assert covered == set(MODULES)


def test_cases_are_ranked_worst_first():
    ranks = [case.rank for case in get_supply_chain_test_cases()]
    assert ranks == sorted(ranks)


def test_every_case_has_steps_and_a_numeric_or_rule_based_expectation():
    for case in get_supply_chain_test_cases():
        assert case.steps and case.preconditions
        assert len(case.expected) > 30, case.case_id
        assert case.expected.lower() != "works correctly"


def test_case_ids_are_unique():
    ids = [case.case_id for case in get_supply_chain_test_cases()]
    assert len(ids) == len(set(ids))


def test_the_matrix_contains_the_case_that_catches_the_dropped_term():
    case = next(c for c in get_supply_chain_test_cases()
                if c.case_id == "SC-101")
    assert case.severity_if_failed == SEV_BLOCKER
    assert str(VOLATILE_SUPPLIER.correct_safety_stock) in case.expected
    assert str(VOLATILE_SUPPLIER.naive_safety_stock) in case.expected


def test_the_matrix_admits_that_its_own_stable_case_is_misleading_alone():
    case = next(c for c in get_supply_chain_test_cases()
                if c.case_id == "SC-102")
    assert "misleading" in case.expected


def test_severity_rank_puts_an_unknown_value_last():
    assert severity_rank(SEV_BLOCKER) < severity_rank(SEV_MINOR)
    assert severity_rank("S9 Cosmetic") > severity_rank(SEV_MINOR)


def test_the_case_rows_are_arrow_safe():
    for row in case_matrix_rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# The defect generator
# ---------------------------------------------------------------------------

def test_the_ticket_carries_both_numbers_and_both_formulas():
    """A planning ticket whose expected value is prose gets closed as cannot
    reproduce."""
    report = generate_defect_report()
    assert report.expected_value > 0 and report.actual_value > 0
    assert report.formula_expected and report.formula_actual
    assert report.formula_expected != report.formula_actual


def test_the_expected_value_is_computed_not_typed():
    report = generate_defect_report(scenario=VOLATILE_SUPPLIER)
    assert report.expected_value == VOLATILE_SUPPLIER.correct_safety_stock
    assert report.actual_value == VOLATILE_SUPPLIER.naive_safety_stock


def test_the_variance_is_the_difference_and_is_negative_when_understated():
    report = generate_defect_report(scenario=VOLATILE_SUPPLIER)
    assert report.variance == pytest.approx(
        report.actual_value - report.expected_value, abs=0.01)
    assert report.variance < 0, "the actual is short, so the variance is down"


def test_the_ticket_id_is_stable_across_separate_processes():
    """Python randomises string hashing per process, so hash() would move the
    identifier between the report and the reproduction."""
    first = generate_defect_report().ticket_id
    result = subprocess.run(
        [sys.executable, "-c",
         "from tools.supply_chain_qa_console.core import "
         "generate_defect_report; print(generate_defect_report().ticket_id)"],
        cwd=str(ROOT), capture_output=True, text=True,
        env={"PYTHONHASHSEED": "random", "PATH": "/usr/bin:/bin"})
    assert result.stdout.strip() == first, result.stderr


def test_the_same_inputs_give_the_same_ticket():
    assert generate_defect_report().ticket_id == \
        generate_defect_report().ticket_id


def test_different_modules_give_different_tickets():
    ids = {generate_defect_report(module, SEV_BLOCKER).ticket_id
           for module in MODULES}
    assert len(ids) == len(MODULES)


def test_the_payload_carries_the_request_and_both_responses():
    payload = json.loads(generate_defect_report().payload_json())
    assert set(payload) == {"request", "response_actual", "response_expected"}
    assert payload["response_actual"]["safety_stock"] != \
        payload["response_expected"]["safety_stock"]
    assert payload["request"]["lead_time"]["std_dev_days"] > 0


def test_the_payload_names_the_z_score_it_used():
    payload = json.loads(generate_defect_report().payload_json())
    assert payload["request"]["z_score"] == z_for(DEFAULT_SERVICE)


def test_the_markdown_ticket_has_every_section_an_engineer_needs():
    markdown = generate_defect_report().as_markdown()
    for heading in ("## Summary", "## Preconditions", "## Steps to reproduce",
                    "## Expected", "## Actual", "## Variance",
                    "## Business impact", "## Sample payload"):
        assert heading in markdown, heading
    assert "```json" in markdown


def test_the_steps_are_numbered_and_reproducible():
    report = generate_defect_report()
    assert len(report.steps) >= 3
    assert "1. " in report.as_markdown()


def test_an_unknown_module_is_refused():
    with pytest.raises(ValueError) as caught:
        generate_defect_report("Forecasting", SEV_BLOCKER)
    assert "Forecasting" in str(caught.value)


def test_an_unknown_severity_is_refused():
    with pytest.raises(ValueError):
        generate_defect_report(MODULE_PLANNING, "S9 Cosmetic")


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("severity", SEVERITIES)
def test_every_module_and_severity_pair_produces_a_complete_ticket(module,
                                                                   severity):
    report = generate_defect_report(module, severity)
    assert report.ticket_id.startswith("SCQA-")
    assert report.summary and report.business_impact
    for row in report.rows():
        assert all(isinstance(v, str) for v in row.values()), row


def test_a_stable_supplier_produces_a_ticket_with_no_variance():
    """Which is the honest output, not a suppressed one."""
    report = generate_defect_report(scenario=STABLE_SUPPLIER)
    assert report.variance == 0
    assert report.variance_percent == 0


def test_the_backlog_has_one_ticket_per_module():
    backlog = defect_backlog()
    assert len(backlog) == len(MODULES)
    assert {r.module for r in backlog} == set(MODULES)


# ---------------------------------------------------------------------------
# Execution metrics
# ---------------------------------------------------------------------------

def test_pass_rate_counts_only_what_executed():
    cycle = ExecutionCycle("c", 100, 60, 20, 20, 0)
    assert cycle.executed == 80
    assert cycle.pass_rate == pytest.approx(0.75)


def test_verified_rate_counts_blocked_tests_as_unknowns():
    """A blocked test is not a pass and not a failure, and counting it out of
    the denominator is how a release is signed off on evidence nobody
    gathered."""
    cycle = ExecutionCycle("c", 100, 60, 20, 20, 0)
    assert cycle.verified_rate == pytest.approx(0.60)
    assert cycle.verified_rate < cycle.pass_rate


def test_a_flattering_pass_rate_can_hide_poor_coverage():
    cycle = ExecutionCycle("c", 100, 47, 3, 50, 0)
    assert cycle.pass_rate == pytest.approx(0.94)
    assert cycle.verified_rate == pytest.approx(0.47)
    assert cycle.regression_status != REGRESSION_CLEAN


def test_any_open_blocker_fails_the_regression():
    assert ExecutionCycle("c", 10, 10, 0, 0, 1).regression_status == \
        REGRESSION_FAILED


def test_any_failure_fails_the_regression():
    assert ExecutionCycle("c", 10, 9, 1, 0, 0).regression_status == \
        REGRESSION_FAILED


def test_blocked_tests_leave_the_cycle_at_risk_rather_than_clean():
    cycle = ExecutionCycle("c", 10, 9, 0, 1, 0)
    assert cycle.regression_status == REGRESSION_AT_RISK
    assert not cycle.shippable


def test_only_a_complete_clean_run_is_shippable():
    cycle = ExecutionCycle("c", 10, 10, 0, 0, 0)
    assert cycle.regression_status == REGRESSION_CLEAN
    assert cycle.shippable
    assert cycle.pass_rate == cycle.verified_rate == 1.0


def test_an_empty_cycle_does_not_divide_by_zero():
    cycle = ExecutionCycle("c", 0, 0, 0, 0, 0)
    assert cycle.pass_rate == 0.0
    assert cycle.verified_rate == 0.0
    assert cycle.coverage == 0.0


def test_tests_never_run_are_counted_and_never_negative():
    assert ExecutionCycle("c", 100, 40, 10, 20, 0).not_run == 30
    assert ExecutionCycle("c", 10, 40, 10, 20, 0).not_run == 0


def test_the_reported_cycles_improve_across_the_run():
    cycles = get_execution_metrics()["cycles"]
    assert len(cycles) >= 3
    rates = [c.verified_rate for c in cycles]
    assert rates == sorted(rates)


def test_the_latest_cycle_is_judged_rather_than_assumed_good():
    metrics = get_execution_metrics()
    latest = metrics["latest"]
    assert latest.pass_rate == 1.0
    assert not latest.shippable, (
        "a hundred percent pass rate with blocked tests is not clean")
    assert metrics["regression_status"] == REGRESSION_AT_RISK


def test_the_execution_rows_are_arrow_safe():
    for row in execution_rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# Summary and house rules
# ---------------------------------------------------------------------------

def test_the_summary_agrees_with_the_parts_it_counts():
    summary = console_summary()
    assert summary["test_cases"] == len(get_supply_chain_test_cases())
    assert summary["modules"] == len(MODULES)
    assert summary["open_defects"] == len(defect_backlog())
    assert summary["regression_status"] == \
        get_execution_metrics()["regression_status"]


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "supply_chain_qa_console" / "core.py").read_text()
    assert "streamlit" not in source


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [c.expected + c.title for c in get_supply_chain_test_cases()]
        + [r.summary + r.business_impact for r in defect_backlog()]
        + [c.cycle for c in get_execution_metrics()["cycles"]])
    assert "—" not in text
    assert "–" not in text
