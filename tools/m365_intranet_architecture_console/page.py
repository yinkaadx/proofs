"""Microsoft 365 Intranet Architecture Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine can sit behind the real Graph, Dataverse and
Power Automate calls.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.m365_intranet_architecture_console.core import (
    ANNUAL_ALLOWANCE,
    EMPLOYEE,
    ENGINE_VERSION,
    MANAGER,
    MAX_WEEKLY_HOURS,
    MODULES,
    PROJECTS,
    RISK_NONE,
    ROLE_MANAGER,
    ROLES,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    TENANT,
    TODAY,
    PtoLedger,
    TimesheetLedger,
    assess,
    can_open,
    decide,
    generate_insight,
    insight_prompt,
    log_hours,
    module_rows,
    project_rows,
    request_pto,
    visible_modules,
)

STATE = "m365_state"

STATUS_TONE = {STATUS_APPROVED: "ok", STATUS_REJECTED: "crit",
               STATUS_PENDING: "warn"}

PROJECT_CHOICES = [project.code for project in PROJECTS]


def _state() -> dict:
    if STATE not in st.session_state:
        st.session_state[STATE] = {
            "pto": PtoLedger(),
            "timesheet": TimesheetLedger(),
            "last_pto": None,
            "last_timesheet": None,
            "last_routing": None,
        }
    return st.session_state[STATE]


def _request_pto() -> None:
    """Raise a request from the controls on screen.

    Every value is read from live widget state rather than from arguments bound
    at the previous render, which are one interaction stale.
    """
    state = _state()
    state["last_pto"] = request_pto(
        state["pto"], EMPLOYEE,
        start=str(st.session_state.get("m365_pto_start", "2026-09-21")),
        days=float(st.session_state.get("m365_pto_days", 3.0)),
        reason=str(st.session_state.get("m365_pto_reason", "Annual leave")))
    state["last_routing"] = None


def _log_hours() -> None:
    state = _state()
    state["last_timesheet"] = log_hours(
        state["timesheet"], EMPLOYEE,
        project_code=str(st.session_state.get("m365_ts_project",
                                              PROJECT_CHOICES[0])),
        day=str(st.session_state.get("m365_ts_day", TODAY)),
        hours=float(st.session_state.get("m365_ts_hours", 7.5)))


def _decide(approve: bool) -> None:
    state = _state()
    request_id = st.session_state.get("m365_approve_target")
    pending = [r for r in state["pto"].requests if r.status == STATUS_PENDING]
    target = next((r for r in pending if r.request_id == request_id), None)
    if target is None:
        return
    approver = (MANAGER if st.session_state.get("m365_approver", MANAGER.name)
                == MANAGER.name else EMPLOYEE)
    state["last_routing"] = decide(state["pto"], target, approver, approve)


def _reset() -> None:
    state = _state()
    state["pto"] = PtoLedger()
    state["timesheet"] = TimesheetLedger()
    state["last_pto"] = None
    state["last_timesheet"] = None
    state["last_routing"] = None


def render() -> None:
    inject()
    state = _state()
    pto: PtoLedger = state["pto"]
    timesheet: TimesheetLedger = state["timesheet"]

    st.markdown(
        """
