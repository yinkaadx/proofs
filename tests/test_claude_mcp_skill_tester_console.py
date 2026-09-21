"""Tests for the Claude MCP Skill and Agent Testing Console.

Three properties carry this file, and each is a silent failure rather than a
loud one, which is the only reason the tool exists.

A manifest that parses is not a manifest that loads. Type errors are reported
rather than coerced, an unknown key is reported rather than ignored, and a
description too thin to match is a skill that never loads and never errors.

A policy denial and an empty table produce the same response. The simulator
must return zero rows without raising in the denial case, and must say so, or
it is reproducing the bug rather than exposing it.

A partial matrix must never report as a clean run. Coverage is asserted
separately from the pass count across every subset of categories.

Pass marker: pytest reports all tests passed, exit 0.
Fail marker: any failure or error line, exit non zero.
"""

from __future__ import annotations

import json
import subprocess
import sys
from itertools import combinations as subsets
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.claude_mcp_skill_tester_console.core import (  # noqa: E402
    CATEGORY_HAPPY,
    CATEGORY_MALFORMED,
    CATEGORY_PERMISSION,
    CATEGORY_TIMEOUT,
    ENGINE_VERSION,
    FIXTURE_ROWS,
    KNOWN_KEYS,
    KNOWN_TENANTS,
    MIN_DESCRIPTION_CHARS,
    OPTIONAL_KEYS,
    REQUIRED_CATEGORIES,
    REQUIRED_KEYS,
    RESULT_BYPASSED,
    RESULT_EMPTY_BY_POLICY,
    RESULT_ERROR,
    RESULT_FAIL,
    RESULT_PASS,
    RESULT_ROWS,
    ROLES,
    ROLE_ANON,
    ROLE_AUTHENTICATED,
    ROLE_SERVICE,
    SAMPLE_MANIFESTS,
    SEVERITY_CRITICAL,
    VALID_FAILED,
    VALID_PASSED,
    VALID_UNPARSEABLE,
    execute_boundary_test_matrix,
    simulate_mcp_connection,
    validate_skill_manifest,
)

GOOD = {
    "name": "gtm-container-auditor",
    "description": ("Audit a Google Tag Manager container and report tags "
                    "with no trigger and triggers with no tag. Use when asked "
                    "to review or clean a container."),
}


# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------


def test_a_complete_manifest_validates_and_would_load():
    report = validate_skill_manifest(GOOD)
    assert report.status == VALID_PASSED
    assert report.would_load
    assert report.is_valid
    assert report.missing_keys == ()
    assert report.type_errors == ()
    assert report.unknown_keys == ()


def test_each_required_key_removed_in_turn_stops_the_skill_loading():
    for key in REQUIRED_KEYS:
        payload = {k: v for k, v in GOOD.items() if k != key}
        report = validate_skill_manifest(payload)
        assert key in report.missing_keys, key
        assert not report.would_load, key
        assert report.severity == SEVERITY_CRITICAL, key


def test_a_wrong_type_is_reported_rather_than_coerced():
    """Coercing here hides a bug that resurfaces at load time with no
    useful message."""
    for key, bad in (("name", 123), ("description", 456),
                     ("license", ["MIT"]), ("allowed-tools", 7)):
        payload = dict(GOOD)
        payload[key] = bad
        report = validate_skill_manifest(payload)
        assert report.type_errors, (key, bad)
        assert any(key in message for message in report.type_errors), key
        assert not report.would_load, key


def test_allowed_tools_accepts_both_documented_shapes():
    for value in ("Bash, Read", ["Bash", "Read"]):
        report = validate_skill_manifest({**GOOD, "allowed-tools": value})
        assert report.type_errors == (), value
        assert report.would_load, value


def test_an_unknown_key_is_reported_rather_than_ignored():
    """A typo in a key name is a setting that is silently absent."""
    report = validate_skill_manifest({**GOOD, "descriptoin": "oops"})
    assert "descriptoin" in report.unknown_keys
    assert any(f.code == "SKL-UNKNOWN" for f in report.findings)


def test_a_description_too_thin_to_match_is_critical():
    """The loader matches on the description, so a thin one never loads and
    never errors either."""
    report = validate_skill_manifest({**GOOD, "description": "Builds reports."})
    assert report.description_chars < MIN_DESCRIPTION_CHARS
    assert not report.would_load
    assert report.severity == SEVERITY_CRITICAL
    assert any(f.code == "SKL-THIN" for f in report.findings)


