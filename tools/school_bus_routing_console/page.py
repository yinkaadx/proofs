"""School Bus Routing and Network Analyst Console.

Rendered inside the hub app. Every figure on this page is computed from the
controls on each run, so a count on screen can never describe a school or a
route that was replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.school_bus_routing_console.core import (
    BASELINE_WALK_MILES,
    COMFORTABLE_WALK_MILES,
    COST_SPLIT,
    DEFAULT_SCHOOL_DAYS,
    DISPOSITIONS,
    DISPOSITION_ELIMINATED,
    DISPOSITION_REDEPLOYED,
    ENGINE_VERSION,
    NETWORK_TYPES,
    RUNS_PER_DAY,
    SECONDS_PER_STOP,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    calculate_network_vs_euclidean,
    generate_cost_impact_report,
    simulate_stop_consolidation,
)

TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}
DISPOSITION_TONE = {
    DISPOSITION_ELIMINATED: "ok",
    DISPOSITION_REDEPLOYED: "crit",
}
NETWORK_BY_LABEL = {n.label: n for n in NETWORK_TYPES}


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


def _assumptions(title: str, lines) -> None:
    st.subheader(title)
    for line in lines:
        st.markdown(f"- {line}")


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>School Bus Routing and Network Analyst Console</h1>
  <p>Three calculations a transportation department is asked for, and the
  place each one is usually got wrong. A walk zone drawn as the crow flies
  counts children as walkers who cannot walk it, and the ones it miscounts
  are the ones behind a highway with no crossing. Stop consolidation is sold
  as a pure win when it is paid for by families in walking. And buses saved
  is reported as money saved, which it only becomes when the bus and the
  driver actually leave the fleet.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Walk zone**: switch the network type and watch the walker "
            "count fall. It can only ever fall.\n"
            "2. **Stops**: push the walk distance past a quarter mile and "
            "read what the tool says about who pays for the saving.\n"
            "3. **Cost**: change the disposition. The gross never moves and "
            "the cash moves a great deal."
        )
        st.divider()
        st.caption(
            "Nothing here reads a real street layer or a real student list. "
            "It is a way of sizing a question before a routing run is "
            "commissioned."
        )
        st.caption(
            "Every model states its own assumptions on screen, because the "
            "circuity of a network, the dwell of a stop and the length of a "
            "school year are local facts rather than constants."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_network, tab_stops, tab_cost = st.tabs(
        ["Walk Zone Reality", "Stop Consolidation", "Cost Impact"])

    # -----------------------------------------------------------------
    with tab_network:
        st.subheader("Straight line against the street network")
        left, right = st.columns(2)
        with left:
            student_count = st.slider("Students inside the radius as the crow flies",
                                      50, 5000, 1200, 50)
            radius = st.slider("Walk radius in policy, in miles",
                               0.25, 3.0, 1.5, 0.25)
        with right:
            network_label = st.selectbox("Street network type",
                                         [n.label for n in NETWORK_TYPES],
                                         index=1)
            chosen = NETWORK_BY_LABEL[network_label]
            st.caption(f"Circuity {chosen.circuity}. {chosen.note}")

        split = calculate_network_vs_euclidean(
            int(student_count), float(radius), chosen)

        _kpis([
            ("Called walkers by straight line", str(split.walkers_straight_line)),
            ("Can actually walk it", str(split.walkers_network)),
            ("Need a seat", str(split.reclassified_to_bus)),
            ("Cut off by a barrier", str(split.barrier_isolated)),
            ("Walk zone shrinks by", f"{split.walk_zone_shrink_pct} percent"),
        ])
        st.caption(
            f"{split.walkers_network} walkers plus "
            f"{split.bus_eligible_network} bus eligible equals "
            f"{split.student_count} students, which is everyone inside the "
            f"radius. The reachable radius on this network is "
            f"{split.effective_radius_miles} miles rather than "
            f"{split.radius_miles}."
        )

        st.markdown(
            f"""
