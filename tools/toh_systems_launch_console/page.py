"""TOH Systems and Commerce Launch Console.

Rendered inside the hub app. Every number on this page is computed from the
controls on each run, so nothing on screen can describe a state that was
replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.toh_systems_launch_console.core import (
    COLOR_CRIT,
    COLOR_OK,
    COLOR_WARN,
    DAMAGE_CLAIM_WINDOW_DAYS,
    ENGINE_VERSION,
    FRESH_WITHIN_MINUTES,
    ITEM_CONDITIONS,
    RETURN_WINDOW_DAYS,
    SAMPLE_TILES,
    STALE_AFTER_MINUTES,
    dashboard_board,
    evaluate_dashboard_freshness,
    get_system_of_record_map,
    pilot_supervision_rate,
    simulate_supervised_agent_pilot,
)

DIRECTION_TONE = {
    "one way out": "ok",
    "two way": "warn",
    "read only": "info",
}


def _kpis(items: list[tuple[str, str]]) -> None:
    cells = "".join(
        f'<div class="app-kpi"><b>{esc(value)}</b><span>{esc(label)}</span></div>'
        for label, value in items)
    st.markdown(f'<div class="app-kpis">{cells}</div>', unsafe_allow_html=True)


def _reading_card(reading) -> None:
    st.markdown(
        f"""
<div class="app-card {reading.alert_color}">
  <h4><span class="app-tag {reading.alert_color}">{esc(reading.status)}</span>
  {esc(reading.source)}</h4>
  <p>{esc(reading.headline)}.</p>
  <div class="app-ev">{esc(reading.fix)}</div>