def test_the_description_floor_is_where_it_says_it_is():
    under = validate_skill_manifest(
        {**GOOD, "description": "x" * (MIN_DESCRIPTION_CHARS - 1)})
    at = validate_skill_manifest(
        {**GOOD, "description": "x" * MIN_DESCRIPTION_CHARS})
    assert not under.would_load
    assert at.would_load


def test_a_name_that_is_not_a_slug_is_refused():
    for bad in ("My Skill Name", "Skill_Name", "UPPER", "trailing-",
                "double--hyphen", "has space"):
        report = validate_skill_manifest({**GOOD, "name": bad})
        assert not report.would_load, bad
        assert any(f.code == "SKL-NAME" for f in report.findings), bad
    for good in ("a", "gtm-container-auditor", "skill2", "a-b-c"):
        assert validate_skill_manifest({**GOOD, "name": good}).would_load, good


def test_malformed_json_is_reported_as_a_parse_failure_not_a_missing_key():
    """A validator that reports a parse failure as a missing key sends the
    reader to the wrong place."""
    for bad in ("{not json", "", "   ", "[1, 2, 3]", '"just a string"'):
        report = validate_skill_manifest(bad)
        assert report.status == VALID_UNPARSEABLE, bad
        assert not report.parsed, bad
        assert not report.would_load, bad
        assert report.checks == (), bad


def test_a_json_string_and_an_equivalent_dict_agree():
    as_dict = validate_skill_manifest(GOOD)
    as_text = validate_skill_manifest(json.dumps(GOOD))
    assert as_dict.would_load == as_text.would_load
    assert as_dict.status == as_text.status
    assert as_dict.description_chars == as_text.description_chars


def test_every_sample_manifest_demonstrates_a_distinct_failure():
    """Each sample must fail differently, or the set is not teaching five
    things. The signature includes the name and description failures, because
    the first version of this test used only the key level signals and two
    samples collided."""
    outcomes = {}
    for label, payload in SAMPLE_MANIFESTS.items():
        report = validate_skill_manifest(payload)
        codes = frozenset(f.code for f in report.findings
                          if f.severity == SEVERITY_CRITICAL)
        outcomes[label] = (bool(report.missing_keys), bool(report.type_errors),
                           bool(report.unknown_keys), report.would_load, codes)
    assert len(set(outcomes.values())) == len(outcomes), outcomes
    assert sum(1 for v in outcomes.values() if v[3]) == 1
    # And the five distinct failure modes are the ones the labels promise.
    all_codes = set().union(*(v[4] for v in outcomes.values()))
    for expected in ("SKL-MISSING", "SKL-TYPE", "SKL-THIN", "SKL-NAME"):
        assert expected in all_codes, expected


def test_the_known_key_set_is_the_required_set_plus_the_optional_one():
    assert set(KNOWN_KEYS) == set(REQUIRED_KEYS) | set(OPTIONAL_KEYS)
    assert not set(REQUIRED_KEYS) & set(OPTIONAL_KEYS)
    assert "name" in REQUIRED_KEYS and "description" in REQUIRED_KEYS


# ---------------------------------------------------------------------------
# The MCP connection
# ---------------------------------------------------------------------------


def test_a_policy_denial_returns_zero_rows_and_does_not_raise():
    """The whole point. Postgres filters rather than refusing, so a denial
    and an empty table are the same response."""
    response = simulate_mcp_connection(
        {"role": ROLE_AUTHENTICATED, "tenant_id": ""}, enforce_rls=True)
    assert response.outcome == RESULT_EMPTY_BY_POLICY
    assert response.row_count == 0
    assert not response.raised_error
    assert response.silent_denial
    assert response.severity == SEVERITY_CRITICAL
    assert any(f.code == "MCP-SILENT" for f in response.findings)


def test_the_denial_and_the_empty_table_are_indistinguishable_in_the_payload():
    denial = simulate_mcp_connection(
        {"role": ROLE_AUTHENTICATED, "tenant_id": ""}, enforce_rls=True)
    empty = simulate_mcp_connection(
        {"role": ROLE_AUTHENTICATED, "tenant_id": "nobody"}, enforce_rls=True)
    assert json.loads(denial.response_json)["data"] == []
    assert json.loads(empty.response_json)["data"] == []
    assert denial.row_count == empty.row_count == 0
    assert not denial.raised_error and not empty.raised_error


