"""Pipedrive API and Integration Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine sits behind the real webhook handler.
"""

from __future__ import annotations

import json

import streamlit as st

from shared.theme import esc, inject
from tools.pipedrive_integration_engine.core import (
    BLOCKED,
    DIRECT_PATH,
    DISPATCH_LABEL,
    DISPATCHED,
    ENGINE_VERSION,
    INTENT_LABEL,
    LLM_ASSISTANT,
    REP_TAKEOVER_VALUE,
    SAMPLE_DEALS,
    SAMPLE_HISTORY,
    SAMPLE_PEOPLE,
    STAGE_ORDER,
    STAGE_TEMPLATES,
    SUBSCRIBED,
    ZAPIER_PATH,
    Deal,
    Thread,
    apply_opt_in,
    apply_opt_out,
    consent_label,
    dispatch_allowed,
    engine_kpis,
    export_csv,
    export_json,
    find_deal,
    find_person,
    handover_activity,
    hashes_of,
    incremental_plan,
    is_opt_in,
    is_opt_out,
    ledger_rows,
    money,
    normalize_phone,
    open_deal_for,
    path_comparison,
    pipedrive_note_call,
    power_bi_manifest,
    rep_metrics,
    route_inbound,
    route_webhook,
    stage_durations,
    webhook_payload,
)

STATE = "pie_state"

TONE = {DISPATCHED: "ok", BLOCKED: "crit"}


def _state() -> dict:
    if STATE not in st.session_state:
        people = list(SAMPLE_PEOPLE)
        deal = SAMPLE_DEALS[0]
        dispatch = route_webhook(deal, "Demo Booked", "evt-0001", tuple(people))
        thread_person = people[1]
        thread_deal = find_deal("PD-D-9002")
        thread = Thread(thread_person.person_id, thread_deal.deal_id)
        rows = ledger_rows()
        st.session_state[STATE] = {
            "people": people,
            "deal": deal,
            "previous_stage": "Demo Booked",
            "dispatch": dispatch,
            "events": 1,
            "thread": thread,
            "thread_person_id": thread_person.person_id,
            "turns": [],
            "opt_out": None,
            "blocked_attempts": 0,
            "baseline_hashes": hashes_of(rows),
            "baseline_watermark": max(r.updated_at for r in rows),
            "edited_deals": {},
        }
    return st.session_state[STATE]


def _people(state: dict) -> tuple:
    return tuple(state["people"])


def _replace_person(state: dict, person) -> None:
    state["people"] = [person if p.person_id == person.person_id else p
                       for p in state["people"]]


def _current_deals(state: dict) -> tuple:
    """Sample deals with any edit made in the ledger tab applied."""
    edits = state["edited_deals"]
    return tuple(edits.get(d.deal_id, d) for d in SAMPLE_DEALS)


def _call_block(call, label: str) -> None:
    st.markdown(f"**{label}**")
    st.code(call.as_json(), language="json")


def _hop_list(title: str, hops, tone: str, note: str) -> str:
    items = "".join(
        f'<div class="app-stage"><span class="s {tone}">{index + 1}</span>'
        f'<span class="d">{esc(hop)}</span></div>'
        for index, hop in enumerate(hops)
    )
    return (f'<div class="app-card {"ok" if tone == "pass" else "warn"}">'
            f'<h4>{esc(title)}</h4>{items}'
            f'<p>{esc(note)}</p></div>')


