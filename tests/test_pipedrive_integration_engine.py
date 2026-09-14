"""Ground truth tests for the Pipedrive API and integration engine.

Pass and fail markers are declared before execution. Every case names the
dispatch status, intent, handover decision, consent state or hash property it
must produce, and every result is parsed programmatically rather than read.

Run either way:
    python3 -m pytest tests/test_pipedrive_integration_engine.py -q
    python3 tests/test_pipedrive_integration_engine.py

Pass marker under pytest: every test passes, zero errors, exit code 0.
Pass marker under python3: final line is exactly "PIPEDRIVE RESULT: PASS <n>/<n>"
and exit code 0. Fail marker: any line starting "FAIL", plus exit code 1.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.pipedrive_integration_engine.core import (  # noqa: E402
    BLOCK_NO_PHONE,
    BLOCK_NO_TEMPLATE,
    BLOCK_OPT_OUT,
    BLOCKED,
    DIRECT_PATH,
    DISPATCHED,
    HANDOVER_REASON_ASKED,
    HANDOVER_REASON_REPEAT,
    HANDOVER_REASON_VALUE,
    INTENT_BOOKING,
    INTENT_GENERAL,
    INTENT_HANDOVER,
    INTENT_OPT_IN,
    INTENT_OPT_OUT,
    INTENT_PRICING,
    INTENT_STATUS,
    LLM_ASSISTANT,
    OPT_OUT_KEYWORDS,
    REP_TAKEOVER_VALUE,
    SAMPLE_DEALS,
    SAMPLE_HISTORY,
    SAMPLE_PEOPLE,
    STAGE_TEMPLATES,
    SUBSCRIBED,
    UNSUBSCRIBED,
    ZAPIER_PATH,
    Deal,
    Person,
    StageChange,
    Thread,
    apply_opt_in,
    apply_opt_out,
    build_prompt,
    change_hash,
    classify,
    consent_label,
    days_in_current_stage,
    days_open,
    dispatch_allowed,
    engine_kpis,
    export_csv,
    export_json,
    find_deal,
    find_person,
    handover_activity,
    hashes_of,
    incremental_plan,
    is_opt_in,
    is_opt_out,
    ledger_rows,
    matched_opt_out_keyword,
    normalize_phone,
    open_deal_for,
    path_comparison,
    person_by_phone,
    pipedrive_note_call,
    power_bi_manifest,
    redact,
    rep_metrics,
    route_inbound,
    route_webhook,
    stage_durations,
    stage_message,
    webhook_payload,
)

NOW = "2026-09-14T09:14:01Z"
SUBSCRIBED_PERSON = SAMPLE_PEOPLE[0]          # Hannah, subscribed
SECOND_PERSON = SAMPLE_PEOPLE[1]              # Marcus, subscribed
OPTED_OUT_PERSON = SAMPLE_PEOPLE[3]           # Tom, already unsubscribed
BIG_DEAL = SAMPLE_DEALS[0]                    # 48250 GBP, above the rep threshold
SMALL_DEAL = SAMPLE_DEALS[1]                  # 16400 EUR, below it


def _fresh_thread(person: Person, deal: Deal) -> Thread:
    return Thread(person.person_id, deal.deal_id)


# ---------------------------------------------------------------------------
# Direct webhook translation
# ---------------------------------------------------------------------------

def test_webhook_payload_is_the_shape_pipedrive_actually_posts():
    body = webhook_payload(BIG_DEAL, "Demo Booked", "evt-0001")
    assert body["meta"]["action"] == "change"
    assert body["meta"]["entity"] == "deal"
    assert body["meta"]["entity_id"] == BIG_DEAL.deal_id
    assert body["meta"]["correlation_id"] == "evt-0001"
    assert body["meta"]["version"] == "2.0"
    assert body["data"]["stage_name"] == BIG_DEAL.stage
    assert body["data"]["person_id"] == BIG_DEAL.person_id
    assert body["previous"]["stage_name"] == "Demo Booked"


def test_stage_change_translates_into_one_direct_sinch_request():
    out = route_webhook(BIG_DEAL, "Demo Booked", "evt-0001", SAMPLE_PEOPLE, now=NOW)
    assert out.status == DISPATCHED
    assert out.call is not None
    assert out.call.method == "POST"
    assert out.call.url.startswith("https://sms.api.sinch.com/xms/v1/")
    assert out.call.url.endswith("/batches")
    assert out.call.body["to"] == ["+447700900412"]
    assert out.call.body["body"] == out.text
    assert out.call.body["client_reference"] == f"{BIG_DEAL.deal_id}:{BIG_DEAL.person_id}"


def test_the_dispatch_path_contains_no_broker_at_all():
    out = route_webhook(BIG_DEAL, "Demo Booked", "evt-0001", SAMPLE_PEOPLE, now=NOW)
    assert out.zapier_used is False
    assert out.hops == DIRECT_PATH
    assert not any("zapier" in hop.lower() for hop in out.hops)
    assert len(DIRECT_PATH) < len(ZAPIER_PATH)
    comparison = path_comparison(600)
    assert comparison["hops_removed"] == len(ZAPIER_PATH) - len(DIRECT_PATH)
    assert comparison["zapier_tasks_per_month"] == 1200
    assert comparison["direct_delay_seconds"] == 0.0
    assert comparison["zapier_delay_seconds"] > 0


def test_translation_is_fast_enough_for_a_webhook_handler():
    out = route_webhook(BIG_DEAL, "Demo Booked", "evt-0001", SAMPLE_PEOPLE, now=NOW)
    # Pipedrive retries a webhook that does not answer quickly, so the whole
    # translation has to sit far inside one second.
    assert out.elapsed_ms < 250.0


def test_the_api_token_never_reaches_the_request_in_clear():
    out = route_webhook(BIG_DEAL, "Demo Booked", "evt-0001", SAMPLE_PEOPLE,
                        api_token="sinch_live_7c41f9e2a8b3", now=NOW)
    rendered = out.call.as_json()
    assert "sinch_live_7c41f9e2a8b3" not in rendered
    assert "****" in rendered
    assert redact("short") == "****"


def test_the_template_carries_the_person_and_the_deal():
    text = stage_message(BIG_DEAL, SUBSCRIBED_PERSON)
    assert "Hannah" in text
    assert BIG_DEAL.title in text
    assert "48,250.00" in text


def test_a_stage_with_no_template_sends_nothing():
    deal = replace(BIG_DEAL, stage="Lost")
    out = route_webhook(deal, "Negotiation", "evt-0002", SAMPLE_PEOPLE, now=NOW)
    assert out.status == BLOCKED
    assert out.reason == BLOCK_NO_TEMPLATE
    assert out.call is None
    assert "Lost" not in STAGE_TEMPLATES


def test_an_unknown_person_blocks_rather_than_guessing():
    deal = replace(BIG_DEAL, person_id="PD-P-0000")
    out = route_webhook(deal, "Demo Booked", "evt-0003", SAMPLE_PEOPLE, now=NOW)
    assert out.status == BLOCKED
    assert out.call is None
    assert "PD-P-0000" in out.reason


def test_a_person_with_no_number_blocks():
    person = replace(SUBSCRIBED_PERSON, phone="")
    out = route_webhook(BIG_DEAL, "Demo Booked", "evt-0004", (person,), now=NOW)
    assert out.status == BLOCKED
    assert out.reason == BLOCK_NO_PHONE


def test_the_note_written_back_to_pipedrive_targets_both_records():
    out = route_webhook(BIG_DEAL, "Demo Booked", "evt-0001", SAMPLE_PEOPLE, now=NOW)
    note = pipedrive_note_call(out.deal_id, out.person_id, f"SMS sent: {out.text}")
    assert note.method == "POST"
    assert note.url.endswith("/api/v2/notes")
    assert note.body["deal_id"] == BIG_DEAL.deal_id
    assert note.body["person_id"] == BIG_DEAL.person_id
    assert out.text in note.body["content"]


def test_numbers_reach_e164_however_a_rep_typed_them():
    assert normalize_phone("07700 900412") == "+447700900412"
    assert normalize_phone("+44 7700 900412") == "+447700900412"
    assert normalize_phone("0044 7700900412") == "+447700900412"
    assert normalize_phone("(07700) 900-412") == "+447700900412"
    assert normalize_phone("") == ""
    assert person_by_phone("07700 900412", SAMPLE_PEOPLE) == SUBSCRIBED_PERSON
    assert person_by_phone("07700 000000", SAMPLE_PEOPLE) is None


# ---------------------------------------------------------------------------
# AI thread routing
# ---------------------------------------------------------------------------

def test_intents_are_named_in_priority_order():
    assert classify("STOP") == INTENT_OPT_OUT
    assert classify("start") == INTENT_OPT_IN
    assert classify("can I speak to a human") == INTENT_HANDOVER
    assert classify("how much does it cost?") == INTENT_PRICING
    assert classify("can we book a demo next week?") == INTENT_BOOKING
    assert classify("any news on the proposal?") == INTENT_STATUS
    assert classify("thanks, got it") == INTENT_GENERAL


def test_an_inbound_message_is_answered_against_the_pipedrive_record():
    thread = _fresh_thread(SECOND_PERSON, SMALL_DEAL)
    turn = route_inbound(thread, "How much is this going to cost?",
                         SECOND_PERSON, SMALL_DEAL)
    assert turn.intent == INTENT_PRICING
    assert turn.handover is False
    assert turn.person_id == SECOND_PERSON.person_id
    assert turn.deal_id == SMALL_DEAL.deal_id
    assert "Marcus" in turn.reply
    assert SMALL_DEAL.title in turn.reply
    assert "16,400.00" in turn.reply
    assert turn.model == LLM_ASSISTANT


def test_the_prompt_carries_the_person_and_deal_context():
    thread = _fresh_thread(SECOND_PERSON, SMALL_DEAL)
    turn = route_inbound(thread, "where are we on this?", SECOND_PERSON, SMALL_DEAL)
    assert SECOND_PERSON.person_id in turn.prompt
    assert SMALL_DEAL.deal_id in turn.prompt
    assert SMALL_DEAL.stage in turn.prompt
    assert normalize_phone(SECOND_PERSON.phone) in turn.prompt
    assert "marketing_status" in turn.prompt


def test_both_sides_of_the_exchange_land_in_the_thread():
    thread = _fresh_thread(SECOND_PERSON, SMALL_DEAL)
    route_inbound(thread, "any news?", SECOND_PERSON, SMALL_DEAL)
    assert len(thread.inbound) == 1
    assert len(thread.outbound) == 1
    assert thread.messages[0].direction == "inbound"
    assert thread.messages[1].direction == "outbound"
    assert thread.messages[1].author == LLM_ASSISTANT


def test_a_person_with_no_open_deal_is_still_answered():
    lonely = replace(SECOND_PERSON, person_id="PD-P-9999")
    thread = Thread(lonely.person_id, "")
    turn = route_inbound(thread, "how much is it?", lonely, None)
    assert turn.deal_id == ""
    assert turn.reply
    assert turn.handover is False


def test_the_thread_finds_the_most_recently_touched_open_deal():
    found = open_deal_for(SUBSCRIBED_PERSON.person_id, SAMPLE_DEALS)
    assert found is not None
    assert found.deal_id == BIG_DEAL.deal_id
    assert open_deal_for("PD-P-0000", SAMPLE_DEALS) is None


def test_a_prompt_is_built_even_before_the_first_reply():
    thread = _fresh_thread(SECOND_PERSON, SMALL_DEAL)
    prompt = build_prompt(thread, SECOND_PERSON, SMALL_DEAL, "hello")
    assert "PIPEDRIVE RECORD" in prompt
    assert "THREAD SO FAR" in prompt
    assert "hello" in prompt


# ---------------------------------------------------------------------------
# Handover detection
# ---------------------------------------------------------------------------

def test_asking_for_a_human_flags_a_sales_rep_handover():
    thread = _fresh_thread(SECOND_PERSON, SMALL_DEAL)
    turn = route_inbound(thread, "Can I speak to a human please?",
                         SECOND_PERSON, SMALL_DEAL)
    assert turn.handover is True
    assert turn.handover_reason == HANDOVER_REASON_ASKED
    assert thread.handover is True
    assert thread.handover_at
    assert "person" in turn.reply.lower()


def test_every_way_a_customer_asks_for_a_person_is_caught():
    for text in ("can I talk to someone", "I want a human", "call me please",
                 "put me through to sales", "are you a bot?",
                 "speak to a real person", "get me a human",
                 "can a sales rep ring me", "transfer me to an agent"):
        thread = _fresh_thread(SECOND_PERSON, SMALL_DEAL)
        turn = route_inbound(thread, text, SECOND_PERSON, SMALL_DEAL)
        assert turn.handover is True, f"missed a handover request: {text!r}"


def test_ordinary_messages_do_not_trigger_a_false_handover():
    for text in ("that was human error on our side", "how much is it?",
                 "can we book a demo", "thanks, got it",
                 "the agent portal link is broken"):
        thread = _fresh_thread(SECOND_PERSON, SMALL_DEAL)
        turn = route_inbound(thread, text, SECOND_PERSON, SMALL_DEAL)
        assert turn.handover is False, f"false handover on: {text!r}"


def test_the_assistant_goes_silent_once_a_rep_owns_the_thread():
    thread = _fresh_thread(SECOND_PERSON, SMALL_DEAL)
    route_inbound(thread, "can I speak to a human", SECOND_PERSON, SMALL_DEAL)
    outbound_before = len(thread.outbound)
    follow_up = route_inbound(thread, "are you still there?",
                              SECOND_PERSON, SMALL_DEAL)
    assert follow_up.reply == ""
    assert follow_up.handover is True
    assert len(thread.outbound) == outbound_before


def test_a_big_deal_price_question_goes_to_a_rep_but_a_booking_does_not():
    assert BIG_DEAL.value > REP_TAKEOVER_VALUE
    pricing = _fresh_thread(SUBSCRIBED_PERSON, BIG_DEAL)
    turn = route_inbound(pricing, "what is the price?", SUBSCRIBED_PERSON, BIG_DEAL)
    assert turn.handover is True
    assert turn.handover_reason == HANDOVER_REASON_VALUE

    booking = _fresh_thread(SUBSCRIBED_PERSON, BIG_DEAL)
    other = route_inbound(booking, "can we book a demo next week?",
                          SUBSCRIBED_PERSON, BIG_DEAL)
    assert other.handover is False


def test_the_same_question_a_third_time_goes_to_a_rep():
    thread = _fresh_thread(SECOND_PERSON, SMALL_DEAL)
    first = route_inbound(thread, "how much is it?", SECOND_PERSON, SMALL_DEAL)
    second = route_inbound(thread, "and how much including support?",
                           SECOND_PERSON, SMALL_DEAL)
    third = route_inbound(thread, "sorry, what is the cost again?",
                          SECOND_PERSON, SMALL_DEAL)
    assert first.handover is False
    assert second.handover is False
    assert third.handover is True
    assert third.handover_reason == HANDOVER_REASON_REPEAT


def test_a_handover_creates_a_real_pipedrive_call_activity():
    thread = _fresh_thread(SECOND_PERSON, SMALL_DEAL)
    route_inbound(thread, "please call me", SECOND_PERSON, SMALL_DEAL)
    call = handover_activity(thread, SECOND_PERSON, SMALL_DEAL)
    assert call.method == "POST"
    assert call.url.endswith("/api/v2/activities")
    assert call.body["type"] == "call"
    assert call.body["deal_id"] == SMALL_DEAL.deal_id
    assert call.body["person_id"] == SECOND_PERSON.person_id
    assert call.body["owner_id"] == SECOND_PERSON.owner
    assert call.body["done"] is False
    assert SECOND_PERSON.name in call.body["subject"]
    assert "please call me" in call.body["note"]


# ---------------------------------------------------------------------------
# Opt out state changes
# ---------------------------------------------------------------------------

def test_every_carrier_keyword_is_recognised():
    for keyword in OPT_OUT_KEYWORDS:
        assert is_opt_out(keyword), f"missed the keyword {keyword!r}"
    assert is_opt_out("STOP")
    assert is_opt_out("stop.")
    assert is_opt_out("Stop all")
    assert is_opt_out("STOP please, too many texts")
    assert is_opt_out("please remove me from this list")
    assert not is_opt_out("can you stop by on Thursday?")
    assert not is_opt_out("")
    assert matched_opt_out_keyword("stop.") == "STOP"
    assert matched_opt_out_keyword("hello there") == ""


def test_a_stop_unsubscribes_the_person_in_pipedrive():
    result = apply_opt_out(SUBSCRIBED_PERSON, "STOP", now="2026-09-14T10:05:00Z")
    assert result.applied is True
    assert result.keyword == "STOP"
    assert result.person.marketing_status == UNSUBSCRIBED
    assert result.person.sms_opt_out is True
    assert result.person.opt_out_at == "2026-09-14T10:05:00Z"
    assert result.person.opt_out_keyword == "STOP"
    assert result.person.opted_out is True
    # The original record is untouched: the change is returned, not mutated.
    assert SUBSCRIBED_PERSON.opted_out is False
    assert consent_label(result.person) == "Unsubscribed"


def test_the_opt_out_writes_every_consent_field_back():
    result = apply_opt_out(SUBSCRIBED_PERSON, "STOP", now="2026-09-14T10:05:00Z")
    fields = {u.field_name: u for u in result.updates}
    assert set(fields) == {"marketing_status", "sms_opt_out",
                           "sms_opt_out_at", "sms_opt_out_keyword"}
    assert fields["marketing_status"].old_value == SUBSCRIBED
    assert fields["marketing_status"].new_value == UNSUBSCRIBED
    assert fields["sms_opt_out"].new_value == "True"
    patch = result.calls[0]
    assert patch.method == "PATCH"
    assert patch.url.endswith(f"/persons/{SUBSCRIBED_PERSON.person_id}")
    assert patch.body["marketing_status"] == UNSUBSCRIBED
    assert patch.body["custom_fields"]["sms_opt_out"] is True


def test_exactly_one_confirmation_goes_back_and_no_more():
    result = apply_opt_out(SUBSCRIBED_PERSON, "STOP")
    sends = [c for c in result.calls if "sinch.com" in c.url]
    assert len(sends) == 1
    assert "unsubscribed" in result.confirmation.lower()
    assert "START" in result.confirmation

    again = apply_opt_out(result.person, "STOP")
    assert again.applied is False
    assert again.calls == ()
    assert again.person == result.person


def test_an_unsubscribed_person_blocks_every_later_dispatch():
    result = apply_opt_out(SUBSCRIBED_PERSON, "STOP")
    allowed, reason = dispatch_allowed(result.person)
    assert allowed is False
    assert reason == BLOCK_OPT_OUT

    people = tuple(result.person if p.person_id == result.person.person_id else p
                   for p in SAMPLE_PEOPLE)
    out = route_webhook(BIG_DEAL, "Demo Booked", "evt-0009", people, now=NOW)
    assert out.status == BLOCKED
    assert out.reason == BLOCK_OPT_OUT
    assert out.call is None


def test_the_sample_book_already_carries_one_unsubscribed_person():
    assert OPTED_OUT_PERSON.opted_out is True
    allowed, _ = dispatch_allowed(OPTED_OUT_PERSON)
    assert allowed is False
    out = route_webhook(SAMPLE_DEALS[3], "Negotiation", "evt-0010",
                        SAMPLE_PEOPLE, now=NOW)
    assert out.status == BLOCKED
    assert out.reason == BLOCK_OPT_OUT


def test_a_non_keyword_message_leaves_consent_alone():
    result = apply_opt_out(SUBSCRIBED_PERSON, "thanks, that is all clear")
    assert result.applied is False
    assert result.keyword == ""
    assert result.person == SUBSCRIBED_PERSON
    assert result.calls == ()


def test_start_puts_consent_back():
    stopped = apply_opt_out(SUBSCRIBED_PERSON, "STOP").person
    assert is_opt_in("START")
    back = apply_opt_in(stopped, "START")
    assert back.applied is True
    assert back.person.marketing_status == SUBSCRIBED
    assert back.person.sms_opt_out is False
    assert back.person.opted_out is False
    allowed, _ = dispatch_allowed(back.person)
    assert allowed is True
    assert apply_opt_in(SUBSCRIBED_PERSON, "START").applied is False


def test_consent_is_read_through_one_property_only():
    half = Person("PD-P-5000", "Half Way", "+447700900999",
                  marketing_status=UNSUBSCRIBED, sms_opt_out=False)
    assert half.opted_out is True
    other_half = Person("PD-P-5001", "Other Half", "+447700900998",
                        marketing_status=SUBSCRIBED, sms_opt_out=True)
    assert other_half.opted_out is True


# ---------------------------------------------------------------------------
# Power BI ledger
# ---------------------------------------------------------------------------

def test_every_row_carries_a_stable_change_hash():
    rows = ledger_rows(SAMPLE_DEALS, SAMPLE_PEOPLE, SAMPLE_HISTORY)
    assert len(rows) == len(SAMPLE_DEALS)
    for row in rows:
        assert len(row.change_hash) == 64
        assert int(row.change_hash, 16) >= 0
    again = ledger_rows(SAMPLE_DEALS, SAMPLE_PEOPLE, SAMPLE_HISTORY)
    assert [r.change_hash for r in rows] == [r.change_hash for r in again]


def test_the_hash_ignores_fields_that_move_on_their_own():
    base = {"deal_id": "PD-D-1", "value": 10.0}
    assert change_hash(base) == change_hash({**base, "extracted_at": "now"})
    assert change_hash(base) != change_hash({**base, "value": 11.0})


def test_an_unchanged_table_sends_nothing_at_all():
    rows = ledger_rows(SAMPLE_DEALS, SAMPLE_PEOPLE, SAMPLE_HISTORY)
    plan = incremental_plan(rows, hashes_of(rows), "2026-09-01T00:00:00Z")
    assert plan.rows_to_send == ()
    assert plan.saved_rows == len(rows)
    assert plan.watermark == max(r.updated_at for r in rows)


def test_one_changed_deal_moves_one_row():
    rows = ledger_rows(SAMPLE_DEALS, SAMPLE_PEOPLE, SAMPLE_HISTORY)
    baseline = hashes_of(rows)
    changed_deals = tuple(
        replace(d, value=d.value + 500.0) if d.deal_id == "PD-D-9002" else d
        for d in SAMPLE_DEALS
    )
    rows_after = ledger_rows(changed_deals, SAMPLE_PEOPLE, SAMPLE_HISTORY)
    plan = incremental_plan(rows_after, baseline, "2026-09-01T00:00:00Z")
    assert len(plan.changed_rows) == 1
    assert plan.changed_rows[0].deal_id == "PD-D-9002"
    assert len(plan.rows_to_send) == 1
    assert plan.saved_rows == len(rows) - 1


def test_a_deal_never_seen_before_counts_as_new():
    rows = ledger_rows(SAMPLE_DEALS, SAMPLE_PEOPLE, SAMPLE_HISTORY)
    partial = {k: v for k, v in hashes_of(rows).items() if k != "PD-D-9001"}
    plan = incremental_plan(rows, partial)
    assert [r.deal_id for r in plan.new_rows] == ["PD-D-9001"]
    assert plan.changed_rows == ()


def test_an_opt_out_shows_up_in_the_power_bi_row():
    stopped = apply_opt_out(SUBSCRIBED_PERSON, "STOP").person
    people = tuple(stopped if p.person_id == stopped.person_id else p
                   for p in SAMPLE_PEOPLE)
    before = ledger_rows(SAMPLE_DEALS, SAMPLE_PEOPLE, SAMPLE_HISTORY)[0]
    after = ledger_rows(SAMPLE_DEALS, people, SAMPLE_HISTORY)[0]
    assert before.sms_consent == "Subscribed"
    assert after.sms_consent == "Unsubscribed"
    assert before.change_hash != after.change_hash


def test_stage_durations_are_measured_not_guessed():
    durations = stage_durations("PD-D-9001", SAMPLE_HISTORY, now="2026-09-14T09:00:00Z")
    stages = [name for name, _ in durations]
    assert stages == ["Qualified", "Demo Booked", "Proposal Sent"]
    assert all(days > 0 for _, days in durations)
    # The starting stage has no entry event, so it is left out rather than invented.
    assert "Lead In" not in stages
    assert stage_durations("PD-D-0000", SAMPLE_HISTORY) == []

    deal = find_deal("PD-D-9001", SAMPLE_DEALS)
    assert days_in_current_stage(deal, SAMPLE_HISTORY, "2026-09-14T09:00:00Z") > 0
    assert days_open(deal, SAMPLE_HISTORY, "2026-09-14T09:00:00Z") > \
        days_in_current_stage(deal, SAMPLE_HISTORY, "2026-09-14T09:00:00Z")


def test_a_stage_duration_is_never_negative_when_history_arrives_out_of_order():
    history = (
        StageChange("PD-D-7", "Qualified", "Won", "2026-09-10T00:00:00Z"),
        StageChange("PD-D-7", "Lead In", "Qualified", "2026-09-01T00:00:00Z"),
    )
    durations = stage_durations("PD-D-7", history, now="2026-09-12T00:00:00Z")
    assert [name for name, _ in durations] == ["Qualified", "Won"]
    assert all(days >= 0 for _, days in durations)


def test_rep_metrics_add_up_to_the_book():
    metrics = rep_metrics(SAMPLE_DEALS, SAMPLE_HISTORY)
    assert {m.rep for m in metrics} == {"r.okonkwo", "s.patel"}
    assert sum(m.deals for m in metrics) == len(SAMPLE_DEALS)
    assert sum(m.open_deals for m in metrics) == \
        len([d for d in SAMPLE_DEALS if d.status == "open"])
    assert sum(m.won_deals for m in metrics) == \
        len([d for d in SAMPLE_DEALS if d.status == "won"])
    total_pipeline = sum(d.value for d in SAMPLE_DEALS if d.status == "open")
    assert abs(sum(m.pipeline_value for m in metrics) - total_pipeline) < 0.01
    for m in metrics:
        assert 0.0 <= m.win_rate <= 1.0


def test_the_export_is_a_file_power_bi_can_take_as_it_stands():
    rows = ledger_rows(SAMPLE_DEALS, SAMPLE_PEOPLE, SAMPLE_HISTORY)
    csv = export_csv(rows)
    lines = csv.splitlines()
    assert len(lines) == len(rows) + 1
    assert lines[0].split(",")[0] == "deal_id"
    assert "change_hash" in lines[0]
    for row in rows:
        assert row.change_hash in csv
    # A title with a comma must not split into two columns.
    comma_deal = replace(SAMPLE_DEALS[0], title="Rollout, phase two")
    quoted = export_csv(ledger_rows((comma_deal,), SAMPLE_PEOPLE, SAMPLE_HISTORY))
    assert '"Rollout, phase two"' in quoted

    plan = incremental_plan(rows, {})
    batch = export_json(plan)
    assert '"table": "fact_deal_state"' in batch
    assert '"watermark"' in batch

    manifest = power_bi_manifest(rows)
    assert manifest["key"] == "deal_id"
    assert manifest["watermark_column"] == "updated_at"
    assert manifest["change_column"] == "change_hash"
    assert manifest["rows_available"] == len(rows)
    assert set(manifest["columns"]) >= {"deal_id", "stage", "sms_consent",
                                        "days_in_stage", "change_hash"}


def test_the_kpis_describe_the_run_that_actually_happened():
    dispatch = route_webhook(BIG_DEAL, "Demo Booked", "evt-0001", SAMPLE_PEOPLE, now=NOW)
    rows = ledger_rows(SAMPLE_DEALS, SAMPLE_PEOPLE, SAMPLE_HISTORY)
    plan = incremental_plan(rows, hashes_of(rows))
    thread = _fresh_thread(SECOND_PERSON, SMALL_DEAL)
    route_inbound(thread, "can I speak to a human", SECOND_PERSON, SMALL_DEAL)
    kpis = engine_kpis(dispatch, plan, [thread], SAMPLE_PEOPLE)
    assert kpis.dispatch_ms == dispatch.elapsed_ms
    assert kpis.sub_second is True
    assert kpis.handovers == 1
    assert kpis.blocked_by_consent == 1
    assert kpis.rows_to_send == 0
    assert kpis.rows_skipped == len(rows)
    assert kpis.ingest_saving == 1.0
    assert kpis.zapier_hops_removed == len(ZAPIER_PATH) - len(DIRECT_PATH)


# ---------------------------------------------------------------------------
# House style
# ---------------------------------------------------------------------------

def test_no_em_or_en_dashes_anywhere_in_the_prose():
    # Spelled by code point so the detector itself carries no dash.
    banned = (chr(8212), chr(8211))
    prose: list[tuple[str, str]] = []
    prose += [(f"template {stage}", text) for stage, text in STAGE_TEMPLATES.items()]
    prose += [(f"direct hop {i}", hop) for i, hop in enumerate(DIRECT_PATH)]
    prose += [(f"zapier hop {i}", hop) for i, hop in enumerate(ZAPIER_PATH)]
    prose += [("handover asked", HANDOVER_REASON_ASKED),
              ("handover repeat", HANDOVER_REASON_REPEAT),
              ("handover value", HANDOVER_REASON_VALUE),
              ("block opt out", BLOCK_OPT_OUT),
              ("block no phone", BLOCK_NO_PHONE),
              ("block no template", BLOCK_NO_TEMPLATE)]

    dispatch = route_webhook(BIG_DEAL, "Demo Booked", "evt-0001", SAMPLE_PEOPLE, now=NOW)
    prose += [("dispatch reason", dispatch.reason), ("dispatch text", dispatch.text)]

    thread = _fresh_thread(SECOND_PERSON, SMALL_DEAL)
    for message in ("how much is it?", "can we book a demo",
                    "any news?", "thanks", "can I speak to a human"):
        turn = route_inbound(thread, message, SECOND_PERSON, SMALL_DEAL)
        prose.append((f"reply to {message}", turn.reply))

    result = apply_opt_out(SUBSCRIBED_PERSON, "STOP")
    prose += [("opt out note", result.note), ("confirmation", result.confirmation)]
    prose += [(f"person {p.person_id}", p.name) for p in SAMPLE_PEOPLE]
    prose += [(f"deal {d.deal_id}", d.title) for d in SAMPLE_DEALS]

    offenders = [name for name, text in prose if any(b in (text or "") for b in banned)]
    assert not offenders, f"em or en dashes found in: {offenders}"


def test_the_engine_never_imports_streamlit():
    source = (Path(__file__).resolve().parents[1]
              / "tools" / "pipedrive_integration_engine" / "core.py").read_text()
    assert "import streamlit" not in source
    assert "streamlit" not in source


# ---------------------------------------------------------------------------
# Standalone runner, so the suite reports the same way as the rest of the repo
# ---------------------------------------------------------------------------

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
        print(f"PIPEDRIVE RESULT: FAIL {len(failures)} of {len(tests)} checks failed")
        for name in failures:
            print(f"  - {name}")
        sys.exit(1)
    print(f"PIPEDRIVE RESULT: PASS {len(tests)}/{len(tests)}")
    sys.exit(0)
