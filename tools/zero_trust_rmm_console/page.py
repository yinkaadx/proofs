"""Zero Trust Remote Access Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine can sit behind a real RMM API.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import streamlit as st

from shared.theme import esc, inject
from tools.zero_trust_rmm_console.core import (
    AUDITOR,
    CODE_TTL_MINUTES,
    CODE_VALID,
    ENGINE_VERSION,
    MFA_NOT_ENROLLED,
    MFA_OK,
    MFA_TIMEOUT,
    MFA_WRONG_CODE,
    OUTCOME_LABEL,
    REDEEM_LABEL,
    SAMPLE_MACHINES,
    SAMPLE_USERS,
    TENANTS,
    CodeVault,
    MfaLedger,
    access_matrix,
    attempt_login,
    can_connect_to,
    compliance,
    user_by_username,
    visible_machines,
)

STATE = "ztrmm_state"

OUTCOME_TONE = {
    MFA_OK: "ok",
    MFA_TIMEOUT: "warn",
    MFA_WRONG_CODE: "warn",
    MFA_NOT_ENROLLED: "crit",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state() -> dict:
    if STATE not in st.session_state:
        st.session_state[STATE] = {
            "ledger": MfaLedger(),
            "vault": CodeVault(),
            # A code that grants remote access must never come from a
            # predictable source, so production uses the system CSPRNG. Tests
            # inject a seeded Random instead.
            "rng": secrets.SystemRandom(),
            "last_login": None,
            "last_code": None,
            "last_redeem": None,
        }
    return st.session_state[STATE]


def _login(username: str) -> None:
    state = _state()
    user = user_by_username(username)
    if user is None:
        return
    state["last_login"] = attempt_login(user, state["ledger"], state["rng"], _now())


def _issue_code(username: str, ttl: int) -> None:
    state = _state()
    user = user_by_username(username)
    if user is None:
        return
    try:
        state["last_code"] = state["vault"].issue(user, state["rng"], _now(),
                                                  ttl_minutes=ttl)
        state["last_code_error"] = ""
    except PermissionError as exc:
        state["last_code"] = None
        state["last_code_error"] = str(exc)


def _redeem(code: str) -> None:
    state = _state()
    state["last_redeem"] = state["vault"].redeem(code.strip(), _now())


def render() -> None:
    inject()
    state = _state()
    ledger: MfaLedger = state["ledger"]
    vault: CodeVault = state["vault"]

    st.markdown(
        """
