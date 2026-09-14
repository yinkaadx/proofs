"""TradingView MT5 Bridge Diagnostic Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine can sit behind the real bridge process.
"""

from __future__ import annotations

import json

import streamlit as st

from shared.theme import esc, inject
from tools.tv_mt5_bridge_diagnostic.core import (
    CAUSE_GENUINE,
    CAUSE_NONE,
    ENGINE_VERSION,
    ERROR_HEADLINE,
    SAMPLE_PAYLOAD,
    SYMBOLS,
    AlertParseError,
    Latency,
    build_trail,
    diagnose_stop_out,
    parse_alert,
    simulate_execution,
    symbol_by_name,
    trail_rows,
)

STATE = "tv_mt5_state"

BROKEN_PAYLOADS = {
    "Valid alert": SAMPLE_PAYLOAD,
    "Unsubstituted placeholder": json.dumps({
        "action": "buy", "ticker": "EURUSD", "price": "{{close}}",
        "sl": 1.08340, "tp": 1.08940, "contracts": 1.0,
        "strategy": "London breakout v3", "time": "2026-09-12T08:30:00Z",
    }, indent=2),
    "Trailing comma": (
        '{\n  "action": "buy",\n  "ticker": "EURUSD",\n  "price": 1.08540,\n'
        '  "sl": 1.08340,\n  "tp": 1.08940,\n}'
    ),
    "Missing stop loss": json.dumps({
        "action": "buy", "ticker": "EURUSD", "price": 1.08540,
        "tp": 1.08940, "contracts": 1.0,
        "time": "2026-09-12T08:30:00Z",
    }, indent=2),
    "Stop on the wrong side": json.dumps({
        "action": "buy", "ticker": "EURUSD", "price": 1.08540,
        "sl": 1.08740, "tp": 1.08940, "contracts": 1.0,
        "time": "2026-09-12T08:30:00Z",
    }, indent=2),
}

PRESET_ORDER = list(BROKEN_PAYLOADS)


def _state() -> dict:
    if STATE not in st.session_state:
        st.session_state[STATE] = {
            "alert": None,
            "error": None,
            "execution": None,
            "diagnosis": None,
        }
    return st.session_state[STATE]


def _parse(body_key: str) -> None:
    """Read the payload exactly as the bridge would, and keep the refusal.

    The body is read from session state rather than from an argument bound at
    render time, because an argument captured on the previous render is one
    interaction stale: the user edits the box, clicks parse, and the old text
    is what gets parsed.
    """
    raw = str(st.session_state.get(body_key, ""))
    state = _state()
    state["execution"] = None
    state["diagnosis"] = None
    try:
        state["alert"] = parse_alert(raw)
        state["error"] = None
    except AlertParseError as exc:
        state["alert"] = None
        state["error"] = exc


def _execute() -> None:
    state = _state()
    alert = state["alert"]
    if alert is None:
        return
    symbol = symbol_by_name(alert.ticker) or SYMBOLS[0]
    latency = Latency(int(st.session_state.get("tv_webhook_ms", 0)),
                      int(st.session_state.get("tv_bridge_ms", 0)),
                      int(st.session_state.get("tv_broker_ms", 0)))
    state["execution"] = simulate_execution(
        alert, symbol, float(st.session_state.get("tv_spread", 0.0)), latency,
        market_move_points=float(st.session_state.get("tv_move", 0.0)))
    state["diagnosis"] = None


def _diagnose(extreme_key: str) -> None:
    state = _state()
    execution = state["execution"]
    if execution is None:
        return
    chart_extreme = st.session_state.get(extreme_key)
    if chart_extreme is None:
        return
    state["diagnosis"] = diagnose_stop_out(execution, float(chart_extreme))


