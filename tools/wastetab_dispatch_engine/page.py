"""WasteTab Logistics and Financial Engine.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine can sit behind the real WordPress webhook.
"""

from __future__ import annotations

import json
from decimal import Decimal

import streamlit as st

from shared.theme import esc, inject
from tools.wastetab_dispatch_engine.core import (
    BOOKED,
    CANCELLED,
    CARRIERS,
    COMPLETED,
    DEFAULT_MARGIN_PERCENT,
    ENGINE_VERSION,
    LINK_QUOTE,
    NEW_ENQUIRY,
    ON_HOLD,
    QUOTE_SENT,
    SERVICES,
    STATES,
    SURCHARGE_BANDS,
    TRANSITIONS,
    VALVE_PAUSED,
    VALVE_REFUSED,
    WASTE_GENERAL,
    WASTE_TYPES,
    Job,
    build_quote,
    dispatch,
    kpi_counts,
    link_rows,
    move,
    open_job,
    payment_link,
    pounds,
    processor_fee,
    resolve_safety_valve,
    sample_enquiry,
    to_pence,
    trigger_safety_valve,
)

STATE = "wastetab_state"

STATE_TONE = {NEW_ENQUIRY: "info", QUOTE_SENT: "warn", BOOKED: "ok",
              ON_HOLD: "crit", COMPLETED: "ok", CANCELLED: "crit"}

SAMPLE_POSTCODES = ["SE15 4RT", "LS1 4DY", "G1 2FF", "BS1 5TR", "EH1 1YZ",
                    "not a postcode"]


def _state() -> dict:
    if STATE not in st.session_state:
        st.session_state[STATE] = {"job": None, "last_move": None,
                                   "last_valve": None}
    return st.session_state[STATE]


def _open_job() -> None:
    """Open a job from the enquiry currently on screen.

    Every value is read from live widget state rather than from arguments bound
    at the previous render, which are one interaction stale.
    """
    state = _state()
    enquiry = sample_enquiry(
        postcode=str(st.session_state.get("wt_postcode", SAMPLE_POSTCODES[0])),
        service=str(st.session_state.get("wt_service", SERVICES[1])),
        waste_type=str(st.session_state.get("wt_waste", WASTE_GENERAL)))
    state["job"] = open_job(enquiry)
    state["last_move"] = None
    state["last_valve"] = None


def _move(to_state: str) -> None:
    state = _state()
    job = state["job"]
    if job is None:
        return
    charge = 0
    if to_state == BOOKED and job.state == QUOTE_SENT and job.quote is not None:
        charge = job.quote.charge_pence
    state["last_move"] = move(job, to_state, actor="operator",
                              charge_pence=charge)


def _send_quote() -> None:
    """Price the job, attach the link and move it to quote sent."""
    state = _state()
    job = state["job"]
    if job is None:
        return
    result = dispatch(job.postcode, job.service, job.waste_type)
    if not result.matched:
        state["last_move"] = None
        return
    try:
        margin = Decimal(str(st.session_state.get("wt_margin",
                                                  DEFAULT_MARGIN_PERCENT)))
    except Exception:  # noqa: BLE001
        margin = DEFAULT_MARGIN_PERCENT
    job.carrier = result.best
    job.quote = build_quote(result.best.bid_pence, margin)
    payment_link(job, job.quote.charge_pence, LINK_QUOTE)
    state["last_move"] = move(
        job, QUOTE_SENT, actor="system",
        detail=f"Quoted {pounds(job.quote.charge_pence)} against "
               f"{result.best.name} at {pounds(result.best.bid_pence)}.")


def _valve(found_waste: str) -> None:
    state = _state()
    job = state["job"]
    if job is None:
        return
    state["last_valve"] = trigger_safety_valve(job, found_waste)


def _resolve(paid: bool) -> None:
    state = _state()
    job = state["job"]
    if job is None:
        return
    state["last_valve"] = resolve_safety_valve(job, paid)