def test_a_scoped_read_returns_only_that_tenants_rows():
    for tenant in KNOWN_TENANTS:
        response = simulate_mcp_connection(
            {"role": ROLE_AUTHENTICATED, "tenant_id": tenant},
            enforce_rls=True)
        assert response.outcome == RESULT_ROWS, tenant
        assert response.row_count > 0, tenant
        assert response.cross_tenant_rows == 0, tenant
        assert all(r["tenant_id"] == tenant for r in response.rows), tenant


def test_the_service_role_key_bypasses_every_policy():
    """An MCP server with this key has no tenancy at all, however careful
    the policies are."""
    response = simulate_mcp_connection(
        {"role": ROLE_SERVICE, "tenant_id": "acme"}, enforce_rls=True)
    assert response.outcome == RESULT_BYPASSED
    assert response.row_count == len(FIXTURE_ROWS)
    assert response.cross_tenant_rows > 0
    assert response.severity == SEVERITY_CRITICAL
    assert any(f.code == "MCP-BYPASS" for f in response.findings)


def test_switching_security_off_is_the_same_exposure_as_the_service_key():
    by_key = simulate_mcp_connection(
        {"role": ROLE_SERVICE, "tenant_id": "acme"}, enforce_rls=True)
    by_flag = simulate_mcp_connection(
        {"role": ROLE_AUTHENTICATED, "tenant_id": "acme"}, enforce_rls=False)
    assert by_key.outcome == by_flag.outcome == RESULT_BYPASSED
    assert by_key.row_count == by_flag.row_count == len(FIXTURE_ROWS)
    assert by_flag.cross_tenant_rows > 0


def test_no_configuration_ever_leaks_another_tenant_while_enforcing():
    for role, tenant in ((ROLE_ANON, ""), (ROLE_ANON, "acme"),
                         (ROLE_AUTHENTICATED, "acme"),
                         (ROLE_AUTHENTICATED, "globex"),
                         (ROLE_AUTHENTICATED, "nobody")):
        response = simulate_mcp_connection(
            {"role": role, "tenant_id": tenant}, enforce_rls=True)
        assert response.cross_tenant_rows == 0, (role, tenant)


def test_a_missing_table_raises_but_a_denial_does_not():
    """The missing table is the easy failure. The hard one is the query that
    succeeds and returns nothing."""
    missing = simulate_mcp_connection(
        {"role": ROLE_AUTHENTICATED, "tenant_id": "acme",
         "table": "nonexistent"}, enforce_rls=True)
    assert missing.raised_error
    assert missing.outcome == RESULT_ERROR
    assert "42P01" in missing.response_json
    denial = simulate_mcp_connection(
        {"role": ROLE_AUTHENTICATED, "tenant_id": ""}, enforce_rls=True)
    assert not denial.raised_error


def test_an_unknown_role_is_refused_rather_than_downgraded():
    for bad in ("", "admin", "postgres", "root"):
        response = simulate_mcp_connection(
            {"role": bad, "tenant_id": "acme"}, enforce_rls=True)
        assert response.raised_error, bad
        assert response.row_count == 0, bad
        assert response.outcome == RESULT_ERROR, bad


def test_every_response_carries_the_silence_warning():
    for role, enforce in ((ROLE_AUTHENTICATED, True), (ROLE_SERVICE, True),
                          (ROLE_AUTHENTICATED, False)):
        response = simulate_mcp_connection(
            {"role": role, "tenant_id": "acme"}, enforce)
        assert any(f.code == "MCP-SILENCE-RULE" for f in response.findings), role


def test_a_scoped_response_reports_its_tenant_context():
    """The one field that makes an empty result readable."""
    response = simulate_mcp_connection(
        {"role": ROLE_AUTHENTICATED, "tenant_id": "acme"}, enforce_rls=True)
    assert json.loads(response.response_json)["tenant_context"] == "acme"


# ---------------------------------------------------------------------------
# The boundary matrix
# ---------------------------------------------------------------------------


def test_the_full_matrix_covers_every_category():
    matrix = execute_boundary_test_matrix("gtm-container-auditor")
    assert matrix.coverage_sufficient
    assert set(matrix.categories_covered) == set(REQUIRED_CATEGORIES)
    assert matrix.categories_missing == ()
    assert matrix.reconciles


def test_any_missing_category_makes_the_run_insufficient():
    """Whatever was not exercised is unmeasured rather than passing."""
    for size in range(1, len(REQUIRED_CATEGORIES)):
        for subset in subsets(REQUIRED_CATEGORIES, size):
            matrix = execute_boundary_test_matrix("x", subset)
            assert not matrix.coverage_sufficient, subset
            assert matrix.categories_missing, subset
            assert any(f.code == "MTX-COVERAGE" for f in matrix.findings), subset