def render() -> None:
    inject()
    state = _state()

    st.markdown(
        """
<div class="app-hero">
  <h1>TradingView MT5 Bridge Diagnostic Console</h1>
  <p>An alert fires on the chart, a bridge forwards it, and MT5 fills it at a
  different price. This shows where the difference came from, millisecond by
  millisecond, and answers the question that follows a bad day: was the stop
  hit because price got there, or because the spread and the delay got there
  first.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Webhook Payload**: paste or pick an alert and parse it.\n"
            "2. **Broker Execution**: set the spread and the delay, then fill it.\n"
            "3. **Latency and Drift**: enter the worst price the chart printed.\n"
            "4. **Audit Trail**: read the sequence with the clock running."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. TradingView, the bridge and "
            "MT5 are all simulated in this session, so no order is ever placed."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_payload, tab_exec, tab_drift, tab_trail = st.tabs(
        ["Webhook Payload", "Broker Execution", "Latency and Drift",
         "Audit Trail"]
    )

    # -----------------------------------------------------------------
    # Webhook payload simulator
    # -----------------------------------------------------------------
    with tab_payload:
        st.markdown("#### The payload TradingView posts")
        st.caption(
            "TradingView sends the alert message body verbatim. Whatever is "
            "typed in the alert box is what the bridge receives, placeholders "
            "and typing mistakes included."
        )

        preset = st.selectbox("Preset payload", PRESET_ORDER, key="tv_preset")
        # The key carries the preset so choosing a different one loads that
        # payload rather than leaving the previous text in place.
        body_key = f"tv_body_{PRESET_ORDER.index(preset)}"
        st.text_area("Webhook body", value=BROKEN_PAYLOADS[preset],
                     height=260, key=body_key)

        st.button("Parse payload", type="primary", on_click=_parse,
                  args=(body_key,), key="tv_parse_btn")

        alert, error = state["alert"], state["error"]
        if error is not None:
            headline = ERROR_HEADLINE.get(error.code, "Payload refused")
            st.markdown(
                f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">{esc(headline)}</span>Alert refused</h4>
  <div class="app-ev">{esc(error.code)}</div>
  <p>{esc(error.message)}</p>
</div>
""",
                unsafe_allow_html=True,
            )
            st.error(
                "Nothing was sent to MT5. A bridge that accepts a malformed "
                "alert places a real order with a wrong level, and the first "
                "sign of trouble is a filled position nobody intended."
            )
        elif alert is not None:
            st.markdown(
                f"""
<div class="app-card ok">
  <h4><span class="app-tag ok">Parsed</span>{esc(alert.action)}
  {esc(alert.ticker)} {alert.contracts:g}</h4>
  <div class="app-ev">entry {alert.price} &nbsp; stop {alert.stop_loss}
  &nbsp; target {alert.take_profit}</div>
  <p>Strategy {esc(alert.strategy)}, fired at {esc(alert.fired_at)}. Risk to
  stop is {alert.risk_price():.5f} on the chart.</p>
</div>
""",
                unsafe_allow_html=True,
            )
        else:
            st.info("No alert parsed yet. Parse a payload to start the flow.")

    # -----------------------------------------------------------------
    # Broker execution log
    # -----------------------------------------------------------------
    with tab_exec:
        alert = state["alert"]
        if alert is None:
            st.info("Parse a payload on the first tab, then fill it here.")
        else:
            st.markdown("#### Broker quote and fill")
            st.caption(
                "The chart plots the mid. The broker quotes a bid and an ask "
                "around it. A buy pays the ask and a sell receives the bid, so "
                "a fill is never the price on the chart."
            )

            c1, c2 = st.columns(2)
            c1.number_input(
                "Spread in points", min_value=0.0, max_value=500.0, value=12.0,
                step=1.0, key="tv_spread")
            c2.number_input(
                "Market move while in flight, in points", min_value=-500.0,
                max_value=500.0, value=8.0, step=1.0, key="tv_move")

            c3, c4, c5 = st.columns(3)
            c3.number_input("TradingView to bridge, ms",
                                         min_value=0, max_value=20000,
                                         value=420, step=10, key="tv_webhook_ms")
            c4.number_input("Bridge processing, ms", min_value=0,
                                        max_value=20000, value=95, step=10,
                                        key="tv_bridge_ms")
            c5.number_input("Bridge to MT5 fill, ms", min_value=0,
                                        max_value=20000, value=310, step=10,
                                        key="tv_broker_ms")

            st.button("Execute on MT5", type="primary", on_click=_execute,
                      key="tv_execute_btn")

            execution = state["execution"]
            if execution is not None:
                symbol = execution.symbol
                tone = "warn" if execution.adverse else "ok"
                label = "Adverse fill" if execution.adverse else "Fill in favour"
                st.markdown(
                    f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(label)}</span>{esc(alert.action)}
  {esc(alert.ticker)} filled at {symbol.round(execution.fill_price)}</h4>
  <div class="app-ev">bid {symbol.round(execution.quote_at_fill.bid)}
  &nbsp; ask {symbol.round(execution.quote_at_fill.ask)}
  &nbsp; spread {execution.spread_points:.0f} points</div>
  <p>Drift from the chart price is {execution.drift_points:+.1f} points, of
  which {symbol.to_points(execution.spread_component_price):.1f} is the half
  spread paid for crossing the book and
  {symbol.to_points(abs(execution.latency_component_price)):.1f} is the market
  moving during {execution.latency.total_ms} ms in flight.</p>
