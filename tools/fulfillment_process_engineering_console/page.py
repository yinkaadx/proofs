"""Fulfillment Process Engineering Console.

Rendered inside the hub app. Every figure on this page is computed from the
controls on each run, so a constraint on screen can never describe a line
that was rebalanced two interactions ago.
"""

from __future__ import annotations

import math

import streamlit as st

from shared.theme import esc, inject
from tools.fulfillment_process_engineering_console.core import (
    ENGINE_VERSION,
    PATTERNS,
    PRIORITY_CONSTRAINT,
    SAMPLE_STEPS,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    UNIVERSAL_EDGE_CASES,
    UTILISATION_COMFORTABLE,
    UTILISATION_STRAINED,
    ProcessStep,
    analyze_cycle_time_bottleneck,
    generate_software_requirement,
    simulate_dispatch_capacity,
)

TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}


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
  <h1>Fulfillment Process Engineering Console</h1>
  <p>Three calculations that decide where the next hour of effort goes.
  Throughput on a serial line is set by the slowest station and by nothing
  else, so an hour saved anywhere but the constraint is an hour saved
  nowhere. Queue wait does not rise in proportion to load, it rises toward a
  wall, so a fleet run at ninety five percent is not efficient, it is a
  queue. And automating a step that is not the constraint spends engineering
  and changes no number, so the ticket carries that check inside it.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Constraint**: speed up any station that is not the "
            "constraint and watch throughput refuse to move.\n"
            "2. **Capacity**: raise the order volume slowly and watch the "
            "wait curve bend, then break.\n"
            "3. **Ticket**: mark the step as not the constraint and read "
            "what the priority line says."
        )
        st.divider()
        st.caption(
            "Nothing here reads a warehouse system or a fleet feed. It is a "
            "way of sizing a decision before a project is scoped."
        )
        st.caption(
            "The queue model assumes arrivals with no pattern and service "
            "times that vary exponentially. Real delivery is neither, so the "
            "absolute waits are a guide while the utilisation and the "
            "instability threshold hold regardless."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_line, tab_queue, tab_ticket = st.tabs(
        ["Cycle Time Constraint", "Dispatch Capacity", "Automation Ticket"])

    # -----------------------------------------------------------------
    with tab_line:
        st.subheader("Which station actually limits the line")
        steps = []
        for index, seed in enumerate(SAMPLE_STEPS):
            left, middle, right = st.columns([3, 1, 1])
            with left:
                st.markdown(f"**{seed['name']}**")
                st.caption("manual" if seed["manual"] else "already automated")
            with middle:
                minutes = st.number_input(
                    "Minutes", min_value=0.1, max_value=120.0,
                    value=float(seed["minutes"]), step=0.5,
                    key=f"min{index}")
            with right:
                resources = st.number_input(
                    "People", min_value=1, max_value=20,
                    value=int(seed["parallel_resources"]), step=1,
                    key=f"res{index}")
            steps.append(ProcessStep(seed["name"], float(minutes),
                                     int(resources), bool(seed["manual"])))

        report = analyze_cycle_time_bottleneck(steps)

        _kpis([
            ("Constraint", report.constraint_name),
            ("Throughput", f"{report.throughput_per_hour:.2f} an hour"),
            ("Lead time", f"{report.lead_time_minutes:.1f} minutes"),
            ("Line balance", f"{report.line_balance_pct:.0f} percent"),
            ("Idle per unit", f"{report.idle_minutes_per_unit:.1f} minutes"),
        ])
        st.caption(
            f"The station minutes add to {report.total_work_minutes:.2f}, "
            f"which is the lead time. The line pays for "
            f"{report.constraint_minutes * len(report.readings):.2f} station "
            f"minutes a unit, so {report.idle_minutes_per_unit:.2f} of it is "
            f"idle waiting on the constraint."
        )

        st.markdown(
            f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">CONSTRAINT</span>
  {esc(report.headline)}</h4>
  <p>Throughput is one unit per constraint cycle, whatever the other
  stations can manage. Lead time is the sum of the stations, which answers a
  different question.</p>
  <div class="app-ev">An hour saved anywhere but here is an hour saved
  nowhere.</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**Station by station**")
        for reading in report.readings:
            row_tone = "crit" if reading.is_constraint else "info"
            label = "THE CONSTRAINT" if reading.is_constraint else f"{reading.utilisation_pct:.0f}% busy"
            st.markdown(
                f"""
<div class="app-card {row_tone}">
  <h4><span class="app-tag {row_tone}">{esc(label)}</span>
  {esc(reading.step.name)}</h4>
  <p>{reading.step.minutes:.1f} minutes of work across
  {reading.step.parallel_resources} resource(s) is
  {reading.effective_minutes:.2f} minutes a unit, or
  {reading.capacity_per_hour:.1f} an hour on its own.</p>
  <div class="app-ev">{'Sets the pace for everything else'
                       if reading.is_constraint
                       else f'Idle {reading.idle_minutes_per_unit:.2f} minutes every cycle'}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("**Findings**")
        for finding in report.findings:
            _finding_card(finding)

    # -----------------------------------------------------------------
    with tab_queue:
        st.subheader("Where the queue stops being linear")
        left, right = st.columns(2)
        with left:
            order_volume = st.slider("Orders arriving per hour",
                                     0.0, 60.0, 18.0, 1.0)
            drivers = st.slider("Active drivers", 1, 30, 6, 1)
        with right:
            delivery_time = st.slider("Average minutes per delivery",
                                      5.0, 120.0, 18.0, 1.0)

        capacity = simulate_dispatch_capacity(order_volume, int(drivers),
                                              delivery_time)

        wait_label = ("no steady state" if not capacity.stable
                      else f"{capacity.average_wait_minutes:.1f} minutes")
        _kpis([
            ("Utilisation", f"{capacity.utilisation_pct:.0f} percent"),
            ("Fleet capacity",
             f"{capacity.total_capacity_per_hour:.1f} an hour"),
            ("Average wait", wait_label),
            ("Chance of waiting",
             f"{capacity.probability_of_waiting * 100:.0f} percent"),
            ("Drivers for comfort",
             str(capacity.drivers_needed_for_comfort)),
        ])

        tone = ("crit" if not capacity.stable
                or capacity.utilisation_pct >= UTILISATION_STRAINED
                else "warn" if capacity.utilisation_pct >= UTILISATION_COMFORTABLE
                else "ok")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">
  {capacity.utilisation_pct:.0f} percent</span>
  {esc(capacity.headline)}</h4>
  <p>Each driver completes {capacity.service_rate_per_driver:.2f} deliveries
  an hour, so the fleet offers {capacity.total_capacity_per_hour:.1f} against
  {capacity.order_volume:.0f} arriving.</p>
  <div class="app-ev">{'Stable, so an average wait exists'
                       if capacity.stable
                       else 'Unstable, so no average wait exists to quote'}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**The curve, at this fleet and this delivery time**")
        for share in (50, 70, 80, 85, 90, 95, 98):
            target = capacity.total_capacity_per_hour * share / 100
            row = simulate_dispatch_capacity(target, int(drivers),
                                             delivery_time)
            st.markdown(
                f"- At **{share} percent** ({target:.1f} orders an hour) the "
                f"average wait is **{row.average_wait_minutes:.1f} minutes**")
        st.caption(
            "Fifty to seventy adds a little. Ninety to ninety eight "
            "multiplies it. Planning from a linear assumption is how a fleet "
            "that looked fine last month has a backlog this month."
        )

        st.markdown("**Findings**")
        for finding in capacity.findings:
            _finding_card(finding)

        st.subheader("What this model assumes")
        for line in capacity.assumptions:
            st.markdown(f"- {line}")

    # -----------------------------------------------------------------
    with tab_ticket:
        st.subheader("A ticket that knows whether it is worth writing")
        left, right = st.columns(2)
        with left:
            bottleneck = st.selectbox(
                "The manual step", [p.label for p in PATTERNS])
            custom = st.text_input("Or describe your own", value="")
        with right:
            is_constraint = st.toggle(
                "This step is the constraint on the line", value=True)
            st.caption(
                "Turn this off to see what the priority line says about "
                "automating a step that is not the constraint."
            )

        target = custom.strip() or bottleneck
        requirement = generate_software_requirement(target, bool(is_constraint))

        _kpis([
            ("Priority", requirement.priority),
            ("Matched a pattern", "yes" if requirement.matched else "no"),
            ("Edge cases", str(requirement.edge_case_count)),
            ("Saving per unit",
             f"{requirement.minutes_saved_per_unit:.1f} minutes"),
        ])

        tone = "ok" if is_constraint else "crit"
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(requirement.priority)}</span>
  {esc(requirement.title)}</h4>
  <p>{esc(requirement.trigger_event
          or 'No trigger was written, because this step matched no known pattern.')}</p>
  <div class="app-ev">{esc(requirement.automated_action
                           or 'Write the action by hand using the four universal edge cases.')}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**The ticket, ready to paste**")
        st.code(requirement.ticket, language="text")

        st.markdown("**Edge cases in full**")
        for index, case in enumerate(requirement.edge_cases, start=1):
            universal = case in UNIVERSAL_EDGE_CASES
            st.markdown(
                f"{index}. {case} "
                f"{'*(applies to every automation)*' if universal else ''}")

        st.markdown("**Findings**")
        for finding in requirement.findings:
            _finding_card(finding)
