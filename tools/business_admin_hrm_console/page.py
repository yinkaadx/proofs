"""Business Admin and HRM Architecture Console.

Rendered inside the hub app. All logic lives in core.py, which has no
Streamlit dependency, generates the organisation rather than describing
it, and really signs the tokens it verifies.

Everything on this page is evaluated live from the controls. There is no
session state to go stale, which removes the class of bug where a card on
screen describes a verdict that two interactions ago stopped being true.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.business_admin_hrm_console.core import (
    ACTIONS,
    ALGORITHM,
    ALLOW,
    ALLOW_SCOPED,
    DENY,
    ENGINE_VERSION,
    RESOURCES,
    ROLES,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    VERIFIED,
    WP_TO_EMPLOYEE,
    get_organization_schema,
    read_claims_without_verifying,
    simulate_rbac_access,
    simulate_wordpress_jwt_bridge,
)

_TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}
_DECISION_TONE = {ALLOW: "ok", ALLOW_SCOPED: "warn", DENY: "crit"}
_DECISION_MARK = {ALLOW: "full", ALLOW_SCOPED: "scoped", DENY: "deny"}


def _kpis(pairs) -> None:
    cells = "".join(
        f'<div class="app-kpi {tone}"><b>{esc(value)}</b>'
        f"<span>{esc(label)}</span></div>"
        for value, label, tone in pairs)
    st.markdown(f'<div class="app-kpis">{cells}</div>',
                unsafe_allow_html=True)


def _finding_card(finding) -> None:
    tone = _TONE[finding.severity]
    st.markdown(
        f'<div class="app-card {tone}">\n'
        f'  <h4><span class="app-tag {tone}">{esc(finding.code)}</span>\n'
        f"  {esc(finding.title)}</h4>\n"
        f"  <p>{esc(finding.detail)}</p>\n"
        f'  <div class="app-ev">Fix: {esc(finding.fix)}</div>\n'
        f"</div>",
        unsafe_allow_html=True)


def _stage(state: str, name: str, detail: str) -> str:
    return (f'<div class="app-stage"><span class="s {state}">'
            f"{esc(state.upper())}</span>"
            f'<span class="n">{esc(name)}</span>'
            f'<span class="d">{esc(detail)}</span></div>')


def render() -> None:
    inject()
    st.markdown(
        '<div class="app-hero">\n'
        "  <h1>Business Admin and HRM Architecture Console</h1>\n"
        "  <p>Three parts of an internal platform, each measured rather\n"
        "  than claimed. The organisation is generated and then walked, so\n"
        "  the absence of a reporting cycle is a result rather than a line\n"
        "  on a diagram. The access matrix is enumerated in full and\n"
        "  defaults to deny, so a resource nobody wrote a rule for is\n"
        "  refused rather than opened. And the tokens are really signed with\n"
        "  HMAC SHA256, so the forgery that renames the algorithm to none\n"
        "  is shown being rejected rather than described.</p>\n"
        "</div>",
        unsafe_allow_html=True)

    org_tab, rbac_tab, jwt_tab = st.tabs(
        ["Organisation schema", "Role access matrix", "WordPress bridge"])

    # -- 1. organisation ---------------------------------------------------
    with org_tab:
        st.markdown(
            "#### A manager column cannot be stopped from forming a cycle\n\n"
            "A foreign key checks that the manager exists. A CHECK "
            "constraint sees one row. Neither can see a path, so A reports "
            "to B reports to A satisfies the whole schema and the first "
            "recursive headcount query never returns.")

        schema = get_organization_schema()
        tone = _TONE[schema.severity]

        _kpis([
            (str(schema.headcount), "Employees", "ok"),
            (str(len(schema.departments)), "Departments", ""),
            (str(schema.max_depth), "Reporting depth", ""),
            (str(len(schema.cycles)), "Cycles found",
             "ok" if schema.acyclic else "crit"),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag '
            f'{"ok" if schema.acyclic else "crit"}">WALKED</span>\n'
            f"  {esc(schema.headline)}</h4>\n"
            f"  <p>{schema.root_count} employee(s) report to nobody, "
            f"{len(schema.orphans)} reference a manager that is not in the "
            f"table, and every chain reaches a root.</p>\n"
            f'  <div class="app-ev">{len(schema.clients)} client(s), '
            f"{len(schema.projects)} project(s), "
            f"{len(schema.assignments)} assignment(s)</div>\n"
            f"</div>",
            unsafe_allow_html=True)

        st.table({
            "Department": list(schema.headcount_by_department),
            "Headcount": list(schema.headcount_by_department.values()),
        })
        st.caption(
            f"The department headcounts sum to "
            f"{sum(schema.headcount_by_department.values())}, which equals "
            f"the {schema.headcount} rows generated. Nothing is counted "
            f"twice and nothing is missing.")

        for table in schema.tables:
            keys = ", ".join(table.primary_key)
            refs = ("; ".join(table.foreign_keys)
                    if table.foreign_keys else "none")
            st.markdown(
                f'<div class="app-card info">\n'
                f"  <h4>{esc(table.name)}</h4>\n"
                f"  <p>{esc(table.purpose)}</p>\n"
                f'  <div class="app-ev">Primary key: {esc(keys)}<br>'
                f"Foreign keys: {esc(refs)}<br>"
                f"Columns: {esc(', '.join(table.columns))}</div>\n"
                f"  <p><b>What no constraint here can stop:</b> "
                f"{esc(table.constraint_gap)}</p>\n"
                f"</div>",
                unsafe_allow_html=True)

        for finding in schema.findings:
            _finding_card(finding)

    # -- 2. rbac -----------------------------------------------------------
    with rbac_tab:
        st.markdown(
            "#### Default deny, and a scoped allow is its own answer\n\n"
            "An allow that is really an allow for some rows, recorded as a "
            "plain allow, is how a project manager who may read their own "
            "clients ends up reading every client. The role check passes "
            "and the row filter lives only in whichever query remembered "
            "it.")

        col_a, col_b, col_c = st.columns(3)
        with col_a:
            role = st.selectbox("Role", ROLES, index=1)
        with col_b:
            resource = st.selectbox("Resource", RESOURCES, index=3)
        with col_c:
            action = st.selectbox("Action", ACTIONS, index=0)

        verdict = simulate_rbac_access(role, resource, action)
        tone = _TONE[verdict.severity]
        decision_tone = _DECISION_TONE[verdict.decision]

        _kpis([
            (verdict.decision, "Decision", decision_tone),
            ("yes" if verdict.allowed else "no", "Allowed",
             "ok" if verdict.allowed else "crit"),
            ("yes" if verdict.scoped else "no", "Needs a row filter",
             "warn" if verdict.scoped else "ok"),
            ("yes" if verdict.known_resource else "no", "Known resource",
             "ok" if verdict.known_resource else "warn"),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag {decision_tone}">'
            f'{esc(verdict.decision)}</span>\n'
            f"  {esc(verdict.headline)}</h4>\n"
            f"</div>",
            unsafe_allow_html=True)

        if verdict.row_predicate:
            st.code(
                f"CREATE POLICY {resource}_scope ON {resource}\n"
                f"  USING ({verdict.row_predicate});",
                language="sql")

        st.markdown("#### Every role against every resource, nothing hidden")
        for one_action in ACTIONS:
            rows = {"Resource": list(RESOURCES)}
            for one_role in ROLES:
                rows[one_role] = [
                    _DECISION_MARK[simulate_rbac_access(
                        one_role, one_resource, one_action).decision]
                    for one_resource in RESOURCES]
            st.markdown(f"**Action: {one_action}**")
            st.table(rows)

        st.caption(
            "A cell reading scoped is an allow that is not safe without its "
            "row predicate attached. A cell reading deny needed no rule to "
            "reach, because the default is deny and a grant has to be "
            "written down to exist.")

        unknown = simulate_rbac_access(role, "table_added_next_quarter",
                                       action)
        st.markdown(
            f'<div class="app-card warn">\n'
            f'  <h4><span class="app-tag warn">FALL THROUGH</span>\n'
            f"  A resource nobody wrote a rule for: "
            f"{esc(unknown.decision)}</h4>\n"
            f"  <p>A matrix that falls through to allow hands every new "
            f"table to every role until somebody remembers to restrict it, "
            f"and nothing fails while that is true.</p>\n"
            f"</div>",
            unsafe_allow_html=True)

        for finding in verdict.findings:
            _finding_card(finding)

    # -- 3. jwt bridge -----------------------------------------------------
    with jwt_tab:
        st.markdown(
            "#### The algorithm is pinned server side, not read from the "
            "token\n\n"
            "A verifier that takes its algorithm from the header of the "
            "token it is checking accepts a token whose header says none "
            "and whose signature is an empty string. Below, the same "
            "payload is forged that way on every run and rejected.")

        col_d, col_e = st.columns(2)
        with col_d:
            wp_user = st.selectbox(
                "WordPress user", list(WP_TO_EMPLOYEE) + [999], index=0,
                format_func=lambda value: (
                    f"{value} (employee {WP_TO_EMPLOYEE[value]})"
                    if value in WP_TO_EMPLOYEE
                    else f"{value} (no employee mapping)"))
        with col_e:
            ttl = st.slider("Token lifetime in seconds", 60, 86400, 900, 60)

        bridge = simulate_wordpress_jwt_bridge(wp_user, ttl_seconds=ttl)
        tone = _TONE[bridge.severity]
        status_tone = "ok" if bridge.status == VERIFIED else "crit"

        _kpis([
            (bridge.status, "Session", status_tone),
            (str(bridge.employee_id or "none"), "Employee",
             "ok" if bridge.employee_id else "crit"),
            ("rejected" if not bridge.forgery_accepted else "accepted",
             "Algorithm none forgery",
             "ok" if not bridge.forgery_accepted else "crit"),
            (str(len(bridge.session_context)), "Session variables set",
             "ok" if bridge.session_context else "crit"),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag {status_tone}">'
            f'{esc(bridge.status)}</span>\n'
            f"  {esc(bridge.headline)}</h4>\n"
            f"  <p>{esc(bridge.reason)}</p>\n"
            f"</div>",
            unsafe_allow_html=True)

        st.markdown("**Issued token**")
        st.code(bridge.token, language="text")

        st.markdown(
            "**What anyone holding it can read, with no secret and no "
            "permission**")
        st.json(read_claims_without_verifying(bridge.token))

        st.markdown("**The same payload, forged with no signature**")
        st.code(bridge.forged_token, language="text")

        rows = "".join([
            _stage("pass" if bridge.verified else "fail",
                   "Signature, expiry, issuer, audience", bridge.reason),
            _stage("pass" if not bridge.forgery_accepted else "fail",
                   "Algorithm none forgery",
                   ("Rejected: the verifier never reads the algorithm out "
                    "of the token it is checking."
                    if not bridge.forgery_accepted
                    else "Accepted, which is the vulnerability.")),
            _stage("pass" if bridge.session_context else "fail",
                   "PostgreSQL session context",
                   ("; ".join(f"SET LOCAL {name} = '{value}'"
                              for name, value in bridge.session_context)
                    if bridge.session_context
                    else "No variable set at all, because a default "
                         "employee identifier is read by every policy on "
                         "the database as a real person.")),
        ])
        st.markdown(f'<div class="app-card">{rows}</div>',
                    unsafe_allow_html=True)

        for finding in bridge.findings:
            _finding_card(finding)

    st.markdown(
        f'<div class="app-foot">Engine {esc(ENGINE_VERSION)}. The '
        f"organisation is generated from a fixed seed and then walked, so "
        f"the headcount, the depth, and the absence of cycles are measured "
        f"on this page rather than asserted. The access matrix defaults to "
        f"deny and an unrecognised resource is refused by name. Tokens are "
        f"signed with {esc(ALGORITHM)} using a demonstration secret that is "
        f"published in the source, because this page proves a verification "
        f"rule and protects nothing.</div>",
        unsafe_allow_html=True)
