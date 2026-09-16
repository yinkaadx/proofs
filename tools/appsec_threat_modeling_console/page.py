"""AppSec Threat Modeling & Research Console.

Rendered inside the hub. Every number on this page is computed live from
core.py, which holds no Streamlit import and no session state, so nothing on
screen can describe a selection that was replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.appsec_threat_modeling_console.core import (
    CVSS_VERSION,
    ENGINE_VERSION,
    FINDING_CRIT,
    FINDING_OK,
    FINDING_WARN,
    GUARDRAIL,
    OWASP_EDITION,
    PRIOR_OWASP_EDITION,
    TARGET_LAYERS,
    VULN_CLASSES,
    analyze_threat_vector,
    capability_rows,
    coverage_summary,
    drifted_categories,
    get_security_capability_matrix,
    impact_saturation_rows,
    ranked_portfolio,
    rounding_audit,
    scope_is_monotonic,
    sensitivity_rows,
    translate_to_product_feature,
)

TONE = {FINDING_CRIT: "crit", FINDING_WARN: "warn", FINDING_OK: "ok"}

VULN_BY_LABEL = {item.name: item for item in VULN_CLASSES}
LAYER_BY_LABEL = {item.name: item for item in TARGET_LAYERS}

COVERAGE_TONE = {"Covered": "ok", "Partial": "warn", "Not covered": "crit"}

DISCLAIMER = (
    "Nothing here scans, probes or reaches a live system. The vulnerability "
    "classes are worked examples, the scores are computed from the published "
    f"{CVSS_VERSION} equations, and the control mappings are a reading of the "
    "frameworks rather than an audit opinion."
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
  <h1>AppSec Threat Modeling &amp; Research Console</h1>
  <p>A severity score is an argument with numbers in it, and most of the
  argument happens over metrics that cannot change the answer. This console
  scores a vulnerability class at the layer it actually sits at, separates how
  bad it is from how likely it is to be exploited, and turns the result into
  stories a delivery team can take without a translator in the room.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Threat vector**: pick a class, then move it between layers "
            "and watch the score and the likelihood part company.\n"
            "2. **Product translation**: take the same finding into stories "
            "and acceptance criteria.\n"
            "3. **Capability matrix**: see which rows a backlog tagged "
            "against the 2021 list cannot be migrated into.\n"
            "4. **Scoring engine**: the measured properties of the equations, "
            "including one claim that was tested and dropped."
        )
        st.divider()
        st.caption(DISCLAIMER)
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_threat, tab_product, tab_matrix, tab_engine = st.tabs(
        ["Threat Vector", "Product Translation", "Capability Matrix",
         "Scoring Engine"])

    # -----------------------------------------------------------------
    # The same class at a different layer is a different severity
    # -----------------------------------------------------------------
    with tab_threat:
        c1, c2 = st.columns([3, 2])
        vuln_label = c1.selectbox("Vulnerability class", list(VULN_BY_LABEL),
                                  key="atm_vuln")
        layer_label = c2.selectbox("Where it sits", list(LAYER_BY_LABEL),
                                   index=1, key="atm_layer")

        analysis = analyze_threat_vector(VULN_BY_LABEL[vuln_label].key,
                                         LAYER_BY_LABEL[layer_label].key)
        breakdown = analysis.breakdown
        band_tone = ("crit" if analysis.band in ("Critical", "High")
                     else "warn" if analysis.band == "Medium" else "ok")
        likely_tone = ("crit" if analysis.exploit_likelihood >= 70
                       else "warn" if analysis.exploit_likelihood >= 40
                       else "ok")
        delta = breakdown.score - analysis.baseline.score

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {band_tone}"><div class="n">{breakdown.score:.1f}</div>
    <div class="l">Base score, {esc(analysis.band)}</div></div>
  <div class="app-kpi {likely_tone}">
    <div class="n">{analysis.exploit_likelihood}</div>
    <div class="l">Exploit likelihood, {esc(analysis.likelihood_band)}</div></div>
  <div class="app-kpi"><div class="n">{delta:+.1f}</div>
    <div class="l">Moved by the layer</div></div>
  <div class="app-kpi"><div class="n">{esc(analysis.vuln.cwe)}</div>
    <div class="l">{esc(analysis.vuln.owasp_2025)}</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.code(breakdown.vector, language="text")
        st.caption(esc(analysis.layer.note))

        st.markdown("##### How the attack actually works")
        st.markdown(
            f"""
