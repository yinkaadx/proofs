"""Claude MCP Skill and Agent Testing Console.

Rendered inside the hub app. Every verdict on this page is computed from the
controls on each run, so a result on screen can never describe a manifest or
a query that was replaced two interactions ago.
"""

from __future__ import annotations

import json

import streamlit as st

from shared.theme import esc, inject
from tools.claude_mcp_skill_tester_console.core import (
    ENGINE_VERSION,
    KNOWN_KEYS,
    MIN_DESCRIPTION_CHARS,
    OPTIONAL_KEYS,
    REQUIRED_CATEGORIES,
    REQUIRED_KEYS,
    RESULT_BYPASSED,
    RESULT_EMPTY_BY_POLICY,
    RESULT_ERROR,
    RESULT_ROWS,
    ROLES,
    SAMPLE_MANIFESTS,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    VALID_PASSED,
    VALID_UNPARSEABLE,
    execute_boundary_test_matrix,
    simulate_mcp_connection,
    validate_skill_manifest,
)

TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}
OUTCOME_TONE = {RESULT_ROWS: "ok", RESULT_EMPTY_BY_POLICY: "crit",
                RESULT_BYPASSED: "crit", RESULT_ERROR: "warn"}


def _kpis(items: list[tuple[str, str]]) -> None:
    cells = "".join(
        f'<div class="app-kpi"><b>{esc(value)}</b><span>{esc(label)}</span></div>'
        for label, value in items)
    st.markdown(f'<div class="app-kpis">{cells}</div>', unsafe_allow_html=True)


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
  <h1>Claude MCP Skill and Agent Testing Console</h1>
  <p>Three checks that catch the failures an agent integration produces
  silently. A skill is loaded by matching its description, so a thin one is
  not a style problem, it is a skill that never loads and never errors. A
  database with row level security returns zero rows rather than refusing, so
  a tool reporting no results may be reporting you are not allowed. And a
  test run of only happy paths tells you the thing works when nothing is
  wrong, which was never in doubt.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Manifest**: pick the sample with the typo and watch the "
            "unknown key get reported rather than ignored.\n"
            "2. **MCP**: clear the tenant and read what zero rows actually "
            "means, then switch to the service role key.\n"
            "3. **Matrix**: drop a category and watch the pass count stop "
            "meaning what it appears to mean."
        )
        st.divider()
        st.caption(
            "Nothing here loads a real skill, contacts a database or calls "
            "an MCP server. The fixtures are local."
        )
        st.caption(
            f"The manifest rules come from the twelve skill manifests on the "
            f"machine that built this: {', '.join(REQUIRED_KEYS)} appear in "
            f"all twelve, license in four. They are stated as that evidence "
            "rather than as a specification."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_manifest, tab_mcp, tab_matrix = st.tabs(
        ["Skill Manifest", "MCP Connection", "Boundary Matrix"])

    # -----------------------------------------------------------------
    with tab_manifest:
        st.subheader("Would this manifest actually load")
        choice = st.selectbox("A manifest", list(SAMPLE_MANIFESTS))
        default = json.dumps(SAMPLE_MANIFESTS[choice], indent=2)
        payload = st.text_area("The frontmatter as JSON", value=default,
                               height=200, key=f"manifest_{choice}")

        report = validate_skill_manifest(payload)

        _kpis([
            ("Status", report.status),
            ("Would load", "yes" if report.would_load else "no"),
            ("Missing", str(len(report.missing_keys))),
            ("Type errors", str(len(report.type_errors))),
            ("Unknown keys", str(len(report.unknown_keys))),
        ])

        tone = "ok" if report.would_load else "crit"
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(report.status)}</span>
  {esc(report.headline)}</h4>
  <p>{esc('Parsed into an object with ' + str(len(report.keys_seen))
           + ' key(s).' if report.parsed
           else 'Nothing was checked, because nothing could be read.')}</p>
  <div class="app-ev">Description:
  {report.description_chars} characters, against a
  {MIN_DESCRIPTION_CHARS} character floor for loading at all</div>
