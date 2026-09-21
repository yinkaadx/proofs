"""Tests for the RevOps EA and Automation Console.

The triage tests are about the verdict the founder never sees, because
Archive is the only routing mistake that is not recoverable by the person
it was made for. The chase tests are about the ladder having a last rung,
because a chase that never ends is the real failure of an assistant
function.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

from tools.revops_ea_automation_console.core import (
    ARCHIVE,
    CHASE_CHASE,
    CHASE_NUDGE,
    CHASE_PULL,
    CHASE_WAIT,
    CHASE_WARN,
    DRAFT_READY,
    ENGINE_VERSION,
    FOUNDER_ACTION,
    LOGGED,
    MAX_CHASE_MESSAGES,
    PULL_THRESHOLD_DAYS,
    REFUSED,
    ROUTES,
    SAMPLE_EMAILS,
    SAMPLE_NOTES,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    evaluate_contractor_chase,
    log_call_decision,
    simulate_inbox_triage,
)
from tools.registry import all_tools

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL_DIR = ROOT / "tools" / "revops_ea_automation_console"


# ---------------------------------------------------------------------------
# Inbox triage
# ---------------------------------------------------------------------------

def test_a_contract_with_a_deadline_reaches_the_founder():
    result = simulate_inbox_triage(
        "Our MSA renewal needs pricing confirmed by EOD Friday.")
    assert result.route == FOUNDER_ACTION
    assert result.escalate_signals


def test_a_scheduling_request_is_draftable():
    result = simulate_inbox_triage(
        "Are you available for a call next week? Happy to work around your "
        "calendar.")
    assert result.route == DRAFT_READY


def test_a_bulk_mailing_is_archived_on_positive_evidence():
    result = simulate_inbox_triage(
        "This is an automated message from our weekly digest. Unsubscribe at "
        "any time.")
    assert result.route == ARCHIVE
    assert result.noise_signals


def test_an_unrecognised_message_goes_to_the_founder_not_the_archive():
    """Absence of evidence is never grounds for archiving. The queue of
    unrecognised mail is the list of rules still missing."""
    result = simulate_inbox_triage(
        "Following on from the thing we discussed, I had a thought.")
    assert result.route == FOUNDER_ACTION
    assert "TRI-UNKNOWN" in {f.code for f in result.findings}


def test_an_empty_message_is_escalated_rather_than_archived():
    result = simulate_inbox_triage("")
    assert result.route == FOUNDER_ACTION
    assert result.summary == ""


def test_escalation_beats_a_draftable_signal_in_the_same_message():
    """A polite scheduling request wrapped around a cancellation is a
    cancellation."""
    result = simulate_inbox_triage(
        "Thanks for the update, and are you available next week? We are "
        "cancelling the contract either way.")
    assert result.draftable_signals
    assert result.route == FOUNDER_ACTION


def test_escalation_beats_a_noise_signal_in_the_same_message():
    """An invoice inside a newsletter template is still an invoice, and the
    unsubscribe footer must not archive it."""
    result = simulate_inbox_triage(
        "Your invoice is overdue and payment is required. Unsubscribe at any "
        "time.")
    assert result.noise_signals
    assert result.route == FOUNDER_ACTION


def test_archive_is_never_reached_with_any_escalation_signal_present():
    """The property the whole router rests on, over every sample and over
    every escalation phrase."""
    phrases = ("contract", "invoice", "legal", "terminate", "urgent",
               "renewal", "complaint", "signature")
    for phrase in phrases:
        result = simulate_inbox_triage(
            f"Unsubscribe at any time. This is an automated message "
            f"regarding your {phrase}.")
        assert result.route != ARCHIVE, phrase
        assert result.route == FOUNDER_ACTION, phrase


def test_every_route_is_one_of_the_three_declared_routes():
    for _, body in SAMPLE_EMAILS:
        assert simulate_inbox_triage(body).route in ROUTES


def test_the_summary_is_extractive_and_never_generated():
    """A summary that paraphrases a figure is a summary the founder acts on
    and cannot check."""
    body = ("We need the revised figure of 42500 confirmed by Friday. "
            "Please sign the amendment.")
    result = simulate_inbox_triage(body)
    assert result.grounded
    assert result.summary
    assert result.summary.split(".")[0] in body


def test_the_summary_invents_no_figure_that_was_not_in_the_message():
    body = "Please confirm whether the approval is still on track."
    result = simulate_inbox_triage(body)
    assert not any(ch.isdigit() for ch in result.summary)


# ---------------------------------------------------------------------------
# Contractor chase
# ---------------------------------------------------------------------------

def test_day_four_gives_the_explicit_monday_recommendation():
    verdict = evaluate_contractor_chase(4)
    assert verdict.recommendation == (
        "Four days. Still nothing, I would give it until Monday then pull "
        "the task.")
    assert verdict.stage == CHASE_CHASE


def test_the_first_two_days_recommend_no_action():
    for day in (0, 1):
        verdict = evaluate_contractor_chase(day)
        assert verdict.stage == CHASE_WAIT, day
        assert verdict.chase_messages_sent == 0, day


def test_the_ladder_walks_wait_nudge_chase_warn_pull():
    seen = [evaluate_contractor_chase(day).stage for day in range(0, 8)]
    assert seen == [CHASE_WAIT, CHASE_WAIT, CHASE_NUDGE, CHASE_CHASE,
                    CHASE_CHASE, CHASE_WARN, CHASE_WARN, CHASE_PULL]


def test_the_recommendation_never_softens_as_the_wait_grows():
    ranks = [evaluate_contractor_chase(day).rank for day in range(0, 30)]
    assert ranks == sorted(ranks)


def test_the_ladder_has_a_terminal_state_and_stays_there():
    """A chase with no last rung is a habit, not a process."""
    for day in range(PULL_THRESHOLD_DAYS, 60):
        verdict = evaluate_contractor_chase(day)
        assert verdict.terminal, day
        assert verdict.stage == CHASE_PULL, day
        assert verdict.rank == 4, day


def test_nothing_before_the_threshold_is_terminal():
    for day in range(0, PULL_THRESHOLD_DAYS):
        assert not evaluate_contractor_chase(day).terminal, day


def test_the_chase_message_budget_is_never_exceeded():
    for day in range(0, 60):
        verdict = evaluate_contractor_chase(day)
        assert 0 <= verdict.chase_messages_sent <= MAX_CHASE_MESSAGES, day


def test_every_rung_states_a_decision_rather_than_a_feeling():
    for day in range(0, 12):
        text = evaluate_contractor_chase(day).recommendation
        assert len(text) > 40, day
        assert text[0].isupper(), day


def test_the_pull_verdict_carries_a_critical_finding():
    verdict = evaluate_contractor_chase(PULL_THRESHOLD_DAYS)
    assert any(f.severity == SEVERITY_CRITICAL for f in verdict.findings)
    assert "CHS-PULL" in {f.code for f in verdict.findings}


def test_a_negative_wait_raises():
    with pytest.raises(ValueError):
        evaluate_contractor_chase(-1)


# ---------------------------------------------------------------------------
# Call decision ledger
# ---------------------------------------------------------------------------

def test_a_decision_with_an_artifact_is_logged_with_a_check_marker():
    ledger = log_call_decision(
        "Yinka will send pricing_v3.xlsx by Friday")
    entry = ledger.entries[0]
    assert entry.status == LOGGED
    assert entry.owner == "Yinka"
    assert entry.due == "Friday"
    assert entry.check_marker.startswith("CHECK:")
    assert "pricing_v3.xlsx" in entry.check_marker


def test_a_deliverable_with_no_runnable_check_is_refused():
    """The only way to know whether it happened would be to ask the person
    who owns it, which is a conversation rather than a status."""
    ledger = log_call_decision("Dele will look at the onboarding thing")
    entry = ledger.entries[0]
    assert entry.status == REFUSED
    assert not entry.verifiable
    assert entry.check_marker == ""
    assert "DEC-CHECK" in {f.code for f in entry.findings}


def test_a_refused_entry_emits_no_partial_artifact():
    """A half filled decision in a ledger reads as a decision that was
    made."""
    ledger = log_call_decision("Sam will get it sorted")
    assert ledger.logged == 0
    assert all(e.check_marker == "" for e in ledger.entries if not e.logged)


def test_a_line_with_no_owner_is_kept_rather_than_discarded():
    """The dangerous version of this tool is the one that silently drops
    what it could not read."""
    ledger = log_call_decision("We agreed the new positioning is stronger")
    assert ledger.entries == ()
    assert ledger.unparsed == ("We agreed the new positioning is stronger",)
    assert "DEC-UNPARSED" in {f.code for f in ledger.findings}


def test_the_ledger_totals_equal_the_lines_in():
    ledger = log_call_decision(SAMPLE_NOTES)
    assert ledger.logged + ledger.refused + len(ledger.unparsed) == \
        ledger.lines_in
    assert ledger.lines_in == 5


def test_each_verifiable_kind_produces_a_check_marker():
    cases = {
        "Ana will publish https://example.com/report": "https://example.com",
        "Bo will close RVP-482": "RVP-482",
        "Cal will update the HubSpot pipeline": "HubSpot",
        "Dee will deliver 500 leads": "500 leads",
        "Eve will ship notes.md": "notes.md",
    }
    for line, expected in cases.items():
        entry = log_call_decision(line).entries[0]
        assert entry.status == LOGGED, line
        assert expected in entry.check_marker, line


def test_a_missing_date_is_flagged_without_refusing_the_entry():
    ledger = log_call_decision("Sam to update the HubSpot deal stage")
    entry = ledger.entries[0]
    assert entry.logged
    assert entry.due == ""
    assert "DEC-DUE" in {f.code for f in entry.findings}


def test_empty_notes_produce_an_empty_ledger_rather_than_an_error():
    ledger = log_call_decision("")
    assert ledger.entries == ()
    assert ledger.lines_in == 0


# ---------------------------------------------------------------------------
# House invariants
# ---------------------------------------------------------------------------

def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    """The engine has to run outside the app, or it is not an engine."""
    script = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.revops_ea_automation_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT))
    done = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    for name in ("core.py", "page.py"):
        text = (TOOL_DIR / name).read_text(encoding="utf-8")
        assert "—" not in text, name
        assert "–" not in text, name


def test_the_tool_is_registered_with_a_unique_icon():
    tools = [t for t in all_tools() if not t.key.startswith("synthetic-")]
    keys = [t.key for t in tools]
    assert "revops-ea-automation-console" in keys
    icons = [t.icon for t in tools]
    assert len(icons) == len(set(icons))
    assert SEVERITY_OK != SEVERITY_CRITICAL
