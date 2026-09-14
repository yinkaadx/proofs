"""Engine tests for the Aerial Insights QA and Production Console.

Written for pytest. Deterministic throughout: the scheduler is tick based and
nothing reads the clock or a random source, which is what makes a race
reproducible rather than occasional.

Run: pytest tests/test_aerial_insights_qa_console.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.aerial_insights_qa_console.core import (  # noqa: E402
    ACCEPTED,
    BAD_SIGNATURE,
    BASE_RSS_MB,
    CODE_CLEAN,
    CODE_LEAK,
    CODE_OOM,
    CODE_RACE,
    CONTAINER_LIMIT_MB,
    DEPLOY_STEPS,
    DONE,
    ERR_POOL_TIMEOUT,
    ERR_TOO_MANY,
    LEAK_MB_PER_TASK,
    LOST,
    PRODUCTION_URL_FIXED,
    REPLAYED,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SIGNATURE_TOLERANCE_S,
    STALE,
    STEP_BLOCKED,
    STEP_FAIL,
    STEP_PASS,
    TEST_SIGNING_SECRET,
    WRONG_MODE,
    PoolConfig,
    WebhookDelivery,
    WebhookLedger,
    audit_pool,
    handle_webhook,
    pool_comparison,
    replay_deliveries,
    run_deploy,
    run_queue,
    sample_deliveries,
    sample_tiles,
    sign,
    verify,
)

NOW = 1_789_000_000
PAYLOAD = {"customer": "cus_aerial_8841", "amount": 24900,
           "plan": "survey-pro-monthly"}


def delivery(event_id: str = "evt_1", event_type: str = "invoice.paid",
             payload: dict | None = None, timestamp: int = NOW,
             livemode: bool = True, signature: str | None = None):
    payload = PAYLOAD if payload is None else payload
    return WebhookDelivery(
        event_id, event_type, payload, timestamp,
        sign(payload, timestamp) if signature is None else signature,
        livemode)


def codes(findings) -> set:
    return {finding.code for finding in findings}


# ---------------------------------------------------------------------------
# Queue logic
# ---------------------------------------------------------------------------

def test_a_batch_is_built_by_rule_so_a_run_repeats():
    assert sample_tiles(8) == sample_tiles(8)
    assert len(sample_tiles(12)) == 12


def test_a_negative_batch_is_refused():
    with pytest.raises(ValueError):
        sample_tiles(-1)


def test_a_queue_with_no_workers_is_refused():
    with pytest.raises(ValueError):
        run_queue(sample_tiles(4), worker_count=0)


def test_without_an_atomic_claim_every_idle_worker_takes_the_same_tile():
    """The race, reproduced rather than sampled. Three workers polling on one
    tick all read the same head row as queued."""
    run = run_queue(sample_tiles(8), worker_count=3, atomic_claim=False,
                    releases_memory=True)
    assert run.double_claimed > 0
    assert run.wasted_inferences > 0
    assert CODE_RACE in codes(run.findings)


def test_an_atomic_claim_gives_every_worker_its_own_tile():
    run = run_queue(sample_tiles(8), worker_count=3, atomic_claim=True,
                    releases_memory=True)
    assert run.double_claimed == 0
    assert run.wasted_inferences == 0
    assert CODE_RACE not in codes(run.findings)


def test_one_worker_cannot_race_itself_which_is_why_staging_looks_fine():
    run = run_queue(sample_tiles(8), worker_count=1, atomic_claim=False,
                    releases_memory=True)
    assert run.double_claimed == 0


def test_every_tile_is_claimed_exactly_once_when_the_claim_is_atomic():
    run = run_queue(sample_tiles(16), worker_count=4, atomic_claim=True,
                    releases_memory=True)
    assert all(tile.claims == 1 for tile in run.tiles)


def test_the_batch_drains_faster_once_the_workers_stop_duplicating_work():
    slow = run_queue(sample_tiles(16), worker_count=3, atomic_claim=False,
                     releases_memory=True)
    quick = run_queue(sample_tiles(16), worker_count=3, atomic_claim=True,
                      releases_memory=True)
    assert quick.ticks < slow.ticks
    assert quick.completed == 16


def test_memory_climbs_by_the_leak_on_every_tile():
    run = run_queue(sample_tiles(4), worker_count=1, atomic_claim=True,
                    releases_memory=False)
    worker = run.workers[0]
    assert worker.rss_mb == BASE_RSS_MB + 4 * LEAK_MB_PER_TASK


def test_memory_stays_flat_once_the_tensors_are_released():
    run = run_queue(sample_tiles(20), worker_count=2, atomic_claim=True,
                    releases_memory=True)
    assert all(worker.rss_mb == BASE_RSS_MB for worker in run.workers)
    assert run.restarts == 0


def test_a_leaking_worker_is_killed_once_it_crosses_the_container_limit():
    """Thirty tiles over three workers is ten each, and a worker crosses the
    limit on its ninth: (2048 - 512) / 180 is 8.5. Twenty four tiles is eight
    each, which survives, and is exactly why a smaller batch looks fine."""
    survives = run_queue(sample_tiles(24), worker_count=3, atomic_claim=True,
                         releases_memory=False)
    assert survives.restarts == 0

    run = run_queue(sample_tiles(30), worker_count=3, atomic_claim=True,
                    releases_memory=False)
    assert run.restarts > 0
    assert CODE_OOM in codes(run.findings)


def test_a_tile_held_by_a_killed_worker_is_lost_rather_than_retried():
    """The row still reads running, so nothing picks it up again."""
    run = run_queue(sample_tiles(30), worker_count=3, atomic_claim=True,
                    releases_memory=False)
    assert run.lost > 0
    assert any(tile.status == LOST for tile in run.tiles)
    assert run.completed < len(run.tiles)


def test_the_leak_is_reported_even_before_a_worker_dies():
    """A batch too small to kill anything still carries the defect."""
    run = run_queue(sample_tiles(4), worker_count=3, atomic_claim=True,
                    releases_memory=False)
    assert run.restarts == 0
    assert CODE_LEAK in codes(run.findings)


def test_both_fixes_together_produce_a_clean_run():
    run = run_queue(sample_tiles(24), worker_count=3, atomic_claim=True,
                    releases_memory=True)
    assert run.healthy is True
    assert codes(run.findings) == {CODE_CLEAN}
    assert run.completed == 24
    assert run.lost == 0


def test_a_clean_run_leaves_no_tile_running():
    run = run_queue(sample_tiles(12), worker_count=3, atomic_claim=True,
                    releases_memory=True)
    assert all(tile.status == DONE for tile in run.tiles)


def test_every_finding_names_the_change_that_fixes_it():
    run = run_queue(sample_tiles(24), worker_count=3)
    for finding in run.findings:
        assert finding.fix
        assert len(finding.detail) > 40
    assert "SKIP LOCKED" in " ".join(f.fix for f in run.findings)


def test_the_queue_tables_are_arrow_safe():
    run = run_queue(sample_tiles(24), worker_count=3)
    for rows in (run.tile_rows(), run.worker_rows(), run.finding_rows()):
        for column in {key for row in rows for key in row}:
            kinds = {type(row[column]).__name__ for row in rows}
            assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Connection validation
# ---------------------------------------------------------------------------

def test_the_demand_is_instances_times_the_pool_each_one_keeps():
    config = PoolConfig(instances=40, connection_limit=5)
    assert config.demanded == 200
    assert config.available == 97
    assert config.exhausted is True


def test_the_database_running_out_reports_the_prisma_error_code():
    findings = audit_pool(PoolConfig(instances=40, connection_limit=5))
    assert ERR_TOO_MANY in codes(findings)
    assert any(f.severity == SEVERITY_CRITICAL for f in findings)


def test_a_bouncer_holds_a_small_fixed_pool_however_many_instances_there_are():
    """Client count stops being the database's problem, which is the point."""
    few = PoolConfig(instances=10, connection_limit=1, pgbouncer=True)
    many = PoolConfig(instances=400, connection_limit=1, pgbouncer=True)
    assert few.demanded == many.demanded
    assert many.exhausted is False


