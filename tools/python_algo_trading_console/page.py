"""Python Algorithmic Trading Console.

Rendered inside the hub app. Every figure on this page is computed from the
controls on each run, and nothing on it contacts a broker, reads market data
or holds a credential.
"""

from __future__ import annotations

import json

import streamlit as st

from shared.theme import esc, inject
from tools.python_algo_trading_console.core import (
    ADVERSARIAL_DAYS,
    ARM_TOKEN,
    ENGINE_VERSION,
    ENVIRONMENTS,
    ENV_LIVE,
    MAX_EXPOSURE_PERCENT,
    MAX_RISK_PERCENT,
    MILESTONE_TIERS,
    MODES,
    MODE_LIVE,
    SAMPLE_ORDER,
    SETUP_QUALITIES,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    STATUS_BLOCKED,
    STATUS_ROUTED_LIVE,
    STATUS_SIMULATED,
    TIER_NAMES,
    calculate_dynamic_position_size,
    replay_ratchet,
    route_broker_execution,
    simulate_profit_ratchet,
)

TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}
STATUS_TONE = {STATUS_SIMULATED: "ok", STATUS_ROUTED_LIVE: "crit",
               STATUS_BLOCKED: "warn"}


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
  <h1>Python Algorithmic Trading Console</h1>
  <p>Three pieces of a trading system where a bug costs money directly, each
  built to fail in the safe direction. The stated risk percent is a ceiling,
  so a setup grade may only scale a position down and never up. A protected
  floor that can move backward is not a floor, so it is tested against a loss
  larger than every gain before it. And execution defaults to deny: three
  things have to agree before an order would reach a broker, and every other
  combination is blocked.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Sizing**: ask for 25 percent risk and watch it clamp "
            "rather than obey.\n"
            "2. **Ratchet**: replay the adversarial sequence and check the "
            "floor never steps down.\n"
            "3. **Routing**: try every combination of mode, environment and "
            "arm token. Exactly one of them reaches a broker."
        )
        st.divider()
        st.caption(
            "Nothing here places an order, contacts a broker, reads market "
            "data or holds a credential. It is an engineering demonstration "
            "of the safety properties."
        )
        st.caption(
            "It is not financial advice and it is not a trading system. Use "
            "your own risk parameters and your own judgement."
        )
        st.caption(
            f"Hard caps: {MAX_RISK_PERCENT} percent risk on one trade, "
            f"{MAX_EXPOSURE_PERCENT} percent of the account in one position."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_size, tab_ratchet, tab_route = st.tabs(
        ["Position Sizing", "Profit Ratchet", "Execution Router"])

    # -----------------------------------------------------------------
    with tab_size:
        st.subheader("Quality scales the size down, never up")
        left, right = st.columns(2)
        with left:
            equity = st.number_input("Account equity", min_value=0.0,
                                     max_value=10_000_000.0, value=50_000.0,
                                     step=1000.0, format="%.2f")
            risk_percent = st.slider("Risk percent requested per trade",
                                     0.0, 10.0, 1.0, 0.1)
        with right:
            quality = st.selectbox("Setup quality",
                                   [name for name, _m, _n in SETUP_QUALITIES])
            st.caption(
                f"Every grade multiplier is at or below one. That is the "
                f"property that keeps the {MAX_RISK_PERCENT} percent cap a "
                "cap."
            )

        sizing = calculate_dynamic_position_size(f"{equity:.2f}",
                                                 risk_percent, quality)

        _kpis([
            ("Risk on this trade", f"{sizing.risk_amount}"),
            ("As a percent of equity",
             f"{sizing.effective_risk_percent} percent"),
            ("Grade multiplier", f"{sizing.quality_multiplier}"),
            ("Exposure cap", f"{sizing.max_exposure_amount}"),
            ("Within the hard cap",
             "yes" if sizing.within_hard_cap else "no"),
        ])
        st.caption(
            f"Requested {sizing.requested_risk_percent} percent, applied "
            f"{sizing.applied_risk_percent} percent, base risk "
            f"{sizing.base_risk_amount}, scaled by "
            f"{sizing.quality_multiplier} to {sizing.risk_amount}."
        )

        tone = "crit" if sizing.risk_was_clamped else (
            "ok" if sizing.tradeable else "info")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">
  {'CLAMPED' if sizing.risk_was_clamped else sizing.setup_quality}</span>
  {esc(sizing.headline)}</h4>
  <p>{'The request was not honoured. The cap was applied instead.'
      if sizing.risk_was_clamped
      else 'The request is inside the cap and was applied as asked.'}</p>
  <div class="app-ev">Tradeable:
  {'yes' if sizing.tradeable else 'no, this sizes to zero'}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**Findings**")
        for finding in sizing.findings:
            _finding_card(finding)

        st.subheader("The same account at every grade")
        for name, multiplier, note in SETUP_QUALITIES:
            row = calculate_dynamic_position_size(f"{equity:.2f}",
                                                  risk_percent, name)
            st.markdown(
                f"- **{name}** at {multiplier}: risks {row.risk_amount}, "
                f"{row.effective_risk_percent} percent of the account")

    # -----------------------------------------------------------------
    with tab_ratchet:
        st.subheader("A floor that only ever moves one way")
        left, right = st.columns(2)
        with left:
            tier = st.selectbox("Milestone tier", list(TIER_NAMES), index=1)
            daily_pnl = st.number_input("Today's profit or loss",
                                        min_value=-50_000.0,
                                        max_value=50_000.0, value=-4500.0,
                                        step=100.0, format="%.2f")
        with right:
            previous_floor = st.number_input("Protected floor before today",
                                             min_value=0.0,
                                             max_value=1_000_000.0,
                                             value=1500.0, step=100.0,
                                             format="%.2f")
            peak = st.number_input("Highest cumulative profit so far",
                                   min_value=0.0, max_value=1_000_000.0,
                                   value=3000.0, step=100.0, format="%.2f")

        state = simulate_profit_ratchet(
            f"{daily_pnl:.2f}", tier, f"{previous_floor:.2f}",
            f"{peak:.2f}", f"{peak:.2f}")

        _kpis([
            ("Floor before", f"{state.previous_floor}"),
            ("Floor now", f"{state.protected_floor}"),
            ("Moved", "up" if state.floor_moved else "not at all"),
            ("Moved backward",
             "yes" if state.floor_moved_backward else "no, and it cannot"),
            ("Peak profit", f"{state.peak_profit}"),
        ])

        tone = "crit" if state.floor_moved_backward else "ok"
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(state.milestone_tier)}</span>
  {esc(state.headline)}</h4>
  <p>This tier locks {state.lock_ratio} of a peak above
  {state.tier_threshold}, giving a candidate of {state.candidate_floor}. The
  floor is the larger of that and where it already was.</p>
  <div class="app-ev">A loss changes the running total and cannot touch the
  floor, however large it is.</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**Findings**")
        for finding in state.findings:
            _finding_card(finding)

        st.subheader("The tier ladder")
        for name, threshold, lock in MILESTONE_TIERS:
            if not lock:
                continue
            st.markdown(
                f"- **{name}** locks {lock * 100:.0f} percent once the peak "
                f"passes {threshold}")
        st.caption(
            "No lock ratio reaches one, because a floor equal to the balance "
            "is a system that cannot place another trade."
        )

        st.subheader("Replayed against a sequence written to break it")
        ledger = replay_ratchet(ADVERSARIAL_DAYS, tier)
        _kpis([
            ("Days", str(ledger["days"])),
            ("Peak reached", f"{ledger['peak']}"),
            ("Cumulative at the end", f"{ledger['final_cumulative']}"),
            ("Final floor", f"{ledger['final_floor']}"),
            ("Floor ever stepped down",
             "yes" if ledger["ever_moved_backward"] else "no"),
        ])
        st.markdown(
            "- " + "\n- ".join(
                f"Day {index + 1}: {pnl} gives a floor of {row.protected_floor}"
                for index, (pnl, row) in enumerate(zip(ADVERSARIAL_DAYS,
                                                       ledger["rows"]))))
        st.caption(
            f"The sequence climbs past a tier and then gives back more than "
            f"it ever made, ending at {ledger['final_cumulative']}. The floor "
            f"is monotonic across the whole run: "
            f"{'confirmed' if ledger['monotonic'] else 'broken'}."
        )

    # -----------------------------------------------------------------
    with tab_route:
        st.subheader("Default deny, and three things have to agree")
        left, right = st.columns(2)
        with left:
            mode = st.selectbox("Mode the strategy asked for", list(MODES))
            environment = st.selectbox("Environment flag", list(ENVIRONMENTS))
        with right:
            armed = st.toggle("Arm token presented", value=False)
            symbol = st.text_input("Symbol", value=str(SAMPLE_ORDER["symbol"]))
            quantity = st.text_input("Quantity",
                                     value=str(SAMPLE_ORDER["quantity"]))

        payload = {"symbol": symbol, "side": "buy", "quantity": quantity}
        result = route_broker_execution(
            mode, payload, environment, ARM_TOKEN if armed else "")

        _kpis([
            ("Status", result.status),
            ("Would reach a broker",
             "yes" if result.reached_broker else "no"),
            ("Mode", result.mode),
            ("Environment", result.environment),
            ("Armed", "yes" if armed else "no"),
        ])

        tone = STATUS_TONE.get(result.status, "warn")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(result.status)}</span>
  {esc(result.reason)}</h4>
  <p>{'Mode, environment and arm token all agree, so a real router would '
      'send this one.' if result.reached_broker
      else 'Nothing would leave the process in this state.'}</p>
  <div class="app-ev">{esc(result.audit_line)}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**The payload as the router sees it**")
        st.code(json.dumps(result.order_payload, indent=2), language="json")

        st.markdown("**Findings**")
        for finding in result.findings:
            _finding_card(finding)

        st.subheader("Every combination of the three gates")
        for candidate_mode in MODES:
            for candidate_env in ENVIRONMENTS:
                for token_present in (False, True):
                    row = route_broker_execution(
                        candidate_mode, SAMPLE_ORDER, candidate_env,
                        ARM_TOKEN if token_present else "")
                    marker = "**reaches a broker**" if row.reached_broker else row.status.lower()
                    st.markdown(
                        f"- mode {candidate_mode}, environment "
                        f"{candidate_env}, armed "
                        f"{'yes' if token_present else 'no'}: {marker}")
        st.caption(
            "Eight combinations, and exactly one of them reaches a broker. "
            "The test suite enumerates the same cross product and fails if "
            "that count is ever anything other than one."
        )
