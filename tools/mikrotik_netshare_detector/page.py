"""MikroTik NetShare Detection Console.

Rendered inside the hub app. Every verdict on this page is computed from the
controls on each run, so a detection on screen can never describe a
subscriber whose readings were replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.mikrotik_netshare_detector.core import (
    ACTIONS,
    ACTION_BLOCK,
    API_PORT_PLAIN,
    API_PORT_TLS,
    AUTO_ACTION_THRESHOLD,
    DETECTION_THRESHOLD,
    ENGINE_VERSION,
    NATIVE_TTLS,
    ONE_HOP_TTLS,
    OUTCOME_ALREADY,
    OUTCOME_REJECTED,
    PORT_BREADTH_THRESHOLD,
    POLL_INTERVAL_SECONDS,
    SAMPLE_SUBSCRIBERS,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    STATUSES,
    STATUS_ACTIVE,
    STATUS_BLOCKED,
    analyze_tethering_heuristics,
    manage_voucher_status,
    simulate_api_polling_cycle,
)

TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}
CONFIDENCE_TONE = {"NONE": "ok", "LOW": "info", "MODERATE": "warn",
                   "HIGH": "crit"}
STATUS_TONE = {STATUS_ACTIVE: "ok", STATUS_BLOCKED: "crit"}


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
  <h1>MikroTik NetShare Detection Console</h1>
  <p>Detecting a shared connection on a hotspot is heuristic work, and the
  honest version says so on every screen. The cost of being wrong is not a
  log line. It is a paying customer cut off mid session, at a counter, by a
  rule nobody on the desk can explain to them. So the MAC address is scored
  at nothing, because everything behind a tethering phone shares it. One weak
  signal never reaches a detection. And the bar for acting without a person
  sits above anything the weak signals can reach together.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Heuristics**: set TTL to a native value and push both weak "
            "signals up. Two weak signals flag, and still cannot auto block.\n"
            "2. **Polling**: read why the cycle opens and closes rather than "
            "holding a connection.\n"
            "3. **Vouchers**: send the same action twice and watch nothing "
            "happen the second time."
        )
        st.divider()
        st.caption(
            "Nothing here contacts a router. No API session is opened, no "
            "subscriber is read and no voucher is changed anywhere."
        )
        st.caption(
            f"Native TTL starts are {', '.join(str(t) for t in NATIVE_TTLS)}. "
            f"One hop below each is "
            f"{', '.join(str(t) for t in ONE_HOP_TTLS)}, which is what a "
            "phone sharing its connection produces."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_detect, tab_poll, tab_voucher = st.tabs(
        ["Tethering Heuristics", "API Polling Cycle", "Voucher Interface"])

    # -----------------------------------------------------------------
    with tab_detect:
        st.subheader("Weigh the signals without claiming proof")
        left, right = st.columns(2)
        with left:
            mac = st.text_input("Subscriber MAC address",
                                value="DA:11:9C:55:66:77")
            ttl = st.slider("TTL seen at the access point", 1, 255, 63, 1)
        with right:
            agents = st.slider("Distinct operating system families seen",
                               0, 5, 2, 1)
            ports = st.slider("Concurrent connections", 0, 500, 210, 10)

        try:
            verdict = analyze_tethering_heuristics(mac, int(ttl), int(agents),
                                                   int(ports))
        except ValueError as exc:
            st.markdown(
                f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">INPUT REJECTED</span> Nothing was scored</h4>
  <p>{esc(str(exc))}</p>
  <div class="app-ev">Correct the input and the verdict appears.</div>
</div>
""",
                unsafe_allow_html=True,
            )
        else:
            _kpis([
                ("Detected", "yes" if verdict.detected else "no"),
                ("Score", f"{verdict.score} of {DETECTION_THRESHOLD} needed"),
                ("Confidence", verdict.confidence),
                ("Rule", verdict.rule_triggered),
                ("Safe to auto block",
                 "yes" if verdict.safe_to_auto_block else "no"),
            ])
            st.caption(
                f"The triggered rows add to {verdict.score_from_signals}, "
                f"which is the score above. A flag needs "
                f"{DETECTION_THRESHOLD} points and acting without a person "
                f"needs {AUTO_ACTION_THRESHOLD}, which no combination of "
                f"weak signals alone can reach."
            )

            tone = CONFIDENCE_TONE.get(verdict.confidence, "warn")
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(verdict.confidence)}</span>
  {esc(verdict.headline)}</h4>
  <p>{esc(verdict.mac_address)},
  {'randomised address' if verdict.mac_is_randomised else 'factory address'},
  which contributes nothing to this verdict either way.</p>
  <div class="app-ev">Triggered:
  {esc(', '.join(verdict.triggered_rules) or 'nothing')}</div>
</div>
""",
                unsafe_allow_html=True,
            )

            st.markdown("**Signal by signal**")
            for signal in verdict.signals:
                sig_tone = ("crit" if signal.triggered and signal.strength == "strong"
                            else "warn" if signal.triggered else "ok")
                st.markdown(
                    f"""