def render() -> None:
    inject()
    state = _state()
    job: Job | None = state["job"]

    st.markdown(
        """
<div class="app-hero">
  <h1>WasteTab Logistics &amp; Financial Engine</h1>
  <p>An enquiry arrives from a WordPress form, a local carrier is found, a
  price is quoted and a payment link goes out. Two things quietly cost money
  here: a link built from the price rather than grossed up for the processor
  fee arrives short on every single job, and a skip loaded with something other
  than what was quoted has to stop the flow rather than be absorbed. Every
  amount below is integer pence, and the gross up is checked against the
  processor's own rounding rather than trusted to a formula.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Dispatch**: pick a postcode and see which carriers can "
            "actually take it.\n"
            "2. **Gross Up**: watch the payment link amount, and what the "
            "naive one would have cost.\n"
            "3. **Ledger**: move the job through its states and try an "
            "illegal move.\n"
            "4. **Safety Valve**: find different waste on site and watch the "
            "flow pause."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. WordPress, the carrier "
            "database and the payment processor are all simulated, and no "
            "payment link is ever created."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    counts = kpi_counts(job) if job else {"state": "no job", "charged": 0,
                                          "net": 0, "links": 0}
    tone = STATE_TONE.get(counts["state"], "info")
    st.markdown(
        f"""
<div class="app-kpis">
  <div class="app-kpi {tone}"><div class="n">{esc(str(counts['state']))}</div>
    <div class="l">Job state</div></div>
  <div class="app-kpi"><div class="n">{esc(pounds(counts['charged']))}</div>
    <div class="l">Charged</div></div>
  <div class="app-kpi ok"><div class="n">{esc(pounds(counts['net']))}</div>
    <div class="l">Net after fees</div></div>
  <div class="app-kpi"><div class="n">{counts['links']}</div>
    <div class="l">Payment links</div></div>
</div>
""",
        unsafe_allow_html=True,
    )

    tab_dispatch, tab_money, tab_ledger, tab_valve = st.tabs(
        ["Dispatch Router", "Gross Up Engine", "State Ledger", "Safety Valve"]
    )

    # -----------------------------------------------------------------
    # Regional dispatch router
    # -----------------------------------------------------------------
    with tab_dispatch:
        st.markdown("#### The enquiry")
        st.caption(
            "Routing runs on the outward half of the postcode. The inward half "
            "identifies a street and tells a dispatcher nothing about which "
            "depot is nearest."
        )

        c1, c2, c3 = st.columns(3)
        postcode = c1.selectbox("Postcode", SAMPLE_POSTCODES, key="wt_postcode")
        service = c2.selectbox("Service", SERVICES, index=1, key="wt_service")
        waste = c3.selectbox("Waste type", WASTE_TYPES, key="wt_waste")

        result = dispatch(postcode, service, waste)
        tone = "ok" if result.matched else "crit"
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">
  {esc('Carriers found' if result.matched else 'No dispatch')}</span>
  {esc(result.postcode.formatted or postcode)}</h4>
  <div class="app-ev">area {esc(result.postcode.area or 'unreadable')}
  &nbsp; {esc(service)} &nbsp; {esc(waste)}</div>
  <p>{esc(result.reason)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        if result.matched:
            st.markdown("##### Carriers that can take it, cheapest first")
            st.dataframe(result.rows(), width="stretch", hide_index=True)
        else:
            st.warning(
                "The enquiry is held rather than dispatched. Sending it to "
                "whoever is first in the list produces a quote nobody honours."
            )

        st.button("Open this as a job", type="primary", on_click=_open_job,
                  key="wt_open_btn")

        st.markdown("##### The WordPress payload")
        st.code(json.dumps(
            sample_enquiry(postcode, service, waste).as_payload(), indent=2),
            language="json")

        st.markdown("##### The carrier database")
        st.dataframe(
            [{"Code": carrier.code, "Carrier": carrier.name,
              "Areas": ", ".join(carrier.areas),
              "Services": ", ".join(carrier.services),
              "Waste": ", ".join(carrier.waste_types),
              "Bid": pounds(carrier.bid_pence),
              "Active": "Yes" if carrier.active else "No"}
             for carrier in CARRIERS],
            width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Gross up engine
    # -----------------------------------------------------------------
    with tab_money:
        st.markdown("#### From carrier bid to payment link")
        st.caption(
            "The fee is charged on what the customer pays, not on the margin, "
            "so the gross up happens last. The percentage is rounded to the "
            "penny by the processor before the fixed 30p is added, which is "
            "the step a one line formula misses."
        )

        c1, c2 = st.columns(2)
        bid_text = c1.text_input("Carrier bid in pounds", value="100.00",
                                 key="wt_bid")
        margin = c2.number_input("Margin percent", min_value=0.0,
                                 max_value=200.0, value=30.0, step=5.0,
                                 key="wt_margin")

        try:
            bid_pence = to_pence(bid_text)
            quote = build_quote(bid_pence, Decimal(str(margin)))
            error = ""
        except ValueError as exc:
            quote, error = None, str(exc)

        if error:
            st.markdown(
                f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">Refused</span>Carrier bid</h4>
  <p>{esc(error)}</p>
</div>
""",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{esc(pounds(quote.net_target_pence))}</div>
    <div class="l">Net needed</div></div>
  <div class="app-kpi ok"><div class="n">{esc(pounds(quote.charge_pence))}</div>
    <div class="l">Payment link amount</div></div>
  <div class="app-kpi"><div class="n">{esc(pounds(quote.fee_pence))}</div>
    <div class="l">Processor fee</div></div>
  <div class="app-kpi ok"><div class="n">{esc(pounds(quote.net_received_pence))}</div>
    <div class="l">Net received</div></div>
</div>
""",
                unsafe_allow_html=True,
            )

            st.markdown("##### The arithmetic, line by line")
            st.dataframe(quote.rows(), width="stretch", hide_index=True)

            st.markdown(
                f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">What the naive link costs</span>
  {esc(pounds(quote.naive_shortfall_pence))} short on every job</h4>
  <div class="app-ev">charging {esc(pounds(quote.naive_charge_pence))} leaves
  {esc(pounds(quote.naive_charge_pence - processor_fee(quote.naive_charge_pence)))}
  </div>
  <p>Over a hundred jobs that is
  {esc(pounds(quote.naive_shortfall_pence * 100))} taken straight out of
  margin, and it never appears as a line anywhere: the invoice says the right
  number and the bank says a different one.</p>
</div>
""",
                unsafe_allow_html=True,
            )

            st.caption(
                "The gross up is checked against the processor's own rounding "
                "for every net from one penny to two thousand pounds: never "
                "short, and never a penny more than it has to be."
            )

    # -----------------------------------------------------------------
    # State machine ledger
    # -----------------------------------------------------------------
    with tab_ledger:
        if job is None:
            st.info("No job open. Open one from the dispatch tab to start the "
                    "ledger.")
        else:
            allowed = TRANSITIONS.get(job.state, ())
            st.markdown(f"#### {esc(job.reference)}")
            st.caption(
                "A transition not on the map is refused rather than written. A "
                "job that reaches completed without passing booked is a job "
                "nobody was paid for."
            )

            tone = STATE_TONE.get(job.state, "info")
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(job.state)}</span>
  {esc(job.service)} at {esc(job.postcode)}</h4>
  <div class="app-ev">
  {esc(job.carrier.name if job.carrier else 'no carrier yet')} &nbsp;
  {esc(pounds(job.quote.charge_pence) if job.quote else 'not quoted')}</div>
  <p>From here the job may move to
  {esc(', '.join(allowed) if allowed else 'nothing, it is finished')}.</p>
