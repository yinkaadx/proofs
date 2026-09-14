"""EHR Cloud Security and WIF Architecture Console.

Rendered inside the hub app. Every judgement on this page is made in core.py,
which imports no Streamlit and takes its clock as an argument, so the same
engine sits behind a real broker, a real test suite and this console. The page
holds session state and renders what the engine returned. It decides nothing,
and it duplicates no finding as a literal string, because a page that restates
a conclusion the engine did not reach is a page that can drift away from the
system it claims to describe.

Every credential shown here was redacted inside core.py before it was returned,
so nothing on this page can print a token in full even if a caller forgets.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.cloud_security_wif_console.core import (
    ACCESS_BOUNDARY_SIZE_BUDGET,
    CAB_SUPPORTED_SERVICES,
    DEFAULT_TOKEN_LIFETIME_SECONDS,
    ENGINE_VERSION,
    FAULT_LABEL,
    FAULT_NONE,
    FAULTS,
    MAX_ACCESS_BOUNDARY_RULES,
    MAX_TOKEN_LIFETIME_SECONDS,
    ROLE_PERMISSIONS,
    ROLE_WORKLOAD_IDENTITY_USER,
    SAMPLE_ATTEMPTS,
    SAMPLE_BUCKET,
    SAMPLE_DECISIONS,
    SAMPLE_KEY_RISKS,
    SAMPLE_NOW,
    SAMPLE_PROVIDER,
    SAMPLE_SERVICE_ACCOUNT,
    SAMPLE_TENANTS,
    SEVERITY_ORDER,
    SEVERITY_TONE,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    STS_TOKEN_URL,
    VERDICT_FEDERATION,
    VERDICT_IDENTITY,
    VERDICT_LABEL,
    VERDICT_TONE,
    VERDICT_VAULT,
    AccessAttempt,
    boundary_summary,
    console_kpis,
    decision_matrix_rows,
    decision_summary,
    decisions_by_verdict,
    evaluate_attempt,
    evaluate_attempts,
    human_duration,
    key_file_locations,
    key_posture,
    risks_by_severity,
    run_scenario,
    scenario_inputs,
    sorted_risks,
    sts_downscope_request,
    tenant_boundary,
    validate_boundary,
    validate_provider,
)

STATE = "cswif_state"

# The four flow statuses map straight onto the shared app-stage modifiers, so a
# step that fails looks the same here as a failed hop looks anywhere else in
# the hub.
STATUS_WORD = {
    STATUS_PASS: "PASS",
    STATUS_FAIL: "FAIL",
    STATUS_SKIP: "SKIP",
    STATUS_WARN: "WARN",
}

FINDING_TONE = {
    STATUS_PASS: "ok",
    STATUS_FAIL: "crit",
    STATUS_WARN: "warn",
    STATUS_SKIP: "info",
}

ALL_VERDICTS = "All rows"
ALL_SEVERITIES = "All severities"


def _num(value) -> str:
    """A counted value on its way into an unsafe_allow_html block.

    esc() collapses any falsy value to the empty string, which is right for
    absent text and wrong for a number: a KPI tile showing zero residual risk
    would render blank, and the tile that matters most would be the one that
    disappeared. Numbers go through str() first so a zero stays a zero, and
    still through esc(), so the rule that nothing reaches an
    unsafe_allow_html block unescaped holds here with no exemption.
    """
    return esc(str(value))


def _plural(count: int, singular: str, plural: str = "") -> str:
    """Agree a counted noun with its number.

    The counts on this page come from the engine and change the moment a
    sample attempt is added, so a hardcoded singular is a sentence that goes
    wrong on its own. The hub already does this in streamlit_app.py.
    """
    return singular if count == 1 else (plural or f"{singular}s")


def _stopped_at(run) -> str:
    """Name the step that actually stopped the flow.

    FederationRun.failed_step carries the step key, and the steps carry their
    own index and title, so the sentence is built from what the engine decided.
    Naming the injected fault here instead read as "Stopped at step Expired
    assertion", which is both ungrammatical and a different fact: the fault is
    what was broken, the step is where the break was found.
    """
    for step in run.steps:
        if step.key == run.failed_step:
            return f"step {step.index}, {step.title}"
    return "a step the engine did not name"


def _state() -> dict:
    """Counters only. Every result on the page is recomputed from core.py.

    Nothing derived is cached, because a cached verdict is a verdict that can
    disagree with the engine that produced it.
    """
    if STATE not in st.session_state:
        st.session_state[STATE] = {"scenarios_run": 0, "last_fault": FAULT_NONE}
    return st.session_state[STATE]


def _tenant_label(tenant: str, name: str) -> str:
    """Turn tenant-a into the label a reviewer reads, without a second list."""
    suffix = tenant.split("-", 1)[-1].upper()
    return f"Tenant {suffix}, {name}"


def _request_block(call, label: str) -> None:
    """Show a built request in full, JSON first.

    The form encoded leg is printed underneath as well, because the interesting
    part of an STS call is which fields are present and that the whole access
    boundary travels inside one of them.
    """
    if call is None:
        return
    st.markdown(f"**{label}**")
    if call.purpose:
        st.caption(call.purpose)
    st.code(call.as_json(), language="json")
    if call.encoding == "form":
        st.caption("The urlencoded body the endpoint actually receives:")
        st.code(call.as_form(), language="text")
    if call.note:
        st.caption(call.note)


def _step_rows(steps) -> str:
    """One app-stage row per hop, carrying its status and its named reason."""
    parts = []
    for step in steps:
        word = STATUS_WORD.get(step.status, str(step.status).upper())
        detail = step.detail
        if step.reason:
            detail = f"{detail} {step.reason}"
        parts.append(
            f'<div class="app-stage">'
            f'<span class="s {esc(step.status)}">{esc(word)}</span>'
            f'<span class="n">{_num(step.index)}. {esc(step.title)}</span>'
            f'<span class="d">{esc(detail)}</span>'
            f'</div>'
        )
    return "".join(parts)


def _finding_cards(findings) -> str:
    """Configuration findings as cards, tone taken from the status."""
    parts = []
    for finding in findings:
        tone = FINDING_TONE.get(finding.status, "info")
        parts.append(
            f'<div class="app-card {tone}">'
            f'<h4><span class="app-tag {tone}">{esc(STATUS_WORD.get(finding.status, finding.status))}'
            f'</span>{esc(finding.title)}</h4>'
            f'<p>{esc(finding.detail)}</p></div>'
        )
    return "".join(parts)


def render() -> None:
    inject()
    state = _state()

    st.markdown(
        """
