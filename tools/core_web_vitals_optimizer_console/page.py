"""Core Web Vitals & Speed Optimizer Console.

Rendered inside the hub. All logic lives in core.py, which holds no Streamlit
import. Nothing is kept in session state, so every card is evaluated from the
controls as they stand.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.core_web_vitals_optimizer_console.core import (
    AFTER,
    AFTER_CARELESS_FONT,
    BEFORE,
    ENGINE_VERSION,
    FIELD_PERCENTILE,
    FIELD_WINDOW_DAYS,
    GOOD,
    LIGHTHOUSE_PROFILE,
    NEEDS_IMPROVEMENT,
    POOR,
    SEVERITY_CRIT,
    SEVERITY_OK,
    SEVERITY_WARN,
    evaluate_web_vitals,
    get_wordpress_remediation_steps,
    metric_score_rows,
    remediation_rows,
    score_sensitivity,
    simulate_score_jump,
    steps_that_move,
    threshold_rows,
)

TONE = {SEVERITY_CRIT: "crit", SEVERITY_WARN: "warn", SEVERITY_OK: "ok"}
STATUS_TONE = {GOOD: "ok", NEEDS_IMPROVEMENT: "warn", POOR: "crit"}

DISCLAIMER = (
    "This console measures nothing live. The scoring curve, its constants and "
    "every metric weight were read from the Lighthouse source, and the before "
    "and after pages are a worked example whose scores are computed from their "
    "metrics rather than chosen."
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


def _score_gauge(label: str, score: int) -> str:
    tone = "crit" if score < 50 else "warn" if score < 90 else "ok"
    return (f'<div class="app-kpi {tone}"><div class="n">{score}</div>'
            f'<div class="l">{esc(label)}</div></div>')


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>Core Web Vitals &amp; Speed Optimizer Console</h1>
  <p>A Lighthouse score and a Core Web Vitals pass are two different answers to
  two different questions, and the gap between them is where speed work gets
  sold badly. Lighthouse gives Interaction to Next Paint a weight of zero, so a
  page can score 98 and still fail the assessment on the one vital that
  measures whether the site feels responsive. This console keeps the two
  numbers apart and says when they disagree.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Diagnostic**: move the four sliders and watch the pass flip "
            "on one metric alone.\n"
            "2. Put INP above 200 and see a score that cannot notice.\n"
            "3. **Before and after**: the 42 to 98 jump, and why it is not a "
            "pass.\n"
            "4. **Remediation**: ordered by what each fix moves, including the "
            "ones that move nothing that counts."
        )
        st.divider()
        st.caption(DISCLAIMER)
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_diag, tab_jump, tab_fix, tab_scale = st.tabs(
        ["Vitals Diagnostic", "Before and After", "WordPress Remediation",
         "Scoring Model"])

    # -----------------------------------------------------------------
    # Grade four metrics, and be clear which three decide anything
    # -----------------------------------------------------------------
    with tab_diag:
        c1, c2 = st.columns(2)
        lcp = c1.slider("Largest Contentful Paint, seconds", 0.4, 10.0, 2.1,
                        0.1, key="cwv_lcp")
        inp = c2.slider("Interaction to Next Paint, ms", 20, 1200, 260, 10,
                        key="cwv_inp")
        c3, c4 = st.columns(2)
        cls = c3.slider("Cumulative Layout Shift", 0.0, 0.8, 0.04, 0.01,
                        key="cwv_cls")
        ttfb = c4.slider("Time to First Byte, ms", 50, 3000, 420, 10,
                         key="cwv_ttfb")

        assessment = evaluate_web_vitals(lcp, inp, cls, ttfb)
        pass_tone = "ok" if assessment.passes_core_web_vitals else "crit"
        core = [v for v in assessment.verdicts if v.is_core_web_vital]
        good_count = sum(1 for v in core if v.status == GOOD)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {pass_tone}">
    <div class="n">{esc(assessment.seo_indicator)}</div>
    <div class="l">Google Core Web Vitals assessment</div></div>
  <div class="app-kpi {pass_tone}"><div class="n">{good_count} of 3</div>
    <div class="l">Core Web Vitals Good, and it needs all three</div></div>
  <div class="app-kpi {STATUS_TONE[assessment.worst_status]}">
    <div class="n">{esc(assessment.worst_status)}</div>
    <div class="l">Worst of the three</div></div>
  <div class="app-kpi"><div class="n">{FIELD_PERCENTILE}th</div>
    <div class="l">Percentile assessed, over {FIELD_WINDOW_DAYS} days</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.dataframe(assessment.rows(), width="stretch", hide_index=True)

        for finding in assessment.findings:
            _finding_card(finding)

        failing = [v for v in core if v.status != GOOD]
        if failing:
            st.markdown("##### The fixes that move the failing metric")
            acronyms = {"lcp": "LCP", "inp": "INP", "cls": "CLS"}
            for verdict in failing:
                steps = steps_that_move(acronyms[verdict.key])
                st.markdown(f"**{esc(verdict.name)}**")
                if steps:
                    st.dataframe(
                        [{"Priority": str(s.rank), "Fix": s.title,
                          "Effort": s.effort} for s in steps],
                        width="stretch", hide_index=True)
                else:
                    st.caption(
                        "No step in the WordPress list moves this metric on "
                        "its own. That is a finding about the list, not about "
                        "the page."
                    )

        st.markdown("##### The published thresholds")
        st.dataframe(threshold_rows(), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # The jump, and the thing the jump does not prove
    # -----------------------------------------------------------------
    with tab_jump:
        applied = st.toggle("Optimization applied", value=True,
                            key="cwv_applied")
        careless = st.toggle(
            "Font swapped without a size matched fallback", value=False,
            key="cwv_font")
        jump = simulate_score_jump(
            applied, AFTER_CARELESS_FONT if careless else AFTER)

        st.markdown(
            f"""
