"""Page tests for the Microsoft 365 Intranet Architecture Console, via AppTest.

Written for pytest.

Run: pytest tests/test_m365_intranet_architecture_console_page.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.m365_intranet_architecture_console.core import (  # noqa: E402
    ANNUAL_ALLOWANCE,
    EMPLOYEE,
    MANAGER,
    MODULES,
    ROLE_EMPLOYEE,
    ROLE_MANAGER,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    TODAY,
)
from tools.m365_intranet_architecture_console.page import STATE  # noqa: E402

HARNESS = (Path(__file__).resolve().parent
           / "_page_harness_m365_intranet_architecture_console.py")

MANAGER_ONLY = ("Team Approvals", "Project Insights", "Team Utilisation")


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


def run_app(timeout: int = 180) -> AppTest:
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


def as_role(role: str, at: AppTest | None = None) -> AppTest:
    at = at or run_app()
    widget(at, "radio", "Signed in as").set_value(role).run()
    return at


def with_request(days: float = 3.0, start: str = "2026-10-05",
                 at: AppTest | None = None) -> AppTest:
    at = at or run_app()
    widget(at, "text_input", "First day").set_value(start).run()
    widget(at, "number_input", "Days").set_value(days).run()
    return press(at, "Submit the request")


def state_of(at: AppTest) -> dict:
    return at.session_state[STATE]


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------

def test_the_page_renders_with_no_exception():
    at = run_app()
    assert not at.exception, [str(e.value) for e in at.exception]


def test_the_hero_and_the_four_tabs_are_present():
    at = run_app()
    assert "Microsoft 365 Intranet Architecture Console" in text_of(at)
    labels = [tab.label for tab in at.tabs]
    for expected in ("Role Gateway", "Power Apps", "Power Automate",
                     "Project Insights"):
        assert expected in labels


def test_no_two_widgets_of_a_kind_share_a_label():
    at = with_request()
    for kind in ("selectbox", "text_input", "number_input", "button", "radio"):
        labels = [el.label for el in getattr(at, kind)]
        assert len(labels) == len(set(labels)), f"{kind}: {labels}"


def test_it_says_plainly_that_no_tenant_is_read():
    assert "no tenant is read" in text_of(run_app())


# ---------------------------------------------------------------------------
# Role gateway
# ---------------------------------------------------------------------------

def test_an_employee_sees_three_modules_and_a_manager_sees_six():
    assert "3/6" in text_of(as_role(ROLE_EMPLOYEE))
    assert "6/6" in text_of(as_role(ROLE_MANAGER))


@pytest.mark.parametrize("label", MANAGER_ONLY)
def test_a_manager_only_module_is_not_offered_to_an_employee(label):
    at = as_role(ROLE_EMPLOYEE)
    tiles = [module.label for module in MODULES]
    assert label in tiles
    body = text_of(at)
    assert "Denied" in body


def test_opening_a_manager_module_as_an_employee_is_refused_by_a_check():
    """Hiding the tile is not the control. The link still resolves."""
    at = as_role(ROLE_EMPLOYEE)
    widget(at, "selectbox", "Module").set_value("approvals").run()
    body = text_of(at)
    assert "Refused" in body
    assert "the link still resolves" in body
    assert "SG-Intranet-Managers" in body


def test_the_same_module_opens_for_a_manager():
    at = as_role(ROLE_MANAGER)
    widget(at, "selectbox", "Module").set_value("approvals").run()
    body = text_of(at)
    assert "Opened" in body
    assert "PTO Requests" in body


def test_the_matrix_shows_every_module_with_its_group():
    body = text_of(run_app())
    for module in MODULES:
        assert module.label in body
        assert module.entra_group in body


# ---------------------------------------------------------------------------
# Power Apps
# ---------------------------------------------------------------------------

def test_a_clean_request_is_submitted_and_holds_its_days():
    at = with_request(days=3.0)
    assert "Submitted" in text_of(at)
    assert state_of(at)["pto"].balance(EMPLOYEE).pending == 3.0


def test_the_balance_on_screen_drops_by_what_was_requested():
    at = with_request(days=4.0)
    body = text_of(at)
    assert f"{ANNUAL_ALLOWANCE - 4:.1f}" in body
    assert "Held by pending requests" in body


def test_a_request_beyond_the_balance_is_refused_with_the_numbers():
    at = with_request(days=30.0)
    body = text_of(at)
    assert "EXCEEDS_BALANCE" in body
    assert "25.0 available" in body


def test_a_second_request_sees_what_the_first_one_already_held():
    at = with_request(days=20.0, start="2026-10-05")
    at = with_request(days=10.0, start="2026-11-16", at=at)
    body = text_of(at)
    assert "already waiting for approval" in body
    assert state_of(at)["pto"].balance(EMPLOYEE).pending == 20.0


def test_a_date_in_the_past_is_refused():
    at = with_request(start="2026-09-01")
    assert "STARTS_IN_THE_PAST" in text_of(at)


def test_a_long_request_at_short_notice_is_refused():
    at = with_request(days=10.0, start="2026-09-20")
    body = text_of(at)
    assert "NOT_ENOUGH_NOTICE" in body
    assert "14 days of notice" in body


def test_resetting_clears_the_session():
    at = press(with_request(), "Reset this session")
    assert state_of(at)["pto"].requests == []
    assert state_of(at)["last_pto"] is None


def test_hours_are_logged_and_the_week_total_moves():
    at = run_app()
    at = press(at, "Log the hours")
    body = text_of(at)
    assert "Logged" in body
    assert state_of(at)["timesheet"].week_total(EMPLOYEE.upn, TODAY) == 7.5


def test_a_closed_project_cannot_be_billed_to_from_the_form():
    at = run_app()
    widget(at, "selectbox", "Project").set_value("PRJ-109").run()
    at = press(at, "Log the hours")
    body = text_of(at)
    assert "PROJECT_CLOSED" in body
    assert "never reach an invoice" in body


def test_hours_finer_than_a_quarter_are_refused():
    at = run_app()
    widget(at, "number_input", "Hours").set_value(1.1).run()
    at = press(at, "Log the hours")
    assert "NOT_A_QUARTER_HOUR" in text_of(at)


# ---------------------------------------------------------------------------
# Power Automate
# ---------------------------------------------------------------------------

def test_nothing_is_waiting_before_a_request_exists():
    assert "Nothing is waiting for a decision" in text_of(run_app())


def test_a_manager_approves_and_the_status_moves():
    at = with_request()
    at = press(at, "Approve")
    body = text_of(at)
    assert "Flow completed" in body
    assert state_of(at)["pto"].requests[0].status == STATUS_APPROVED


def test_the_run_history_names_the_connectors():
    at = press(with_request(), "Approve")
    body = text_of(at)
    assert "Start and wait for an approval" in body
    assert "Office 365 Outlook" in body
    assert "Microsoft Teams" in body


def test_rejecting_returns_the_days_to_the_balance():
    at = with_request(days=5.0)
    at = press(at, "Reject")
    assert state_of(at)["pto"].requests[0].status == STATUS_REJECTED
    assert state_of(at)["pto"].balance(EMPLOYEE).available == ANNUAL_ALLOWANCE


def test_approving_as_the_employee_is_refused_by_the_flow():
    at = with_request()
    widget(at, "selectbox", "Responding as").set_value(EMPLOYEE.name).run()
    at = press(at, "Approve")
    body = text_of(at)
    assert "Refused by the flow" in body
    assert "Only a manager may approve" in body
    assert state_of(at)["pto"].requests[0].status == STATUS_PENDING


def test_a_refused_decision_still_shows_the_flow_that_delivered_it():
    at = with_request()
    widget(at, "selectbox", "Responding as").set_value(EMPLOYEE.name).run()
    at = press(at, "Approve")
    assert "When an item is created" in text_of(at)


# ---------------------------------------------------------------------------
# Project insights
# ---------------------------------------------------------------------------

def test_the_insight_module_is_refused_to_an_employee():
    at = as_role(ROLE_EMPLOYEE)
    body = text_of(at)
    assert "Switch the role at the top of the page" in body
    assert "not a hidden tile" in body


def test_a_manager_gets_the_executive_summary():
    body = text_of(as_role(ROLE_MANAGER))
    assert "projects needs a decision this week" in body
    assert "hours billed" in body
    assert "PRJ-124" in body


def test_the_summary_names_what_to_do_this_week():
    body = text_of(as_role(ROLE_MANAGER))
    assert "review scope with Halden Utilities" in body


def test_the_closed_project_is_left_out_of_the_report():
    body = text_of(as_role(ROLE_MANAGER))
    assert "PRJ-118" in body
    assert "Migration wave one" not in body


def test_the_prompt_that_would_be_sent_to_claude_is_shown():
    body = text_of(as_role(ROLE_MANAGER))
    assert "Use only the figures below" in body
    assert "No document content does" in body


def test_a_project_too_new_to_read_is_said_so_rather_than_flagged():
    body = text_of(as_role(ROLE_MANAGER))
    assert "too new to read" in body


def test_no_dash_characters_reach_the_screen():
    body = text_of(as_role(ROLE_MANAGER))
    assert "—" not in body
    assert "–" not in body
