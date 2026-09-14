"""Retreat Funnel Redundancy Guard.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine can sit behind the real FG Funnels webhook.
"""

from __future__ import annotations

import json
import random

import streamlit as st

from shared.theme import esc, inject
from tools.retreat_funnel_redundancy_guard.core import (
    ALERT_TOO_LATE,
    DEFAULT_THRESHOLDS,
    ENGINE_VERSION,
    FAILED,
    FAILURE_MODES,
    FAILURE_NONE,
    MANUAL_FALLBACK_MINUTES,
    PROGRAM,
    READINESS,
    RECOVERED,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SOURCES,
    TICKET_PRICE,
    FunnelMetrics,
    Thresholds,
    escalate,
    evaluate_metrics,
    funnel_rows,
    generate_lead,
    metric_rows,
    metrics_headline,
    run_pipeline,
)

STATE = "retreat_guard_state"

FAILURE_LABELS: dict[str, str] = {FAILURE_NONE: "No failure, clean run"}
FAILURE_LABELS.update({mode.code: mode.label for mode in FAILURE_MODES})
FAILURE_CODES: list[str] = list(FAILURE_LABELS)

STATUS_TONE = {FAILED: "crit", RECOVERED: "warn"}


def _state() -> dict:
    if STATE not in st.session_state:
        st.session_state[STATE] = {
            "rng": random.Random(20260912),
            "sequence": 0,
            "lead": None,
            "run": None,
        }
    return st.session_state[STATE]


def _generate() -> None:
    """Build the next inbound lead from the controls currently on screen."""
    state = _state()
    state["sequence"] += 1
    source = st.session_state.get("rfg_source")
    readiness = st.session_state.get("rfg_readiness")
    state["lead"] = generate_lead(
        state["rng"], state["sequence"],
        source=None if source == "Any" else source,
        readiness=None if readiness == "Any" else readiness)
    state["run"] = None


def _run() -> None:
    """Run the pipeline with whatever failure is selected.

    Every value is read from live widget state rather than from arguments bound
    at the previous render, which would be one interaction stale.
    """
    state = _state()
    lead = state["lead"]
    if lead is None:
        return
    state["run"] = run_pipeline(
        lead,
        failure_code=st.session_state.get("rfg_failure", FAILURE_NONE),
        max_retries=int(st.session_state.get("rfg_retries", 3)))