<div class="app-hero">
  <h1>Microsoft 365 Intranet Architecture Console</h1>
  <p>An intranet on SharePoint, Power Apps and Power Automate fails quietly: a
  module hidden from an employee but still reachable by its link, two leave
  requests that each pass validation and together overdraw the balance, a
  request approved by the person who raised it, and a weekly report nobody
  reads because it is a table rather than an answer. All four are handled here
  as the product rather than as edge cases.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Role Gateway**: switch between Employee and Manager and "
            "watch the modules change.\n"
            "2. **Power Apps**: raise leave and log hours, and try to break "
            "the balance.\n"
            "3. **Power Automate**: approve the request, then try approving "
            "your own.\n"
            "4. **Project Insights**: the weekly summary, with the prompt a "
            "real deployment would send."
        )
        st.divider()
        st.caption(
            f"Nothing here touches a live system. {TENANT} is simulated, no "
            f"Graph call is made and no list is written."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    role = st.radio("Signed in as", ROLES, horizontal=True, key="m365_role")
    person = MANAGER if role == ROLE_MANAGER else EMPLOYEE
    allowed = visible_modules(role)
    balance = pto.balance(EMPLOYEE)

    st.markdown(
        f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{len(allowed)}/{len(MODULES)}</div>
    <div class="l">Modules this role opens</div></div>
  <div class="app-kpi"><div class="n">{balance.available:.1f}</div>
    <div class="l">PTO days available</div></div>
  <div class="app-kpi"><div class="n">{timesheet.week_total(EMPLOYEE.upn, TODAY):.2f}</div>
    <div class="l">Hours logged this week</div></div>
  <div class="app-kpi"><div class="n">{generate_insight().at_risk}</div>
    <div class="l">Projects needing a decision</div></div>
</div>
""",
        unsafe_allow_html=True,
    )

    tab_roles, tab_apps, tab_flow, tab_insights = st.tabs(
        ["Role Gateway", "Power Apps", "Power Automate", "Project Insights"]
    )

    # -----------------------------------------------------------------
    # Entra ID role gateway
    # -----------------------------------------------------------------
    with tab_roles:
        st.markdown(f"#### What {esc(person.name)} sees")
        st.caption(
            "Hiding a tile is a convenience, not a control. The module still "
            "answers a direct link, so every open is authorised by the same "
            "function the back end uses and the navigation simply asks it."
        )

        columns = st.columns(3)
        for index, module in enumerate(allowed):
            with columns[index % 3]:
                st.markdown(
                    f"""
<div class="app-tool">
  <h3>{esc(module.label)}</h3>
  <p>{esc(module.summary)}</p>
  <span class="app-pill">{esc(module.entra_group)}</span>
</div>
""",
                    unsafe_allow_html=True,
                )

        st.markdown("##### Try opening a module directly")
        st.caption(
            "This is the bookmarked link an employee still has after a "
            "promotion is reversed, or the one a colleague pasted into Teams."
        )
        target = st.selectbox("Module", [module.key for module in MODULES],
                              format_func=lambda key: next(
                                  m.label for m in MODULES if m.key == key),
                              key="m365_module_target")
        decision = can_open(role, target)
        tone = "ok" if decision.allowed else "crit"
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">
  {esc('Opened' if decision.allowed else 'Refused')}</span>
  {esc(decision.module.label if decision.module else target)}</h4>
  <div class="app-ev">{esc(role)} &nbsp;
  {esc(decision.module.entra_group if decision.module else '')}</div>
  <p>{esc(decision.reason)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### The whole matrix")
        st.dataframe(module_rows(role), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Power Apps
    # -----------------------------------------------------------------
    with tab_apps:
        st.markdown("#### Request time off")
        st.caption(
            f"The allowance is {ANNUAL_ALLOWANCE:.0f} days. A pending request "
            f"holds its days, so a second request sees the balance the first "
            f"one already spent rather than the one payroll finds out about."
        )

        c1, c2, c3 = st.columns(3)
        c1.text_input("First day", value="2026-09-21", key="m365_pto_start")
        c2.number_input("Days", min_value=0.0, max_value=30.0, value=3.0,
                        step=0.5, key="m365_pto_days")
        c3.text_input("Reason", value="Annual leave", key="m365_pto_reason")

        c4, c5 = st.columns(2)
        c4.button("Submit the request", type="primary", on_click=_request_pto,
                  key="m365_pto_btn")
        c5.button("Reset this session", on_click=_reset, key="m365_reset_btn")

        outcome = state["last_pto"]
        if outcome is not None:
            tone = "ok" if outcome.accepted else "crit"
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">
  {esc('Submitted' if outcome.accepted else outcome.code)}</span>
  {esc(EMPLOYEE.name)}</h4>
  <div class="app-ev">
  {balance.available:.1f} day(s) available of {ANNUAL_ALLOWANCE:.0f}</div>
  <p>{esc(outcome.message)}</p>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### The balance, line by line")
        st.dataframe(balance.rows(), width="stretch", hide_index=True)

        if pto.requests:
            st.markdown("##### Requests raised")
            st.dataframe(pto.rows(), width="stretch", hide_index=True)

        st.divider()
        st.markdown("#### Log timesheet hours")
        st.caption(
            f"Quarter hours only, because billing rounds to fifteen minutes, "
            f"and a weekly cap of {MAX_WEEKLY_HOURS:.0f} hours so an overrun "
            f"is a conversation rather than a surprise on the invoice."
        )

        c6, c7, c8 = st.columns(3)
        c6.selectbox("Project", PROJECT_CHOICES, key="m365_ts_project")
        c7.text_input("Day", value=TODAY, key="m365_ts_day")
        c8.number_input("Hours", min_value=0.0, max_value=24.0, value=7.5,
                        step=0.25, key="m365_ts_hours")

        st.button("Log the hours", on_click=_log_hours, key="m365_ts_btn")

        logged = state["last_timesheet"]
        if logged is not None:
            tone = "ok" if logged.accepted else "crit"
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">
  {esc('Logged' if logged.accepted else logged.code)}</span>
  Week total {logged.week_total:.2f} hours</h4>
  <p>{esc(logged.message)}</p>
</div>
""",
                unsafe_allow_html=True,
            )

        if timesheet.entries:
            st.markdown("##### This week")
            st.dataframe(timesheet.rows(), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Power Automate
    # -----------------------------------------------------------------
    with tab_flow:
        pending = [r for r in pto.requests if r.status == STATUS_PENDING]
        st.markdown("#### The approval flow")
        st.caption(
            "Item created, manager resolved from the profile, approval sent, "
            "response written back. The refusals are the part worth reading: a "
            "person approving their own leave is the finding that reaches the "
            "board."
        )

        if not pending:
            st.info("Nothing is waiting for a decision. Raise a request on the "
                    "Power Apps tab first.")
        else:
            c1, c2 = st.columns(2)
            c1.selectbox("Request", [r.request_id for r in pending],
                         key="m365_approve_target")
            c2.selectbox("Responding as", [MANAGER.name, EMPLOYEE.name],
                         key="m365_approver")

            c3, c4 = st.columns(2)
            c3.button("Approve", type="primary", on_click=_decide, args=(True,),
                      key="m365_approve_btn")
            c4.button("Reject", on_click=_decide, args=(False,),
                      key="m365_reject_btn")

        routing = state["last_routing"]
        if routing is not None:
            tone = "ok" if routing.ok else "crit"
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">
  {esc('Flow completed' if routing.ok else 'Refused by the flow')}</span>
  {esc(routing.request.request_id if routing.request else '')}</h4>
  <p>{esc(routing.message)}</p>
</div>
""",
                unsafe_allow_html=True,
            )
            st.markdown("##### Run history")
            st.dataframe(routing.rows(), width="stretch", hide_index=True)

        if pto.requests:
            st.markdown("##### Every request and where it stands")
            st.dataframe(pto.rows(), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Project insights
    # -----------------------------------------------------------------
    with tab_insights:
        gate = can_open(role, "insights")
        if not gate.allowed:
            st.markdown(
                f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">Refused</span>Project Insights</h4>
  <p>{esc(gate.reason)}</p>
</div>
""",
                unsafe_allow_html=True,
            )
            st.info("Switch the role at the top of the page to Manager to open "
                    "this module. The refusal is the same check the back end "
                    "runs, not a hidden tile.")
        else:
            insight = generate_insight()
            tone = "crit" if insight.at_risk else "ok"

            st.markdown("#### Weekly executive summary")
            st.caption(
                "Computed from the same figures in the table below rather than "
                "written about them. A real deployment sends these numbers to "
                "the Claude API for the wording, and doing the arithmetic here "
                "means the summary cannot describe a project the table does "
                "not show."
            )

            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(insight.headline)}</span>
  Week to {TODAY}</h4>
  <div class="app-ev">{insight.hours_billed:,.0f} hours billed &nbsp;
  {insight.value_billed:,.0f} at current rates</div>
  <p>{esc(insight.summary)}</p>