<div class="app-card {sig_tone}">
  <h4><span class="app-tag {sig_tone}">
  {esc(signal.strength)} {'+' + str(signal.weight) if signal.triggered else 'not triggered'}
  </span> {esc(signal.rule)}</h4>
  <p>{esc(signal.evidence)}.</p>
  <div class="app-ev">{esc(signal.caveat)}</div>
</div>
""",
                    unsafe_allow_html=True,
                )

            st.markdown("**Findings**")
            for finding in verdict.findings:
                _finding_card(finding)

        st.subheader("Five subscribers from one poll")
        for vid, sample_mac, sample_ttl, sample_ua, sample_ports in SAMPLE_SUBSCRIBERS:
            row = analyze_tethering_heuristics(sample_mac, sample_ttl,
                                               sample_ua, sample_ports)
            action = ("auto block" if row.safe_to_auto_block else
                      "queue for review" if row.detected else "no action")
            st.markdown(
                f"- **{vid}** TTL {sample_ttl}, {sample_ua} OS family, "
                f"{sample_ports} connections: {row.score} points, "
                f"{row.confidence.lower()} confidence, **{action}**")

    # -----------------------------------------------------------------
    with tab_poll:
        st.subheader("Open it, use it, close it, wait")
        left, right = st.columns(2)
        with left:
            use_tls = st.toggle(f"Use the TLS API port {API_PORT_TLS}",
                                value=True)
        with right:
            interval = st.slider("Seconds between polls", 15, 600,
                                 POLL_INTERVAL_SECONDS, 15)

        cycle = simulate_api_polling_cycle(bool(use_tls), int(interval))

        _kpis([
            ("Port", str(cycle.port)),
            ("Interval", f"{cycle.interval_seconds} seconds"),
            ("Steps", str(len(cycle.steps))),
            ("Sockets held between cycles", str(cycle.sockets_left_open)),
            ("Closes what it opens",
             "yes" if cycle.closes_what_it_opens else "no"),
        ])

        st.markdown(
            f"""
<div class="app-card {'ok' if use_tls else 'crit'}">
  <h4><span class="app-tag {'ok' if use_tls else 'crit'}">
  {'STATELESS' if cycle.stateless else 'HELD'}</span>
  Port {cycle.port}, every {cycle.interval_seconds} seconds</h4>
  <p>{esc(cycle.rationale)}</p>
  <div class="app-ev">{'Credentials stay inside TLS.'
                       if use_tls else
                       'Credentials cross the wire in the clear on port '
                       + str(API_PORT_PLAIN) + '.'}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**One cycle, in order**")
        st.code("\n".join(cycle.log), language="text")

        st.markdown("**What each step holds**")
        for step in cycle.steps:
            held = "socket open" if step.holds_socket else "nothing held"
            st.markdown(f"{step.ordinal}. **{step.phase}**: {step.detail} ({held})")

        st.markdown("**Findings**")
        for finding in cycle.findings:
            _finding_card(finding)

    # -----------------------------------------------------------------
    with tab_voucher:
        st.subheader("Block and unblock, safe to send twice")
        left, right = st.columns(2)
        with left:
            voucher_id = st.text_input("Voucher id", value="V-1042")
            current = st.selectbox("Status on the router now", list(STATUSES))
        with right:
            action = st.selectbox("Action", list(ACTIONS))

        result = manage_voucher_status(voucher_id, action, current)
        retry = manage_voucher_status(result.voucher_id or voucher_id, action,
                                      result.status)

        _kpis([
            ("Was", result.previous_status),
            ("Now", result.status),
            ("Outcome", result.outcome),
            ("Same action again", retry.outcome),
        ])

        tone = ("crit" if result.outcome == OUTCOME_REJECTED
                else STATUS_TONE.get(result.status, "warn"))
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(result.outcome)}</span>
  {esc(result.voucher_id or 'no voucher')} is {esc(result.status)}</h4>
  <p>{'The same action sent again reports ' + esc(retry.outcome.lower())
      + ' and changes nothing, because a retry is normal traffic rather than'
        ' an error.' if result.outcome != OUTCOME_REJECTED
      else 'Nothing was applied.'}</p>
  <div class="app-ev">{esc(result.audit_line or 'no audit line, nothing happened')}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        if action == ACTION_BLOCK and result.changed:
            st.caption(
                "Somebody lost their connection at this moment, wherever they "
                "were. The redirect page is the only thing that tells them "
                "why."
            )

        st.markdown("**Findings**")
        for finding in result.findings:
            _finding_card(finding)

        st.subheader("Every combination of action and starting state")
        for start in STATUSES:
            for verb in ACTIONS:
                row = manage_voucher_status("V-0001", verb, start)
                st.markdown(
                    f"- **{verb}** on a voucher that is {start.lower()}: "
                    f"ends {row.status.lower()}, {row.outcome.lower()}")
