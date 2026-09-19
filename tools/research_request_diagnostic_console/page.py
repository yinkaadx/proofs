"""Research Request Diagnostic Console.

Rendered inside the hub app. Every verdict on this page is computed from the
controls on each run, so a reframe on screen can never describe a request
that was replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.research_request_diagnostic_console.core import (
    ARCHETYPES,
    DECISIVE_MARGIN,
    DEMOGRAPHICS,
    DIMENSIONS,
    ENGINE_VERSION,
    GOALS,
    METHOD_BOTH,
    METHOD_EXISTING,
    METHOD_SURVEY,
    PIVOTAL_QUESTION,
    REFUSALS,
    REGIONS,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    TOTAL_WEIGHT,
    VERDICT_DO_NOT_COMMISSION,
    VERDICT_REFRAME_FIRST,
    VERDICT_UNCLASSIFIED,
    apply_us_market_context,
    evaluate_methodology,
    reframe_client_request,
)

TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}
VERDICT_TONE = {
    VERDICT_DO_NOT_COMMISSION: "crit",
    VERDICT_REFRAME_FIRST: "warn",
    VERDICT_UNCLASSIFIED: "info",
}
METHOD_TONE = {METHOD_EXISTING: "ok", METHOD_SURVEY: "ok", METHOD_BOTH: "warn"}

SAMPLE_REQUESTS = (
    "We want to run a survey to find out why customers are leaving",
    "Can you tell us how much people would pay for the premium tier",
    "We need to size the market before the board meeting",
    "Just curious what people think of our brand",
    "Test this new feature concept with our audience",
    "Work out who our ideal customer is",
    "Find out why the campaign underperformed last quarter",
)


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
  <h1>Research Request Diagnostic Console</h1>
  <p>Three things go wrong between a client asking for research and a
  decision being made better, and all three happen before any fieldwork
  starts. The request names a method rather than a decision. The method is
  chosen before anyone establishes whether the question is about what people
  do or about why they do it. And the finding is then read as though it
  travelled further than the sample it came from.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Reframe**: type a request the tool has never seen and "
            "watch it say so instead of inventing a business problem.\n"
            "2. **Methodology**: read the weights before the answer. They "
            "are published so they can be argued with.\n"
            "3. **Context**: pick a subgroup and see the base size question "
            "raised before anything else."
        )
        st.divider()
        st.caption(
            "Nothing here contacts a client, a panel or a data source. It is "
            "a way of interrogating a brief before it becomes a proposal."
        )
        st.caption(
            "The context layer does not characterise groups of people and "
            "does not supply statistics. It gives provenance and the "
            "questions a finding has not been asked yet."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_reframe, tab_method, tab_context = st.tabs(
        ["Request Reframer", "Methodology", "US Market Context"])

    # -----------------------------------------------------------------
    with tab_reframe:
        st.subheader("From the method they asked for to the decision underneath")
        sample = st.selectbox("A request as it usually arrives",
                              list(SAMPLE_REQUESTS))
        custom = st.text_input("Or paste the one you actually received",
                               value="")

        target = custom.strip() or sample
        reframe = reframe_client_request(target)

        _kpis([
            ("Verdict", reframe.verdict),
            ("Archetype", reframe.archetype_label or "none matched"),
            ("Questions to ask", str(reframe.question_count)),
            ("Classified", "yes" if reframe.classified else "no"),
        ])

        tone = VERDICT_TONE.get(reframe.verdict, "warn")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(reframe.verdict)}</span>
  {esc(reframe.surface_request)}</h4>
  <p>{esc(reframe.underlying_problem
          or 'Nothing was recognised, so nothing has been reframed.')}</p>
  <div class="app-ev">{esc(reframe.real_decision or PIVOTAL_QUESTION)}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        if reframe.classified:
            st.markdown("**The decision this is actually for**")
            st.markdown(f"> {reframe.real_decision}")

        st.markdown("**What has to be answered before a proposal**")
        for question in reframe.missing_context:
            st.markdown(f"- {question}")

        st.markdown("**Findings**")
        for finding in reframe.findings:
            _finding_card(finding)

        st.subheader("The archetypes this reframer knows")
        for archetype in ARCHETYPES:
            st.markdown(
                f"- **{archetype.label}**: usually arrives as "
                f"\"{archetype.surface_reading}\"")

    # -----------------------------------------------------------------
    with tab_method:
        st.subheader("A custom survey against what is already collected")
        goal_label = st.selectbox("The business goal",
                                  [g.label for g in GOALS])
        verdict = evaluate_methodology(goal_label)

        _kpis([
            ("Recommendation", verdict.recommendation),
            ("Question type", verdict.question_type),
            ("Survey", f"{verdict.survey_total} of {TOTAL_WEIGHT}"),
            ("Existing data", f"{verdict.existing_total} of {TOTAL_WEIGHT}"),
            ("Margin", str(verdict.margin)),
        ])

        tone = METHOD_TONE.get(verdict.recommendation, "warn")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(verdict.recommendation)}</span>
  {esc(verdict.goal_label)}</h4>
  <p>{esc(verdict.note)}</p>
  <div class="app-ev">{esc('Decisive at ' + str(verdict.margin) + ' points'
                           if verdict.decisive
                           else 'Inside the ' + str(DECISIVE_MARGIN)
                                + ' point margin, so too close to call')}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**The scoring, row by row**")
        for row in verdict.rows:
            leader = ("survey" if row.survey_weighted > row.existing_weighted
                      else "existing data"
                      if row.existing_weighted > row.survey_weighted
                      else "level")
            st.markdown(
                f"- **{row.dimension}** (weight {row.weight}): survey "
                f"{row.survey_weighted}, existing data "
                f"{row.existing_weighted}, {leader} ahead")
        st.caption(
            f"The survey rows add to {verdict.rows_survey_total} and the "
            f"existing data rows add to {verdict.rows_existing_total}, which "
            f"are the two totals above. The weights add to {TOTAL_WEIGHT}."
        )

        st.markdown("**What each dimension is for**")
        for dimension, weight, note in DIMENSIONS:
            st.markdown(f"- **{dimension}** carries {weight} points. {note}")

        st.markdown("**Findings**")
        for finding in verdict.findings:
            _finding_card(finding)

    # -----------------------------------------------------------------
    with tab_context:
        st.subheader("How far does this finding actually travel")
        raw_finding = st.text_input(
            "The finding as it was reported",
            value="62 percent said they would switch for faster delivery")
        left, right = st.columns(2)
        with left:
            region = st.selectbox("Region it came from", list(REGIONS))
        with right:
            demographic = st.selectbox("Cut it was read on", list(DEMOGRAPHICS))

        layer = apply_us_market_context(raw_finding, region, demographic)

        _kpis([
            ("Confidence", layer.confidence),
            ("Travels nationally",
             "yes" if layer.travels_nationally else "not established"),
            ("Questions raised", str(len(layer.questions_to_ask))),
            ("Structural notes", str(len(layer.structural_notes))),
        ])

        st.markdown(
            f"""
<div class="app-card warn">
  <h4><span class="app-tag warn">{esc(layer.confidence)}</span>
  {esc(layer.region)}, {esc(layer.demographic)}</h4>
  <p>{esc(layer.interpretation)}</p>
  <div class="app-ev">{esc(layer.raw_finding)}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**Ask these before it reaches a slide**")
        for question in layer.questions_to_ask:
            st.markdown(f"- {question}")

        if layer.structural_notes:
            st.markdown("**Structural context for this cut**")
            for note in layer.structural_notes:
                st.markdown(f"- {note}")

        st.markdown("**Findings**")
        for finding in layer.findings:
            _finding_card(finding)

        st.subheader("What this layer deliberately will not do")
        for refusal in REFUSALS:
            st.markdown(f"- {refusal}")
