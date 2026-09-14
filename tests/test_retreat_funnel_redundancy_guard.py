"""Engine tests for the Retreat Funnel Redundancy Guard.

Written for pytest. Deterministic throughout: the clock and the random source
are injected, so a seeded run reproduces exactly.

Run: pytest tests/test_retreat_funnel_redundancy_guard.py
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.retreat_funnel_redundancy_guard.core import (  # noqa: E402
    ALERT_IN_TIME,
    ALERT_NONE,
    ALERT_TOO_LATE,
    CHANNEL_CRITICAL,
    CHANNEL_HIGH,
    DEFAULT_THRESHOLDS,
    FAILED,
    FAILURE_MODES,
    FAILURE_NONE,
    MANUAL_FALLBACK_MINUTES,
    MESSAGE_BY_READINESS,
    OK,
    PROGRAM,
    READINESS,
    READY_BROWSING,
    READY_NOW,
    READY_SOON,
    RECOVERED,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARNING,
    SKIPPED,
    SOURCE_ADS,
    SOURCE_DM,
    SOURCE_REFERRAL,
    SOURCES,
    STEPS,
    TAG_DEPOSIT_ELIGIBLE,
    TAG_PRIORITY_CALL,
    TAG_PROGRAM,
    TAG_READINESS,
    TAG_REFERRAL_CREDIT,
    TAG_SOURCE,
    TAG_WEBINAR,
    TICKET_PRICE,
    FunnelMetrics,
    Thresholds,
    assign_tags,
    escalate,
    evaluate_metrics,
    failure_by_code,
    funnel_rows,
    generate_lead,
    metric_rows,
    metrics_headline,
    run_pipeline,
    step_by_key,
)

NOW = "2026-09-12T09:00:00Z"
SEED = 20260912

ZOOM = "ZOOM_LINK_SYNC_FAILED"          # hard failure, no retry fixes it
TAGS = "TAGS_NOT_APPLIED"               # transient, a retry clears it


def lead(source: str = SOURCE_DM, readiness: str = READY_NOW, sequence: int = 1):
    return generate_lead(random.Random(SEED), sequence, now=NOW, source=source,
                         readiness=readiness)


def result_for(run, key: str):
    for result in run.results:
        if result.step.key == key:
            return result
    raise AssertionError(f"no step keyed {key!r}")


# ---------------------------------------------------------------------------
# Tag assignment
# ---------------------------------------------------------------------------

def test_every_lead_carries_the_program_tag():
    for source in SOURCES:
        for readiness in READINESS:
            assert TAG_PROGRAM in assign_tags(source, readiness)


@pytest.mark.parametrize("source", SOURCES)
def test_each_source_maps_to_its_own_tag(source):
    assert TAG_SOURCE[source] in assign_tags(source, READY_NOW)


@pytest.mark.parametrize("readiness", READINESS)
def test_each_readiness_answer_maps_to_an_intent_tag(readiness):
    assert TAG_READINESS[readiness] in assign_tags(SOURCE_DM, readiness)


def test_a_ready_now_lead_is_routed_to_the_priority_call_queue():
    assert TAG_PRIORITY_CALL in assign_tags(SOURCE_DM, READY_NOW)
    assert TAG_PRIORITY_CALL not in assign_tags(SOURCE_DM, READY_SOON)
    assert TAG_PRIORITY_CALL not in assign_tags(SOURCE_DM, READY_BROWSING)


def test_deposit_eligibility_covers_the_two_buying_answers_only():
    assert TAG_DEPOSIT_ELIGIBLE in assign_tags(SOURCE_DM, READY_NOW)
    assert TAG_DEPOSIT_ELIGIBLE in assign_tags(SOURCE_DM, READY_SOON)
    assert TAG_DEPOSIT_ELIGIBLE not in assign_tags(SOURCE_DM, READY_BROWSING)


def test_a_referral_is_tagged_for_the_credit_owed_to_whoever_sent_them():
    assert TAG_REFERRAL_CREDIT in assign_tags(SOURCE_REFERRAL, READY_SOON)
    assert TAG_REFERRAL_CREDIT not in assign_tags(SOURCE_ADS, READY_SOON)


def test_the_webinar_tag_is_only_added_once_the_lead_registers():
    assert TAG_WEBINAR not in assign_tags(SOURCE_DM, READY_NOW)
    assert TAG_WEBINAR in assign_tags(SOURCE_DM, READY_NOW, registered=True)


def test_an_unknown_source_is_tagged_as_unknown_rather_than_dropped():
    """A lead with no tag receives no automation at all, so an unrecognised
    source still has to produce one."""
    tags = assign_tags("Carrier pigeon", READY_NOW)
    assert "src-unknown" in tags
    assert TAG_PROGRAM in tags


def test_tags_are_assigned_from_the_inputs_and_nothing_else():
    assert assign_tags(SOURCE_DM, READY_NOW) == assign_tags(SOURCE_DM, READY_NOW)


# ---------------------------------------------------------------------------
# Lead payload generation
# ---------------------------------------------------------------------------

def test_the_same_seed_produces_the_same_lead():
    first = generate_lead(random.Random(SEED), 1, now=NOW)
    second = generate_lead(random.Random(SEED), 1, now=NOW)
    assert first == second


def test_different_sequences_produce_different_contact_ids():
    assert lead(sequence=1).lead_id == "FG-00001"
    assert lead(sequence=42).lead_id == "FG-00042"


def test_a_forced_source_and_readiness_are_honoured():
    generated = lead(source=SOURCE_ADS, readiness=READY_BROWSING)
    assert generated.source == SOURCE_ADS
    assert generated.readiness == READY_BROWSING
    assert TAG_SOURCE[SOURCE_ADS] in generated.tags


def test_an_unforced_lead_still_lands_on_real_options():
    generated = generate_lead(random.Random(3), 1, now=NOW)
    assert generated.source in SOURCES
    assert generated.readiness in READINESS


def test_the_first_message_matches_what_the_lead_said_they_wanted():
    for readiness in READINESS:
        assert lead(readiness=readiness).message == MESSAGE_BY_READINESS[readiness]


def test_the_payload_carries_every_field_the_automation_reads():
    payload = lead().as_payload()
    assert set(payload) == {"contact_id", "full_name", "instagram_handle",
                            "email", "source", "custom_fields", "tags",
                            "received_at"}
    assert payload["custom_fields"]["program"] == PROGRAM
    assert payload["tags"] == list(lead().tags)


def test_the_payload_serialises_to_json_for_the_webhook():
    assert json.loads(json.dumps(lead().as_payload()))["contact_id"] == "FG-00001"


def test_the_handle_and_email_are_derived_from_the_name():
    generated = lead()
    first, last = generated.name.lower().split()
    assert generated.handle == f"@{first}.{last}"
    assert generated.email.startswith(f"{first}.{last}@")


def test_a_ready_now_lead_reads_as_hot():
    assert lead(readiness=READY_NOW).hot is True
    assert lead(readiness=READY_BROWSING).hot is False


# ---------------------------------------------------------------------------
# The pipeline and the error guard
# ---------------------------------------------------------------------------

def test_a_clean_run_delivers_every_step_once():
    run = run_pipeline(lead(), FAILURE_NONE)
    assert run.delivered is True
    assert run.broken_step is None
    assert [result.status for result in run.results] == [OK] * len(STEPS)
    assert run.retries_used == 0


def test_a_hard_failure_stops_the_run_at_the_failing_step():
    """Sending a confirmation email with no join link in it is worse than
    sending nothing, so the pipeline stops rather than continuing."""
    run = run_pipeline(lead(), ZOOM)
    assert run.delivered is False
    assert result_for(run, "zoom").status == FAILED
    assert result_for(run, "confirm").status == SKIPPED
    assert result_for(run, "offer").status == SKIPPED


def test_steps_before_the_failure_still_delivered():
    run = run_pipeline(lead(), ZOOM)
    for key in ("contact", "tags", "register"):
        assert result_for(run, key).status == OK


def test_a_transient_failure_recovers_when_it_is_given_retries():
    run = run_pipeline(lead(), TAGS, max_retries=3)
    assert run.delivered is True
    assert result_for(run, "tags").status == RECOVERED
    assert result_for(run, "tags").attempts == 4
    assert run.retries_used == 3


def test_a_transient_failure_is_a_lost_seat_when_retries_are_switched_off():
    run = run_pipeline(lead(), TAGS, max_retries=0)
    assert run.delivered is False
    assert result_for(run, "tags").status == FAILED
    assert "Retries are switched off" in result_for(run, "tags").detail


def test_a_hard_failure_exhausts_every_retry_it_is_given():
    run = run_pipeline(lead(), ZOOM, max_retries=3)
    assert result_for(run, "zoom").attempts == 4
    assert run.retries_used == 3


def test_a_recovered_run_costs_time_the_reminder_schedule_is_counting_on():
    clean = run_pipeline(lead(), FAILURE_NONE)
    recovered = run_pipeline(lead(), TAGS, max_retries=3)
    assert recovered.delivered is True
    assert recovered.total_ms > clean.total_ms + 20000


def test_more_retries_cost_more_time():
    one = run_pipeline(lead(), ZOOM, max_retries=1)
    three = run_pipeline(lead(), ZOOM, max_retries=3)
    assert three.total_ms > one.total_ms


def test_a_negative_retry_count_is_refused_rather_than_simulated():
    with pytest.raises(ValueError):
        run_pipeline(lead(), ZOOM, max_retries=-1)


@pytest.mark.parametrize("mode", FAILURE_MODES, ids=[m.code for m in FAILURE_MODES])
def test_every_failure_mode_hits_a_real_step_and_is_caught(mode):
    assert step_by_key(mode.step_key) is not None
    run = run_pipeline(lead(), mode.code, max_retries=0)
    assert run.broken_step is not None
    assert run.broken_step.step.key == mode.step_key


@pytest.mark.parametrize("mode", FAILURE_MODES, ids=[m.code for m in FAILURE_MODES])
def test_every_failure_mode_names_a_consequence_and_a_manual_fallback(mode):
    assert mode.consequence and mode.fallback
    assert mode.severity in ("Critical", "High")


def test_an_unknown_failure_code_runs_clean_rather_than_guessing():
    assert failure_by_code("NOT_A_FAILURE") is None
    assert run_pipeline(lead(), "NOT_A_FAILURE").delivered is True


def test_the_failure_timestamp_is_the_lead_time_plus_the_elapsed_run():
    run = run_pipeline(lead(), ZOOM)
    assert run.started_at == NOW
    assert run.failed_at.startswith("2026-09-12T09:00:")
    assert run.failed_at > NOW


def test_the_step_table_holds_one_type_per_column_for_arrow():
    rows = run_pipeline(lead(), ZOOM).rows()
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"column {column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Slack escalation
# ---------------------------------------------------------------------------

def test_a_clean_run_raises_nothing():
    alert = escalate(run_pipeline(lead(), FAILURE_NONE))
    assert alert.raised is False
    assert alert.verdict == ALERT_NONE
    assert alert.blocks == []


def test_a_broken_run_raises_an_alert_naming_the_lead_and_the_step():
    alert = escalate(run_pipeline(lead(), ZOOM))
    assert alert.raised is True
    assert "Zoom link sync failed" in alert.headline
    assert "FG-00001" in alert.headline


def test_a_critical_failure_goes_to_the_alert_channel_and_a_high_one_does_not():
    critical = escalate(run_pipeline(lead(), ZOOM))
    high = escalate(run_pipeline(lead(), "REMINDER_WORKFLOW_STALLED",
                                 max_retries=0))
    assert critical.channel == CHANNEL_CRITICAL
    assert high.channel == CHANNEL_HIGH


def test_an_alert_raised_with_time_to_spare_reads_as_in_time():
    alert = escalate(run_pipeline(lead(), ZOOM), minutes_to_webinar=90)
    assert alert.verdict == ALERT_IN_TIME
    assert alert.in_time is True


def test_an_alert_raised_too_late_says_so_rather_than_looking_handled():
    """An alert that lands after the webinar has started is a record, not a
    rescue, so the verdict compares against the time the fallback needs."""
    alert = escalate(run_pipeline(lead(), ZOOM), minutes_to_webinar=5)
    assert alert.verdict == ALERT_TOO_LATE
    assert alert.in_time is False


@pytest.mark.parametrize("minutes,expected", [
    (0, ALERT_TOO_LATE),
    (MANUAL_FALLBACK_MINUTES - 1, ALERT_TOO_LATE),
    (MANUAL_FALLBACK_MINUTES, ALERT_IN_TIME),
    (120, ALERT_IN_TIME),
])
def test_the_deadline_turns_exactly_where_the_fallback_needs_it_to(minutes, expected):
    assert escalate(run_pipeline(lead(), ZOOM),
                    minutes_to_webinar=minutes).verdict == expected


def test_the_money_at_risk_is_the_seats_times_the_ticket():
    alert = escalate(run_pipeline(lead(), ZOOM), seats_at_risk=3)
    assert alert.revenue_at_risk == pytest.approx(3 * TICKET_PRICE)


def test_the_alert_carries_the_manual_fallback_the_team_has_to_run():
    alert = escalate(run_pipeline(lead(), ZOOM))
    assert "by hand" in alert.fallback


def test_the_slack_payload_is_valid_block_kit_and_survives_json():
    alert = escalate(run_pipeline(lead(), ZOOM))
    payload = json.loads(alert.as_json())
    assert payload["channel"] == CHANNEL_CRITICAL
    assert payload["text"] == alert.headline
    kinds = [block["type"] for block in payload["blocks"]]
    assert kinds[0] == "header"
    assert "section" in kinds
    assert kinds[-1] == "context"


def test_the_slack_payload_carries_the_tags_so_the_fix_can_be_done_by_hand():
    generated = lead()
    alert = escalate(run_pipeline(generated, TAGS, max_retries=0))
    body = alert.as_json()
    for tag in generated.tags:
        assert tag in body
    assert "TAGS_NOT_APPLIED" in body


def test_a_recovered_run_raises_nothing_because_nothing_was_lost():
    alert = escalate(run_pipeline(lead(), TAGS, max_retries=3))
    assert alert.raised is False


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def test_the_rates_are_computed_from_the_counts():
    metrics = FunnelMetrics(registrations=400, emails_sent=400, opens=200,
                            clicks=40, show_ups=180, offers=150, closes=15)
    assert metrics.open_rate == pytest.approx(0.5)
    assert metrics.click_rate == pytest.approx(0.1)
    assert metrics.show_up_rate == pytest.approx(0.45)
    assert metrics.close_rate == pytest.approx(0.1)
    assert metrics.revenue == pytest.approx(15 * TICKET_PRICE)


def test_an_empty_funnel_reports_zero_rather_than_dividing_by_zero():
    metrics = FunnelMetrics(registrations=0, emails_sent=0, opens=0, clicks=0,
                            show_ups=0, offers=0, closes=0)
    assert metrics.open_rate == 0.0
    assert metrics.show_up_rate == 0.0
    assert metrics.close_rate == 0.0


def test_a_show_up_rate_below_target_is_flagged_as_critical():
    metrics = FunnelMetrics(registrations=400, emails_sent=400, opens=200,
                            clicks=40, show_ups=100, offers=90, closes=12)
    flag = next(f for f in evaluate_metrics(metrics) if f.name == "Show up rate")
    assert flag.passing is False
    assert flag.severity == SEVERITY_CRITICAL
    assert flag.shortfall == pytest.approx(DEFAULT_THRESHOLDS.show_up_rate - 0.25)


def test_a_show_up_rate_on_target_is_not_flagged():
    metrics = FunnelMetrics(registrations=400, show_ups=200)
    flag = next(f for f in evaluate_metrics(metrics) if f.name == "Show up rate")
    assert flag.passing is True
    assert flag.severity == SEVERITY_OK


def test_the_show_up_flag_points_at_the_pipeline_before_the_audience():
    metrics = FunnelMetrics(registrations=400, show_ups=40)
    flag = next(f for f in evaluate_metrics(metrics) if f.name == "Show up rate")
    assert "Zoom link sync" in flag.note
    assert "reminder" in flag.note


def test_a_raised_threshold_can_flag_a_rate_that_passed_before():
    metrics = FunnelMetrics(registrations=400, show_ups=180)   # 45 percent
    assert next(f for f in evaluate_metrics(metrics)
                if f.name == "Show up rate").passing is True
    strict = Thresholds(show_up_rate=0.60)
    assert next(f for f in evaluate_metrics(metrics, strict)
                if f.name == "Show up rate").passing is False


def test_a_rate_exactly_on_target_passes():
    metrics = FunnelMetrics(registrations=400, show_ups=160)   # 40 percent
    assert next(f for f in evaluate_metrics(metrics)
                if f.name == "Show up rate").passing is True


def test_every_metric_is_evaluated_not_just_the_show_up_rate():
    names = [flag.name for flag in evaluate_metrics(FunnelMetrics())]
    assert names == ["Open rate", "Click rate", "Show up rate", "Close rate"]


def test_the_headline_reports_the_critical_metric_first():
    metrics = FunnelMetrics(registrations=400, emails_sent=400, opens=40,
                            clicks=4, show_ups=40, offers=30, closes=1)
    severity, headline = metrics_headline(evaluate_metrics(metrics), metrics)
    assert severity == SEVERITY_CRITICAL
    assert "Show up rate" in headline


def test_the_headline_turns_the_gap_into_people_who_never_arrived():
    metrics = FunnelMetrics(registrations=400, emails_sent=400, opens=200,
                            clicks=40, show_ups=100, offers=90, closes=12)
    _, headline = metrics_headline(evaluate_metrics(metrics), metrics)
    assert "60 people" in headline


def test_the_headline_falls_back_to_warnings_when_nothing_is_critical():
    metrics = FunnelMetrics(registrations=400, emails_sent=400, opens=40,
                            clicks=4, show_ups=200, offers=180, closes=30)
    severity, headline = metrics_headline(evaluate_metrics(metrics), metrics)
    assert severity == SEVERITY_WARNING
    assert "open rate" in headline


def test_a_healthy_cohort_reports_the_revenue_it_closed():
    metrics = FunnelMetrics(registrations=400, emails_sent=400, opens=200,
                            clicks=40, show_ups=200, offers=180, closes=30)
    severity, headline = metrics_headline(evaluate_metrics(metrics), metrics)
    assert severity == SEVERITY_OK
    assert "30 seats" in headline


def test_the_metric_table_shows_the_shortfall_only_where_there_is_one():
    metrics = FunnelMetrics(registrations=400, emails_sent=400, opens=200,
                            clicks=40, show_ups=100, offers=90, closes=12)
    rows = {row["Metric"]: row for row in metric_rows(evaluate_metrics(metrics))}
    assert rows["Show up rate"]["Shortfall"] != "None"
    assert rows["Open rate"]["Shortfall"] == "None"


def test_the_metric_table_holds_one_type_per_column_for_arrow():
    rows = metric_rows(evaluate_metrics(FunnelMetrics()))
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"column {column!r} mixes {sorted(kinds)}"


def test_the_stage_table_counts_the_drop_at_every_step():
    metrics = FunnelMetrics(registrations=400, emails_sent=400, opens=200,
                            clicks=40, show_ups=100, offers=90, closes=12)
    rows = {row["Stage"]: row for row in funnel_rows(metrics)}
    assert rows["Registered"]["Lost from previous stage"] == "n/a"
    assert rows["Opened"]["Lost from previous stage"] == "200"
    assert rows["Showed up"]["Of registrations"] == "25.0%"


def test_the_stage_table_holds_one_type_per_column_for_arrow():
    rows = funnel_rows(FunnelMetrics())
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"column {column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Prose discipline
# ---------------------------------------------------------------------------

def test_no_dash_characters_in_any_user_facing_prose():
    parts: list[str] = [MESSAGE_BY_READINESS[r] for r in READINESS]
    for mode in FAILURE_MODES:
        parts += [mode.label, mode.consequence, mode.fallback]
        run = run_pipeline(lead(), mode.code, max_retries=0)
        parts += [result.detail for result in run.results]
        alert = escalate(run)
        parts += [alert.headline, alert.summary, alert.fallback, alert.as_json()]
    for metrics in (FunnelMetrics(), FunnelMetrics(show_ups=40, closes=0)):
        flags = evaluate_metrics(metrics)
        parts += [flag.note for flag in flags]
        parts.append(metrics_headline(flags, metrics)[1])
    prose = "\n".join(parts)
    assert "—" not in prose
    assert "–" not in prose