</div>
""",
                unsafe_allow_html=True,
            )

            st.markdown("##### Project by project")
            for risk in [assess(project) for project in PROJECTS
                         if project.open]:
                card = "ok" if risk.level == RISK_NONE else "warn"
                st.markdown(
                    f"""
<div class="app-card {card}">
  <h4><span class="app-tag {card}">{esc(risk.level)}</span>
  {esc(risk.headline)}</h4>
  <p>{esc(risk.detail)}</p>
</div>
""",
                    unsafe_allow_html=True,
                )

            st.markdown("##### What to do this week")
            for action in insight.actions:
                st.markdown(f"- {action}")

            st.markdown("##### The numbers behind it")
            st.dataframe(project_rows(), width="stretch", hide_index=True)

            st.markdown("##### What would be sent to the Claude API")
            st.code(insight_prompt(), language="text")
            st.caption(
                "Shown because it is the first question a security reviewer "
                "asks about an AI feature. Project codes, clients, hours and "
                "dates leave the tenant. No document content does."
            )

    st.markdown(
        f"""
<div class="app-foot">
Microsoft 365 Intranet Architecture Console, engine version {ENGINE_VERSION}. A
simulator: no tenant is read, no Graph or Dataverse call is made and no list is
written. Access is decided by a check rather than by what the navigation
rendered, because a hidden tile still answers its own link.
</div>
""",
        unsafe_allow_html=True,
    )