def test_the_bouncer_fixes_the_configuration_that_was_exhausting_the_database():
    broken = PoolConfig(instances=40, connection_limit=5)
    fixed = PoolConfig(instances=40, connection_limit=1, pgbouncer=True)
    assert broken.exhausted is True
    assert fixed.exhausted is False
    assert codes(audit_pool(fixed)) == {CODE_CLEAN}


def test_pooling_twice_in_series_is_flagged_even_though_it_is_not_exhausted():
    findings = audit_pool(PoolConfig(instances=40, connection_limit=5,
                                     pgbouncer=True))
    assert ERR_POOL_TIMEOUT in codes(findings)
    assert "The bouncer is the" in " ".join(f.fix for f in findings)


def test_a_pool_close_to_its_limit_is_warned_about_before_it_gives_way():
    config = PoolConfig(max_connections=100, instances=18, connection_limit=5)
    assert config.exhausted is False
    assert config.utilisation > 0.8
    assert ERR_POOL_TIMEOUT in codes(audit_pool(config))


def test_a_comfortable_pool_reports_nothing():
    config = PoolConfig(instances=5, connection_limit=5)
    assert codes(audit_pool(config)) == {CODE_CLEAN}
    assert audit_pool(config)[0].severity == SEVERITY_OK


def test_a_timeout_shorter_than_a_cold_query_is_flagged():
    findings = audit_pool(PoolConfig(instances=5, connection_limit=2,
                                     pool_timeout_s=3))
    assert ERR_POOL_TIMEOUT in codes(findings)