def render() -> None:
    inject()
    state = _state()
    lead = state["lead"]
    run = state["run"]

    st.markdown(
        """
<div class="app-hero">
  <h1>Retreat Funnel Redundancy Guard</h1>
  <p>A retreat sells from a webinar, and every step between the first DM and
  the offer is a webhook. A webhook that fails quietly costs a seat: the lead
  is not angry, they simply never got the join link, and nobody finds out until
  the show up rate is read a week later. This catches the failure while there
  is still time to fix it by hand.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Lead Simulator**: generate an inbound DM and see its tags.\n"
            "2. **Redundancy Guard**: inject a failure and watch the retries.\n"
            "3. Read the Slack payload the team would actually receive.\n"
            "4. **Metrics**: drop the show up rate below target and see it "
            "flagged."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. FG Funnels, Zoom and Slack "
            "are all simulated in this session, so no message is ever sent."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_lead, tab_guard, tab_metrics = st.tabs(
        ["Lead Simulator", "Redundancy Guard", "Metrics Dashboard"]
    )

    # -----------------------------------------------------------------
    # FG Funnels lead simulator
    # -----------------------------------------------------------------
    with tab_lead:
        st.markdown("#### Incoming lead")
        st.caption(
            f"This is the payload FG Funnels posts when a DM turns into a "
            f"contact for {PROGRAM}. The tags are assigned by rule, because "
            f"every automation downstream keys off them and a tag applied by "
            f"hand is a tag that is missing at 2am."
        )

        c1, c2 = st.columns(2)
        c1.selectbox("Source", ["Any", *SOURCES], key="rfg_source")
        c2.selectbox("Readiness", ["Any", *READINESS], key="rfg_readiness")

        st.button("Generate inbound lead", type="primary", on_click=_generate,
                  key="rfg_generate_btn")

        if lead is None:
            st.info("No lead yet. Generate one to start the flow.")
        else:
            tone = "ok" if lead.hot else "info"
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(lead.lead_id)}</span>{esc(lead.name)}
  {esc(lead.handle)}</h4>
  <div class="app-ev">{esc(lead.source)} &nbsp; {esc(lead.readiness)}</div>
  <p>{esc(lead.message)}</p>
</div>
""",
                unsafe_allow_html=True,
            )

            st.markdown("##### Tags applied by the automation")
            st.markdown(
                "".join(f'<span class="app-pill">{esc(tag)}</span> '
                        for tag in lead.tags),
                unsafe_allow_html=True,
            )
            if lead.hot:
                st.success(
                    "Tagged for the priority call queue, so this lead is "
                    "routed to a human today rather than into the nurture "
                    "sequence."
                )

            st.markdown("##### The webhook payload")
            st.code(json.dumps(lead.as_payload(), indent=2), language="json")

    # -----------------------------------------------------------------
    # Redundancy and error guard
    # -----------------------------------------------------------------
    with tab_guard:
        if lead is None:
            st.info("Generate a lead on the first tab, then run the pipeline "
                    "here.")
        else:
            st.markdown("#### Run the pipeline")
            st.caption(
                "Seven webhooks between the DM and the offer. Pick one to fail "
                "and watch what the retry budget can and cannot rescue."
            )

            c1, c2, c3 = st.columns([2, 1, 1])
            c1.selectbox("Inject a failure", FAILURE_CODES,
                         format_func=lambda code: FAILURE_LABELS[code],
                         key="rfg_failure")
            c2.number_input("Retries per step", min_value=0, max_value=3,
                            value=3, step=1, key="rfg_retries")
            minutes = c3.number_input("Minutes to webinar", min_value=0,
                                      max_value=1440, value=90, step=15,
                                      key="rfg_minutes")

            st.button("Run the pipeline", type="primary", on_click=_run,
                      key="rfg_run_btn")

            if run is None:
                st.info("Run the pipeline to see the guard work.")
            else:
                if run.delivered:
                    st.markdown(
                        f"""
<div class="app-card ok">
  <h4><span class="app-tag ok">Delivered</span>{esc(run.lead.lead_id)} reached
  the offer</h4>
  <div class="app-ev">{len(run.results)} webhooks &nbsp; {run.retries_used}
  retries used &nbsp; {run.total_ms} ms</div>
  <p>Every step was delivered. Nothing to escalate.</p>
</div>
""",
                        unsafe_allow_html=True,
                    )
                    if run.retries_used:
                        st.warning(
                            f"The run recovered, and it took {run.total_ms} ms "
                            f"instead of the {len(run.results) * 180} ms a "
                            f"clean run takes. A retry that works still costs "
                            f"time the reminder schedule is counting on."
                        )
                else:
                    broken = run.broken_step
                    failure = run.failure
                    st.markdown(
                        f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">{esc(failure.severity)}</span>
  {esc(failure.label)} at {esc(broken.step.label)}</h4>
  <div class="app-ev">{esc(broken.step.webhook)} &nbsp;
  {broken.attempts} attempt(s) &nbsp; {esc(failure.code)}</div>
  <p>{esc(failure.consequence)}</p>
</div>
""",
                        unsafe_allow_html=True,
                    )

                st.markdown("##### Step by step")
                st.dataframe(run.rows(), width="stretch", hide_index=True)

                alert = escalate(run, minutes_to_webinar=int(minutes))
                if not alert.raised:
                    st.success(alert.summary)
                else:
                    tone = "crit" if alert.verdict == ALERT_TOO_LATE else "warn"
                    st.markdown("#### The Slack alert")
                    st.markdown(
                        f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(alert.verdict)}</span>
  {esc(alert.channel)}</h4>
  <div class="app-ev">{alert.seats_at_risk} seat(s) &nbsp;
  {alert.revenue_at_risk:,.0f} at stake &nbsp; webinar in
  {alert.minutes_to_webinar} minutes</div>
  <p>{esc(alert.summary)}</p>
</div>
""",
                        unsafe_allow_html=True,
                    )
                    if alert.verdict == ALERT_TOO_LATE:
                        st.error(
                            f"The manual fallback needs about "
                            f"{MANUAL_FALLBACK_MINUTES} minutes and there are "
                            f"{alert.minutes_to_webinar}. An alert that lands "
                            f"after the webinar has started is a record, not a "
                            f"rescue."
                        )
                    st.markdown(f"**Do this now.** {alert.fallback}")
                    st.markdown("##### The payload posted to Slack")
                    st.code(alert.as_json(), language="json")

    # -----------------------------------------------------------------
    # Metrics dashboard
    # -----------------------------------------------------------------
    with tab_metrics:
        st.markdown("#### Webinar funnel")
        st.caption(
            "The show up rate is the load bearing number. When it drops, the "
            "first suspect is a broken step on the tab before this one, not a "
            "weak audience: a lead who never received a join link is counted "
            "here as a no show and looks exactly like disinterest."
        )

        c1, c2, c3, c4 = st.columns(4)
        registrations = c1.number_input("Registrations", min_value=1,
                                        max_value=10000, value=420, step=10,
                                        key="rfg_registrations")
        opens = c2.number_input("Opens", min_value=0, max_value=10000,
                                value=214, step=10, key="rfg_opens")
        clicks = c3.number_input("Clicks", min_value=0, max_value=10000,
                                 value=63, step=5, key="rfg_clicks")
        show_ups = c4.number_input("Showed up", min_value=0, max_value=10000,
                                   value=151, step=10, key="rfg_show_ups")

        c5, c6, c7 = st.columns(3)
        offers = c5.number_input("Offers made", min_value=0, max_value=10000,
                                 value=138, step=5, key="rfg_offers")
        closes = c6.number_input("Seats closed", min_value=0, max_value=10000,
                                 value=19, step=1, key="rfg_closes")
        show_up_target = c7.slider("Show up rate target", min_value=0.10,
                                   max_value=0.80,
                                   value=DEFAULT_THRESHOLDS.show_up_rate,
                                   step=0.05, key="rfg_show_up_target")

        metrics = FunnelMetrics(
            registrations=int(registrations), emails_sent=int(registrations),
            opens=int(opens), clicks=int(clicks), show_ups=int(show_ups),
            offers=int(offers), closes=int(closes))
        thresholds = Thresholds(show_up_rate=float(show_up_target))
        flags = evaluate_metrics(metrics, thresholds)
        severity, headline = metrics_headline(flags, metrics)
        tone = {SEVERITY_CRITICAL: "crit", SEVERITY_OK: "ok"}.get(severity,
                                                                  "warn")

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{metrics.open_rate:.0%}</div>
    <div class="l">Open rate</div></div>
  <div class="app-kpi"><div class="n">{metrics.click_rate:.0%}</div>
    <div class="l">Click rate</div></div>
  <div class="app-kpi {tone}"><div class="n">{metrics.show_up_rate:.0%}</div>
    <div class="l">Show up rate</div></div>
  <div class="app-kpi"><div class="n">{metrics.close_rate:.0%}</div>
    <div class="l">Close rate</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(severity)}</span>Cohort read</h4>
  <div class="app-ev">{metrics.closes} seat(s) closed &nbsp;
  {metrics.revenue:,.0f} booked at {TICKET_PRICE:,.0f} a seat</div>
  <p>{esc(headline)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### Against target")
        st.dataframe(metric_rows(flags), width="stretch", hide_index=True)

        failing = [flag for flag in flags if not flag.passing]
        for flag in failing:
            tone = "crit" if flag.severity == SEVERITY_CRITICAL else "warn"
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(flag.severity)}</span>
  {esc(flag.name)} {flag.value:.1%} against {flag.threshold:.1%}</h4>
  <p>{esc(flag.note)}</p>
</div>
""",
                unsafe_allow_html=True,
            )
        if not failing:
            st.success(
                "Every metric is above target. The guard has nothing to "
                "explain on this cohort."
            )

        st.markdown("##### Stage by stage")
        st.dataframe(funnel_rows(metrics), width="stretch", hide_index=True)

    st.markdown(
        f"""
<div class="app-foot">
Retreat Funnel Redundancy Guard, engine version {ENGINE_VERSION}. A simulator:
no FG Funnels, Zoom or Slack API is contacted and nothing you enter leaves this
session. The pipeline stops at a hard failure rather than continuing, because a
confirmation email sent with no join link in it is worse than one not sent at
all.
</div>
""",
        unsafe_allow_html=True,
    )