<div class="app-hero">
  <h1>EHR Cloud Security and WIF Architecture Console</h1>
  <p>An Azure managed identity reaches a GCP storage bucket with no key on
  disk. The Entra token is exchanged at Google's Security Token Service, the
  federated token impersonates one service account, and the credential that
  finally touches the web tier expires inside the hour. The same engine then
  downscopes that token to a single tenant prefix and proves it cannot cross
  into another tenant's records, states where a vault is still mandatory and
  where it is no longer needed, and takes apart the static service account key
  on the IIS host one attack vector at a time.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Federation Simulator**: inject a fault and watch that step "
            "fail while the steps behind it are never reached.\n"
            "2. **Tenant Boundary**: switch tenants and read the 200 and the "
            "403 for the same object paths.\n"
            "3. **Decision Matrix**: see which stored secrets the platform "
            "removes and which ones a vault still has to hold.\n"
            "4. **Static Key Inspector**: the four ways the JSON key on the "
            "IIS host is taken, and what replaces each one."
        )
        st.divider()
        st.caption(
            "No live endpoint is called. Every Azure and Google request is "
            "built in full and displayed rather than sent, and every token "
            "value is redacted inside the engine before it reaches the page."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_exec, tab_wif, tab_cab, tab_matrix, tab_key = st.tabs(
        ["Executive Summary", "Federation Simulator", "Tenant Boundary",
         "Decision Matrix", "Static Key Inspector"]
    )

    # The clean run is the reference the page compares against. It is rebuilt
    # every pass rather than cached, because the engine is deterministic and a
    # stale reference would be worse than a recomputed one.
    reference = run_scenario(FAULT_NONE, SAMPLE_NOW)

    # -----------------------------------------------------------------
    # A. Workload Identity Federation simulator
    # -----------------------------------------------------------------
    with tab_wif:
        st.markdown("#### Azure managed identity to a GCS token, hop by hop")
        st.caption(
            "Seven steps, each one checking a rule a real provider enforces. "
            "The faults below do not force a status: they break the input, and "
            "the same unmodified checks find the break. A simulator that can be "
            "told to report a failure proves nothing."
        )

        fault_labels = [label for _, label, _ in FAULTS]
        fault_keys = [key for key, _, _ in FAULTS]
        fault_notes = {key: note for key, _, note in FAULTS}

        c1, c2 = st.columns([3, 2])
        picked_fault = c1.selectbox(
            "Fault to inject into the flow", fault_labels, index=0,
            help="Each option breaks one input. The checks are unchanged.",
        )
        fault = fault_keys[fault_labels.index(picked_fault)]
        lifetime = c2.number_input(
            "Requested lifetime for the impersonated token, in seconds",
            min_value=60, max_value=43200,
            value=int(DEFAULT_TOKEN_LIFETIME_SECONDS), step=300,
            help="Ask for more than an hour and watch the ceiling apply.",
        )

        if fault != state["last_fault"]:
            state["last_fault"] = fault
            state["scenarios_run"] += 1

        run = run_scenario(fault, SAMPLE_NOW, int(lifetime))

        st.caption(f"Scenario: {fault_notes.get(fault, '')}")

        if run.ok:
            st.success(
                f"All {len(run.steps)} steps passed. The credential that "
                f"reaches the storage client is an impersonated access token "
                f"valid for "
                f"{human_duration(run.gcs_token.lifetime_seconds)}, and no "
                f"private key exists anywhere in the path."
            )
        else:
            st.error(
                f"Stopped at {_stopped_at(run)}. "
                f"{run.failure_reason} Every later step is marked not "
                f"attempted rather than passed, because a check that never ran "
                f"is not a check that succeeded."
            )

        st.markdown("##### The flow")
        st.markdown(
            f'<div class="app-card {"ok" if run.ok else "crit"}">'
            f'{_step_rows(run.steps)}</div>',
            unsafe_allow_html=True,
        )

        st.markdown("##### What each step checked")
        for step in run.steps:
            tone = FINDING_TONE.get(step.status, "info")
            st.markdown(
                f'<div class="app-card {tone}">'
                f'<h4><span class="app-tag {tone}">'
                f'{esc(STATUS_WORD.get(step.status, step.status))}</span>'
                f'{_num(step.index)}. {esc(step.title)}</h4>'
                f'<p>{esc(step.detail)}</p>'
                + (f'<p>{esc(step.reason)}</p>' if step.reason else "")
                + "".join(f'<div class="app-ev">{esc(line)}</div>'
                          for line in step.evidence)
                + '</div>',
                unsafe_allow_html=True,
            )

        st.markdown("##### Why the words short lived are checkable")
        st.caption(
            "A claim about lifetime that is not a number next to the rule that "
            "bounds it is a slogan, so each credential is shown beside the "
            "rule that bounds it. Read the third row carefully: "
            "generateAccessToken sets its own expiry, so the impersonated "
            "token is capped by the one hour ceiling rather than by the "
            "federated bearer that requested it, and it can outlive that "
            "bearer by the second the assertion was short. The chain is short "
            "lived at every hop; it is not a chain of descending ceilings."
        )
        st.dataframe(
            [{"Credential": name, "Lifetime": length, "What bounds it": why}
             for name, length, why in run.lifetime_proof()],
            width="stretch", hide_index=True,
        )

        st.markdown("##### The two requests this flow builds")
        st.caption(
            "Taken from the clean run, so the shape of a correct exchange is "
            "always on the page even while a fault is injected. Both bodies "
            "carry a redacted token value, because the engine redacts at the "
            "point of minting rather than at the point of display."
        )
        steps_by_key = {step.key: step for step in reference.steps}
        _request_block(steps_by_key["exchange"].request,
                       f"POST {STS_TOKEN_URL}")
        _request_block(steps_by_key["impersonate"].request,
                       "POST iamcredentials.googleapis.com generateAccessToken")

        st.markdown("##### Provider configuration, checked before anything runs")
        # The provider the run actually used, not the clean fixture. Two of
        # the faults are provider level, and a configuration panel showing a
        # different object from the flow above it is the drift this page is
        # written to avoid.
        _, scenario_provider, _ = scenario_inputs(fault, SAMPLE_NOW)
        st.caption(
            f"Most federation failures are decided when the provider is "
            f"created and only discovered at exchange time. These are the "
            f"checks against the provider this run used: pool "
            f"{scenario_provider.pool_id}, issuer "
            f"{scenario_provider.issuer_uri}, audience "
            f"{scenario_provider.audience}."
        )
        st.markdown(_finding_cards(validate_provider(scenario_provider)),
                    unsafe_allow_html=True)

        st.markdown("##### The binding on the target service account")
        st.caption(
            f"{SAMPLE_SERVICE_ACCOUNT.email} holds "
            f"{ROLE_WORKLOAD_IDENTITY_USER} for the federated principal, on "
            f"the service account resource itself rather than at project "
            f"level. {SAMPLE_SERVICE_ACCOUNT.description}"
        )
        for member in SAMPLE_SERVICE_ACCOUNT.workload_identity_user:
            st.markdown(f'<div class="app-ev">{esc(member)}</div>',
                        unsafe_allow_html=True)

    # -----------------------------------------------------------------
    # B. Multi tenant Credential Access Boundary
    # -----------------------------------------------------------------
    with tab_cab:
        st.markdown("#### One bucket, two tenants, one downscoped token")
        st.caption(
            f"The impersonated token is exchanged a second time at STS, "
            f"carrying a Credential Access Boundary that confines it to one "
            f"tenant's prefix before the request handler ever sees it. "
            f"{CAB_SUPPORTED_SERVICES[0]} is the only service that honours "
            f"one, so this is a confinement mechanism for object access and "
            f"nothing else."
        )

        tenant_options = [_tenant_label(tenant, name)
                          for tenant, name in SAMPLE_TENANTS]
        tenant_ids = [tenant for tenant, _ in SAMPLE_TENANTS]
        picked_tenant = st.radio(
            "Tenant the token is downscoped to", tenant_options,
            index=0, horizontal=True,
        )
        tenant = tenant_ids[tenant_options.index(picked_tenant)]
        st.caption(
            f"{picked_tenant}. The token is downscoped to "
            f"gs://{SAMPLE_BUCKET}/tenants/{tenant}/* and cannot name anything "
            f"outside it, whatever the caller asks for."
        )

        c1, c2 = st.columns([3, 2])
        roles = c1.multiselect(
            "Roles named in availablePermissions",
            sorted(ROLE_PERMISSIONS),
            default=["roles/storage.objectViewer"],
            help="availablePermissions names roles. Only the permissions "
                 "inside those roles become available.",
        )
        include_list = c2.checkbox(
            "Include the objectListPrefix clause for LIST calls", value=True,
            help="A LIST authorizes the bucket, so resource.name carries no "
                 "object key to test.",
        )

        boundary = tenant_boundary(tenant, SAMPLE_BUCKET, tuple(roles),
                                   include_list_clause=include_list)
        decisions = evaluate_attempts(boundary, SAMPLE_ATTEMPTS)
        summary = boundary_summary(decisions, tenant)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{_num(summary["allowed"])}</div>
    <div class="l">Paths returning 200 OK</div></div>
  <div class="app-kpi crit"><div class="n">{_num(summary["denied"])}</div>
    <div class="l">Paths returning 403 Forbidden</div></div>
  <div class="app-kpi warn"><div class="n">{_num(summary["cross_tenant_denied"])}</div>
    <div class="l">Cross tenant reads refused</div></div>
  <div class="app-kpi warn"><div class="n">{_num(summary["prefix_confusion_caught"])}</div>
    <div class="l">Prefix confusion cases caught</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### The boundary sent in the options field")
        st.code(boundary.as_json(), language="json")
        st.caption(
            f"Serialized it is {boundary.serialized_size} of "
            f"{ACCESS_BOUNDARY_SIZE_BUDGET} characters, inside a limit of "
            f"{MAX_ACCESS_BOUNDARY_RULES} rules. The prefix is "
            f"{boundary.prefix} and the trailing delimiter is the whole "
            f"security property."
        )

        _request_block(sts_downscope_request(reference.gcs_token, boundary),
                       "POST sts.googleapis.com v1 token, downscope exchange")

        st.markdown("##### Access attempts against the downscoped token")
        st.caption(
            "The same calls are tested every time, including the sibling "
            "prefix, the other tenant, a path where the prefix appears only "
            "later in the key, and a bucket listing, which is the one call "
            "whose resource is the bucket rather than an object. Switch "
            "tenants above and the verdicts swap."
        )
        for decision in decisions:
            tone = "ok" if decision.allowed else "crit"
            st.markdown(
                f'<div class="app-card {tone}">'
                f'<h4><span class="app-tag {tone}">{esc(decision.status_text)}'
                f'</span>{esc(decision.attempt.label)}</h4>'
                f'<div class="app-ev">{esc(decision.attempt.path)}</div>'
                f'<p>{esc(decision.reason)}</p>'
                f'<p>Permission requested: {esc(decision.attempt.permission)}. '
                f'resource.name: {esc(decision.resource_name)}</p>'
                f'</div>',
                unsafe_allow_html=True,
            )

        st.dataframe(
            [{"Object path": d.attempt.path,
              "Permission": d.attempt.permission,
              "Prefix asked to list": d.attempt.list_prefix or "not a listing",
              "Result": d.status_text,
              "Rule matched": str(d.rule_index) if d.rule_index >= 0 else "none",
              "Prefix matched": d.matched_prefix or "none",
              "A naive prefix would have allowed it":
                  "yes" if d.naive_allowed else "no"}
             for d in decisions],
            width="stretch", hide_index=True,
        )

        confused = [d for d in decisions if d.prefix_confusion]
        if confused:
            st.warning(
                f"{len(confused)} {_plural(len(confused), 'path')} would have "
                f"been served by a boundary "
                f"written without the trailing delimiter. That is the bug this "
                f"feature exists to prevent: one missing character hands "
                f"another tenant's records to the wrong caller with a 200."
            )

        custom = st.text_input(
            "Additional object path to test",
            value=f"gs://{SAMPLE_BUCKET}/tenants/tenant-a-archive/patients/p-0042.json",
            help="Any gs:// path. Try a sibling prefix or a different bucket.",
        )
        # The trim happens here and nowhere deeper. An object key may
        # legitimately begin or end with a space, so the engine compares the
        # exact bytes it is handed; whitespace around something typed into a
        # box is a typing artefact, and removing it is the page's job. The
        # verdict underneath quotes the key it actually evaluated, so the two
        # cannot drift apart without saying so.
        typed = custom.strip()
        if typed:
            extra = evaluate_attempt(
                boundary,
                AccessAttempt(typed, "storage.objects.get",
                              "Path entered above"),
            )
            tone = "ok" if extra.allowed else "crit"
            st.markdown(
                f'<div class="app-card {tone}">'
                f'<h4><span class="app-tag {tone}">{esc(extra.status_text)}'
                f'</span>{esc(extra.attempt.label)}</h4>'
                f'<p>{esc(extra.reason)}</p></div>',
                unsafe_allow_html=True,
            )

        st.markdown("##### The boundary checked against the documented limits")
        st.caption(
            "Client libraries validate almost none of this before the network "
            "call, so a malformed availableResource surfaces either as a 400 "
            "or as a boundary that quietly matches nothing you intended."
        )
        st.markdown(_finding_cards(validate_boundary(boundary)),
                    unsafe_allow_html=True)

    # -----------------------------------------------------------------
    # C. Key Vault versus managed identity
    # -----------------------------------------------------------------
    with tab_matrix:
        st.markdown("#### Where a vault is mandatory, and where it is not")
        st.caption(
            "The honest framing, and the only one that survives an audit: the "
            "platform removes every secret it can mint a token for, and a "
            "vault still holds the residue. Claiming zero stored secrets is "
            "the claim that fails."
        )

        matrix = decision_summary(SAMPLE_DECISIONS)
        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{_num(matrix["secrets_removed"])}</div>
    <div class="l">Stored secrets removed outright</div></div>
  <div class="app-kpi warn"><div class="n">{_num(matrix["secrets_remaining"])}</div>
    <div class="l">Secrets a vault must still hold</div></div>
  <div class="app-kpi ok"><div class="n">{_num(matrix["identity_replaces"])}</div>
    <div class="l">Replaced by a managed identity</div></div>
  <div class="app-kpi ok"><div class="n">{_num(matrix["federation_replaces"])}</div>
    <div class="l">Replaced by federation</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        verdict_options = [ALL_VERDICTS] + [
            VERDICT_LABEL[key]
            for key in (VERDICT_IDENTITY, VERDICT_FEDERATION, VERDICT_VAULT)
        ]
        picked_verdict = st.selectbox(
            "Verdict to filter the matrix by", verdict_options, index=0)
        if picked_verdict == ALL_VERDICTS:
            rows = SAMPLE_DECISIONS
        else:
            key = [k for k, label in VERDICT_LABEL.items()
                   if label == picked_verdict][0]
            rows = decisions_by_verdict(key, SAMPLE_DECISIONS)

        show_reasons = st.checkbox(
            "Show the full reasoning for every matrix row", value=True,
            help="The mechanism and the reason are the argument. The verdict "
                 "on its own is an assertion.",
        )

        st.dataframe(
            [{"Resource": resource, "Platform": platform, "Verdict": verdict,
              "Mechanism": mechanism, "Secret still stored": stored}
             for resource, platform, verdict, mechanism, stored
             in decision_matrix_rows(rows)],
            width="stretch", hide_index=True,
        )

        if show_reasons:
            for row in rows:
                tone = VERDICT_TONE[row.verdict]
                st.markdown(
                    f'<div class="app-card {tone}">'
                    f'<h4><span class="app-tag {tone}">'
                    f'{esc(VERDICT_LABEL[row.verdict])}</span>'
                    f'{esc(row.resource)}</h4>'
                    f'<p>Platform: {esc(row.platform)}. Mechanism: '
                    f'{esc(row.mechanism)}.</p>'
                    f'<p>{esc(row.reason)}</p>'
                    f'<p>Secret before: {"yes" if row.secret_before else "no"}. '
                    f'Secret after: {"yes" if row.secret_after else "no"}.</p>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

        st.info(
            f"{matrix['secrets_removed']} of {matrix['secrets_before']} stored "
            f"secrets are removed by the platform itself, a removal rate of "
            f"{matrix['removal_rate']:.0%}. The "
            f"{matrix['vault_mandatory']} that remain are credentials no "
            f"platform can mint: third party API keys, a partner's pinned "
            f"client certificate, a legacy login with no token endpoint "
            f"behind it."
        )

    # -----------------------------------------------------------------
    # D. IIS static key failure modes
    # -----------------------------------------------------------------
    with tab_key:
        st.markdown("#### The static JSON key on the IIS host")
        posture = key_posture(SAMPLE_KEY_RISKS)
        st.caption(
            "Every vector below is a property of the artefact rather than of "
            "the host, which is the whole argument: the file does not need to "
            "be defended better, it needs to not exist."
        )

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi crit"><div class="n">{_num(posture["critical"])}</div>
    <div class="l">Critical vectors</div></div>
  <div class="app-kpi warn"><div class="n">{_num(posture["high"])}</div>
    <div class="l">High severity vectors</div></div>
  <div class="app-kpi ok"><div class="n">{_num(posture["vectors_closed_by_wif"])}</div>
    <div class="l">Closed by removing the key</div></div>
  <div class="app-kpi ok"><div class="n">{_num(posture["residual_after_wif"])}</div>
    <div class="l">Residual after federation</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        severity_options = [ALL_SEVERITIES] + [
            name.capitalize() for name in SEVERITY_ORDER]
        picked_severity = st.selectbox(
            "Severity to filter the failure modes by", severity_options, index=0)
        if picked_severity == ALL_SEVERITIES:
            risks = sorted_risks(SAMPLE_KEY_RISKS)
        else:
            risks = risks_by_severity(picked_severity.lower(), SAMPLE_KEY_RISKS)

        for risk in risks:
            tone = SEVERITY_TONE.get(risk.severity, "info")
            st.markdown(
                f'<div class="app-card {tone}">'
                f'<h4><span class="app-tag {tone}">{esc(risk.severity)}</span>'
                f'{esc(risk.vector)}</h4>'
                f'<p>How it is exploited: {esc(risk.exploit)}</p>'
                f'<p>Blast radius: {esc(risk.blast_radius)}</p>'
                f'<p>What federation replaces it with: '
                f'{esc(risk.wif_replacement)}</p>'
                f'<p>Control: {esc(risk.control)}</p>'
                f'</div>',
                unsafe_allow_html=True,
            )

        st.markdown("##### Where the key is actually found")
        st.dataframe(
            [{"Location": where, "Why it matters": why}
             for where, why in key_file_locations()],
            width="stretch", hide_index=True,
        )
        st.success(posture["statement"])

    # -----------------------------------------------------------------
    # Executive summary, rendered last so it reads the live controls
    # -----------------------------------------------------------------
    with tab_exec:
        kpis = console_kpis(run, decisions, tenant, SAMPLE_DECISIONS,
                            SAMPLE_KEY_RISKS)
        lifetime_label = (human_duration(kpis.final_lifetime_seconds)
                          if kpis.final_lifetime_seconds else "no token issued")
        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {"ok" if run.ok else "crit"}">
    <div class="n">{_num(kpis.steps_passed)}/{_num(kpis.steps_total)}</div>
    <div class="l">Federation steps passed</div></div>
  <div class="app-kpi {"ok" if kpis.short_lived else "crit"}">
    <div class="n">{esc(lifetime_label)}</div>
    <div class="l">Life of the GCS token</div></div>
  <div class="app-kpi ok"><div class="n">{_num(kpis.cross_tenant_denied)}</div>
    <div class="l">Cross tenant reads refused</div></div>
  <div class="app-kpi warn"><div class="n">{_num(kpis.prefix_confusion_caught)}</div>
    <div class="l">Prefix confusion caught</div></div>
  <div class="app-kpi ok">
    <div class="n">{_num(kpis.secrets_removed)}/{_num(kpis.secrets_total)}</div>
    <div class="l">Stored secrets removed</div></div>
  <div class="app-kpi crit"><div class="n">{_num(kpis.critical_key_risks)}</div>
    <div class="l">Critical static key vectors</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        if run.ok and kpis.short_lived:
            st.success(
                f"The whole chain holds under the current scenario. An Azure "
                f"managed identity reached {SAMPLE_BUCKET} with no key on "
                f"disk, the credential it used expires in "
                f"{lifetime_label}, which is inside the "
                f"{human_duration(MAX_TOKEN_LIFETIME_SECONDS)} ceiling, and "
                f"the downscoped token refused {kpis.cross_tenant_denied} "
                f"cross tenant reads."
            )
        else:
            st.error(
                f"The federation simulator is currently running a fault: "
                f"{FAULT_LABEL.get(run.fault, run.fault)}. {run.failure_reason} "
                f"The boundary and matrix figures below are unaffected, "
                f"because they are evaluated independently."
            )

        st.markdown("##### What this console demonstrates, and how it is measured")
        st.dataframe(
            [
                {"Claim": "An Azure workload reaches GCS with no stored key",
                 "Measured by": f"{reference.passed} of "
                                f"{len(reference.steps)} validated steps in "
                                f"the clean run",
                 "Result": "proved" if reference.ok else "not proved"},
                {"Claim": "The credential is short lived",
                 "Measured by": f"Impersonated token lifetime against the "
                                f"{human_duration(MAX_TOKEN_LIFETIME_SECONDS)} "
                                f"ceiling",
                 "Result": human_duration(
                     reference.gcs_token.lifetime_seconds)
                 if reference.gcs_token else "no token issued"},
                {"Claim": "A tenant token cannot read another tenant's objects",
                 "Measured by": f"{summary['total']} calls evaluated against "
                                f"the boundary for {tenant}",
                 "Result": f"{summary['allowed']} allowed, "
                           f"{summary['denied']} refused"},
                {"Claim": "Prefix confusion is caught rather than assumed away",
                 "Measured by": "The guarded prefix test compared against a "
                                "plain startsWith on the same paths",
                 "Result": f"{summary['prefix_confusion_caught']} "
                           f"{_plural(summary['prefix_confusion_caught'], 'case')} "
                           f"a naive boundary would have served"},
                {"Claim": "Stored secrets are removed where the platform can "
                          "mint a token",
                 "Measured by": f"{matrix['rows']} resources in the decision "
                                f"matrix",
                 "Result": f"{matrix['secrets_removed']} removed, "
                           f"{matrix['secrets_remaining']} still vaulted"},
                {"Claim": "The static key on IIS is the worst artefact in the "
                          "estate",
                 "Measured by": f"{posture['vectors']} attack vectors, "
                                f"{posture['critical']} of them critical",
                 "Result": f"{posture['vectors_closed_by_wif']} closed by "
                           f"removing the file"},
            ],
            width="stretch", hide_index=True,
        )

        st.caption(
            f"Evaluated at {SAMPLE_NOW} against pool "
            f"{SAMPLE_PROVIDER.pool_id}, provider "
            f"{SAMPLE_PROVIDER.provider_id}, service account "
            f"{SAMPLE_SERVICE_ACCOUNT.email}. "
            f"{state['scenarios_run']} fault scenarios exercised in this "
            f"session."
        )

    st.markdown(
        '<div class="app-foot">Nothing on this page calls a live endpoint. '
        'Every Azure and Google request is built in full and displayed rather '
        'than sent, and every token value is redacted inside the engine at the '
        'moment it is minted, so a caller that forgets to redact still cannot '
        'leak one. The failure scenarios break the inputs and let the '
        'unmodified checks find the break, which is the only version of this '
        'simulation that is worth anything.</div>',
        unsafe_allow_html=True,
    )