<div class="app-card warn">
  <h4><span class="app-tag warn">{esc(split.network_type.label)}</span>
  {esc(split.headline)}</h4>
  <p>{esc(split.network_type.note)}</p>
  <div class="app-ev">A street is never shorter than the straight line it
  follows, so this number can only move one way.</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**Findings**")
        for finding in split.findings:
            _finding_card(finding)

        st.subheader("The same school on each network type")
        for network in NETWORK_TYPES:
            row = calculate_network_vs_euclidean(
                int(student_count), float(radius), network)
            st.markdown(
                f"- **{network.label}** at circuity {network.circuity}: "
                f"{row.walkers_network} walk, {row.reclassified_to_bus} need "
                f"a seat, {row.barrier_isolated} of them cut off entirely")

        _assumptions("What this estimate rests on", split.assumptions)

    # -----------------------------------------------------------------
    with tab_stops:
        st.subheader("Fewer stops, and who pays for them")
        left, right = st.columns(2)
        with left:
            current_stops = st.slider("Stops on the route today", 5, 200, 80, 5)
        with right:
            max_walk = st.slider("Furthest a child walks to a stop, in miles",
                                 0.05, 0.60, 0.25, 0.05)
            st.caption(
                f"The current stop list is assumed to reflect a "
                f"{BASELINE_WALK_MILES:.2f} mile walk. Past "
                f"{COMFORTABLE_WALK_MILES:.2f} miles the tool objects."
            )

        plan = simulate_stop_consolidation(int(current_stops), float(max_walk))

        _kpis([
            ("Stops today", str(plan.current_stops)),
            ("Stops after", str(plan.optimized_stops)),
            ("Removed", f"{plan.stops_removed} ({plan.removal_pct} percent)"),
            ("Minutes a day", str(plan.daily_minutes_saved)),
            ("Hours a year", str(plan.annual_hours_saved)),
        ])
        st.caption(
            f"{plan.optimized_stops} kept plus {plan.stops_removed} removed "
            f"equals {plan.current_stops} stops, which is the whole route. "
            f"At {SECONDS_PER_STOP} seconds of dwell across "
            f"{RUNS_PER_DAY} runs."
        )

        tone = "crit" if max_walk > COMFORTABLE_WALK_MILES else "ok"
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{plan.stops_removed} removed</span>
  {esc(plan.headline)}</h4>
  <p>The time comes off the route. The walking goes onto families, on the
  same streets the route was arranged to avoid.</p>
  <div class="app-ev">Walk audit every merged catchment before the stop list
  is published.</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**Findings**")
        for finding in plan.findings:
            _finding_card(finding)

        st.subheader("How the walk distance trades against the stop count")
        for walk in (0.10, 0.15, 0.20, 0.25, 0.30, 0.40):
            row = simulate_stop_consolidation(int(current_stops), walk)
            flag = " (past what the youngest can do)" if walk > COMFORTABLE_WALK_MILES else ""
            st.markdown(
                f"- A **{walk:.2f} mile** walk gives {row.optimized_stops} "
                f"stops and {row.daily_minutes_saved} minutes a day{flag}")

        _assumptions("What this estimate rests on", plan.assumptions)

    # -----------------------------------------------------------------
    with tab_cost:
        st.subheader("Buses removed, and what of it is actually money")
        left, right = st.columns(2)
        with left:
            buses_saved = st.slider("Buses coming off the schedule", 0, 25, 3, 1)
            daily_cost = st.number_input("Fully loaded daily cost per bus",
                                         min_value=50.0, max_value=3000.0,
                                         value=515.00, step=5.0, format="%.2f")
        with right:
            disposition = st.selectbox("What happens to the bus and the driver",
                                       list(DISPOSITIONS))
            school_days = st.slider("School days in the year", 150, 200,
                                    DEFAULT_SCHOOL_DAYS, 1)

        impact = generate_cost_impact_report(
            int(buses_saved), f"{daily_cost:.2f}", disposition,
            int(school_days))

        _kpis([
            ("Gross annual", f"{impact.gross_annual_saving}"),
            ("Cash leaving the budget", f"{impact.cash_annual_saving}"),
            ("Held as capacity", f"{impact.capacity_annual_value}"),
            ("Per day", f"{impact.gross_daily_saving}"),
        ])
        st.caption(
            f"Cash {impact.cash_annual_saving} plus capacity "
            f"{impact.capacity_annual_value} equals the gross "
            f"{impact.gross_annual_saving}, and the component rows below add "
            f"to {impact.rows_annual_total}, which is the same figure."
        )

        tone = DISPOSITION_TONE.get(impact.disposition, "warn")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(impact.disposition)}</span>
  {esc(impact.headline)}</h4>
  <p>The gross figure does not move when the disposition changes. The cash
  figure moves a great deal, and it is the cash figure a budget is built
  from.</p>
  <div class="app-ev">Decide the disposition before the number is
  presented, not after it is questioned.</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**Where the money sits**")
        for row in impact.rows:
            state = ("stops, so this is cash" if row.realised
                     else "continues, so this is not cash")
            st.markdown(
                f"- **{row.component}** at {row.share * 100:.0f} percent: "
                f"{row.annual_amount} a year, {state}")

        st.markdown("**The leadership summary**")
        for label, value in impact.leadership_summary:
            st.markdown(f"- **{label}**: {value}")

        st.markdown("**Findings**")
        for finding in impact.findings:
            _finding_card(finding)

        st.subheader("The same removal under each disposition")
        for option in DISPOSITIONS:
            row = generate_cost_impact_report(
                int(buses_saved), f"{daily_cost:.2f}", option,
                int(school_days))
            st.markdown(
                f"- **{option}**: {row.cash_annual_saving} cash, "
                f"{row.capacity_annual_value} capacity")

        _assumptions("What this estimate rests on", impact.assumptions)