<div class="app-hero">
  <h1>Zero Trust Remote Access Console</h1>
  <p>Two tenants on one self hosted platform. Pick a user and the estate shrinks
  to exactly what their role and sites permit, a login only completes when a
  second factor answers, and an ad hoc support code is six digits that expire.
  Nothing here is implied by trust: every boundary is checked and written down.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Access Matrix**: pick a user and see their machines.\n"
            "2. **MFA Ledger**: attempt a login and watch the second factor decide.\n"
            "3. **Ad hoc Codes**: issue a six digit code and redeem it."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. Access decisions, MFA outcomes "
            "and codes are simulated in this session."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_matrix, tab_mfa, tab_codes = st.tabs(
        ["Access Matrix", "MFA Ledger", "Ad hoc Codes"]
    )

    # -----------------------------------------------------------------
    # Multitenant access matrix
    # -----------------------------------------------------------------
    with tab_matrix:
        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{len(TENANTS)}</div>
    <div class="l">Tenants</div></div>
  <div class="app-kpi"><div class="n">{len(SAMPLE_USERS)}</div>
    <div class="l">Users</div></div>
  <div class="app-kpi"><div class="n">{len(SAMPLE_MACHINES)}</div>
    <div class="l">Unattended machines</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("#### Who can see what")
        st.caption(
            "One row per account. The tenant boundary is absolute, and a "
            "technician is narrowed further to the sites they support."
        )
        st.dataframe(access_matrix(), width="stretch", hide_index=True)

        st.markdown("#### Filter the estate by user")
        username = st.selectbox(
            "Acting user",
            [u.username for u in SAMPLE_USERS],
            format_func=lambda u: f"{u} ({user_by_username(u).role})",
            key="ztrmm_user",
        )
        user = user_by_username(username)
        visible = visible_machines(user)

        st.markdown(
            f"""
<div class="app-card {'ok' if user.can('connect') else 'warn'}">
  <h4><span class="app-tag {'ok' if user.can('connect') else 'warn'}">
  {esc(user.role)}</span>{esc(user.display_name)}</h4>
  <p><strong>Tenant:</strong> {esc(user.tenant)} &middot;
     <strong>Sites:</strong> {esc(', '.join(user.sites) or 'all sites')} &middot;
     <strong>Machines visible:</strong> {len(visible)} of {len(SAMPLE_MACHINES)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        if visible:
            st.dataframe(
                [
                    {"Machine": m.machine_id, "Hostname": m.hostname,
                     "Tenant": m.tenant, "Site": m.site,
                     "Operating system": m.operating_system,
                     "Can connect": "Yes" if can_connect_to(user, m)[0] else "No"}
                    for m in visible
                ],
                width="stretch", hide_index=True,
            )
        else:
            st.warning(
                f"{user.display_name} can see no machines at all. That is the "
                f"correct outcome for a role with no view capability or a "
                f"technician with no sites assigned."
            )

        if user.role == AUDITOR:
            st.info(
                "The auditor role is read only by design. It exists so someone "
                "can review who reached which machine without holding that "
                "access themselves."
            )

        hidden = [m for m in SAMPLE_MACHINES if m not in visible]
        if hidden:
            with st.expander(f"Why {len(hidden)} machine(s) are hidden"):
                for machine in hidden:
                    allowed, reason = can_connect_to(user, machine)
                    st.markdown(f"- **{machine.machine_id}** ({machine.tenant}, "
                                f"{machine.site}): {reason}")

    # -----------------------------------------------------------------
    # MFA enforcement ledger
    # -----------------------------------------------------------------
    with tab_mfa:
        stats = compliance(ledger)
        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{stats['granted']}</div>
    <div class="l">Sessions granted</div></div>
  <div class="app-kpi warn"><div class="n">{stats['denied']}</div>
    <div class="l">Denied at MFA</div></div>
  <div class="app-kpi ok"><div class="n">{stats['enrolment_rate']:.0%}</div>
    <div class="l">MFA enrolment</div></div>
  <div class="app-kpi crit"><div class="n">{stats['granted_without_mfa']}</div>
    <div class="l">Granted without MFA</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("#### Attempt a login")
        st.caption(
            "The password is assumed correct. What decides the outcome is the "
            "second factor, which may be approved, time out, or be answered "
            "with the wrong code."
        )
        login_user = st.selectbox(
            "Account attempting to sign in",
            [u.username for u in SAMPLE_USERS],
            format_func=lambda u: (f"{u} ({user_by_username(u).tenant}"
                                   f"{'' if user_by_username(u).mfa_enrolled else ', no MFA'})"),
            key="ztrmm_login_user",
        )
        st.button("Attempt login", type="primary", on_click=_login,
                  args=(login_user,), key="ztrmm_login_btn")

        attempt = state["last_login"]
        if attempt is not None:
            tone = OUTCOME_TONE.get(attempt.outcome, "info")
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(OUTCOME_LABEL.get(attempt.outcome, attempt.outcome))}</span>
  {esc(attempt.username)} &middot; {esc(attempt.tenant)}</h4>
  <div class="app-ev">{esc(attempt.timestamp)} from {esc(attempt.source_ip)}</div>
  <p>{esc(attempt.detail)}</p>
</div>
""",
                unsafe_allow_html=True,
            )
            if attempt.outcome == MFA_NOT_ENROLLED:
                st.error(
                    "Refused outright rather than downgraded to a password. An "
                    "account without a second factor would otherwise be the way "
                    "into the tenant."
                )

        if stats["unenrolled"]:
            st.warning(
                f"{len(stats['unenrolled'])} account(s) have no second factor "
                f"enrolled and can never sign in: "
                f"{', '.join(stats['unenrolled'])}. Enrol or disable them."
            )

        st.markdown("#### Audit trail")
        if ledger.attempts:
            st.dataframe(ledger.rows(), width="stretch", hide_index=True)
            if stats["granted_without_mfa"] == 0:
                st.success(
                    "Every granted session passed a second factor. That is the "
                    "claim this ledger exists to support."
                )
        else:
            st.info("No login has been attempted yet.")

    # -----------------------------------------------------------------
    # Ad hoc session codes
    # -----------------------------------------------------------------
    with tab_codes:
        st.markdown("#### Issue a code for an attended session")
        st.caption(
            "Six digits, because someone reads it down a phone line. Short "
            "lived and single use, for the same reason."
        )

        c1, c2 = st.columns([2, 1])
        issuer = c1.selectbox(
            "Issuing user",
            [u.username for u in SAMPLE_USERS],
            format_func=lambda u: f"{u} ({user_by_username(u).role})",
            key="ztrmm_issuer",
        )
        ttl = c2.number_input("Valid for, minutes", min_value=1, max_value=120,
                              value=CODE_TTL_MINUTES, step=1, key="ztrmm_ttl")
        st.button("Generate session code", type="primary", on_click=_issue_code,
                  args=(issuer, int(ttl)), key="ztrmm_issue_btn")

        if state.get("last_code_error"):
            st.error(state["last_code_error"])

        issued = state["last_code"]
        if issued is not None:
            st.markdown(
                f"""
<div class="app-card ok">
  <h4><span class="app-tag ok">Active</span>Session code</h4>
  <div class="app-ev">{esc(issued.code)}</div>
  <p>Issued by {esc(issued.issued_by)} for {esc(issued.tenant)} at
  {esc(issued.issued_at)}. Expires {esc(issued.expires_at)}, and it can be used
  once.</p>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("#### Redeem a code")
        r1, r2 = st.columns([2, 1])
        candidate = r1.text_input("Code to redeem", key="ztrmm_redeem_code",
                                  placeholder="123456")
        r2.button("Redeem", on_click=_redeem, args=(candidate,),
                  key="ztrmm_redeem_btn")

        redeemed = state["last_redeem"]
        if redeemed is not None:
            status, record = redeemed
            if status == CODE_VALID:
                st.success(
                    f"{REDEEM_LABEL[status]}. The session is open and the code "
                    f"is now spent, so the same digits cannot be used again."
                )
            else:
                st.error(
                    f"{REDEEM_LABEL[status]}. No session was opened."
                )

        st.markdown("#### Issued codes")
        if vault.codes:
            st.dataframe(vault.rows(_now()), width="stretch", hide_index=True)
        else:
            st.info("No code has been issued yet.")

    st.markdown(
        f"""
<div class="app-foot">
Zero Trust Remote Access Console, engine version {ENGINE_VERSION}. A simulator:
no RMM, directory or identity provider is contacted. In production the code
generator draws from the system CSPRNG, because a predictable session code
hands out remote access.
</div>
""",
        unsafe_allow_html=True,
    )
