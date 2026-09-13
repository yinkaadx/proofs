"""Airtable WhatsApp Automation Guard.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine can sit behind the real Make webhook.
"""

from __future__ import annotations

import json
import random

import streamlit as st

from shared.theme import esc, inject
from tools.airtable_whatsapp_automation_guard.core import (
    APPOINTMENT_TEMPLATE,
    AutomationLedger,
    BLOCKED,
    DUPLICATE,
    ENGINE_VERSION,
    ERROR_NONE,
    FAILED,
    PHONE_STYLES,
    QUEUED,
    SENT,
    SKIPPED,
    STATUS_READY,
    STATUSES,
    TWILIO_ERRORS,
    build_request,
    generate_record,
    idempotency_key,
    kpi_counts,
    normalise_phone,
    process_record,
    run_batch,
    sample_batch,
)

STATE = "airtable_wa_state"

OUTCOME_TONE = {SENT: "ok", DUPLICATE: "info", SKIPPED: "info",
                BLOCKED: "crit", FAILED: "crit", QUEUED: "warn"}

PHONE_LABELS = {
    "+2348031234567": "Clean E.164",
    "08031234567": "Local, leading zero",
    "+234 803 123 4567": "Spaced",
    "(0803) 123-4567": "Brackets and dashes",
    "002348031234567": "Double zero prefix",
    "0803123": "Truncated",
    "0803 CALL ME": "Letters in the field",
    "": "Empty field",
}

FAILURE_CHOICES: dict[str, int] = {"Twilio accepts it": ERROR_NONE}
FAILURE_CHOICES.update({f"{error.code} {error.label}": error.code
                        for error in TWILIO_ERRORS})


def _state() -> dict:
    if STATE not in st.session_state:
        st.session_state[STATE] = {
            "rng": random.Random(20260913),
            "sequence": 0,
            "ledger": AutomationLedger(),
            "record": None,
        }
    return st.session_state[STATE]


def _generate() -> None:
    """Build the next record from the controls on screen, then run it.

    Every value is read from live widget state rather than from arguments bound
    at the previous render, which are one interaction stale.
    """
    state = _state()
    state["sequence"] += 1
    phone_choice = st.session_state.get("awa_phone", "Any")
    status_choice = st.session_state.get("awa_status", "Any")
    record = generate_record(
        state["rng"], state["sequence"],
        phone=None if phone_choice == "Any" else phone_choice,
        status=None if status_choice == "Any" else status_choice)
    state["record"] = record
    process_record(
        state["ledger"], record,
        within_window=bool(st.session_state.get("awa_window", True)),
        failure_code=FAILURE_CHOICES[
            st.session_state.get("awa_failure", "Twilio accepts it")])


def _resend() -> None:
    """Send the same record again, which is what a Make retry does."""
    state = _state()
    record = state["record"]
    if record is None:
        return
    process_record(
        state["ledger"], record,
        within_window=bool(st.session_state.get("awa_window", True)),
        failure_code=FAILURE_CHOICES[
            st.session_state.get("awa_failure", "Twilio accepts it")])


def _reset() -> None:
    state = _state()
    state["ledger"] = AutomationLedger()
    state["record"] = None
    state["sequence"] = 0
    state["rng"] = random.Random(20260913)


