"""Tests for the Data Pipeline and Governance Architecture Console.

The gate tests are about refusal rather than about admission: anyone can
write a check that passes a clean row. The governance tests are about the
cells where row level security quietly does not hold, because those are the
cells a team ships believing the opposite.
"""

from __future__ import annotations

import itertools
import pathlib
import subprocess
import sys

import pytest

from tools.data_pipeline_governance_console.core import (
    ADMITTED,
    ENFORCEMENT_BYPASSED,
    ENFORCEMENT_FILTERED,
    ENFORCEMENT_INERT,
    ENGINE_VERSION,
    OWN_TENANT_ROWS,
    REASON_DUPLICATE,
    REASON_MALFORMED,
    REASON_NULL,
    REASON_SCHEMA,
    REJECTED,
    ROLE_ANALYST,
    ROLE_APP_SERVICE,
    ROLE_SUPERUSER,
    ROLE_TABLE_OWNER,
    ROLE_TENANT_USER,
    ROLES,
    SAMPLE_ROWS,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    TARGET_ALL,
    TARGET_OTHER,
    TARGET_OWN,
    TARGETS,
    TOTAL_ROWS,
    compare_architectures,
    get_architecture,
    get_pipeline_architecture_matrix,
    run_quality_gate_batch,
    sha256_of,
    simulate_data_governance_rls,
    simulate_data_quality_gate,
)
from tools.registry import all_tools

CLEAN = sha256_of('{"order":"A-1001","total":42.5}')
OTHER = sha256_of('{"order":"A-1002","total":19.0}')

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL_DIR = ROOT / "tools" / "data_pipeline_governance_console"


# ---------------------------------------------------------------------------
# Quality gate
# ---------------------------------------------------------------------------

def test_a_clean_row_is_admitted():
    result = simulate_data_quality_gate(CLEAN, True, 0)
    assert result.admitted
    assert result.status == ADMITTED
    assert result.reasons == ()


def test_the_gate_reports_every_reason_not_only_the_first():
    """A gate that short circuits turns a backfill into a queue of single
    fixes, each costing another full run to discover the next one."""
    result = simulate_data_quality_gate("not-a-digest", False, 3)
    assert not result.admitted
    assert set(result.reasons) == {REASON_MALFORMED, REASON_SCHEMA,
                                   REASON_NULL}
    assert len(result.reasons) == 3


def test_a_duplicate_is_caught_on_content_not_on_an_identifier():
    """A producer retry after a timeout carries the same content under a
    fresh identifier, which is exactly the duplicate an identifier keyed
    gate admits."""
    first = simulate_data_quality_gate(CLEAN, True, 0, seen_hashes=())
    second = simulate_data_quality_gate(CLEAN, True, 0,
                                        seen_hashes=(CLEAN,))
    assert first.admitted
    assert not second.admitted
    assert REASON_DUPLICATE in second.reasons


def test_a_different_payload_is_not_a_duplicate():
    result = simulate_data_quality_gate(OTHER, True, 0, seen_hashes=(CLEAN,))
    assert result.admitted


def test_a_malformed_digest_is_refused_before_dedupe_is_attempted():
    """Without a digest the gate cannot tell a retry from a new row, so
    deduplication is absent rather than merely weakened."""
    result = simulate_data_quality_gate("", True, 0, seen_hashes=(CLEAN,))
    assert REASON_MALFORMED in result.reasons
    assert REASON_DUPLICATE not in result.reasons


def test_the_digest_comparison_is_case_and_whitespace_insensitive():
    result = simulate_data_quality_gate(f"  {CLEAN.upper()}  ", True, 0,
                                        seen_hashes=(CLEAN,))
    assert REASON_DUPLICATE in result.reasons


def test_the_null_tolerance_boundary_is_inclusive():
    assert simulate_data_quality_gate(CLEAN, True, 2,
                                      null_tolerance=2).admitted
    assert not simulate_data_quality_gate(CLEAN, True, 3,
                                          null_tolerance=2).admitted


def test_nothing_is_ever_admitted_with_a_reason_outstanding():
    """The property the whole gate rests on, checked over every combination
    rather than on a chosen example."""
    digests = (CLEAN, "", "zz" * 32)
    for digest, schema_valid, nulls, seen in itertools.product(
            digests, (True, False), (0, 1, 4), ((), (CLEAN,))):
        result = simulate_data_quality_gate(digest, schema_valid, nulls,
                                            seen_hashes=seen)
        assert result.admitted == (not result.reasons)
        assert result.status == (ADMITTED if result.admitted else REJECTED)
        if not result.admitted:
            assert any(f.severity == SEVERITY_CRITICAL
                       for f in result.findings)


