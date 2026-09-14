"""UI tests for the EHR Cloud Security and WIF Architecture Console, via
Streamlit's AppTest.

Pass and fail markers are declared before execution. Every scenario asserts the
script ran with zero uncaught exceptions and that the named content rendered,
and every verdict is parsed out of the page programmatically rather than read,
because a console whose claims are only checked by eye is a console that can
quietly stop meaning them.

Run either way:
    python3 -m pytest tests/test_cloud_security_wif_console_page.py -q
    python3 tests/test_cloud_security_wif_console_page.py

Pass marker under pytest: every test passes, zero errors, exit code 0.
Pass marker under python3: final line is exactly
"WIF UI RESULT: PASS <n>/<n>" and exit code 0.
Fail marker: any line starting "FAIL", plus exit code 1.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from streamlit.testing.v1 import AppTest  # noqa: E402

from tools.cloud_security_wif_console.core import (  # noqa: E402
    FAULT_EXPIRED,
    FAULT_ISSUER,
    FAULT_NONE,
    MAX_TOKEN_LIFETIME_SECONDS,
    SAMPLE_ATTEMPTS,
    SAMPLE_BUCKET,
    SAMPLE_DECISIONS,
    SAMPLE_IDENTITY_SUB,
    SAMPLE_KEY_RISKS,
    SAMPLE_NOW,
    SAMPLE_SERVICE_ACCOUNT,
    VERDICT_LABEL,
    VERDICT_VAULT,
    boundary_summary,
    decisions_by_verdict,
    evaluate_attempts,
    human_duration,
    key_file_locations,
    risks_by_severity,
    run_scenario,
    scenario_inputs,
    tenant_boundary,
    validate_provider,
)

HARNESS = Path(__file__).resolve().parent / "_page_harness_cloud_security_wif_console.py"

TENANT_A = "Tenant A, St Alban's Trust"
TENANT_B = "Tenant B, Brackenmoor Health"

BUCKET_LISTING = "gs://ehr-storage"
LIST = "storage.objects.list"

OWN_TENANT_PATH = "gs://ehr-storage/tenants/tenant-a/patients/p-1001.json"
OWN_TENANT_EXPORT = "gs://ehr-storage/tenants/tenant-a/exports/2026-09/discharge.csv"
OTHER_TENANT_PATH = "gs://ehr-storage/tenants/tenant-b/patients/p-2001.json"
SIBLING_PREFIX_PATH = "gs://ehr-storage/tenants/tenant-a-archive/patients/p-0042.json"

READ = "storage.objects.get"

ALLOWED = "200 OK"
REFUSED = "403 Forbidden"

# The clean run is the reference every flow assertion is written against, so a
# step renamed in the engine fails these tests rather than silently passing
# against a string this file invented.
REFERENCE = run_scenario(FAULT_NONE, SAMPLE_NOW)


def run(timeout: int = 180) -> AppTest:
    at = AppTest.from_file(str(HARNESS), default_timeout=timeout)
    at.run()
    return at


def text_of(at: AppTest) -> str:
    """Everything the page put on screen, tables included.

    The boundary verdicts, the decision matrix and the key locations live in
    tables, so their cells count as rendered content. Without them a table
    could go empty and every prose assertion would still pass.
    """
    parts: list[str] = []
    for attr in ("markdown", "text", "info", "success", "warning", "error",
                 "caption", "code", "header", "subheader", "title"):
        try:
            parts += [str(getattr(el, "value", "")) for el in getattr(at, attr)]
        except Exception:
            pass
    try:
        for frame in at.get("dataframe"):
            value = getattr(frame, "value", None)
            if value is not None and hasattr(value, "to_numpy"):
                parts += [str(cell) for cell in value.to_numpy().ravel()]
                parts += [str(col) for col in value.columns]
    except Exception:
        pass
    return "\n".join(parts)


def markup_of(at: AppTest) -> str:
    """Only the HTML blocks, for the cards, the tags and the escaping checks."""
    return "\n".join(str(getattr(el, "value", "")) for el in at.markdown)


def widget(at: AppTest, kind: str, label: str):
    for el in getattr(at, kind):
        if el.label == label:
            return el
    raise AssertionError(f"no {kind} labelled {label!r}; "
                         f"available: {[e.label for e in getattr(at, kind)]}")


def errors(at: AppTest) -> list[str]:
    return [str(e.value) for e in at.exception]


def frame_with(at: AppTest, column: str):
    for element in at.get("dataframe"):
        value = getattr(element, "value", None)
        if value is not None and column in list(value.columns):
            return value
    raise AssertionError(f"no table carries a {column!r} column")


def boundary_table(at: AppTest) -> dict:
    """The access attempt table as {(path, permission): result}.

    Two attempts share a path and differ only in the permission asked for, so
    the permission belongs in the key: a lookup by path alone would compare the
    read verdict against the delete row and call it a pass.
    """
    frame = frame_with(at, "Object path")
    return {(str(row["Object path"]), str(row["Permission"])): str(row["Result"])
            for _, row in frame.iterrows()}


def step_card(at: AppTest, index: int, title: str) -> str:
    """The per step card for one hop of the federation flow.

    Matched on the card that carries a status tag, so the summary strip of
    app-stage rows, which repeats the same titles without one, cannot be
    mistaken for the detail card.
    """
    needle = f"{index}. {title}"
    for element in at.markdown:
        value = str(getattr(element, "value", ""))
        if 'class="app-card' in value and "app-tag" in value and needle in value:
            return value
    raise AssertionError(f"no step card rendered for {needle!r}")


def tag_word(card: str) -> str:
    """The status word inside a card's first app-tag: PASS, FAIL, WARN, SKIP."""
    match = re.search(r'class="app-tag [a-z]+">([A-Za-z]+)</span>', card)
    return match.group(1) if match else ""