def render() -> None:
    inject()
    state = _state()
    ledger: AutomationLedger = state["ledger"]
    record = state["record"]

    st.markdown(
        """
<div class="app-hero">
  <h1>Airtable WhatsApp Automation Guard</h1>
  <p>A record changes in Airtable, Make picks it up, and a WhatsApp message
  goes out. Three things go wrong and all three are silent until somebody
  complains: a retry sends the same message twice, a phone number typed by a
  human stops the whole scenario, and a message sent outside the twenty four
  hour window is rejected with a code the scenario retries forever. Nothing in
  this engine raises, so one bad record never takes the batch with it.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Webhook**: generate a record and watch it run end to end.\n"
            "2. Press **Retry** to send the same record again, the way Make "
            "does after a timeout.\n"
            "3. Pick a broken phone shape and see it blocked before Twilio.\n"
            "4. **Batch** runs every case at once, including a real retry."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. Airtable, Make and Twilio are "
            "all simulated in this session, so no message is ever sent."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    counts = kpi_counts(ledger)
    st.markdown(
        f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{counts[SENT]}</div>
    <div class="l">Messages sent</div></div>
  <div class="app-kpi"><div class="n">{counts[DUPLICATE]}</div>
    <div class="l">Duplicates suppressed</div></div>
  <div class="app-kpi crit"><div class="n">{counts[BLOCKED] + counts[FAILED]}</div>
    <div class="l">Alerts raised</div></div>
  <div class="app-kpi"><div class="n">{counts[SKIPPED]}</div>
    <div class="l">Left alone</div></div>
</div>
""",
        unsafe_allow_html=True,
    )

    tab_webhook, tab_router, tab_ledger, tab_batch = st.tabs(
        ["Webhook Simulator", "Twilio Router", "Ledger and Alerts",
         "Whole Batch"]
    )

    # -----------------------------------------------------------------
    # Airtable webhook simulator
    # -----------------------------------------------------------------
    with tab_webhook:
        st.markdown("#### The record Airtable posts")
        st.caption(
            "The Phone Number field is free text, so a real base contains "
            "every shape below. Pick one and watch where it is caught."
        )

        c1, c2 = st.columns(2)
        c1.selectbox("Phone shape", ["Any", *PHONE_STYLES],
                     format_func=lambda value: (
                         "Any" if value == "Any"
                         else f"{PHONE_LABELS.get(value, value)}"),
                     key="awa_phone")
        c2.selectbox("Record status", ["Any", *STATUSES], key="awa_status")

        c3, c4 = st.columns(2)
        c3.selectbox("Twilio response", list(FAILURE_CHOICES),
                     key="awa_failure")
        c4.checkbox("Inside the twenty four hour window", value=True,
                    key="awa_window")

        c5, c6, c7 = st.columns(3)
        c5.button("Generate a record", type="primary", on_click=_generate,
                  key="awa_generate_btn")
        c6.button("Retry the same record", on_click=_resend,
                  key="awa_resend_btn", disabled=record is None)
        c7.button("Clear the ledger", on_click=_reset, key="awa_reset_btn")

        if record is None:
            st.info("No record yet. Generate one to start the run.")
        else:
            last = ledger.results[-1]
            tone = OUTCOME_TONE.get(last.outcome, "info")
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(last.outcome)}</span>
  {esc(record.name)}</h4>
  <div class="app-ev">{esc(record.record_id)} &nbsp;
  {esc(record.phone or 'empty phone field')} &nbsp;
  {esc(record.status)}</div>
  <p>{esc(last.detail)}</p>
