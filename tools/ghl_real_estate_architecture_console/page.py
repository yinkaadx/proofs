"""GHL Real Estate Architecture Console.

Rendered inside the hub app. Every figure on this page is computed from the
controls on each run, so a record on screen can never describe a property or
an envelope that was replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.ghl_real_estate_architecture_console.core import (
    DEDUPE_DUPLICATE,
    DEDUPE_NEW,
    DEDUPE_REJECTED,
    ENGINE_VERSION,
    ENVELOPE_COMPLETED,
    ENVELOPE_DECLINED,
    ENVELOPE_IN_PROGRESS,
    PIPELINE_STAGE,
    SAMPLE_ADDRESSES,
    SAMPLE_CONTACTS,
    SAMPLE_PROPERTIES,
    SAMPLE_SIGNERS,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    SIGNER_STATES,
    generate_reporting_ledger,
    normalize_and_dedupe_lead,
    simulate_multi_signer_contract,
)

TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}
DEDUPE_TONE = {DEDUPE_NEW: "ok", DEDUPE_DUPLICATE: "info",
               DEDUPE_REJECTED: "crit"}
ENVELOPE_TONE = {ENVELOPE_COMPLETED: "ok", ENVELOPE_IN_PROGRESS: "warn",
                 ENVELOPE_DECLINED: "crit"}


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
  <h1>GHL Real Estate Architecture Console</h1>
  <p>One modelling decision decides whether a real estate CRM reports the
  truth, and it is made in the first week and discovered in the first board
  pack. A contact is a person. A property is the thing being transacted. One
  sale has a buyer, a seller, two agents and a lender attached, so if the
  pipeline value lives on the contact then that sale is counted once per
  person who touched it. The overstatement is not a rounding issue, it is the
  deal value multiplied by the number of people in the room.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Intake**: type the same address two ways and watch it "
            "merge, then change the unit and watch it stay separate.\n"
            "2. **Contract**: mark one signer completed and check the "
            "envelope refuses to say completed.\n"
            "3. **Ledger**: read the gap between the two models as "
            "arithmetic rather than as an opinion."
        )
        st.divider()
        st.caption(
            "Nothing here contacts a CRM, a USPS service or a signing "
            "provider. No record is created and no envelope is sent."
        )
        st.caption(
            "True CASS certification and a real ZIP plus four assignment "
            "need a licensed USPS data set, which this does not have. What "
            "it produces is a Publication 28 style standardisation, which is "
            "the part that decides deduplication."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_intake, tab_contract, tab_ledger = st.tabs(
        ["Lead Intake", "Multi Signer Contract", "Reporting Ledger"])

    # -----------------------------------------------------------------
    with tab_intake:
        st.subheader("What counts as the same property")
        sample = st.selectbox("An address as a lead types it",
                              list(SAMPLE_ADDRESSES))
        custom = st.text_input("Or paste one of your own", value="")
        target = custom.strip() or sample

        # Every earlier sample is treated as already on file, so the
        # duplicate path is reachable from the first interaction.
        existing = []
        for address in SAMPLE_ADDRESSES:
            if address == target:
                break
            row = normalize_and_dedupe_lead(address, tuple(existing))
            if row.parsed and row.dedupe_key not in existing:
                existing.append(row.dedupe_key)

        result = normalize_and_dedupe_lead(target, tuple(existing))

        _kpis([
            ("Status", result.dedupe_status),
            ("Unit kept", "yes" if result.has_unit else "none found"),
            ("State", result.state or "not found"),
            ("ZIP", result.zip5 or "not found"),
            ("Parsed", "yes" if result.parsed else "no"),
        ])

        tone = DEDUPE_TONE.get(result.dedupe_status, "warn")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(result.dedupe_status)}</span>
  {esc(result.standardised or 'nothing was standardised')}</h4>
  <p>Raw input: {esc(result.raw)}</p>
  <div class="app-ev">Dedupe key:
  {esc(result.dedupe_key or 'none, so no record should be created')}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**Field by field**")
        for label, value in (("Primary number", result.primary_number),
                             ("Street", result.street),
                             ("Unit designator", result.unit_designator),
                             ("Unit number", result.unit_number),
                             ("City", result.city),
                             ("State", result.state),
                             ("ZIP", result.zip5),
                             ("ZIP plus four", result.zip4)):
            st.markdown(f"- **{label}**: {value or 'not present'}")

        st.markdown("**Findings**")
        for finding in result.findings:
            _finding_card(finding)

        st.subheader("The sample list, keyed in order")
        running: list[str] = []
        for address in SAMPLE_ADDRESSES:
            row = normalize_and_dedupe_lead(address, tuple(running))
            marker = ("**merged into the existing property**"
                      if row.dedupe_status == DEDUPE_DUPLICATE
                      else "new property record")
            st.markdown(f"- `{address}` becomes `{row.standardised}`, {marker}")
            if row.parsed and row.dedupe_key not in running:
                running.append(row.dedupe_key)
        st.caption(
            "Two spellings of one address merge. A different unit at the "
            "same street address does not, and neither does the same street "
            "address with no unit at all, because those are three different "
            "properties."
        )

    # -----------------------------------------------------------------
    with tab_contract:
        st.subheader("Complete means every signer, not the first one")
        property_id = st.text_input("Property id", value="P-1001")

        signers = []
        for index, seed in enumerate(SAMPLE_SIGNERS):
            left, right = st.columns([2, 1])
            with left:
                st.markdown(f"**{seed['name']}**")
                st.caption(seed["role"])
            with right:
                status = st.selectbox(
                    f"Status for {seed['name']}", list(SIGNER_STATES),
                    index=list(SIGNER_STATES).index(seed["status"]),
                    key=f"signer{index}", label_visibility="collapsed")
            signers.append({"name": seed["name"], "role": seed["role"],
                            "status": status})

        envelope = simulate_multi_signer_contract(property_id or "P-1001",
                                                  signers)

        _kpis([
            ("Envelope", envelope.envelope_status),
            ("Pipeline stage", envelope.pipeline_stage),
            ("Signed", f"{envelope.completed_count} of {len(envelope.signers)}"),
            ("Outstanding", str(envelope.outstanding_count)),
            ("Declined", str(envelope.declined_count)),
        ])
        st.caption(
            f"{envelope.completed_count} completed plus "
            f"{envelope.outstanding_count} outstanding plus "
            f"{envelope.declined_count} declined equals "
            f"{len(envelope.signers)} signers, which is everybody on the "
            f"envelope."
        )

        tone = ENVELOPE_TONE.get(envelope.envelope_status, "info")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(envelope.envelope_status)}</span>
  {esc(envelope.headline)}</h4>
  <p>The pipeline is driven by the envelope state rather than by a signature
  webhook. One signature is an event, not a state.</p>
  <div class="app-ev">Terminal:
  {'yes, nothing further will change this' if envelope.is_terminal
   else 'no, the envelope is still live'}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**The tracking matrix**")
        for signer in envelope.signers:
            row_tone = ("ok" if signer.completed else
                        "crit" if signer.status == "Declined" else "warn")
            st.markdown(
                f"""
