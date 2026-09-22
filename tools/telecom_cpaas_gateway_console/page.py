"""Telecom CPaaS and SMPP Gateway Console.

Rendered inside the hub app. All logic lives in core.py, which has no
Streamlit dependency.

Everything on this page is evaluated live from the controls. There is no
session state to go stale, which removes the class of bug where a card on
screen describes a session that two interactions ago stopped being true.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.telecom_cpaas_gateway_console.core import (
    APPLICATION_TIMEOUT_SECONDS,
    ENGINE_VERSION,
    ENQUIRE_LINK_SECONDS,
    LICENSED_TPS,
    MENU_TREE,
    NETWORK_SESSION_BUDGET_SECONDS,
    PRIMARY_STATES,
    ROUTE_HELD,
    ROUTE_PRIMARY,
    ROUTE_SECONDARY,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    STATE_ACTIVE,
    STATE_COMPLETED,
    STATE_TIMED_OUT,
    USSD_MAX_CHARACTERS,
    compare_stacks,
    get_telecom_architecture_matrix,
    reconnect_ramp,
    simulate_smpp_carrier_failover,
    simulate_ussd_session,
    validate_menu_payload,
)

_TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}

_STATE_TONE = {STATE_ACTIVE: "ok", STATE_COMPLETED: "ok",
               STATE_TIMED_OUT: "crit"}

_ROUTE_TONE = {ROUTE_PRIMARY: "ok", ROUTE_SECONDARY: "warn",
               ROUTE_HELD: "crit"}


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


def _sequence_diagram(lanes, arrows, height: int = 250) -> None:
    """Inline SVG sequence diagram. Lanes are vertical, arrows are hops."""
    width = 720
    spacing = width / (len(lanes) + 1)
    parts: list[str] = []
    positions = {}
    for index, lane in enumerate(lanes, start=1):
        x = spacing * index
        positions[lane] = x
        parts.append(
            f'<text x="{x:.0f}" y="18" text-anchor="middle" '
            f'font-size="12" font-weight="650" '
            f'fill="var(--app-ink)">{esc(lane)}</text>')
        parts.append(
            f'<line x1="{x:.0f}" y1="28" x2="{x:.0f}" y2="{height - 12}" '
            f'stroke="var(--app-line)" stroke-width="2" />')

    top = 52
    for order, (source, target, label, tone) in enumerate(arrows):
        y = top + order * 34
        x1 = positions[source]
        x2 = positions[target]
        colour = {"ok": "var(--app-ok)", "crit": "var(--app-crit)",
                  "warn": "var(--app-warn)"}[tone]
        dash = ' stroke-dasharray="6 4"' if tone == "crit" else ""
        if x1 == x2:
            parts.append(
                f'<path d="M {x1:.0f} {y} q 42 0 42 12 q 0 12 -42 12" '
                f'fill="none" stroke="{colour}" stroke-width="2"{dash} />')
            parts.append(
                f'<text x="{x1 + 52:.0f}" y="{y + 6}" font-size="11" '
                f'fill="{colour}">{esc(label)}</text>')
            continue
        direction = 1 if x2 > x1 else -1
        end = x2 - direction * 6
        parts.append(
            f'<line x1="{x1:.0f}" y1="{y}" x2="{end:.0f}" y2="{y}" '
            f'stroke="{colour}" stroke-width="2"{dash} />')
        parts.append(
            f'<path d="M {end:.0f} {y} l {-direction * 8} -5 '
            f'l 0 10 z" fill="{colour}" />')
        parts.append(
            f'<text x="{(x1 + x2) / 2:.0f}" y="{y - 6}" '
            f'text-anchor="middle" font-size="11" '
            f'fill="{colour}">{esc(label)}</text>')

    st.markdown(
        f'<div class="app-card"><svg viewBox="0 0 {width} {height}" '
        f'width="100%" height="{height}" style="display:block">'
        f'{"".join(parts)}</svg></div>',
        unsafe_allow_html=True)


def render() -> None:
    inject()
    st.markdown(
        '<div class="app-hero">\n'
        "  <h1>Telecom CPaaS and SMPP Gateway Console</h1>\n"
        "  <p>Three parts of a messaging platform, each built around a\n"
        "  failure that logs as a success. A USSD session lives on the\n"
        "  operator's gateway, so a reply written after the timer fires is\n"
        "  discarded before it reaches the handset while the application's\n"
        "  own write returns fine. Failing over to a second carrier moves\n"
        "  the traffic and leaves every outstanding delivery receipt keyed\n"
        "  to the carrier that has just been left. And coming back from an\n"
        "  outage at the licensed rate is how the reconnection fails.</p>\n"
        "</div>",
        unsafe_allow_html=True)

    ussd_tab, smpp_tab, stack_tab = st.tabs(
        ["USSD session", "SMPP failover", "Architecture matrix"])

    # -- 1. USSD -----------------------------------------------------------
    with ussd_tab:
        st.markdown(
            "#### A late reply is not a slow reply, it is no reply\n\n"
            "Once the operator releases the session, the response the "
            "application writes goes nowhere. The socket write succeeds, so "
            "nothing in the application's own logs records a failure, and "
            "the subscriber sees the operator's timeout text.")

        col_a, col_b = st.columns(2)
        with col_a:
            step = st.slider("Menu step", 0, len(MENU_TREE) - 1, 2, 1)
            session_id = st.text_input("Session identifier",
                                       value="ATUid_7f31a8")
        with col_b:
            delay = st.slider("Response delay in seconds", 0.0, 20.0, 12.5,
                              0.5)
            elapsed = st.slider("Seconds already spent in this session",
                                0.0, 200.0, 24.0, 1.0)

        session = simulate_ussd_session(session_id, step, delay, elapsed)
        tone = _TONE[session.severity]
        state_tone = _STATE_TONE[session.state]

        _kpis([
            (session.state, "Session state", state_tone),
            (f"{session.response_delay_seconds:.1f}s", "This step",
             "crit" if session.timed_out else "ok"),
            (f"{session.elapsed_seconds:.0f}s of "
             f"{NETWORK_SESSION_BUDGET_SECONDS}s", "Session cap", ""),
            ("yes" if session.payload_delivered else "no",
             "Payload delivered",
             "ok" if session.payload_delivered else "crit"),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag {state_tone}">'
            f'{esc(session.state)}</span>\n'
            f"  {esc(session.headline)}</h4>\n"
            f"</div>",
            unsafe_allow_html=True)

        _sequence_diagram(
            ["Handset", "Operator USSD", "Application"],
            [("Handset", "Operator USSD", "dial short code", "ok"),
             ("Operator USSD", "Application", f"step {step} request", "ok"),
             ("Application", "Application",
              f"{delay:.1f}s of work", "crit" if session.timed_out else "ok"),
             ("Operator USSD", "Handset",
              "session released, operator timeout text"
              if session.timed_out else "menu shown", state_tone),
             ("Application", "Operator USSD",
              "response discarded" if session.timed_out
              else "response accepted", state_tone)],
            height=232)

        if session.payload_delivered:
            st.markdown("**What the subscriber sees**")
            st.code(session.menu_payload, language="text")
        else:
            st.markdown(
                f'<div class="app-card crit">\n'
                f'  <h4><span class="app-tag crit">NOT DELIVERED</span>\n'
                f"  Nothing reached the handset</h4>\n"
                f"  <p>{esc(session.intercept_message)}</p>\n"
                f'  <div class="app-ev">Log this. Do not send it.</div>\n'
                f"</div>",
                unsafe_allow_html=True)
            st.markdown("**What the application would have sent**")
            st.code(MENU_TREE[step].payload, language="text")

        st.markdown("#### Every menu payload, checked on its own")
        rows = []
        for node in MENU_TREE:
            valid, note = validate_menu_payload(node.payload)
            rows.append(_stage("pass" if valid else "fail",
                               f"Step {node.step}", note))
        rows.append(_stage(
            "fail", "A payload with no prefix",
            validate_menu_payload("Welcome, press 1 to continue")[1]))
        st.markdown(f'<div class="app-card">{"".join(rows)}</div>',
                    unsafe_allow_html=True)
        st.caption(
            f"The limit is {USSD_MAX_CHARACTERS} characters and the string "
            f"is cut rather than wrapped, so an option past the cut is one "
            f"the subscriber selects without seeing. The application budget "
            f"here is {APPLICATION_TIMEOUT_SECONDS} seconds per step.")

        for finding in session.findings:
            _finding_card(finding)

    # -- 2. SMPP -----------------------------------------------------------
    with smpp_tab:
        st.markdown(
            "#### Failover moves the traffic, not the message identifiers\n\n"
            "A delivery receipt for a message already submitted to the "
            "primary arrives on the primary's session under the primary's "
            "identifier. An application correlating on that identifier "
            "alone never matches it once the traffic has moved.")

        col_c, col_d = st.columns(2)
        with col_c:
            pending = st.slider("Pending messages", 0, 50000, 4500, 100)
            primary = st.selectbox("Primary bind", PRIMARY_STATES, index=4)
        with col_d:
            secondary = st.toggle("Secondary carrier configured",
                                  value=True)

        failover = simulate_smpp_carrier_failover(pending, primary,
                                                  secondary)
        tone = _TONE[failover.severity]
        route_tone = _ROUTE_TONE.get(failover.route, "warn")

        _kpis([
            (failover.route, "Routing", route_tone),
            (f"{failover.submitting_tps} TPS", "Submit rate",
             "crit" if failover.submitting_tps == 0 else "warn"),
            (f"{failover.queued:,}", "Queued", ""),
            (f"{failover.dlr_at_risk:,}", "Receipts stranded",
             "crit" if failover.dlr_at_risk else "ok"),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag {route_tone}">'
            f'{esc(failover.route)}</span>\n'
            f"  {esc(failover.headline)}</h4>\n"
            f'  <div class="app-ev">'
            f"{failover.in_flight} PDU(s) in the window, drain estimate "
            f"{failover.drain_seconds} second(s)</div>\n"
            f"</div>",
            unsafe_allow_html=True)

        _sequence_diagram(
            ["Application", "Primary SMSC", "Secondary SMSC"],
            [("Application", "Primary SMSC", "bind_transceiver", "ok"),
             ("Application", "Primary SMSC",
              f"enquire_link every {ENQUIRE_LINK_SECONDS}s",
              "crit" if primary == PRIMARY_STATES[4] else "ok"),
             ("Application", "Primary SMSC", "submit_sm",
              "ok" if failover.route == ROUTE_PRIMARY else "crit"),
             ("Application", "Secondary SMSC",
              "failover submit_sm" if failover.route == ROUTE_SECONDARY
              else "no secondary route",
              "warn" if failover.route == ROUTE_SECONDARY else "crit"),
             ("Primary SMSC", "Application",
              "deliver_sm receipt, primary identifier",
              "crit" if failover.dlr_at_risk else "ok")],
            height=232)

        st.markdown("**Rate ramp after the bind is re established**")
        st.table({
            "At second": [step.at_second for step in failover.ramp],
            "Submit TPS": [step.tps for step in failover.ramp],
            "Share of licensed": [f"{step.tps / LICENSED_TPS:.0%}"
                                  for step in failover.ramp],
        })

        st.markdown("#### Every primary state against the same backlog")
        rows = []
        for state in PRIMARY_STATES:
            outcome = simulate_smpp_carrier_failover(pending, state,
                                                     secondary)
            level = ("pass" if outcome.route == ROUTE_PRIMARY
                     else "fail" if outcome.route == ROUTE_HELD else "warn")
            rows.append(_stage(
                level, state,
                f"{outcome.route}. {outcome.submitting_tps} TPS, "
                f"{outcome.dlr_at_risk} receipt(s) stranded."))
        st.markdown(f'<div class="app-card">{"".join(rows)}</div>',
                    unsafe_allow_html=True)

        for finding in failover.findings:
            _finding_card(finding)

    # -- 3. architecture ---------------------------------------------------
    with stack_tab:
        st.markdown(
            "#### All three speak SMPP 3.4, so that decides nothing\n\n"
            "What decides is who owns the edges: a receipt carrying an "
            "identifier the gateway never issued, a carrier that unbinds "
            "with the window half full, a sequence number that wraps. A "
            "custom build re earns every one of those, one production "
            "incident at a time.")

        matrix = get_telecom_architecture_matrix()
        facts = compare_stacks()

        st.table({
            "Stack": [s.name for s in matrix],
            "Built in": [s.language for s in matrix],
            "Speaks SMPP 3.4": ["yes" if s.speaks_smpp_34 else "no"
                                for s in matrix],
            "Delivery": [f"{s.delivery_weeks_low} to "
                         f"{s.delivery_weeks_high} weeks" for s in matrix],
        })

        for option in matrix:
            st.markdown(
                f'<div class="app-card info">\n'
                f"  <h4>{esc(option.name)}</h4>\n"
                f"  <p><b>Protocol:</b> "
                f"{esc(option.protocol_compliance)}</p>\n"
                f"  <p><b>Scalability:</b> {esc(option.scalability)}</p>\n"
                f"  <p><b>Choose it when:</b> "
                f"{esc(option.choose_when)}</p>\n"
                f"  <p><b>Avoid it when:</b> {esc(option.avoid_when)}</p>\n"
                f'  <div class="app-ev">Edges you own: '
                f"{esc(option.edge_cases_owned)}</div>\n"
                f"</div>",
                unsafe_allow_html=True)

        st.caption(
            f"Ranked by delivery the order is "
            f"{', '.join(facts['by_delivery'])}. The custom build starts at "
            f"{facts['custom_multiple']} times the fastest option's floor, "
            f"and that floor is the optimistic end of it. "
            f"{facts['the_axis_that_decides']}")

    st.markdown(
        f'<div class="app-foot">Engine {esc(ENGINE_VERSION)}. A timed out '
        f"USSD step returns no deliverable payload at all, because the "
        f"network has already released the session and the text the "
        f"application would have sent belongs in the log rather than on "
        f"the wire. The rate ramp starts at {reconnect_ramp()[0].tps} TPS "
        f"and reaches the licensed {LICENSED_TPS} only after the carrier "
        f"has held the bind, because dumping a backlog into a session the "
        f"carrier has just accepted is what gets it throttled.</div>",
        unsafe_allow_html=True)