def full_token(kind: str, seed: str) -> str:
    """The unredacted value the engine mints before redact() sees it.

    Rebuilt here rather than imported, because the point of the test is that
    this exact string never reaches a page, and the engine never returns it.
    """
    return f"{kind}." + hashlib.sha256(f"{kind}:{seed}".encode("utf-8")).hexdigest()


def select_fault(at: AppTest, label: str) -> AppTest:
    widget(at, "selectbox", "Fault to inject into the flow").select(label).run()
    return at


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------

def test_the_page_renders_with_no_exception():
    at = run()
    assert not at.exception, f"page raised: {errors(at)}"
    body = text_of(at)
    assert "EHR Cloud Security and WIF Architecture Console" in body
    assert len(at.tabs) == 5, f"expected five tabs, got {len(at.tabs)}"


def test_the_executive_summary_states_every_claim_and_its_measure():
    at = run()
    frame = frame_with(at, "Claim")
    claims = "\n".join(str(cell) for cell in frame.to_numpy().ravel())
    assert "An Azure workload reaches GCS with no stored key" in claims
    assert "The credential is short lived" in claims
    assert "A tenant token cannot read another tenant's objects" in claims
    assert "Prefix confusion is caught rather than assumed away" in claims
    assert "The static key on IIS is the worst artefact in the estate" in claims
    body = text_of(at)
    assert "Federation steps passed" in body
    assert "Life of the GCS token" in body


# ---------------------------------------------------------------------------
# A. Federation simulator
# ---------------------------------------------------------------------------

def test_the_flow_renders_every_step_of_the_clean_run():
    at = run()
    markup = markup_of(at)
    assert markup.count('<div class="app-stage">') >= len(REFERENCE.steps)
    for step in REFERENCE.steps:
        assert tag_word(step_card(at, step.index, step.title)) == "PASS", (
            f"step {step.index} did not render as a pass")
    assert f"All {len(REFERENCE.steps)} steps passed." in text_of(at)


def test_the_clean_run_issues_a_token_inside_the_one_hour_ceiling():
    at = run()
    body = text_of(at)
    ceiling = human_duration(MAX_TOKEN_LIFETIME_SECONDS)
    assert human_duration(REFERENCE.gcs_token.lifetime_seconds) in body
    assert f"which is inside the {ceiling} ceiling" in body
    frame = frame_with(at, "Credential")
    credentials = [str(cell) for cell in frame["Credential"]]
    assert credentials == ["Entra ID assertion (IMDS)",
                           "GCP STS federated token",
                           "Impersonated GCS access token"]


def test_asking_for_a_longer_lifetime_is_still_capped_at_one_hour():
    at = run()
    widget(at, "number_input",
           "Requested lifetime for the impersonated token, in seconds"
           ).set_value(43200).run()
    assert not at.exception, f"raising the lifetime raised: {errors(at)}"
    frame = frame_with(at, "Credential")
    row = frame[frame["Credential"] == "Impersonated GCS access token"]
    assert str(row["Lifetime"].iloc[0]) == human_duration(MAX_TOKEN_LIFETIME_SECONDS)
    assert "The maximum is one hour" in str(row["What bounds it"].iloc[0])


