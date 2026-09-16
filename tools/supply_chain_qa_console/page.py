"""Supply Chain QA & Regression Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine could generate tickets from a CI run.

Everything is evaluated live from the controls, so no card on screen can
describe a scenario that was changed two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.supply_chain_qa_console.core import (
    DEFAULT_SERVICE,
    ENGINE_VERSION,
    MODULES,
    REGRESSION_CLEAN,
    SERVICE_LEVELS,
    SEVERITIES,
    STABLE_SUPPLIER,
    VOLATILE_SUPPLIER,
    PlanningScenario,
    console_summary,
    defect_backlog,
    execution_rows,
    generate_defect_report,
    get_execution_metrics,
    get_supply_chain_test_cases,
    severity_tone,
    case_matrix_rows,
    z_for,
)


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>Supply Chain QA &amp; Regression Console</h1>
  <p>A planning defect is not like a interface defect. Nothing crashes,
  nothing looks wrong, and the number is simply the wrong number, so it ships
  and is believed for a quarter until somebody counts the shelf. That makes
  the arithmetic the test, and it makes a ticket worthless unless it carries
  both numbers and the formula that separates them.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    summary = console_summary()

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Cases**: read what each one expects as a number.\n"
            "2. **Defect**: drag lead time sigma and watch the gap open.\n"
            "3. **Execution**: compare pass rate against verified rate.\n"
            "4. **Backlog**: one ticket per module."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. No planning run is triggered, "
            "no ticket is filed and no test is executed."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_cases, tab_defect, tab_exec, tab_backlog = st.tabs(
        ["Test Cases", "Defect Generator", "Execution", "Backlog"])

    # -----------------------------------------------------------------
    # Test cases
    # -----------------------------------------------------------------
    with tab_cases:
        st.markdown("#### Every case expects a number, not correct behaviour")
        st.caption(
            "A planning case judged by eye is not a case. Each of these names "
            "the figure it expects and, where it matters, the figure a known "
            "bug produces instead."
        )

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{summary['test_cases']}</div>
    <div class="l">Test cases</div></div>
  <div class="app-kpi"><div class="n">{summary['regression_cases']}</div>
    <div class="l">In the regression pack</div></div>
  <div class="app-kpi crit"><div class="n">{summary['blocker_cases']}</div>
    <div class="l">Blocker if they fail</div></div>
  <div class="app-kpi"><div class="n">{summary['modules']}</div>
    <div class="l">Modules covered</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for case in get_supply_chain_test_cases():
            tone = severity_tone(case.severity_if_failed)
            steps = "".join(f"<li>{esc(step)}</li>" for step in case.steps)
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(case.case_id)}</span>
  {esc(case.title)}</h4>
  <p>{esc(case.module)}. Fails at {esc(case.severity_if_failed)}.</p>
  <ol>{steps}</ol>
  <div class="app-ev">Expected: {esc(case.expected)}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### The matrix")
        st.dataframe(case_matrix_rows(), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Defect generator
    # -----------------------------------------------------------------
    with tab_defect:
        st.markdown("#### The gap opens with lead time variance, not demand")
        st.caption(
            "The common formula multiplies demand variance by the square root "
            "of lead time and treats lead time itself as fixed. That is "
            "correct only when the supplier never varies. Move the slider and "
            "watch what the missing term is worth."
        )

        c1, c2 = st.columns(2)
        lt_sigma = c1.slider("Lead time standard deviation, days",
                             min_value=0.0, max_value=8.0, value=4.5, step=0.5,
                             key="scq_lt_sigma")
        service = c2.selectbox("Service level", sorted(SERVICE_LEVELS),
                               index=sorted(SERVICE_LEVELS).index(DEFAULT_SERVICE),
                               format_func=lambda value: f"{value:.0%}",
                               key="scq_service")

        c3, c4 = st.columns(2)
        module = c3.selectbox("Module", list(MODULES), index=1, key="scq_mod")
        severity = c4.selectbox("Severity", list(SEVERITIES), index=0,
                                key="scq_sev")

        scenario = PlanningScenario(
            VOLATILE_SUPPLIER.sku, VOLATILE_SUPPLIER.demand_mean,
            VOLATILE_SUPPLIER.demand_sigma, VOLATILE_SUPPLIER.lead_time_days,
            lt_sigma, service)
        report = generate_defect_report(module, severity, scenario)
        agree = scenario.shortfall < 0.01

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok">
    <div class="n">{scenario.correct_safety_stock:,.0f}</div>
    <div class="l">Expected units</div></div>
  <div class="app-kpi {'ok' if agree else 'crit'}">
    <div class="n">{scenario.naive_safety_stock:,.0f}</div>
    <div class="l">Actual units</div></div>
  <div class="app-kpi {'ok' if agree else 'crit'}">
    <div class="n">{scenario.shortfall:,.0f}</div>
    <div class="l">Units short</div></div>
  <div class="app-kpi"><div class="n">{z_for(service)}</div>
    <div class="l">z at {service:.0%}</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        if agree:
            st.success(
                "With lead time sigma at zero the two formulas agree exactly, "
                "which is why a suite tested only against a reliable supplier "
                "passes while the bug is live.")
        else:
            st.markdown(
                f"""
