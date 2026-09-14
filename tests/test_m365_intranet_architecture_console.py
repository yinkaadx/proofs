"""Engine tests for the Microsoft 365 Intranet Architecture Console.

Written for pytest. Deterministic throughout: the reporting date is injected
and nothing reads the clock or a random source.

Run: pytest tests/test_m365_intranet_architecture_console.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.m365_intranet_architecture_console.core import (  # noqa: E402
    ANNUAL_ALLOWANCE,
    DENIED_NOT_IN_GROUP,
    EMPLOYEE,
    ERR_BALANCE,
    ERR_CLOSED_PROJECT,
    ERR_FUTURE,
    ERR_HALF_DAY,
    ERR_HOURS_INCREMENT,
    ERR_HOURS_RANGE,
    ERR_IN_THE_PAST,
    ERR_NONE,
    ERR_NOTICE,
    ERR_OVERLAP,
    ERR_WEEK_CAP,
    ERR_ZERO_DAYS,
    LONG_REQUEST_DAYS,
    LONG_REQUEST_NOTICE_DAYS,
    MANAGER,
    MAX_DAILY_HOURS,
    MAX_WEEKLY_HOURS,
    MIN_ELAPSED_FOR_TREND,
    MODULES,
    OTHER_MANAGER,
    PROJECTS,
    RISK_BURNING_FAST,
    RISK_NONE,
    RISK_OVER_BUDGET,
    RISK_OVERDUE,
    ROLE_EMPLOYEE,
    ROLE_MANAGER,
    ROLES,
    ROUTE_DENIED_CLOSED,
    ROUTE_DENIED_ROLE,
    ROUTE_DENIED_SELF,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    TODAY,
    VARIANCE_TOLERANCE,
    Project,
    PtoLedger,
    TimesheetLedger,
    assess,
    can_open,
    decide,
    generate_insight,
    insight_prompt,
    log_hours,
    module_by_key,
    module_rows,
    project_by_code,
    project_rows,
    request_pto,
    visible_modules,
)

MANAGER_ONLY = ("approvals", "insights", "utilisation")
SHARED = ("timesheet", "pto_request", "handbook")


def ledger_with(days: float = 3.0, start: str = "2026-10-05") -> PtoLedger:
    """Starts three weeks out so a long request clears the notice rule. A
    nearer date is correctly refused, which is what this helper first got
    wrong."""
    ledger = PtoLedger()
    request_pto(ledger, EMPLOYEE, start, days)
    return ledger


# ---------------------------------------------------------------------------
# Role rendering and the gateway
# ---------------------------------------------------------------------------

def test_an_employee_sees_only_the_shared_modules():
    keys = {module.key for module in visible_modules(ROLE_EMPLOYEE)}
    assert keys == set(SHARED)


def test_a_manager_sees_everything():
    keys = {module.key for module in visible_modules(ROLE_MANAGER)}
    assert keys == {module.key for module in MODULES}


@pytest.mark.parametrize("key", MANAGER_ONLY)
def test_a_manager_only_module_is_refused_to_an_employee(key):
    """Hiding a tile is a convenience, not a control: the link still resolves,
    so the refusal has to come from a check."""
    decision = can_open(ROLE_EMPLOYEE, key)
    assert decision.allowed is False
    assert DENIED_NOT_IN_GROUP in decision.reason


@pytest.mark.parametrize("key", MANAGER_ONLY)
def test_a_manager_opens_every_manager_module(key):
    assert can_open(ROLE_MANAGER, key).allowed is True


@pytest.mark.parametrize("key", SHARED)
def test_both_roles_open_the_shared_modules(key):
    for role in ROLES:
        assert can_open(role, key).allowed is True


def test_the_refusal_names_the_group_that_would_grant_it():
    decision = can_open(ROLE_EMPLOYEE, "approvals")
    assert "SG-Intranet-Managers" in decision.reason
    assert "the link still resolves" in decision.reason


def test_a_module_that_does_not_exist_is_refused_rather_than_opened():
    decision = can_open(ROLE_MANAGER, "payroll_export")
    assert decision.allowed is False
    assert decision.module is None


def test_every_module_declares_a_group_and_a_list():
    for module in MODULES:
        assert module.entra_group.startswith("SG-")
        assert module.sharepoint_list
        assert module.roles


def test_the_matrix_shows_what_is_refused_as_well_as_what_is_granted():
    rows = module_rows(ROLE_EMPLOYEE)
    assert len(rows) == len(MODULES)
    assert {row["This role"] for row in rows} == {"Open", "Denied"}


def test_the_matrix_is_arrow_safe():
    for role in ROLES:
        rows = module_rows(role)
        for column in {key for row in rows for key in row}:
            kinds = {type(row[column]).__name__ for row in rows}
            assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


def test_a_module_lookup_returns_none_rather_than_guessing():
    assert module_by_key("nope") is None
    assert module_by_key("insights").label == "Project Insights"


# ---------------------------------------------------------------------------
# PTO balance validation
# ---------------------------------------------------------------------------

def test_a_clean_request_is_accepted_and_holds_its_days():
    ledger = PtoLedger()
    outcome = request_pto(ledger, EMPLOYEE, "2026-09-21", 3)
    assert outcome.accepted is True
    assert outcome.request.status == STATUS_PENDING
    assert ledger.balance(EMPLOYEE).pending == 3.0
    assert ledger.balance(EMPLOYEE).available == ANNUAL_ALLOWANCE - 3


def test_a_pending_request_is_held_back_from_the_next_one():
    """Two requests raised the same morning would otherwise each pass against
    the same balance and together overdraw it."""
    ledger = ledger_with(days=20.0)
    outcome = request_pto(ledger, EMPLOYEE, "2026-11-02", 10)
    assert outcome.accepted is False
    assert outcome.code == ERR_BALANCE
    assert "already waiting for approval" in outcome.message


def test_a_request_for_exactly_the_balance_is_allowed():
    ledger = PtoLedger()
    assert request_pto(ledger, EMPLOYEE, "2026-10-05",
                       ANNUAL_ALLOWANCE).accepted is True


def test_a_request_one_day_over_the_balance_is_refused():
    ledger = PtoLedger()
    outcome = request_pto(ledger, EMPLOYEE, "2026-10-05", ANNUAL_ALLOWANCE + 1)
    assert outcome.accepted is False
    assert outcome.code == ERR_BALANCE
    assert f"{ANNUAL_ALLOWANCE:.1f} available" in outcome.message


def test_zero_or_negative_days_are_refused():
    ledger = PtoLedger()
    assert request_pto(ledger, EMPLOYEE, "2026-09-21", 0).code == ERR_ZERO_DAYS
    assert request_pto(ledger, EMPLOYEE, "2026-09-21", -2).code == ERR_ZERO_DAYS


def test_a_half_day_is_allowed_and_a_quarter_day_is_not():
    ledger = PtoLedger()
    assert request_pto(ledger, EMPLOYEE, "2026-09-21", 0.5).accepted is True
    assert request_pto(ledger, EMPLOYEE, "2026-10-05", 1.25).code == ERR_HALF_DAY


def test_a_date_in_the_past_is_refused():
    ledger = PtoLedger()
    outcome = request_pto(ledger, EMPLOYEE, "2026-09-01", 2)
    assert outcome.code == ERR_IN_THE_PAST


def test_todays_date_is_refused_for_want_of_notice():
    ledger = PtoLedger()
    assert request_pto(ledger, EMPLOYEE, TODAY, 1).code == ERR_NOTICE


def test_a_long_request_needs_more_notice():
    ledger = PtoLedger()
    soon = request_pto(ledger, EMPLOYEE, "2026-09-20",
                       LONG_REQUEST_DAYS + 1)
    assert soon.code == ERR_NOTICE
    assert str(LONG_REQUEST_NOTICE_DAYS) in soon.message


def test_a_long_request_with_enough_notice_is_accepted():
    ledger = PtoLedger()
    assert request_pto(ledger, EMPLOYEE, "2026-10-05",
                       LONG_REQUEST_DAYS + 1).accepted is True


def test_a_short_request_needs_only_a_day_of_notice():
    ledger = PtoLedger()
    assert request_pto(ledger, EMPLOYEE, "2026-09-15", 1).accepted is True


def test_an_overlapping_request_is_refused_and_names_the_clash():
    ledger = ledger_with(days=3.0, start="2026-09-21")
    outcome = request_pto(ledger, EMPLOYEE, "2026-09-22", 2)
    assert outcome.code == ERR_OVERLAP
    assert "PTO-0001" in outcome.message


def test_a_request_that_starts_the_day_after_another_ends_is_fine():
    ledger = ledger_with(days=3.0, start="2026-09-21")   # covers 21 to 23
    assert request_pto(ledger, EMPLOYEE, "2026-09-24", 2).accepted is True


def test_a_rejected_request_frees_its_days_for_another():
    ledger = ledger_with(days=20.0)
    decide(ledger, ledger.requests[0], MANAGER, approve=False)
    assert ledger.balance(EMPLOYEE).pending == 0.0
    assert request_pto(ledger, EMPLOYEE, "2026-11-02", 10).accepted is True


def test_an_approved_request_counts_as_taken_rather_than_pending():
    ledger = ledger_with(days=4.0)
    decide(ledger, ledger.requests[0], MANAGER, approve=True)
    balance = ledger.balance(EMPLOYEE)
    assert balance.taken == 4.0
    assert balance.pending == 0.0
    assert balance.available == ANNUAL_ALLOWANCE - 4


def test_the_balance_table_is_arrow_safe():
    rows = ledger_with().balance(EMPLOYEE).rows()
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Timesheets
# ---------------------------------------------------------------------------

def test_a_clean_timesheet_line_is_accepted():
    ledger = TimesheetLedger()
    outcome = log_hours(ledger, EMPLOYEE, "PRJ-118", TODAY, 7.5)
    assert outcome.accepted is True
    assert outcome.code == ERR_NONE
    assert ledger.week_total(EMPLOYEE.upn, TODAY) == 7.5


def test_hours_outside_the_daily_range_are_refused():
    ledger = TimesheetLedger()
    assert log_hours(ledger, EMPLOYEE, "PRJ-118", TODAY, 0).code == \
        ERR_HOURS_RANGE
    assert log_hours(ledger, EMPLOYEE, "PRJ-118", TODAY,
                     MAX_DAILY_HOURS + 1).code == ERR_HOURS_RANGE


def test_anything_finer_than_a_quarter_hour_is_refused():
    ledger = TimesheetLedger()
    outcome = log_hours(ledger, EMPLOYEE, "PRJ-118", TODAY, 1.1)
    assert outcome.code == ERR_HOURS_INCREMENT
    assert "fifteen minutes" in outcome.message


def test_a_future_day_is_refused():
    ledger = TimesheetLedger()
    assert log_hours(ledger, EMPLOYEE, "PRJ-118", "2026-09-20", 4).code == \
        ERR_FUTURE


def test_a_closed_project_cannot_be_billed_to():
    ledger = TimesheetLedger()
    outcome = log_hours(ledger, EMPLOYEE, "PRJ-109", TODAY, 4)
    assert outcome.code == ERR_CLOSED_PROJECT
    assert "never reach an invoice" in outcome.message


def test_an_unknown_project_is_refused_rather_than_created():
    ledger = TimesheetLedger()
    assert log_hours(ledger, EMPLOYEE, "PRJ-999", TODAY, 4).code == \
        ERR_CLOSED_PROJECT


def test_the_weekly_cap_is_enforced_across_entries():
    ledger = TimesheetLedger()
    for day, hours in (("2026-09-14", 10), ("2026-09-15", 10),
                       ("2026-09-16", 10), ("2026-09-17", 10)):
        assert log_hours(ledger, EMPLOYEE, "PRJ-118", day, hours,
                         today="2026-09-18").accepted is True
    outcome = log_hours(ledger, EMPLOYEE, "PRJ-118", "2026-09-18", 11,
                        today="2026-09-18")
    assert outcome.code == ERR_WEEK_CAP
    assert f"{MAX_WEEKLY_HOURS:.0f} cap" in outcome.message


def test_the_week_total_counts_only_that_week():
    ledger = TimesheetLedger()
    log_hours(ledger, EMPLOYEE, "PRJ-118", "2026-09-14", 8)
    log_hours(ledger, EMPLOYEE, "PRJ-118", "2026-09-07", 8, today=TODAY)
    assert ledger.week_total(EMPLOYEE.upn, "2026-09-14") == 8.0


def test_hours_roll_up_per_project():
    ledger = TimesheetLedger()
    log_hours(ledger, EMPLOYEE, "PRJ-118", "2026-09-14", 6)
    log_hours(ledger, EMPLOYEE, "PRJ-121", "2026-09-14", 2)
    assert ledger.hours_for_project("PRJ-118") == 6.0
    assert ledger.hours_for_project("PRJ-121") == 2.0


# ---------------------------------------------------------------------------
# Approval routing
# ---------------------------------------------------------------------------

def test_a_manager_approves_and_the_status_moves():
    ledger = ledger_with()
    result = decide(ledger, ledger.requests[0], MANAGER, approve=True)
    assert result.ok is True
    assert result.request.status == STATUS_APPROVED
    assert ledger.requests[0].status == STATUS_APPROVED


def test_a_manager_rejects_and_the_days_come_back():
    ledger = ledger_with(days=5.0)
    result = decide(ledger, ledger.requests[0], MANAGER, approve=False)
    assert result.request.status == STATUS_REJECTED
    assert result.balance.available == ANNUAL_ALLOWANCE


def test_the_flow_runs_its_steps_in_order_with_the_connectors_named():
    ledger = ledger_with()
    steps = decide(ledger, ledger.requests[0], MANAGER, approve=True).steps
    assert [step.sequence for step in steps] == [1, 2, 3, 4, 5, 6, 7]
    connectors = {step.connector for step in steps}
    assert "Approvals" in connectors
    assert "SharePoint" in connectors
    assert "Office 365 Outlook" in connectors


def test_the_flow_timestamps_move_forward():
    ledger = ledger_with()
    steps = decide(ledger, ledger.requests[0], MANAGER, approve=True).steps
    stamps = [step.at for step in steps]
    assert stamps == sorted(stamps)


def test_an_employee_cannot_approve_anything():
    ledger = ledger_with()
    result = decide(ledger, ledger.requests[0], EMPLOYEE, approve=True)
    assert result.ok is False
    assert ROUTE_DENIED_ROLE in result.message
    assert ledger.requests[0].status == STATUS_PENDING


def test_a_person_cannot_approve_their_own_request():
    """The finding that reaches the board."""
    ledger = PtoLedger()
    request_pto(ledger, MANAGER, "2026-09-21", 2)
    result = decide(ledger, ledger.requests[0], MANAGER, approve=True)
    assert result.ok is False
    assert ROUTE_DENIED_SELF in result.message
    assert ledger.requests[0].status == STATUS_PENDING


def test_another_manager_may_approve_a_managers_own_leave():
    ledger = PtoLedger()
    request_pto(ledger, MANAGER, "2026-09-21", 2)
    result = decide(ledger, ledger.requests[0], OTHER_MANAGER, approve=True)
    assert result.ok is True


def test_a_second_decision_is_refused_rather_than_overwriting_the_first():
    ledger = ledger_with()
    decide(ledger, ledger.requests[0], MANAGER, approve=True)
    again = decide(ledger, ledger.requests[0], OTHER_MANAGER, approve=False)
    assert again.ok is False
    assert ROUTE_DENIED_CLOSED in again.message
    assert ledger.requests[0].status == STATUS_APPROVED


def test_a_refused_decision_still_shows_the_flow_that_delivered_it():
    ledger = ledger_with()
    result = decide(ledger, ledger.requests[0], EMPLOYEE, approve=True)
    assert len(result.steps) == 4
    assert result.rows()[0]["Connector"] == "SharePoint"


def test_the_run_history_is_arrow_safe():
    ledger = ledger_with()
    rows = decide(ledger, ledger.requests[0], MANAGER, approve=True).rows()
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Project risk and the insight
# ---------------------------------------------------------------------------

def project(billed: float = 100, budget: float = 200, start: str = "2026-06-01",
            due: str = "2026-12-01", open_: bool = True) -> Project:
    return Project("PRJ-TEST", "Test", "Client", budget, billed, 100, start,
                   due, open_)


def test_a_project_over_its_budget_is_the_first_thing_reported():
    risk = assess(project(billed=210, budget=200))
    assert risk.level == RISK_OVER_BUDGET
    assert risk.at_risk is True


def test_a_project_past_its_date_is_flagged():
    risk = assess(project(billed=50, due="2026-09-01"))
    assert risk.level == RISK_OVERDUE
    assert "day(s) ago" in risk.headline


def test_a_project_burning_faster_than_the_calendar_is_flagged():
    risk = assess(project(billed=180, budget=200, start="2026-08-01",
                          due="2026-12-01"))
    assert risk.level == RISK_BURNING_FAST
    assert "runs out around" in risk.detail


def test_a_project_inside_the_tolerance_is_left_alone():
    risk = assess(project(billed=100, budget=200, start="2026-06-15",
                          due="2026-12-15"))
    assert risk.level == RISK_NONE


def test_a_project_a_week_old_is_not_flagged_on_a_swing():
    """A burn ratio on a week of data moves on one day's work. Flagging it
    produces a report that cries wolf on every new project."""
    risk = assess(project(billed=40, budget=200, start="2026-09-07",
                          due="2027-01-15"))
    assert risk.level == RISK_NONE
    assert "too new to read" in risk.headline


def test_a_new_project_already_over_budget_is_still_flagged():
    """Being early does not excuse spending the whole budget."""
    risk = assess(project(billed=220, budget=200, start="2026-09-10",
                          due="2027-01-15"))
    assert risk.level == RISK_OVER_BUDGET


def test_the_trend_threshold_turns_where_it_is_declared():
    assert MIN_ELAPSED_FOR_TREND > 0
    early = project(billed=90, budget=100, start="2026-09-12",
                    due="2027-09-12")
    assert early.elapsed(TODAY) < MIN_ELAPSED_FOR_TREND
    assert assess(early).level == RISK_NONE


def test_variance_is_the_burn_against_the_calendar():
    subject = project(billed=150, budget=200, start="2026-06-01",
                      due="2026-12-01")
    assert subject.variance(TODAY) == pytest.approx(
        subject.burn() - subject.elapsed(TODAY))
    assert VARIANCE_TOLERANCE > 0


def test_an_empty_budget_does_not_divide_by_zero():
    assert project(billed=10, budget=0).burn() == 0.0


def test_a_project_with_no_span_does_not_divide_by_zero():
    assert project(start="2026-09-14", due="2026-09-14").elapsed(TODAY) == 1.0


def test_the_seeded_portfolio_reports_one_project_needing_a_decision():
    insight = generate_insight()
    assert insight.at_risk == 1
    assert "PRJ-124" in " ".join(insight.actions)


def test_the_headline_conjugates_for_one_project_and_for_several():
    """An executive summary that cannot conjugate reads as machine written,
    which is the one thing this module cannot afford."""
    one = generate_insight()
    assert one.at_risk == 1
    assert "projects needs a decision" in one.headline

    several = generate_insight((
        project(billed=210, budget=200),
        project(billed=190, budget=200, start="2026-08-01", due="2026-12-01"),
    ))
    assert several.at_risk == 2
    assert "projects need a decision" in several.headline


def test_the_summary_quotes_the_hours_and_value_it_measured():
    insight = generate_insight()
    live = [p for p in PROJECTS if p.open]
    assert insight.hours_billed == pytest.approx(
        sum(p.hours_billed for p in live))
    assert f"{insight.hours_billed:,.0f} hours" in insight.summary


def test_the_summary_names_the_nearest_deadline():
    insight = generate_insight()
    soonest = min((p for p in PROJECTS if p.open),
                  key=lambda p: p.days_left(TODAY))
    assert soonest.code in insight.summary
    assert soonest.due in insight.summary


def test_a_finding_is_produced_for_every_live_project():
    live = [p for p in PROJECTS if p.open]
    assert len(generate_insight().findings) == len(live)


def test_a_portfolio_with_nothing_at_risk_says_so_and_asks_for_nothing():
    calm = (project(billed=50, budget=200, start="2026-06-01",
                    due="2026-12-01"),)
    insight = generate_insight(calm)
    assert insight.at_risk == 0
    assert "tracking to plan" in insight.headline
    assert "Nothing needs a decision" in insight.actions[0]


def test_an_empty_portfolio_is_handled_rather_than_crashing():
    insight = generate_insight(tuple())
    assert insight.at_risk == 0
    assert insight.hours_billed == 0.0


def test_a_closed_project_is_left_out_of_the_report():
    codes = {row["Project"] for row in project_rows()}
    assert "PRJ-109" not in codes
    assert "PRJ-109" not in insight_prompt()


def test_the_prompt_carries_the_figures_and_no_document_content():
    prompt = insight_prompt()
    for live in (p for p in PROJECTS if p.open):
        assert live.code in prompt
        assert live.client in prompt
    assert TODAY in prompt
    assert "Use only the figures below" in prompt


def test_the_project_table_is_arrow_safe():
    rows = project_rows()
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


def test_a_project_lookup_returns_none_rather_than_guessing():
    assert project_by_code("PRJ-000") is None
    assert project_by_code("PRJ-118").name == "Intranet rebuild"


# ---------------------------------------------------------------------------
# Prose discipline
# ---------------------------------------------------------------------------

def test_no_dash_characters_in_any_user_facing_prose():
    parts: list[str] = []
    for role in ROLES:
        for module in MODULES:
            parts.append(can_open(role, module.key).reason)
    ledger = PtoLedger()
    for start, days in (("2026-09-21", 3), ("2026-09-01", 2), (TODAY, 1),
                        ("2026-10-05", 30), ("2026-09-21", 1.25)):
        parts.append(request_pto(ledger, EMPLOYEE, start, days).message)
    timesheet = TimesheetLedger()
    for code, day, hours in (("PRJ-118", TODAY, 7.5), ("PRJ-109", TODAY, 4),
                             ("PRJ-118", TODAY, 1.1)):
        parts.append(log_hours(timesheet, EMPLOYEE, code, day, hours).message)
    result = decide(ledger, ledger.requests[0], EMPLOYEE, approve=True)
    parts.append(result.message)
    parts += [step.detail for step in result.steps]
    insight = generate_insight()
    parts += [insight.headline, insight.summary, *insight.findings,
              *insight.actions, insight_prompt()]
    prose = "\n".join(parts)
    assert "—" not in prose
    assert "–" not in prose
