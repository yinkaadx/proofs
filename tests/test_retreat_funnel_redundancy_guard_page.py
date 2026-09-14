"""Page tests for the Retreat Funnel Redundancy Guard, via AppTest.

Written for pytest.

Run: pytest tests/test_retreat_funnel_redundancy_guard_page.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.retreat_funnel_redundancy_guard.core import (  # noqa: E402
    ALERT_IN_TIME,
    ALERT_TOO_LATE,
    CHANNEL_CRITICAL,
    FAILURE_MODES,
    FAILURE_NONE,
    READY_BROWSING,
    READY_NOW,
    SOURCE_ADS,
    SOURCE_REFERRAL,
    TAG_PRIORITY_CALL,
    TAG_REFERRAL_CREDIT,
)
from tools.retreat_funnel_redundancy_guard.page import STATE  # noqa: E402

HARNESS = (Path(__file__).resolve().parent
           / "_page_harness_retreat_funnel_redundancy_guard.py")

ZOOM = "ZOOM_LINK_SYNC_FAILED"
TAGS = "TAGS_NOT_APPLIED"


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


def run_app(timeout: int = 120) -> AppTest:
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


def with_lead(source: str | None = None,
              readiness: str | None = None) -> AppTest:
    at = run_app()
    if source:
        widget(at, "selectbox", "Source").set_value(source).run()
    if readiness:
        widget(at, "selectbox", "Readiness").set_value(readiness).run()
    return press(at, "Generate inbound lead")


def piped(failure: str = FAILURE_NONE, retries: int | None = None,
          minutes: int | None = None, at: AppTest | None = None) -> AppTest:
    at = at or with_lead()
    widget(at, "selectbox", "Inject a failure").set_value(failure).run()
    if retries is not None:
        widget(at, "number_input", "Retries per step").set_value(retries).run()
    if minutes is not None:
        widget(at, "number_input", "Minutes to webinar").set_value(minutes).run()
    return press(at, "Run the pipeline")


def state_of(at: AppTest) -> dict:
    return at.session_state[STATE]


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------

def test_the_page_renders_with_no_exception():
    at = run_app()
    assert not at.exception, [str(e.value) for e in at.exception]


def test_the_hero_and_the_three_tabs_are_present():
    at = run_app()
    assert "Retreat Funnel Redundancy Guard" in text_of(at)
    labels = [tab.label for tab in at.tabs]
    for expected in ("Lead Simulator", "Redundancy Guard", "Metrics Dashboard"):
        assert expected in labels


def test_nothing_downstream_is_offered_before_a_lead_exists():
    at = run_app()
    body = text_of(at)
    assert "No lead yet" in body
    assert "Generate a lead on the first tab" in body
    assert [b.label for b in at.button] == ["Generate inbound lead"]


def test_no_two_widgets_of_a_kind_share_a_label():
    at = piped(ZOOM)
    for kind in ("selectbox", "number_input", "button", "slider"):
        labels = [el.label for el in getattr(at, kind)]
        assert len(labels) == len(set(labels)), f"{kind}: {labels}"


# ---------------------------------------------------------------------------
# Lead simulator
# ---------------------------------------------------------------------------

def test_generating_a_lead_shows_its_payload_and_its_tags():
    at = with_lead()
    body = text_of(at)
    assert "FG-00001" in body
    assert "contact_id" in body
    assert "retreat-costa-rica-2026" in body


def test_each_generated_lead_gets_the_next_contact_id():
    at = with_lead()
    at = press(at, "Generate inbound lead")
    assert state_of(at)["lead"].lead_id == "FG-00002"


def test_a_forced_source_and_readiness_reach_the_generated_lead():
    at = with_lead(source=SOURCE_REFERRAL, readiness=READY_BROWSING)
    lead = state_of(at)["lead"]
    assert lead.source == SOURCE_REFERRAL
    assert lead.readiness == READY_BROWSING
    assert TAG_REFERRAL_CREDIT in lead.tags


def test_a_hot_lead_is_called_out_for_the_priority_queue():
    at = with_lead(source=SOURCE_ADS, readiness=READY_NOW)
    assert TAG_PRIORITY_CALL in state_of(at)["lead"].tags
    assert "priority call queue" in text_of(at)


def test_a_browsing_lead_is_not_called_out_as_priority():
    at = with_lead(source=SOURCE_ADS, readiness=READY_BROWSING)
    assert "priority call queue" not in text_of(at)


def test_generating_a_new_lead_clears_the_previous_run():
    at = piped(ZOOM)
    assert state_of(at)["run"] is not None
    at = press(at, "Generate inbound lead")
    assert state_of(at)["run"] is None
    assert "Run the pipeline to see the guard work" in text_of(at)


# ---------------------------------------------------------------------------
# Redundancy guard
# ---------------------------------------------------------------------------

def test_a_clean_run_reports_every_webhook_delivered():
    at = piped(FAILURE_NONE)
    body = text_of(at)
    assert "Every step was delivered" in body
    assert "there is nothing to escalate" in body
    assert state_of(at)["run"].delivered is True
    assert all(result.ok for result in state_of(at)["run"].results)


def test_the_failure_chosen_on_screen_is_the_one_actually_injected():
    """The selector formats its options, and a formatted label reaching the
    engine instead of the code would silently run a clean pipeline."""
    for mode in FAILURE_MODES:
        at = piped(mode.code, retries=0)
        run = state_of(at)["run"]
        assert run.failure is not None, mode.code
        assert run.failure.code == mode.code
        assert run.broken_step.step.key == mode.step_key


def test_a_zoom_failure_stops_the_run_and_raises_a_slack_alert():
    at = piped(ZOOM, minutes=90)
    body = text_of(at)
    assert "Zoom link sync failed" in body
    assert CHANNEL_CRITICAL in body
    assert ALERT_IN_TIME in body
    assert '"blocks"' in body


def test_the_slack_payload_names_the_manual_fallback():
    body = text_of(piped(ZOOM))
    assert "Do this now" in body
    assert "by hand" in body


def test_an_alert_with_no_time_left_is_marked_too_late():
    body = text_of(piped(ZOOM, minutes=0))
    assert ALERT_TOO_LATE in body
    assert "is a record, not a rescue" in body


def test_a_transient_failure_recovers_and_raises_nothing():
    at = piped(TAGS, retries=3)
    run = state_of(at)["run"]
    assert run.delivered is True
    body = text_of(at)
    assert "recovered" in body.lower()
    assert CHANNEL_CRITICAL not in body


def test_switching_retries_off_turns_a_recoverable_failure_into_an_alert():
    at = piped(TAGS, retries=0)
    assert state_of(at)["run"].delivered is False
    assert "Retries are switched off" in text_of(at)


def test_the_step_table_lists_every_webhook_in_order():
    body = text_of(piped(ZOOM))
    for label in ("Contact created", "Tags applied", "Webinar registration",
                  "Zoom link sync", "Confirmation email and SMS",
                  "Reminder sequence scheduled", "Offer and booking link"):
        assert label in body


# ---------------------------------------------------------------------------
# Metrics dashboard
# ---------------------------------------------------------------------------

def test_the_dashboard_opens_on_a_cohort_with_a_show_up_problem():
    body = text_of(run_app())
    assert "Show up rate" in body
    assert "Critical" in body


def test_a_show_up_rate_below_target_is_flagged_with_the_people_it_cost():
    at = run_app()
    widget(at, "number_input", "Registrations").set_value(400).run()
    widget(at, "number_input", "Showed up").set_value(100).run()
    body = text_of(at)
    assert "Show up rate" in body
    assert "never arrived" in body


def test_a_healthy_cohort_is_not_flagged_at_all():
    at = run_app()
    for label, value in (("Registrations", 400), ("Opens", 200), ("Clicks", 40),
                         ("Showed up", 200), ("Offers made", 180),
                         ("Seats closed", 30)):
        widget(at, "number_input", label).set_value(value).run()
    body = text_of(at)
    assert "Every metric is above target" in body


def test_raising_the_target_flags_a_rate_that_passed_a_moment_earlier():
    at = run_app()
    for label, value in (("Registrations", 400), ("Opens", 200), ("Clicks", 40),
                         ("Showed up", 200), ("Offers made", 180),
                         ("Seats closed", 30)):
        widget(at, "number_input", label).set_value(value).run()
    assert "Every metric is above target" in text_of(at)

    widget(at, "slider", "Show up rate target").set_value(0.70).run()
    body = text_of(at)
    assert "Every metric is above target" not in body
    assert "never arrived" in body


def test_the_stage_table_is_on_screen_with_its_drops():
    body = text_of(run_app())
    assert "Lost from previous stage" in body
    assert "Registered" in body
    assert "Closed" in body


def test_an_empty_funnel_does_not_crash_the_dashboard():
    at = run_app()
    for label in ("Opens", "Clicks", "Showed up", "Offers made", "Seats closed"):
        widget(at, "number_input", label).set_value(0).run()
    assert not at.exception, [str(e.value) for e in at.exception]


@pytest.mark.parametrize("failure", [FAILURE_NONE, ZOOM, TAGS])
def test_no_dash_characters_reach_the_screen(failure):
    body = text_of(piped(failure, retries=0))
    assert "—" not in body
    assert "–" not in body