def test_a_negative_null_count_raises():
    with pytest.raises(ValueError):
        simulate_data_quality_gate(CLEAN, True, -1)


def test_a_negative_tolerance_raises():
    with pytest.raises(ValueError):
        simulate_data_quality_gate(CLEAN, True, 0, null_tolerance=-1)


def test_an_admitted_row_still_raises_the_ledger_durability_problem():
    """An in memory dedupe set is emptied by a restart, and every in flight
    retry is then admitted as new."""
    codes = {f.code for f in simulate_data_quality_gate(CLEAN, True,
                                                        0).findings}
    assert "DQ-LEDGER" in codes


def test_the_batch_rejects_an_in_batch_retry_using_the_first_copy():
    rows = [
        {"payload_hash": CLEAN, "schema_valid": True, "null_count": 0},
        {"payload_hash": CLEAN, "schema_valid": True, "null_count": 0},
    ]
    batch = run_quality_gate_batch(rows)
    assert batch.results[0].admitted
    assert not batch.results[1].admitted
    assert REASON_DUPLICATE in batch.results[1].reasons


def test_the_batch_totals_equal_the_rows_in():
    batch = run_quality_gate_batch(SAMPLE_ROWS)
    assert batch.total == len(SAMPLE_ROWS)
    assert batch.admitted + batch.rejected == batch.total


def test_a_rejected_row_never_enters_the_digest_ledger():
    """A rejected row lands nowhere, including in the record of what has
    landed, or the retry that fixes it would be refused as a duplicate."""
    rows = [{"payload_hash": CLEAN, "schema_valid": False, "null_count": 0}]
    batch = run_quality_gate_batch(rows)
    assert not batch.results[0].admitted
    assert CLEAN not in batch.seen_after
    fixed = simulate_data_quality_gate(CLEAN, True, 0,
                                       seen_hashes=batch.seen_after)
    assert fixed.admitted


def test_the_sample_batch_demonstrates_each_rejection_reason():
    batch = run_quality_gate_batch(SAMPLE_ROWS)
    seen = {reason for r in batch.results for reason in r.reasons}
    assert seen == {REASON_DUPLICATE, REASON_SCHEMA, REASON_NULL,
                    REASON_MALFORMED}


# ---------------------------------------------------------------------------
# Architecture matrix
# ---------------------------------------------------------------------------

def test_the_matrix_covers_three_shapes_with_no_blank_cell():
    matrix = get_pipeline_architecture_matrix()
    assert len(matrix) == 3
    for item in matrix:
        for value in (item.name, item.latency, item.complexity, item.cost,
                      item.cost_shape, item.fails_as, item.choose_when,
                      item.avoid_when):
            assert value.strip()


def test_latency_order_is_the_exact_reverse_of_complexity_order():
    """There is no cell in this matrix where a team gets lower latency
    without taking on more to operate."""
    facts = compare_architectures()
    assert facts["latency_order_reverses_complexity_order"] is True
    assert facts["by_latency"] == ("streaming", "micro_batch", "batch")
    assert facts["by_complexity"] == ("batch", "micro_batch", "streaming")


def test_the_lowest_latency_shape_has_the_quietest_failure_mode():
    """The axis that decides whether a team can operate what it built."""
    streaming = get_architecture("streaming")
    batch = get_architecture("batch")
    assert "quietly" in streaming.fails_as.lower()
    assert "loudly" in batch.fails_as.lower()
    assert streaming.latency_high_seconds < batch.latency_low_seconds


def test_an_unknown_architecture_key_raises():
    with pytest.raises(KeyError):
        get_architecture("lambda_architecture")


# ---------------------------------------------------------------------------
# Row level security governance
# ---------------------------------------------------------------------------

def test_an_enforced_policy_returns_no_rows_from_another_tenant():
    verdict = simulate_data_governance_rls(ROLE_TENANT_USER, TARGET_OTHER)
    assert verdict.enforcement == ENFORCEMENT_FILTERED
    assert verdict.rows_visible == 0
    assert verdict.blocked


def test_an_enforced_policy_narrows_a_select_all_to_the_own_tenant_rows():
    verdict = simulate_data_governance_rls(ROLE_ANALYST, TARGET_ALL)
    assert verdict.rows_visible == OWN_TENANT_ROWS
    assert verdict.rows_withheld == TOTAL_ROWS - OWN_TENANT_ROWS


def test_a_denial_is_indistinguishable_from_an_empty_table():
    """The engine returns zero rows and raises nothing, so a wrong tenant
    claim presents as an empty dashboard rather than as an error."""
    verdict = simulate_data_governance_rls(ROLE_TENANT_USER, TARGET_OTHER)
    assert verdict.silent
    assert "RLS-SILENT" in {f.code for f in verdict.findings}


