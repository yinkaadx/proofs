"""Secure SharePoint Architecture Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine can read a real tenant through Microsoft Graph.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.sharepoint_zero_trust_simulator.core import (
    BAND_BLOCKED,
    BAND_READY,
    CLIENT_APPS,
    COUNTRIES,
    CRITICAL,
    DEFAULT_TENANT,
    DEVICE_POLICIES,
    ENGINE_VERSION,
    GUEST,
    HARDENED_TENANT,
    HIGH,
    LIBRARIES,
    LINK_TYPES,
    RISK_LEVELS,
    SHARING_LEVELS,
    USER_TYPES,
    SignInContext,
    TenantConfig,
    audit_findings,
    audit_rows,
    audit_score,
    evaluate_sign_in,
    matrix_rows,
    remediation_script,
    role_summary_rows,
)

STATE = "sharepoint_ztr_state"

TONE_BY_SEVERITY = {CRITICAL: "crit", HIGH: "warn"}

# Which config field each control on screen writes to. Named once so the
# checklist, the matrix and the sign in simulator cannot drift apart.
TOGGLES: tuple[tuple[str, str], ...] = (
    ("mfa_enforced", "Multi factor authentication enforced"),
    ("legacy_auth_blocked", "Legacy authentication blocked"),
    ("dlp_policy_enabled", "Data loss prevention policy active"),
    ("versioning_enabled", "Version history retained"),
    ("site_creation_restricted", "Site creation restricted to administrators"),
)


def _state() -> dict:
    if STATE not in st.session_state:
        st.session_state[STATE] = {"config": DEFAULT_TENANT}
    return st.session_state[STATE]


def _config() -> TenantConfig:
    return _state()["config"]


def _load(config: TenantConfig) -> None:
    """Load a whole posture at once, and clear the widgets bound to it.

    The controls are rebuilt from the loaded configuration on the next render,
    so their stored values have to go: a widget key that survives would put the
    old setting straight back on the following interaction.
    """
    _state()["config"] = config
    for key in list(st.session_state.keys()):
        if str(key).startswith("sp_ctl_"):
            del st.session_state[key]


def _apply_controls() -> None:
    """Read every control out of live widget state and rebuild the config.

    Read here rather than passed in as callback arguments: an argument is bound
    at the previous render, so it is one interaction stale.
    """
    state = _state()
    current = state["config"]
    updates = {field: bool(st.session_state.get(f"sp_ctl_{field}",
                                                getattr(current, field)))
               for field, _ in TOGGLES}
    updates["external_sharing"] = st.session_state.get(
        "sp_ctl_external_sharing", current.external_sharing)
    updates["default_link_type"] = st.session_state.get(
        "sp_ctl_default_link_type", current.default_link_type)
    updates["unmanaged_device_policy"] = st.session_state.get(
        "sp_ctl_unmanaged_device_policy", current.unmanaged_device_policy)
    for field in ("guest_expiry_days", "anonymous_link_expiry_days",
                  "idle_session_timeout_minutes"):
        updates[field] = int(st.session_state.get(f"sp_ctl_{field}",
                                                  getattr(current, field)))
    state["config"] = TenantConfig(**updates)


def render() -> None:
    inject()
    config = _config()
    findings = audit_findings(config)
    score = audit_score(findings)

    st.markdown(
        """