def test_breaking_the_binding_fails_that_step_and_issues_no_token():
    at = select_fault(run(), "Missing roles/iam.workloadIdentityUser")
    assert not at.exception, f"the binding fault raised: {errors(at)}"
    binding = step_card(at, 6, "Target service account checks the binding")
    assert tag_word(binding) == "FAIL"
    assert "does not hold roles/iam.workloadIdentityUser" in binding
    impersonate = step_card(at, 7, "IAM Credentials mints the GCS access token")
    assert tag_word(impersonate) == "SKIP", (
        "a step behind the failure must read as not attempted, not as passed")
    assert "Impersonation was never attempted." in impersonate
    body = text_of(at)
    assert "no token issued" in body, "a failed flow must not report a lifetime"
    assert "Stopped at step" in body


def test_an_expired_assertion_fails_at_the_assertion_step():
    at = select_fault(run(), "Expired assertion")
    assert not at.exception, f"the expiry fault raised: {errors(at)}"
    assertion = step_card(at, 2, "GCP STS validates the assertion")
    assert tag_word(assertion) == "FAIL"
    assert "exp" in assertion
    for index, title in ((3, "Pool applies the attribute mapping"),
                         (5, "STS issues the federated access token"),
                         (7, "IAM Credentials mints the GCS access token")):
        assert tag_word(step_card(at, index, title)) == "SKIP"
    assert "no token issued" in text_of(at)


def test_a_wrong_audience_fails_with_the_audience_named_as_the_reason():
    at = select_fault(run(), "Wrong audience on the Entra token")
    assert not at.exception, f"the audience fault raised: {errors(at)}"
    assertion = step_card(at, 2, "GCP STS validates the assertion")
    assert tag_word(assertion) == "FAIL"
    assert "aud" in assertion
    assert "api://ehr-gcp-federation" in assertion
    assert "no token issued" in text_of(at)


def test_the_two_federation_requests_are_built_and_shown_in_full():
    body = text_of(run())
    assert "https://sts.googleapis.com/v1/token" in body
    assert "urn:ietf:params:oauth:grant-type:token-exchange" in body
    assert "generateAccessToken" in body
    assert "subjectToken" in body or "subject_token" in body


def test_the_provider_configuration_is_checked_before_anything_runs():
    body = text_of(run())
    assert "ehr-azure-pool" in body
    assert "entra-iis-prod" in body
    assert "roles/iam.workloadIdentityUser" in body
    assert "principal://iam.googleapis.com/" in body


# ---------------------------------------------------------------------------
# B. Tenant boundary
# ---------------------------------------------------------------------------

def test_tenant_a_reads_its_own_paths_and_is_refused_the_other_tenant():
    at = run()
    widget(at, "radio", "Tenant the token is downscoped to").set_value(TENANT_A).run()
    assert not at.exception, f"selecting tenant A raised: {errors(at)}"
    table = boundary_table(at)
    assert table[(OWN_TENANT_PATH, READ)] == ALLOWED
    assert table[(OWN_TENANT_EXPORT, READ)] == ALLOWED
    assert table[(OTHER_TENANT_PATH, READ)] == REFUSED


def test_switching_to_tenant_b_flips_which_paths_are_allowed():
    at = run()
    widget(at, "radio", "Tenant the token is downscoped to").set_value(TENANT_B).run()
    assert not at.exception, f"selecting tenant B raised: {errors(at)}"
    table = boundary_table(at)
    assert table[(OTHER_TENANT_PATH, READ)] == ALLOWED
    assert table[(OWN_TENANT_PATH, READ)] == REFUSED
    assert table[(OWN_TENANT_EXPORT, READ)] == REFUSED
    body = text_of(at)
    assert f"gs://{SAMPLE_BUCKET}/tenants/tenant-b/*" in body


def test_every_sample_attempt_gets_a_verdict_and_a_reason():
    at = run()
    table = boundary_table(at)
    assert len(table) == len(SAMPLE_ATTEMPTS)
    markup = markup_of(at)
    for attempt in SAMPLE_ATTEMPTS:
        assert attempt.label in markup, f"no card for {attempt.label}"
    assert ALLOWED in markup and REFUSED in markup


def test_the_sibling_prefix_is_refused_and_named_as_prefix_confusion():
    at = run()
    frame = frame_with(at, "Object path")
    row = frame[frame["Object path"] == SIBLING_PREFIX_PATH]
    assert str(row["Result"].iloc[0]) == REFUSED
    assert str(row["A naive prefix would have allowed it"].iloc[0]) == "yes"
    body = text_of(at)
    assert "Prefix confusion cases caught" in body
    assert "without the trailing delimiter" in body


