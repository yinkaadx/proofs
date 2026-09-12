"""WP Form Debugger: enterprise WordPress form diagnostics.

Streamlit front end. All analysis logic lives in wpfd_core.py.
"""

from __future__ import annotations

from html import escape

import streamlit as st

from wpfd_core import (
    ENGINE_VERSION,
    KNOWN_ISSUES,
    PROVIDERS,
    SAMPLE_BROKEN_HTML,
    SAMPLE_HEALTHY_HTML,
    SEVERITY_LABEL,
    analyze_html,
    build_report,
    severity_counts,
    simulate_smtp,
)

st.set_page_config(
    page_title="WP Form Debugger",
    page_icon="🛠",
    layout="wide",
    initial_sidebar_state="expanded",
)

STYLE = """
<style>
:root {
  --wpfd-ink: #0f172a;
  --wpfd-muted: #64748b;
  --wpfd-line: #e2e8f0;
  --wpfd-accent: #1d4ed8;
  --wpfd-crit: #b91c1c;
  --wpfd-warn: #b45309;
  --wpfd-ok: #047857;
  --wpfd-card: #ffffff;
  --wpfd-soft: #f8fafc;
}
@media (prefers-color-scheme: dark) {
  :root {
    --wpfd-ink: #e2e8f0;
    --wpfd-muted: #94a3b8;
    --wpfd-line: #334155;
    --wpfd-accent: #60a5fa;
    --wpfd-crit: #f87171;
    --wpfd-warn: #fbbf24;
    --wpfd-ok: #34d399;
    --wpfd-card: #111827;
    --wpfd-soft: #0f172a;
  }
}
.wpfd-hero {
  border: 1px solid var(--wpfd-line);
  border-left: 4px solid var(--wpfd-accent);
  border-radius: 10px;
  padding: 1.35rem 1.6rem;
  background: var(--wpfd-card);
  margin-bottom: 1.25rem;
}
.wpfd-hero h1 {
  margin: 0 0 .3rem 0;
  font-size: 1.65rem;
  letter-spacing: -.02em;
  color: var(--wpfd-ink);
}
.wpfd-hero p { margin: 0; color: var(--wpfd-muted); font-size: .95rem; line-height: 1.5; }
.wpfd-kpis { display: flex; gap: .75rem; flex-wrap: wrap; margin: .25rem 0 1rem 0; }
.wpfd-kpi {
  flex: 1 1 150px;
  border: 1px solid var(--wpfd-line);
  border-radius: 10px;
  padding: .85rem 1rem;
  background: var(--wpfd-card);
}
.wpfd-kpi .n { font-size: 1.7rem; font-weight: 650; line-height: 1.1; color: var(--wpfd-ink); }
.wpfd-kpi .l {
  font-size: .72rem; text-transform: uppercase; letter-spacing: .09em;
  color: var(--wpfd-muted); margin-top: .2rem;
}
.wpfd-kpi.crit .n { color: var(--wpfd-crit); }
.wpfd-kpi.warn .n { color: var(--wpfd-warn); }
.wpfd-kpi.ok .n { color: var(--wpfd-ok); }
.wpfd-card {
  border: 1px solid var(--wpfd-line);
  border-radius: 10px;
  padding: 1rem 1.15rem;
  background: var(--wpfd-card);
  margin-bottom: .8rem;
}
.wpfd-card.crit { border-left: 4px solid var(--wpfd-crit); }
.wpfd-card.warn { border-left: 4px solid var(--wpfd-warn); }
.wpfd-card.info { border-left: 4px solid var(--wpfd-accent); }
.wpfd-card h4 { margin: 0 0 .35rem 0; font-size: 1rem; color: var(--wpfd-ink); }
.wpfd-card p { margin: .3rem 0; color: var(--wpfd-ink); font-size: .9rem; line-height: 1.55; }
.wpfd-tag {
  display: inline-block; font-size: .68rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .08em;
  padding: .12rem .5rem; border-radius: 999px; margin-right: .5rem;
}
.wpfd-tag.crit { background: rgba(185,28,28,.12); color: var(--wpfd-crit); }
.wpfd-tag.warn { background: rgba(180,83,9,.12); color: var(--wpfd-warn); }
.wpfd-tag.info { background: rgba(29,78,216,.12); color: var(--wpfd-accent); }
.wpfd-ev {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: .8rem; background: var(--wpfd-soft);
  border: 1px solid var(--wpfd-line); border-radius: 6px;
  padding: .45rem .6rem; margin: .45rem 0; color: var(--wpfd-ink);
  overflow-x: auto; white-space: pre-wrap; word-break: break-word;
}
.wpfd-stage {
  display: flex; gap: .7rem; align-items: baseline;
  border-bottom: 1px solid var(--wpfd-line); padding: .55rem .1rem;
}
.wpfd-stage:last-child { border-bottom: 0; }
.wpfd-stage .s { font-weight: 700; font-size: .72rem; letter-spacing: .07em; min-width: 4.2rem; }
.wpfd-stage .s.pass { color: var(--wpfd-ok); }
.wpfd-stage .s.warn { color: var(--wpfd-warn); }
.wpfd-stage .s.fail { color: var(--wpfd-crit); }
.wpfd-stage .s.skip { color: var(--wpfd-muted); }
.wpfd-stage .n { font-weight: 600; min-width: 11rem; color: var(--wpfd-ink); font-size: .88rem; }
.wpfd-stage .d { color: var(--wpfd-muted); font-size: .86rem; line-height: 1.5; }
.wpfd-foot {
  border-top: 1px solid var(--wpfd-line); margin-top: 2rem; padding-top: .9rem;
  color: var(--wpfd-muted); font-size: .78rem; line-height: 1.6;
}
@media (max-width: 640px) {
  .wpfd-stage { flex-direction: column; gap: .15rem; }
  .wpfd-stage .n { min-width: 0; }
}
</style>
"""
st.markdown(STYLE, unsafe_allow_html=True)