def test_a_partial_run_can_be_all_green_and_still_insufficient():
    """The exact failure the coverage line exists to prevent."""
    matrix = execute_boundary_test_matrix("x", (CATEGORY_HAPPY,))
    assert matrix.failed == 0
    assert matrix.passed == len(matrix.cases)
    assert not matrix.coverage_sufficient
    assert matrix.severity == SEVERITY_CRITICAL


def test_the_matrix_contains_deliberate_failures():
    """A matrix that has never failed is usually one that cannot fail."""
    matrix = execute_boundary_test_matrix("x")
    assert matrix.failed > 0
    assert any(c.result == RESULT_FAIL for c in matrix.cases)
    assert any(f.code == "MTX-FAIL" for f in matrix.findings)


def test_an_all_green_run_is_flagged_as_suspicious():
    matrix = execute_boundary_test_matrix("x", (CATEGORY_HAPPY,))
    assert matrix.failed == 0
    assert any(f.code == "MTX-GREEN" for f in matrix.findings)


def test_the_counts_always_reconcile_for_any_subset():
    for size in range(1, len(REQUIRED_CATEGORIES) + 1):
        for subset in subsets(REQUIRED_CATEGORIES, size):
            matrix = execute_boundary_test_matrix("x", subset)
            assert matrix.reconciles, subset
            assert matrix.passed + matrix.failed == len(matrix.cases), subset
            assert len(matrix.log) == len(matrix.cases), subset


def test_every_case_belongs_to_a_requested_category():
    for subset in ((CATEGORY_HAPPY,), (CATEGORY_MALFORMED, CATEGORY_TIMEOUT),
                   REQUIRED_CATEGORIES):
        matrix = execute_boundary_test_matrix("x", subset)
        for case in matrix.cases:
            assert case.category in subset, (subset, case.category)


def test_every_case_states_what_it_gave_expected_and_observed():
    for case in execute_boundary_test_matrix("x").cases:
        assert len(case.given) > 20, case.name
        assert len(case.expected) > 20, case.name
        assert case.observed.strip(), case.name
        assert case.result in (RESULT_PASS, RESULT_FAIL), case.name


def test_the_skill_name_reaches_every_case():
    matrix = execute_boundary_test_matrix("my-particular-skill")
    assert all("my-particular-skill" in c.name for c in matrix.cases)


def test_an_unnamed_skill_or_empty_category_list_raises():
    with pytest.raises(ValueError):
        execute_boundary_test_matrix("")
    with pytest.raises(ValueError):
        execute_boundary_test_matrix("x", ())
    with pytest.raises(ValueError):
        execute_boundary_test_matrix("x", ("Vibes",))


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "claude_mcp_skill_tester_console"
              / "core.py").read_text()
    assert "import streamlit" not in source
    probe = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.claude_mcp_skill_tester_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT)
    )
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [report.headline + " ".join(f.title + f.detail + f.fix
                                    for f in report.findings)
         for payload in list(SAMPLE_MANIFESTS.values()) + ["{bad", GOOD]
         for report in (validate_skill_manifest(payload),)]
        + [response.headline + " ".join(f.title + f.detail + f.fix
                                        for f in response.findings)
           for role, tenant, enforce in (
               (ROLE_AUTHENTICATED, "acme", True),
               (ROLE_AUTHENTICATED, "", True),
               (ROLE_SERVICE, "acme", True),
               ("bogus", "acme", True))
           for response in (simulate_mcp_connection(
               {"role": role, "tenant_id": tenant}, enforce),)]
        + [matrix.headline + " ".join(f.title + f.detail + f.fix
                                      for f in matrix.findings)
           + " ".join(c.given + c.expected + c.observed for c in matrix.cases)
           for subset in (REQUIRED_CATEGORIES, (CATEGORY_HAPPY,))
           for matrix in (execute_boundary_test_matrix("x", subset),)]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools
    tools = all_tools()
    entry = next(t for t in tools
                 if t.key == "claude-mcp-skill-tester-console")
    assert entry.title == "Claude MCP Skill & Agent Testing Console"
    icons = [t.icon for t in tools]
    assert icons.count(entry.icon) == 1, "this icon is already used by another tool"
    assert len(entry.tagline) > 30