def test_the_table_owner_reads_every_tenant_until_force_is_set():
    """The exemption that bites in practice: the migration role owns the
    table, the application connects as the migration role."""
    unforced = simulate_data_governance_rls(ROLE_TABLE_OWNER, TARGET_OTHER,
                                            rls_enabled=True, forced=False)
    forced = simulate_data_governance_rls(ROLE_TABLE_OWNER, TARGET_OTHER,
                                          rls_enabled=True, forced=True)
    assert unforced.enforcement == ENFORCEMENT_BYPASSED
    assert unforced.rows_visible == TOTAL_ROWS - OWN_TENANT_ROWS
    assert forced.enforcement == ENFORCEMENT_FILTERED
    assert forced.rows_visible == 0


def test_force_does_not_reach_a_superuser_or_a_bypassrls_role():
    """FORCE ROW LEVEL SECURITY binds the table owner. It does not bind a
    superuser, and it does not bind a role holding BYPASSRLS."""
    for role in (ROLE_SUPERUSER, ROLE_APP_SERVICE):
        verdict = simulate_data_governance_rls(role, TARGET_ALL,
                                               rls_enabled=True, forced=True)
        assert verdict.enforcement == ENFORCEMENT_BYPASSED, role
        assert verdict.rows_visible == TOTAL_ROWS, role


def test_a_policy_without_enable_protects_nothing_from_anyone():
    """CREATE POLICY on its own changes nothing, and the policy sits in the
    catalogue looking like protection."""
    for role in ROLES:
        verdict = simulate_data_governance_rls(role, TARGET_ALL,
                                               rls_enabled=False)
        assert verdict.enforcement == ENFORCEMENT_INERT, role
        assert verdict.rows_visible == TOTAL_ROWS, role
        assert "RLS-INERT" in {f.code for f in verdict.findings}, role


def test_exactly_the_exempt_roles_cross_the_tenant_boundary():
    """The whole grid, so no leaking cell is hidden behind a chosen
    example."""
    leaked = {role for role in ROLES
              if simulate_data_governance_rls(
                  role, TARGET_OTHER, rls_enabled=True,
                  forced=False).rows_visible > 0}
    assert leaked == {ROLE_TABLE_OWNER, ROLE_APP_SERVICE, ROLE_SUPERUSER}


def test_forcing_narrows_the_leak_to_the_two_engine_level_exemptions():
    leaked = {role for role in ROLES
              if simulate_data_governance_rls(
                  role, TARGET_OTHER, rls_enabled=True,
                  forced=True).rows_visible > 0}
    assert leaked == {ROLE_APP_SERVICE, ROLE_SUPERUSER}


def test_no_cell_ever_returns_more_rows_than_the_query_asked_for():
    for role, target, enabled, forced in itertools.product(
            ROLES, TARGETS, (True, False), (True, False)):
        verdict = simulate_data_governance_rls(role, target, enabled, forced)
        assert 0 <= verdict.rows_visible <= verdict.rows_requested
        assert verdict.blocked == (verdict.rows_visible
                                   < verdict.rows_requested)


def test_every_verdict_raises_the_trusted_claim_problem():
    """A USING clause comparing against a session variable trusts whatever
    last set it."""
    verdict = simulate_data_governance_rls(ROLE_TENANT_USER, TARGET_OWN)
    assert "RLS-CLAIM" in {f.code for f in verdict.findings}


def test_the_emitted_sql_reflects_the_switches_rather_than_asserting_them():
    off = simulate_data_governance_rls(ROLE_TENANT_USER, TARGET_OWN,
                                       rls_enabled=False, forced=False)
    assert any(line.startswith("-- ALTER TABLE tenant_data ENABLE")
               for line in off.sql)
    on = simulate_data_governance_rls(ROLE_TENANT_USER, TARGET_OWN,
                                      rls_enabled=True, forced=True)
    assert "ALTER TABLE tenant_data ENABLE ROW LEVEL SECURITY;" in on.sql
    assert "ALTER TABLE tenant_data FORCE ROW LEVEL SECURITY;" in on.sql


def test_an_unknown_role_or_target_raises():
    with pytest.raises(ValueError):
        simulate_data_governance_rls("root", TARGET_OWN)
    with pytest.raises(ValueError):
        simulate_data_governance_rls(ROLE_TENANT_USER, "all_the_things")


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
        "import tools.data_pipeline_governance_console.core as c; "
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
    assert "data-pipeline-governance-console" in keys
    icons = [t.icon for t in tools]
    assert len(icons) == len(set(icons))
    assert SEVERITY_OK != SEVERITY_CRITICAL