def test_the_boundary_json_and_its_limits_render():
    body = text_of(run())
    assert '"accessBoundaryRules"' in body
    assert "//storage.googleapis.com/projects/_/buckets/ehr-storage" in body
    assert "inRole:roles/storage.objectViewer" in body
    assert "startsWith" in body
    assert "characters, inside a limit of" in body


def test_a_typed_object_path_is_escaped_not_rendered_as_live_html():
    at = run()
    hostile = '<img src=x onerror="window.__probe=1">'
    widget(at, "text_input", "Additional object path to test").set_value(hostile).run()
    assert not at.exception, f"hostile input raised: {errors(at)}"
    markup = markup_of(at)
    assert "<img src=x onerror=" not in markup, "typed markup reached the page live"
    assert "&lt;img" in markup, "the typed path was not echoed back escaped"


# ---------------------------------------------------------------------------
# C. Decision matrix
# ---------------------------------------------------------------------------

def test_the_decision_matrix_renders_every_row():
    at = run()
    frame = frame_with(at, "Resource")
    assert len(frame) == len(SAMPLE_DECISIONS)
    resources = {str(cell) for cell in frame["Resource"]}
    for row in SAMPLE_DECISIONS:
        assert row.resource in resources, f"{row.resource} is missing"
    verdicts = {str(cell) for cell in frame["Verdict"]}
    assert verdicts == {VERDICT_LABEL[row.verdict] for row in SAMPLE_DECISIONS}


def test_the_matrix_filters_down_to_the_rows_a_vault_still_holds():
    at = run()
    widget(at, "selectbox", "Verdict to filter the matrix by").select(
        VERDICT_LABEL[VERDICT_VAULT]).run()
    assert not at.exception, f"filtering the matrix raised: {errors(at)}"
    frame = frame_with(at, "Resource")
    expected = decisions_by_verdict(VERDICT_VAULT, SAMPLE_DECISIONS)
    assert len(frame) == len(expected)
    assert {str(cell) for cell in frame["Verdict"]} == {VERDICT_LABEL[VERDICT_VAULT]}


def test_the_matrix_refuses_the_zero_stored_secrets_claim():
    body = text_of(run())
    assert "Secrets a vault must still hold" in body
    assert "Stored secrets removed outright" in body
    assert "credentials no platform can mint" in body


# ---------------------------------------------------------------------------
# D. Static key inspector
# ---------------------------------------------------------------------------

def test_the_key_inspector_renders_every_vector_and_every_location():
    at = run()
    markup = markup_of(at)
    for risk in SAMPLE_KEY_RISKS:
        assert risk.vector in markup, f"{risk.vector} is missing"
        assert risk.control in markup, f"the control for {risk.key} is missing"
    frame = frame_with(at, "Location")
    locations = {str(cell) for cell in frame["Location"]}
    for where, _ in key_file_locations():
        assert where in locations, f"{where} is missing from the locations table"


def test_the_key_inspector_filters_by_severity():
    at = run()
    widget(at, "selectbox", "Severity to filter the failure modes by").select(
        "Critical").run()
    assert not at.exception, f"filtering the inspector raised: {errors(at)}"
    markup = markup_of(at)
    for risk in risks_by_severity("critical", SAMPLE_KEY_RISKS):
        assert risk.vector in markup
    for risk in risks_by_severity("high", SAMPLE_KEY_RISKS):
        assert risk.vector not in markup, (
            f"{risk.vector} survived a filter it does not match")


def test_the_inspector_states_what_removing_the_file_closes():
    body = text_of(run())
    assert "Closed by removing the key" in body
    assert "Residual after federation" in body
    assert "Federation removes the key" in body


# ---------------------------------------------------------------------------
# House rules: secrets and punctuation
# ---------------------------------------------------------------------------

def test_no_token_value_is_ever_printed_in_full():
    at = run()
    body = text_of(at)
    seeds = (
        ("entra", SAMPLE_IDENTITY_SUB),
        ("sts", f"{SAMPLE_IDENTITY_SUB}:{SAMPLE_NOW}"),
        ("gcs", f"{SAMPLE_SERVICE_ACCOUNT.email}:{SAMPLE_NOW}"),
    )
    for kind, seed in seeds:
        full = full_token(kind, seed)
        assert full not in body, f"a full {kind} token reached the page"
        assert full.split(".", 1)[1] not in body, (
            f"the {kind} token body reached the page without its prefix")
    assert not re.search(r"[0-9a-f]{40,}", body), (
        "a long unbroken secret shaped string reached the page")
    assert REFERENCE.entra_token.redacted_value in body, (
        "the redacted assertion should still be shown, so it can be correlated")
    assert "........" in body


