"""Server Side Tracking and Claude Agent Console.

Rendered inside the hub app. Every verdict on this page is computed from the
controls on each run, so nothing on screen can describe a container state that
was replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.sgtm_claude_agent_console.core import (
    DISPATCH_FULL,
    DISPATCH_MODELLED_ONLY,
    DISPATCH_RESTRICTED,
    ENGINE_VERSION,
    REGIONS,
    SAMPLE_EVENT_PAIRS,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_OK,
    STANDARD_EVENTS,
    consent_matrix,
    dedup_board,
    evaluate_consent_mode_v2,
    generate_claude_skill_manifest,
    validate_capi_dedup,
)

SEVERITY_TONE = {SEVERITY_CRITICAL: "crit", SEVERITY_HIGH: "warn",
                 SEVERITY_OK: "ok"}
DISPATCH_TONE = {DISPATCH_FULL: "ok", DISPATCH_RESTRICTED: "warn",
                 DISPATCH_MODELLED_ONLY: "info"}


def _kpis(items: list[tuple[str, str]]) -> None:
    cells = "".join(
        f'<div class="app-kpi"><b>{esc(value)}</b><span>{esc(label)}</span></div>'
        for label, value in items)
    st.markdown(f'<div class="app-kpis">{cells}</div>', unsafe_allow_html=True)


def _dedup_card(result) -> None:
    tone = SEVERITY_TONE.get(result.severity, "warn")
    st.markdown(
        f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(result.match_status)}</span>
  {esc(result.event_name)}</h4>
  <p>{esc(result.headline)}.</p>
  <div class="app-ev">{esc(result.fix)}</div>
</div>
""",
        unsafe_allow_html=True,
    )


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>Server Side Tracking and Claude Agent Console</h1>
  <p>A Stape server container moves the measurement off the page, which fixes
  the blocking problem and introduces three quieter ones. A browser event and
  a server event that do not share an id count the same sale twice. A consent
  signal that the container forwards anyway is the breach nobody notices until
  an audit. And a container nobody can read in full keeps a stale tag alive
  through three reviews. Each of those is checkable, and this checks them.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Deduplication**: make the two ids differ and watch the "
            "counted events go to two.\n"
            "2. **Consent**: deny ad user data while granting "
            "personalization and read what the tester says about the pair.\n"
            "3. **Skill manifest**: copy the file and the install path into "
            "your own repository."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live container. No Stape account is "
            "connected, no Pixel fires and no GTM workspace is read."
        )
        st.caption(
            "The consent rules are the rules this tester applies, named in "
            "the open. They are not legal advice."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_dedup, tab_consent, tab_skill = st.tabs(
        ["CAPI Deduplication", "Consent Mode v2", "Claude Skill Manifest"])

    # -----------------------------------------------------------------
    with tab_dedup:
        st.subheader("Will Meta fold these two events into one")
        st.caption(
            "The rule is the event name and the event id, both matching. "
            "Anything else and the platform counts the action twice, which "
            "inflates the conversion count and splits the match signals "
            "across two records instead of concentrating them on one."
        )

        left, right = st.columns(2)
        with left:
            browser_id = st.text_input("Browser eventID on the Pixel call",
                                       value="evt-9f1c-4a7e")
            event_name = st.selectbox("Event name", list(STANDARD_EVENTS),
                                      index=list(STANDARD_EVENTS).index("Purchase"))
        with right:
            server_id = st.text_input("Server event_id on the CAPI payload",
                                      value="evt-9f1c-4a7e")
            custom_name = st.text_input(
                "Or type a custom event name, which overrides the list above",
                value="")

        chosen = custom_name.strip() or event_name
        result = validate_capi_dedup(browser_id, server_id, chosen)
        _kpis([
            ("Status", result.match_status),
            ("Events Meta counts", str(result.counted_events)),
            ("Conversion inflation", f"{result.inflation_pct:.0f} percent"),
            ("Match quality estimate",
             f"{result.emq_impact_points:+.1f} points"),
        ])
        _dedup_card(result)
        if not result.is_standard_event:
            st.caption(
                f"{chosen} is not one of the {len(STANDARD_EVENTS)} standard "
                "event names, so standard optimisation does not read it."
            )

        st.subheader("Five pairs from a real looking container")
        board = dedup_board(SAMPLE_EVENT_PAIRS)
        _kpis([
            ("Pairs checked", str(board["total"])),
            ("Deduplicated", str(board["deduplicated"])),
            ("Double counted", str(board["double_counted"])),
            ("Reported conversions",
             f"{board['events_meta_counts']} for {board['actions_taken']} actions"),
        ])
        st.caption(
            f"{board['deduplicated']} clean plus {board['double_counted']} "
            f"broken equals {board['total']} pairs, and the container reports "
            f"{board['events_meta_counts']} events for "
            f"{board['actions_taken']} actions, an overstatement of "
            f"{board['inflation_pct']:.0f} percent."
        )
        for entry in board["results"]:
            _dedup_card(entry)

    # -----------------------------------------------------------------
    with tab_consent:
        st.subheader("What the tag may dispatch")
        st.caption(
            "ad_user_data is the gate: it decides whether an identifier "
            "leaves at all. ad_personalization decides what that identifier "
            "may then be used for. Granting the second while denying the "
            "first is incoherent rather than permissive, and this says so "
            "instead of quietly picking one."
        )

        left, right = st.columns(2)
        with left:
            region = st.selectbox("Region", list(REGIONS))
            user_data = st.toggle("ad_user_data granted", value=True)
        with right:
            personalization = st.toggle("ad_personalization granted", value=True)

        decision = evaluate_consent_mode_v2(user_data, personalization, region)
        tone = DISPATCH_TONE.get(decision.dispatch_status, "warn")
        _kpis([
            ("Dispatch", decision.dispatch_status),
            ("Identifiers leave",
             "yes" if decision.identifiers_leave_the_browser else "no"),
            ("Express consent",
             "required" if decision.express_consent_required else "not required"),
            ("Safe default",
             "yes" if decision.compliant_default else "check the grant"),
        ])
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(decision.dispatch_status)}</span>
  {esc(decision.region)}</h4>
  <p>{esc(decision.headline)}.</p>
  <div class="app-ev">{esc(decision.legal_basis)}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        allowed, blocked = st.columns(2)
        with allowed:
            st.markdown("**May fire**")
            for item in decision.tags_allowed:
                st.markdown(f"- {item}")
        with blocked:
            st.markdown("**Must not fire**")
            if decision.tags_blocked:
                for item in decision.tags_blocked:
                    st.markdown(f"- {item}")
            else:
                st.markdown("- Nothing is blocked in this state")

        for note in decision.findings:
            st.markdown(
                f'<div class="app-card info"><p>{esc(note)}</p></div>',
                unsafe_allow_html=True)
        st.markdown(
            f'<div class="app-card warn"><p>{esc(decision.fix)}</p></div>',
            unsafe_allow_html=True)

        st.subheader(f"All four signal states in {region}")
        for row in consent_matrix(region):
            mark = "yes" if row.identifiers_leave_the_browser else "no"
            st.markdown(
                f"- ad_user_data **{'granted' if row.ad_user_data_granted else 'denied'}**, "
                f"ad_personalization **{'granted' if row.ad_personalization_granted else 'denied'}** "
                f"gives **{row.dispatch_status}**, identifiers leave: {mark}")

    # -----------------------------------------------------------------
    with tab_skill:
        manifest = generate_claude_skill_manifest()
        st.subheader("A Claude Code skill that audits the container for you")
        st.caption(
            "Clicking through a workspace by hand is how a stale tag survives "
            "three reviews. This skill reads the container through the Tag "
            "Manager API and reports. It is read only by construction: every "
            "call is a GET and it never publishes a version."
        )
        _kpis([
            ("Skill name", manifest.name),
            ("Tools granted", ", ".join(manifest.allowed_tools)),
            ("API", "Tag Manager v2"),
            ("Writes", "none"),
        ])
        st.markdown(f"**Save as** `{manifest.install_path}`")
        st.code(manifest.manifest, language="markdown")

        st.markdown("**Scope the token needs**")
        st.code(manifest.scope, language="text")
        st.markdown("**API base**")
        st.code(manifest.api_base, language="text")
        st.caption(
            "Once the file is in place, ask Claude Code to audit the "
            "container by name. The skill loads itself when the request "
            "matches its description."
        )