def render() -> None:
    inject()
    state = _state()

    st.markdown(
        """
<div class="app-hero">
  <h1>Pipedrive API and Integration Console</h1>
  <p>A Pipedrive stage change calls Sinch directly, with no Zapier anywhere in
  the path. Inbound replies are answered against the person and deal record and
  handed to a sales rep the moment the customer asks for one. A STOP keyword
  unsubscribes the person in Pipedrive and blocks every later dispatch, and the
  whole pipeline exports to Power BI with a change hash per row so a refresh
  moves only what moved.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Webhook Router**: fire a stage change and read the exact "
            "Sinch request it produced.\n"
            "2. **AI SMS Thread**: reply as the customer and ask for a human.\n"
            "3. **Opt Out**: send STOP, then try to dispatch again.\n"
            "4. **Power BI Ledger**: change a deal and watch the batch shrink."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. Every Pipedrive and Sinch "
            "request is built in full and shown rather than sent, and API "
            "tokens are redacted before they reach the page."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_exec, tab_hook, tab_thread, tab_opt, tab_bi = st.tabs(
        ["Executive Summary", "Direct Webhook Router", "AI SMS Thread",
         "Opt Out Synchronizer", "Power BI Ledger"]
    )

    rows = ledger_rows(_current_deals(state), _people(state), SAMPLE_HISTORY)
    plan = incremental_plan(rows, state["baseline_hashes"],
                            state["baseline_watermark"])

    # -----------------------------------------------------------------
    # Executive summary
    # -----------------------------------------------------------------
    with tab_exec:
        kpis = engine_kpis(state["dispatch"], plan, [state["thread"]],
                           _people(state))
        comparison = path_comparison()
        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">0</div>
    <div class="l">Zapier steps in the path</div></div>
  <div class="app-kpi"><div class="n">{kpis.dispatch_ms:.2f} ms</div>
    <div class="l">Webhook to Sinch request</div></div>
  <div class="app-kpi warn"><div class="n">{kpis.handovers}</div>
    <div class="l">Threads with a rep</div></div>
  <div class="app-kpi crit"><div class="n">{kpis.blocked_by_consent}</div>
    <div class="l">Numbers blocked by consent</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.success(
            f"The stage change reached a built Sinch request in "
            f"{kpis.dispatch_ms:.2f} ms, inside the same process that received "
            f"the webhook. A polling Zap would not have fired yet: its median "
            f"wait before the trigger even runs is "
            f"{comparison['zapier_delay_seconds']:.0f} seconds."
        )

        c1, c2 = st.columns(2)
        with c1:
            st.markdown(
                _hop_list("Direct, what this console does", DIRECT_PATH, "pass",
                          "Two systems hold the customer's number: Pipedrive "
                          "and Sinch. No task counter, no polling interval, no "
                          "third vendor in the middle."),
                unsafe_allow_html=True,
            )
        with c2:
            st.markdown(
                _hop_list("Through Zapier, for comparison", ZAPIER_PATH, "warn",
                          f"Six hops, two billed tasks per event, and a third "
                          f"vendor holding the number in transit. At 600 events "
                          f"a month that is "
                          f"{comparison['zapier_tasks_per_month']:,} tasks."),
                unsafe_allow_html=True,
            )

        st.markdown("##### What each part of the job now does")
        st.dataframe(
            [
                {"Capability": "Stage change to SMS",
                 "Mechanism": "Pipedrive webhook posted to our endpoint, which "
                              "calls the Sinch XMS batches API",
                 "Third party in the path": "None"},
                {"Capability": "Inbound reply handling",
                 "Mechanism": f"{LLM_ASSISTANT} answers with the person and "
                              f"deal record attached to the prompt",
                 "Third party in the path": "None"},
                {"Capability": "Sales rep handover",
                 "Mechanism": "Phrase detection on every inbound message, "
                              "which creates a Pipedrive call activity",
                 "Third party in the path": "None"},
                {"Capability": "Opt out",
                 "Mechanism": "STOP marks marketing_status unsubscribed and "
                              "the router refuses every later dispatch",
                 "Third party in the path": "None"},
                {"Capability": "Power BI feed",
                 "Mechanism": "Row level SHA256 change hash plus an updated_at "
                              "watermark for incremental refresh",
                 "Third party in the path": "None"},
            ],
            width="stretch", hide_index=True,
        )

    # -----------------------------------------------------------------
    # Direct webhook router
    # -----------------------------------------------------------------
    with tab_hook:
        st.markdown("#### Fire a Pipedrive stage change")
        st.caption(
            "This is the body Pipedrive posts the moment a deal moves. Our "
            "endpoint receives it, resolves the person, checks consent and "
            "builds the Sinch request in the same call."
        )

        deal_labels = [f"{d.deal_id} {d.title}" for d in SAMPLE_DEALS]
        picked = st.selectbox("Deal", deal_labels, index=0)
        base = SAMPLE_DEALS[deal_labels.index(picked)]

        c1, c2 = st.columns(2)
        previous_stage = c1.selectbox(
            "Previous stage", list(STAGE_ORDER),
            index=STAGE_ORDER.index("Demo Booked"),
        )
        new_stage = c2.selectbox(
            "New stage", list(STAGE_ORDER),
            index=STAGE_ORDER.index(base.stage) if base.stage in STAGE_ORDER else 0,
            help="A stage with no mapped template sends nothing at all, which "
                 "is the safe default.",
        )

        candidate = Deal(
            deal_id=base.deal_id, title=base.title, person_id=base.person_id,
            stage=new_stage, value=base.value, currency=base.currency,
            owner=base.owner, status=base.status, updated_at=base.updated_at,
        )

        person = find_person(candidate.person_id, _people(state))
        if person is not None:
            allowed, gate_reason = dispatch_allowed(person)
            st.caption(
                f"{person.name} on {normalize_phone(person.phone)}, consent "
                f"status {consent_label(person)}. {gate_reason}"
            )

        st.markdown("##### The webhook body Pipedrive posts")
        st.code(
            json.dumps(
                webhook_payload(candidate, previous_stage,
                                f"evt-{state['events']:04d}"),
                indent=2,
            ),
            language="json",
        )

        if st.button("Fire the stage change webhook", type="primary"):
            state["events"] += 1
            state["deal"] = candidate
            state["previous_stage"] = previous_stage
            state["dispatch"] = route_webhook(
                candidate, previous_stage, f"evt-{state['events']:04d}",
                _people(state))
            st.rerun()

        dispatch = state["dispatch"]
        tone = TONE.get(dispatch.status, "info")
        st.markdown(
            f'<div class="app-card {tone}">'
            f'<h4><span class="app-tag {tone}">'
            f'{esc(DISPATCH_LABEL[dispatch.status])}</span>'
            f'{esc(dispatch.deal_id)} at {esc(dispatch.stage)}</h4>'
            f'<p>{esc(dispatch.reason)}</p>'
            f'<p>Event {esc(dispatch.event_id)} resolved in '
            f'{dispatch.elapsed_ms:.3f} ms. Zapier used: '
            f'{"yes" if dispatch.zapier_used else "no"}.</p>'
            f'</div>',
            unsafe_allow_html=True,
        )

        if dispatch.status == DISPATCHED and dispatch.call is not None:
            st.markdown("##### The message that goes out")
            st.markdown(
                f'<div class="app-ev">To {esc(dispatch.to_number)}<br>'
                f'{esc(dispatch.text)}</div>',
                unsafe_allow_html=True,
            )
            _call_block(dispatch.call, "Outbound Sinch request, built directly")
            _call_block(
                pipedrive_note_call(dispatch.deal_id, dispatch.person_id,
                                    f"SMS sent: {dispatch.text}"),
                "Written back onto the Pipedrive record",
            )
        else:
            st.warning(
                "Nothing was sent, and that is the correct outcome here. The "
                "router refuses rather than guessing."
            )

        st.markdown("##### Stage templates in force")
        st.dataframe(
            [{"Stage": stage,
              "Sends an SMS": "Yes" if stage in STAGE_TEMPLATES else "No",
              "Template": STAGE_TEMPLATES.get(stage, "No template, nothing is sent")}
             for stage in STAGE_ORDER],
            width="stretch", hide_index=True,
        )

    # -----------------------------------------------------------------
    # Two way AI SMS thread
    # -----------------------------------------------------------------
    with tab_thread:
        st.markdown("#### Two way thread, tied to a Person and a Deal")
        person_labels = [f"{p.person_id} {p.name}" for p in _people(state)]
        current_index = [p.person_id for p in _people(state)].index(
            state["thread_person_id"])
        picked_person = st.selectbox("Person in the thread", person_labels,
                                     index=current_index)
        chosen = _people(state)[person_labels.index(picked_person)]
        if chosen.person_id != state["thread_person_id"]:
            deal_for = open_deal_for(chosen.person_id, _current_deals(state))
            state["thread_person_id"] = chosen.person_id
            state["thread"] = Thread(chosen.person_id,
                                     deal_for.deal_id if deal_for else "")
            state["turns"] = []

        thread = state["thread"]
        deal_for = find_deal(thread.deal_id, _current_deals(state))
        st.caption(
            f"Every inbound message is matched to {chosen.name} by number, and "
            f"logged against "
            f"{thread.deal_id or 'the person only, since no deal is open'}"
            f"{'' if deal_for is None else ' worth ' + money(deal_for.value, deal_for.currency)}"
            f". A deal above {money(REP_TAKEOVER_VALUE, 'GBP')} never has its "
            f"price negotiated by the assistant."
        )

        message = st.text_input(
            "Customer message",
            value="How much is this going to cost?",
            help="Type anything. Asking for a human hands the thread over.",
        )

        b1, b2, b3 = st.columns(3)
        if b1.button("Send as the customer", type="primary", width="stretch"):
            turn = route_inbound(thread, message, chosen, deal_for)
            state["turns"].append(turn)
            st.rerun()
        if b2.button("Ask for a human", width="stretch"):
            turn = route_inbound(thread, "Can I speak to a human please?",
                                 chosen, deal_for)
            state["turns"].append(turn)
            st.rerun()
        if b3.button("Reset this thread", width="stretch"):
            state["thread"] = Thread(chosen.person_id,
                                     deal_for.deal_id if deal_for else "")
            state["turns"] = []
            st.rerun()

        if thread.handover:
            st.error(
                f"Sales rep handover flagged. {thread.handover_reason} The "
                f"assistant has stopped replying in this thread, because two "
                f"voices answering one customer is how they get told two "
                f"different things."
            )
        elif thread.messages:
            st.success(
                "The assistant is still handling this thread. No rep has been "
                "pulled in, because nothing in it needed one."
            )

        st.markdown("##### Transcript")
        if not thread.messages:
            st.caption("Nothing in the thread yet. Send a message to start it.")
        for entry in thread.messages:
            who = chosen.name if entry.direction == "inbound" else LLM_ASSISTANT
            side = "info" if entry.direction == "inbound" else "ok"
            st.markdown(
                f'<div class="app-card {side}">'
                f'<h4><span class="app-tag {side}">'
                f'{esc("Inbound" if entry.direction == "inbound" else "Outbound")}'
                f'</span>{esc(who)}</h4>'
                f'<p>{esc(entry.text)}</p></div>',
                unsafe_allow_html=True,
            )

        if state["turns"]:
            st.markdown("##### How each message was routed")
            st.dataframe(
                [{"Intent": INTENT_LABEL.get(t_.intent, t_.intent),
                  "Person": t_.person_id,
                  "Deal": t_.deal_id or "none",
                  "Handover": "Yes" if t_.handover else "No",
                  "Confidence": f"{t_.confidence:.0%}"}
                 for t_ in state["turns"]],
                width="stretch", hide_index=True,
            )

            last = state["turns"][-1]
            if last.prompt:
                with st.expander("The prompt the assistant was given"):
                    st.code(last.prompt, language="text")

        if thread.handover:
            _call_block(handover_activity(thread, chosen, deal_for),
                        "The Pipedrive activity that puts this on a rep's list")

    # -----------------------------------------------------------------
    # Opt out synchronizer
    # -----------------------------------------------------------------
    with tab_opt:
        st.markdown("#### Inbound STOP, all the way through")
        st.caption(
            "An opt out that only silences one campaign is not an opt out. "
            "This marks the Pipedrive person unsubscribed, sends exactly one "
            "confirmation, and then refuses every automated dispatch to that "
            "number."
        )

        opt_labels = [f"{p.person_id} {p.name} ({consent_label(p)})"
                      for p in _people(state)]
        picked_opt = st.selectbox("Person sending the message", opt_labels, index=0)
        target = _people(state)[opt_labels.index(picked_opt)]

        keyword = st.text_input("Inbound message text", value="STOP")
        st.caption(
            f"Recognised as an opt out: "
            f"{'yes' if is_opt_out(keyword) else 'no'}. Matching ignores case "
            f"and punctuation, so STOP, stop. and Stop all reach the same rule."
        )

        c1, c2 = st.columns(2)
        if c1.button("Process the inbound message", type="primary", width="stretch"):
            # START and STOP are two different rules, so the text picks one
            # rather than falling through from the other and losing its reason.
            result = (apply_opt_in(target, keyword) if is_opt_in(keyword)
                      else apply_opt_out(target, keyword))
            if result.applied:
                _replace_person(state, result.person)
            state["opt_out"] = result
            st.rerun()
        if c2.button("Try to dispatch to this person", width="stretch"):
            state["blocked_attempts"] += 1
            state["events"] += 1
            deal_for_target = open_deal_for(target.person_id, _current_deals(state))
            if deal_for_target is not None:
                state["dispatch"] = route_webhook(
                    deal_for_target, "Qualified", f"evt-{state['events']:04d}",
                    _people(state))
            st.rerun()

        result = state["opt_out"]
        if result is not None:
            tone = "crit" if result.applied else "info"
            st.markdown(
                f'<div class="app-card {tone}">'
                f'<h4><span class="app-tag {tone}">'
                f'{esc("Consent changed" if result.applied else "No change")}'
                f'</span>{esc(result.keyword or "no keyword matched")}</h4>'
                f'<p>{esc(result.note)}</p></div>',
                unsafe_allow_html=True,
            )
            if result.updates:
                st.markdown("##### Fields written back to Pipedrive")
                st.dataframe(
                    [{"Entity": u.entity, "Record": u.entity_id,
                      "Field": u.field_name, "Before": u.old_value,
                      "After": u.new_value}
                     for u in result.updates],
                    width="stretch", hide_index=True,
                )
            for call in result.calls:
                _call_block(call, call.purpose)
            if result.confirmation:
                st.caption(
                    "That confirmation is the last automated message this "
                    "number receives. Nothing after it is sent."
                )

        st.markdown("##### Consent state across the book")
        st.dataframe(
            [{"Person": p.name, "Number": normalize_phone(p.phone),
              "marketing_status": p.marketing_status,
              "sms_opt_out": "Yes" if p.sms_opt_out else "No",
              "Opted out at": p.opt_out_at or "not opted out",
              "Keyword": p.opt_out_keyword or "none",
              "Automated dispatch": "Blocked" if p.opted_out else "Allowed"}
             for p in _people(state)],
            width="stretch", hide_index=True,
        )

        if state["blocked_attempts"]:
            latest = state["dispatch"]
            if latest.status == BLOCKED:
                st.error(
                    f"Dispatch attempt {latest.event_id} was refused. "
                    f"{latest.reason} Nothing reached Sinch, so nothing "
                    f"reached the handset."
                )
            else:
                st.info(
                    f"Dispatch attempt {latest.event_id} was allowed, because "
                    f"that person still has consent on file."
                )

    # -----------------------------------------------------------------
    # Power BI ledger
    # -----------------------------------------------------------------
    with tab_bi:
        st.markdown("#### Incremental ingestion ledger")
        st.caption(
            "Every row carries a SHA256 of its own business fields. Power BI "
            "keeps the previous hashes and the updated_at watermark, so a "
            "refresh pulls only the rows whose hash moved."
        )

        edit_labels = [f"{d.deal_id} {d.title}" for d in SAMPLE_DEALS]
        c1, c2 = st.columns([2, 1])
        edited = c1.selectbox("Deal to change", edit_labels, index=0,
                              key="pie_bi_deal")
        source = SAMPLE_DEALS[edit_labels.index(edited)]
        current = state["edited_deals"].get(source.deal_id, source)
        new_value = c2.number_input("New deal value", min_value=0.0, step=500.0,
                                    value=float(current.value), format="%.2f")

        d1, d2 = st.columns(2)
        if d1.button("Apply the change", type="primary", width="stretch"):
            state["edited_deals"][source.deal_id] = Deal(
                deal_id=source.deal_id, title=source.title,
                person_id=source.person_id, stage=source.stage,
                value=float(new_value), currency=source.currency,
                owner=source.owner, status=source.status,
                updated_at=source.updated_at,
            )
            st.rerun()
        if d2.button("Reset the ledger", width="stretch"):
            state["edited_deals"] = {}
            st.rerun()

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi warn"><div class="n">{len(plan.rows_to_send)}</div>
    <div class="l">Rows this refresh sends</div></div>
  <div class="app-kpi ok"><div class="n">{plan.saved_rows}</div>
    <div class="l">Rows skipped, hash unchanged</div></div>
  <div class="app-kpi"><div class="n">{len(rows)}</div>
    <div class="l">Rows in the table</div></div>
  <div class="app-kpi ok"><div class="n">{len(plan.rows_to_send) / max(1, len(rows)):.0%}</div>
    <div class="l">Share of the table moved</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        if plan.changed_rows:
            st.warning(
                f"{len(plan.changed_rows)} row hash moved since the last "
                f"refresh, so only that row is pulled. The watermark advances "
                f"to {plan.watermark}."
            )
        else:
            st.success(
                "No hash moved since the last refresh, so an incremental run "
                "would transfer nothing at all and the report would still be "
                "correct."
            )

        st.markdown("##### fact_deal_state")
        st.dataframe(
            [{"deal_id": r.deal_id, "deal_title": r.deal_title,
              "person_name": r.person_name, "stage": r.stage,
              "status": r.status, "value": f"{r.value:,.2f}",
              "currency": r.currency, "owner": r.owner,
              "days_in_stage": r.days_in_stage, "days_open": r.days_open,
              "sms_consent": r.sms_consent, "updated_at": r.updated_at,
              "change_hash": r.change_hash}
             for r in rows],
            width="stretch", hide_index=True,
        )

        st.markdown("##### Stage durations feeding the report")
        duration_rows = []
        for deal in _current_deals(state):
            for stage, days in stage_durations(deal.deal_id, SAMPLE_HISTORY):
                duration_rows.append({"deal_id": deal.deal_id, "Stage": stage,
                                      "Days in stage": days})
        st.dataframe(duration_rows, width="stretch", hide_index=True)

        st.markdown("##### Sales rep metrics")
        st.dataframe(
            [{"Rep": m.rep, "Deals": m.deals, "Open": m.open_deals,
              "Won": m.won_deals,
              "Pipeline": f"{m.pipeline_value:,.2f}",
              "Won value": f"{m.won_value:,.2f}",
              "Average days in stage": m.avg_days_in_stage,
              "Win rate": f"{m.win_rate:.0%}"}
             for m in rep_metrics(_current_deals(state), SAMPLE_HISTORY)],
            width="stretch", hide_index=True,
        )

        st.markdown("##### Dataset manifest")
        st.code(json.dumps(power_bi_manifest(rows), indent=2),
                language="json")

        e1, e2 = st.columns(2)
        e1.download_button("Download the full table as CSV",
                           data=export_csv(rows),
                           file_name="fact_deal_state.csv", mime="text/csv",
                           width="stretch")
        e2.download_button("Download this incremental batch as JSON",
                           data=export_json(plan),
                           file_name="fact_deal_state_batch.json",
                           mime="application/json", width="stretch")

    st.markdown(
        '<div class="app-foot">Every request on this page is built in full and '
        'displayed rather than sent, and API tokens are redacted before they '
        'reach the browser. Consent is checked at the single gate every '
        'automated dispatch passes through, so an opt out cannot be bypassed '
        'by adding another campaign.</div>',
        unsafe_allow_html=True,
    )
