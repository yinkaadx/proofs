"""Houzez IDX and HubSpot Console.

Rendered inside the hub app. Every payload on this page is generated from the
controls on each run, so a record on screen can never describe a listing or a
lead that was replaced two interactions ago.
"""

from __future__ import annotations

import json

import streamlit as st

from shared.theme import esc, inject
from tools.houzez_idx_hubspot_console.core import (
    ENGINE_VERSION,
    FIELD_MAP,
    PROPERTY_TYPES,
    ROUTE_DUPLICATE,
    ROUTE_REJECTED,
    SAMPLE_EVENTS,
    SAMPLE_LISTINGS,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    SYNC_PUBLISH,
    SYNC_SUPPRESS,
    TRACKING_COOKIE_NAME,
    TYPE_MAP,
    VALID_ACCEPTED,
    VALID_ACCEPTED_UNATTRIBUTED,
    VALID_REJECTED,
    replay_ledger,
    simulate_idx_feed_sync,
    simulate_make_lead_routing,
    validate_hubspot_embed,
)

TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}
ACTION_TONE = {SYNC_PUBLISH: "ok", SYNC_SUPPRESS: "crit"}
STATUS_TONE = {VALID_ACCEPTED: "ok", VALID_ACCEPTED_UNATTRIBUTED: "warn",
               VALID_REJECTED: "crit"}