<div class="app-kpis">
  {_score_gauge("Lighthouse mobile, before", jump.before_score)}
  {_score_gauge("Lighthouse mobile, after", jump.after_score)}
  <div class="app-kpi {"ok" if jump.after_passes else "crit"}">
    <div class="n">{"PASS" if jump.after_passes else "FAIL"}</div>
    <div class="l">Core Web Vitals assessment, after</div></div>
  <div class="app-kpi"><div class="n">{jump.points_gained:+d}</div>
    <div class="l">Points, on a number that cannot see INP</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok">
    <div class="n">{jump.bytes_saved / 1000:,.0f} kB</div>
    <div class="l">Bytes no longer downloaded</div></div>
  <div class="app-kpi ok">
    <div class="n">{jump.main_thread_saved_ms:,} ms</div>
    <div class="l">Main thread execution saved</div></div>
  <div class="app-kpi"><div class="n">{esc(LIGHTHOUSE_PROFILE.split(",")[0])}</div>
    <div class="l">{esc(LIGHTHOUSE_PROFILE)}</div></div>
  <div class="app-kpi {"crit" if jump.lab_field_gap else "ok"}">
    <div class="n">{"yes" if jump.lab_field_gap else "no"}</div>
    <div class="l">Score says shipped, assessment says not yet</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for finding in jump.findings:
            _finding_card(finding)

        st.markdown("##### Every metric, before and after")
        st.dataframe(jump.metric_rows(), width="stretch", hide_index=True)

        st.markdown("##### What stopped being downloaded")
        st.dataframe(jump.payload_rows(), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # The fixes
    # -----------------------------------------------------------------
    with tab_fix:
        st.caption(
            "Ordered by points per hour on a page builder site. The two "
            "columns that matter most are what each fix moves and what it does "
            "not, because the fix most often sold moves neither the score nor "
            "the assessment by itself."
        )
        st.dataframe(remediation_rows(), width="stretch", hide_index=True)

        for step in get_wordpress_remediation_steps():
            tone = "warn" if "INP" in step.moves else "info"
            st.markdown(
                f"""
<div class="app-card">
  <h4><span class="app-tag {tone}">{step.rank}</span> {esc(step.title)}</h4>
  <p>{esc(step.action)}</p>
  <p><strong>Moves</strong> {esc(", ".join(step.moves))}.
  <strong>Does not move</strong> {esc(", ".join(step.does_not_move))}.
  Effort {esc(step.effort)}.</p>
  <div class="app-ev">Risk: {esc(step.risk)} WordPress:
  {esc(step.wordpress_note)}</div>
</div>
""",
                unsafe_allow_html=True,
            )

    # -----------------------------------------------------------------
    # How the score is actually made
    # -----------------------------------------------------------------
    with tab_scale:
        st.markdown("##### The five metrics the score is made of")
        st.caption(
            "Interaction to Next Paint and Time to First Byte are reported by "
            "Lighthouse and weighted at zero. The curve below is the log "
            "normal one Lighthouse uses, reproduced with its own constants: a "
            "metric at its p10 scores 90 and a metric at its median scores 50."
        )
        st.dataframe(metric_score_rows(BEFORE.lab_metrics()), width="stretch",
                     hide_index=True)

        st.markdown("##### If there were budget for one metric only")
        st.caption(
            "Each row takes the before page and moves that one metric to its "
            "Good boundary, leaving the other four alone."
        )
        st.dataframe(score_sensitivity(), width="stretch", hide_index=True)

        st.markdown(
            f"""
<div class="app-foot">{esc(DISCLAIMER)} Remediation steps:
{len(get_wordpress_remediation_steps())}. Engine version {ENGINE_VERSION}.</div>
""",
            unsafe_allow_html=True,
        )
