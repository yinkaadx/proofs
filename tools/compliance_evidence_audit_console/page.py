"""Compliance Evidence and WORM Audit Console.

Rendered inside the hub app. Every digest and verdict on this page is
computed from the controls on each run, so nothing on screen can describe a
payload that was replaced two interactions ago.
"""

from __future__ import annotations

import json

import streamlit as st

from shared.theme import esc, inject
from tools.compliance_evidence_audit_console.core import (
    ADMITTED,
    ENGINE_VERSION,
    ENTRY_COMPLETE,
    GENESIS,
    KIND_NONE,
    PROPOSED_STATUSES,
    REJECTED,
    SAMPLE_CHECKS,
    SAMPLE_ENTRY,
    SAMPLE_PAYLOADS,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    TRACKER_FIELDS,
    build_chain,
    canonicalise,
    format_readiness_tracker_entry,
    generate_evidence_hash,
    validate_api_compliance,
    verify_chain,
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
  <h1>Compliance Evidence and WORM Audit Console</h1>
  <p>Three parts of an evidence pipeline, each holding the property that
  decides whether the evidence survives a hostile reader. A hash proves
  integrity only if the same logical record always produces the same digest,
  and only if the digest lives somewhere the writer cannot reach. An intent
  and a deployed behaviour that disagree are a finding ranked by what the gap
  costs rather than by how far apart the numbers are. And a tracker entry
  with a blank field is worse than no entry, because it looks answered.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Evidence**: reorder the keys in the payload and watch the "
            "digest stay the same, then delete a chain record.\n"
            "2. **API**: set an endpoint to return 200 where 403 was "
            "intended and read where that ranks.\n"
            "3. **Tracker**: blank one of the eight points and watch the "
            "formatter refuse rather than emit a gap."
        )
        st.divider()
        st.caption(
            "Nothing here writes to real storage or calls a real endpoint. "
            "The hashing is genuine SHA256 from the standard library and the "
            "test suite checks it against hashlib directly."
        )
        st.caption(
            "This is a formatter and a checker, not legal advice. Whether "
            "the answers are adequate for your obligation is a judgement it "
            "does not make."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_hash, tab_api, tab_tracker = st.tabs(
        ["Evidence Hashing", "API Compliance", "Readiness Tracker"])

    # -----------------------------------------------------------------
    with tab_hash:
        st.subheader("Canonicalise, hash, then chain")
        default = json.dumps(SAMPLE_PAYLOADS[0], indent=2)
        payload_text = st.text_area("The artifact record as JSON",
                                    value=default, height=160)
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            payload = payload_text

        record = generate_evidence_hash(payload)

        _kpis([
            ("Status", record.status),
            ("Canonical bytes", str(record.canonical_bytes)),
            ("Content hash", record.content_hash[:16] or "none"),
            ("Chain hash", record.chain_hash[:16] or "none"),
            ("Self consistent",
             "yes" if record.admitted and record.self_consistent else "no"),
        ])

        tone = "ok" if record.admitted else "crit"
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(record.status)}</span>
  {esc(record.headline)}</h4>
  <p>The digest covers the canonical form rather than the text as typed, so
  reordering the keys or changing the whitespace does not change it.</p>
  <div class="app-ev">Previous link: {esc(record.previous_hash[:24])}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**The canonical form that was hashed**")
        st.code(record.canonical_form or "nothing was hashed", language="json")
        if record.admitted:
            st.markdown("**The digests**")
            st.code(
                f"content = SHA256(canonical)          = {record.content_hash}\n"
                f"chain   = SHA256(previous|content)   = {record.chain_hash}",
                language="text")

        st.markdown("**Findings**")
        for finding in record.findings:
            _finding_card(finding)

        st.subheader("A three record chain, and what deletion does to it")
        chain = build_chain(SAMPLE_PAYLOADS)
        intact = verify_chain(chain)
        tampered = verify_chain(chain[:1] + chain[2:])
        _kpis([
            ("Records", str(intact["records"])),
            ("Intact", "yes" if intact["intact"] else "no"),
            ("Head", intact["head"][:16]),
            ("After a deletion",
             "broken at record " + str(tampered["broken_at"])
             if not tampered["intact"] else "still intact"),
        ])
        for row in chain:
            st.markdown(
                f"- Record {row.sequence}: content `{row.content_hash[:16]}`, "
                f"chains from `{row.previous_hash[:16]}` to "
                f"`{row.chain_hash[:16]}`")
        st.caption(
            f"Remove the second record and verification stops at record "
            f"{tampered['broken_at']}, because record three still commits to "
            f"a predecessor that is no longer there. A per record hash on "
            f"its own would have reported every remaining record as valid."
        )

    # -----------------------------------------------------------------
    with tab_api:
        st.subheader("Intended against deployed")
        left, right = st.columns(2)
        with left:
            endpoint = st.text_input("Endpoint", value="GET /v1/admin/export")
            expected = st.number_input("Status the requirement names",
                                       min_value=100, max_value=599,
                                       value=403, step=1)
        with right:
            observed = st.number_input("Status the deployment returns",
                                       min_value=100, max_value=599,
                                       value=200, step=1)

        check = validate_api_compliance(endpoint or "unnamed endpoint",
                                        int(observed), int(expected))

        _kpis([
            ("Verdict", check.verdict),
            ("Discrepancy", check.kind),
            ("Intended", f"{check.expected_status} ({check.expected_class})"),
            ("Deployed", f"{check.response_code} ({check.observed_class})"),
            ("Rank", f"{check.rank} of 5"),
        ])

        tone = "ok" if check.compliant else (
            "crit" if check.rank <= 2 else "warn")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(check.kind)}</span>
  {esc(check.headline)}</h4>
  <p>Discrepancies are ranked by what they cost rather than by how far apart
  the two numbers are. A success code where an authorisation failure was
  required is the worst thing on this page and is numerically close to
  nothing.</p>
  <div class="app-ev">Lower rank is worse. Rank 0 is an authorisation
  bypass.</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**Findings**")
        for finding in check.findings:
            _finding_card(finding)

        st.subheader("A sample sweep, worst first")
        rows = sorted((validate_api_compliance(ep, obs, exp)
                       for ep, obs, exp in SAMPLE_CHECKS),
                      key=lambda r: r.rank)
        for row in rows:
            marker = "compliant" if row.compliant else f"**{row.kind}**"
            st.markdown(
                f"- `{row.endpoint}`: intended {row.expected_status}, "
                f"deployed {row.response_code}, {marker}")
        st.caption(
            f"{sum(1 for r in rows if r.compliant)} compliant and "
            f"{sum(1 for r in rows if not r.compliant)} discrepant equals "
            f"{len(rows)} endpoints, which is the whole sweep."
        )

    # -----------------------------------------------------------------
    with tab_tracker:
        st.subheader("Eight points, or nothing")
        values = {}
        for key, label in TRACKER_FIELDS:
            if key == "proposed_status":
                values[key] = st.selectbox(
                    label, list(PROPOSED_STATUSES),
                    index=list(PROPOSED_STATUSES).index(
                        SAMPLE_ENTRY["proposed_status"]))
            elif key in ("answer", "actual_behavior", "limitations",
                         "corrective_action"):
                values[key] = st.text_area(label,
                                           value=str(SAMPLE_ENTRY[key]),
                                           height=80, key=f"trk_{key}")
            else:
                values[key] = st.text_input(label,
                                            value=str(SAMPLE_ENTRY[key]),
                                            key=f"trk_{key}")

        entry = format_readiness_tracker_entry(**values)

        _kpis([
            ("Status", entry.status),
            ("Points answered",
             f"{sum(1 for f in entry.fields if f.present)} of {entry.point_count}"),
            ("Blank", str(len(entry.missing))),
            ("Placeholders", str(len(entry.placeholders))),
            ("Entry digest", entry.entry_hash[:16] or "none"),
        ])

        tone = "ok" if entry.complete else "crit"
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(entry.status)}</span>
  {esc(entry.headline)}</h4>
  <p>{esc('Every point carries an answer, so the entry was produced and'
           ' hashed.' if entry.complete
           else 'No entry was produced. A row with a blank field reads as'
                ' answered to everybody except the person who has to defend'
                ' it.')}</p>
  <div class="app-ev">Limitations and corrective action are required even
  when the answer is none, because writing none is a statement and a blank
  is not.</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**Point by point**")
        for check in entry.fields:
            row_tone = "ok" if not check.problem else "crit"
            state = check.problem or "answered"
            preview = (check.value[:90] + "..." if len(check.value) > 90
                       else check.value or "nothing supplied")
            st.markdown(
                f"""
<div class="app-card {row_tone}">
  <h4><span class="app-tag {row_tone}">{esc(state)}</span>
  {esc(check.label)}</h4>
  <p>{esc(preview)}</p>
</div>
""",
                unsafe_allow_html=True,
            )

        if entry.complete:
            st.markdown("**The deliverable**")
            st.code(entry.entry_text, language="text")
            st.markdown("**Its digest**")
            st.code(entry.entry_hash, language="text")

        st.markdown("**Findings**")
        for finding in entry.findings:
            _finding_card(finding)