</div>
""",
                    unsafe_allow_html=True,
                )

                st.markdown("##### Where the milliseconds went")
                st.dataframe(execution.latency.rows(), width="stretch",
                             hide_index=True)
                st.caption(
                    f"The slowest stage is {execution.latency.worst_stage}, "
                    f"which is where a fix is worth the most."
                )

                st.markdown("##### The MT5 execution record")
                st.code(json.dumps({
                    "symbol": symbol.name,
                    "side": alert.action,
                    "volume": alert.contracts,
                    "requested_price": alert.price,
                    "bid": symbol.round(execution.quote_at_fill.bid),
                    "ask": symbol.round(execution.quote_at_fill.ask),
                    "fill_price": symbol.round(execution.fill_price),
                    "slippage_points": execution.slippage_points,
                    "latency_ms": execution.latency.total_ms,
                    "executed_at": execution.executed_at,
                }, indent=2), language="json")

    # -----------------------------------------------------------------
    # Latency and drift analyser
    # -----------------------------------------------------------------
    with tab_drift:
        execution = state["execution"]
        if execution is None:
            st.info(
                "Fill an order on the second tab, then bring the chart's own "
                "worst price here to settle the stop out question."
            )
        else:
            alert = execution.alert
            symbol = execution.symbol
            st.markdown("#### Was the stop out premature")
            st.caption(
                "A long is stopped on the bid, which sits half a spread below "
                "the chart, so the chart only has to fall to the stop plus half "
                "the spread. That is why a stop fires at a price the chart "
                "never printed."
            )

            default_extreme = symbol.round(
                alert.stop_loss + (execution.quote_at_fill.spread_price / 4
                                   if alert.is_long
                                   else -execution.quote_at_fill.spread_price / 4))
            # The key carries the levels, so a different alert starts from a
            # sensible default instead of inheriting the previous one.
            extreme_key = (f"tv_extreme_{alert.ticker}_{alert.action}_"
                           f"{alert.stop_loss}")
            st.number_input(
                "Worst price the chart printed", value=float(default_extreme),
                step=float(symbol.point * 10), format=f"%.{symbol.digits}f",
                key=extreme_key)

            st.button("Diagnose", type="primary", on_click=_diagnose,
                      args=(extreme_key,), key="tv_diagnose_btn")

            diagnosis = state["diagnosis"]
            if diagnosis is not None:
                tone = {CAUSE_NONE: "ok", CAUSE_GENUINE: "info"}.get(
                    diagnosis.cause, "crit")
                st.markdown(
                    f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(diagnosis.headline)}</span>
  {esc(alert.ticker)} {esc(alert.action)}</h4>
  <div class="app-ev">stop fires at a chart price of
  {symbol.round(diagnosis.trigger_price)}</div>
  <p>{esc(diagnosis.explanation)}</p>
</div>
""",
                    unsafe_allow_html=True,
                )
                st.markdown("##### The arithmetic, shown rather than asserted")
                st.dataframe(diagnosis.verdict_rows, width="stretch",
                             hide_index=True)
                if diagnosis.cause not in (CAUSE_NONE, CAUSE_GENUINE):
                    st.warning(
                        f"Widening the stop by the {execution.spread_points:.0f} "
                        f"point spread would have kept this position open, and "
                        f"cutting the {execution.latency.worst_stage} stage "
                        f"would have improved the entry by "
                        f"{diagnosis.latency_cost_points:.0f} points."
                    )

    # -----------------------------------------------------------------
    # Audit trail
    # -----------------------------------------------------------------
    with tab_trail:
        execution = state["execution"]
        if execution is None:
            st.info("The trail is written when an order is executed.")
        else:
            trail = build_trail(execution, state["diagnosis"])
            st.markdown(
                f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{execution.latency.total_ms}</div>
    <div class="l">Milliseconds in flight</div></div>
  <div class="app-kpi warn"><div class="n">{execution.spread_points:.0f}</div>
    <div class="l">Spread in points</div></div>
  <div class="app-kpi {'crit' if execution.adverse else 'ok'}">
    <div class="n">{execution.slippage_points:+.1f}</div>
    <div class="l">Slippage in points</div></div>
  <div class="app-kpi"><div class="n">{len(trail)}</div>
    <div class="l">Trail entries</div></div>
</div>
""",
                unsafe_allow_html=True,
            )

            st.markdown("#### Sequence of events")
            st.dataframe(trail_rows(trail), width="stretch", hide_index=True)

            st.markdown("##### Export for the broker ticket")
            st.code(json.dumps([
                {"sequence": e.sequence, "timestamp": e.timestamp,
                 "elapsed_ms": e.elapsed_ms, "stage": e.stage,
                 "detail": e.detail}
                for e in trail
            ], indent=2), language="json")
            st.caption(
                "This is the evidence a broker ticket needs: the alert time, "
                "the fill time, the quote on both sides and the level that "
                "actually triggered."
            )

    st.markdown(
        f"""
<div class="app-foot">
TradingView MT5 Bridge Diagnostic Console, engine version {ENGINE_VERSION}. A
simulator: no TradingView webhook is received, no MT5 terminal is contacted and
nothing you enter leaves this session. A refused payload never becomes an order,
because an alert accepted with a guessed level is a real loss.
</div>
""",
        unsafe_allow_html=True,
    )