</div>
""",
                unsafe_allow_html=True,
            )

            columns = st.columns(4)
            columns[0].button("Send the quote", on_click=_send_quote,
                              key="wt_quote_btn",
                              disabled=job.state != NEW_ENQUIRY)
            columns[1].button("Mark booked", on_click=_move, args=(BOOKED,),
                              key="wt_booked_btn",
                              disabled=BOOKED not in allowed)
            columns[2].button("Mark completed", on_click=_move,
                              args=(COMPLETED,), key="wt_completed_btn",
                              disabled=COMPLETED not in allowed)
            columns[3].button("Cancel the job", on_click=_move,
                              args=(CANCELLED,), key="wt_cancel_btn",
                              disabled=CANCELLED not in allowed)

            st.markdown("##### Try a move the job cannot make")
            st.caption(
                "The buttons above are disabled for illegal moves, which is "
                "convenience. This asks the engine directly, which is the "
                "control."
            )
            target = st.selectbox("Move to", list(STATES), key="wt_force_state")
            st.button("Attempt the move", on_click=_move, args=(target,),
                      key="wt_force_btn")

            last = state["last_move"]
            if last is not None:
                move_tone = "ok" if last.ok else "crit"
                st.markdown(
                    f"""
<div class="app-card {move_tone}">
  <h4><span class="app-tag {move_tone}">
  {esc('Accepted' if last.ok else 'Refused')}</span>{esc(job.state)}</h4>
  <p>{esc(last.message)}</p>