st.markdown(
    """
<div class="wpfd-hero">
  <h1>WP Form Debugger</h1>
  <p>Diagnose why a WordPress form stopped submitting or stopped delivering,
  then copy the exact PHP, JavaScript and wp-config.php fix. Static audit of your
  form markup, a triage library for the known failure modes, and an SMTP
  delivery check that validates your mail path before you touch the server.</p>
</div>
""",
    unsafe_allow_html=True,
)

with st.sidebar:
    st.subheader("Engagement")
    site_label = st.text_input("Site or client label", value="",
                               placeholder="acme.com contact form",
                               help="Appears in the downloaded report header.")
    st.divider()
    st.subheader("How to use")
    st.markdown(
        "1. **Form Audit**: paste the rendered form HTML.\n"
        "2. **Known Issues**: pick the symptoms you see.\n"
        "3. **SMTP Delivery Check**: validate the mail path.\n"
        "4. **Report**: download everything as one Markdown file."
    )
    st.divider()
    st.caption(
        "Copy rendered markup, not page source: right click the form, "
        "Inspect, right click the <form> node, Copy, Copy outerHTML."
    )
    st.caption(f"Engine version {ENGINE_VERSION}")

tab_audit, tab_issues, tab_smtp, tab_report = st.tabs(
    ["Form Audit", "Known Issues", "SMTP Delivery Check", "Report"]
)


def _sev_class(sev: str) -> str:
    return {"critical": "crit", "warning": "warn", "info": "info"}[sev]


def esc(text: str) -> str:
    """Escape before interpolating into an unsafe_allow_html block. Evidence
    strings are pasted markup, so unescaped they would render as live HTML."""
    return escape(str(text or ""), quote=True)