def test_utilisation_does_not_divide_by_zero_on_an_empty_database():
    config = PoolConfig(max_connections=3, reserved_for_superuser=3)
    assert config.available == 0
    assert config.utilisation == 0.0


def test_the_comparison_shows_the_same_load_both_ways():
    rows = {row["Measure"]: row
            for row in pool_comparison(PoolConfig(instances=40,
                                                  connection_limit=5))}
    assert rows["Connections opened"]["Direct"] == "200"
    assert rows["Connections opened"]["With PgBouncer"] == "20"
    assert rows["Outcome"]["Direct"] == ERR_TOO_MANY
    assert rows["Outcome"]["With PgBouncer"] == "Holds"


def test_the_fixed_url_carries_the_settings_the_findings_ask_for():
    assert "pgbouncer=true" in PRODUCTION_URL_FIXED
    assert "connection_limit=1" in PRODUCTION_URL_FIXED


def test_the_pool_tables_are_arrow_safe():
    config = PoolConfig()
    for rows in (config.rows(), pool_comparison(config)):
        for column in {key for row in rows for key in row}:
            kinds = {type(row[column]).__name__ for row in rows}
            assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Stripe idempotency
# ---------------------------------------------------------------------------

def test_a_signature_verifies_against_the_body_and_the_timestamp():
    ok, failure = verify(delivery(), NOW)
    assert ok is True
    assert failure == ""


def test_a_signature_over_a_different_body_does_not_verify():
    forged = WebhookDelivery("evt_x", "invoice.paid",
                             {**PAYLOAD, "amount": 1}, NOW,
                             sign(PAYLOAD, NOW))
    assert verify(forged, NOW)[0] is False


def test_a_signature_computed_with_another_secret_does_not_verify():
    other = WebhookDelivery("evt_x", "invoice.paid", PAYLOAD, NOW,
                            sign(PAYLOAD, NOW, "another-test-secret"))
    assert verify(other, NOW)[0] is False


def test_the_signature_is_a_stripe_shaped_header():
    header = sign(PAYLOAD, NOW)
    assert header.startswith(f"t={NOW},v1=")
    assert len(header.split("v1=")[1]) == 64


def test_a_valid_event_is_accepted_once_and_charges_once():
    ledger = WebhookLedger()
    result = handle_webhook(ledger, delivery(), NOW)
    assert result.outcome == ACCEPTED
    assert result.charged_cents == 24900
    assert ledger.charged_cents == 24900


def test_the_retry_returns_the_stored_response_rather_than_charging_again():
    """Stripe retries until it gets a 2xx, so the second delivery must be
    answered with the first response rather than reprocessed."""
    ledger = WebhookLedger()
    first = handle_webhook(ledger, delivery(), NOW)
    again = handle_webhook(ledger, delivery(), NOW)
    assert again.outcome == REPLAYED
    assert again.charged_cents == 0
    assert again.stored_response == first.stored_response
    assert ledger.charged_cents == 24900


def test_twenty_retries_still_charge_once():
    ledger = WebhookLedger()
    for _ in range(20):
        handle_webhook(ledger, delivery(), NOW)
    assert ledger.charged_cents == 24900
    assert ledger.count(REPLAYED) == 19


def test_an_unsigned_request_never_reaches_the_handler():
    ledger = WebhookLedger()
    result = handle_webhook(ledger, delivery(signature="t=1,v1=" + "0" * 64),
                            NOW)
    assert result.outcome == BAD_SIGNATURE
    assert ledger.handled == {}
    assert ledger.charged_cents == 0


def test_a_request_captured_earlier_is_refused_on_its_timestamp():
    """The signature is genuine. The timestamp is what stops a replay."""
    old = delivery(timestamp=NOW - 3600)
    assert verify(old, NOW - 3600)[0] is True
    ledger = WebhookLedger()
    assert handle_webhook(ledger, old, NOW).outcome == STALE


def test_the_tolerance_turns_exactly_where_it_is_declared():
    ledger = WebhookLedger()
    inside = delivery(event_id="evt_in",
                      timestamp=NOW - SIGNATURE_TOLERANCE_S)
    outside = delivery(event_id="evt_out",
                       timestamp=NOW - SIGNATURE_TOLERANCE_S - 1)
    assert handle_webhook(ledger, inside, NOW).outcome == ACCEPTED
    assert handle_webhook(ledger, outside, NOW).outcome == STALE


