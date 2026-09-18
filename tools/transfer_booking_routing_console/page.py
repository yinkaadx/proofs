"""Transfer Booking and Routing Console.

Rendered inside the hub app. Every figure on this page is computed from the
controls on each run, so a quote on screen can never describe a journey that
was replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.transfer_booking_routing_console.core import (
    DISTANCE_BANDS,
    ENGINE_VERSION,
    FIXED_ROUTES,
    MINIMUM_FARE,
    OS_TYPES,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    TERMINAL_STATUSES,
    TRANSITIONS,
    VEHICLES,
    calculate_transfer_fare,
    evaluate_web_gps_limits,
    fixed_route_break_even,
    money,
    simulate_driver_status,
)

TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}
NO_ROUTE = "No fixed route, meter only"


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
  <h1>Transfer Booking and Routing Console</h1>
  <p>Three parts of a transfer platform, and one of them is the part where
  the honest answer is that the thing being asked for cannot be built the way
  it was specified. The pricing is marginal by band, so there is no cliff at a
  boundary. The driver state machine has no move that bills for a journey
  nobody took. And background GPS in a browser does not work, on either
  platform, for a reason no amount of engineering gets past.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Pricing**: hold a fixed route against the meter and change "
            "the vehicle until the flat price starts losing money.\n"
            "2. **Web GPS**: read the blocking constraint first. It is the "
            "one that decides the answer.\n"
            "3. **Driver status**: step to a terminal state and see that "
            "nothing is offered after it."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live booking system. No journey is "
            "priced for a real customer and no driver is tracked."
        )
        st.caption(
            "The browser behaviour described is structural rather than "
            "version specific. Check the current support tables before "
            "committing to a wake lock in a build plan."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_fare, tab_gps, tab_status = st.tabs(
        ["Distance Pricing", "Web GPS Limits", "Driver Status"])

    # -----------------------------------------------------------------
    with tab_fare:
        st.subheader("The meter against the flat price")
        left, right = st.columns(2)
        with left:
            vehicle_title = st.selectbox(
                "Vehicle", [v.title for v in VEHICLES])
            distance = st.slider("Distance in km", 0.5, 150.0, 28.0, 0.1)
        with right:
            # Defaults to a real route rather than to the meter alone: the
            # comparison is the point of this tab, and a first load that
            # shows nothing to compare teaches nobody anything.
            route_name = st.selectbox(
                "Fixed route to compare against",
                [r.name for r in FIXED_ROUTES] + [NO_ROUTE])
            if route_name != NO_ROUTE:
                route = next(r for r in FIXED_ROUTES if r.name == route_name)
                st.caption(
                    f"Published at {route.price} for a costed "
                    f"{route.distance_km} km."
                )

        quote = calculate_transfer_fare(
            distance, vehicle_title,
            "" if route_name == NO_ROUTE else route_name)

        kpis = [
            ("Metered fare", f"{quote.metered_fare}"),
            ("Distance charge", f"{quote.distance_charge}"),
            ("Minimum applied", "yes" if quote.minimum_applied else "no"),
        ]
        if quote.fixed_price is not None:
            kpis.append(("Flat price", f"{quote.fixed_price}"))
            kpis.append(("Meter less flat", f"{quote.difference:+}"))
        _kpis(kpis)

        st.markdown("**The breakdown**")
        for row in quote.rows:
            st.markdown(f"- {row.label}: **{row.amount}**")
        st.caption(
            f"The rows add to {quote.rows_total}, which is the subtotal of "
            f"{quote.subtotal}. The minimum fare of {money(MINIMUM_FARE)} "
            f"{'raised it to ' + str(quote.metered_fare) if quote.minimum_applied else 'did not apply'}."
        )

        tone = "crit" if quote.fixed_route_loses_money else "ok"
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(quote.vehicle.title)}</span>
  {esc(quote.vehicle.note)}</h4>
  <p>{esc(quote.headline)}.</p>
  <div class="app-ev">{quote.vehicle.seats} seats, priced at
  {quote.vehicle.multiplier} times the saloon</div>
</div>
""",
            unsafe_allow_html=True,
        )

        for finding in quote.findings:
            _finding_card(finding)

        st.subheader("The bands, and why they are marginal")
        lower = 0.0
        for band in DISTANCE_BANDS:
            upper = ("onwards" if band.upper_km == float("inf")
                     else f"to {band.upper_km:.0f} km")
            st.markdown(
                f"- **{band.label}**: {band.rate_per_km} per km, from "
                f"{lower:.0f} km {upper}")
            lower = band.upper_km if band.upper_km != float("inf") else lower
        st.caption(
            "Each rate applies only to the kilometres inside its own band. "
            "Charging the whole journey at the band it ends in would make a "
            "50.1 km run cheaper than a 49.9 km one, which customers find "
            "before finance does."
        )

        st.subheader("Every fixed route against this vehicle")
        for route in FIXED_ROUTES:
            meter = fixed_route_break_even(route, vehicle_title)
            gap = money(meter - route.price)
            verdict = ("loses " + str(gap) if gap > 0 else
                       "carries " + str(abs(gap)) if gap < 0 else "breaks even")
            st.markdown(
                f"- **{route.name}** at {route.distance_km} km: published "
                f"{route.price}, meters {meter}, {verdict}")

    # -----------------------------------------------------------------
    with tab_gps:
        st.subheader("Can a browser track a driver in the background")
        os_type = st.selectbox("Platform", list(OS_TYPES))
        assessment = evaluate_web_gps_limits(os_type)

        _kpis([
            ("Verdict", assessment.verdict),
            ("Blocking constraints", str(len(assessment.blocking_constraints))),
            ("Total constraints", str(len(assessment.constraints))),
            ("Background tracking",
             "yes" if assessment.background_tracking_possible else "no"),
        ])

        st.markdown(
            f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">{esc(assessment.verdict)}</span>
  {esc(assessment.os_type)}</h4>
  <p>{esc(assessment.headline)}.</p>
  <div class="app-ev">{esc(assessment.fix)}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**The constraints that decide it**")
        for constraint in assessment.blocking_constraints:
            st.markdown(
                f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">{esc(constraint.code)}</span>
  {esc(constraint.title)}</h4>
  <p>{esc(constraint.detail)}</p>
  <div class="app-ev">{esc(constraint.workaround)}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("**The rest, which are real but not decisive**")
        for constraint in assessment.constraints:
            if constraint.blocking:
                continue
            st.markdown(
                f"""
<div class="app-card warn">
  <h4><span class="app-tag warn">{esc(constraint.code)}</span>
  {esc(constraint.title)}</h4>
  <p>{esc(constraint.detail)}</p>
  <div class="app-ev">{esc(constraint.workaround)}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.subheader("What does work")
        for index, alternative in enumerate(assessment.alternatives, start=1):
            st.markdown(f"{index}. {alternative}")

    # -----------------------------------------------------------------
    with tab_status:
        st.subheader("Where a job can go next")
        current = st.selectbox("Current status", list(TRANSITIONS))
        transition = simulate_driver_status(current)

        _kpis([
            ("Terminal", "yes" if transition.is_terminal else "no"),
            ("Obvious next", transition.next_status or "nothing"),
            ("Exceptions", str(len(transition.exceptions))),
            ("Billable", "yes" if transition.billable else "no"),
        ])

        tone = "ok" if not transition.is_terminal else "info"
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(transition.current_status)}</span>
  {esc(transition.headline)}</h4>
  <p>{esc(transition.fix)}</p>
  <div class="app-ev">Allowed:
  {esc(', '.join(transition.allowed_next) or 'nothing at all')}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        if not transition.is_terminal:
            st.markdown("**The primary control**")
            st.markdown(f"- Move to **{transition.next_status}**")
            if transition.exceptions:
                st.markdown("**Behind a confirmation**")
                for option in transition.exceptions:
                    st.markdown(f"- {option}")

        for finding in transition.findings:
            _finding_card(finding)

        st.subheader("The whole machine")
        for state, allowed in TRANSITIONS.items():
            if state in TERMINAL_STATUSES:
                st.markdown(f"- **{state}**: terminal, nothing follows")
            else:
                st.markdown(f"- **{state}**: {', '.join(allowed)}")
