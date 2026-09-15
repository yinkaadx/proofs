"""Engine tests for the Mobile QA Bug Bash Console.

Written for pytest. Deterministic: nothing in the engine reads the clock or a
random source, so the same bug produces the same payload every time.

Run: pytest tests/test_mobile_qa_bug_bash_console.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.mobile_qa_bug_bash_console.core import (  # noqa: E402
    ASANA_PROJECT_GID,
    ENGINE_VERSION,
    FREQUENCIES,
    GAP_MISSING,
    GAP_UNDESIGNED,
    NOTIFY_ON_CALL,
    REPRODUCIBILITY_FIELDS,
    REQUIRED_FIELDS,
    SEV_BLOCKER,
    SEV_CRITICAL,
    SEV_MAJOR,
    SEV_MINOR,
    SEVERITIES,
    SEVERITY_ASSIGNEE,
    SEVERITY_SLA_HOURS,
    SLACK_CHANNEL,
    all_test_cases,
    assess_report,
    audit_figma_gaps,
    bug_bash_summary,
    dispatch_rows,
    figma_rows,
    get_sample_bug_report,
    map_reviews_to_test_cases,
    report_lines,
    review_rows,
    severity_rank,
    simulate_asana_slack_dispatch,
)


# ---------------------------------------------------------------------------
# The bug report structure
# ---------------------------------------------------------------------------

def test_the_sample_report_carries_every_required_field():
    report = get_sample_bug_report()
    assert set(REQUIRED_FIELDS).issubset(report)


def test_the_eleven_named_fields_are_the_required_ones():
    for name in ("Title", "Severity", "Frequency", "Device", "OS Version",
                 "Build Number", "Preconditions", "Steps to Reproduce",
                 "Expected Behavior", "Actual Behavior", "Log Snippet"):
        assert name in REQUIRED_FIELDS, name
    assert len(REQUIRED_FIELDS) == 11


def test_no_field_in_the_sample_is_a_placeholder():
    """A worked example with 'enter your device here' in it teaches nothing."""
    report = get_sample_bug_report()
    for name, value in report.items():
        text = " ".join(value) if isinstance(value, list) else str(value)
        assert text.strip(), name
        for placeholder in ("TODO", "TBD", "xxx", "<", "[insert"):
            assert placeholder.lower() not in text.lower(), (name, placeholder)


def test_the_steps_and_preconditions_are_ordered_lists_not_prose():
    report = get_sample_bug_report()
    assert isinstance(report["Steps to Reproduce"], list)
    assert isinstance(report["Preconditions"], list)
    assert len(report["Steps to Reproduce"]) >= 3


def test_the_severity_and_frequency_come_from_the_known_sets():
    report = get_sample_bug_report()
    assert report["Severity"] in SEVERITIES
    assert report["Frequency"] in FREQUENCIES


def test_the_build_number_identifies_a_build_not_just_a_version():
    """Version alone is ambiguous across a release candidate cycle."""
    build = get_sample_bug_report()["Build Number"]
    assert "build" in build.lower()
    assert any(char.isdigit() for char in build)


def test_the_log_snippet_carries_the_exception_and_a_line_number():
    log = get_sample_bug_report()["Log Snippet"]
    assert "Exception" in log or "EXCEPTION" in log
    assert ".kt:" in log or ".java:" in log


def test_the_actual_behavior_states_how_often_it_reproduced():
    assert "5 times out of 5" in get_sample_bug_report()["Actual Behavior"]


# ---------------------------------------------------------------------------
# Scoring a report
# ---------------------------------------------------------------------------

def test_the_sample_report_scores_complete_and_reproducible():
    quality = assess_report()
    assert quality.complete
    assert quality.reproducible
    assert quality.score == 1.0
    assert quality.verdict == "Ready to assign"


def test_an_empty_string_counts_as_missing_rather_than_present():
    """A field somebody tabbed through is exactly as useless as an absent one,
    and counting it as present is how a thin report is called complete."""
    report = get_sample_bug_report()
    report["Build Number"] = "   "
    quality = assess_report(report)
    assert "Build Number" in quality.empty
    assert not quality.complete
    assert quality.score < 1.0


@pytest.mark.parametrize("field", REPRODUCIBILITY_FIELDS)
def test_losing_any_reproducibility_field_makes_the_report_unactionable(field):
    report = get_sample_bug_report()
    report.pop(field)
    quality = assess_report(report)
    assert not quality.reproducible
    assert quality.verdict == "Cannot be reproduced, send it back"


def test_losing_a_non_reproducibility_field_is_a_gap_not_a_blocker():
    report = get_sample_bug_report()
    report.pop("Log Snippet")
    quality = assess_report(report)
    assert quality.reproducible
    assert not quality.complete
    assert quality.verdict == "Actionable with gaps"


def test_an_empty_report_scores_zero_and_names_every_field():
    quality = assess_report({})
    assert quality.score == 0.0
    assert len(quality.missing) == len(REQUIRED_FIELDS)
    assert not quality.reproducible


def test_assessing_none_falls_back_to_the_sample():
    assert assess_report(None).complete


def test_the_field_table_is_arrow_safe():
    for row in assess_report().rows():
        assert all(isinstance(v, str) for v in row.values()), row


def test_the_pasteable_report_contains_every_field_heading():
    text = report_lines()
    for name in REQUIRED_FIELDS:
        assert f"{name}:" in text, name


def test_the_pasteable_report_numbers_the_steps():
    assert "  1. Open the app" in report_lines()


# ---------------------------------------------------------------------------
# Reviews mapped to test cases
# ---------------------------------------------------------------------------

def test_every_category_carries_at_least_one_test_case():
    for entry in map_reviews_to_test_cases():
        assert entry.test_cases
        assert entry.example_review
        assert entry.review_count > 0


def test_categories_are_ranked_worst_severity_first():
    entries = map_reviews_to_test_cases()
    ranks = [entry.rank for entry in entries]
    assert ranks == sorted(ranks)
    assert entries[0].worst_severity == SEV_BLOCKER


def test_the_loudest_category_does_not_outrank_the_worst_one():
    """Logged out has the most reviews; crashes on payment is worse."""
    entries = map_reviews_to_test_cases()
    loudest = max(entries, key=lambda e: e.review_count)
    assert entries[0].review_count < loudest.review_count
    assert entries[0].rank < loudest.rank


def test_a_categorys_worst_severity_is_the_worst_of_its_cases():
    for entry in map_reviews_to_test_cases():
        worst = min(case.rank for case in entry.test_cases)
        assert severity_rank(entry.worst_severity) == worst


def test_every_test_case_has_an_id_a_severity_and_steps():
    seen = set()
    for case in all_test_cases():
        assert case.case_id.startswith("TC-")
        assert case.case_id not in seen, f"duplicate {case.case_id}"
        seen.add(case.case_id)
        assert case.severity in SEVERITIES
        assert len(case.steps) >= 2


def test_all_test_cases_are_sorted_worst_first():
    ranks = [case.rank for case in all_test_cases()]
    assert ranks == sorted(ranks)


def test_severity_rank_puts_an_unknown_value_last_rather_than_crashing():
    assert severity_rank(SEV_BLOCKER) < severity_rank(SEV_MINOR)
    assert severity_rank("S9 Whatever") > severity_rank(SEV_MINOR)


def test_the_review_rows_are_arrow_safe():
    for row in review_rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# The Asana and Slack dispatch
# ---------------------------------------------------------------------------

def test_the_asana_task_name_carries_the_bug_id():
    """The name is the only part that survives into Slack, a search box and
    somebody reading it aloud on a call."""
    dispatch = simulate_asana_slack_dispatch("BUG-4821", "Crash on pay",
                                             SEV_BLOCKER)
    assert dispatch.asana["data"]["name"] == "[BUG-4821] Crash on pay"


def test_the_asana_task_lands_in_the_right_project():
    dispatch = simulate_asana_slack_dispatch()
    assert dispatch.asana["data"]["projects"] == [ASANA_PROJECT_GID]


@pytest.mark.parametrize("severity", SEVERITIES)
def test_severity_decides_the_assignee_and_the_response_target(severity):
    dispatch = simulate_asana_slack_dispatch("BUG-1", "x", severity)
    assert dispatch.asana["data"]["assignee"] == SEVERITY_ASSIGNEE[severity]
    fields = dispatch.asana["data"]["custom_fields"]
    assert fields["sla_hours"] == SEVERITY_SLA_HOURS[severity]
    assert fields["severity"] == severity


def test_only_the_top_two_severities_page_the_on_call():
    for severity in SEVERITIES:
        dispatch = simulate_asana_slack_dispatch("BUG-1", "x", severity)
        expected = severity in NOTIFY_ON_CALL
        assert dispatch.notified_on_call is expected
        assert dispatch.slack["notify_on_call"] is expected


def test_a_blocker_and_a_minor_do_not_reach_the_same_person():
    assert SEVERITY_ASSIGNEE[SEV_BLOCKER] != SEVERITY_ASSIGNEE[SEV_MINOR]


def test_the_response_targets_get_looser_as_severity_falls():
    hours = [SEVERITY_SLA_HOURS[s] for s in SEVERITIES]
    assert hours == sorted(hours)


def test_the_slack_payload_names_the_channel_and_the_bug_in_its_text():
    dispatch = simulate_asana_slack_dispatch("BUG-77", "Login loops",
                                             SEV_CRITICAL)
    assert dispatch.slack["channel"] == SLACK_CHANNEL
    assert "BUG-77" in dispatch.slack["text"]
    assert SEV_CRITICAL in dispatch.slack["text"]


def test_the_slack_attachment_colour_tracks_severity():
    critical = simulate_asana_slack_dispatch("B", "x", SEV_BLOCKER)
    minor = simulate_asana_slack_dispatch("B", "x", SEV_MINOR)
    assert critical.slack["attachments"][0]["color"] != \
        minor.slack["attachments"][0]["color"]


def test_the_slack_attachment_carries_every_field_a_responder_needs():
    dispatch = simulate_asana_slack_dispatch()
    titles = {f["title"] for f in dispatch.slack["attachments"][0]["fields"]}
    assert {"Bug", "Severity", "Assigned to", "Response target",
            "Summary"} <= titles


def test_both_payloads_serialise_as_json():
    dispatch = simulate_asana_slack_dispatch()
    assert json.loads(dispatch.asana_json()) == dispatch.asana
    assert json.loads(dispatch.slack_json()) == dispatch.slack


def test_dispatch_is_deterministic():
    first = simulate_asana_slack_dispatch("BUG-9", "Same bug", SEV_MAJOR)
    second = simulate_asana_slack_dispatch("BUG-9", "Same bug", SEV_MAJOR)
    assert first.asana == second.asana
    assert first.slack == second.slack


def test_a_blank_id_or_title_is_filled_rather_than_sent_empty():
    """An empty task name creates a ticket nobody can find again."""
    dispatch = simulate_asana_slack_dispatch("   ", "   ", SEV_MAJOR)
    assert dispatch.bug_id == "BUG-UNKNOWN"
    assert dispatch.title == "Untitled defect"
    assert "[BUG-UNKNOWN] Untitled defect" == dispatch.asana["data"]["name"]


def test_whitespace_around_the_id_and_title_is_trimmed():
    dispatch = simulate_asana_slack_dispatch("  BUG-3  ", "  Crash  ",
                                             SEV_MAJOR)
    assert dispatch.asana["data"]["name"] == "[BUG-3] Crash"


def test_an_unknown_severity_is_refused_rather_than_dispatched():
    with pytest.raises(ValueError):
        simulate_asana_slack_dispatch("BUG-1", "x", "S9 Cosmetic")


def test_the_dispatch_rows_report_both_destinations_and_are_arrow_safe():
    rows = dispatch_rows(simulate_asana_slack_dispatch())
    assert {row["Destination"] for row in rows} == {"Asana", "Slack"}
    for row in rows:
        assert all(isinstance(v, str) for v in row.values()), row


def test_the_dispatch_row_says_when_the_on_call_was_paged():
    paged = dispatch_rows(simulate_asana_slack_dispatch("B", "x", SEV_BLOCKER))
    quiet = dispatch_rows(simulate_asana_slack_dispatch("B", "x", SEV_MINOR))
    assert "on call paged" in paged[1]["Result"]
    assert "on call paged" not in quiet[1]["Result"]


# ---------------------------------------------------------------------------
# The Figma gap auditor
# ---------------------------------------------------------------------------

def test_the_auditor_finds_gaps_in_all_three_directions():
    kinds = {gap.kind for gap in audit_figma_gaps()}
    assert GAP_MISSING in kinds
    assert GAP_UNDESIGNED in kinds
    assert len(kinds) == 3


def test_a_screen_built_with_no_design_is_reported_at_all():
    """The one a side by side review cannot show, because there is nothing to
    put beside it."""
    undesigned = [g for g in audit_figma_gaps() if g.kind == GAP_UNDESIGNED]
    assert undesigned
    assert all(gap.detail for gap in undesigned)


def test_gaps_are_ranked_worst_first():
    ranks = [gap.rank for gap in audit_figma_gaps()]
    assert ranks == sorted(ranks)


def test_every_gap_names_a_screen_a_frame_and_a_severity():
    for gap in audit_figma_gaps():
        assert gap.screen and gap.frame and gap.detail
        assert gap.severity in SEVERITIES


def test_the_figma_rows_are_arrow_safe():
    for row in figma_rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# The board, and house rules
# ---------------------------------------------------------------------------

def test_the_summary_counts_what_the_tabs_show():
    summary = bug_bash_summary()
    assert summary["categories"] == len(map_reviews_to_test_cases())
    assert summary["test_cases"] == len(all_test_cases())
    assert summary["figma_gaps"] == len(audit_figma_gaps())
    assert summary["report_complete"] is True
    assert summary["blockers"] >= 1


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (Path(__file__).resolve().parents[1] / "tools"
              / "mobile_qa_bug_bash_console" / "core.py").read_text()
    assert "streamlit" not in source


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [report_lines()]
        + [e.example_review for e in map_reviews_to_test_cases()]
        + [c.title for c in all_test_cases()]
        + [g.detail for g in audit_figma_gaps()]
        + [simulate_asana_slack_dispatch("B", "x", s).asana["data"]["notes"]
           for s in SEVERITIES])
    assert "—" not in text
    assert "–" not in text
