"""Page tests for the Airtable WhatsApp Automation Guard, via AppTest.

Written for pytest.

Run: pytest tests/test_airtable_whatsapp_automation_guard_page.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.airtable_whatsapp_automation_guard.core import (  # noqa: E402
    BLOCKED,
    DUPLICATE,
    FAILED,
    PHONE_CLEAN,
    PHONE_EMPTY,
    PHONE_LETTERS,
    PHONE_LOCAL,
    PHONE_SHORT,
    QUEUED,
    SENT,
    SKIPPED,
    STATUS_CANCELLED,
    STATUS_READY,
    TWILIO_ERRORS,
)
from tools.airtable_whatsapp_automation_guard.page import (  # noqa: E402
    PHONE_LABELS,
    STATE,
)

HARNESS = (Path(__file__).resolve().parent
           / "_page_harness_airtable_whatsapp_automation_guard.py")

GENERATE = "Generate a record"
RETRY = "Retry the same record"
CLEAR = "Clear the ledger"


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


def generated(phone: str = PHONE_LOCAL, status: str = STATUS_READY,
              response: str | None = None, within: bool | None = None,
              at: AppTest | None = None) -> AppTest:
    at = at or run_app()
    widget(at, "selectbox", "Phone shape").set_value(phone).run()
    widget(at, "selectbox", "Record status").set_value(status).run()
    if response is not None:
        widget(at, "selectbox", "Twilio response").set_value(response).run()
    if within is not None:
        widget(at, "checkbox",
               "Inside the twenty four hour window").set_value(within).run()
    return press(at, GENERATE)


def state_of(at: AppTest) -> dict:
    return at.session_state[STATE]


def outcomes(at: AppTest) -> list[str]:
    return [result.outcome for result in state_of(at)["ledger"].results]


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------

def test_the_page_renders_with_no_exception():
    at = run_app()
    assert not at.exception, [str(e.value) for e in at.exception]


def test_the_hero_and_the_four_tabs_are_present():
    at = run_app()
    assert "Airtable WhatsApp Automation Guard" in text_of(at)
    labels = [tab.label for tab in at.tabs]
    for expected in ("Webhook Simulator", "Twilio Router", "Ledger and Alerts",
                     "Whole Batch"):
        assert expected in labels


def test_nothing_downstream_is_offered_before_a_record_exists():
    at = run_app()
    body = text_of(at)
    assert "No record yet" in body
    assert "Generate a record on the first tab" in body
    assert state_of(at)["record"] is None


def test_the_retry_button_is_disabled_until_there_is_something_to_retry():
    at = run_app()
    assert widget(at, "button", RETRY).proto.disabled is True


def test_no_two_widgets_of_a_kind_share_a_label():
    at = generated()
    for kind in ("selectbox", "checkbox", "button"):
        labels = [el.label for el in getattr(at, kind)]
        assert len(labels) == len(set(labels)), f"{kind}: {labels}"


def test_it_says_plainly_that_no_message_is_ever_sent():
    assert "no message is ever sent" in text_of(run_app())


# ---------------------------------------------------------------------------
# Webhook simulator
# ---------------------------------------------------------------------------

def test_generating_a_record_shows_its_airtable_payload():
    at = generated()
    body = text_of(at)
    assert '"createdTime"' in body
    assert '"Phone Number"' in body
    assert state_of(at)["record"] is not None


def test_a_clean_record_sends():
    at = generated()
    assert outcomes(at) == [SENT]
    assert SENT in text_of(at)


def test_the_normalisation_table_shows_what_changed():
    body = text_of(generated(PHONE_LOCAL))
    assert "+2348031234567" in body
    assert "As typed in Airtable" in body


def test_a_number_already_in_e164_is_reported_as_unchanged():
    assert "Already in E.164" in text_of(generated(PHONE_CLEAN))


@pytest.mark.parametrize("phone", [PHONE_LETTERS, PHONE_EMPTY, PHONE_SHORT])
def test_every_broken_phone_shape_is_blocked_before_twilio(phone):
    at = generated(phone)
    assert outcomes(at) == [BLOCKED]
    assert "No request built" in text_of(at)


def test_a_cancelled_record_is_left_alone():
    at = generated(PHONE_CLEAN, STATUS_CANCELLED)
    assert outcomes(at) == [SKIPPED]
    assert "only Ready to notify sends" in text_of(at)


def test_retrying_the_same_record_is_suppressed():
    """What Make does after a scenario times out having already succeeded."""
    at = generated()
    at = press(at, RETRY)
    assert outcomes(at) == [SENT, DUPLICATE]
    assert "gets it twice" in text_of(at)


def test_ten_retries_still_send_once():
    at = generated()
    for _ in range(10):
        at = press(at, RETRY)
    assert outcomes(at).count(SENT) == 1
    assert outcomes(at).count(DUPLICATE) == 10


def test_clearing_the_ledger_starts_again():
    at = press(generated(), CLEAR)
    assert state_of(at)["ledger"].results == []
    assert state_of(at)["record"] is None
    assert "No record yet" in text_of(at)


def test_each_generated_record_gets_its_own_id():
    at = generated()
    first = state_of(at)["record"].record_id
    at = press(at, GENERATE)
    assert state_of(at)["record"].record_id != first


# ---------------------------------------------------------------------------
# Twilio router
# ---------------------------------------------------------------------------

def test_the_request_carries_the_whatsapp_prefix_on_both_addresses():
    body = text_of(generated())
    assert "whatsapp:+2348031234567" in body
    assert "whatsapp:+14155238886" in body


def test_inside_the_window_a_free_form_body_is_shown():
    body = text_of(generated(within=True))
    assert "Free form allowed" in body
    assert "Body" in body


def test_outside_the_window_the_template_is_used_and_the_reason_is_given():
    body = text_of(generated(within=False))
    assert "Template required" in body
    assert "ContentSid" in body
    assert "63016" in body


def test_the_curl_command_is_shown_with_the_token_left_as_an_environment_variable():
    body = text_of(generated())
    assert "curl -X POST" in body
    assert "TWILIO_AUTH_TOKEN" in body


def test_the_idempotency_key_is_shown_with_what_it_is_made_of():
    body = text_of(generated())
    assert state_of(generated())["record"] is not None
    assert "record id and a digest" in body


def test_a_blocked_record_shows_no_request_at_all():
    body = text_of(generated(PHONE_LETTERS))
    assert "Nothing is sent to Twilio at all" in body
    assert "curl -X POST" not in body


# ---------------------------------------------------------------------------
# Ledger and alerts
# ---------------------------------------------------------------------------

def test_the_ledger_is_empty_until_a_record_is_processed():
    assert "The ledger fills as records are processed" in text_of(run_app())


def test_a_permanent_twilio_error_is_logged_as_do_not_retry():
    at = generated(response="21211 Invalid To number")
    assert outcomes(at) == [FAILED]
    body = text_of(at)
    assert "Do not retry" in body
    assert "21211" in body


def test_a_rate_limit_is_queued_rather_than_alerted_as_permanent():
    at = generated(response="20429 Too many requests")
    assert outcomes(at) == [QUEUED]
    assert "Retry it" in text_of(at)


def test_a_clean_run_reports_no_alerts():
    body = text_of(generated())
    assert "No alerts" in body


def test_the_sent_keys_are_listed_once_something_has_been_sent():
    body = text_of(generated())
    assert "Idempotency key" in body
    assert "SM" in body


# ---------------------------------------------------------------------------
# Whole batch
# ---------------------------------------------------------------------------

def test_the_batch_runs_every_case_without_stopping():
    body = text_of(run_app())
    assert "7 record(s), nothing raised" in body
    assert "Retry suppressed" in body


def test_the_batch_lists_the_errors_the_guard_knows():
    body = text_of(run_app())
    for error in TWILIO_ERRORS:
        assert str(error.code) in body
    assert "Only one of these is worth retrying" in body


def test_the_batch_can_be_run_outside_the_window():
    at = run_app()
    widget(at, "checkbox",
           "Run the batch inside the twenty four hour window").set_value(False).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "7 record(s), nothing raised" in text_of(at)


def test_every_phone_shape_has_a_readable_label():
    labels = set(PHONE_LABELS.values())
    assert len(labels) == len(PHONE_LABELS)
    assert "Empty field" in labels


def test_no_dash_characters_reach_the_screen():
    body = text_of(generated(PHONE_LETTERS))
    assert "—" not in body
    assert "–" not in body