</div>
""",
        unsafe_allow_html=True,
    )


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>TOH Systems and Commerce Launch Console</h1>
  <p>Three questions a launch has to answer before it goes live. Which system
  owns each record, so two systems never both believe they own the customer.
  Whether the number on the dashboard is current, because a figure with no
  sync time is read as current however old it is. And whether the customer
  care agent may send what it drafted, which in this pilot it usually may
  not. Each answer here is computed from the rules, not asserted.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **System of record**: read one owner per entity and the rule "
            "that settles a conflict.\n"
            "2. **Dashboard freshness**: move the sync age and the two counts "
            "and watch a tile go amber, then red.\n"
            "3. **Agent pilot**: change the condition and the days, then "
            "engage the kill switch and see every path held."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. No order is read, no "
            "customer record is written and no reply is sent to anyone."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_map, tab_data, tab_agent = st.tabs(
        ["System of Record", "Dashboard Freshness", "Supervised Agent Pilot"])

    # -----------------------------------------------------------------
    with tab_map:
        rows = get_system_of_record_map()
        st.subheader("One owner per entity")
        st.caption(
            "Two systems that both believe they own a record will both write "
            "to it, and the last writer wins by accident rather than by "
            "design. Every arrow below points away from its owner."
        )
        _kpis([
            ("Entities mapped", str(len(rows))),
            ("Master systems", str(len({r.master_system for r in rows}))),
            ("Two way syncs", str(sum(1 for r in rows
                                      if r.sync_direction == "two way"))),
        ])

        for row in rows:
            tone = DIRECTION_TONE.get(row.sync_direction, "info")
            readers = ", ".join(row.readers)
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(row.sync_direction)}</span>
  {esc(row.entity)}</h4>
  <p><b>{esc(row.master_system)}</b> owns it, keyed on
  <code>{esc(row.key_field)}</code>, and it flows to {esc(readers)}.</p>
  <div class="app-ev">{esc(row.conflict_rule)}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.subheader("The same map as a flow")
        lines = "\n".join(
            f"{row.master_system} owns {row.entity} to {', '.join(row.readers)}"
            for row in rows)
        st.code(lines, language="text")

    # -----------------------------------------------------------------
    with tab_data:
        st.subheader("Is the tile telling the truth")
        st.caption(
            f"Current inside {FRESH_WITHIN_MINUTES} minutes, late past that, "
            f"and wrong past {STALE_AFTER_MINUTES}. A count that disagrees "
            "with its source is red whatever its age, because a fresh wrong "
            "number is the one a person acts on."
        )

        left, right = st.columns(2)
        with left:
            source_name = st.text_input("Tile", value="Stock on hand")
            minutes = st.slider("Minutes since last sync", 0, 240, 38)
        with right:
            source_count = st.number_input("Count in the source system",
                                           min_value=0, value=9_612, step=1)
            target_count = st.number_input("Count on the dashboard",
                                           min_value=0, value=9_612, step=1)

        reading = evaluate_dashboard_freshness(
            source_name, int(minutes), int(source_count), int(target_count))
        _kpis([
            ("Status", reading.status),
            ("Variance", f"{reading.variance:+d} records"),
            ("Drift", f"{reading.variance_pct:.2f} percent"),
            ("Blocks launch", "yes" if reading.blocks_launch else "no"),
        ])
        _reading_card(reading)

        st.subheader("The launch board")
        board = dashboard_board(SAMPLE_TILES)
        blocking = sum(1 for r in board if r.blocks_launch)
        watch = sum(1 for r in board if r.alert_color == COLOR_WARN)
        clear = sum(1 for r in board if r.alert_color == COLOR_OK)
        _kpis([
            ("Tiles", str(len(board))),
            ("Blocking", str(blocking)),
            ("Watch", str(watch)),
            ("Clear", str(clear)),
        ])
        st.caption(
            f"{blocking} blocking plus {watch} watch plus {clear} clear "
            f"equals {len(board)} tiles, which is every tile on the board."
        )
        for entry in board:
            _reading_card(entry)

    # -----------------------------------------------------------------
    with tab_agent:
        st.subheader("Drafted by the agent, sent by a person")
        st.caption(
            f"Returns run for {RETURN_WINDOW_DAYS} days and damage claims for "
            f"{DAMAGE_CLAIM_WINDOW_DAYS}. Exactly one path leaves without a "
            "person reading it: an unopened item inside the window, where the "
            "policy holds no judgement for the agent to get wrong."
        )

        left, right = st.columns(2)
        with left:
            order_id = st.text_input("Order reference", value="TOH-10241")
            condition = st.selectbox("Item condition", list(ITEM_CONDITIONS))
        with right:
            days = st.slider("Days since delivery", 0, 120, 4)
            engaged = st.toggle("Engage the kill switch", value=False)

        decision = simulate_supervised_agent_pilot(
            order_id, int(days), condition, kill_switch_engaged=bool(engaged))
        tone = COLOR_OK if decision.eligible else COLOR_CRIT
        if decision.requires_human_approval and decision.eligible:
            tone = COLOR_WARN

        _kpis([
            ("Policy", decision.policy_code),
            ("Eligible", "yes" if decision.eligible else "no"),
            ("Human approval", "required" if decision.requires_human_approval
             else "not required"),
            ("Kill switch", decision.kill_switch),
        ])

        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(decision.policy_code)}</span>
  {esc(decision.outcome)}</h4>
  <p>Order {esc(decision.order_id)} on day {int(days)},
  condition {esc(condition)}.</p>
  <div class="app-ev">Sends without a person:
  {'yes' if decision.sends_without_a_person else 'no'}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**Drafted reply**")
        st.text_area("Drafted reply", value=decision.drafted_response,
                     height=170, label_visibility="collapsed")

        st.markdown("**Guardrails that fired**")
        for note in decision.guardrails:
            st.markdown(
                f'<div class="app-card info"><p>{esc(note)}</p></div>',
                unsafe_allow_html=True)

        st.subheader("What the pilot supervises")
        stats = pilot_supervision_rate(kill_switch_engaged=bool(engaged))
        _kpis([
            ("Sample cases", str(stats["total"])),
            ("Held for a person", str(stats["supervised"])),
            ("Sent automatically", str(stats["automatic"])),
            ("Supervised", f"{stats['supervised_pct']:.1f} percent"),
        ])
        st.caption(
            f"{stats['supervised']} held plus {stats['automatic']} automatic "
            f"equals {stats['total']} cases, which is the whole sample."
        )
