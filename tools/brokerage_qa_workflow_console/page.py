"""Real Estate Brokerage QA & Workflow Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine could run as a pre release gate.

Everything is evaluated live from the controls, so no card on screen can
describe a permission state that was changed two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.brokerage_qa_workflow_console.core import (
    DISCLAIMER,
    ENGINE_VERSION,
    MOD_BROKER_REVIEW,
    MODULES,
    ROLE_TEAM_LEAD,
    ROLES,
    SEV_BLOCKER,
    STATUS_BLOCKED,
    console_summary,
    evaluate_role_permissions,
    generate_marker_io_bug_report,
    get_brokerage_workflows,
    money,
    permission_matrix,
    separation_of_duties_report,
    severity_tone,
    split_commission,
    split_with_cap,
    workflow_rows,
    workflow_summary,
)


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>Real Estate Brokerage QA &amp; Workflow Console</h1>
  <p>A role matrix answers whether a Team Lead may approve a transaction, and
  the answer is yes. It cannot answer whether this Team Lead may approve this
  transaction, and the answer is no when they are the agent on it. Broker
  supervision exists so somebody other than the person who wrote the deal has
  looked at it, and a role check alone cannot see that, because the role is
  correct and the actor is wrong.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    summary = console_summary()
    counts = workflow_summary()

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Workflows**: two open blockers, both the same hole.\n"
            "2. **Permissions**: set the actor and the agent to the same id.\n"
            "3. **Commissions**: move the cap and watch the legs still sum.\n"
            "4. **Defect**: the report, ready for Marker.io."
        )
        st.divider()
        st.caption(DISCLAIMER)
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_flow, tab_perm, tab_money, tab_bug = st.tabs(
        ["Workflow Checks", "Permissions", "Commission Splits", "Defect Report"])

    # -----------------------------------------------------------------
    # Workflows
    # -----------------------------------------------------------------
    with tab_flow:
        st.markdown("#### Every check states a rule, not correct behaviour")

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{counts['total']}</div>
    <div class="l">Checkpoints</div></div>
  <div class="app-kpi crit"><div class="n">{counts['blockers_open']}</div>
    <div class="l">Blockers open</div></div>
  <div class="app-kpi warn"><div class="n">{counts['blocked']}</div>
    <div class="l">Blocked, an unknown</div></div>
  <div class="app-kpi {'ok' if counts['verified_rate'] > 0.9 else 'warn'}">
    <div class="n">{counts['verified_rate']:.0%}</div>
    <div class="l">Verified of all planned</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.caption(
            f"{counts['passed']} passed of {counts['executed']} that ran, "
            f"which reads as {counts['passed'] / counts['executed']:.0%} if "
            f"you quote it that way. The figure above counts the blocked "
            f"check as an unknown rather than a pass, because it is one."
        )

        for check in get_brokerage_workflows():
            st.markdown(
                f"""
