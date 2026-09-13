"""10DLC Campaign Registry Compliance Validator.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine can gate a real TCR submission script.

Everything on this page is evaluated live from the controls rather than stored
between reruns. There is no session state to go stale, which removes the whole
class of bug where a card on screen describes an input that was replaced two
interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.ten_dlc_compliance_validator.core import (
    DISQUALIFIED,
    DISQUALIFIER,
    EIN_REGISTRY,
    ENGINE_VERSION,
    MANDATORY,
    READY,
    SAMPLE_CONSENT_DISQUALIFIED,
    SAMPLE_CONSENT_GOOD,
    SAMPLE_CONSENT_THIN,
    SAMPLE_MESSAGE_EMOJI,
    SAMPLE_MESSAGE_GOOD,
    SAMPLE_MESSAGE_THIN,
    brand_rows,
    check_message,
    consent_fix,
    consent_rows,
    consent_verdict,
    match_brand,
    scan_consent,
)

CONSENT_PRESETS = {
    "Compliant page": SAMPLE_CONSENT_GOOD,
    "Thin page, the usual first draft": SAMPLE_CONSENT_THIN,
    "Compliant page with sharing language": SAMPLE_CONSENT_DISQUALIFIED,
    "Empty, paste your own": "",
}

MESSAGE_PRESETS = {
    "Compliant sample": SAMPLE_MESSAGE_GOOD,
    "Shortened link, no opt out": SAMPLE_MESSAGE_THIN,
    "One emoji, which halves the length": SAMPLE_MESSAGE_EMOJI,
    "Empty, paste your own": "",
}

BRAND_PRESETS = {
    "Missing the full stop": ("Northwind Logistics, Inc", "47-1829304"),
    "Exact, as filed": ("Northwind Logistics, Inc.", "47-1829304"),
    "Trade name instead of the legal one": ("Northwind Logistics",
                                            "47-1829304"),
    "Ampersand spelled out": ("Bright and Early Coaching LLC", "82-4471903"),
    "EIN not on file": ("Northwind Logistics, Inc.", "99-9999999"),
}


def _load_brand() -> None:
    """Load a preset into the two fields the checker reads."""
    name, ein = BRAND_PRESETS[st.session_state.get("dlc_brand_preset",
                                                  FIRST_BRAND_PRESET)]
    st.session_state["dlc_legal_name"] = name
    st.session_state["dlc_ein"] = ein


# Seeded from the preset the selector opens on, so the two never disagree.
FIRST_BRAND_PRESET = next(iter(BRAND_PRESETS))
DEFAULT_NAME, DEFAULT_EIN = BRAND_PRESETS[FIRST_BRAND_PRESET]


def render() -> None:
    inject()
    # Seeded once rather than passed as a widget default, because a widget that
    # carries a default and is also written through session state is explicitly
    # unsupported: Streamlit warns, and the behaviour is not guaranteed to hold.
    st.session_state.setdefault("dlc_legal_name", DEFAULT_NAME)
    st.session_state.setdefault("dlc_ein", DEFAULT_EIN)

    st.markdown(
        """
