"""HubSpot B2B Network Architect.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine could sit behind the real integration.

Everything on this page is evaluated live from the controls. There is no
session state to go stale, which removes the class of bug where a card on
screen describes a portal that was replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.hubspot_b2b_network_architect.core import (
    ASSOCIATE_ENDPOINT,
    CATEGORY_HUBSPOT,
    CATEGORY_USER,
    CONNECTED_AT,
    CREATE_ENDPOINT,
    DELIMITER,
    ENGINE_VERSION,
    LABEL_TARGET_FOR,
    LABELS_ENDPOINT,
    LOGGED,
    MODE_BLIND_CREATE,
    MODE_SEARCH_APPEND,
    MODE_SEARCH_REPLACE,
    MODES,
    NETWORKS,
    PORTAL_LABELS,
    PROPERTY_FIELD_TYPE,
    PROPERTY_NAME,
    PROPERTY_TYPE,
    SAMPLE_COMPANIES,
    SEARCH_ENDPOINT,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_OK,
    SKIPPED_HISTORICAL,
    SKIPPED_SEEN,
    SOURCES,
    Portal,
    audit_ledger,
    audit_portal,
    categorise,
    domain_variants,
    ledger_comparison,
    map_prospect,
    normalise_domain,
    payload_json,
    process_payload,
    sample_messages,
)

TONE = {SEVERITY_CRITICAL: "crit", SEVERITY_HIGH: "warn", SEVERITY_OK: "ok"}

NETWORK_BY_LABEL = {option.label: option.value for option in NETWORKS}
COMPANY_BY_NAME = {seed.name: seed for seed in SAMPLE_COMPANIES}


def _tone(severity: str) -> str:
    return TONE.get(severity, "warn")


def _finding_card(finding) -> None:
    tone = _tone(finding.severity)
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
  <h1>HubSpot B2B Network Architect</h1>
  <p>Gridwise sells into a network of networks, and the portal has to say so
  without saying it three times. A company sits in more than one network at
  once. A prospect works at one company and matters because of another. A
  LinkedIn sync arrives carrying every message ever sent. Each of those has an
  obvious model that duplicates and a correct model that does not, and the
  difference shows up as a record count rather than as an opinion.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Networks**: tick all three, then switch the write mode and "
            "watch the record count.\n"
            "2. **Associations**: hardcode the typeId and see which company "
            "the label lands on.\n"
            "3. **LinkedIn**: turn the ledger off and count the enrolments."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live portal. No HubSpot token is held, no "
            "record is written and no LinkedIn account is connected."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_networks, tab_assoc, tab_ledger = st.tabs(
        ["Network Categorization", "Prospect Associations", "LinkedIn Ledger"])

    # -----------------------------------------------------------------
    # One company, three networks, one record
    # -----------------------------------------------------------------
    with tab_networks:
        st.markdown("#### One company in several networks")
        st.caption(
            "The categorization is a property of the company, not a company of "
            "its own. One custom property of type "
            f"{PROPERTY_TYPE} with fieldType {PROPERTY_FIELD_TYPE}, which is "
            "HubSpot's multiple checkboxes, holds every network the company "
            "belongs to as one semicolon delimited string."
        )

        c1, c2 = st.columns([2, 3])
        company_name = c1.selectbox("Company", list(COMPANY_BY_NAME),
                                    key="hba_company")
        seed = COMPANY_BY_NAME[company_name]
        chosen_labels = c2.multiselect(
            "Gridwise networks to assign",
            [option.label for option in NETWORKS],
            default=[NETWORKS[0].label, NETWORKS[1].label],
            key="hba_networks")
        chosen = [NETWORK_BY_LABEL[label] for label in chosen_labels]

        c3, c4 = st.columns([3, 2])
        mode = c3.radio("How the integration writes the property", MODES,
                        index=0, key="hba_mode")
        normalise = c4.toggle("Normalise the domain before searching",
                              value=True, key="hba_normalise")

        st.caption(
            f"{esc(seed.website)} normalises to "
            f"{esc(normalise_domain(seed.website))}. "
            f"{seed.industry}, about {seed.headcount} people."
        )

        portal = categorise(seed, chosen, mode=mode, normalise=normalise)
        clean = categorise(seed, chosen, mode=MODE_SEARCH_APPEND,
                           normalise=True)
        findings = audit_portal(portal, requested=len(chosen))
        record_tone = "ok" if len(portal.records) <= 1 else "crit"
        networks_held = (len(portal.records[0].networks) if portal.records
                         else 0)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {record_tone}"><div class="n">{len(portal.records)}</div>
    <div class="l">Company records, against {len(clean.records)} clean</div></div>
  <div class="app-kpi crit"><div class="n">{portal.duplicate_count}</div>
    <div class="l">Duplicates created</div></div>
  <div class="app-kpi"><div class="n">{networks_held}</div>
    <div class="l">Networks on the first record</div></div>
  <div class="app-kpi"><div class="n">{len(portal.writes)}</div>
    <div class="l">Requests sent</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for finding in findings:
            _finding_card(finding)

        st.markdown("##### The portal after the run")
        st.dataframe(portal.rows(), width="stretch", hide_index=True)

        st.markdown("##### Every request, in order")
        st.dataframe(portal.write_rows(), width="stretch", hide_index=True)

        if portal.writes:
            last = portal.writes[-1]
            st.markdown("##### The last request in full")
            st.code(f"{last.verb} {last.endpoint.split(' ', 1)[-1]}\n"
                    f"{last.pretty_body}", language="json")
            st.caption(esc(last.note))

        st.markdown("##### The same website as five people paste it")
        st.dataframe(domain_variants(seed), width="stretch", hide_index=True)
        st.caption(
            f"Search is {SEARCH_ENDPOINT.split(' ', 1)[-1]} on domain EQ the "
            f"normalised value. Skip the normalisation and the search misses "
            f"by a prefix, so {CREATE_ENDPOINT.split(' ', 1)[-1]} runs and the "
            f"portal gains a second record for a company it already had."
        )

    # -----------------------------------------------------------------
    # The prospect association map
    # -----------------------------------------------------------------
    with tab_assoc:
        st.markdown("#### A prospect belongs to two companies at once")
        st.caption(
            "The prospect works at their employer, which is the primary "
            "company. The prospect also matters because of a Gridwise client "
            "they are being worked for, which is a labelled association to a "
            "different company. HubSpot allows many company associations per "
            "contact and exactly one primary, so neither company has to be "
            "duplicated to hold both facts."
        )

        c1, c2 = st.columns([2, 3])
        label_name = c1.selectbox(
            "Custom association label",
            [entry.label for entry in PORTAL_LABELS
             if entry.category == CATEGORY_USER],
            key="hba_label")
        hardcode = c2.toggle(
            "Hardcode the typeId instead of resolving the label",
            value=False, key="hba_hardcode")

        mapped = map_prospect(label_name=label_name, hardcode_type_id=hardcode)
        primaries = mapped.primary_company_ids
        primary_tone = "ok" if len(primaries) == 1 else "crit"
        label_tone = "ok" if mapped.labelled_rows else "crit"

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{len(mapped.rows)}</div>
    <div class="l">Association rows written</div></div>
  <div class="app-kpi {primary_tone}"><div class="n">{len(primaries)}</div>
    <div class="l">Companies marked primary</div></div>
  <div class="app-kpi {label_tone}"><div class="n">{len(mapped.labelled_rows)}</div>
    <div class="l">Labelled associations</div></div>
  <div class="app-kpi ok"><div class="n">0</div>
    <div class="l">Companies duplicated</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for finding in mapped.findings:
            _finding_card(finding)

        st.markdown("##### The associations the portal holds")
        st.dataframe(mapped.table_rows(), width="stretch", hide_index=True)
        st.caption(
            "Three rows for two companies. Associating a primary company "
            "writes the unlabeled association and the primary marker, so an "
            "association count never matches a company count."
        )

        st.markdown("##### The label table, as the portal returns it")
        st.dataframe(
            [{"typeId": str(entry.type_id), "Category": entry.category,
              "Label": entry.label or "(none)"} for entry in PORTAL_LABELS],
            width="stretch", hide_index=True)
        st.caption(
            f"Read the first column twice. A typeId is unique only inside its "
            f"category, so {CATEGORY_HUBSPOT} 1 is the primary marker while "
            f"{CATEGORY_USER} 1 is {LABEL_TARGET_FOR} in this portal. That is "
            f"why the integration resolves labels by name at start up rather "
            f"than carrying a number in its source."
        )

        st.markdown("##### The request")
        st.code(f"{ASSOCIATE_ENDPOINT}\n\n{mapped.pretty_payload()}",
                language="json")
        st.code(f"# resolved once at start up, never hardcoded\n"
                f"{LABELS_ENDPOINT}", language="bash")

    # -----------------------------------------------------------------
    # The LinkedIn deduplication ledger
    # -----------------------------------------------------------------
    with tab_ledger:
        st.markdown("#### The first sync carries the entire history")
        st.caption(
            "These integrations pull the whole existing conversation the first "
            "time it syncs, not only what arrives afterwards. Written straight "
            "to the timeline that reads as a burst of fresh activity on a day "
            "when nobody spoke to anybody."
        )

        c1, c2 = st.columns([2, 3])
        source = c1.selectbox("Integration", list(SOURCES), key="hba_source")
        skip_history = c2.toggle(
            "Skip anything sent before the connection", value=True,
            key="hba_skip")

        messages = sample_messages()
        delivered = st.slider("Messages delivered", min_value=1,
                              max_value=len(messages), value=len(messages),
                              key="hba_delivered")
        batch = messages[:delivered]

        ledger = process_payload(batch, connected_at=CONNECTED_AT,
                                 skip_history=skip_history, source=source)
        findings = audit_ledger(ledger, skip_history=skip_history)
        logged_tone = "ok" if skip_history else "crit"

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {logged_tone}"><div class="n">{ledger.logged}</div>
    <div class="l">Activities written</div></div>
  <div class="app-kpi"><div class="n">{ledger.count(SKIPPED_HISTORICAL)}</div>
    <div class="l">Historical, skipped</div></div>
  <div class="app-kpi"><div class="n">{ledger.count(SKIPPED_SEEN)}</div>
    <div class="l">Redelivered, refused</div></div>
  <div class="app-kpi {logged_tone}">
    <div class="n">{ledger.workflow_triggers}</div>
    <div class="l">Workflow enrolments</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for finding in findings:
            _finding_card(finding)

        st.markdown("##### The ledger")
        st.dataframe(ledger.rows(), width="stretch", hide_index=True)

        last = ledger.results[-1]
        tone = "ok" if last.outcome == LOGGED else "warn"
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(last.outcome)}</span>
  {esc(last.message.message_urn)}</h4>
  <div class="app-ev">sha256 {esc(last.digest)}</div>
  <p>{esc(last.detail)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### The same payload with the ledger on and off")
        st.dataframe(ledger_comparison(messages), width="stretch",
                     hide_index=True)
        st.caption(
            "Every activity written enrols the contact in whatever workflow "
            "watches for a LinkedIn reply. The enrolment count is the number "
            "that turns a tidy backfill into an apology."
        )

        st.markdown(f"##### The payload {source} posts")
        st.code(payload_json(batch, source=source), language="json")

    st.markdown(
        f"""
<div class="app-foot">
HubSpot B2B Network Architect, engine version {ENGINE_VERSION}. A simulator: no
HubSpot portal is connected, no access token is held, no record or association
is written and no LinkedIn account is read. Every figure on this page is
computed from the controls above it.
</div>
""",
        unsafe_allow_html=True,
    )