</div>
""",
                unsafe_allow_html=True,
            )

            st.markdown("##### The Airtable payload")
            st.code(json.dumps(record.as_payload(), indent=2), language="json")

            phone = normalise_phone(record.phone)
            st.markdown("##### What normalisation did to the phone number")
            st.dataframe(
                [{"Field": "As typed in Airtable",
                  "Value": record.phone or "empty"},
                 {"Field": "Normalised",
                  "Value": phone.e164 if phone.ok else "refused"},
                 {"Field": "Verdict",
                  "Value": "Valid E.164" if phone.ok else phone.error_code},
                 {"Field": "Note",
                  "Value": phone.message or ("Changed on the way through"
                                             if phone.changed
                                             else "Already in E.164")}],
                width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Twilio payload router
    # -----------------------------------------------------------------
    with tab_router:
        if record is None:
            st.info("Generate a record on the first tab, then the Twilio "
                    "request appears here.")
        else:
            phone = normalise_phone(record.phone)
            if not phone.ok:
                st.markdown(
                    f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">No request built</span>
  {esc(phone.error_code)}</h4>
  <p>{esc(phone.message)}</p>
</div>
""",
                    unsafe_allow_html=True,
                )
                st.warning(
                    "Nothing is sent to Twilio at all. Building a request from "
                    "a number that did not normalise is how error 21211 ends "
                    "up in the logs once per record, every run."
                )
            else:
                within = bool(st.session_state.get("awa_window", True))
                request = build_request(record, phone, within_window=within)
                st.markdown("#### The Twilio request")
                st.caption(
                    "Both addresses carry the whatsapp: prefix. Leaving it off "
                    "the To address silently sends an SMS instead, at a "
                    "different price and to a phone that may not be the one "
                    "the customer uses for WhatsApp."
                )

                st.markdown(
                    f"""
<div class="app-card {'ok' if within else 'warn'}">
  <h4><span class="app-tag {'ok' if within else 'warn'}">
  {esc('Free form allowed' if within else 'Template required')}</span>
  {esc(request.to)}</h4>
  <div class="app-ev">{esc(request.from_)} &nbsp;
  {esc(APPOINTMENT_TEMPLATE.name if not within else 'no template needed')}</div>
  <p>{esc(request.body if within else APPOINTMENT_TEMPLATE.body)}</p>
</div>
""",
                    unsafe_allow_html=True,
                )

                if not within:
                    st.warning(
                        "More than twenty four hours since the customer last "
                        "wrote, so a free form Body would come back as error "
                        "63016. The router sends ContentSid and "
                        "ContentVariables instead, and omits Body rather than "
                        "sending something Twilio ignores."
                    )

                st.markdown("##### Form parameters")
                st.dataframe(
                    [{"Parameter": key, "Value": value}
                     for key, value in request.as_form().items()],
                    width="stretch", hide_index=True)

                st.markdown("##### The request as curl")
                st.code(request.as_curl(), language="bash")

                st.markdown("##### Idempotency key for this send")
                st.code(idempotency_key(record, request), language="text")
                st.caption(
                    "The record id and a digest of the message together. The "
                    "record id alone would suppress a genuinely different "
                    "second message, and the body alone would collide between "
                    "two records saying the same thing."
                )

    # -----------------------------------------------------------------
    # Ledger and alerts
    # -----------------------------------------------------------------
    with tab_ledger:
        if not ledger.results:
            st.info("The ledger fills as records are processed.")
        else:
            st.markdown("#### Every record this run touched")
            st.dataframe(ledger.rows(), width="stretch", hide_index=True)

            if ledger.alerts:
                st.markdown("#### Alerts")
                st.caption(
                    "Logged rather than raised. A Make scenario that throws "
                    "stops the run, and every record after the bad one is "
                    "never processed."
                )
                st.dataframe(ledger.alert_rows(), width="stretch",
                             hide_index=True)
                for result in ledger.alerts[-3:]:
                    retryable = result.error.retryable if result.error else False
                    tone = "warn" if retryable else "crit"
                    st.markdown(
                        f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">
  {esc('Retry it' if retryable else 'Do not retry')}</span>
  {esc(result.record.record_id)}</h4>
  <div class="app-ev">{esc(result.record.phone or 'empty phone field')}</div>
  <p>{esc(result.error.action if result.error else result.detail)}</p>
</div>
""",
                        unsafe_allow_html=True,
                    )
            else:
                st.success("No alerts. Every record either sent or was left "
                           "alone on purpose.")

            if ledger.queued:
                st.markdown("#### Held for retry")
                for result in ledger.queued[-3:]:
                    st.markdown(
                        f"""
<div class="app-card warn">
  <h4><span class="app-tag warn">Retry it</span>
  {esc(result.record.record_id)}</h4>
  <div class="app-ev">{esc(str(result.error.code) if result.error else '')}
  &nbsp; {esc(result.record.phone or 'empty phone field')}</div>
  <p>{esc(result.error.action if result.error else result.detail)}</p>
</div>
""",
                        unsafe_allow_html=True,
                    )
                st.caption(
                    "These are not alerts. A rate limit clears itself, so the "
                    "record goes back on the queue rather than in front of a "
                    "person."
                )

            st.markdown("##### Keys already sent")
            if ledger.sent_keys:
                st.dataframe(
                    [{"Idempotency key": key, "Message SID": sid}
                     for key, sid in ledger.sent_keys.items()],
                    width="stretch", hide_index=True)
            else:
                st.info("Nothing has been sent yet, so no key is held.")

    # -----------------------------------------------------------------
    # The whole batch
    # -----------------------------------------------------------------
    with tab_batch:
        st.markdown("#### One run over every case")
        st.caption(
            "Seven records: two that send, two phone numbers a person typed "
            "badly, two records the automation should leave alone, and the "
            "same record arriving twice because Make retried."
        )

        batch_within = st.checkbox(
            "Run the batch inside the twenty four hour window", value=True,
            key="awa_batch_window")
        batch = run_batch(sample_batch(), within_window=batch_within)
        batch_counts = kpi_counts(batch)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{batch_counts[SENT]}</div>
    <div class="l">Sent</div></div>
  <div class="app-kpi"><div class="n">{batch_counts[DUPLICATE]}</div>
    <div class="l">Retry suppressed</div></div>
  <div class="app-kpi crit"><div class="n">{batch_counts[BLOCKED]}</div>
    <div class="l">Blocked before Twilio</div></div>
  <div class="app-kpi"><div class="n">{batch_counts[SKIPPED]}</div>
    <div class="l">Not {STATUS_READY.lower()}</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.dataframe(batch.rows(), width="stretch", hide_index=True)

        st.markdown(
            f"""
<div class="app-card ok">
  <h4><span class="app-tag ok">Batch completed</span>
  {len(batch.results)} record(s), nothing raised</h4>
  <p>Two records could not be sent and two were deliberately left alone. The
  run finished either way, which is the difference between a scenario that
  alerts and one that stops with the rest of the queue unsent.</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### Twilio errors this guard knows")
        st.dataframe(
            [{"Code": str(error.code), "Meaning": error.label,
              "Retryable": "Yes" if error.retryable else "No",
              "What to do": error.action}
             for error in TWILIO_ERRORS],
            width="stretch", hide_index=True)
        st.caption(
            "Only one of these is worth retrying. A scenario that retries the "
            "rest fails identically every time and spends an operation on each "
            "attempt."
        )

    st.markdown(
        f"""
<div class="app-foot">
Airtable WhatsApp Automation Guard, engine version {ENGINE_VERSION}. A
simulator: no Airtable base is read, no Make scenario runs and no Twilio message
is ever sent. Nothing in the engine raises, because a scenario that throws stops
the run and leaves every record after the bad one unprocessed.
</div>
""",
        unsafe_allow_html=True,
    )