<div class="app-card {check.tone}">
  <h4><span class="app-tag {check.tone}">{esc(check.status)}</span>
  {esc(check.check_id)} {esc(check.title)}</h4>
  <p>{esc(check.why_it_matters)}</p>
  <div class="app-ev">{esc(check.module)}, {esc(check.severity)}.
  Expected: {esc(check.expected)}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### The matrix")
        st.dataframe(workflow_rows(), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Permissions
    # -----------------------------------------------------------------
    with tab_perm:
        st.markdown("#### Two passes, because one is not enough")
        st.caption(
            "The role decides what the module allows. The actor decides what "
            "may be done to this particular record. Most systems check the "
            "first and stop, which is how a Team Lead approves their own deal "
            "while every test against the matrix passes."
        )

        c1, c2 = st.columns(2)
        role = c1.selectbox("Role", list(ROLES),
                            index=list(ROLES).index(ROLE_TEAM_LEAD),
                            key="brq_role")
        module = c2.selectbox("Module", list(MODULES),
                              index=list(MODULES).index(MOD_BROKER_REVIEW),
                              key="brq_mod")

        c3, c4 = st.columns(2)
        actor = c3.text_input("Signed in user", value="AG-4471",
                              key="brq_actor")
        subject = c4.text_input("Agent on the file", value="AG-4471",
                                key="brq_subject")

        result = evaluate_role_permissions(role, module, actor, subject)
        tone = "crit" if result.self_action_blocked else "ok"

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{len(result.allowed)}</div>
    <div class="l">Allowed by the role</div></div>
  <div class="app-kpi {tone}"><div class="n">{len(result.effective)}</div>
    <div class="l">Allowed on this record</div></div>
  <div class="app-kpi {tone}">
    <div class="n">{len(result.self_action_blocked)}</div>
    <div class="l">Withheld, self review</div></div>
  <div class="app-kpi {'crit' if result.self_review else 'ok'}">
    <div class="n">{'Yes' if result.self_review else 'No'}</div>
    <div class="l">Actor is the agent on the file</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">
  {esc(', '.join(result.self_action_blocked) or 'nothing withheld')}</span>
  {esc(role)} in {esc(module)}</h4>
  <p>{esc(result.note)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### Action by action on this record")
        st.dataframe(result.rows(), width="stretch", hide_index=True)

        st.markdown(f"##### The role matrix for {esc(module)}, the incomplete picture")
        st.dataframe(permission_matrix(module), width="stretch",
                     hide_index=True)

        st.markdown("##### Separation of duties across every module")
        st.dataframe(separation_of_duties_report(actor or "AG-4471"),
                     width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Commission splits
    # -----------------------------------------------------------------
    with tab_money:
        st.markdown("#### The legs sum to the gross, to the cent")
        st.caption(
            "Every leg is held as an integer number of cents and the leftover "
            "goes to one named leg by largest remainder, rather than each leg "
            "being rounded on its own and the total landing a penny out. A "
            "penny that does not reconcile is a ledger that will not close."
        )

        c1, c2 = st.columns(2)
        gross = c1.number_input("Gross commission", 0.01, 1_000_000.0,
                                9000.0, 100.0, key="brq_gross")
        cap_left = c2.number_input("Cap remaining", 0.0, 100_000.0, 1200.0,
                                   100.0, key="brq_cap")

        c3, c4 = st.columns(2)
        pre_bps = c3.slider("Agent share before the cap, percent", 50, 95, 70,
                            key="brq_pre") * 100
        post_bps = c4.slider("Agent share after the cap, percent", 50, 100,
                             100, key="brq_post") * 100

        capped = split_with_cap(gross, pre_bps, post_bps, cap_left)
        flat = split_commission(gross, (("Agent", pre_bps),
                                        ("Brokerage", 10_000 - pre_bps)))
        difference = capped.legs[0].cents - flat.legs[0].cents

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{money(capped.legs[0].cents)}</div>
    <div class="l">Agent, cap applied</div></div>
  <div class="app-kpi"><div class="n">{money(flat.legs[0].cents)}</div>
    <div class="l">Agent, one rate throughout</div></div>
  <div class="app-kpi {'crit' if difference else 'ok'}">
    <div class="n">{money(abs(difference))}</div>
    <div class="l">Difference on this file</div></div>
  <div class="app-kpi {capped.tone}">
    <div class="n">{'Yes' if capped.reconciles else 'No'}</div>
    <div class="l">Reconciles to the gross</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        if difference:
            st.markdown(
                f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">{money(abs(difference))}</span>
  The cap boundary falls inside this file</h4>
  <p>The first {money(min(int(cap_left * 100), capped.gross_cents))} splits at
  {pre_bps / 100:.0f} percent and the rest at {post_bps / 100:.0f} percent, so
  the file has two rates in it. Applying one rate to the whole amount is wrong
  by {money(abs(difference))} here, and it surfaces at year end rather than on
  the file.</p>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### With the cap applied")
        st.dataframe(capped.rows(), width="stretch", hide_index=True)

        st.markdown("##### A three way split, with a referral")
        three = split_commission(gross, (("Agent", pre_bps),
                                         ("Brokerage", 10_000 - pre_bps - 500),
                                         ("Referral", 500)))
        st.dataframe(three.rows(), width="stretch", hide_index=True)
        if three.remainder_assigned_to:
            st.caption(
                f"The leftover cents went to {three.remainder_assigned_to} by "
                f"largest remainder, which keeps every leg closest to its "
                f"exact share and makes the total exact rather than close.")

    # -----------------------------------------------------------------
    # Defect report
    # -----------------------------------------------------------------
    with tab_bug:
        st.markdown("#### The report, ready for Marker.io")
        st.caption(
            "Marker.io captures the screen and the session automatically. "
            "What it cannot capture is which role was signed in and what the "
            "brokerage consequence is, so both are written out rather than "
            "left to the screenshot."
        )

        failing = [c.check_id for c in get_brokerage_workflows()
                   if not c.passed]
        chosen = st.selectbox("Checkpoint", failing, key="brq_check")
        report = generate_marker_io_bug_report(chosen)

        st.markdown(
            f"""
<div class="app-card {report.tone}">
  <h4><span class="app-tag {report.tone}">{esc(report.severity)}</span>
  {esc(report.title)}</h4>
  <p>{esc(report.workflow_impact)}</p>
  <div class="app-ev">{esc(report.screen)} as {esc(report.role)}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### Summary")
        st.dataframe(report.rows(), width="stretch", hide_index=True)

        st.markdown("##### The report")
        st.code(report.as_markdown(), language="markdown")
        st.info(DISCLAIMER)

    st.markdown(
        f"""
<div class="app-foot">
Real Estate Brokerage QA &amp; Workflow Console, engine version
{ENGINE_VERSION}. A simulator: no brokerage system is connected, no MLS is
queried, no transaction is approved and no commission is paid. Money is held as
integer cents throughout, and nothing here reads the clock or a random source.
{esc(DISCLAIMER)}
</div>
""",
        unsafe_allow_html=True,
    )
