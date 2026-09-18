"""IPB V5 Upgrade Diagnostic Console.

Rendered inside the hub app. Every block on this page is generated from the
controls on each run, so a query or a declaration on screen can never
describe an input that was replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.ipb_v5_upgrade_diagnostic.core import (
    ENGINE_VERSION,
    HOOK_LIBRARY,
    LEGACY_LIBRARY,
    PHP_FLOOR_LABEL,
    PHP_SUPPORTED_CEILING_LABEL,
    RECOVERY_BLOCKED,
    SAMPLE_PLUGINS,
    SAMPLE_TABLES,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    TOKEN_PREFIX,
    map_theme_variables,
    scan_plugin_compatibility,
    simulate_database_recovery,
)

TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}
VERDICT_TONE = {
    "REWRITE REQUIRED": "crit",
    "PORTABLE TO AN APPLICATION": "warn",
    "NO BLOCKERS FOUND": "ok",
    "NOT SCANNED": "warn",
}


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
  <h1>IPB V5 Upgrade Diagnostic Console</h1>
  <p>Invision Community 5 is not a point release. The plugin system is gone,
  so a plugin is rewritten as an application rather than upgraded. Themes stop
  carrying hardcoded values, so every colour typed into a template has to
  become a custom property. And Pages keeps each database in its own table
  with column names assigned per site, so a recovery query written from a
  table name alone is a guess made against production.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Plugins**: scan a sample, then type a name it does not "
            "know and read what it says instead of passing it.\n"
            "2. **Theme**: every declaration carries the old value as the "
            "fallback, so a wrong token name cannot blank the element.\n"
            "3. **Records**: give it a table name that is not an identifier "
            "and it emits no SQL at all."
        )
        st.divider()
        st.caption(
            "Nothing here touches a forum. No site is contacted, no theme is "
            "read and no query is run anywhere."
        )
        st.caption(
            "The container that built this has no route to the vendor "
            "documentation, so version floors and token names are marked as "
            "what this diagnostic applies rather than quoted as fact. Both "
            "have a confirm step attached."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_plugin, tab_theme, tab_db = st.tabs(
        ["Plugin Compatibility", "Theme Variables", "Record Recovery"])

    # -----------------------------------------------------------------
    with tab_plugin:
        st.subheader("What stops working, and what replaces it")
        left, right = st.columns(2)
        with left:
            plugin_name = st.selectbox("Plugin", list(SAMPLE_PLUGINS))
            custom_name = st.text_input(
                "Or type a plugin this diagnostic has never read", value="")
        with right:
            php_version = st.selectbox(
                "PHP on the host",
                ["7.4", "8.0", "8.1", "8.2", "8.3"], index=3)
            st.caption(
                f"This diagnostic applies a floor of PHP {PHP_FLOOR_LABEL} "
                f"and treats {PHP_SUPPORTED_CEILING_LABEL} as the tested "
                "ceiling. Confirm both in the admin panel system information "
                "screen before booking a window."
            )

        chosen = custom_name.strip() or plugin_name
        scan = scan_plugin_compatibility(chosen, php_version)

        _kpis([
            ("Verdict", scan.verdict),
            ("Constructs found", str(scan.total_constructs)),
            ("Need a rewrite", str(scan.rewrite_count)),
            ("Just move", str(scan.port_count)),
            ("PHP floor", "cleared" if scan.php_meets_floor else "below"),
        ])
        st.caption(
            f"{scan.rewrite_count} rewrite plus {scan.port_count} port equals "
            f"{scan.total_constructs} constructs, which is everything found."
        )

        tone = VERDICT_TONE.get(scan.verdict, "warn")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(scan.verdict)}</span>
  {esc(scan.plugin_name)} on PHP {esc(scan.php_version)}</h4>
  <p>{esc(scan.headline)}.</p>
  <div class="app-ev">Every IPS4 plugin becomes an application in 5, whatever
  this verdict says. The verdict is about how much of the inside changes.</div>
</div>
""",
            unsafe_allow_html=True,
        )

        if scan.deprecated_hooks:
            st.markdown("**Hook by hook**")
            for hook in scan.deprecated_hooks:
                hook_tone = "crit" if hook.effort == "Rewrite" else "warn"
                st.markdown(
                    f"""
<div class="app-card {hook_tone}">
  <h4><span class="app-tag {hook_tone}">{esc(hook.effort)}</span>
  {esc(hook.legacy_hook)}</h4>
  <p>{esc(hook.detail)}</p>
  <div class="app-ev">Replaced by {esc(hook.extension_point)}</div>
</div>
""",
                    unsafe_allow_html=True,
                )

        st.markdown("**Findings**")
        for finding in scan.findings:
            _finding_card(finding)

        st.subheader("The full construct library")
        for hook in HOOK_LIBRARY:
            st.markdown(
                f"- **{hook.legacy_hook}** ({hook.kind}, {hook.effort}) "
                f"becomes `{hook.extension_point}`")

    # -----------------------------------------------------------------
    with tab_theme:
        st.subheader("Hardcoded value to custom property")
        left, right = st.columns(2)
        with left:
            element = st.selectbox("Legacy declaration", list(LEGACY_LIBRARY))
        with right:
            custom_element = st.text_input(
                "Or type a declaration from your own theme", value="")

        target = custom_element.strip() or element
        mapping = map_theme_variables(target)

        _kpis([
            ("Kind", mapping.kind),
            ("In the library", "yes" if mapping.known else "no"),
            ("Old value", mapping.legacy_value),
            ("Fallback", "yes" if mapping.has_fallback else "no"),
        ])

        st.markdown("**Was**")
        st.code(f"{mapping.property_name}: {mapping.legacy_value};",
                language="css")
        st.markdown("**Becomes**")
        st.code(mapping.declaration, language="css")
        st.caption(
            f"The token follows the {TOKEN_PREFIX} convention this mapper "
            "uses. The fallback is what makes that safe: if the token does "
            "not exist in your installed theme, the browser falls back to the "
            "old value and the element renders exactly as it did in 4."
        )

        st.markdown("**Findings**")
        for finding in mapping.findings:
            _finding_card(finding)

        st.subheader("The whole library at once")
        for legacy in LEGACY_LIBRARY:
            row = map_theme_variables(legacy)
            st.markdown(f"- `{legacy}` becomes `{row.declaration}`")

    # -----------------------------------------------------------------
    with tab_db:
        st.subheader("Lift the records out without touching the originals")
        table_name = st.selectbox("Pages table", list(SAMPLE_TABLES))
        custom_table = st.text_input(
            "Or type the table name from your own database", value="")

        target_table = custom_table.strip() or table_name
        plan = simulate_database_recovery(target_table)

        _kpis([
            ("Status", plan.status),
            ("Database id", plan.database_id or "not a Pages table"),
            ("Steps", str(len(plan.queries))),
            ("Writes to the source", "none"),
        ])

        if plan.status == RECOVERY_BLOCKED:
            st.markdown(
                """
<div class="app-card crit">
  <h4><span class="app-tag crit">BLOCKED</span> No SQL was generated</h4>
  <p>That table name is not a MySQL identifier, so any query built around it
  would be a query nobody should paste into a production shell.</p>
  <div class="app-ev">Fix the name and the sequence appears.</div>
</div>
""",
                unsafe_allow_html=True,
            )
        else:
            for title, query in plan.queries:
                st.markdown(f"**{title}**")
                st.code(query, language="sql")

        st.markdown("**Before you run any of it**")
        for finding in plan.findings:
            _finding_card(finding)