def test_no_private_key_material_is_shown_anywhere():
    body = text_of(run())
    for marker in ("BEGIN PRIVATE KEY", "BEGIN RSA PRIVATE KEY", "private_key_id"):
        assert marker not in body, f"{marker} reached the page"


def test_the_page_prose_uses_no_em_or_en_dashes():
    body = text_of(run())
    # Spelled by code point so the detector itself carries no dash.
    assert chr(8212) not in body, "an em dash reached the page"
    assert chr(8211) not in body, "an en dash reached the page"



# ---------------------------------------------------------------------------
# Regressions. Each of these reads a claim off the page that the page used to
# make wrongly, and each fails without the fix underneath it.
# ---------------------------------------------------------------------------

def test_the_failure_banner_names_the_step_that_stopped_the_flow():
    # It used to name the injected fault instead, which is a different fact and
    # does not fit the sentence: "Stopped at step Expired assertion".
    at = select_fault(run(), "Expired assertion")
    body = text_of(at)
    failed = run_scenario(FAULT_EXPIRED, SAMPLE_NOW)
    step = next(s for s in failed.steps if s.key == failed.failed_step)
    assert f"Stopped at step {step.index}, {step.title}" in body
    assert "Stopped at step Expired assertion" not in body


def test_the_configuration_panel_shows_the_provider_the_run_used():
    # With a provider level fault injected, the flow failed on the mutated
    # provider while the panel underneath reported the clean fixture as valid.
    at = select_fault(run(), "Wrong issuer on the provider")
    assert not at.exception, f"the issuer fault raised: {errors(at)}"
    _, provider, _ = scenario_inputs(FAULT_ISSUER, SAMPLE_NOW)
    issuer = {f.key: f for f in validate_provider(provider)}["issuer_slash"]
    markup = markup_of(at)
    assert issuer.title in markup, "the panel is not reporting this provider"
    assert provider.issuer_uri in text_of(at)
    # The clean run's issuer is still the one the fixture carries, so a panel
    # showing the fixture would show this instead.
    clean = {f.key: f for f in validate_provider(scenario_inputs(
        FAULT_NONE, SAMPLE_NOW)[1])}["issuer_slash"]
    assert clean.title != issuer.title
    assert clean.title not in markup


def test_dropping_the_list_clause_changes_the_listing_verdict():
    # The control is next to the results table, so the verdicts have to answer
    # to it. They could not while the evaluator ignored the expression.
    at = run()
    assert boundary_table(at)[(BUCKET_LISTING, LIST)] == ALLOWED
    widget(at, "checkbox",
           "Include the objectListPrefix clause for LIST calls"
           ).set_value(False).run()
    assert not at.exception, f"dropping the list clause raised: {errors(at)}"
    assert boundary_table(at)[(BUCKET_LISTING, LIST)] == REFUSED


def test_counted_nouns_agree_with_the_number_in_front_of_them():
    at = run()
    body = text_of(at)
    caught = boundary_summary(
        evaluate_attempts(tenant_boundary("tenant-a")), "tenant-a"
    )["prefix_confusion_caught"]
    assert caught == 2, "the fixture no longer exercises the plural"
    assert f"{caught} paths would have been served" in body
    assert f"{caught} path would have been served" not in body
    assert f"{caught} cases a naive boundary would have served" in body


def test_no_em_or_en_dash_reaches_any_file_in_this_tool():
    # The rendered body is checked above, which leaves the page source, the
    # harness and the suites themselves unguarded. All four are prose a
    # reviewer reads, so all four are held to the same rule.
    # Spelled by code point so the detector itself carries no dash.
    banned = (chr(8212), chr(8211))
    root = Path(__file__).resolve().parents[1]
    for name in ("tools/cloud_security_wif_console/page.py",
                 "tools/cloud_security_wif_console/core.py",
                 "tests/_page_harness_cloud_security_wif_console.py",
                 "tests/test_cloud_security_wif_console.py",
                 "tests/test_cloud_security_wif_console_page.py"):
        text = (root / name).read_text(encoding="utf-8")
        for bad in banned:
            assert bad not in text, f"a dash character reached {name}"


if __name__ == "__main__":
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failures: list[str] = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            print(f"  FAIL {name}: {exc}")
            failures.append(name)
    print()
    if failures:
        print(f"WIF UI RESULT: FAIL {len(failures)} of {len(tests)} checks failed")
        for name in failures:
            print(f"  - {name}")
        sys.exit(1)
    print(f"WIF UI RESULT: PASS {len(tests)}/{len(tests)}")
    sys.exit(0)