<div class="app-hero">
  <h1>10DLC Campaign Registry Compliance Validator</h1>
  <p>A 10DLC registration fails on details that look like nothing: a legal name
  that differs from the IRS record by one full stop, a consent page missing one
  sentence, a sample message with no opt out in it. Each one is a rejection, a
  vetting fee spent and days of undeliverable texts. This checks all three
  before the submission, and says which characters are wrong rather than
  handing back a score.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Brand Identity**: check the legal name against the EIN "
            "record.\n"
            "2. **Consent Scanner**: paste the opt in page and read the "
            "missing clauses.\n"
            "3. **Sample Message**: paste the SMS and see its segments and "
            "its opt out.\n"
            "4. The brand from step one is carried into steps two and three, "
            "because all three have to name the same company."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. The EIN records are a small "
            "simulated set and no submission is made to The Campaign Registry."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_brand, tab_consent, tab_message = st.tabs(
        ["Brand Identity", "Consent Scanner", "Sample Message"]
    )

    # -----------------------------------------------------------------
    # Brand identity matcher
    # -----------------------------------------------------------------
    with tab_brand:
        st.markdown("#### Legal name against the EIN record")
        st.caption(
            "The registry compares strings, not intentions. A name that reads "
            "the same to a person and differs by a character is an instant "
            "rejection, and the vetting fee is not returned."
        )

        st.selectbox("Load a brand example", list(BRAND_PRESETS),
                     key="dlc_brand_preset", on_change=_load_brand)

        c1, c2 = st.columns([2, 1])
        legal_name = c1.text_input("Legal business name",
                                   key="dlc_legal_name")
        ein = c2.text_input("EIN", key="dlc_ein")

        match = match_brand(legal_name, ein)
        tone = "ok" if match.accepted else "crit"
        tag = "Verified" if match.accepted else "Rejected"

        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(tag)}</span>{esc(match.verdict)}</h4>
  <div class="app-ev">{esc(match.submitted_name or 'no name entered')}</div>
  <p>{esc(match.explanation)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        if match.differences:
            st.markdown("##### Exactly what differs")
            st.dataframe(match.difference_rows(), width="stretch",
                         hide_index=True)

        if match.correction and not match.exact:
            st.markdown("##### Paste this into the brand form")
            st.code(match.correction, language="text")

        st.markdown("##### The submission as the registry reads it")
        st.dataframe(brand_rows(match), width="stretch", hide_index=True)

        st.markdown("##### The simulated EIN records")
        st.caption(
            "A real check runs against the IRS file. These five stand in for "
            "it, and each one is a shape that trips registrations up."
        )
        st.dataframe(
            [{"EIN": record.ein, "Legal name on file": record.legal_name,
              "Entity type": record.entity_type, "State": record.state}
             for record in EIN_REGISTRY],
            width="stretch", hide_index=True)

    brand_for_downstream = (match.record.legal_name if match.record
                            else legal_name)

    # -----------------------------------------------------------------
    # Opt in consent scanner
    # -----------------------------------------------------------------
    with tab_consent:
        st.markdown("#### The opt in page")
        st.caption(
            "A reviewer reads this page clause by clause and stops at the "
            "first one missing. The wording for anything absent is generated "
            "below, in the order it should appear."
        )

        preset = st.selectbox("Load a consent page example",
                              list(CONSENT_PRESETS), key="dlc_consent_preset")
        consent_text = st.text_area(
            "Consent page wording", value=CONSENT_PRESETS[preset], height=200,
            key=f"dlc_consent_text_{list(CONSENT_PRESETS).index(preset)}")

        findings = scan_consent(consent_text, brand_for_downstream)
        verdict = consent_verdict(findings)
        tone = {READY: "ok", DISQUALIFIED: "crit"}.get(verdict.headline, "warn")

        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(verdict.headline)}</span>
  {verdict.passing_mandatory} of {verdict.total_mandatory} mandatory clauses
  present</h4>
  <div class="app-ev">checked against {esc(brand_for_downstream)}</div>
  <p>{esc(verdict.summary)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### Clause by clause")
        st.dataframe(consent_rows(findings), width="stretch", hide_index=True)

        for finding in findings:
            if finding.passing:
                continue
            is_disqualifier = finding.clause.level == DISQUALIFIER
            card_tone = "crit" if is_disqualifier or \
                finding.clause.level == MANDATORY else "warn"
            st.markdown(
                f"""
<div class="app-card {card_tone}">
  <h4><span class="app-tag {card_tone}">{esc(finding.clause.level)}</span>
  {esc(finding.clause.label)}</h4>
  <div class="app-ev">{esc(finding.excerpt or 'not on the page')}</div>
  <p>{esc(finding.clause.why)}</p>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### The wording to add")
        st.code(consent_fix(findings), language="text")

    # -----------------------------------------------------------------
    # Sample message formatter
    # -----------------------------------------------------------------
    with tab_message:
        st.markdown("#### The sample message")
        st.caption(
            "Carriers read the message, not the intention behind it. The opt "
            "out is what gets a campaign rejected, and adding it can push the "
            "message into a second segment, which doubles what every send "
            "costs."
        )

        c1, c2 = st.columns([2, 1])
        message_preset = c1.selectbox("Load a message example",
                                      list(MESSAGE_PRESETS),
                                      key="dlc_message_preset")
        first_message = c2.checkbox("This is the first message of the campaign",
                                    value=True, key="dlc_first_message")
        message_text = st.text_area(
            "Sample message", value=MESSAGE_PRESETS[message_preset], height=120,
            key=f"dlc_message_text_{list(MESSAGE_PRESETS).index(message_preset)}")

        report = check_message(message_text, brand_for_downstream,
                               first_message=first_message)
        tone = "ok" if report.ready else "crit"
        segment_tone = "ok" if report.segments <= 1 else "warn"

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{report.units}</div>
    <div class="l">Characters counted</div></div>
  <div class="app-kpi {segment_tone}"><div class="n">{report.segments}</div>
    <div class="l">Segments billed</div></div>
  <div class="app-kpi"><div class="n">{esc(report.encoding)}</div>
    <div class="l">Encoding</div></div>
  <div class="app-kpi {tone}">
    <div class="n">{sum(1 for c in report.checks if c.passing)}/{len(report.checks)}</div>
    <div class="l">Checks passing</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">
  {esc('Ready to submit' if report.ready else 'Will be rejected')}</span>
  {len(report.failing)} issue(s) on this message</h4>
  <div class="app-ev">{esc(report.text or 'no message entered')}</div>
  <p>{esc(report.encoding)} at {report.units} units, billed as
  {report.segments} segment(s).</p>
</div>
""",
            unsafe_allow_html=True,
        )

        if report.encoding != "GSM 7 bit":
            st.warning(
                "One character outside the GSM alphabet, usually an emoji or a "
                "curly quote pasted from a document, moves the whole message "
                "to UCS 2 and cuts the single segment length from 160 to 70."
            )

        st.markdown("##### Check by check")
        st.dataframe(report.rows(), width="stretch", hide_index=True)

        for check in report.failing:
            card_tone = "crit" if check.level == MANDATORY else "warn"
            st.markdown(
                f"""
<div class="app-card {card_tone}">
  <h4><span class="app-tag {card_tone}">{esc(check.level)}</span>
  {esc(check.label)}</h4>
  <p>{esc(check.detail)}</p>
  <div class="app-ev">{esc(check.fix)}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        if report.corrected != report.text:
            st.markdown("##### The message with the missing parts added")
            st.code(report.corrected, language="text")
            if report.corrected_segments > report.segments:
                st.warning(
                    f"Adding the opt out takes this from {report.segments} to "
                    f"{report.corrected_segments} segments, so every send now "
                    f"costs {report.corrected_segments} times the single "
                    f"segment rate. Shortening the body elsewhere is usually "
                    f"cheaper than paying that on every message forever."
                )

    ready_all = (match.accepted and consent_verdict(findings).ready
                 and report.ready)
    st.markdown(
        f"""
<div class="app-card {'ok' if ready_all else 'warn'}">
  <h4><span class="app-tag {'ok' if ready_all else 'warn'}">
  {esc('All three pass' if ready_all else 'Not ready')}</span>
  Submission readiness</h4>
  <p>{esc('Brand, consent page and sample message all pass, so the '
          'registration can be submitted.' if ready_all else
          'The registry checks all three. Any one of them failing stops the '
          'campaign, so fix the tabs marked above before submitting.')}</p>
</div>
""",
        unsafe_allow_html=True,
    )

    st.markdown(
        f"""
<div class="app-foot">
10DLC Campaign Registry Compliance Validator, engine version {ENGINE_VERSION}.
A simulator: no submission is made to The Campaign Registry, no carrier is
contacted and the EIN records are a small simulated set rather than the IRS
file. Everything on the page is evaluated live from the controls, so nothing on
screen can describe an input that has since been replaced.
</div>
""",
        unsafe_allow_html=True,
    )