# ---------------------------------------------------------------------------
# Tab 1: Form audit
# ---------------------------------------------------------------------------
with tab_audit:
    st.markdown("#### Paste your form HTML")
    c1, c2, c3 = st.columns([1, 1, 2])
    if c1.button("Load broken sample", use_container_width=True):
        st.session_state["html_input"] = SAMPLE_BROKEN_HTML
    if c2.button("Load healthy sample", use_container_width=True):
        st.session_state["html_input"] = SAMPLE_HEALTHY_HTML

    html = st.text_area(
        "Form markup",
        key="html_input",
        height=260,
        placeholder="<form id=\"contact-form\" method=\"post\" action=\"...\"> ... </form>",
        label_visibility="collapsed",
    )

    if html and html.strip():
        findings = analyze_html(html)
        st.session_state["findings"] = findings
        counts = severity_counts(findings)
        st.markdown(
            f"""
<div class="wpfd-kpis">
  <div class="wpfd-kpi crit"><div class="n">{counts['critical']}</div><div class="l">Critical</div></div>
  <div class="wpfd-kpi warn"><div class="n">{counts['warning']}</div><div class="l">Warnings</div></div>
  <div class="wpfd-kpi"><div class="n">{counts['info']}</div><div class="l">Informational</div></div>
  <div class="wpfd-kpi ok"><div class="n">{len(findings)}</div><div class="l">Checks flagged</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        if counts["critical"] == 0 and counts["warning"] == 0:
            st.success("No blocking defects found in the markup. If submissions still fail, "
                       "the fault is server side: work through Known Issues and the SMTP Delivery Check.")

        for f in findings:
            cls = _sev_class(f.severity)
            st.markdown(
                f"""
<div class="wpfd-card {cls}">
  <h4><span class="wpfd-tag {cls}">{SEVERITY_LABEL[f.severity]}</span>{esc(f.title)}</h4>
  <div class="wpfd-ev">{esc(f.evidence)}</div>
  <p>{esc(f.explanation)}</p>
</div>
""",
                unsafe_allow_html=True,
            )
            if f.fix_php or f.fix_js or f.fix_wpconfig or f.fix_notes:
                with st.expander(f"Fix: {f.title}"):
                    if f.fix_php:
                        st.markdown("**functions.php**")
                        st.code(f.fix_php, language="php")
                    if f.fix_js:
                        st.markdown("**JavaScript**")
                        st.code(f.fix_js, language="javascript")
                    if f.fix_wpconfig:
                        st.markdown("**wp-config.php**")
                        st.code(f.fix_wpconfig, language="php")
                    if f.fix_notes:
                        st.info(f.fix_notes)
    else:
        st.session_state["findings"] = None
        st.info("Paste form markup above, or load a sample, to run the audit.")

    with st.expander("What the audit checks"):
        st.markdown(
            "- Missing or wrong `action`, including `mailto:` and plain `http://` submissions\n"
            "- `admin-ajax.php` posts with no `action` field (the 400 and bare 0 responses)\n"
            "- Missing security nonce, and nonce expiry under page caching\n"
            "- `GET` where the handler reads `$_POST`\n"
            "- Fields with no `name` attribute, which never reach PHP\n"
            "- File uploads without `multipart/form-data`\n"
            "- Nested forms, duplicate ids, missing submit control, typeless buttons\n"
            "- Inline jQuery using `$` under noConflict, and hardcoded admin-ajax URLs\n"
            "- Contact Form 7, WPForms, Gravity Forms, Elementor and reCAPTCHA markers"
        )

# ---------------------------------------------------------------------------
# Tab 2: Known issues
# ---------------------------------------------------------------------------
with tab_issues:
    st.markdown("#### Select the symptoms you are seeing")
    picked_keys = st.multiselect(
        "Known issues",
        options=list(KNOWN_ISSUES.keys()),
        format_func=lambda k: KNOWN_ISSUES[k].title,
        label_visibility="collapsed",
        placeholder="Choose one or more known failure modes",
    )
    picked = [KNOWN_ISSUES[k] for k in picked_keys]
    st.session_state["issues"] = picked

    if not picked:
        st.info("Pick the failure modes that match the reported behaviour. "
                "Each one returns its diagnosis plus the exact fix.")
    for issue in picked:
        st.markdown(
            f"""
<div class="wpfd-card crit">
  <h4>{esc(issue.title)}</h4>
  <p><strong>Symptoms:</strong> {esc(issue.symptoms)}</p>
  <p><strong>Diagnosis:</strong> {esc(issue.diagnosis)}</p>
</div>
""",
            unsafe_allow_html=True,
        )
        with st.expander(f"Fix: {issue.title}"):
            if issue.fix_php:
                st.markdown("**functions.php**")
                st.code(issue.fix_php, language="php")
            if issue.fix_js:
                st.markdown("**JavaScript**")
                st.code(issue.fix_js, language="javascript")
            if issue.fix_wpconfig:
                st.markdown("**wp-config.php**")
                st.code(issue.fix_wpconfig, language="php")
            if issue.fix_notes:
                st.info(issue.fix_notes)
            if not (issue.fix_php or issue.fix_js or issue.fix_wpconfig):
                st.caption("This issue is resolved by configuration rather than code; see the notes above.")

# ---------------------------------------------------------------------------
# Tab 3: SMTP delivery check
# ---------------------------------------------------------------------------
with tab_smtp:
    st.markdown("#### Validate the mail path before you touch the server")
    st.caption(
        "This runs a simulated handshake: every stage is checked against the "
        "provider's own requirements and the port and encryption pairings a real "
        "relay enforces. No connection is opened and no credential leaves this page."
    )

    provider = st.selectbox("Mail provider", options=list(PROVIDERS.keys()))
    profile = PROVIDERS[provider]
    default_ports = list(profile["ports"].keys()) or [587, 465, 2525, 25]

    c1, c2, c3 = st.columns([2, 1, 1])
    host = c1.text_input("SMTP host", value=profile["host"],
                         placeholder="smtp.example.com")
    port = c2.selectbox("Port", options=sorted(set(default_ports + [587, 465, 2525, 25])),
                        index=sorted(set(default_ports + [587, 465, 2525, 25])).index(
                            default_ports[0] if default_ports else 587))
    encryption = c3.selectbox("Encryption", options=["STARTTLS", "SSL", "None"],
                              index=0 if profile["ports"].get(port, "STARTTLS") != "SSL" else 1)

    c4, c5 = st.columns(2)
    username = c4.text_input("SMTP username", placeholder="postmaster@example.com")
    password = c5.text_input("SMTP password or API key", type="password",
                             placeholder="App password or API key")

    c6, c7 = st.columns(2)
    from_addr = c6.text_input("From address", placeholder="no-reply@example.com")
    from_name = c7.text_input("From name", value="Website")

    if profile["note"]:
        st.info(profile["note"])

    if st.button("Run delivery check", type="primary"):
        result = simulate_smtp(provider, host, int(port), encryption,
                               username, password, from_addr, from_name or "Website")
        st.session_state["smtp"] = result

    result = st.session_state.get("smtp")
    if result:
        st.markdown("##### Handshake trace")
        rows = "".join(
            f'<div class="wpfd-stage"><div class="s {esc(s)}">{esc(s.upper())}</div>'
            f'<div class="n">{esc(n)}</div><div class="d">{esc(d)}</div></div>'
            for n, s, d in result.stages
        )
        st.markdown(f'<div class="wpfd-card">{rows}</div>', unsafe_allow_html=True)

        if result.verdict_level == "pass":
            st.success(result.verdict)
        elif result.verdict_level == "warn":
            st.warning(result.verdict)
        else:
            st.error(result.verdict)

        if result.recommendations:
            st.markdown("##### Recommendations")
            for rec in result.recommendations:
                st.markdown(f"- {rec}")

        st.markdown("##### Generated fix")
        st.markdown("**wp-config.php** (add above the line that says *That's all, stop editing!*)")
        st.code(result.fix_wpconfig, language="php")
        st.markdown("**functions.php**")
        st.code(result.fix_php, language="php")
        st.warning(
            "The wp-config.php block above contains the SMTP password exactly as "
            "you typed it, so treat this screen as sensitive: paste it straight "
            "into the file and keep wp-config.php out of version control. The "
            "downloadable report redacts the password so it stays safe to share."
        )
        st.caption(
            "Both snippets are fragments for files that already open a PHP block, "
            "so neither carries its own opening tag. Paste them inside the "
            "existing PHP code."
        )

# ---------------------------------------------------------------------------
# Tab 4: Report
# ---------------------------------------------------------------------------
with tab_report:
    st.markdown("#### Client ready report")
    findings = st.session_state.get("findings")
    issues = st.session_state.get("issues") or []
    smtp = st.session_state.get("smtp")

    if findings is None and not issues and smtp is None:
        st.info("Run at least one of the audit, triage or delivery check to build a report.")
    else:
        report = build_report(findings, issues, smtp, site_label)
        name_part = "".join(ch for ch in site_label if ch.isalnum() or ch in "-_") or "wp-form-debugger"
        st.download_button(
            "Download report (Markdown)",
            data=report,
            file_name=f"{name_part}-form-diagnostics.md",
            mime="text/markdown",
            type="primary",
        )
        st.markdown("##### Preview")
        st.markdown(report)

st.markdown(
    f"""
<div class="wpfd-foot">
WP Form Debugger, engine version {ENGINE_VERSION}. Static analysis and a simulated
SMTP handshake: no connection is opened, no credential is transmitted or stored,
and nothing you paste leaves this session. Always apply fixes on staging first and
keep a copy of wp-config.php and functions.php before editing.
</div>
""",
    unsafe_allow_html=True,
)
