"""HIPAA Web & Tracking Audit Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine could run as a pre launch gate in CI.

Everything is evaluated live from the controls, so no card on screen can
describe a configuration that was replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.hipaa_tracking_audit_console.core import (
    BAA_AVAILABLE,
    DISCLAIMER,
    ENGINE_VERSION,
    HANDOFF_API_POST,
    HANDOFF_METHODS,
    HANDOFF_QUERY_PARAMS,
    PAGE_BOOKING,
    PAGE_TYPES,
    baa_rows,
    evaluate_ehr_handoff,
    evaluate_gtm_exposure,
    generate_clinician_summary,
    get_baa_matrix,
    unsigned_vendors,
)


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>HIPAA Web &amp; Tracking Audit Console</h1>
  <p>Most advice a therapy practice gets about website tracking is either
  frightening and wrong or reassuring and wrong. The line moved in June 2024,
  when a federal court struck down the part of OCR's guidance that treated a
  visit to a public page as health information. What did not move is the part
  that matters here: a booking form and a client login handle real PHI, and a
  tag on either of those is a disclosure to a vendor who will not sign for
  it.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Tag audit**: pick a page type and switch the Google Ads tag "
            "on and off.\n"
            "2. **EHR handoff**: compare a pre filled link against a server "
            "side POST.\n"
            "3. **BAA matrix**: read which vendors will sign and which will "
            "not.\n"
            "4. **For the owner**: the same findings in plain language."
        )
        st.divider()
        st.caption(DISCLAIMER)
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_tags, tab_handoff, tab_baa, tab_owner = st.tabs(
        ["Tag Audit", "EHR Handoff", "BAA Matrix", "For the Owner"])

    # -----------------------------------------------------------------
    # Google Tag Manager exposure
    # -----------------------------------------------------------------
    with tab_tags:
        st.markdown("#### One page, judged the way a privacy review judges it")

        c1, c2, c3 = st.columns([3, 2, 2])
        page_type = c1.selectbox("Page type", list(PAGE_TYPES),
                                 index=list(PAGE_TYPES).index(PAGE_BOOKING),
                                 key="hta_page")
        collects = c2.toggle("Collects booking details", value=True,
                             key="hta_collects")
        ads = c3.toggle("Google Ads tag fires here", value=True, key="hta_ads")

        finding = evaluate_gtm_exposure(page_type, collects, ads)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {finding.tone}"><div class="n">{esc(finding.risk)}</div>
    <div class="l">Risk level</div></div>
  <div class="app-kpi {'crit' if finding.ocr_violation else 'ok'}">
    <div class="n">{'Yes' if finding.ocr_violation else 'No'}</div>
    <div class="l">Likely OCR violation</div></div>
  <div class="app-kpi"><div class="n">{len(finding.remediation)}</div>
    <div class="l">Actions to take</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown(
            f"""
<div class="app-card {finding.tone}">
  <h4><span class="app-tag {finding.tone}">{esc(finding.risk)}</span>
  {esc(finding.headline)}</h4>
  <p>{esc(finding.explanation)}</p>
  <div class="app-ev">{esc(finding.authority)}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        if finding.remediation:
            st.markdown("##### What to do")
            for step in finding.remediation:
                st.markdown(f"- {step}")

        st.markdown("##### The inputs this verdict came from")
        st.dataframe(finding.rows(), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # The EHR handoff
    # -----------------------------------------------------------------
    with tab_handoff:
        st.markdown("#### Pre filling an intake, two ways")
        st.caption(
            "The difference is not encryption. Both are over TLS. The "
            "difference is that a query string is part of the address, and "
            "addresses get written down everywhere by default."
        )

        method = st.radio("How the details reach the EHR", list(HANDOFF_METHODS),
                          index=0, key="hta_method")
        assessment = evaluate_ehr_handoff(method)

        st.markdown(
            f"""
<div class="app-card {assessment.tone}">
  <h4><span class="app-tag {assessment.tone}">{esc(assessment.status)}</span>
  {esc(assessment.method)}</h4>
  <p>{esc(assessment.summary)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### " + ("The controls that make this safe"
                                if assessment.safe
                                else "Everywhere the details end up"))
        st.dataframe(assessment.rows(), width="stretch", hide_index=True)

        st.markdown("##### What it looks like on the wire")
        st.code(assessment.example, language="http")

        other = (HANDOFF_API_POST if method == HANDOFF_QUERY_PARAMS
                 else HANDOFF_QUERY_PARAMS)
        st.caption(f"Switch to {other} above to see the other side.")

    # -----------------------------------------------------------------
    # BAA matrix
    # -----------------------------------------------------------------
    with tab_baa:
        st.markdown("#### Who will sign, and who will not")
        entries = get_baa_matrix()
        signed = [e for e in entries if e.status == BAA_AVAILABLE]

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{len(signed)}</div>
    <div class="l">BAA available as standard</div></div>
  <div class="app-kpi crit"><div class="n">{len(entries) - len(signed)}</div>
    <div class="l">Not available or conditional</div></div>
  <div class="app-kpi warn"><div class="n">{len(unsigned_vendors())}</div>
    <div class="l">Still to confirm in writing</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for entry in entries:
            st.markdown(
                f"""
<div class="app-card {entry.tone}">
  <h4><span class="app-tag {entry.tone}">{esc(entry.status)}</span>
  {esc(entry.vendor)}</h4>
  <p>{esc(entry.detail)}</p>
  <div class="app-ev">{esc(entry.action)}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### The matrix")
        st.dataframe(baa_rows(), width="stretch", hide_index=True)
        st.caption(
            "The confirm in writing column is the honest part. A matrix that "
            "looked equally certain about all four rows would invite somebody "
            "to rely on the weakest one."
        )

    # -----------------------------------------------------------------
    # Plain language summary
    # -----------------------------------------------------------------
    with tab_owner:
        st.markdown("#### For whoever runs the practice")
        st.caption(
            "Written for a practice owner rather than a security team, so "
            "every line says what to do rather than what to be aware of."
        )
        for index, point in enumerate(generate_clinician_summary(), start=1):
            st.markdown(
                f"""
<div class="app-card">
  <h4><span class="app-tag">{index}</span></h4>
  <p>{esc(point)}</p>
</div>
""",
                unsafe_allow_html=True,
            )
        st.info(DISCLAIMER)

    st.markdown(
        f"""
<div class="app-foot">
HIPAA Web &amp; Tracking Audit Console, engine version {ENGINE_VERSION}. A
simulator: no website is scanned, no tag manager is read, no EHR is contacted
and no vendor is asked anything. Every conclusion names its source so it can be
checked with counsel. {esc(DISCLAIMER)}
</div>
""",
        unsafe_allow_html=True,
    )