GOOD_COOKIE = "9f2c41ab77de40518c3b6a0e5d17b842"


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
  <h1>Houzez IDX and HubSpot Console</h1>
  <p>Three joints in a property funnel, and each one fails quietly rather
  than loudly. An MLS record carries display permissions alongside its data,
  and publishing a listing that forbids it gets the whole feed switched off. A
  HubSpot form with no tracking cookie does not fail, it succeeds with no page
  history attached, so attribution understates whatever actually works. And a
  webhook retry writes the same lead twice unless the key that stops it comes
  from the lead itself.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **IDX**: sync the listing that forbids display and check "
            "that no meta comes back at all.\n"
            "2. **HubSpot**: clear the tracking cookie and watch the "
            "submission still be accepted.\n"
            "3. **Routing**: replay the same lead twice and check the "
            "counters do not move."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. No MLS feed is pulled, no "
            "HubSpot portal is contacted and no CRM record is written."
        )
        st.caption(
            "The sample listings carry their own display flags, because that "
            "is how a real feed delivers them: as fields on the record, not "
            "as a policy document nobody reads."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_idx, tab_hs, tab_route = st.tabs(
        ["IDX Feed Sync", "HubSpot Embed", "Lead Routing Ledger"])

    # -----------------------------------------------------------------
    with tab_idx:
        st.subheader("One feed record into Houzez post meta")
        left, right = st.columns(2)
        with left:
            mls_id = st.selectbox("Listing", list(SAMPLE_LISTINGS))
            custom_id = st.text_input(
                "Or an id that is not in the current pull", value="")
        with right:
            property_type = st.selectbox("Feed property type",
                                         list(PROPERTY_TYPES))
            st.caption(
                f"The feed sends {len(PROPERTY_TYPES)} types and Houzez has a "
                f"term for each one. An unmapped type imports with no term "
                f"and drops out of every filtered search."
            )

        target = custom_id.strip() or mls_id
        record = simulate_idx_feed_sync(target, property_type)

        _kpis([
            ("Action", record.action),
            ("Meta fields written", str(record.mapped_field_count)),
            ("Withheld", str(len(record.withheld_fields))),
            ("Publishable", "yes" if record.publishable else "no"),
        ])

        tone = ACTION_TONE.get(record.action, "warn")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(record.action)}</span>
  {esc(record.mls_id)}</h4>
  <p>Type term {esc(record.houzez_type_term or 'none')}, status term
  {esc(record.houzez_status_term or 'none')}.</p>
  <div class="app-ev">{esc('Credit: ' + record.attribution
                           if record.attribution
                           else 'No brokerage attribution on this record')}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        if record.meta:
            st.markdown("**Post meta ready for the template**")
            st.code(json.dumps(record.meta, indent=2), language="json")
            st.markdown("**Taxonomy terms**")
            st.code(json.dumps(record.taxonomies, indent=2), language="json")
        else:
            st.markdown(
                """
<div class="app-card crit">
  <h4><span class="app-tag crit">NO META</span> Nothing was mapped</h4>
  <p>The permissions are applied before the mapping rather than after. A
  dictionary that exists is a dictionary something will eventually render, so
  a suppressed listing produces none at all.</p>
  <div class="app-ev">Read the findings below for which rule stopped it.</div>
</div>
""",
                unsafe_allow_html=True,
            )

        if record.withheld_fields and record.meta:
            st.caption(
                "Held back on purpose: "
                + ", ".join(f"`{f}`" for f in record.withheld_fields)
                + ". The keys are absent rather than empty, so a template "
                  "that checks for the key does not print a blank line."
            )

        st.markdown("**Findings**")
        for finding in record.findings:
            _finding_card(finding)

        st.subheader("The field map")
        for feed_field, houzez_key, note in FIELD_MAP:
            st.markdown(f"- `{feed_field}` becomes `{houzez_key}`. {note}.")

        st.subheader("The type map")
        for feed_type, term in TYPE_MAP.items():
            st.markdown(f"- `{feed_type}` becomes the term **{term}**")

    # -----------------------------------------------------------------
    with tab_hs:
        st.subheader("Accepted, and separately, attributed")
        left, right = st.columns(2)
        with left:
            email = st.text_input("email", value="aoife.kelly@example.com")
            firstname = st.text_input("firstname", value="Aoife")
        with right:
            phone = st.text_input("phone", value="")
            cookie = st.text_input(
                f"{TRACKING_COOKIE_NAME} cookie value", value=GOOD_COOKIE)
            st.caption(
                "Clear this to see a submission that succeeds and arrives "
                "attributed to nobody."
            )

        form_data = {"email": email, "firstname": firstname}
        if phone.strip():
            form_data["phone"] = phone
        validation = validate_hubspot_embed(form_data, cookie)

        _kpis([
            ("Status", validation.status),
            ("Cookie present",
             "yes" if validation.tracking_cookie_present else "no"),
            ("Cookie valid",
             "yes" if validation.tracking_cookie_valid else "no"),
            ("Attribution works",
             "yes" if validation.attribution_works else "no"),
        ])

        tone = STATUS_TONE.get(validation.status, "warn")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(validation.status)}</span>
  {len(validation.fields_seen)} field(s) in the payload</h4>
  <p>HubSpot accepting a submission and reporting being able to attribute it
  are two different questions. Only the first one is usually asked.</p>
  <div class="app-ev">Missing recommended:
  {esc(', '.join(validation.missing_recommended) or 'none')}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**The payload as it goes to the forms API**")
        st.code(json.dumps(validation.payload, indent=2), language="json")
        if not validation.tracking_cookie_valid:
            st.caption(
                "The context object is empty because no valid cookie was "
                "read. The submission still succeeds."
            )

        st.markdown("**Findings**")
        for finding in validation.findings:
            _finding_card(finding)

    # -----------------------------------------------------------------
    with tab_route:
        st.subheader("Write it once, however many times it arrives")
        left, right = st.columns(2)
        with left:
            lead_email = st.text_input(
                "Lead email", value="aoife.kelly@example.com")
        with right:
            interest = st.text_input("Property interest", value="MLS-4471902")

        first = simulate_make_lead_routing(lead_email, interest)
        retry = simulate_make_lead_routing(
            lead_email, interest,
            seen_enquiry_keys=(first.idempotency_key,),
            seen_contact_keys=(first.contact_key,))

        _kpis([
            ("First arrival", first.outcome),
            ("Same lead again", retry.outcome),
            ("Enquiry key", first.idempotency_key or "none"),
            ("Contact key", first.contact_key or "none"),
        ])

        for label, result in (("First arrival", first),
                              ("The very same lead, arriving again", retry)):
            row_tone = ("crit" if result.outcome == ROUTE_REJECTED else
                        "info" if result.outcome == ROUTE_DUPLICATE else "ok")
            st.markdown(
                f"""
<div class="app-card {row_tone}">
  <h4><span class="app-tag {row_tone}">{esc(result.outcome)}</span>
  {esc(label)}</h4>
  <p>Contact {'created' if result.contact_created else 'not created'},
  enquiry {'created' if result.enquiry_created else 'not created'}.</p>
  <div class="app-ev">{esc('Wrote to the CRM' if result.wrote_anything
                           else 'Wrote nothing at all')}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        if first.crm_record:
            st.markdown("**The record the first arrival creates**")
            st.code(json.dumps(first.crm_record, indent=2), language="json")

        st.markdown("**Findings**")
        for finding in first.findings:
            _finding_card(finding)

        st.subheader("A replay of six arrivals")
        ledger = replay_ledger(SAMPLE_EVENTS)
        _kpis([
            ("Arrivals", str(ledger["events"])),
            ("Contacts created", str(ledger["contacts_created"])),
            ("Enquiries created", str(ledger["enquiries_created"])),
            ("Duplicates suppressed", str(ledger["duplicates_suppressed"])),
            ("Rejected", str(ledger["rejected"])),
        ])
        for (event_email, event_interest), row in zip(SAMPLE_EVENTS,
                                                      ledger["rows"]):
            st.markdown(
                f"- `{event_email}` about `{event_interest}` gives "
                f"**{row.outcome}**")
        st.caption(
            f"Six arrivals produced {ledger['contacts_created']} contact(s) "
            f"and {ledger['enquiries_created']} enquiry / enquiries. The "
            f"second and fourth are the same person and property as the "
            f"first, one of them typed in a different case, and neither "
            f"wrote anything."
        )