<div class="app-card {report.tone}">
  <h4><span class="app-tag {report.tone}">{esc(report.severity)}</span>
  {esc(report.ticket_id)} understated {scenario.understated_by:,.2f} times
  over</h4>
  <p>{esc(report.business_impact)}</p>
  <div class="app-ev">{esc(report.formula_expected)} against
  {esc(report.formula_actual)}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### The ticket")
        st.dataframe(report.rows(), width="stretch", hide_index=True)
        st.code(report.as_markdown(), language="markdown")

        st.markdown("##### The payload an engineer replays")
        st.code(report.payload_json(), language="json")

    # -----------------------------------------------------------------
    # Execution
    # -----------------------------------------------------------------
    with tab_exec:
        st.markdown("#### Pass rate is not the number that matters")
        st.caption(
            "A blocked test is not a pass and it is not a failure, it is an "
            "unknown. Counting blocked tests out of the denominator is how a "
            "cycle reports ninety four percent while a third of the suite "
            "never ran, and how a release is signed off on evidence nobody "
            "gathered."
        )

        metrics = get_execution_metrics()
        latest = metrics["latest"]

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{latest.pass_rate:.0%}</div>
    <div class="l">Pass rate of executed</div></div>
  <div class="app-kpi {latest.tone}">
    <div class="n">{latest.verified_rate:.0%}</div>
    <div class="l">Verified of planned</div></div>
  <div class="app-kpi {'crit' if latest.open_blockers else 'ok'}">
    <div class="n">{latest.open_blockers}</div>
    <div class="l">Open blockers</div></div>
  <div class="app-kpi {latest.tone}">
    <div class="n">{esc(latest.regression_status)}</div>
    <div class="l">Regression status</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for cycle in metrics["cycles"]:
            st.markdown(
                f"""
<div class="app-card {cycle.tone}">
  <h4><span class="app-tag {cycle.tone}">
  {esc(cycle.regression_status)}</span> {esc(cycle.cycle)}</h4>
  <p>{cycle.passed} passed, {cycle.failed} failed, {cycle.blocked} blocked
  and {cycle.not_run} never run, out of {cycle.planned} planned.</p>
  <div class="app-ev">Quoted pass rate {cycle.pass_rate:.1%}, actually
  verified {cycle.verified_rate:.1%}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### The cycles")
        st.dataframe(execution_rows(), width="stretch", hide_index=True)

        if latest.shippable:
            st.success("Clean. Nothing failed, nothing is blocked and "
                       "coverage is complete.")
        else:
            st.warning(
                f"Not {REGRESSION_CLEAN.lower()}. {latest.blocked} test(s) are "
                f"blocked, so {1 - latest.verified_rate:.0%} of the planned "
                f"suite is an unknown rather than a pass.")

    # -----------------------------------------------------------------
    # Backlog
    # -----------------------------------------------------------------
    with tab_backlog:
        st.markdown("#### One ticket per module")
        st.caption(
            "Every expected figure here is computed by the correct formula "
            "rather than typed in, so a developer reproducing the ticket gets "
            "the same number."
        )
        for report in defect_backlog():
            st.markdown(
                f"""
<div class="app-card {report.tone}">
  <h4><span class="app-tag {report.tone}">{esc(report.severity)}</span>
  {esc(report.ticket_id)} {esc(report.module)}</h4>
  <p>{esc(report.summary)}</p>
  <div class="app-ev">Expected {report.expected_value:,.2f}, actual
  {report.actual_value:,.2f}, variance {report.variance:,.2f}
  ({report.variance_percent:,.1f}%)</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### A reliable supplier, for contrast")
        st.dataframe(
            [{"Scenario": "Volatile supplier",
              "Lead time sigma": f"{VOLATILE_SUPPLIER.lead_time_sigma} days",
              "Naive": f"{VOLATILE_SUPPLIER.naive_safety_stock:,.2f}",
              "Correct": f"{VOLATILE_SUPPLIER.correct_safety_stock:,.2f}"},
             {"Scenario": "Stable supplier",
              "Lead time sigma": f"{STABLE_SUPPLIER.lead_time_sigma} days",
              "Naive": f"{STABLE_SUPPLIER.naive_safety_stock:,.2f}",
              "Correct": f"{STABLE_SUPPLIER.correct_safety_stock:,.2f}"}],
            width="stretch", hide_index=True)
        st.caption(
            "The two rows are the whole argument. A suite built on the second "
            "supplier is green while the first is losing sales."
        )

    st.markdown(
        f"""
<div class="app-foot">
Supply Chain QA &amp; Regression Console, engine version {ENGINE_VERSION}. A
simulator: no planning run is triggered, no ticket is filed and no test is
executed. Every figure is computed from the controls on screen, and nothing
here reads the clock or a random source, so a ticket carries the same numbers
every time it is generated.
</div>
""",
        unsafe_allow_html=True,
    )