</div>
""",
                    unsafe_allow_html=True,
                )

            if job.entries:
                st.markdown("##### The ledger")
                st.dataframe(job.rows(), width="stretch", hide_index=True)
            if job.links:
                st.markdown("##### Payment links raised")
                st.dataframe(link_rows(job), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Safety valve
    # -----------------------------------------------------------------
    with tab_valve:
        if job is None:
            st.info("Open a job first. The valve only fires on a booked "
                    "collection.")
        else:
            st.markdown("#### Materially different waste")
            st.caption(
                "The alternative to stopping is absorbing it, which means the "
                "carrier is paid for a job they did not agree to do or the "
                "platform pays the difference. Both happen quietly, once per "
                "job, until somebody reads the margin at the end of the "
                "quarter."
            )

            found = st.selectbox("What the carrier found on site", WASTE_TYPES,
                                 key="wt_found_waste")
            st.caption(
                f"Uplift bands: " + ", ".join(
                    f"{name.lower()} {pounds(value)}"
                    for name, value in SURCHARGE_BANDS.items() if value))

            c1, c2, c3 = st.columns(3)
            c1.button("Carrier reports the load", type="primary",
                      on_click=_valve, args=(found,), key="wt_valve_btn",
                      disabled=job.state != BOOKED)
            c2.button("Customer pays the surcharge", on_click=_resolve,
                      args=(True,), key="wt_paid_btn",
                      disabled=job.state != ON_HOLD)
            c3.button("Customer declines", on_click=_resolve, args=(False,),
                      key="wt_declined_btn", disabled=job.state != ON_HOLD)

            valve = state["last_valve"]
            if valve is None:
                st.info(
                    "Nothing reported yet. Book the job on the ledger tab, "
                    "then have the carrier report what is actually in the skip."
                )
            else:
                valve_tone = {VALVE_PAUSED: "crit",
                              VALVE_REFUSED: "warn"}.get(valve.outcome, "ok")
                st.markdown(
                    f"""
<div class="app-card {valve_tone}">
  <h4><span class="app-tag {valve_tone}">{esc(valve.outcome)}</span>
  {esc(valve.found_waste or job.waste_type)}</h4>
  <p>{esc(valve.message)}</p>
</div>
""",
                    unsafe_allow_html=True,
                )

                if valve.surcharge is not None:
                    st.markdown("##### The supplementary link")
                    st.code(json.dumps({
                        "reference": valve.surcharge.reference,
                        "amount": pounds(valve.surcharge.amount_pence),
                        "fee": pounds(valve.surcharge.fee_pence),
                        "net": pounds(valve.surcharge.net_pence),
                        "url": valve.surcharge.url,
                        "description": valve.surcharge.description,
                    }, indent=2), language="json")

            if job.entries:
                st.markdown("##### What the ledger recorded")
                st.dataframe(job.rows(), width="stretch", hide_index=True)

    st.markdown(
        f"""
<div class="app-foot">
WasteTab Logistics &amp; Financial Engine, engine version {ENGINE_VERSION}. A
simulator: no WordPress webhook is received, no carrier is contacted and no
payment link is created. Every amount is integer pence, because a float is the
reason a gross up lands a penny short.
</div>
""",
        unsafe_allow_html=True,
    )
