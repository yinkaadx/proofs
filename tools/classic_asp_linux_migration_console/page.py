"""Classic ASP to Linux Migration Console.

Rendered inside the hub app. All logic lives in core.py, which has no
Streamlit dependency and really parses the VBScript it is handed.

Everything on this page is evaluated live from the controls. There is no
session state to go stale, which removes the class of bug where a card on
screen describes a conversion that two interactions ago stopped being
true.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.classic_asp_linux_migration_console.core import (
    BLOCKED,
    CONVERTED,
    ENGINE_VERSION,
    LEGACY_TABLES,
    REDIRECT_QUERY,
    REQUEST_SEARCH_ORDER,
    SAMPLE_ASP_IDENTIFIER,
    SAMPLE_ASP_INJECTION,
    SAMPLE_URLS,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    SLOT_IDENTIFIER,
    convert_vbscript_query,
    generate_seo_301_mapping,
    map_database_schema,
)

_TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}

_SNIPPETS = {
    "Concatenated values, injectable": SAMPLE_ASP_INJECTION,
    "Sort column taken from the query string": SAMPLE_ASP_IDENTIFIER,
}


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
        "  <h1>Classic ASP to Linux Migration Console</h1>\n"
        "  <p>Three stages of moving a Windows hosted Classic ASP site onto\n"
        "  Linux, each built around a break that the move itself creates.\n"
        "  Parameter binding covers values and never identifiers, so a\n"
        "  converter that wraps a column name in a placeholder ships code\n"
        "  that fails on its first run. IIS matched URLs and table names\n"
        "  without regard to case and Linux does not. And an Nginx location\n"
        "  block never sees the query string, so the obvious redirect for a\n"
        "  page identified by its arguments matches nothing at all and the\n"
        "  server starts without complaint.</p>\n"
        "</div>",
        unsafe_allow_html=True)

    sql_tab, schema_tab, seo_tab = st.tabs(
        ["VBScript to prepared statement", "Schema mapping",
         "301 redirect engine"])

    # -- 1. vbscript conversion -------------------------------------------
    with sql_tab:
        st.markdown(
            "#### A placeholder cannot stand where a column name goes\n\n"
            "The conversion stops when an identifier is interpolated. "
            "Emitting a placeholder there produces code that fails "
            "immediately, and the fix a developer reaches for under "
            "pressure is taking the placeholder back out again.")

        choice = st.selectbox("Legacy snippet", list(_SNIPPETS), index=0)
        legacy = st.text_area("Classic ASP", value=_SNIPPETS[choice],
                              height=140)

        conversion = convert_vbscript_query(legacy)
        tone = _TONE[conversion.severity]
        status_tone = "ok" if conversion.converted else "crit"

        _kpis([
            (conversion.status, "Status", status_tone),
            (str(len(conversion.value_slots)), "Values bound", "ok"),
            (str(len(conversion.identifier_slots)), "Identifiers refused",
             "crit" if conversion.identifier_slots else "ok"),
            (str(len(conversion.slots)), "Concatenation points", ""),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag {status_tone}">'
            f'{esc(conversion.status)}</span>\n'
            f"  {esc(conversion.headline)}</h4>\n"
            f'  <div class="app-ev">'
            f"{esc(conversion.sql_template or 'nothing parsed')}</div>\n"
            f"</div>",
            unsafe_allow_html=True)

        if conversion.slots:
            rows = "".join(
                _stage("fail" if slot.slot_kind == SLOT_IDENTIFIER
                       else "pass",
                       f"{slot.expression}",
                       f"{slot.slot_kind} slot, read from {slot.source}"
                       f" as {slot.key!r}. Follows "
                       f"{slot.preceding_sql.strip()!r}.")
                for slot in conversion.slots)
            st.markdown(f'<div class="app-card">{rows}</div>',
                        unsafe_allow_html=True)

        before_tab, php_tab, python_tab = st.tabs(
            ["Before", "PHP PDO", "Python psycopg"])
        with before_tab:
            st.code(legacy, language="vbscript")
        with php_tab:
            st.code(conversion.php_pdo or "(nothing emitted)",
                    language="php")
        with python_tab:
            st.code(conversion.python_psycopg or "(nothing emitted)",
                    language="python")

        st.caption(
            f"A bare Request() call resolves against "
            f"{', '.join(REQUEST_SEARCH_ORDER)}, in that order, so a value "
            f"the page expected from the query string can arrive in a "
            f"cookie and the cookie wins over the form post.")

        for finding in conversion.findings:
            _finding_card(finding)

    # -- 2. schema mapping -------------------------------------------------
    with schema_tab:
        st.markdown(
            "#### The type mapping is the easy half\n\n"
            "The half that breaks the migration is that Access and SQL "
            "Server matched text without regard to case and the Linux "
            "targets do not, so a sign in that worked on the old box now "
            "fails as a wrong password rather than as an error.")

        table = st.selectbox("Legacy table", LEGACY_TABLES, index=0)
        schema = map_database_schema(table)
        tone = _TONE[schema.severity]

        _kpis([
            (schema.new_table_name, "New table", "ok"),
            (str(len(schema.columns)), "Columns mapped", ""),
            (str(sum(1 for c in schema.columns if c.nullable)), "Nullable",
             ""),
            (str(len(schema.findings)), "Migration traps named", "warn"),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag ok">MAPPED</span>\n'
            f"  {esc(schema.headline)}</h4>\n"
            f"</div>",
            unsafe_allow_html=True)

        st.table({
            "Legacy column": [c.legacy_name for c in schema.columns],
            "Legacy type": [c.legacy_type for c in schema.columns],
            "New column": [c.new_name for c in schema.columns],
            "MySQL": [c.mysql_type for c in schema.columns],
            "PostgreSQL": [c.postgres_type for c in schema.columns],
        })

        mysql_tab, pg_tab = st.tabs(["MySQL 8", "PostgreSQL 16"])
        with mysql_tab:
            st.code(schema.mysql_ddl, language="sql")
        with pg_tab:
            st.code(schema.postgres_ddl, language="sql")

        rows = "".join(
            _stage("skip", c.new_name, c.note) for c in schema.columns)
        st.markdown(f'<div class="app-card">{rows}</div>',
                    unsafe_allow_html=True)

        for finding in schema.findings:
            _finding_card(finding)

    # -- 3. redirects ------------------------------------------------------
    with seo_tab:
        st.markdown(
            "#### A location block never sees the query string\n\n"
            "A rule written as a location on an ASP page whose identity "
            "lives in its arguments matches nothing. The configuration "
            "loads, the server starts, and every one of those URLs serves "
            "a 404 with a redirect rule in the file that looks like it "
            "covers them.")

        preset = st.selectbox("Legacy URL", SAMPLE_URLS, index=0)
        legacy_url = st.text_input("As it appears in the access logs",
                                   value=preset)

        try:
            mapping = generate_seo_301_mapping(legacy_url)
        except ValueError as error:
            st.markdown(
                f'<div class="app-card crit">\n'
                f'  <h4><span class="app-tag crit">REFUSED</span>\n'
                f"  {esc(str(error))}</h4>\n"
                f"</div>",
                unsafe_allow_html=True)
        else:
            tone = _TONE[mapping.severity]

            _kpis([
                (mapping.new_path, "New path", "ok"),
                (mapping.strategy, "Strategy",
                 "crit" if mapping.uses_query_string else "ok"),
                (str(len(mapping.query_pairs)), "Query arguments", ""),
                ("301", "Status code", "ok"),
            ])

            left, right = st.columns(2)
            with left:
                st.markdown(
                    f'<div class="app-card crit">\n'
                    f'  <h4><span class="app-tag crit">BEFORE</span>\n'
                    f"  {esc(mapping.legacy_asp_url)}</h4>\n"
                    f"  <p>Served by IIS at every capitalisation, and "
                    f"identified by its query string.</p>\n"
                    f"</div>",
                    unsafe_allow_html=True)
            with right:
                st.markdown(
                    f'<div class="app-card ok">\n'
                    f'  <h4><span class="app-tag ok">AFTER</span>\n'
                    f"  {esc(mapping.new_path)}</h4>\n"
                    f"  <p>One canonical path, reached in a single "
                    f"response.</p>\n"
                    f"</div>",
                    unsafe_allow_html=True)

            st.markdown(
                f'<div class="app-card {tone}">\n'
                f'  <h4><span class="app-tag {tone}">301</span>\n'
                f"  {esc(mapping.headline)}</h4>\n"
                f"</div>",
                unsafe_allow_html=True)

            exact_tab, case_tab = st.tabs(
                ["Primary rule", "Case insensitive fallback"])
            with exact_tab:
                st.code(mapping.nginx_rule, language="nginx")
            with case_tab:
                st.code(mapping.case_insensitive_rule, language="nginx")

            st.markdown("#### Every sample URL through the same engine")
            rows = []
            for url in SAMPLE_URLS:
                outcome = generate_seo_301_mapping(url)
                state = "warn" if outcome.uses_query_string else "pass"
                rows.append(_stage(
                    state, url,
                    f"{outcome.new_path}. {outcome.strategy}."))
            st.markdown(f'<div class="app-card">{"".join(rows)}</div>',
                        unsafe_allow_html=True)
            st.caption(
                f"A URL whose strategy reads {REDIRECT_QUERY} cannot be "
                f"redirected with a plain location block, whatever the "
                f"rule looks like.")

            for finding in mapping.findings:
                _finding_card(finding)

    st.markdown(
        f'<div class="app-foot">Engine {esc(ENGINE_VERSION)}. The VBScript '
        f"is parsed rather than pattern replaced, so the quotes inside a "
        f"Request call are treated as part of that call and not as SQL. A "
        f"conversion reaches {esc(CONVERTED)} only when every "
        f"concatenation point is a value, and reaches {esc(BLOCKED)} with "
        f"no prepared statement emitted the moment one of them is an "
        f"identifier.</div>",
        unsafe_allow_html=True)
