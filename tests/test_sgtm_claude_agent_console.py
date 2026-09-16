"""Tests for the Server Side Tracking and Claude Agent Console.

Two things are worth proving. Meta's deduplication rule is the event name and
the event id both matching, so every way of breaking that pair must produce a
double count and no way of breaking it may quietly pass. And the consent
evaluation must treat ad_user_data as the gate, so no signal combination lets
an identifier leave while that one is denied.

Pass marker: pytest reports all tests passed, exit 0.
Fail marker: any failure or error line, exit non zero.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.sgtm_claude_agent_console.core import (  # noqa: E402
    DISPATCH_FULL,
    DISPATCH_MODELLED_ONLY,
    DISPATCH_RESTRICTED,
    ENGINE_VERSION,
    EXPRESS_CONSENT_REGIONS,
    GTM_READONLY_SCOPE,
    MATCH_BOTH_IDS_MISSING,
    MATCH_BROWSER_ID_MISSING,
    MATCH_DEDUPLICATED,
    MATCH_ID_MISMATCH,
    MATCH_SERVER_ID_MISSING,
    REGION_CANADA,
    REGION_EEA,
    REGION_QUEBEC,
    REGION_REST,
    REGIONS,
    SAMPLE_EVENT_PAIRS,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_OK,
    STANDARD_EVENTS,
    consent_matrix,
    dedup_board,
    evaluate_consent_mode_v2,
    generate_claude_skill_manifest,
    validate_capi_dedup,
)

# ---------------------------------------------------------------------------
# CAPI deduplication
# ---------------------------------------------------------------------------


def test_a_matching_pair_on_a_standard_event_deduplicates():
    result = validate_capi_dedup("evt-1", "evt-1", "Purchase")
    assert result.match_status == MATCH_DEDUPLICATED
    assert result.deduplicated
    assert result.counted_events == 1
    assert result.severity == SEVERITY_OK
    assert result.emq_impact_points == 0.0
    assert result.inflation_pct == 0.0


def test_every_way_of_breaking_the_pair_double_counts():
    """One table, four failure shapes, no hand picked example doing the work."""
    cases = (
        ("evt-1", "evt-2", MATCH_ID_MISMATCH),
        ("", "evt-2", MATCH_BROWSER_ID_MISSING),
        ("evt-1", "", MATCH_SERVER_ID_MISSING),
        ("", "", MATCH_BOTH_IDS_MISSING),
    )
    for browser_id, server_id, expected in cases:
        result = validate_capi_dedup(browser_id, server_id, "Purchase")
        assert result.match_status == expected, (browser_id, server_id)
        assert not result.deduplicated
        assert result.counted_events == 2
        assert result.severity == SEVERITY_CRITICAL
        assert result.inflation_pct == 100.0
        assert result.emq_impact_points < 0


def test_deduplication_needs_the_ids_to_be_identical_not_merely_similar():
    assert validate_capi_dedup("evt-1", " evt-1 ", "Lead").deduplicated, (
        "surrounding whitespace is stripped before the comparison")
    assert not validate_capi_dedup("evt-1", "EVT-1", "Lead").deduplicated, (
        "the ids are compared exactly, and Meta does not fold case")
    assert not validate_capi_dedup("evt-1", "evt-10", "Lead").deduplicated, (
        "a prefix is not a match")


def test_a_custom_event_name_deduplicates_but_is_flagged():
    result = validate_capi_dedup("evt-1", "evt-1", "PurchaseComplete")
    assert result.deduplicated
    assert result.counted_events == 1
    assert not result.is_standard_event
    assert result.severity == SEVERITY_HIGH
    assert result.emq_impact_points < 0, (
        "a name that is custom by accident costs standard optimisation")


def test_every_standard_event_name_is_recognised():
    for name in STANDARD_EVENTS:
        assert validate_capi_dedup("evt-1", "evt-1", name).is_standard_event, name
    assert len(set(STANDARD_EVENTS)) == len(STANDARD_EVENTS)
    assert "Purchase" in STANDARD_EVENTS
    assert "purchase" not in STANDARD_EVENTS, "the names are case sensitive"


def test_an_event_with_no_name_raises_rather_than_passing():
    with pytest.raises(ValueError):
        validate_capi_dedup("evt-1", "evt-1", "")
    with pytest.raises(ValueError):
        validate_capi_dedup("evt-1", "evt-1", "   ")


def test_every_failure_carries_a_fix_naming_what_to_change():
    for browser_id, server_id in (("evt-1", "evt-2"), ("", "evt-2"),
                                  ("evt-1", ""), ("", "")):
        result = validate_capi_dedup(browser_id, server_id, "Purchase")
        assert len(result.fix) > 40
        assert len(result.headline) > 30


def test_the_board_totals_equal_the_rows():
    board = dedup_board(SAMPLE_EVENT_PAIRS)
    assert board["deduplicated"] + board["double_counted"] == board["total"]
    assert board["total"] == len(SAMPLE_EVENT_PAIRS)
    counted = sum(r.counted_events for r in board["results"])
    assert counted == board["events_meta_counts"]
    assert board["events_meta_counts"] >= board["actions_taken"], (
        "deduplication can never report fewer events than there were actions")


def test_the_board_inflation_is_derived_from_the_same_counts_it_prints():
    board = dedup_board(SAMPLE_EVENT_PAIRS)
    expected = round(
        (board["events_meta_counts"] - board["actions_taken"])
        / board["actions_taken"] * 100, 1)
    assert board["inflation_pct"] == expected


# ---------------------------------------------------------------------------
# Consent Mode v2
# ---------------------------------------------------------------------------


def test_both_signals_granted_is_full_dispatch():
    decision = evaluate_consent_mode_v2(True, True, REGION_CANADA)
    assert decision.dispatch_status == DISPATCH_FULL
    assert decision.identifiers_leave_the_browser
    assert decision.tags_blocked == ()


def test_user_data_without_personalization_measures_but_does_not_profile():
    decision = evaluate_consent_mode_v2(True, False, REGION_QUEBEC)
    assert decision.dispatch_status == DISPATCH_RESTRICTED
    assert decision.identifiers_leave_the_browser
    assert any("Remarketing" in tag for tag in decision.tags_blocked)
    assert not any("Remarketing" in tag for tag in decision.tags_allowed)


def test_ad_user_data_is_the_gate_in_every_region():
    """The one invariant. Deny ad_user_data and nothing identifying leaves,
    whatever personalization says and whatever region it is."""
    for region in REGIONS:
        for personalization in (True, False):
            decision = evaluate_consent_mode_v2(False, personalization, region)
            assert not decision.identifiers_leave_the_browser, region
            assert decision.dispatch_status == DISPATCH_MODELLED_ONLY
            assert not any("customer parameters" in tag
                           for tag in decision.tags_allowed)


def test_personalization_without_user_data_is_reported_as_incoherent():
    decision = evaluate_consent_mode_v2(False, True, REGION_EEA)
    assert decision.findings, "the contradictory pair passed without comment"
    assert any("denied" in note for note in decision.findings)
    assert decision.dispatch_status == DISPATCH_MODELLED_ONLY


def test_the_express_consent_regions_are_quebec_and_the_eea():
    assert REGION_QUEBEC in EXPRESS_CONSENT_REGIONS
    assert REGION_EEA in EXPRESS_CONSENT_REGIONS
    assert REGION_CANADA not in EXPRESS_CONSENT_REGIONS
    assert REGION_REST not in EXPRESS_CONSENT_REGIONS
    assert evaluate_consent_mode_v2(True, True, REGION_QUEBEC).express_consent_required
    assert not evaluate_consent_mode_v2(True, True, REGION_CANADA).express_consent_required


def test_a_denied_default_is_the_safe_state_where_a_choice_is_required():
    quiet = evaluate_consent_mode_v2(False, False, REGION_QUEBEC)
    assert quiet.compliant_default
    assert any("correct default" in note for note in quiet.findings)
    granted = evaluate_consent_mode_v2(True, True, REGION_QUEBEC)
    assert any("affirmative choice" in note for note in granted.findings), (
        "a grant in Quebec must at least ask whether it came from a click")


def test_every_region_states_its_own_basis_and_none_is_empty():
    for region in REGIONS:
        decision = evaluate_consent_mode_v2(True, True, region)
        assert len(decision.legal_basis) > 60, region
        assert region in decision.headline


def test_an_unknown_region_raises_rather_than_defaulting_to_the_loosest():
    with pytest.raises(ValueError):
        evaluate_consent_mode_v2(True, True, "Atlantis")
    assert evaluate_consent_mode_v2(True, True, "").region == REGION_REST


def test_the_matrix_covers_all_four_states_once_each():
    for region in REGIONS:
        rows = consent_matrix(region)
        assert len(rows) == 4
        pairs = {(r.ad_user_data_granted, r.ad_personalization_granted)
                 for r in rows}
        assert pairs == {(True, True), (True, False), (False, True), (False, False)}
        leaving = sum(1 for r in rows if r.identifiers_leave_the_browser)
        assert leaving == 2, (
            "exactly the two states that grant ad_user_data may send an id")


# ---------------------------------------------------------------------------
# Claude skill manifest
# ---------------------------------------------------------------------------


def test_the_manifest_parses_into_the_fields_claude_code_reads():
    manifest = generate_claude_skill_manifest()
    assert manifest.name == "gtm-container-auditor"
    assert manifest.manifest.startswith("---\n")
    assert f"name: {manifest.name}" in manifest.frontmatter
    assert "description:" in manifest.frontmatter
    assert len(manifest.description) > 80
    assert manifest.allowed_tools == ("Bash", "Read", "Grep")


def test_the_manifest_installs_where_claude_code_looks_for_a_skill():
    manifest = generate_claude_skill_manifest()
    assert manifest.install_path == ".claude/skills/gtm-container-auditor/SKILL.md"


def test_the_audit_skill_is_read_only_by_construction():
    manifest = generate_claude_skill_manifest()
    assert manifest.is_read_only
    assert "never publishes a version" in manifest.description
    assert "Never edit, never publish, never delete" in manifest.body
    assert "Every call below is a GET" in manifest.body


def test_the_manifest_names_the_read_only_scope_and_the_api_base():
    manifest = generate_claude_skill_manifest()
    assert manifest.scope == GTM_READONLY_SCOPE
    assert manifest.scope.endswith("tagmanager.readonly")
    assert manifest.api_base == "https://tagmanager.googleapis.com/tagmanager/v2"
    assert manifest.api_base in manifest.body


def test_the_manifest_tells_the_agent_what_to_look_for():
    body = generate_claude_skill_manifest().body
    for wanted in ("firingTriggerId", "consentSettings", "event_id",
                   "versions:live"):
        assert wanted in body, wanted


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "sgtm_claude_agent_console" / "core.py").read_text()
    assert "import streamlit" not in source
    probe = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.sgtm_claude_agent_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT)
    )
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [r.headline + r.fix for r in dedup_board(SAMPLE_EVENT_PAIRS)["results"]]
        + [d.headline + d.fix + d.legal_basis + " ".join(d.findings)
           for region in REGIONS for d in consent_matrix(region)]
        + [generate_claude_skill_manifest().manifest]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools
    tools = all_tools()
    entry = next(t for t in tools if t.key == "sgtm-claude-agent-console")
    assert entry.title == "Server Side Tracking & Claude Agent Console"
    icons = [t.icon for t in tools]
    assert icons.count(entry.icon) == 1, "this icon is already used by another tool"
    assert len(entry.tagline) > 30
