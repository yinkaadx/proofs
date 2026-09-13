"""Page tests for the Aerial Insights QA and Production Console, via AppTest.

Written for pytest.

Run: pytest tests/test_aerial_insights_qa_console_page.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.aerial_insights_qa_console.core import (  # noqa: E402
    ACCEPTED,
    BAD_SIGNATURE,
    CODE_CLEAN,
    CODE_LEAK,
    CODE_OOM,
    CODE_RACE,
    ERR_POOL_TIMEOUT,
    ERR_TOO_MANY,
    REPLAYED,
    STALE,
    sample_deliveries,
)

HARNESS = (Path(__file__).resolve().parent
           / "_page_harness_aerial_insights_qa_console.py")

ATOMIC = "Claim atomically, with FOR UPDATE SKIP LOCKED"
RELEASE = "Release tensors and images after each tile"
BOUNCER = "PgBouncer in transaction mode"
DRY_RUN = "Dry run completes"
EVIDENCE = "Evidence captured"
ROLLBACK = "Rollback rehearsed"


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


def set_toggle(at: AppTest, label: str, value: bool) -> AppTest:
    widget(at, "toggle", label).set_value(value).run()
    return at


def set_slider(at: AppTest, label: str, value) -> AppTest:
    widget(at, "slider", label).set_value(value).run()
    return at


def fixed_queue(at: AppTest | None = None) -> AppTest:
    at = at or run_app()
    at = set_toggle(at, ATOMIC, True)
    return set_toggle(at, RELEASE, True)


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------

def test_the_page_renders_with_no_exception():
    at = run_app()
    assert not at.exception, [str(e.value) for e in at.exception]


def test_the_hero_and_the_four_tabs_are_present():
    at = run_app()
    # The hero carries the registered title, ampersand included.
    # AppTest reports the markdown source, where that is &amp;.
    assert "Aerial Insights QA &amp; Production Console" in text_of(at)
    labels = [tab.label for tab in at.tabs]
    for expected in ("Worker Queue", "Prisma Auditor", "Stripe Ledger",
                     "Deployment Closure"):
        assert expected in labels


def test_no_two_widgets_of_a_kind_share_a_label():
    at = run_app()
    for kind in ("slider", "toggle", "selectbox", "button"):
        labels = [el.label for el in getattr(at, kind)]
        assert len(labels) == len(set(labels)), f"{kind}: {labels}"


def test_it_says_plainly_that_no_migration_is_ever_applied():
    assert "no migration is ever applied" in text_of(run_app())


# ---------------------------------------------------------------------------
# Worker queue
# ---------------------------------------------------------------------------

def test_the_queue_opens_broken_and_names_all_three_findings():
    body = text_of(run_app())
    assert CODE_RACE in body
    assert CODE_LEAK in body
    assert CODE_OOM in body


def test_the_race_finding_names_the_statement_that_fixes_it():
    body = text_of(run_app())
    assert "FOR UPDATE SKIP LOCKED" in body
    assert "nobody asked for" in body


def test_turning_on_the_atomic_claim_clears_the_race():
    at = set_toggle(run_app(), ATOMIC, True)
    body = text_of(at)
    assert "Two workers claimed the same tile" not in body


def test_turning_on_both_fixes_produces_a_clean_run():
    body = text_of(fixed_queue())
    assert CODE_CLEAN in body
    assert "ran clean" in body


def test_the_comparison_shows_the_same_batch_with_both_fixes_on():
    body = text_of(run_app())
    assert "Both fixes on" in body
    assert "Ticks to drain the batch" in body


def test_a_single_worker_cannot_race_itself():
    at = set_slider(run_app(), "Workers", 1)
    assert "Two workers claimed the same tile" not in text_of(at)


def test_a_bigger_batch_still_renders():
    at = set_slider(run_app(), "Tiles in the batch", 40)
    assert not at.exception, [str(e.value) for e in at.exception]
    assert CODE_OOM in text_of(at)


# ---------------------------------------------------------------------------
# Prisma auditor
# ---------------------------------------------------------------------------

def test_the_pool_opens_exhausted_with_the_prisma_error_code():
    body = text_of(run_app())
    assert ERR_TOO_MANY in body
    assert "runs out of connections" in body


def test_lowering_the_instance_count_clears_the_exhaustion():
    at = set_slider(run_app(), "Concurrent instances", 5)
    body = text_of(at)
    assert "The database runs out of connections" not in body


def test_adding_the_bouncer_fixes_it_but_flags_the_double_pool():
    at = set_toggle(run_app(), BOUNCER, True)
    body = text_of(at)
    assert "The database runs out of connections" not in body
    assert ERR_POOL_TIMEOUT in body
    assert "The bouncer is the" in body


def test_the_bouncer_with_a_single_connection_reports_nothing():
    at = set_toggle(run_app(), BOUNCER, True)
    at = set_slider(at, "Prisma connection_limit", 1)
    body = text_of(at)
    assert CODE_CLEAN in body


def test_both_connection_strings_are_shown():
    body = text_of(run_app())
    assert "pgbouncer=true" in body
    assert "connection_limit=1" in body
    assert "bouncer.internal:6432" in body


def test_the_password_is_left_as_an_environment_variable():
    body = text_of(run_app())
    assert "${DB_PASSWORD}" in body


# ---------------------------------------------------------------------------
# Stripe ledger
# ---------------------------------------------------------------------------

def test_all_four_gates_are_on_screen():
    body = text_of(run_app())
    assert ACCEPTED in body
    assert REPLAYED in body
    assert BAD_SIGNATURE in body
    assert STALE in body


def test_the_whole_set_charges_once():
    body = text_of(run_app())
    assert "249.00 taken across" in body


def test_stepping_back_shows_the_state_before_the_retry():
    at = set_slider(run_app(), "Deliveries received", 1)
    body = text_of(at)
    assert ACCEPTED in body
    assert REPLAYED not in body


def test_the_retry_explains_why_the_stored_response_is_returned():
    at = set_slider(run_app(), "Deliveries received", 2)
    body = text_of(at)
    assert "retries until it gets a 2xx" in body


def test_the_signature_header_is_shown_in_the_shape_stripe_sends_it():
    body = text_of(run_app())
    assert "Stripe-Signature: t=" in body
    assert "v1=" in body


def test_the_delivered_event_is_shown_as_json():
    body = text_of(run_app())
    assert '"livemode"' in body
    assert '"survey-pro-monthly"' in body


@pytest.mark.parametrize("count", range(1, len(sample_deliveries()) + 1))
def test_every_prefix_of_the_delivery_set_renders(count):
    at = set_slider(run_app(), "Deliveries received", count)
    assert not at.exception, [str(e.value) for e in at.exception]


# ---------------------------------------------------------------------------
# Deployment closure
# ---------------------------------------------------------------------------

def test_every_gate_passing_applies_the_migration():
    body = text_of(run_app())
    assert "Applied" in body
    assert "migrate deploy" in body


def test_a_failed_dry_run_leaves_production_untouched():
    at = set_toggle(run_app(), DRY_RUN, False)
    body = text_of(at)
    assert "Production untouched" in body
    assert "nothing is known about" in body


def test_missing_evidence_blocks_the_apply():
    at = set_toggle(run_app(), EVIDENCE, False)
    body = text_of(at)
    assert "Production untouched" in body
    assert "nothing to compare against" in body


def test_an_unrehearsed_rollback_blocks_the_apply():
    at = set_toggle(run_app(), ROLLBACK, False)
    body = text_of(at)
    assert "a paragraph, not a plan" in body
    assert "blocked, not skipped" in body


def test_the_terminal_shows_the_commands_that_would_run():
    body = text_of(run_app())
    assert "prisma migrate diff" in body
    assert "pg_dump --schema-only" in body
    assert "evidence/manifest.txt" in body


def test_no_dash_characters_reach_the_screen():
    at = set_toggle(run_app(), ROLLBACK, False)
    body = text_of(at)
    assert "—" not in body
    assert "–" not in body
