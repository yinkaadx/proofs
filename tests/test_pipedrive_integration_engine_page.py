"""UI tests for the Pipedrive API and Integration Console, via Streamlit's
AppTest.

Pass and fail markers are declared before execution. Every scenario asserts the
script ran with zero uncaught exceptions and that the named content rendered,
and every result is parsed programmatically rather than read.

Run either way:
    python3 -m pytest tests/test_pipedrive_integration_engine_page.py -q
    python3 tests/test_pipedrive_integration_engine_page.py

Pass marker under pytest: every test passes, zero errors, exit code 0.
Pass marker under python3: final line is exactly
"PIPEDRIVE UI RESULT: PASS <n>/<n>" and exit code 0.
Fail marker: any line starting "FAIL", plus exit code 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from streamlit.testing.v1 import AppTest  # noqa: E402

from tools.pipedrive_integration_engine.core import (  # noqa: E402
    SAMPLE_DEALS,
    SAMPLE_PEOPLE,
    ledger_rows,
)

HARNESS = Path(__file__).resolve().parent / "_page_harness_pipedrive_integration_engine.py"


def run(timeout: int = 120) -> AppTest:
    at = AppTest.from_file(str(HARNESS), default_timeout=timeout)
    at.run()
    return at


def text_of(at: AppTest) -> str:
    """Everything the page put on screen, tables included.

    Tables carry the ledger and the consent book, so their cells count as
    rendered content. Without them a table could go empty unnoticed.
    """
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


def widget(at: AppTest, kind: str, label: str):
    for el in getattr(at, kind):
        if el.label == label:
            return el
    raise AssertionError(f"no {kind} labelled {label!r}; "
                         f"available: {[e.label for e in getattr(at, kind)]}")


def errors(at: AppTest) -> list[str]:
    return [str(e.value) for e in at.exception]


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------

def test_the_page_renders_with_no_exception():
    at = run()
    assert not at.exception, f"page raised: {errors(at)}"
    body = text_of(at)
    assert "Pipedrive API and Integration Console" in body
    assert len(at.tabs) == 5, f"expected five tabs, got {len(at.tabs)}"


def test_the_executive_summary_states_the_zapier_position():
    body = text_of(run())
    assert "Zapier steps in the path" in body
    assert "Direct, what this console does" in body
    assert "Through Zapier, for comparison" in body
    assert "1,200" in body, "the monthly Zapier task count is shown"


def test_the_cold_start_dispatch_is_already_built():
    body = text_of(run())
    assert "Dispatched to Sinch" in body
    assert "https://sms.api.sinch.com/xms/v1/" in body
    assert "+447700900412" in body
    assert "Zapier used: no" in body


def test_no_api_token_is_ever_printed_in_clear():
    body = text_of(run())
    for secret in ("sinch_live_7c41f9e2a8b3", "pd_live_9f22a1c47b6e"):
        assert secret not in body, f"a live token reached the page: {secret}"
    assert "****" in body


# ---------------------------------------------------------------------------
# Direct webhook router
# ---------------------------------------------------------------------------

def test_firing_a_stage_change_produces_the_sinch_request():
    at = run()
    widget(at, "selectbox", "Deal").select(
        f"{SAMPLE_DEALS[1].deal_id} {SAMPLE_DEALS[1].title}").run()
    widget(at, "selectbox", "New stage").select("Qualified").run()
    widget(at, "button", "Fire the stage change webhook").click().run()
    assert not at.exception, f"firing raised: {errors(at)}"
    body = text_of(at)
    assert "Dispatched to Sinch" in body
    assert "+447700900518" in body, "the second person's number was used"
    assert '"client_reference": "PD-D-9002:PD-P-4402"' in body


def test_a_stage_with_no_template_sends_nothing():
    at = run()
    widget(at, "selectbox", "New stage").select("Lost").run()
    widget(at, "button", "Fire the stage change webhook").click().run()
    assert not at.exception, f"firing raised: {errors(at)}"
    body = text_of(at)
    assert "Blocked before dispatch" in body
    assert "No message template is mapped to that stage." in body
    assert "Nothing was sent, and that is the correct outcome here." in body


def test_the_webhook_body_shown_is_the_version_two_shape():
    body = text_of(run())
    assert '"action": "change"' in body
    assert '"entity": "deal"' in body
    assert '"previous"' in body


# ---------------------------------------------------------------------------
# Two way AI SMS thread and handover
# ---------------------------------------------------------------------------

def test_an_inbound_message_is_answered_against_the_deal():
    at = run()
    widget(at, "button", "Send as the customer").click().run()
    assert not at.exception, f"sending raised: {errors(at)}"
    body = text_of(at)
    assert "How much is this going to cost?" in body
    assert "Fabrikam depot hardware refresh" in body
    assert "16,400.00" in body
    assert "Pricing question" in body
    assert "The assistant is still handling this thread." in body


def test_asking_for_a_human_flags_the_handover_on_the_page():
    at = run()
    widget(at, "button", "Ask for a human").click().run()
    assert not at.exception, f"handover raised: {errors(at)}"
    body = text_of(at)
    assert "Sales rep handover flagged." in body
    assert "The customer asked for a human." in body
    assert "/api/v2/activities" in body, "the Pipedrive call activity is shown"
    assert '"type": "call"' in body
    assert '"person_id": "PD-P-4402"' in body


def test_the_thread_carries_the_person_and_deal_ids():
    at = run()
    widget(at, "button", "Send as the customer").click().run()
    body = text_of(at)
    assert "PD-P-4402" in body
    assert "PD-D-9002" in body


def test_a_typed_message_is_escaped_not_rendered_as_live_html():
    at = run()
    hostile = '<img src=x onerror="window.__probe=1">hello'
    widget(at, "text_input", "Customer message").set_value(hostile).run()
    widget(at, "button", "Send as the customer").click().run()
    assert not at.exception, f"hostile input raised: {errors(at)}"
    rendered = "\n".join(str(getattr(el, "value", "")) for el in at.markdown)
    assert "<img src=x onerror=" not in rendered
    assert "&lt;img" in rendered


def test_resetting_the_thread_empties_it():
    at = run()
    widget(at, "button", "Send as the customer").click().run()
    widget(at, "button", "Reset this thread").click().run()
    assert not at.exception, f"reset raised: {errors(at)}"
    assert "Nothing in the thread yet." in text_of(at)


# ---------------------------------------------------------------------------
# Opt out synchronizer
# ---------------------------------------------------------------------------

def test_a_stop_unsubscribes_the_person_on_the_page():
    at = run()
    widget(at, "button", "Process the inbound message").click().run()
    assert not at.exception, f"opt out raised: {errors(at)}"
    body = text_of(at)
    assert "Consent changed" in body
    assert "unsubscribed" in body
    assert "marketing_status" in body
    assert "PATCH" in body
    assert "Blocked" in body, "the consent table marks dispatch as blocked"


def test_a_dispatch_after_a_stop_is_refused():
    at = run()
    widget(at, "button", "Process the inbound message").click().run()
    widget(at, "button", "Try to dispatch to this person").click().run()
    assert not at.exception, f"blocked dispatch raised: {errors(at)}"
    body = text_of(at)
    assert "was refused" in body
    assert "The person is unsubscribed in Pipedrive, so nothing was sent." in body
    assert "Nothing reached Sinch, so nothing reached the handset." in body


def test_a_message_that_is_not_a_keyword_leaves_consent_alone():
    at = run()
    widget(at, "text_input", "Inbound message text").set_value(
        "thanks, that is all clear").run()
    widget(at, "button", "Process the inbound message").click().run()
    assert not at.exception, f"non keyword raised: {errors(at)}"
    body = text_of(at)
    assert "No change" in body
    assert "so consent is unchanged" in body


def test_the_already_unsubscribed_person_is_shown_as_blocked_from_the_start():
    body = text_of(run())
    assert "Tom Beaufort" in body
    assert "Blocked" in body


# ---------------------------------------------------------------------------
# Power BI ledger
# ---------------------------------------------------------------------------

def test_the_ledger_renders_every_row_with_its_hash():
    body = text_of(run())
    assert "fact_deal_state" in body
    for row in ledger_rows():
        assert row.change_hash in body, f"missing hash for {row.deal_id}"
    assert "Rows skipped, hash unchanged" in body
    assert "an incremental run would transfer nothing at all" in body


def test_changing_a_deal_moves_exactly_one_row():
    at = run()
    widget(at, "number_input", "New deal value").set_value(60000.0).run()
    widget(at, "button", "Apply the change").click().run()
    assert not at.exception, f"applying a change raised: {errors(at)}"
    body = text_of(at)
    assert "1 row hash moved since the last refresh" in body
    assert "60,000.00" in body


def test_the_ledger_resets_back_to_an_unchanged_table():
    at = run()
    widget(at, "number_input", "New deal value").set_value(60000.0).run()
    widget(at, "button", "Apply the change").click().run()
    widget(at, "button", "Reset the ledger").click().run()
    assert not at.exception, f"resetting raised: {errors(at)}"
    assert "an incremental run would transfer nothing at all" in text_of(at)


def test_stage_durations_and_rep_metrics_render():
    body = text_of(run())
    assert "Stage durations feeding the report" in body
    assert "Sales rep metrics" in body
    for rep in {d.owner for d in SAMPLE_DEALS}:
        assert rep in body
    assert "Days in stage" in body


def test_both_exports_are_offered():
    at = run()
    labels = {el.label for el in at.get("download_button")}
    assert "Download the full table as CSV" in labels
    assert "Download this incremental batch as JSON" in labels


def test_the_dataset_manifest_names_the_incremental_policy():
    body = text_of(run())
    assert '"watermark_column": "updated_at"' in body
    assert '"change_column": "change_hash"' in body
    assert "incremental, hourly, on the watermark" in body


# ---------------------------------------------------------------------------
# House style
# ---------------------------------------------------------------------------

def test_the_page_prose_uses_no_em_or_en_dashes():
    body = text_of(run())
    # Spelled by code point so the detector itself carries no dash.
    assert chr(8212) not in body, "an em dash reached the page"
    assert chr(8211) not in body, "an en dash reached the page"


def test_every_sample_person_appears_in_the_consent_book():
    body = text_of(run())
    for person in SAMPLE_PEOPLE:
        assert person.name in body, f"{person.name} is missing from the page"


if __name__ == "__main__":
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failures: list[str] = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            print(f"  FAIL {name}: {exc}")
            failures.append(name)
    print()
    if failures:
        print(f"PIPEDRIVE UI RESULT: FAIL {len(failures)} of {len(tests)} checks failed")
        for name in failures:
            print(f"  - {name}")
        sys.exit(1)
    print(f"PIPEDRIVE UI RESULT: PASS {len(tests)}/{len(tests)}")
    sys.exit(0)