<div class="app-card {row_tone}">
  <h4><span class="app-tag {row_tone}">{esc(signer.status)}</span>
  Signer {signer.ordinal}: {esc(signer.name)}</h4>
  <p>{esc(signer.role)}</p>
  <div class="app-ev">{'Blocking the envelope' if signer.blocking
                       else 'Done, no further action'}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("**Findings**")
        for finding in envelope.findings:
            _finding_card(finding)

        st.subheader("Every envelope state maps to exactly one stage")
        for state, stage in PIPELINE_STAGE.items():
            st.markdown(f"- Envelope **{state}** puts the pipeline at **{stage}**")

    # -----------------------------------------------------------------
    with tab_ledger:
        st.subheader("The same data under both models")
        ledger = generate_reporting_ledger(SAMPLE_CONTACTS, SAMPLE_PROPERTIES)

        _kpis([
            ("Properties", str(ledger.property_count)),
            ("Contacts involved", str(ledger.distinct_contact_count)),
            ("Property model", f"{ledger.property_model_total}"),
            ("Contact model", f"{ledger.contact_model_total}"),
            ("Overstatement",
             f"{ledger.overstatement} ({ledger.overstatement_pct} percent)"),
        ])
        st.caption(
            f"The property rows add to {ledger.property_model_total} and the "
            f"contact rows add to {ledger.contact_model_total}. The property "
            f"total plus the overstatement of {ledger.overstatement} equals "
            f"the contact total, which is how the gap is arithmetic rather "
            f"than an opinion."
        )

        st.markdown(
            f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">DOUBLE COUNTED</span>
  {esc(ledger.headline)}</h4>
  <p>Every deal is counted once per person attached to it. A filter cannot
  fix this, because the duplication is in the data model rather than in the
  query.</p>
  <div class="app-ev">True pipeline value:
  {ledger.true_pipeline_value}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**Property by property**")
        for row in ledger.rows:
            st.markdown(
                f"""
<div class="app-card {'crit' if row.overstatement else 'ok'}">
  <h4><span class="app-tag {'crit' if row.overstatement else 'ok'}">
  {esc(row.property_id)}</span> {esc(row.address)}</h4>
  <p>Worth {row.deal_value} with {row.contact_count} related contact(s):
  {esc(', '.join(row.related_contacts))}.</p>
  <div class="app-ev">Property model {row.property_model_value},
  contact model {row.contact_model_value},
  overstated by {row.overstatement}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("**Findings**")
        for finding in ledger.findings:
            _finding_card(finding)
