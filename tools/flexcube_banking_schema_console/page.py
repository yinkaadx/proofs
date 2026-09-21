"""Core Banking Schema and Reporting Console.

Rendered inside the hub app. All logic lives in core.py, which has no
Streamlit dependency and keeps every monetary amount in Decimal, so the
same engine could run against a real extract without changing a line.

Everything on this page is evaluated live from the controls. There is no
session state to go stale, which removes the class of bug where a card on
screen describes a reconciliation that two interactions ago stopped being
true.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.flexcube_banking_schema_console.core import (
    BLOCKED,
    ENGINE_VERSION,
    MODULES,
    READY,
    REPORT_TYPES,
    ROLE_DERIVED,
    ROLE_MOVEMENT,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    STANDARDS,
    extract_regulatory_audit,
    map_core_banking_schema,
    simulate_gl_reconciliation,
)

_TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}
_ROLE_TONE = {ROLE_MOVEMENT: "pass", ROLE_DERIVED: "fail"}


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
        "  <h1>Core Banking Schema and Reporting Console</h1>\n"
        "  <p>Three stages of a core banking reporting chain, each holding\n"
        "  the property that decides whether the numbers can be defended. A\n"
        "  schema map is only useful if it says which columns are the record\n"
        "  and which are a cache over the movement log. A reconciliation\n"
        "  that leads with net variance hides offsetting errors, so gross is\n"
        "  reported beside it. And a regulatory figure that cannot be walked\n"
        "  back to a table and a column cannot be defended, so an extract\n"
        "  missing lineage on any field is refused whole. No monetary amount\n"
        "  anywhere in the engine is a floating point number.</p>\n"
        "</div>",
        unsafe_allow_html=True)

    schema_tab, recon_tab, reg_tab = st.tabs(
        ["Schema map", "GL reconciliation", "Regulatory extract"])

    # -- 1. schema map -----------------------------------------------------
    with schema_tab:
        st.markdown(
            "#### Which columns are the record, and which are a cache\n\n"
            "A reporting query written against a balance column returns a "
            "number instantly and returns a wrong one whenever the cache "
            "has drifted, and nothing in the result set says which "
            "happened.")

        module = st.selectbox("Module", MODULES, index=0)
        schema = map_core_banking_schema(module)
        tone = _TONE[schema.severity]

        _kpis([
            (str(len(schema.tables)), "Tables", ""),
            (str(len(schema.movement_tables)), "Append only", "ok"),
            (str(len(schema.cache_tables)), "Caches", "crit"),
            (module, "Module", ""),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag {tone}">SCHEMA</span>\n'
            f"  {esc(schema.headline)}</h4>\n"
            f"</div>",
            unsafe_allow_html=True)

        for table in schema.tables:
            state = _ROLE_TONE.get(table.role, "skip")
            rows = "".join(
                _stage("skip", column.name,
                       f"{column.sql_type}. {column.note}")
                for column in table.columns)
            keys = ", ".join(table.primary_key)
            refs = ("; ".join(table.foreign_keys)
                    if table.foreign_keys else "none")
            st.markdown(
                f'<div class="app-card">\n'
                f'  <h4><span class="app-tag '
                f'{"ok" if table.is_record_of_truth else "crit"}">'
                f"{esc(table.role)}</span>\n"
                f"  {esc(table.name)}</h4>\n"
                f"  <p>{esc(table.purpose)}</p>\n"
                f'  <div class="app-ev">Primary key: {esc(keys)}<br>'
                f"Foreign keys: {esc(refs)}</div>\n"
                f"  {rows}\n"
                f"</div>",
                unsafe_allow_html=True)
            del state

        for finding in schema.findings:
            _finding_card(finding)

    # -- 2. reconciliation -------------------------------------------------
    with recon_tab:
        st.markdown(
            "#### A ledger that ties is not a ledger that is right\n\n"
            "Every posting is a balanced pair, including the ones that "
            "landed in suspense, so debits equal credits whatever the "
            "exceptions are. The number that decides whether the day is "
            "clean is the gross variance, not the net one.")

        left, right = st.columns(2)
        with left:
            volume = st.slider("Transaction volume", 0, 100000, 25000, 500)
        with right:
            rate = st.slider("Discrepancy rate", 0.0, 0.20, 0.04, 0.005)

        recon = simulate_gl_reconciliation(volume, rate)
        tone = _TONE[recon.severity]

        _kpis([
            (f"{recon.matched:,}", "Matched", "ok"),
            (f"{recon.exceptions:,}", "Exceptions",
             "crit" if recon.exceptions else "ok"),
            (f"{recon.net_variance:+,.2f}", "Net variance", ""),
            (f"{recon.gross_variance:,.2f}", "Gross variance",
             "crit" if recon.netting_hides_errors else "warn"),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag {tone}">RECONCILIATION</span>\n'
            f"  {esc(recon.headline)}</h4>\n"
            f"  <p>Debits {recon.total_debits:,.2f} against credits "
            f"{recon.total_credits:,.2f}. The ledger ties: "
            f"{'yes' if recon.balanced else 'no'}.</p>\n"
            f'  <div class="app-ev">Suspense holds '
            f"{recon.suspense_count} item(s) at "
            f"{recon.suspense_balance:+,.2f}</div>\n"
            f"</div>",
            unsafe_allow_html=True)

        if recon.buckets:
            st.table({
                "Exception kind": [b.kind for b in recon.buckets],
                "Count": [b.count for b in recon.buckets],
                "Net": [f"{b.net_amount:+,.2f}" for b in recon.buckets],
                "Gross": [f"{b.gross_amount:,.2f}" for b in recon.buckets],
            })
            st.caption(
                f"{recon.matched:,} matched plus {recon.exceptions:,} "
                f"exceptions equals {recon.transaction_volume:,} "
                f"transactions in. Every amount in this table is a Decimal, "
                f"exact to the penny.")

        for finding in recon.findings:
            _finding_card(finding)

    # -- 3. regulatory extract --------------------------------------------
    with reg_tab:
        st.markdown(
            "#### A number you cannot trace is a number you cannot defend\n\n"
            "A regulator does not ask whether a figure is right. It asks "
            "where the figure came from. An extract with a field that has "
            "no source table and column is refused whole rather than filed "
            "with a gap, because a pack with a gap still looks complete to "
            "the person signing it.")

        col_a, col_b = st.columns(2)
        with col_a:
            report = st.selectbox("Report", REPORT_TYPES, index=0)
        with col_b:
            standard = st.selectbox("Standard", STANDARDS, index=0)

        extract = extract_regulatory_audit(report, standard)
        tone = _TONE[extract.severity]
        status_tone = "ok" if extract.ready else "crit"

        _kpis([
            (extract.status, "Status", status_tone),
            (str(len(extract.fields)), "Fields", ""),
            (str(len(extract.missing_lineage)), "Missing lineage",
             "crit" if extract.missing_lineage else "ok"),
            (str(len(extract.thresholds)), "Thresholds shipped",
             "ok" if extract.thresholds else "crit"),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag {status_tone}">'
            f'{esc(extract.status)}</span>\n'
            f"  {esc(extract.headline)}</h4>\n"
            f"</div>",
            unsafe_allow_html=True)

        rows = []
        for one in extract.fields:
            state = "pass" if one.has_lineage else "fail"
            source = (f"{one.source_table}.{one.source_column}"
                      if one.has_lineage else "no source table or column")
            rows.append(_stage(state, one.name, f"{source}. {one.rule}"))
        st.markdown(f'<div class="app-card">{"".join(rows)}</div>',
                    unsafe_allow_html=True)

        if extract.thresholds:
            st.table({
                "Measure": [t[0] for t in extract.thresholds],
                "Framework minimum": [t[1] for t in extract.thresholds],
                "Note": [t[2] for t in extract.thresholds],
            })

        for finding in extract.findings:
            _finding_card(finding)

    st.markdown(
        f'<div class="app-foot">Engine {esc(ENGINE_VERSION)}. Table and '
        f"column names here are representative of the shape core banking "
        f"schemas share and are not a transcription of any vendor catalogue, "
        f"so read the real names from the installed database before writing "
        f"a migration against them. An extract reaches {esc(READY)} only "
        f"when every field traces to a table and a column, and reaches "
        f"{esc(BLOCKED)} otherwise. Local central bank thresholds are not "
        f"shipped, because they are the binding ones and guessing at them "
        f"would be worse than leaving them empty.</div>",
        unsafe_allow_html=True)