<div class="app-hero">
  <h1>Secure SharePoint Architecture Console</h1>
  <p>Three questions decide whether a SharePoint rollout is safe to sign off:
  who gets in, what they can touch once they are in, and whether the tenant is
  configured to keep it that way. All three are answered here from one
  configuration, so they cannot quietly disagree with each other.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Entra ID Login**: pick a user type and watch the policies "
            "decide.\n"
            "2. **Access Matrix**: see what each permission level reaches.\n"
            "3. **Deployment Audit**: close the open controls and watch the "
            "other two tabs move with them.\n"
            "4. Start on the Microsoft default posture, which fails its own "
            "audit."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. Entra ID, SharePoint and the "
            "tenant settings are all simulated in this session."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    band_tone = {BAND_READY: "ok", BAND_BLOCKED: "crit"}.get(score.band, "warn")
    st.markdown(
        f"""
<div class="app-kpis">
  <div class="app-kpi {band_tone}"><div class="n">{score.percent}%</div>
    <div class="l">Controls passing</div></div>
  <div class="app-kpi crit"><div class="n">{score.open_critical}</div>
    <div class="l">Critical open</div></div>
  <div class="app-kpi warn"><div class="n">{score.open_high}</div>
    <div class="l">High severity open</div></div>
  <div class="app-kpi"><div class="n">{esc(config.external_sharing)}</div>
    <div class="l">External sharing</div></div>
</div>
""",
        unsafe_allow_html=True,
    )

    tab_login, tab_matrix, tab_audit = st.tabs(
        ["Entra ID Login", "Access Matrix", "Deployment Audit"]
    )

    # -----------------------------------------------------------------
    # Entra ID login simulator
    # -----------------------------------------------------------------
    with tab_login:
        st.markdown("#### Sign in as")
        st.caption(
            "Every policy in the stack is evaluated, not just the first one to "
            "object, because the useful part is seeing which policies had an "
            "opinion. A block anywhere denies the sign in, which is how Entra "
            "ID resolves the conflict."
        )

        c1, c2, c3 = st.columns(3)
        user_type = c1.selectbox("User type", USER_TYPES, key="sp_user_type")
        country = c2.selectbox("Sign in location", COUNTRIES, key="sp_country")
        client_app = c3.selectbox("Client", CLIENT_APPS, key="sp_client")

        c4, c5, c6 = st.columns(3)
        risk_level = c4.selectbox("Sign in risk", RISK_LEVELS, key="sp_risk")
        device_compliant = c5.checkbox("Device is enrolled and compliant",
                                       value=True, key="sp_device")
        mfa_completed = c6.checkbox("Second factor satisfied", value=True,
                                    key="sp_mfa")

        decision = evaluate_sign_in(
            SignInContext(user_type=user_type, device_compliant=device_compliant,
                          mfa_completed=mfa_completed, country=country,
                          risk_level=risk_level, client_app=client_app),
            config)

        tone = "ok" if decision.allowed else "crit"
        tag = "Granted" if decision.allowed else "Blocked"
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(tag)}</span>{esc(decision.headline)}</h4>
  <div class="app-ev">{esc(decision.user_type)} &nbsp; permission level
  {esc(decision.role)}</div>
  <p>{esc(decision.summary)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### Conditional access evaluation")
        st.dataframe(decision.rows(), width="stretch", hide_index=True)

        blocking = decision.blocking_policy
        if blocking is not None:
            st.error(f"{blocking.name}: {blocking.reason}")

        if decision.session_controls:
            st.markdown("##### Session controls on this sign in")
            for control in decision.session_controls:
                st.markdown(
                    f"""
<div class="app-card info"><p>{esc(control)}</p></div>
""",
                    unsafe_allow_html=True,
                )
        elif decision.allowed:
            st.warning(
                "No session controls apply. The session can be used from "
                "anywhere, for as long as the browser stays open, with "
                "downloads allowed."
            )

        if decision.allowed:
            st.markdown("##### What this sign in reaches")
            st.dataframe(
                [{"Document library": row["Document library"],
                  "Sensitivity": row["Sensitivity"],
                  "This session may": row["External guest"]
                  if decision.user_type == GUEST else row[decision.role]}
                 for row in matrix_rows(config)],
                width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Role based access matrix
    # -----------------------------------------------------------------
    with tab_matrix:
        st.markdown("#### What each permission level reaches")
        st.caption(
            "SharePoint ships three permission levels. The tenant has the last "
            "word over all of them: a guest holding Visitor still reaches "
            "nothing confidential, and nothing at all when external sharing is "
            "off."
        )
        st.dataframe(matrix_rows(config), width="stretch", hide_index=True)

        st.markdown("##### The levels in one line each")
        st.dataframe(role_summary_rows(), width="stretch", hide_index=True)

        confidential = [library.name for library in LIBRARIES
                        if not library.guest_accessible]
        st.caption(
            f"{', '.join(confidential)} are never guest reachable at any "
            f"permission level, because the boundary is the library, not the "
            f"person."
        )

    # -----------------------------------------------------------------
    # Secure deployment audit
    # -----------------------------------------------------------------
    with tab_audit:
        st.markdown("#### Tenant configuration")
        st.caption(
            "This starts on the posture Microsoft hands over, which fails its "
            "own audit. Change a control and every tab moves with it."
        )

        c1, c2 = st.columns(2)
        c1.button("Load the Microsoft default posture", on_click=_load,
                  args=(DEFAULT_TENANT,), key="sp_load_default")
        c2.button("Load the hardened posture", type="primary", on_click=_load,
                  args=(HARDENED_TENANT,), key="sp_load_hardened")

        with st.form("sp_controls"):
            f1, f2, f3 = st.columns(3)
            f1.selectbox("External sharing", SHARING_LEVELS,
                         index=SHARING_LEVELS.index(config.external_sharing),
                         key="sp_ctl_external_sharing")
            f2.selectbox("Default sharing link", LINK_TYPES,
                         index=LINK_TYPES.index(config.default_link_type),
                         key="sp_ctl_default_link_type")
            f3.selectbox("Unmanaged devices", DEVICE_POLICIES,
                         index=DEVICE_POLICIES.index(
                             config.unmanaged_device_policy),
                         key="sp_ctl_unmanaged_device_policy")

            f4, f5, f6 = st.columns(3)
            f4.number_input("Guest access expires after days", min_value=0,
                            max_value=730, value=config.guest_expiry_days,
                            step=10, key="sp_ctl_guest_expiry_days")
            f5.number_input("Anonymous link expires after days", min_value=0,
                            max_value=730,
                            value=config.anonymous_link_expiry_days, step=7,
                            key="sp_ctl_anonymous_link_expiry_days")
            f6.number_input("Idle session timeout in minutes", min_value=0,
                            max_value=1440,
                            value=config.idle_session_timeout_minutes, step=15,
                            key="sp_ctl_idle_session_timeout_minutes")

            for field, label in TOGGLES:
                st.checkbox(label, value=getattr(config, field),
                            key=f"sp_ctl_{field}")

            st.form_submit_button("Apply configuration", type="primary",
                                  on_click=_apply_controls)

        st.markdown(
            f"""
<div class="app-card {band_tone}">
  <h4><span class="app-tag {band_tone}">{esc(score.band)}</span>
  {score.passing} of {score.total} controls passing</h4>
  <p>{esc(score.verdict)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### The checklist")
        st.dataframe(audit_rows(findings), width="stretch", hide_index=True)

        open_items = [f for f in findings if not f.passing]
        if open_items:
            st.markdown("##### Open controls, worst first")
            for finding in open_items:
                tone = TONE_BY_SEVERITY.get(finding.severity, "info")
                st.markdown(
                    f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(finding.severity)}</span>
  {esc(finding.title)}</h4>
  <div class="app-ev">required {esc(finding.requirement)} &nbsp; currently
  {esc(finding.current)}</div>
  <p>{esc(finding.why)}</p>
</div>
""",
                    unsafe_allow_html=True,
                )
            st.markdown("##### Remediation script")
            st.code(remediation_script(findings), language="powershell")
        else:
            st.success(
                "Every control passes. The remediation script is empty because "
                "there is nothing left to change."
            )

    st.markdown(
        f"""
<div class="app-foot">
Secure SharePoint Architecture Console, engine version {ENGINE_VERSION}. A
simulator: no tenant is read, no Graph call is made and nothing you enter leaves
this session. The sign in decision, the access matrix and the audit all read one
configuration object, because a console whose three answers disagree certifies a
posture the tenant does not have.
</div>
""",
        unsafe_allow_html=True,
    )