def test_a_test_event_cannot_grant_a_live_entitlement():
    ledger = WebhookLedger(livemode=True)
    result = handle_webhook(ledger, delivery(livemode=False), NOW)
    assert result.outcome == WRONG_MODE
    assert ledger.charged_cents == 0


def test_an_event_that_is_not_a_payment_charges_nothing():
    ledger = WebhookLedger()
    result = handle_webhook(ledger, delivery(event_type="customer.updated"),
                            NOW)
    assert result.outcome == ACCEPTED
    assert result.charged_cents == 0


def test_the_sample_set_exercises_all_four_gates():
    ledger = replay_deliveries(sample_deliveries(NOW), NOW)
    outcomes = [result.outcome for result in ledger.results]
    assert outcomes == [ACCEPTED, REPLAYED, BAD_SIGNATURE, STALE]
    assert ledger.charged_cents == 24900


def test_replaying_the_whole_set_twice_charges_nothing_extra():
    once = replay_deliveries(sample_deliveries(NOW), NOW)
    twice = replay_deliveries(sample_deliveries(NOW), NOW,
                              replay_deliveries(sample_deliveries(NOW), NOW))
    assert twice.charged_cents == once.charged_cents


def test_the_signing_secret_in_this_module_is_not_credential_shaped():
    """Nothing in a repository should look like a credential, even when it is
    not one, and a scanner refusing the push would be right to."""
    assert not TEST_SIGNING_SECRET.startswith("whsec_")
    assert "not-a-credential" in TEST_SIGNING_SECRET


def test_the_webhook_table_is_arrow_safe():
    rows = replay_deliveries(sample_deliveries(NOW), NOW).rows()
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Deployment closure
# ---------------------------------------------------------------------------

def test_every_gate_passing_applies_the_migration():
    result = run_deploy()
    assert result.applied is True
    assert result.blocked_reason == ""
    assert all(status == STEP_PASS for status in result.statuses.values())


def test_a_failed_dry_run_blocks_everything_after_it():
    result = run_deploy(dry_run_ok=False)
    assert result.applied is False
    assert result.statuses["dry_run"] == STEP_FAIL
    assert result.statuses["evidence"] == STEP_BLOCKED
    assert result.statuses["apply"] == STEP_BLOCKED


def test_missing_evidence_blocks_the_apply():
    result = run_deploy(evidence_ok=False)
    assert result.applied is False
    assert "nothing to compare against" in result.blocked_reason


def test_an_unrehearsed_rollback_blocks_the_apply():
    result = run_deploy(rollback_ok=False)
    assert result.applied is False
    assert result.statuses["rollback"] == STEP_FAIL
    assert "a paragraph, not a plan" in result.blocked_reason


def test_a_blocked_step_is_blocked_rather_than_skipped_quietly():
    """A pipeline that continues past a failed gate and reports success is
    worse than one with no gate at all."""
    result = run_deploy(dry_run_ok=False)
    assert STEP_BLOCKED in result.terminal()
    assert "[pass] Migration applied to production" not in result.terminal()


def test_the_terminal_shows_the_command_for_every_step():
    terminal = run_deploy().terminal()
    for step in DEPLOY_STEPS:
        assert step.command.split()[0] in terminal
    assert "[pass] Migration applied to production" in terminal


def test_every_step_names_the_evidence_file_it_leaves_behind():
    for step in DEPLOY_STEPS:
        assert step.evidence.startswith("evidence/")


def test_the_dry_run_reports_what_the_migration_would_do():
    dry_run = DEPLOY_STEPS[0]
    assert "No destructive operation detected" in dry_run.output
    assert "ALTER TABLE" in dry_run.output


def test_the_deploy_table_is_arrow_safe():
    rows = run_deploy(rollback_ok=False).rows()
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Prose discipline
# ---------------------------------------------------------------------------

def test_no_dash_characters_in_any_user_facing_prose():
    parts: list[str] = []
    for run in (run_queue(sample_tiles(24), worker_count=3),
                run_queue(sample_tiles(8), worker_count=2, atomic_claim=True,
                          releases_memory=True)):
        for finding in run.findings:
            parts += [finding.title, finding.detail, finding.fix]
    for config in (PoolConfig(), PoolConfig(instances=5, connection_limit=2),
                   PoolConfig(pgbouncer=True, connection_limit=5)):
        for finding in audit_pool(config):
            parts += [finding.title, finding.detail, finding.fix]
    for result in replay_deliveries(sample_deliveries(NOW), NOW).results:
        parts.append(result.detail)
    for flags in ((True, True, True), (False, True, True), (True, False, True),
                  (True, True, False)):
        deploy = run_deploy(*flags)
        parts += [deploy.blocked_reason, deploy.terminal()]
    prose = "\n".join(parts)
    assert "—" not in prose
    assert "–" not in prose