<div class="app-card">
  <p>{esc(analysis.vuln.mechanics)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        for finding in analysis.findings:
            _finding_card(finding)

        st.markdown("##### Every metric and the weight it carried")
        st.dataframe(breakdown.rows(), width="stretch", hide_index=True)

        st.markdown("##### The arithmetic, not just the answer")
        st.dataframe(breakdown.arithmetic_rows(), width="stretch",
                     hide_index=True)

        st.markdown("##### The whole backlog at this layer, ordered twice")
        st.caption(
            "Left column is the order a team would work in. Right column is "
            "the order a base score sort produces. Where the two disagree, "
            "the sort is wrong about what gets attacked first."
        )
        st.dataframe(ranked_portfolio(analysis.layer.key), width="stretch",
                     hide_index=True)

    # -----------------------------------------------------------------
    # From a scored finding to something a delivery team can take
    # -----------------------------------------------------------------
    with tab_product:
        translation = translate_to_product_feature(analysis)
        st.markdown(f"#### {esc(translation.headline)}")
        priority_tone = ("crit" if translation.priority == "This sprint"
                         else "warn" if translation.priority == "Next sprint"
                         else "ok")
        st.markdown(
            f"""
<div class="app-card {priority_tone}">
  <h4><span class="app-tag {priority_tone}">Priority</span>
  {esc(translation.priority)}</h4>
  <p>Priority comes from severity and likelihood together. One high number on
  its own moves the item to the next sprint, not into this one.</p>
  <div class="app-ev">{esc(translation.guardrail)}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### Engineering user stories")
        st.dataframe(translation.story_rows(), width="stretch",
                     hide_index=True)

        st.markdown("##### Mitigation acceptance criteria")
        st.caption(
            "These are what carry the claim that the vulnerability is gone. "
            "A control mapping does not."
        )
        st.dataframe(translation.criteria_rows(), width="stretch",
                     hide_index=True)

        st.markdown("##### Compliance mapping")
        st.dataframe(translation.control_rows(), width="stretch",
                     hide_index=True)

    # -----------------------------------------------------------------
    # The matrix, and what the edition change broke
    # -----------------------------------------------------------------
    with tab_matrix:
        summary = coverage_summary()
        drifted = drifted_categories()
        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{summary['Covered']}</div>
    <div class="l">Rows covered</div></div>
  <div class="app-kpi warn"><div class="n">{summary['Partial']}</div>
    <div class="l">Rows partially covered</div></div>
  <div class="app-kpi crit"><div class="n">{summary['Not covered']}</div>
    <div class="l">Rows not covered</div></div>
  <div class="app-kpi crit"><div class="n">{len(drifted)}</div>
    <div class="l">Rows a 2021 tag cannot migrate into</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.caption(
            f"Benchmarked against {OWASP_EDITION}, with the "
            f"{PRIOR_OWASP_EDITION} category each row came from held beside "
            f"it. The two editions are not a renumbering of each other."
        )
        st.dataframe(capability_rows(), width="stretch", hide_index=True)

        st.markdown("##### The rows where a mechanical migration fails")
        for row in drifted:
            st.markdown(
                f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">{esc(row.drift)}</span>
  {esc(row.category_2025)}</h4>
  <p>{esc(row.note)}</p>
  <div class="app-ev">Was {esc(row.category_2021)}. Defensive feature:
  {esc(row.feature)}.</div>
</div>
""",
                unsafe_allow_html=True,
            )

    # -----------------------------------------------------------------
    # What the equations actually do, measured
    # -----------------------------------------------------------------
    with tab_engine:
        st.markdown("##### Which metric is worth arguing about")
        st.caption(
            "Every base vector, every single metric substitution, counted. A "
            "review that spends its time on User Interaction is arguing over "
            "the metric least able to change the answer."
        )
        st.dataframe(sensitivity_rows(), width="stretch", hide_index=True)

        st.markdown("##### Impact does not add up")
        st.caption(
            "The impact sub score saturates, so three wrecked impact metrics "
            "are worth far less than three times one."
        )
        st.dataframe(impact_saturation_rows(), width="stretch",
                     hide_index=True)

        checked, lowered = scope_is_monotonic()
        audit = rounding_audit()
        tone = "ok" if audit.clean else "crit"
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">Tested and dropped</span>
  The float rounding scare</h4>
  <p>The specification replaced a naive ceiling with integer arithmetic in
  Appendix A, so it was reasonable to expect a float implementation to
  misreport some base vectors. {esc(audit.verdict)}</p>
  <div class="app-ev">Checked {audit.checked} vectors, {audit.divergent}
  disagreed. Scope monotonicity checked over {checked} pairs, {lowered}
  lowered the score.</div>
</div>
""",
            unsafe_allow_html=True,
        )
        if audit.rows():
            st.dataframe(audit.rows(), width="stretch", hide_index=True)

        st.markdown(
            f"""
<div class="app-foot">{esc(DISCLAIMER)} Capability matrix rows:
{len(get_security_capability_matrix())}. Engine version {ENGINE_VERSION}.</div>
""",
            unsafe_allow_html=True,
        )
