"""Mobile QA Bug Bash Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine could sit behind a real Asana or Slack
integration.

Everything is evaluated live from the controls, so no card on screen can
describe a dispatch that was replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.mobile_qa_bug_bash_console.core import (
    ASANA_ENDPOINT,
    ENGINE_VERSION,
    REPRODUCIBILITY_FIELDS,
    REQUIRED_FIELDS,
    SEVERITIES,
    SEVERITY_SLA_HOURS,
    SLACK_CHANNEL,
    SLACK_ENDPOINT,
    all_test_cases,
    assess_report,
    audit_figma_gaps,
    bug_bash_summary,
    dispatch_rows,
    figma_rows,
    get_sample_bug_report,
    map_reviews_to_test_cases,
    report_lines,
    review_rows,
    severity_tone,
    simulate_asana_slack_dispatch,
)


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>Mobile QA Bug Bash Console</h1>
  <p>A bug report is only worth writing if a developer can reproduce it
  without asking a question, and the missing piece is almost never the
  description. It is the build number, the exact device, or the precondition
  the reporter set up an hour earlier without noticing. This runs a bug bash
  end to end: the report as a structured record, the one star reviews turned
  into ranked test cases, the ticket and the alert that come out of triage,
  and the places where the build and the design file disagree.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    summary = bug_bash_summary()

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Report**: delete a field and watch the verdict change.\n"
            "2. **Reviews**: read the categories, ranked worst first.\n"
            "3. **Dispatch**: pick a severity and press Dispatch.\n"
            "4. **Figma**: the gaps a side by side review cannot show."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. No Asana task is created, no "
            "Slack message is sent and no Figma file is read."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_report, tab_reviews, tab_dispatch, tab_figma = st.tabs(
        ["Bug Report", "Review Triage", "Asana and Slack", "Figma Gaps"])

    # -----------------------------------------------------------------
    # The bug report
    # -----------------------------------------------------------------
    with tab_report:
        st.markdown("#### A report a developer can act on unassisted")

        report = get_sample_bug_report()
        dropped = st.multiselect(
            "Remove fields to see what a thin report costs",
            list(REQUIRED_FIELDS), default=[], key="mqa_dropped")
        for name in dropped:
            report.pop(name, None)

        quality = assess_report(report)
        tone = ("ok" if quality.complete
                else "crit" if not quality.reproducible else "warn")

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {tone}"><div class="n">{quality.score:.0%}</div>
    <div class="l">Fields present</div></div>
  <div class="app-kpi {'ok' if quality.reproducible else 'crit'}">
    <div class="n">{'Yes' if quality.reproducible else 'No'}</div>
    <div class="l">Can be reproduced</div></div>
  <div class="app-kpi {'crit' if quality.missing else 'ok'}">
    <div class="n">{len(quality.missing) + len(quality.empty)}</div>
    <div class="l">Fields missing or empty</div></div>
  <div class="app-kpi"><div class="n">{len(REQUIRED_FIELDS)}</div>
    <div class="l">Fields required</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(quality.verdict)}</span>
  {esc(str(report.get('Severity', 'No severity set')))}</h4>
  <p>Three fields decide whether a bug can be reproduced at all:
  {esc(', '.join(REPRODUCIBILITY_FIELDS))}. Without them a developer cannot
  set up the same conditions, so the report is not merely thin, it is
  unactionable.</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### Field by field")
        st.dataframe(quality.rows(), width="stretch", hide_index=True)

        st.markdown("##### The report as it would be pasted into a ticket")
        st.code(report_lines(report), language="text")

    # -----------------------------------------------------------------
    # Review triage
    # -----------------------------------------------------------------
    with tab_reviews:
        st.markdown("#### One star reviews, translated into tests")
        st.caption(
            "A one star review is a bug report written by somebody with no "
            "vocabulary for it. Ranking by severity is what stops the loudest "
            "category rather than the worst one going to the top."
        )

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{summary['categories']}</div>
    <div class="l">Complaint categories</div></div>
  <div class="app-kpi"><div class="n">{summary['test_cases']}</div>
    <div class="l">Test cases derived</div></div>
  <div class="app-kpi crit"><div class="n">{summary['blockers']}</div>
    <div class="l">Blockers</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for entry in map_reviews_to_test_cases():
            card = severity_tone(entry.worst_severity)
            st.markdown(
                f"""
