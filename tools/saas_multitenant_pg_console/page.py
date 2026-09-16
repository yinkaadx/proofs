"""SaaS Multi Tenant PostgreSQL Console.

Rendered inside the hub. All logic lives in core.py, which holds no Streamlit
import, and nothing is kept in session state, so no card on screen can be
describing a configuration that was replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.saas_multitenant_pg_console.core import (
    COMPANIES,
    ENGINE_VERSION,
    MODEL_BY_NAME,
    OUTCOME_BLOCKED,
    OUTCOME_ERROR,
    OUTCOME_LEAKED,
    SCOPE_LOCAL,
    SCOPE_SESSION,
    SCOPES,
    SERVER_MEASURED,
    SEVERITY_CRIT,
    SEVERITY_OK,
    SEVERITY_WARN,
    TENANTS,
    RlsConfig,
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

TONE = {SEVERITY_CRIT: "crit", SEVERITY_WARN: "warn", SEVERITY_OK: "ok"}

TENANT_BY_LABEL = {f"{t.name} ({t.tenant_id})": t for t in TENANTS}
COMPANY_BY_LABEL = {f"{c.company_id} {c.name}": c for c in COMPANIES}

DISCLAIMER = (
    "This console holds no connection and reaches no database. It reproduces "
    f"behaviour measured on {SERVER_MEASURED} before the engine was written, "
    "and the transcript of those probes ships beside it."
)


def _finding_card(finding) -> None:
    tone = TONE.get(finding.severity, "warn")
    st.markdown(
        f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(finding.code)}</span>
  {esc(finding.title)}</h4>
  <p>{esc(finding.detail)}</p>
  <div class="app-ev">{esc(finding.fix)}</div>
</div>
""",
        unsafe_allow_html=True,
    )


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>SaaS Multi Tenant PostgreSQL Console</h1>
  <p>Row Level Security fails quietly. The policy is written, the migration
  runs, the tests pass, and the one line that makes the policy apply to the
  role your application actually connects as was never added. Nothing errors.
  This console runs a tenant's query through the policy under each of those
  configurations and shows which rows come back.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Isolation tester**: pick a tenant and a company they do not "
            "own, then turn FORCE off and watch the row arrive.\n"
            "2. Switch the context scope to plain SET and set a previous "
            "tenant to see the pooled connection leak.\n"
            "3. **Architecture**: move the tenant count and read the two "
            "numbers that actually decide the model.\n"
            "4. **DDL**: copy the script. It was executed before it shipped."
        )
        st.divider()
        st.caption(DISCLAIMER)
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_query, tab_matrix, tab_arch, tab_ddl = st.tabs(
        ["Isolation Tester", "Cross Tenant Matrix", "Architecture",
         "Production DDL"])

    # -----------------------------------------------------------------
    # One query, one policy, one configuration
    # -----------------------------------------------------------------
    with tab_query:
        c1, c2 = st.columns(2)
        tenant = TENANT_BY_LABEL[c1.selectbox(
            "Connected tenant", list(TENANT_BY_LABEL), key="smp_tenant")]
        company = COMPANY_BY_LABEL[c2.selectbox(
            "Company being requested", list(COMPANY_BY_LABEL), index=2,
            key="smp_company")]

        st.markdown("###### Connection and table configuration")
        d1, d2, d3 = st.columns(3)
        rls_enabled = d1.toggle("ENABLE ROW LEVEL SECURITY", value=True,
                                key="smp_enable")
        force_rls = d2.toggle("FORCE ROW LEVEL SECURITY", value=True,
                              key="smp_force")
        as_owner = d3.toggle("Connect as the table owner", value=False,
                             key="smp_owner")

        e1, e2, e3 = st.columns(3)
        bypass = e1.toggle("Role has BYPASSRLS", value=False, key="smp_bypass")
        extra = e2.toggle("A reporting policy was added", value=False,
                          key="smp_extra")
        restrictive = e3.toggle("That policy is AS RESTRICTIVE", value=False,
                                key="smp_restrictive")

        f1, f2, f3 = st.columns(3)
        scope = f1.radio("Context scope", SCOPES, index=0, key="smp_scope")
        set_context = f2.toggle("This request sets its context", value=True,
                                key="smp_setctx")
        missing_ok = f3.toggle("current_setting passes missing_ok", value=True,
                               key="smp_missing")

        pooled = st.toggle("Behind a pooler in transaction mode", value=True,
                           key="smp_pooled")
        previous = None
        if pooled:
            choice = st.selectbox(
                "Tenant that last used this pooled connection",
                ["none"] + list(TENANT_BY_LABEL), key="smp_prev")
            if choice != "none":
                previous = TENANT_BY_LABEL[choice].tenant_id

        config = RlsConfig(
            connect_as_owner=as_owner, rls_enabled=rls_enabled,
            force_rls=force_rls, bypassrls_role=bypass,
            extra_permissive_policy=extra, extra_policy_restrictive=restrictive,
            scope=scope, pooled=pooled, missing_ok=missing_ok,
            set_context=set_context)

        result = simulate_rls_query(tenant.tenant_id, company.company_id,
                                    config, previous)

        if result.outcome == OUTCOME_LEAKED:
            outcome_tone = "crit"
        elif result.outcome == OUTCOME_ERROR:
            outcome_tone = "warn"
        elif result.outcome == OUTCOME_BLOCKED:
            outcome_tone = "ok"
        else:
            outcome_tone = "ok"
        owns = "yes" if company.tenant_id == tenant.tenant_id else "no"

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {outcome_tone}"><div class="n">{len(result.rows)}</div>
    <div class="l">Rows returned, {esc(result.outcome)}</div></div>
  <div class="app-kpi"><div class="n">{esc(
      result.active_context or "unset")}</div>
    <div class="l">Active tenant context</div></div>
  <div class="app-kpi"><div class="n">{owns}</div>
    <div class="l">Caller owns this company</div></div>
  <div class="app-kpi {"crit" if result.bypass_reason else "ok"}">
    <div class="n">{"yes" if result.bypass_reason else "no"}</div>
    <div class="l">Policy bypassed</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.caption(f"Context source: {esc(result.context_source)}.")
        if result.bypass_reason:
            st.caption(f"Bypass reason: {esc(result.bypass_reason)}.")

        st.markdown("##### What the application sent")
        st.code(result.sql, language="sql")

        if result.error:
            st.error(result.error)

        for finding in result.findings:
            _finding_card(finding)

        st.markdown("##### What came back")
        if result.rows:
            st.dataframe(result.row_dicts(), width="stretch", hide_index=True)
        else:
            st.caption(
                "No rows. Under a correct configuration that is what a request "
                "for another tenant's data is supposed to look like."
            )

    # -----------------------------------------------------------------
    # One query proves nothing, the grid proves something
    # -----------------------------------------------------------------
    with tab_matrix:
        st.caption(
            "Every tenant against every company under the configuration set on "
            "the first tab. A single passing query is not an isolation test; "
            "this grid is."
        )
        leaks = leak_count(config, previous)
        total = len(TENANTS) * len(COMPANIES)
        tone = "crit" if leaks else "ok"
        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {tone}"><div class="n">{leaks}</div>
    <div class="l">Cross tenant leaks</div></div>
  <div class="app-kpi"><div class="n">{total}</div>
    <div class="l">Pairs tested</div></div>
  <div class="app-kpi"><div class="n">{len(TENANTS)}</div>
    <div class="l">Tenants</div></div>
  <div class="app-kpi"><div class="n">{len(COMPANIES)}</div>
    <div class="l">Companies</div></div>