</div>
""",
            unsafe_allow_html=True,
        )

        if report.parsed:
            st.markdown("**Key by key**")
            for check in report.checks:
                row_tone = ("ok" if check.present and check.type_ok
                            else "crit" if check.required
                            else "info")
                state = ("present" if check.present else "absent")
                st.markdown(
                    f"- `{check.key}` ({'required' if check.required else 'optional'}): "
                    f"{state}, expected `{check.expected_type}`, "
                    f"got `{check.actual_type}`"
                    f"{'' if check.type_ok else ' **type error**'}")

        st.markdown("**Findings**")
        for finding in report.findings:
            _finding_card(finding)

        st.subheader("Keys this validator knows")
        st.markdown(
            f"- Required: {', '.join('`' + k + '`' for k in REQUIRED_KEYS)}\n"
            f"- Optional: {', '.join('`' + k + '`' for k in OPTIONAL_KEYS)}")

    # -----------------------------------------------------------------
    with tab_mcp:
        st.subheader("What the database actually returns")
        left, right = st.columns(2)
        with left:
            role = st.selectbox("Role the connection runs as", list(ROLES),
                                index=1)
            tenant = st.text_input("Tenant claim on the session", value="acme")
        with right:
            enforce = st.toggle("Row level security enforced", value=True)
            table = st.text_input("Table", value="documents")

        response = simulate_mcp_connection(
            {"role": role, "tenant_id": tenant, "table": table}, enforce)

        _kpis([
            ("Outcome", response.outcome),
            ("Rows", str(response.row_count)),
            ("Raised an error", "yes" if response.raised_error else "no"),
            ("Silent denial", "yes" if response.silent_denial else "no"),
            ("Other tenants' rows", str(response.cross_tenant_rows)),
        ])

        tone = OUTCOME_TONE.get(response.outcome, "warn")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(response.outcome)}</span>
  {esc(response.headline)}</h4>
  <p>An empty result and a policy denial are the same response. Nothing in
  the payload distinguishes them unless the tool puts the tenant context in
  it deliberately.</p>
  <div class="app-ev">Role {esc(response.role)}, security
  {'enforced' if response.enforce_rls else 'switched off'}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**The response the agent would read**")
        st.code(response.response_json, language="json")

        st.markdown("**Findings**")
        for finding in response.findings:
            _finding_card(finding)

        st.subheader("The same table under four configurations")
        for label, params, rls in (
                ("Scoped read, tenant set",
                 {"role": "authenticated", "tenant_id": "acme"}, True),
                ("No tenant claim set",
                 {"role": "authenticated", "tenant_id": ""}, True),
                ("Service role key",
                 {"role": "service_role", "tenant_id": "acme"}, True),
                ("Security switched off",
                 {"role": "authenticated", "tenant_id": "acme"}, False)):
            row = simulate_mcp_connection(params, rls)
            st.markdown(
                f"- **{label}**: {row.row_count} row(s), "
                f"{row.cross_tenant_rows} belonging to another tenant, "
                f"{'no error raised' if not row.raised_error else 'error raised'}")

    # -----------------------------------------------------------------
    with tab_matrix:
        st.subheader("What the pass count is worth")
        left, right = st.columns(2)
        with left:
            skill_name = st.text_input("Skill under test",
                                       value="gtm-container-auditor")
        with right:
            categories = st.multiselect(
                "Categories to run", list(REQUIRED_CATEGORIES),
                default=list(REQUIRED_CATEGORIES))

        if not categories:
            st.markdown(
                """
<div class="app-card crit">
  <h4><span class="app-tag crit">NOTHING SELECTED</span>
  A matrix with no categories tests nothing</h4>
  <p>Select at least one category. A run with none of them would report a
  clean result having exercised nothing at all.</p>
  <div class="app-ev">That is the failure this whole tab argues against.</div>
</div>
""",
                unsafe_allow_html=True,
            )
        else:
            matrix = execute_boundary_test_matrix(
                skill_name or "unnamed-skill", tuple(categories))

            _kpis([
                ("Passed", f"{matrix.passed} of {len(matrix.cases)}"),
                ("Failed", str(matrix.failed)),
                ("Categories",
                 f"{len(matrix.categories_covered)} of {len(REQUIRED_CATEGORIES)}"),
                ("Coverage",
                 "full" if matrix.coverage_sufficient else "incomplete"),
            ])
            st.caption(
                f"{matrix.passed} passed plus {matrix.failed} failed equals "
                f"{len(matrix.cases)} cases, which is every case run."
            )

            tone = "ok" if matrix.coverage_sufficient and not matrix.failed else "crit"
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">
  {'FULL COVERAGE' if matrix.coverage_sufficient else 'PARTIAL'}</span>
  {esc(matrix.headline)}</h4>
  <p>{esc('Everything was exercised.' if matrix.coverage_sufficient
           else 'Untested: ' + ', '.join(matrix.categories_missing)
                + '. Whatever was not exercised is unmeasured rather than'
                  ' passing.')}</p>
  <div class="app-ev">A pass count from a partial matrix still reads as a
  pass, which is why the coverage line sits beside it.</div>
</div>
""",
                unsafe_allow_html=True,
            )

            st.markdown("**The run log**")
            st.code("\n".join(matrix.log), language="text")

            st.markdown("**Case by case**")
            for case in matrix.cases:
                row_tone = "ok" if case.passed else "crit"
                st.markdown(
                    f"""
<div class="app-card {row_tone}">
  <h4><span class="app-tag {row_tone}">{esc(case.result)}</span>
  {esc(case.category)}: {esc(case.name)}</h4>
  <p>Given {esc(case.given.lower())}, expected
  {esc(case.expected.lower())}.</p>
  <div class="app-ev">Observed: {esc(case.observed)}</div>
</div>
""",
                    unsafe_allow_html=True,
                )

            st.markdown("**Findings**")
            for finding in matrix.findings:
                _finding_card(finding)
