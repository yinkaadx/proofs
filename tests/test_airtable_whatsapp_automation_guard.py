"""Engine tests for the Airtable WhatsApp Automation Guard.

Written for pytest. Deterministic throughout: the random source is injected and
nothing reads the clock.

Run: pytest tests/test_airtable_whatsapp_automation_guard.py
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.airtable_whatsapp_automation_guard.core import (  # noqa: E402
    APPOINTMENT_TEMPLATE,
    BLOCKED,
    DEFAULT_COUNTRY_CODE,
    DUPLICATE,
    E164_MAX_DIGITS,
    ERR_PHONE_EMPTY,
    ERR_PHONE_LETTERS,
    ERR_PHONE_LONG,
    ERR_PHONE_SHORT,
    ERROR_NONE,
    FAILED,
    MIN_NATIONAL_DIGITS,
    PHONE_BRACKETED,
    PHONE_CLEAN,
    PHONE_DOUBLE_ZERO,
    PHONE_EMPTY,
    PHONE_LETTERS,
    PHONE_LOCAL,
    PHONE_SHORT,
    PHONE_SPACED,
    PHONE_STYLES,
    QUEUED,
    SENT,
    SKIPPED,
    STATUS_CANCELLED,
    STATUS_HOLD,
    STATUS_NOTIFIED,
    STATUS_READY,
    STATUSES,
    TWILIO_ERRORS,
    TWILIO_FROM,
    AirtableRecord,
    AutomationLedger,
    ContentTemplate,
    build_request,
    error_by_code,
    generate_record,
    idempotency_key,
    kpi_counts,
    message_sid,
    normalise_phone,
    process_record,
    run_batch,
    sample_batch,
)

NOW = "2026-09-13T09:00:00.000Z"


def record(phone: str = PHONE_LOCAL, status: str = STATUS_READY,
           record_id: str = "recA1b2C3d4E5f6G",
           name: str = "Amara Okafor") -> AirtableRecord:
    return AirtableRecord(record_id, name, phone, status,
                          "2026-09-15 at 10:30", NOW)


# ---------------------------------------------------------------------------
# Payload generation
# ---------------------------------------------------------------------------

def test_the_same_seed_generates_the_same_record():
    first = generate_record(random.Random(7), 1, NOW)
    second = generate_record(random.Random(7), 1, NOW)
    assert first == second


def test_a_generated_record_id_has_airtables_own_shape():
    """Seventeen characters starting with rec. The ledger is keyed on it, so a
    shorter stand in would hide a collision the real ids cannot have."""
    generated = generate_record(random.Random(3), 1, NOW)
    assert generated.record_id.startswith("rec")
    assert len(generated.record_id) == 17
    assert generated.record_id[3:].isalnum()


def test_generated_records_do_not_collide():
    rng = random.Random(1)
    ids = {generate_record(rng, index, NOW).record_id for index in range(200)}
    assert len(ids) == 200


def test_a_forced_phone_and_status_are_honoured():
    generated = generate_record(random.Random(1), 1, NOW, phone=PHONE_CLEAN,
                                status=STATUS_HOLD)
    assert generated.phone == PHONE_CLEAN
    assert generated.status == STATUS_HOLD


def test_an_unforced_record_still_lands_on_real_options():
    generated = generate_record(random.Random(5), 1, NOW)
    assert generated.phone in PHONE_STYLES
    assert generated.status in STATUSES


def test_the_payload_matches_the_shape_airtable_posts():
    payload = record().as_payload()
    assert set(payload) == {"id", "createdTime", "fields"}
    assert set(payload["fields"]) == {"Name", "Phone Number", "Status",
                                      "Appointment"}
    assert payload["id"] == "recA1b2C3d4E5f6G"
    assert json.loads(json.dumps(payload))["fields"]["Status"] == STATUS_READY


def test_the_first_name_is_taken_from_the_name_field():
    assert record(name="Amara Okafor").first_name == "Amara"
    assert record(name="").first_name == "there"


# ---------------------------------------------------------------------------
# Phone normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [PHONE_CLEAN, PHONE_LOCAL, PHONE_SPACED,
                                 PHONE_BRACKETED, PHONE_DOUBLE_ZERO])
def test_every_shape_a_person_types_normalises_to_the_same_number(raw):
    result = normalise_phone(raw)
    assert result.ok is True
    assert result.e164 == "+2348031234567"


def test_a_number_already_in_e164_is_left_alone():
    result = normalise_phone(PHONE_CLEAN)
    assert result.changed is False


def test_a_number_that_had_to_be_rewritten_says_so():
    assert normalise_phone(PHONE_LOCAL).changed is True


def test_an_empty_field_is_refused_rather_than_guessed():
    result = normalise_phone(PHONE_EMPTY)
    assert result.ok is False
    assert result.error_code == ERR_PHONE_EMPTY
    assert "fixing in Airtable" in result.message


def test_letters_in_the_field_are_refused():
    result = normalise_phone(PHONE_LETTERS)
    assert result.ok is False
    assert result.error_code == ERR_PHONE_LETTERS
    assert "every time" in result.message


def test_a_truncated_local_number_is_caught_despite_the_country_code():
    """Prefixing the country code pushes a six digit fragment over the global
    minimum, so the national part has to be checked on its own."""
    result = normalise_phone(PHONE_SHORT)
    assert result.ok is False
    assert result.error_code == ERR_PHONE_SHORT
    assert "truncated" in result.message


def test_the_national_length_gate_turns_where_it_should():
    short = "0" + "8" * (MIN_NATIONAL_DIGITS - 1)
    fine = "0" + "8" * MIN_NATIONAL_DIGITS
    assert normalise_phone(short).ok is False
    assert normalise_phone(fine).ok is True


def test_a_number_longer_than_e164_allows_is_refused():
    result = normalise_phone("+" + "9" * (E164_MAX_DIGITS + 1))
    assert result.ok is False
    assert result.error_code == ERR_PHONE_LONG


@pytest.mark.parametrize("raw,expected", [("+12025550143", "+12025550143"),
                                          ("+4915112345678", "+4915112345678")])
def test_a_foreign_number_already_in_e164_is_kept_as_it_is(raw, expected):
    """The default country code must not be forced onto a number that already
    carries its own."""
    assert normalise_phone(raw).e164 == expected


def test_the_default_country_code_can_be_changed():
    assert normalise_phone("07700900123",
                           country_code="+44").e164 == "+447700900123"


def test_whitespace_around_a_number_is_not_a_failure():
    assert normalise_phone(f"  {PHONE_CLEAN}  ").e164 == "+2348031234567"


def test_the_country_code_constant_is_the_one_in_use():
    assert normalise_phone(PHONE_LOCAL).e164.startswith(DEFAULT_COUNTRY_CODE)


# ---------------------------------------------------------------------------
# Twilio formatting
# ---------------------------------------------------------------------------

def test_both_addresses_carry_the_whatsapp_prefix():
    """Leaving it off the To address silently sends an SMS instead, at a
    different price and possibly to a different phone."""
    request = build_request(record(), normalise_phone(PHONE_LOCAL))
    assert request.to == "whatsapp:+2348031234567"
    assert request.from_ == TWILIO_FROM
    assert request.as_form()["To"].startswith("whatsapp:")


def test_the_url_targets_the_accounts_own_messages_endpoint():
    request = build_request(record(), normalise_phone(PHONE_LOCAL))
    assert request.url.endswith("/Messages.json")
    assert request.account_sid in request.url


def test_inside_the_window_a_free_form_body_is_sent():
    form = build_request(record(), normalise_phone(PHONE_LOCAL),
                         within_window=True).as_form()
    assert "Body" in form
    assert "ContentSid" not in form
    assert "Amara" in form["Body"]


def test_outside_the_window_a_template_is_sent_and_body_is_omitted():
    """A Body outside the window comes back as 63016, so it is not sent at all
    rather than sent and ignored."""
    form = build_request(record(), normalise_phone(PHONE_LOCAL),
                         within_window=False).as_form()
    assert "Body" not in form
    assert form["ContentSid"] == APPOINTMENT_TEMPLATE.sid
    assert json.loads(form["ContentVariables"])["1"] == "Amara"


def test_the_template_renders_its_variables_in_order():
    rendered = APPOINTMENT_TEMPLATE.render({"first_name": "Chidi",
                                            "appointment": "2026-09-15"})
    assert "Hi Chidi" in rendered
    assert "2026-09-15" in rendered
    assert "{{1}}" not in rendered


def test_a_missing_variable_renders_empty_rather_than_leaving_the_placeholder():
    rendered = APPOINTMENT_TEMPLATE.render({"first_name": "Chidi"})
    assert "{{2}}" not in rendered


def test_every_request_carries_a_status_callback():
    form = build_request(record(), normalise_phone(PHONE_LOCAL)).as_form()
    assert form["StatusCallback"].startswith("https://")


def test_the_curl_form_contains_every_parameter():
    request = build_request(record(), normalise_phone(PHONE_LOCAL))
    curl = request.as_curl()
    for key in request.as_form():
        assert key in curl
    assert "TWILIO_AUTH_TOKEN" in curl
    assert "--data-urlencode" in curl


def test_a_request_cannot_be_built_from_a_number_that_did_not_normalise():
    with pytest.raises(ValueError):
        build_request(record(phone=PHONE_LETTERS),
                      normalise_phone(PHONE_LETTERS))


def test_a_custom_template_is_used_when_one_is_given():
    template = ContentTemplate("HXcustom", "reschedule_v1",
                               "Hello {{1}}, moved to {{2}}.",
                               ("first_name", "appointment"))
    request = build_request(record(), normalise_phone(PHONE_LOCAL),
                            template=template)
    assert request.content_sid == "HXcustom"
    assert request.body.startswith("Hello Amara")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_the_same_record_and_message_produce_the_same_key():
    request = build_request(record(), normalise_phone(PHONE_LOCAL))
    assert idempotency_key(record(), request) == \
        idempotency_key(record(), request)


def test_two_records_saying_the_same_thing_have_different_keys():
    """The body alone would collide between two people with the same name and
    appointment."""
    one = record(record_id="recAAAAAAAAAAAAA")
    two = record(record_id="recBBBBBBBBBBBBB")
    request = build_request(one, normalise_phone(PHONE_LOCAL))
    assert idempotency_key(one, request) != idempotency_key(two, request)


def test_a_genuinely_different_message_to_one_record_is_not_suppressed():
    """The record id alone would block a real second message."""
    reschedule = ContentTemplate("HXresched", "reschedule_v1",
                                 "Hi {{1}}, your appointment moved to {{2}}.",
                                 ("first_name", "appointment"))
    first = build_request(record(), normalise_phone(PHONE_LOCAL))
    second = build_request(record(), normalise_phone(PHONE_LOCAL),
                           template=reschedule)
    assert idempotency_key(record(), first) != idempotency_key(record(), second)


def test_a_retry_of_the_same_record_is_suppressed():
    ledger = AutomationLedger()
    first = process_record(ledger, record())
    again = process_record(ledger, record())
    assert first.outcome == SENT
    assert again.outcome == DUPLICATE
    assert again.message_sid == first.message_sid


def test_ten_retries_still_send_once():
    ledger = AutomationLedger()
    for _ in range(10):
        process_record(ledger, record())
    assert ledger.count(SENT) == 1
    assert ledger.count(DUPLICATE) == 9
    assert len(ledger.sent_keys) == 1


def test_a_message_sid_is_stable_for_a_given_key():
    assert message_sid("recX:abc") == message_sid("recX:abc")
    assert message_sid("recX:abc").startswith("SM")
    assert message_sid("recX:abc") != message_sid("recY:abc")


def test_a_send_that_failed_is_not_recorded_as_sent():
    """Otherwise a retry after a genuine failure would be suppressed and the
    customer would never hear from anyone."""
    ledger = AutomationLedger()
    failed = process_record(ledger, record(), failure_code=21211)
    assert failed.outcome == FAILED
    assert ledger.sent_keys == {}
    retried = process_record(ledger, record())
    assert retried.outcome == SENT


# ---------------------------------------------------------------------------
# Status handling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [STATUS_NOTIFIED, STATUS_HOLD,
                                    STATUS_CANCELLED])
def test_only_a_ready_record_sends(status):
    ledger = AutomationLedger()
    result = process_record(ledger, record(status=status))
    assert result.outcome == SKIPPED
    assert ledger.sent_keys == {}


def test_a_cancelled_record_is_never_messaged():
    """A scenario that ignores the status is how a cancelled booking gets a
    confirmation."""
    ledger = AutomationLedger()
    result = process_record(ledger, record(status=STATUS_CANCELLED))
    assert "Cancelled" in result.detail
    assert ledger.count(SENT) == 0


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

def test_a_bad_phone_number_is_blocked_before_twilio_is_called():
    ledger = AutomationLedger()
    result = process_record(ledger, record(phone=PHONE_LETTERS))
    assert result.outcome == BLOCKED
    assert result.request is None
    assert result.alert is True


@pytest.mark.parametrize("error", TWILIO_ERRORS, ids=[str(e.code) for e in
                                                      TWILIO_ERRORS])
def test_every_twilio_error_is_caught_and_classified(error):
    ledger = AutomationLedger()
    result = process_record(ledger, record(), failure_code=error.code)
    assert result.outcome == (QUEUED if error.retryable else FAILED)
    assert result.error is error
    assert str(error.code) in result.detail


def test_a_queued_record_is_held_apart_from_the_alerts():
    """A rate limit clears itself, so it belongs on a queue rather than in
    front of a person."""
    ledger = AutomationLedger()
    process_record(ledger, record(), failure_code=20429)
    process_record(ledger, record(phone=PHONE_EMPTY), failure_code=ERROR_NONE)
    assert [r.outcome for r in ledger.queued] == [QUEUED]
    assert [r.outcome for r in ledger.alerts] == [BLOCKED]


def test_only_the_rate_limit_is_worth_retrying():
    retryable = [error.code for error in TWILIO_ERRORS if error.retryable]
    assert retryable == [20429]


def test_a_permanent_error_says_not_to_retry_it():
    assert "Retrying fails identically" in error_by_code(21211).action


def test_an_unknown_error_code_is_treated_as_no_error():
    ledger = AutomationLedger()
    assert process_record(ledger, record(), failure_code=99999).outcome == SENT


def test_nothing_raises_even_when_the_formatter_blows_up():
    """The whole guard. A Make scenario that throws stops the run, and every
    record after the bad one is never processed."""
    class Exploding(ContentTemplate):
        def render(self, values):
            raise RuntimeError("template service unreachable")

    ledger = AutomationLedger()
    result = process_record(ledger, record(),
                            template=Exploding("HX", "boom", "x", ()))
    assert result.outcome == BLOCKED
    assert "skipped it rather than stopping the run" in result.detail
    assert "RuntimeError" in result.detail


def test_a_batch_finishes_even_when_a_record_in_the_middle_fails():
    records = [record(record_id="rec1"), record(phone=PHONE_LETTERS,
                                                record_id="rec2"),
               record(record_id="rec3")]
    ledger = run_batch(records)
    assert len(ledger.results) == 3
    assert ledger.count(SENT) == 2
    assert ledger.count(BLOCKED) == 1


def test_an_alert_carries_what_to_do_rather_than_only_what_broke():
    ledger = run_batch([record(phone=PHONE_EMPTY)])
    rows = ledger.alert_rows()
    assert len(rows) == 1
    assert rows[0]["Retryable"] == "No"
    assert rows[0]["What to do"]


# ---------------------------------------------------------------------------
# The sample batch
# ---------------------------------------------------------------------------

def test_the_sample_batch_exercises_every_case():
    counts = kpi_counts(run_batch(sample_batch()))
    assert counts[SENT] == 2
    assert counts[DUPLICATE] == 1
    assert counts[BLOCKED] == 2
    assert counts[SKIPPED] == 2


def test_the_sample_batch_never_sends_twice_to_the_same_record():
    ledger = run_batch(sample_batch())
    sent = [result.record.record_id for result in ledger.results
            if result.outcome == SENT]
    assert len(sent) == len(set(sent))


def test_running_the_same_batch_twice_sends_nothing_extra():
    once = run_batch(sample_batch())
    twice = run_batch(sample_batch(), run_batch(sample_batch()))
    assert twice.count(SENT) == once.count(SENT)


def test_the_batch_run_outside_the_window_still_sends_through_the_template():
    ledger = run_batch(sample_batch(), within_window=False)
    assert ledger.count(SENT) == 2
    sent = next(r for r in ledger.results if r.outcome == SENT)
    assert "ContentSid" in sent.request.as_form()


def test_every_table_is_arrow_safe():
    ledger = run_batch(sample_batch())
    for rows in (ledger.rows(), ledger.alert_rows()):
        for column in {key for row in rows for key in row}:
            kinds = {type(row[column]).__name__ for row in rows}
            assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Prose discipline
# ---------------------------------------------------------------------------

def test_no_dash_characters_in_any_user_facing_prose():
    parts: list[str] = []
    for raw in PHONE_STYLES:
        parts.append(normalise_phone(raw).message)
    for error in TWILIO_ERRORS:
        parts += [error.label, error.meaning, error.action]
    for result in run_batch(sample_batch()).results:
        parts.append(result.detail)
    for code in (21211, 20429, ERROR_NONE):
        parts.append(process_record(AutomationLedger(), record(),
                                    failure_code=code).detail)
    prose = "\n".join(parts)
    assert "—" not in prose
    assert "–" not in prose