<div class="app-card {card}">
  <h4><span class="app-tag {card}">{esc(entry.worst_severity)}</span>
  {esc(entry.category)}</h4>
  <p>{esc(entry.example_review)}</p>
  <div class="app-ev">{entry.review_count} reviews,
  {entry.average_stars:.1f} stars average,
  {len(entry.test_cases)} test case(s)</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### The categories")
        st.dataframe(review_rows(), width="stretch", hide_index=True)

        st.markdown("##### Every test case, worst first")
        st.dataframe(
            [{"Case": case.case_id, "Severity": case.severity,
              "Title": case.title, "Steps": str(len(case.steps))}
             for case in all_test_cases()],
            width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Asana and Slack dispatch
    # -----------------------------------------------------------------
    with tab_dispatch:
        st.markdown("#### What triage sends, and to whom")
        st.caption(
            "A severity that does not change who is notified is a severity "
            "nobody will bother setting correctly, so the assignee and the "
            "response target are both derived from it."
        )

        c1, c2, c3 = st.columns([2, 4, 3])
        bug_id = c1.text_input("Bug ID", value="BUG-4821", key="mqa_id")
        title = c2.text_input("Title", value="Checkout crashes on Pay Now",
                              key="mqa_title")
        severity = c3.selectbox("Severity", list(SEVERITIES), index=0,
                                key="mqa_sev")

        dispatch = simulate_asana_slack_dispatch(bug_id, title, severity)
        tone = dispatch.tone

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {tone}"><div class="n">{esc(severity)}</div>
    <div class="l">Severity</div></div>
  <div class="app-kpi"><div class="n">{SEVERITY_SLA_HOURS[severity]}</div>
    <div class="l">Response target, hours</div></div>
  <div class="app-kpi {tone}">
    <div class="n">{'Yes' if dispatch.notified_on_call else 'No'}</div>
    <div class="l">On call paged</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        sent = st.button("Dispatch to Asana and Slack", type="primary",
                         key="mqa_dispatch")
        if sent:
            st.success(
                f"Simulated. {dispatch.bug_id} would be assigned to "
                f"{dispatch.asana['data']['assignee']} and posted to "
                f"{SLACK_CHANNEL}. Nothing was actually sent.")
            st.dataframe(dispatch_rows(dispatch), width="stretch",
                         hide_index=True)
        else:
            st.caption(
                "The payloads below are built live from the fields above. "
                "Press Dispatch to see what the two calls would return.")

        st.markdown(f"##### Asana, {esc(ASANA_ENDPOINT)}")
        st.code(dispatch.asana_json(), language="json")

        st.markdown(f"##### Slack, {esc(SLACK_ENDPOINT)}")
        st.code(dispatch.slack_json(), language="json")

    # -----------------------------------------------------------------
    # Figma gaps
    # -----------------------------------------------------------------
    with tab_figma:
        st.markdown("#### Where the build and the design file disagree")
        st.caption(
            "The third kind is the one design reviews miss. A screen built "
            "with no design at all never appears in a side by side "
            "comparison, because there is nothing to put beside it, so it is "
            "the state most likely to ship."
        )

        gaps = audit_figma_gaps()
        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi crit"><div class="n">{len(gaps)}</div>
    <div class="l">Gaps found</div></div>
  <div class="app-kpi warn">
    <div class="n">{len([g for g in gaps if g.kind == 'Built with no design'])}</div>
    <div class="l">Built with no design</div></div>
  <div class="app-kpi crit">
    <div class="n">{len([g for g in gaps if g.kind == 'Not built'])}</div>
    <div class="l">Designed, never built</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for gap in gaps:
            card = severity_tone(gap.severity)
            st.markdown(
                f"""
<div class="app-card {card}">
  <h4><span class="app-tag {card}">{esc(gap.kind)}</span>
  {esc(gap.screen)}</h4>
  <p>{esc(gap.detail)}</p>
  <div class="app-ev">{esc(gap.frame)} &nbsp; {esc(gap.severity)}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### The gaps")
        st.dataframe(figma_rows(), width="stretch", hide_index=True)

    st.markdown(
        f"""
<div class="app-foot">
Mobile QA Bug Bash Console, engine version {ENGINE_VERSION}. A simulator: no
Asana task is created, no Slack webhook is called, no Figma file is read and no
device is tested. Every payload is built from the fields on screen, and nothing
here reads the clock or a random source, so the same bug produces the same
payload every time.
</div>
""",
        unsafe_allow_html=True,
    )
