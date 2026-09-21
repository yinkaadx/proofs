"""Data Pipeline and Governance Architecture Console.

Rendered inside the hub app. All logic lives in core.py, which has no
Streamlit dependency, so the same engine could sit behind a real gate.

Everything on this page is evaluated live from the controls. There is no
session state to go stale, which removes the class of bug where a card on
screen describes a verdict that two interactions ago stopped being true.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.data_pipeline_governance_console.core import (
    ADMITTED,
    ENGINE_VERSION,
    OWN_TENANT_ROWS,
    ROLE_APP_SERVICE,
    ROLE_TABLE_OWNER,
    ROLE_TENANT_USER,
    ROLES,
    SAMPLE_ROWS,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    TARGET_OTHER,
    TARGETS,
    TOTAL_ROWS,
    compare_architectures,
    get_pipeline_architecture_matrix,
    run_quality_gate_batch,
    sha256_of,
    simulate_data_governance_rls,
    simulate_data_quality_gate,
)

_TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}


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
        "  <h1>Data Pipeline and Governance Architecture Console</h1>\n"
        "  <p>Three parts of a data platform, each holding the property that\n"
        "  decides whether the platform is trustworthy rather than merely\n"
        "  running. A quality gate is worth something only if a rejected row\n"
        "  lands nowhere and only if it dedupes on content rather than on an\n"
        "  identifier the producer can regenerate. An architecture choice is\n"
        "  a trade, and the axis teams forget is how each shape fails. Row\n"
        "  level security is enforced by the database engine, which is why\n"
        "  it survives a compromised application, and also why the ways it\n"
        "  quietly does not hold are engine facts rather than code bugs.</p>\n"
        "</div>",
        unsafe_allow_html=True)

    gate_tab, matrix_tab, rls_tab = st.tabs(
        ["Quality gate", "Architecture trade offs", "Row level security"])

    # -- 1. quality gate ---------------------------------------------------
    with gate_tab:
        st.markdown(
            "#### One row, checked once, told everything that is wrong\n\n"
            "A gate that stops at the first failure turns a backfill into a "
            "queue of single fixes: correct the schema, re run for an hour, "
            "discover the nulls, correct those, re run again. Every reason "
            "is collected before the verdict is returned.")

        left, right = st.columns([3, 2])
        with left:
            payload = st.text_area(
                "Payload content, hashed for you",
                value='{"order":"A-1001","total":42.5}', height=90)
        with right:
            schema_valid = st.toggle("Matches the schema contract",
                                     value=True)
            null_count = st.slider("Nulls in non nullable columns", 0, 5, 0)
            already_seen = st.toggle(
                "This digest already landed once", value=False)

        digest = sha256_of(payload)
        seen = (digest,) if already_seen else ()
        result = simulate_data_quality_gate(
            digest, schema_valid, null_count, seen_hashes=seen)

        _kpis([
            (result.status, "Verdict",
             "ok" if result.admitted else "crit"),
            (str(len(result.reasons)), "Rejection reasons",
             "ok" if result.admitted else "crit"),
            (result.short_hash, "Content digest", ""),
            (str(result.null_count), "Null count",
             "ok" if result.null_count <= result.null_tolerance else "crit"),
        ])

        if result.reasons:
            st.markdown("**Every reason this row was refused**")
            rows = "".join(
                _stage("fail", f"Reason {index}", reason)
                for index, reason in enumerate(result.reasons, start=1))
            st.markdown(f'<div class="app-card crit">{rows}</div>',
                        unsafe_allow_html=True)

        for finding in result.findings:
            _finding_card(finding)

        st.markdown(
            "#### The duplicate an identifier based gate never catches\n\n"
            "Row two below is a producer retry after a network timeout. It "
            "carries the same content and would carry a fresh identifier in "
            "any real system, so a gate keyed on the identifier admits it "
            "and the order is counted twice. Keyed on the SHA256 of the "
            "content, the first copy rejects the second.")

        batch = run_quality_gate_batch(SAMPLE_ROWS)
        _kpis([
            (str(batch.total), "Rows in", ""),
            (str(batch.admitted), "Admitted", "ok"),
            (str(batch.rejected), "Rejected", "crit"),
            (str(len(batch.seen_after)), "Digests in the ledger", ""),
        ])
        lines = []
        for row, outcome in zip(SAMPLE_ROWS, batch.results):
            state = "pass" if outcome.admitted else "fail"
            detail = (outcome.status if outcome.admitted
                      else "; ".join(outcome.reasons))
            lines.append(_stage(state, row["label"], detail))
        st.markdown(f'<div class="app-card">{"".join(lines)}</div>',
                    unsafe_allow_html=True)
        st.caption(
            f"{batch.admitted} admitted plus {batch.rejected} rejected "
            f"equals {batch.total} rows in. Nothing was dropped on the floor "
            f"and nothing was counted twice.")

    # -- 2. architecture matrix -------------------------------------------
    with matrix_tab:
        st.markdown(
            "#### Latency, complexity, cost, and the axis that decides\n\n"
            "Latency and cost are the axes every comparison lists. The one "
            "that decides whether a team can operate what it built is the "
            "failure mode, and it runs the opposite way to latency: the "
            "fastest architecture has the quietest failures.")

        matrix = get_pipeline_architecture_matrix()
        facts = compare_architectures()

        st.table({
            "Architecture": [a.name for a in matrix],
            "Latency": [a.latency for a in matrix],
            "Complexity": [a.complexity for a in matrix],
            "Cost": [a.cost for a in matrix],
        })

        for item in matrix:
            st.markdown(
                f'<div class="app-card info">\n'
                f"  <h4>{esc(item.name)}</h4>\n"
                f"  <p><b>Fails as:</b> {esc(item.fails_as)}</p>\n"
                f"  <p><b>Choose it when:</b> {esc(item.choose_when)}</p>\n"
                f"  <p><b>Avoid it when:</b> {esc(item.avoid_when)}</p>\n"
                f'  <div class="app-ev">Cost shape: '
                f"{esc(item.cost_shape)}</div>\n"
                f"</div>",
                unsafe_allow_html=True)

        ordered = " then ".join(facts["by_latency"])
        st.caption(
            f"Ranked by latency the order is {ordered}, which is the exact "
            f"reverse of the ranking by complexity: "
            f"{facts['latency_order_reverses_complexity_order']}. "
            f"{facts['every_second_of_latency_is_bought_with_complexity']}")

    # -- 3. row level security --------------------------------------------
    with rls_tab:
        st.markdown(
            "#### What the engine does, not what the application intends\n\n"
            "Row level security is worth reaching for precisely because the "
            "engine applies it to every connection, including one opened by "
            "a compromised application. The same fact produces its failure "
            "modes: the exemptions are engine level and cannot be undone "
            "from application code.")

        col_a, col_b = st.columns(2)
        with col_a:
            role = st.selectbox("Connecting role", ROLES,
                                index=ROLES.index(ROLE_TENANT_USER))
            target = st.selectbox("What the query asks for", TARGETS,
                                  index=TARGETS.index(TARGET_OTHER))
        with col_b:
            enabled = st.toggle("ENABLE ROW LEVEL SECURITY has run",
                                value=True)
            forced = st.toggle("FORCE ROW LEVEL SECURITY is set", value=False)

        verdict = simulate_data_governance_rls(role, target, enabled, forced)
        tone = _TONE[verdict.severity]

        _kpis([
            (verdict.enforcement.split(",")[0], "Engine enforcement", tone),
            (str(verdict.rows_visible), "Rows returned",
             "crit" if not verdict.blocked and target == TARGET_OTHER
             else "ok"),
            (str(verdict.rows_requested), "Rows the query asked for", ""),
            (str(verdict.rows_withheld), "Rows withheld", ""),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag {tone}">VERDICT</span>\n'
            f"  {esc(verdict.headline)}</h4>\n"
            f"  <p>{esc(verdict.enforcement)}</p>\n"
            f"</div>",
            unsafe_allow_html=True)

        st.code("\n".join(verdict.sql), language="sql")

        for finding in verdict.findings:
            _finding_card(finding)

        st.markdown(
            "#### Every role against every target, with nothing hidden\n\n"
            "The grid below is generated, not written. A cell that reads "
            f"{TOTAL_ROWS} is a role that can read the whole table while the "
            "policy sits in the catalogue looking like protection.")

        grid_rows = []
        for one_role in ROLES:
            cells = []
            for one_target in TARGETS:
                cell = simulate_data_governance_rls(
                    one_role, one_target, enabled, forced)
                cells.append(cell.rows_visible)
            grid_rows.append(cells)

        st.table({
            "Role": list(ROLES),
            "Own tenant": [r[0] for r in grid_rows],
            "Another tenant": [r[1] for r in grid_rows],
            "Every row": [r[2] for r in grid_rows],
        })
        st.caption(
            f"The table holds {TOTAL_ROWS} rows, {OWN_TENANT_ROWS} of them "
            f"belonging to the connecting tenant. With the policy enforced, "
            f"a cell under Another tenant reads 0 and the engine raises "
            f"nothing, which is why a wrong tenant claim presents as an "
            f"empty dashboard rather than as an error.")

    st.markdown(
        f'<div class="app-foot">Engine {esc(ENGINE_VERSION)}. '
        f"Verdicts are evaluated live from the controls, and the row level "
        f"security behaviour follows PostgreSQL: a superuser and any role "
        f"holding BYPASSRLS are exempt and FORCE does not reach them, while "
        f"the table owner is exempt until FORCE is set. The admitted status "
        f"{esc(ADMITTED)} is never reached with a reason outstanding. "
        f"Roles modelled include {esc(ROLE_TABLE_OWNER)} and "
        f"{esc(ROLE_APP_SERVICE)}, which are the two exemptions that bite in "
        f"practice.</div>",
        unsafe_allow_html=True)