</div>
""",
            unsafe_allow_html=True,
        )
        st.dataframe(isolation_matrix(config, previous), width="stretch",
                     hide_index=True)

        st.markdown("##### What was actually run against a server")
        st.caption(
            "None of the behaviour above is a guess. These probes were "
            f"executed on {SERVER_MEASURED} before the engine was written."
        )
        st.dataframe(probe_rows(), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Choosing a model
    # -----------------------------------------------------------------
    with tab_arch:
        st.dataframe(tradeoff_rows(), width="stretch", hide_index=True)

        st.markdown("##### What each model costs at your tenant count")
        g1, g2 = st.columns(2)
        tenant_count = g1.slider("Tenants", 1, 5000, 500, key="smp_tenants")
        pool_size = g2.slider("Connections per pool", 1, 50, 10,
                              key="smp_pool")
        st.caption(
            "Catalog size and backend connections are what decide this in "
            "practice, and neither appears on a feature comparison chart."
        )
        st.dataframe(scaling_rows(tenant_count, pool_size), width="stretch",
                     hide_index=True)

        for model in get_architecture_tradeoffs():
            st.markdown(
                f"""
<div class="app-card">
  <h4><span class="app-tag info">{esc(model.name)}</span>
  {esc(model.best_for)}</h4>
  <p>{esc(model.breaks_at)}.</p>
  <div class="app-ev">Migrations: {esc(model.migration_cost)}. Noisy
  neighbour: {esc(model.noisy_neighbour)}.</div>
</div>
""",
                unsafe_allow_html=True,
            )

    # -----------------------------------------------------------------
    # The script
    # -----------------------------------------------------------------
    with tab_ddl:
        st.caption(
            "Run with psql -v ON_ERROR_STOP=1 -v app_password=... "
            "-v tenant_id=... This exact script was executed against "
            f"{SERVER_MEASURED} and the isolation it produces was measured "
            "before it shipped."
        )
        st.code(get_sample_rls_ddl(), language="sql")

        st.markdown("##### The lines whose absence is silent")
        st.dataframe(ddl_checklist(), width="stretch", hide_index=True)

        st.markdown(
            f"""
<div class="app-foot">{esc(DISCLAIMER)} Models compared:
{len(get_architecture_tradeoffs())}. Probes recorded: {len(probe_rows())}.
Engine version {ENGINE_VERSION}.</div>
""",
            unsafe_allow_html=True,
        )
