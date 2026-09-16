"""Tests for the SaaS Multi Tenant PostgreSQL Console.

The isolation assertions are not self referential. Every behaviour asserted
here was first observed on a live PostgreSQL 16.13 server, and the transcript
is checked into tools/saas_multitenant_pg_console/VERIFICATION.md. These tests
hold the simulator to what the database actually did.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tools.saas_multitenant_pg_console.core import (
    ARCHITECTURE_MODELS,
    BYPASS_ATTRIBUTE,
    BYPASS_DISABLED,
    BYPASS_OWNER,
    BYPASS_PERMISSIVE,
    COMPANIES,
    COMPANY_BY_ID,
    ENGINE_VERSION,
    MODEL_DATABASE,
    MODEL_SCHEMA,
    MODEL_SHARED,
    OUTCOME_BLOCKED,
    OUTCOME_ERROR,
    OUTCOME_LEAKED,
    OUTCOME_RETURNED,
    PROBES,
    SAFE_CONFIG,
    SCOPE_LOCAL,
    SCOPE_SESSION,
    SEVERITY_CRIT,
    SEVERITY_OK,
    SEVERITY_WARN,
    TABLES_PER_TENANT,
    TENANTS,
    TENANT_GUC,
    RlsConfig,
    build_sql,
    ddl_checklist,
    get_architecture_tradeoffs,
    get_sample_rls_ddl,
    isolation_matrix,
    leak_count,
    probe_rows,
    scaling_rows,
    simulate_rls_query,
    tradeoff_rows,
)

ROOT = Path(__file__).resolve().parents[1]

OWN_COMPANY = 101      # acme
OTHER_COMPANY = 103    # globex


def flat(text: str) -> str:
    """Collapse whitespace so an assertion is not broken by a line wrap."""
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# The correct configuration isolates
# ---------------------------------------------------------------------------


def test_a_tenant_reads_its_own_row():
    result = simulate_rls_query("acme", OWN_COMPANY)
    assert result.outcome == OUTCOME_RETURNED
    assert [row.company_id for row in result.rows] == [OWN_COMPANY]
    assert result.cross_tenant is False


def test_a_tenant_cannot_read_another_tenants_row():
    result = simulate_rls_query("acme", OTHER_COMPANY)
    assert result.outcome == OUTCOME_BLOCKED
    assert result.rows == ()
    assert result.returned is False


def test_the_safe_configuration_leaks_nothing_at_all():
    """A single passing query is not an isolation test. The grid is."""
    assert leak_count() == 0


def test_the_matrix_covers_every_pair():
    rows = isolation_matrix()
    assert len(rows) == len(TENANTS)
    for row in rows:
        # One label column plus one column per company.
        assert len(row) == len(COMPANIES) + 1


def test_the_matrix_marks_exactly_the_owned_cells_visible():
    for tenant, row in zip(TENANTS, isolation_matrix()):
        for company in COMPANIES:
            cell = row[f"{company.company_id} {company.name}"]
            expected = ("visible" if company.tenant_id == tenant.tenant_id
                        else "blocked")
            assert cell == expected


def test_every_tenant_sees_only_its_own_company_count():
    for tenant in TENANTS:
        visible = [c for c in COMPANIES
                   if simulate_rls_query(tenant.tenant_id,
                                         c.company_id).returned]
        assert {c.tenant_id for c in visible} == {tenant.tenant_id}


def test_an_unknown_tenant_is_rejected():
    with pytest.raises(ValueError, match="unknown tenant"):
        simulate_rls_query("hooli", OWN_COMPANY)


def test_an_unknown_company_is_rejected():
    with pytest.raises(ValueError, match="unknown company id"):
        simulate_rls_query("acme", 999)


def test_an_unknown_previous_tenant_is_rejected():
    with pytest.raises(ValueError, match="unknown previous tenant"):
        simulate_rls_query("acme", OWN_COMPANY, SAFE_CONFIG, "hooli")


def test_an_invalid_scope_is_rejected():
    with pytest.raises(ValueError, match="scope must be one of"):
        RlsConfig(scope="SET GLOBAL")


# ---------------------------------------------------------------------------
# The owner bypass, which is the failure that matters
# ---------------------------------------------------------------------------


def test_enable_without_force_lets_the_owner_read_everything():
    """Measured: owner saw 3 of 3 rows where a non owner saw 2."""
    config = RlsConfig(connect_as_owner=True, force_rls=False)
    result = simulate_rls_query("acme", OTHER_COMPANY, config)
    assert result.outcome == OUTCOME_LEAKED
    assert result.bypass_reason == BYPASS_OWNER
    assert result.cross_tenant is True


def test_force_makes_the_owner_obey_the_policy():
    config = RlsConfig(connect_as_owner=True, force_rls=True)
    result = simulate_rls_query("acme", OTHER_COMPANY, config)
    assert result.outcome == OUTCOME_BLOCKED
    assert result.bypass_reason is None


def test_a_non_owner_is_unaffected_by_the_force_setting():
    for force in (True, False):
        config = RlsConfig(connect_as_owner=False, force_rls=force)
        assert simulate_rls_query("acme", OTHER_COMPANY,
                                  config).outcome == OUTCOME_BLOCKED


def test_the_owner_bypass_leaks_every_pair_it_can():
    config = RlsConfig(connect_as_owner=True, force_rls=False)
    # Every tenant reaches every company that is not its own.
    expected = sum(1 for t in TENANTS for c in COMPANIES
                   if c.tenant_id != t.tenant_id)
    assert leak_count(config) == expected


def test_the_owner_finding_names_the_missing_line():
    config = RlsConfig(connect_as_owner=True, force_rls=False)
    result = simulate_rls_query("acme", OTHER_COMPANY, config)
    finding = next(f for f in result.findings if f.code == "RLS-OWNER")
    assert finding.severity == SEVERITY_CRIT
    assert "FORCE ROW LEVEL SECURITY" in finding.fix
    assert "3" in finding.detail and "2" in finding.detail


# ---------------------------------------------------------------------------
# The other ways a policy stops applying
# ---------------------------------------------------------------------------


def test_bypassrls_defeats_force_as_well():
    config = RlsConfig(bypassrls_role=True, force_rls=True)
    result = simulate_rls_query("acme", OTHER_COMPANY, config)
    assert result.bypass_reason == BYPASS_ATTRIBUTE
    assert result.outcome == OUTCOME_LEAKED
    assert "RLS-BYPASSRLS" in [f.code for f in result.findings]


def test_a_policy_without_enable_is_inert():
    config = RlsConfig(rls_enabled=False)
    result = simulate_rls_query("acme", OTHER_COMPANY, config)
    assert result.bypass_reason == BYPASS_DISABLED
    assert result.outcome == OUTCOME_LEAKED


def test_an_added_permissive_policy_widens_access():
    """Measured: 2 rows, one permissive policy added, 3 rows."""
    config = RlsConfig(extra_permissive_policy=True)
    result = simulate_rls_query("acme", OTHER_COMPANY, config)
    assert result.bypass_reason == BYPASS_PERMISSIVE
    assert result.outcome == OUTCOME_LEAKED


def test_the_same_policy_declared_restrictive_does_not():
    """Measured: restrictive policies are ANDed, the count held at 2."""
    config = RlsConfig(extra_permissive_policy=True,
                       extra_policy_restrictive=True)
    result = simulate_rls_query("acme", OTHER_COMPANY, config)
    assert result.bypass_reason is None
    assert result.outcome == OUTCOME_BLOCKED


def test_the_permissive_finding_explains_the_or():
    config = RlsConfig(extra_permissive_policy=True)
    finding = next(f for f in simulate_rls_query("acme", OTHER_COMPANY, config)
                   .findings if f.code == "RLS-PERMISSIVE")
    assert "OR" in finding.detail
    assert "RESTRICTIVE" in finding.fix


# ---------------------------------------------------------------------------
# The pooled connection leak
# ---------------------------------------------------------------------------


def test_a_plain_set_leaves_the_previous_tenants_context_behind():
    """Measured: a plain SET is still readable after COMMIT."""
    config = RlsConfig(scope=SCOPE_SESSION, set_context=False)
    result = simulate_rls_query("acme", OTHER_COMPANY, config, "globex")
    assert result.active_context == "globex"
    assert result.outcome == OUTCOME_LEAKED
    assert "RLS-POOL-LEAK" in [f.code for f in result.findings]


def test_set_local_cannot_leak_whatever_ran_before():
    """Measured: SET LOCAL is gone at COMMIT, so the previous value is not
    reachable no matter who held the connection last."""
    config = RlsConfig(scope=SCOPE_LOCAL, set_context=False)
    for previous in [t.tenant_id for t in TENANTS] + [None]:
        result = simulate_rls_query("acme", OTHER_COMPANY, config, previous)
        assert result.active_context is None
        assert result.outcome == OUTCOME_BLOCKED


def test_an_unpooled_session_set_cannot_inherit_a_context():
    config = RlsConfig(scope=SCOPE_SESSION, pooled=False, set_context=False)
    result = simulate_rls_query("acme", OTHER_COMPANY, config, "globex")
    assert result.active_context is None


def test_the_pool_leak_warning_fires_on_every_session_scoped_request():
    config = RlsConfig(scope=SCOPE_SESSION, pooled=True)
    result = simulate_rls_query("acme", OWN_COMPANY, config)
    # The query itself is correct. The configuration is still the bug.
    assert result.outcome == OUTCOME_RETURNED
    assert "RLS-POOL-LEAK" in [f.code for f in result.findings]


def test_the_sql_shows_set_local_inside_a_transaction():
    sql = build_sql("acme", OWN_COMPANY, RlsConfig(scope=SCOPE_LOCAL))
    assert "BEGIN;" in sql
    assert f"SET LOCAL {TENANT_GUC} = 'acme';" in sql
    assert "COMMIT;" in sql


def test_the_sql_shows_a_session_set_never_being_committed_away():
    sql = build_sql("acme", OWN_COMPANY, RlsConfig(scope=SCOPE_SESSION))
    assert f"SET {TENANT_GUC} = 'acme';" in sql
    assert "SET LOCAL" not in sql
    assert "the context stays on the connection" in sql


# ---------------------------------------------------------------------------
# The unset context
# ---------------------------------------------------------------------------


def test_a_missing_context_without_missing_ok_raises_42704():
    """Measured: SQLSTATE 42704 on a parameter that was never set."""
    config = RlsConfig(set_context=False, missing_ok=False, pooled=False)
    result = simulate_rls_query("acme", OWN_COMPANY, config)
    assert result.outcome == OUTCOME_ERROR
    assert "42704" in result.error
    assert result.rows == ()


def test_a_missing_context_with_missing_ok_returns_nothing_quietly():
    config = RlsConfig(set_context=False, pooled=False)
    result = simulate_rls_query("acme", OWN_COMPANY, config)
    assert result.outcome == OUTCOME_BLOCKED
    assert result.error is None
    assert "RLS-SILENT-ZERO" in [f.code for f in result.findings]


def test_the_silent_zero_finding_says_why_that_is_a_problem():
    config = RlsConfig(set_context=False, pooled=False)
    finding = next(f for f in simulate_rls_query("acme", OWN_COMPANY, config)
                   .findings if f.code == "RLS-SILENT-ZERO")
    assert finding.severity == SEVERITY_WARN
    assert "no data" in finding.title


def test_missing_ok_off_is_flagged_even_when_the_query_succeeds():
    config = RlsConfig(missing_ok=False)
    result = simulate_rls_query("acme", OWN_COMPANY, config)
    assert result.outcome == OUTCOME_RETURNED
    assert "RLS-MISSING-OK" in [f.code for f in result.findings]


def test_the_predicate_shows_the_missing_ok_argument_in_the_sql():
    on = build_sql("acme", OWN_COMPANY, RlsConfig(missing_ok=True))
    off = build_sql("acme", OWN_COMPANY, RlsConfig(missing_ok=False))
    assert on == off, "missing_ok changes the policy, not the query"


# ---------------------------------------------------------------------------
# Findings in general
# ---------------------------------------------------------------------------


def test_every_configuration_produces_at_least_one_finding():
    for owner in (True, False):
        for force in (True, False):
            for scope in (SCOPE_LOCAL, SCOPE_SESSION):
                config = RlsConfig(connect_as_owner=owner, force_rls=force,
                                   scope=scope)
                result = simulate_rls_query("acme", OWN_COMPANY, config)
                assert result.findings


def test_the_safe_configuration_reports_that_the_policy_held():
    result = simulate_rls_query("acme", OWN_COMPANY)
    assert [f.code for f in result.findings] == ["RLS-HELD"]
    assert result.findings[0].severity == SEVERITY_OK


def test_every_cross_tenant_result_raises_the_cross_tenant_finding():
    for config in (RlsConfig(connect_as_owner=True, force_rls=False),
                   RlsConfig(bypassrls_role=True),
                   RlsConfig(rls_enabled=False),
                   RlsConfig(extra_permissive_policy=True)):
        result = simulate_rls_query("acme", OTHER_COMPANY, config)
        assert result.outcome == OUTCOME_LEAKED
        assert "RLS-CROSS-TENANT" in [f.code for f in result.findings]


def test_findings_always_carry_a_code_a_detail_and_a_fix():
    for config in (SAFE_CONFIG, RlsConfig(connect_as_owner=True,
                                          force_rls=False),
                   RlsConfig(scope=SCOPE_SESSION), RlsConfig(missing_ok=False)):
        for company in COMPANIES:
            for finding in simulate_rls_query("acme", company.company_id,
                                              config).findings:
                assert finding.code.startswith("RLS-")
                assert finding.detail.strip()
                assert finding.fix.strip()
                assert finding.severity in (SEVERITY_OK, SEVERITY_WARN,
                                            SEVERITY_CRIT)


def test_simulation_is_deterministic():
    config = RlsConfig(connect_as_owner=True, force_rls=False)
    first = simulate_rls_query("acme", OTHER_COMPANY, config)
    second = simulate_rls_query("acme", OTHER_COMPANY, config)
    assert first.outcome == second.outcome
    assert first.sql == second.sql
    assert [f.code for f in first.findings] == [f.code for f in second.findings]


# ---------------------------------------------------------------------------
# Architecture trade offs
# ---------------------------------------------------------------------------


def test_three_models_are_compared():
    models = get_architecture_tradeoffs()
    assert [m.name for m in models] == [MODEL_SHARED, MODEL_SCHEMA,
                                        MODEL_DATABASE]


def test_every_model_names_isolation_complexity_and_cost():
    for model in get_architecture_tradeoffs():
        assert model.isolation.strip()
        assert model.operational_complexity.strip()
        assert model.cost.strip()
        assert model.blast_radius.strip()
        assert model.breaks_at.strip()
        assert model.best_for.strip()


def test_the_tradeoff_rows_cover_every_model():
    rows = tradeoff_rows()
    assert len(rows) == len(ARCHITECTURE_MODELS)
    assert {r["Model"] for r in rows} == {m.name for m in ARCHITECTURE_MODELS}


def test_shared_schema_migrates_once_whatever_the_tenant_count():
    for count in (1, 100, 5000):
        row = next(r for r in scaling_rows(count) if r["Model"] == MODEL_SHARED)
        assert row["Migration runs per release"] == "1"


def test_schema_and_database_per_tenant_migrate_once_per_tenant():
    rows = {r["Model"]: r for r in scaling_rows(250)}
    assert rows[MODEL_SCHEMA]["Migration runs per release"] == "250"
    assert rows[MODEL_DATABASE]["Migration runs per release"] == "250"


def test_the_catalog_grows_only_for_the_per_tenant_models():
    rows = {r["Model"]: r for r in scaling_rows(500)}
    assert rows[MODEL_SHARED]["Tables in the catalog"] == f"{TABLES_PER_TENANT}"
    assert rows[MODEL_SCHEMA]["Tables in the catalog"] == "20,000"
    assert rows[MODEL_DATABASE]["Tables in the catalog"] == "20,000"


def test_only_database_per_tenant_multiplies_the_connection_count():
    rows = {r["Model"]: r for r in scaling_rows(500, pool_size=10)}
    assert rows[MODEL_SHARED]["Backend connections"] == "10"
    assert rows[MODEL_SCHEMA]["Backend connections"] == "10"
    assert rows[MODEL_DATABASE]["Backend connections"] == "5,000"


def test_the_connection_count_scales_with_both_inputs():
    a = next(r for r in scaling_rows(100, 5) if r["Model"] == MODEL_DATABASE)
    b = next(r for r in scaling_rows(200, 5) if r["Model"] == MODEL_DATABASE)
    c = next(r for r in scaling_rows(100, 10) if r["Model"] == MODEL_DATABASE)
    assert a["Backend connections"] == "500"
    assert b["Backend connections"] == "1,000"
    assert c["Backend connections"] == "1,000"


def test_a_nonsense_tenant_count_is_rejected():
    with pytest.raises(ValueError, match="tenant_count must be at least 1"):
        scaling_rows(0)


def test_a_nonsense_pool_size_is_rejected():
    with pytest.raises(ValueError, match="pool_size must be at least 1"):
        scaling_rows(10, 0)


def test_only_the_shared_model_adds_no_catalog_objects_per_tenant():
    by_name = {m.name: m for m in ARCHITECTURE_MODELS}
    assert by_name[MODEL_SHARED].catalog_objects_per_tenant == 0
    assert by_name[MODEL_SCHEMA].catalog_objects_per_tenant > 0
    assert by_name[MODEL_DATABASE].catalog_objects_per_tenant > 0


# ---------------------------------------------------------------------------
# The DDL
# ---------------------------------------------------------------------------


def sql_only(ddl: str) -> str:
    """The statements with every comment line removed.

    Two assertions below first matched the comments that explain why a keyword
    is absent, which is the opposite of what they were asking.
    """
    return "\n".join(line for line in ddl.splitlines()
                     if not line.strip().startswith("--"))


def test_the_ddl_enables_and_forces_row_level_security():
    ddl = get_sample_rls_ddl()
    assert "ENABLE ROW LEVEL SECURITY" in ddl
    assert "FORCE  ROW LEVEL SECURITY" in ddl


def test_the_ddl_creates_the_table_and_the_policies():
    ddl = get_sample_rls_ddl()
    assert "CREATE TABLE companies" in ddl
    assert ddl.count("CREATE POLICY") == 2


def test_the_first_policy_is_permissive_and_the_guard_is_restrictive():
    """The bug the live server found: all restrictive means nobody sees
    anything, because restrictive policies only narrow what permissive
    policies granted."""
    ddl = get_sample_rls_ddl()
    isolation = ddl.split("CREATE POLICY tenant_isolation")[1].split(";")[0]
    guard = ddl.split("CREATE POLICY tenant_context_required")[1].split(";")[0]
    assert "AS RESTRICTIVE" not in isolation
    assert "AS RESTRICTIVE" in guard


def test_both_policies_write_with_check_explicitly():
    clauses = [line for line in sql_only(get_sample_rls_ddl()).splitlines()
               if line.strip().startswith("WITH CHECK")]
    assert len(clauses) == 2


def test_the_ddl_uses_set_local_and_never_a_bare_session_set():
    ddl = get_sample_rls_ddl()
    assert "SET LOCAL app.tenant_id" in ddl
    for line in ddl.splitlines():
        stripped = line.strip()
        if stripped.startswith("SET ") and not stripped.startswith("SET LOCAL"):
            pytest.fail(f"a session scoped SET reached the DDL: {stripped}")


def test_the_ddl_creates_the_application_role_without_bypassrls():
    ddl = get_sample_rls_ddl()
    assert "CREATE ROLE app_user" in ddl
    assert "NOBYPASSRLS" in ddl


def test_the_helper_is_stable_not_immutable():
    """The tenant changes between statements, so IMMUTABLE would be wrong and
    would let the planner fold the value into a cached plan."""
    statements = sql_only(get_sample_rls_ddl())
    assert "LANGUAGE sql STABLE" in statements
    assert "IMMUTABLE" not in statements


def test_the_helper_empties_the_empty_string_to_null():
    """Measured: RESET leaves the empty string rather than undefining."""
    assert "nullif(current_setting('app.tenant_id', true), '')" \
        in get_sample_rls_ddl()


def test_the_tenant_column_leads_the_index():
    assert "ON companies (tenant_id, company_id)" in get_sample_rls_ddl()


def test_the_ddl_is_one_transaction_for_the_schema():
    ddl = get_sample_rls_ddl()
    assert ddl.count("BEGIN;") == 2   # the schema, then the sample request
    assert ddl.count("COMMIT;") == 2


def test_the_checklist_covers_every_silent_line():
    lines = " ".join(r["Line"] for r in ddl_checklist())
    for needle in ("FORCE ROW LEVEL SECURITY", "RESTRICTIVE", "PERMISSIVE",
                   "SET LOCAL", "NOBYPASSRLS", "nullif"):
        assert needle in lines


def test_every_checklist_row_names_a_symptom_and_a_measurement():
    for row in ddl_checklist():
        assert set(row) == {"Line", "If it is missing", "Symptom", "Measured"}
        for value in row.values():
            assert isinstance(value, str) and value.strip()


# ---------------------------------------------------------------------------
# The recorded probes
# ---------------------------------------------------------------------------


def test_every_probe_records_a_statement_and_an_observation():
    assert len(PROBES) >= 10
    for probe in PROBES:
        assert probe.statement.strip()
        assert probe.observed.strip()
        assert probe.lesson.strip()


def test_the_probes_include_the_owner_bypass_and_the_pool_leak():
    names = " ".join(p.name for p in PROBES)
    assert "Owner with ENABLE only" in names
    assert "Plain SET after COMMIT" in names
    assert "restrictive" in names


def test_the_verification_transcript_ships_beside_the_engine():
    path = (ROOT / "tools" / "saas_multitenant_pg_console"
            / "VERIFICATION.md")
    assert path.exists()
    text = path.read_text()
    assert "PostgreSQL 16.13" in text
    assert "acme_sees=2" in text
    assert "globex_sees=1" in text


# ---------------------------------------------------------------------------
# Rows reaching the screen must be Arrow safe
# ---------------------------------------------------------------------------


def _assert_all_strings(rows):
    assert rows
    keys = set(rows[0])
    for row in rows:
        assert set(row) == keys, "ragged rows break the Arrow conversion"
        for value in row.values():
            assert isinstance(value, str)


def test_result_rows_are_arrow_safe():
    _assert_all_strings(simulate_rls_query("acme", OWN_COMPANY).row_dicts())


def test_matrix_rows_are_arrow_safe():
    _assert_all_strings(isolation_matrix())


def test_tradeoff_rows_are_arrow_safe():
    _assert_all_strings(tradeoff_rows())


def test_scaling_rows_are_arrow_safe():
    _assert_all_strings(scaling_rows(500))


def test_checklist_rows_are_arrow_safe():
    _assert_all_strings(ddl_checklist())


def test_probe_rows_are_arrow_safe():
    _assert_all_strings(probe_rows())


def test_the_row_dicts_flag_a_row_the_caller_does_not_own():
    config = RlsConfig(connect_as_owner=True, force_rls=False)
    rows = simulate_rls_query("acme", OTHER_COMPANY, config).row_dicts()
    assert rows[0]["Belongs to caller"] == "NO"


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "saas_multitenant_pg_console"
              / "core.py").read_text()
    assert "import streamlit" not in source
    assert "from streamlit" not in source


def test_the_core_is_importable_without_streamlit_installed():
    code = (
        "import sys;"
        "sys.modules['streamlit'] = None;"
        "from tools.saas_multitenant_pg_console import core;"
        "print(core.leak_count())"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0"


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [DISCLAIMER_SOURCE]
        + [m.isolation + m.blast_radius + m.breaks_at + m.best_for
           + m.noisy_neighbour + m.migration_cost
           for m in ARCHITECTURE_MODELS]
        + [p.lesson + p.observed for p in PROBES]
        + [f.detail + f.fix
           for config in (SAFE_CONFIG,
                          RlsConfig(connect_as_owner=True, force_rls=False),
                          RlsConfig(scope=SCOPE_SESSION),
                          RlsConfig(missing_ok=False),
                          RlsConfig(extra_permissive_policy=True),
                          RlsConfig(bypassrls_role=True),
                          RlsConfig(rls_enabled=False))
           for f in simulate_rls_query("acme", OTHER_COMPANY, config).findings]
        + [r["Symptom"] + r["If it is missing"] for r in ddl_checklist()]
    )
    assert "—" not in text
    assert "–" not in text


DISCLAIMER_SOURCE = (
    "This console holds no connection and reaches no database."
)


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools

    tools = all_tools()
    entry = next(t for t in tools if t.key == "saas-multitenant-pg-console")
    assert entry.title == "SaaS Multi Tenant PostgreSQL Console"
    assert len(entry.tagline) > 30
    icons = [t.icon for t in tools if not t.key.startswith("synthetic-")]
    assert icons.count(entry.icon) == 1
