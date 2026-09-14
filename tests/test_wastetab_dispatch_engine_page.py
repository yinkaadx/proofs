"""Page tests for the WasteTab Logistics and Financial Engine, via AppTest.

Written for pytest.

Run: pytest tests/test_wastetab_dispatch_engine_page.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.wastetab_dispatch_engine.core import (  # noqa: E402
    BOOKED,
    CANCELLED,
    COMPLETED,
    GRAB,
    NEW_ENQUIRY,
    NO_MATCH_AREA,
    NO_MATCH_WASTE,
    ON_HOLD,
    QUOTE_SENT,
    SKIP_12,
    SKIP_8,
    VALVE_PAUSED,
    VALVE_REFUSED,
    VALVE_RESUMED,
    WASTE_CONSTRUCTION,
    WASTE_GENERAL,
    WASTE_HAZARDOUS,
)
from tools.wastetab_dispatch_engine.page import STATE  # noqa: E402

HARNESS = (Path(__file__).resolve().parent
           / "_page_harness_wastetab_dispatch_engine.py")

OPEN_JOB = "Open this as a job"
SEND_QUOTE = "Send the quote"
MARK_BOOKED = "Mark booked"
MARK_COMPLETED = "Mark completed"
CANCEL = "Cancel the job"
REPORT = "Carrier reports the load"
PAY = "Customer pays the surcharge"
DECLINE = "Customer declines"
ATTEMPT = "Attempt the move"


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


def opened(postcode: str = "SE15 4RT", service: str = SKIP_8,
           waste: str = WASTE_GENERAL, at: AppTest | None = None) -> AppTest:
    at = at or run_app()
    widget(at, "selectbox", "Postcode").set_value(postcode).run()
    widget(at, "selectbox", "Service").set_value(service).run()
    widget(at, "selectbox", "Waste type").set_value(waste).run()
    return press(at, OPEN_JOB)


def booked(at: AppTest | None = None) -> AppTest:
    at = press(opened(at=at), SEND_QUOTE)
    return press(at, MARK_BOOKED)


def job_of(at: AppTest):
    return at.session_state[STATE]["job"]


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------

def test_the_page_renders_with_no_exception():
    at = run_app()
    assert not at.exception, [str(e.value) for e in at.exception]


def test_the_hero_and_the_four_tabs_are_present():
    at = run_app()
    # The hero carries the registered title, ampersand and all. AppTest
    # reports the markdown source, where that is written &amp;.
    assert "WasteTab Logistics &amp; Financial Engine" in text_of(at)
    labels = [tab.label for tab in at.tabs]
    for expected in ("Dispatch Router", "Gross Up Engine", "State Ledger",
                     "Safety Valve"):
        assert expected in labels


def test_nothing_downstream_is_offered_before_a_job_is_open():
    at = run_app()
    body = text_of(at)
    assert "No job open" in body
    assert "Open a job first" in body
    assert job_of(at) is None


def test_no_two_widgets_of_a_kind_share_a_label():
    at = opened()
    for kind in ("selectbox", "text_input", "number_input", "button"):
        labels = [el.label for el in getattr(at, kind)]
        assert len(labels) == len(set(labels)), f"{kind}: {labels}"


def test_it_says_plainly_that_no_payment_link_is_created():
    assert "no payment link is ever created" in text_of(run_app())


# ---------------------------------------------------------------------------
# Dispatch router
# ---------------------------------------------------------------------------

def test_a_london_postcode_finds_the_london_carriers():
    body = text_of(run_app())
    assert "Carriers found" in body
    assert "Thameside Clearance" in body
    assert "Meridian Waste" in body


def test_an_area_nobody_covers_is_refused_on_screen():
    at = run_app()
    widget(at, "selectbox", "Postcode").set_value("EH1 1YZ").run()
    body = text_of(at)
    assert NO_MATCH_AREA in body
    assert "held rather than dispatched" in body


def test_a_bad_postcode_is_refused_rather_than_routed():
    at = run_app()
    widget(at, "selectbox", "Postcode").set_value("not a postcode").run()
    body = text_of(at)
    assert "No dispatch" in body
    assert "held for a human" in body


def test_waste_nobody_local_is_licensed_for_is_refused():
    at = run_app()
    widget(at, "selectbox", "Waste type").set_value(WASTE_HAZARDOUS).run()
    assert NO_MATCH_WASTE in text_of(at)


def test_the_dormant_carrier_is_shown_in_the_database_but_never_dispatched():
    body = text_of(run_app())
    assert "Dormant Skips" in body
    at = run_app()
    result_rows = [r for r in text_of(at).split("\n")]
    assert "Dormant Skips" in "\n".join(result_rows)


def test_the_wordpress_payload_is_shown():
    body = text_of(run_app())
    assert '"form_id": "wastetab-enquiry"' in body
    assert '"postcode"' in body


def test_opening_a_job_starts_it_as_a_new_enquiry():
    at = opened()
    assert job_of(at).state == NEW_ENQUIRY
    assert job_of(at).postcode == "SE15 4RT"


# ---------------------------------------------------------------------------
# Gross up engine
# ---------------------------------------------------------------------------

def test_the_worked_example_is_on_screen():
    body = text_of(run_app())
    assert "£133.37" in body
    assert "£130.00" in body
    assert "£3.37" in body


def test_the_naive_link_shortfall_is_stated_with_its_cost_over_a_hundred_jobs():
    body = text_of(run_app())
    assert "£3.29" in body
    assert "£329.00" in body


def test_changing_the_bid_changes_the_link_amount():
    at = run_app()
    widget(at, "text_input", "Carrier bid in pounds").set_value("250").run()
    body = text_of(at)
    # £250 bid plus 30 percent is £325 net. (32500 + 30) / 0.977 is 33295.8,
    # so the charge is 33296, the fee is 796 and 32500 lands. Worked by hand
    # rather than read off the engine, or the test would only agree with
    # itself.
    assert "£325.00" in body
    assert "£332.96" in body


def test_changing_the_margin_changes_the_net_target():
    at = run_app()
    widget(at, "number_input", "Margin percent").set_value(50.0).run()
    body = text_of(at)
    assert "£150.00" in body


def test_a_bid_that_is_not_money_is_refused_rather_than_guessed():
    at = run_app()
    widget(at, "text_input", "Carrier bid in pounds").set_value("abc").run()
    body = text_of(at)
    assert "Refused" in body
    assert "not an amount of money" in body
    assert not at.exception, [str(e.value) for e in at.exception]


def test_a_zero_bid_still_produces_a_link_that_covers_the_fee():
    at = run_app()
    widget(at, "text_input", "Carrier bid in pounds").set_value("0").run()
    assert not at.exception, [str(e.value) for e in at.exception]


# ---------------------------------------------------------------------------
# State ledger
# ---------------------------------------------------------------------------

def test_sending_the_quote_prices_the_job_and_attaches_a_link():
    at = press(opened(), SEND_QUOTE)
    job = job_of(at)
    assert job.state == QUOTE_SENT
    assert job.quote is not None
    assert job.carrier.code == "CAR-01"
    assert len(job.links) == 1


def test_the_quoted_link_leaves_the_net_the_margin_needs():
    at = press(opened(), SEND_QUOTE)
    job = job_of(at)
    assert job.quote.net_received_pence == job.quote.net_target_pence


def test_booking_records_the_charge_on_the_ledger():
    at = booked()
    job = job_of(at)
    assert job.state == BOOKED
    assert job.charged_pence == job.quote.charge_pence


def test_the_job_runs_through_to_completed():
    at = press(booked(), MARK_COMPLETED)
    assert job_of(at).state == COMPLETED


def test_an_illegal_move_is_refused_by_the_engine_not_only_by_a_disabled_button():
    at = opened()
    widget(at, "selectbox", "Move to").set_value(COMPLETED).run()
    at = press(at, ATTEMPT)
    body = text_of(at)
    assert "Refused" in body
    assert "may only go to" in body
    assert job_of(at).state == NEW_ENQUIRY


def test_a_finished_job_refuses_any_further_move():
    at = press(booked(), MARK_COMPLETED)
    widget(at, "selectbox", "Move to").set_value(BOOKED).run()
    at = press(at, ATTEMPT)
    assert "does not move again" in text_of(at)


def test_cancelling_is_allowed_from_an_enquiry():
    at = press(opened(), CANCEL)
    assert job_of(at).state == CANCELLED


def test_the_ledger_lists_every_move_with_who_made_it():
    at = booked()
    body = text_of(at)
    assert QUOTE_SENT in body
    assert "operator" in body or "system" in body


# ---------------------------------------------------------------------------
# Safety valve
# ---------------------------------------------------------------------------

def test_the_valve_says_nothing_before_anything_is_reported():
    at = booked()
    assert "Nothing reported yet" in text_of(at)


def test_materially_different_waste_pauses_the_job_and_raises_a_surcharge():
    at = booked()
    widget(at, "selectbox",
           "What the carrier found on site").set_value(WASTE_CONSTRUCTION).run()
    at = press(at, REPORT)
    body = text_of(at)
    assert VALVE_PAUSED in body
    assert job_of(at).state == ON_HOLD
    assert "£86.80" in body       # the grossed up surcharge


def test_paying_the_surcharge_resumes_the_collection():
    at = booked()
    widget(at, "selectbox",
           "What the carrier found on site").set_value(WASTE_CONSTRUCTION).run()
    at = press(at, REPORT)
    at = press(at, PAY)
    assert VALVE_RESUMED in text_of(at)
    assert job_of(at).state == BOOKED


def test_declining_the_surcharge_cancels_the_job():
    at = booked()
    widget(at, "selectbox",
           "What the carrier found on site").set_value(WASTE_HAZARDOUS).run()
    at = press(at, REPORT)
    at = press(at, DECLINE)
    assert VALVE_REFUSED in text_of(at)
    assert job_of(at).state == CANCELLED


def test_the_same_waste_as_quoted_does_not_stop_the_van():
    at = booked()
    widget(at, "selectbox",
           "What the carrier found on site").set_value(WASTE_GENERAL).run()
    at = press(at, REPORT)
    assert job_of(at).state == BOOKED


def test_the_supplementary_link_is_shown_with_its_fee_and_net():
    at = booked()
    widget(at, "selectbox",
           "What the carrier found on site").set_value(WASTE_CONSTRUCTION).run()
    at = press(at, REPORT)
    body = text_of(at)
    assert '"reference"' in body
    assert '"net"' in body
    assert "pay.wastetab.example" in body


def test_no_dash_characters_reach_the_screen():
    at = booked()
    widget(at, "selectbox",
           "What the carrier found on site").set_value(WASTE_CONSTRUCTION).run()
    at = press(at, REPORT)
    body = text_of(at)
    assert "—" not in body
    assert "–" not in body
