"""Ground truth tests for the cloud security and Workload Identity Federation engine.

Pass and fail markers are declared before execution. Every case names the flow
step, failure reason, lifetime in seconds, boundary verdict, matrix verdict or
severity it must produce, and every result is parsed programmatically rather
than read off a screen.

Run either way:
    python3 -m pytest tests/test_cloud_security_wif_console.py -q
    python3 tests/test_cloud_security_wif_console.py

Pass marker under pytest: every test passes, zero errors, exit code 0.
Pass marker under python3: final line is exactly "WIF RESULT: PASS <n>/<n>"
and exit code 0. Fail marker: any line starting "FAIL", plus exit code 1.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.cloud_security_wif_console.core import (  # noqa: E402
    ACCESS_BOUNDARY_SIZE_BUDGET,
    CAB_SUPPORTED_SERVICES,
    DEFAULT_SCOPE_SUFFIX,
    DEFAULT_TOKEN_LIFETIME_SECONDS,
    FAULT_AUDIENCE,
    FAULT_BINDING,
    FAULT_CONDITION,
    FAULT_EXPIRED,
    FAULT_ISSUER,
    FAULT_LABEL,
    FAULT_NONE,
    FAULT_SUBJECT,
    FAULTS,
    GRANT_TYPE_TOKEN_EXCHANGE,
    IMDS_TOKEN_URL,
    LIST_PERMISSION,
    MAX_ACCESS_BOUNDARY_RULES,
    MAX_ALLOWED_AUDIENCES,
    MAX_TOKEN_LIFETIME_SECONDS,
    OBJECT_LIST_PREFIX_ATTRIBUTE,
    RECOMMENDED_ASSERTION_LIFETIME_SECONDS,
    ROLE_PERMISSIONS,
    ROLE_WORKLOAD_IDENTITY_USER,
    SAMPLE_ASSERTION,
    SAMPLE_ATTEMPTS,
    SAMPLE_BUCKET,
    SAMPLE_DECISIONS,
    SAMPLE_IDENTITY_OID,
    SAMPLE_IDENTITY_SUB,
    SAMPLE_KEY_RISKS,
    SAMPLE_NOW,
    SAMPLE_PROVIDER,
    SAMPLE_SERVICE_ACCOUNT,
    SAMPLE_TENANT_GUID,
    SCOPE_GCS_READ,
    SCOPE_IAM,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_ORDER,
    SEVERITY_TONE,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_TEXT_UNDECIDABLE,
    STATUS_WARN,
    STS_NO_AUTH_NOTE,
    STS_TOKEN_URL,
    TOKEN_TYPE_ACCESS_TOKEN,
    TOKEN_TYPE_JWT,
    VERDICT_FEDERATION,
    VERDICT_IDENTITY,
    VERDICT_LABEL,
    VERDICT_TONE,
    VERDICT_VAULT,
    AccessAttempt,
    AccessBoundary,
    AccessBoundaryRule,
    AvailabilityCondition,
    assertion_clock_ok,
    boundary_expression,
    boundary_permits,
    boundary_summary,
    bucket_resource,
    bucket_resource_name,
    cel_quote,
    cel_starts_with,
    console_kpis,
    decision_matrix_rows,
    decision_summary,
    decisions_by_verdict,
    evaluate_attempt,
    evaluate_attempts,
    evaluate_attribute_condition,
    evaluate_attribute_mapping,
    federated_token_lifetime,
    generate_access_token_request,
    guarded_prefix_match,
    imds_request,
    impersonated_token_lifetime,
    key_file_locations,
    key_posture,
    naive_prefix_match,
    object_key_traversal,
    object_resource_name,
    parse_condition,
    parse_gs_uri,
    principal_member,
    redact,
    risks_by_severity,
    rule_permits,
    run_federation,
    run_scenario,
    scenario_inputs,
    shift,
    sorted_risks,
    sts_downscope_request,
    sts_exchange_request,
    tenant_boundary,
    tenant_prefix,
    validate_boundary,
    validate_provider,
    validate_root,
    validate_tenant_id,
)

NOW = SAMPLE_NOW

# The eight sample paths, named rather than indexed, because the adversarial
# ones are the whole point of the evaluator and a bare index hides which is
# which when a case fails.
ATTEMPT_OWN = SAMPLE_ATTEMPTS[0]
ATTEMPT_NESTED = SAMPLE_ATTEMPTS[1]
ATTEMPT_SIBLING = SAMPLE_ATTEMPTS[2]
ATTEMPT_CROSS = SAMPLE_ATTEMPTS[3]
ATTEMPT_MIDDLE = SAMPLE_ATTEMPTS[4]
ATTEMPT_BARE = SAMPLE_ATTEMPTS[5]
ATTEMPT_OTHER_BUCKET = SAMPLE_ATTEMPTS[6]
ATTEMPT_DELETE = SAMPLE_ATTEMPTS[7]

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"


def _failed_steps(run) -> list:
    return [step for step in run.steps if step.status == STATUS_FAIL]


def _step(run, key: str):
    return next(step for step in run.steps if step.key == key)


def _run_text(run) -> str:
    """Everything the console could print from one run, as a single string.

    Used for the redaction checks, because a leak anywhere in the evidence, the
    reasons or the request bodies is a leak.
    """
    parts: list[str] = [run.failure_reason]
    for step in run.steps:
        parts += [step.title, step.detail, step.reason, *step.evidence]
        if step.request is not None:
            parts += [step.request.as_json(), step.request.as_form(),
                      step.request.note, step.request.purpose]
        if step.token is not None:
            parts += [step.token.redacted_value, step.token.ceiling_reason]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Provider configuration, which is where most federation failures are decided
# ---------------------------------------------------------------------------

def test_the_sample_provider_passes_every_configuration_check():
    findings = validate_provider(SAMPLE_PROVIDER)
    assert findings
    assert [f.status for f in findings] == [STATUS_PASS] * len(findings)
    keys = {f.key for f in findings}
    assert {"provider_id", "issuer_scheme", "issuer_slash", "subject_mapping",
            "attribute_keys", "condition_length", "audience"} <= keys


def test_a_v1_issuer_without_its_trailing_slash_is_flagged_not_ignored():
    # IMDS issues v1.0 tokens whose iss carries the slash, so dropping it is
    # the single most common way a working provider stops working.
    broken = replace(SAMPLE_PROVIDER,
                     issuer_uri=f"https://sts.windows.net/{SAMPLE_TENANT_GUID}")
    slash = {f.key: f for f in validate_provider(broken)}["issuer_slash"]
    assert slash.status != STATUS_PASS

    no_subject = replace(SAMPLE_PROVIDER,
                         attribute_mapping={"attribute.tenant_id": "assertion.tid"})
    mapping = {f.key: f for f in validate_provider(no_subject)}["subject_mapping"]
    assert mapping.status == STATUS_FAIL


def test_the_audience_is_the_canonical_resource_name_not_a_guess():
    assert SAMPLE_PROVIDER.audience == f"//iam.googleapis.com/{SAMPLE_PROVIDER.resource_name}"
    assert SAMPLE_PROVIDER.https_audience.startswith("https://iam.googleapis.com/")
    assert SAMPLE_ASSERTION.audience == SAMPLE_PROVIDER.audience
    assert principal_member(SAMPLE_PROVIDER, SAMPLE_IDENTITY_SUB) in \
        SAMPLE_SERVICE_ACCOUNT.workload_identity_user


# ---------------------------------------------------------------------------
# A. The happy path, which has to produce a real credential or prove nothing
# ---------------------------------------------------------------------------

def test_the_clean_run_reaches_a_short_lived_gcs_token():
    run = run_scenario(FAULT_NONE, now=NOW)
    assert run.ok is True
    assert run.failed_step == ""
    assert run.failure_reason == ""
    assert len(run.steps) == 7
    assert run.passed == 7
    assert [step.status for step in run.steps] == [STATUS_PASS] * 7
    assert run.sts_token is not None
    assert run.gcs_token is not None
    assert run.final_token is run.gcs_token
    assert run.gcs_token.scopes == (SCOPE_GCS_READ,)
    assert run.gcs_token.token_type == "Bearer"
    assert run.gcs_token.issued_token_type == TOKEN_TYPE_ACCESS_TOKEN


def test_each_leg_of_the_flow_is_the_request_the_api_actually_takes():
    run = run_scenario(FAULT_NONE, now=NOW)
    imds = _step(run, "imds").request
    assert imds.method == "GET"
    assert imds.url.startswith(IMDS_TOKEN_URL)
    assert imds.headers["Metadata"] == "true"

    exchange = _step(run, "exchange").request
    assert exchange.method == "POST"
    assert exchange.url == STS_TOKEN_URL
    assert exchange.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert exchange.body["grant_type"] == GRANT_TYPE_TOKEN_EXCHANGE
    assert exchange.body["subject_token_type"] == TOKEN_TYPE_JWT
    assert exchange.body["audience"] == SAMPLE_PROVIDER.audience
    assert "Authorization" not in exchange.headers
    assert "Authorization" in STS_NO_AUTH_NOTE

    impersonate = _step(run, "impersonate").request
    assert impersonate.url.startswith(
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/")
    assert impersonate.url.endswith(":generateAccessToken")
    assert impersonate.headers["Content-Type"] == "application/json"
    # The two legs take different shapes: a space delimited scope string on the
    # form, a JSON array here. Copying one body into the other is a silent 400.
    assert impersonate.body["scope"] == [SCOPE_GCS_READ]
    assert impersonate.body["lifetime"].endswith("s")


def test_no_token_value_ever_leaves_the_engine_in_full():
    run = run_scenario(FAULT_NONE, now=NOW)
    blob = _run_text(run)
    for kind, seed in (("entra", SAMPLE_IDENTITY_SUB),
                       ("sts", f"{SAMPLE_ASSERTION.subject}:{NOW}"),
                       ("gcs", f"{SAMPLE_SERVICE_ACCOUNT.email}:{NOW}")):
        digest = hashlib.sha256(f"{kind}:{seed}".encode("utf-8")).hexdigest()
        assert digest not in blob, f"the full {kind} token value reached the output"
    assert "........" in run.gcs_token.redacted_value
    assert redact("abc") == "***"
    assert redact("") == ""


# ---------------------------------------------------------------------------
# Lifetimes, asserted as numbers so "short lived" is checkable
# ---------------------------------------------------------------------------

def test_the_final_token_lifetime_is_a_number_inside_the_one_hour_ceiling():
    run = run_scenario(FAULT_NONE, now=NOW)
    assert run.gcs_token.lifetime_seconds == 3600.0
    assert 0 < run.gcs_token.lifetime_seconds <= MAX_TOKEN_LIFETIME_SECONDS
    # IMDS returns expires_in 3599, and the federated token cannot outlive its
    # input, so the middle leg is one second short of the hour on purpose.
    assert run.sts_token.lifetime_seconds == 3599.0
    assert run.sts_token.lifetime_seconds <= MAX_TOKEN_LIFETIME_SECONDS
    assert MAX_TOKEN_LIFETIME_SECONDS == 3600
    assert DEFAULT_TOKEN_LIFETIME_SECONDS == 3600
    proof = run.lifetime_proof()
    assert len(proof) == 3
    assert all(reason for _, _, reason in proof)


def test_a_longer_lifetime_cannot_be_requested_past_the_ceiling():
    run = run_scenario(FAULT_NONE, now=NOW, lifetime_seconds=7200)
    assert run.gcs_token.lifetime_seconds == float(MAX_TOKEN_LIFETIME_SECONDS)
    lifetime, reason = impersonated_token_lifetime(7200, run.sts_token, NOW)
    assert lifetime == MAX_TOKEN_LIFETIME_SECONDS
    assert "allowServiceAccountCredentialLifetimeExtension" in reason


def test_the_federated_token_is_bounded_by_the_assertion_behind_it():
    # Ninety nine seconds left on the assertion means ninety nine seconds on
    # the exchange. A token that outlived its own input would be a fabrication.
    late = shift(NOW, 3500)
    lifetime, reason = federated_token_lifetime(SAMPLE_ASSERTION, late)
    assert lifetime == 99
    assert "cannot outlive its input" in reason
    run = run_scenario(FAULT_NONE, now=late)
    assert run.sts_token.lifetime_seconds == 99.0


# ---------------------------------------------------------------------------
# A. Each failure reason, provoked separately and stopping the flow
# ---------------------------------------------------------------------------

def test_a_wrong_audience_is_refused_at_the_assertion_check():
    run = run_scenario(FAULT_AUDIENCE, now=NOW)
    assert run.ok is False
    assert run.failed_step == "assertion"
    assert _step(run, "assertion").status == STATUS_FAIL
    assert _step(run, "assertion").index == 2
    assert "allowedAudiences" in run.failure_reason
    assert run.gcs_token is None
    assert run.sts_token is None


def test_a_wrong_issuer_is_compared_as_a_string_and_refused():
    run = run_scenario(FAULT_ISSUER, now=NOW)
    assert run.failed_step == "assertion"
    assert "issuerUri" in run.failure_reason
    assert "login.microsoftonline.com" in run.failure_reason
    assert run.gcs_token is None


def test_an_expired_assertion_is_refused_against_the_supplied_clock():
    run = run_scenario(FAULT_EXPIRED, now=NOW)
    assert run.failed_step == "assertion"
    assert "expired at" in run.failure_reason
    assert run.gcs_token is None
    # The same inputs an hour earlier would still have been inside the window,
    # which is what makes this a clock check rather than a broken fixture.
    assertion, provider, account = scenario_inputs(FAULT_EXPIRED, NOW)
    earlier = run_federation(assertion, provider, account,
                             now=shift(NOW, -5400))
    assert earlier.ok is True


def test_a_subject_the_mapping_cannot_read_stops_at_the_mapping():
    run = run_scenario(FAULT_SUBJECT, now=NOW)
    assert run.failed_step == "mapping"
    assert _step(run, "assertion").status == STATUS_PASS
    assert _step(run, "mapping").status == STATUS_FAIL
    assert _step(run, "mapping").index == 3
    assert "assertion.sub" in run.failure_reason
    assert run.gcs_token is None
    assert run.sts_token is None

    assertion, provider, _ = scenario_inputs(FAULT_SUBJECT, NOW)
    mapped, reason = evaluate_attribute_mapping(assertion, provider)
    assert mapped == {}
    assert reason


def test_an_attribute_condition_refuses_a_genuine_token_from_the_wrong_identity():
    run = run_scenario(FAULT_CONDITION, now=NOW)
    assert run.failed_step == "condition"
    # Everything before the condition passed: the token is real and correctly
    # mapped, and the pool refuses it anyway. That is the whole argument for
    # pinning tid and oid.
    assert _step(run, "assertion").status == STATUS_PASS
    assert _step(run, "mapping").status == STATUS_PASS
    assert _step(run, "condition").status == STATUS_FAIL
    assert _step(run, "condition").index == 4
    assert "assertion.oid" in run.failure_reason
    assert SAMPLE_IDENTITY_OID in run.failure_reason
    assert run.gcs_token is None
    assert run.sts_token is None


def test_a_missing_workload_identity_user_binding_refuses_impersonation():
    run = run_scenario(FAULT_BINDING, now=NOW)
    assert run.failed_step == "binding"
    assert _step(run, "binding").index == 6
    assert ROLE_WORKLOAD_IDENTITY_USER in run.failure_reason
    assert SAMPLE_SERVICE_ACCOUNT.email in run.failure_reason
    # Federation succeeded, so a federated token exists, and no GCS token does.
    # A valid identity is still not an authorized one.
    assert run.sts_token is not None
    assert run.gcs_token is None
    assert _step(run, "impersonate").status == STATUS_SKIP


def test_every_named_fault_stops_the_flow_and_mints_no_gcs_token():
    for key, label, description in FAULTS:
        run = run_scenario(key, now=NOW)
        assert label and description
        assert FAULT_LABEL[key] == label
        if key == FAULT_NONE:
            assert run.ok is True
            assert run.gcs_token is not None
            continue
        assert run.ok is False, f"{key} was expected to fail"
        assert run.gcs_token is None, f"{key} produced a token anyway"
        failed = _failed_steps(run)
        assert len(failed) == 1, f"{key} failed in {len(failed)} places"
        assert failed[0].reason, f"{key} failed without naming a reason"
        assert run.failure_reason
        # Everything after the failure is skipped rather than reported as a
        # pass on inputs it never saw, and each skip says why.
        tail = [s for s in run.steps if s.index > failed[0].index]
        assert all(s.status == STATUS_SKIP for s in tail), f"{key} kept going"
        assert all(s.reason for s in tail)
        assert run.passed == failed[0].index - 1


def test_the_condition_evaluator_fails_closed_on_anything_it_cannot_read():
    mapped = {"google.subject": SAMPLE_IDENTITY_SUB}
    ok, _ = evaluate_attribute_condition(
        SAMPLE_PROVIDER.attribute_condition, SAMPLE_ASSERTION, mapped)
    assert ok is True
    for expression in ("assertion.tid.startsWith('9c4f')",
                       "assertion.groups.exists(g, g == 'admins')",
                       "assertion.missing_claim == 'x'",
                       "attribute.not_mapped == 'x'"):
        allowed, reason = evaluate_attribute_condition(
            expression, SAMPLE_ASSERTION, mapped)
        assert allowed is False, f"{expression!r} was read as an allow"
        assert reason


# ---------------------------------------------------------------------------
# B. Credential Access Boundary shape
# ---------------------------------------------------------------------------

def test_the_boundary_json_has_the_shape_and_the_spelling_sts_requires():
    boundary = tenant_boundary(TENANT_A)
    body = boundary.to_dict()
    assert set(body) == {"accessBoundary"}
    assert set(body["accessBoundary"]) == {"accessBoundaryRules"}
    rules = body["accessBoundary"]["accessBoundaryRules"]
    assert len(rules) == 1
    rule = rules[0]
    # camelCase, singular availableResource, plural availablePermissions. Every
    # one of these is a place where a plausible guess returns a 400.
    assert set(rule) == {"availableResource", "availablePermissions",
                         "availabilityCondition"}
    assert "available_resource" not in rule
    assert "availableResources" not in rule
    assert "availablePermission" not in rule
    assert rule["availableResource"] == \
        f"//storage.googleapis.com/projects/_/buckets/{SAMPLE_BUCKET}"
    assert rule["availableResource"].startswith("//storage.googleapis.com/")
    assert not rule["availableResource"].endswith("/")
    assert rule["availablePermissions"] == ["inRole:roles/storage.objectViewer"]
    assert all(p.startswith("inRole:") for p in rule["availablePermissions"])
    assert set(rule["availabilityCondition"]) == {"expression", "title", "description"}
    assert json.loads(boundary.as_json()) == body


def test_the_expression_drops_the_prefix_that_the_resource_name_carries():
    boundary = tenant_boundary(TENANT_A)
    expression = boundary.rules[0].availability_condition.expression
    # The asymmetry that breaks hand written boundaries: availableResource
    # carries //storage.googleapis.com and resource.name inside the CEL does
    # not.
    assert "resource.name.startsWith('projects/_/buckets/" in expression
    assert "//storage.googleapis.com" not in expression
    assert f"objects/{tenant_prefix(TENANT_A)}'" in expression
    assert OBJECT_LIST_PREFIX_ATTRIBUTE in expression
    assert object_resource_name(SAMPLE_BUCKET, "tenants/tenant-a/x.json") == \
        "projects/_/buckets/ehr-storage/objects/tenants/tenant-a/x.json"
    assert bucket_resource(SAMPLE_BUCKET).startswith("//storage.googleapis.com/")


def test_the_boundary_stays_inside_the_documented_limits():
    boundary = tenant_boundary(TENANT_A)
    findings = {f.key: f for f in validate_boundary(boundary)}
    assert [f.status for f in findings.values()] == [STATUS_PASS] * len(findings)
    assert len(boundary.rules) <= MAX_ACCESS_BOUNDARY_RULES
    assert boundary.serialized_size <= ACCESS_BOUNDARY_SIZE_BUDGET
    assert CAB_SUPPORTED_SERVICES == ("Cloud Storage",)


def test_an_unterminated_prefix_is_reported_as_a_defect_in_the_boundary():
    rule = AccessBoundaryRule(
        available_resource=bucket_resource(SAMPLE_BUCKET),
        available_permissions=("inRole:roles/storage.objectViewer",),
        availability_condition=AvailabilityCondition(
            expression=boundary_expression(SAMPLE_BUCKET, "tenants/tenant-a",
                                           include_list_clause=False)),
    )
    loose = AccessBoundary(rules=(rule,), tenant=TENANT_A,
                           bucket=SAMPLE_BUCKET, prefix="tenants/tenant-a")
    findings = {f.key: f for f in validate_boundary(loose)}
    assert findings["prefix_delimiter"].status == STATUS_FAIL
    # And the evaluator agrees with the token rather than with the intention.
    # The CEL that was actually written authorizes the sibling folder, so the
    # verdict is 200, which is what the real downscoped token returns. An
    # evaluator that quietly added the delimiter would have shown a 403 here
    # and hidden the one bug this module exists to catch.
    served = evaluate_attempt(loose, ATTEMPT_SIBLING)
    assert served.allowed is True
    assert served.matched_prefix == "tenants/tenant-a"
    # The guarded test refuses the same key, because an unterminated prefix is
    # not a guard and it is not repaired into one.
    assert guarded_prefix_match("tenants/tenant-a-archive/p.json",
                                "tenants/tenant-a") is False

    malformed = AccessBoundary(rules=(replace(
        rule, available_resource=f"projects/_/buckets/{SAMPLE_BUCKET}",
        available_permissions=("roles/storage.objectViewer",)),))
    broken = {f.key: f for f in validate_boundary(malformed)}
    assert broken["resource_format"].status == STATUS_FAIL
    assert broken["permission_format"].status == STATUS_FAIL
    assert {f.key: f for f in validate_boundary(AccessBoundary(rules=()))
            }["rule_count"].status == STATUS_FAIL


def test_a_tenant_identifier_that_could_rewrite_the_cel_is_refused():
    ok, reason = validate_tenant_id(TENANT_A)
    assert ok is True and reason
    for hostile in ("x') || true || ('", "tenant a", "Tenant-A", "", "../etc"):
        allowed, why = validate_tenant_id(hostile)
        assert allowed is False, f"{hostile!r} was accepted as a tenant id"
        assert why
    try:
        tenant_boundary("x') || true || ('")
    except ValueError as exc:
        assert "tenant identifier" in str(exc)
    else:
        raise AssertionError("a hostile tenant id built a boundary anyway")
    assert cel_quote("a'b") == "'a\\'b'"


# ---------------------------------------------------------------------------
# B. Downscoping verdicts, including the adversarial paths
# ---------------------------------------------------------------------------

def test_tenant_a_reads_its_own_prefix_and_nothing_else():
    boundary = tenant_boundary(TENANT_A)
    for attempt in (ATTEMPT_OWN, ATTEMPT_NESTED):
        decision = evaluate_attempt(boundary, attempt)
        assert decision.allowed is True, attempt.path
        assert decision.status_code == 200
        assert decision.matched_prefix == tenant_prefix(TENANT_A)
        assert decision.reason

    cross = evaluate_attempt(boundary, ATTEMPT_CROSS)
    assert cross.allowed is False
    assert cross.status_code == 403
    assert cross.status_text == "403 Forbidden"
    assert cross.reason


def test_tenant_b_is_the_mirror_image_of_tenant_a():
    boundary = tenant_boundary(TENANT_B)
    mirrored = evaluate_attempt(boundary, ATTEMPT_CROSS)
    assert mirrored.allowed is True
    assert mirrored.status_code == 200
    for attempt in (ATTEMPT_OWN, ATTEMPT_NESTED):
        decision = evaluate_attempt(boundary, attempt)
        assert decision.allowed is False, attempt.path
        assert decision.status_code == 403
        assert decision.reason
    summary = boundary_summary(evaluate_attempts(boundary), TENANT_B)
    assert summary["allowed"] == 1
    assert summary["denied"] == len(SAMPLE_ATTEMPTS) - 1


def test_the_sibling_prefix_is_denied_and_the_naive_boundary_would_not_have_been():
    boundary = tenant_boundary(TENANT_A)
    decision = evaluate_attempt(boundary, ATTEMPT_SIBLING)
    assert "tenants/tenant-a-archive/" in decision.attempt.path
    assert decision.allowed is False
    assert decision.status_code == 403
    assert decision.reason
    # The one character difference: without the trailing delimiter this is a
    # 200 handing over another tenant's records.
    assert decision.naive_allowed is True
    assert decision.prefix_confusion is True
    assert guarded_prefix_match("tenants/tenant-a-archive/p.json",
                                tenant_prefix(TENANT_A)) is False
    assert naive_prefix_match("tenants/tenant-a-archive/p.json",
                              tenant_prefix(TENANT_A)) is True


def test_a_prefix_buried_in_the_middle_of_a_key_is_denied():
    boundary = tenant_boundary(TENANT_A)
    decision = evaluate_attempt(boundary, ATTEMPT_MIDDLE)
    assert decision.attempt.path.endswith("audit/tenants/tenant-a/access.log")
    assert decision.allowed is False
    assert decision.status_code == 403
    assert "anchored" in decision.reason
    assert decision.naive_allowed is False
    assert guarded_prefix_match("audit/tenants/tenant-a/access.log",
                                tenant_prefix(TENANT_A)) is False


def test_the_bare_prefix_with_no_delimiter_is_not_a_folder():
    boundary = tenant_boundary(TENANT_A)
    decision = evaluate_attempt(boundary, ATTEMPT_BARE)
    assert parse_gs_uri(decision.attempt.path) == (SAMPLE_BUCKET, "tenants/tenant-a")
    assert decision.allowed is False
    assert decision.prefix_confusion is True
    assert decision.reason


def test_a_denial_always_names_the_clause_that_refused_it():
    boundary = tenant_boundary(TENANT_A)
    decisions = evaluate_attempts(boundary)
    for decision in decisions:
        assert decision.reason, decision.attempt.path
        assert decision.status_code in (200, 403)
        assert decision.allowed == (decision.status_code == 200)

    wrong_bucket = evaluate_attempt(boundary, ATTEMPT_OTHER_BUCKET)
    assert wrong_bucket.allowed is False
    assert "No rule in the boundary names bucket ehr-archive" in wrong_bucket.reason
    assert wrong_bucket.rule_index == -1

    delete = evaluate_attempt(boundary, ATTEMPT_DELETE)
    assert delete.allowed is False
    assert "storage.objects.delete" in delete.reason
    assert "roles/storage.objectViewer" in delete.reason
    assert delete.prefix_confusion is False
    permitted, why = boundary_permits(boundary, "storage.objects.get")
    assert permitted is True and why
    assert "storage.objects.delete" not in ROLE_PERMISSIONS["roles/storage.objectViewer"]


def test_the_boundary_summary_counts_what_the_console_claims():
    boundary = tenant_boundary(TENANT_A)
    decisions = evaluate_attempts(boundary)
    summary = boundary_summary(decisions, TENANT_A)
    assert summary["total"] == len(SAMPLE_ATTEMPTS) == 9
    assert summary["allowed"] == 3
    assert summary["denied"] == 6
    assert summary["allowed"] + summary["denied"] == summary["total"]
    assert summary["cross_tenant_denied"] == 2
    assert summary["prefix_confusion_caught"] == 2


def test_the_downscope_request_carries_the_boundary_and_nothing_it_should_not():
    run = run_scenario(FAULT_NONE, now=NOW)
    boundary = tenant_boundary(TENANT_A)
    call = sts_downscope_request(run.gcs_token, boundary)
    assert call.method == "POST"
    assert call.url == STS_TOKEN_URL
    assert call.body["subject_token_type"] == TOKEN_TYPE_ACCESS_TOKEN
    assert call.body["requested_token_type"] == TOKEN_TYPE_ACCESS_TOKEN
    # audience and scope belong to the flow that exchanges an external
    # credential. This one downscopes a Google token that already exists.
    assert "audience" not in call.body
    assert "scope" not in call.body
    assert json.loads(call.body["options"]) == boundary.to_dict()
    assert "options=" in call.as_form()


# ---------------------------------------------------------------------------
# C. Key Vault versus managed identity matrix
# ---------------------------------------------------------------------------

def test_every_row_of_the_matrix_resolves_to_a_labelled_verdict():
    assert len(SAMPLE_DECISIONS) == 9
    for row in SAMPLE_DECISIONS:
        assert row.verdict in VERDICT_LABEL, row.resource
        assert VERDICT_LABEL[row.verdict]
        assert VERDICT_TONE[row.verdict] in ("ok", "warn", "crit", "info")
        assert row.resource and row.platform and row.mechanism and row.reason
        assert row.secret_before is True
    flat = decision_matrix_rows()
    assert len(flat) == len(SAMPLE_DECISIONS)
    assert all(len(cells) == 5 for cells in flat)
    assert {cells[4] for cells in flat} == {"yes", "no"}


def test_the_key_vault_rows_keep_their_secret_and_say_why():
    vault = decisions_by_verdict(VERDICT_VAULT)
    assert len(vault) == 4
    for row in vault:
        assert row.secret_after is True, row.resource
        assert VERDICT_LABEL[row.verdict] == "Key Vault is mandatory"
        assert "Key Vault" in row.mechanism or "Key Vault" in row.reason
    resources = {row.resource for row in vault}
    assert any("pathology" in name.lower() for name in resources)
    assert any("certificate" in name.lower() for name in resources)


def test_the_identity_and_federation_rows_remove_the_stored_secret():
    identity = decisions_by_verdict(VERDICT_IDENTITY)
    federation = decisions_by_verdict(VERDICT_FEDERATION)
    assert len(identity) == 4
    assert len(federation) == 1
    for row in identity + federation:
        assert row.secret_before is True
        assert row.secret_after is False, row.resource
    assert federation[0].platform == "Azure to GCP"
    assert "Workload Identity Federation" in federation[0].mechanism

    summary = decision_summary()
    assert summary["rows"] == len(SAMPLE_DECISIONS)
    assert summary["secrets_removed"] == 5
    assert summary["secrets_remaining"] == 4
    assert summary["secrets_removed"] + summary["secrets_remaining"] == summary["rows"]
    assert summary["vault_mandatory"] == 4
    assert summary["identity_replaces"] == 4
    assert summary["federation_replaces"] == 1
    # The honest number. Claiming zero remaining secrets is the claim that
    # fails an audit.
    assert 0.0 < summary["removal_rate"] < 1.0


def test_a_resource_nobody_modelled_does_not_crash_the_matrix():
    by_resource = {row.resource: row for row in SAMPLE_DECISIONS}
    assert by_resource.get("Fax gateway in the basement") is None
    assert decisions_by_verdict("no_such_verdict") == ()
    empty = decision_summary(())
    assert empty["rows"] == 0
    assert empty["removal_rate"] == 0.0
    extra = replace(SAMPLE_DECISIONS[0], resource="Fax gateway in the basement")
    flat = decision_matrix_rows((extra,))
    assert flat[0][0] == "Fax gateway in the basement"
    assert decision_summary((extra,))["rows"] == 1


# ---------------------------------------------------------------------------
# D. The static key on the IIS host
# ---------------------------------------------------------------------------

def test_every_failure_vector_carries_a_severity_and_a_named_replacement():
    assert len(SAMPLE_KEY_RISKS) == 4
    for risk in SAMPLE_KEY_RISKS:
        assert risk.severity in SEVERITY_ORDER, risk.key
        assert SEVERITY_TONE[risk.severity] in ("crit", "warn", "info")
        assert risk.vector and risk.exploit and risk.blast_radius
        assert risk.wif_replacement, f"{risk.key} names no replacement"
        assert risk.control, f"{risk.key} names no control"
        assert len(risk.wif_replacement) > 40
    keys = {risk.key for risk in SAMPLE_KEY_RISKS}
    assert keys == {"file_exposure", "lifecycle", "non_repudiation", "memory_dump"}


def test_the_vectors_are_ordered_worst_first_and_partition_by_severity():
    ordered = sorted_risks()
    severities = [risk.severity for risk in ordered]
    assert severities == sorted(severities, key=SEVERITY_ORDER.index)
    assert severities[0] == SEVERITY_CRITICAL
    critical = risks_by_severity(SEVERITY_CRITICAL)
    high = risks_by_severity(SEVERITY_HIGH)
    assert len(critical) == 2
    assert len(high) == 2
    assert len(critical) + len(high) == len(SAMPLE_KEY_RISKS)


def test_removing_the_artefact_closes_every_vector_at_once():
    posture = key_posture()
    assert posture["vectors"] == len(SAMPLE_KEY_RISKS)
    assert posture["critical"] == 2
    assert posture["high"] == 2
    assert posture["vectors_closed_by_wif"] == posture["vectors"]
    assert posture["residual_after_wif"] == 0
    assert posture["statement"]
    locations = key_file_locations()
    assert len(locations) == 3
    assert any("GOOGLE_APPLICATION_CREDENTIALS" in name for name, _ in locations)
    assert any("application_default_credentials.json" in name for name, _ in locations)
    assert all(detail for _, detail in locations)


# ---------------------------------------------------------------------------
# Executive KPIs
# ---------------------------------------------------------------------------

def test_the_kpis_describe_the_run_that_actually_happened():
    run = run_scenario(FAULT_NONE, now=NOW)
    decisions = evaluate_attempts(tenant_boundary(TENANT_A))
    kpis = console_kpis(run, decisions, TENANT_A)
    assert kpis.steps_passed == 7
    assert kpis.steps_total == 7
    assert kpis.final_lifetime_seconds == 3600.0
    assert kpis.short_lived is True
    assert kpis.paths_denied == 6
    assert kpis.cross_tenant_denied == 2
    assert kpis.prefix_confusion_caught == 2
    assert kpis.secrets_removed == 5
    assert kpis.secrets_remaining == 4
    assert kpis.secrets_total == len(SAMPLE_DECISIONS)
    assert kpis.critical_key_risks == 2

    failed = console_kpis(run_scenario(FAULT_BINDING, now=NOW), decisions, TENANT_A)
    assert failed.final_lifetime_seconds == 0.0
    assert failed.short_lived is False
    assert failed.steps_passed < failed.steps_total



# ---------------------------------------------------------------------------
# Regressions. Every case below failed before the defect above it was fixed,
# and each one is a claim the console makes out loud.
# ---------------------------------------------------------------------------

def test_the_v2_issuer_is_reported_rather_than_certified_as_the_v1_form():
    # The headline fault scenario. The card used to read "Issuer URI matches
    # the v1.0 form" for a v2.0 issuer, which is the one configuration error
    # this validator exists to catch.
    _, provider, _ = scenario_inputs(FAULT_ISSUER, NOW)
    assert "login.microsoftonline.com" in provider.issuer_uri
    issuer = {f.key: f for f in validate_provider(provider)}["issuer_slash"]
    assert issuer.status == STATUS_FAIL
    assert "v2.0" in issuer.title or "v2.0" in issuer.detail
    assert "sts.windows.net" in issuer.detail
    # An issuer of neither Entra shape is not certified either.
    stranger = replace(SAMPLE_PROVIDER, issuer_uri="https://issuer.example/")
    unknown = {f.key: f for f in validate_provider(stranger)}["issuer_slash"]
    assert unknown.status == STATUS_WARN


def test_a_long_lived_assertion_is_not_reported_as_inside_the_bound():
    # The sentence used to be unconditional, so a twelve hour token was
    # described as inside a six hour maximum in the same breath.
    short = assertion_clock_ok(SAMPLE_ASSERTION, NOW)
    assert short[0] is True
    assert "inside the recommended maximum" in short[1]
    long_lived = replace(SAMPLE_ASSERTION,
                         expires_at=shift(NOW, 12 * 3600))
    accepted, reason = assertion_clock_ok(long_lived, NOW)
    # STS still accepts it: the 6 hour figure is a recommendation, and the
    # engine must not turn a recommendation into a rejection either.
    assert accepted is True
    assert "beyond the recommended maximum" in reason
    assert "inside the recommended maximum" not in reason
    assert RECOMMENDED_ASSERTION_LIFETIME_SECONDS == 6 * 3600


def test_the_audience_fault_is_the_default_suffix_mismatch_it_claims_to_be():
    assertion, provider, _ = scenario_inputs(FAULT_AUDIENCE, NOW)
    # Both halves of the classic failure are modelled, not described: the
    # configuration keeps /.default and the token carries the bare form.
    assert provider.allowed_audiences == (
        f"api://ehr-gcp-federation{DEFAULT_SCOPE_SUFFIX}",)
    assert assertion.audience == "api://ehr-gcp-federation"
    assert not assertion.audience.endswith(DEFAULT_SCOPE_SUFFIX)
    run = run_scenario(FAULT_AUDIENCE, now=NOW)
    assert run.failed_step == "assertion"
    assert DEFAULT_SCOPE_SUFFIX in run.failure_reason
    # And the configuration check names the same defect before the exchange.
    audience = {f.key: f for f in validate_provider(provider)}["audience"]
    assert audience.status == STATUS_FAIL
    assert DEFAULT_SCOPE_SUFFIX in audience.detail


def test_an_allowedaudiences_list_is_checked_against_its_limits():
    clean = replace(SAMPLE_PROVIDER,
                    allowed_audiences=(SAMPLE_PROVIDER.audience,))
    assert {f.key: f for f in validate_provider(clean)}["audience"].status == STATUS_PASS
    too_many = replace(SAMPLE_PROVIDER, allowed_audiences=tuple(
        f"api://audience-{index}" for index in range(MAX_ALLOWED_AUDIENCES + 1)))
    assert {f.key: f for f in validate_provider(too_many)
            }["audience"].status == STATUS_FAIL
    too_long = replace(SAMPLE_PROVIDER, allowed_audiences=("api://" + "a" * 300,))
    assert {f.key: f for f in validate_provider(too_long)
            }["audience"].status == STATUS_FAIL


def test_the_form_body_encodes_a_space_the_way_urlencode_does():
    # google-auth builds this body with urlencode, which is quote_plus. A
    # space delimited scope string is the field where the difference shows.
    call = sts_exchange_request(SAMPLE_ASSERTION, SAMPLE_PROVIDER,
                                f"{SCOPE_GCS_READ} {SCOPE_IAM}")
    lines = dict(line.split("=", 1) for line in call.as_form().splitlines())
    assert "+" in lines["scope"]
    assert "%20" not in lines["scope"]
    assert "%3A" in lines["grant_type"]


def test_a_dot_segment_key_gets_no_verdict_instead_of_a_200():
    # The URL that carries this key is normalised before it is served, so the
    # object fetched is tenant-b's. A byte comparison says 200, and a 200 here
    # would be the console certifying a traversal out of the tenant prefix.
    boundary = tenant_boundary(TENANT_A)
    escapes = (
        "gs://ehr-storage/tenants/tenant-a/../tenant-b/patients/p-2001.json",
        "gs://ehr-storage/tenants/tenant-a/x/../../tenant-b/p.json",
        "gs://ehr-storage/tenants/tenant-a/./../tenant-b/p.json",
        "gs://ehr-storage/tenants/tenant-a//../tenant-b/p.json",
        "gs://ehr-storage/tenants/tenant-a/%2E%2E/tenant-b/p.json",
        "gs://ehr-storage/tenants/tenant-a/%2e%2e/tenant-b/p.json",
    )
    for path in escapes:
        decision = evaluate_attempt(
            boundary, AccessAttempt(path, "storage.objects.get", "traversal"))
        assert decision.allowed is False, path
        assert decision.status_code != 200, path
        assert decision.status_text == STATUS_TEXT_UNDECIDABLE, path
        assert decision.reason, path
    # An ordinary key is unaffected, and says so by returning nothing.
    assert object_key_traversal("tenants/tenant-a/patients/p-1001.json") == ""
    assert evaluate_attempt(boundary, ATTEMPT_OWN).allowed is True
    assert guarded_prefix_match("tenants/tenant-a/../tenant-b/p.json",
                                tenant_prefix(TENANT_A)) is False


def test_a_permission_is_resolved_against_the_rule_that_matched():
    # Rules are unioned as resources, not as permissions. A writer role on the
    # scratch bucket must not upgrade the read only rule on the EHR bucket.
    read_only = tenant_boundary(TENANT_A, SAMPLE_BUCKET,
                                ("roles/storage.objectViewer",))
    writer = tenant_boundary(TENANT_A, "ehr-scratch",
                             ("roles/storage.objectUser",))
    combined = AccessBoundary(rules=read_only.rules + writer.rules,
                              tenant=TENANT_A, bucket=SAMPLE_BUCKET,
                              prefix=tenant_prefix(TENANT_A))
    delete = evaluate_attempt(combined, ATTEMPT_DELETE)
    assert delete.allowed is False
    assert "storage.objects.delete" in delete.reason
    assert rule_permits(read_only.rules[0], "storage.objects.delete")[0] is False
    assert rule_permits(writer.rules[0], "storage.objects.delete")[0] is True
    # The same call against the scratch bucket is allowed, because that is the
    # rule that names it.
    scratch = evaluate_attempt(combined, AccessAttempt(
        "gs://ehr-scratch/tenants/tenant-a/p.json", "storage.objects.delete",
        "Scratch bucket delete", TENANT_A))
    assert scratch.allowed is True
    assert scratch.rule_index == 1


def test_each_rule_is_decided_on_its_own_condition():
    # The documented shape of a boundary is up to ten unioned rules. Evaluating
    # every one of them against a single prefix field both authorized the wrong
    # tenant and refused the right one, and named the wrong rule while doing it.
    combined = AccessBoundary(
        rules=tenant_boundary(TENANT_A).rules + tenant_boundary(TENANT_B).rules,
        tenant=TENANT_B, bucket=SAMPLE_BUCKET, prefix=tenant_prefix(TENANT_B))
    own = evaluate_attempt(combined, ATTEMPT_OWN)
    assert own.allowed is True
    assert own.rule_index == 0
    assert own.matched_prefix == tenant_prefix(TENANT_A)
    other = evaluate_attempt(combined, ATTEMPT_CROSS)
    assert other.allowed is True
    assert other.rule_index == 1
    assert other.matched_prefix == tenant_prefix(TENANT_B)
    # A path in neither tenant is still refused, by every rule in turn.
    assert evaluate_attempt(combined, ATTEMPT_SIBLING).allowed is False


def test_the_verdict_comes_from_the_expression_and_not_from_a_field():
    boundary = tenant_boundary(TENANT_A)
    # The boundary field is display metadata. Widening it must not widen the
    # token, because the options field STS receives carries the CEL, not this.
    lie = replace(boundary, prefix="tenants/")
    assert evaluate_attempt(lie, ATTEMPT_CROSS).allowed is False
    assert json.loads(sts_downscope_request(
        run_scenario(FAULT_NONE, now=NOW).gcs_token, lie).body["options"]
    ) == boundary.to_dict()
    # And emptying it must not close a boundary whose rules are intact, which
    # is the default value on the public constructor.
    blank = replace(boundary, prefix="")
    assert evaluate_attempt(blank, ATTEMPT_OWN).allowed is True
    hand_built = AccessBoundary(rules=boundary.rules)
    assert evaluate_attempt(hand_built, ATTEMPT_OWN).allowed is True
    assert evaluate_attempt(hand_built, ATTEMPT_CROSS).allowed is False


def test_an_empty_prefix_is_a_tautology_and_is_reported_as_one():
    # startsWith('') is true for every string, so this grants the whole multi
    # tenant bucket while looking exactly like a boundary.
    expression = boundary_expression(SAMPLE_BUCKET, "")
    assert expression.endswith(".startsWith('')")
    wide = AccessBoundary(rules=(AccessBoundaryRule(
        available_resource=bucket_resource(SAMPLE_BUCKET),
        available_permissions=("inRole:roles/storage.objectViewer",),
        availability_condition=AvailabilityCondition(expression=expression)),))
    delimiter = {f.key: f for f in validate_boundary(wide)}["prefix_delimiter"]
    assert delimiter.status == STATUS_FAIL
    assert "tautology" in delimiter.title or "tautology" in delimiter.detail
    # And it is not quietly denied either: the CEL really does allow it.
    assert evaluate_attempt(wide, ATTEMPT_CROSS).allowed is True


def test_a_quote_in_the_expression_cannot_hide_an_unterminated_prefix():
    # cel_quote escapes a quote to \', which used to break the regex the check
    # was written as, turning a genuinely leaking boundary into a pass.
    for bucket in (SAMPLE_BUCKET, "ehr'storage"):
        for with_list in (True, False):
            rule = AccessBoundaryRule(
                available_resource=bucket_resource(bucket),
                available_permissions=("inRole:roles/storage.objectViewer",),
                availability_condition=AvailabilityCondition(
                    expression=boundary_expression(
                        bucket, "tenants/tenant-a", with_list)))
            findings = {f.key: f for f in
                        validate_boundary(AccessBoundary(rules=(rule,)))}
            assert findings["prefix_delimiter"].status == STATUS_FAIL, (
                f"{bucket} with list clause {with_list} was passed")
    # The parser reads the escaped literal back as the value CEL compares.
    clauses, reason = parse_condition(
        boundary_expression("ehr'storage", "tenants/tenant-a/"))
    assert reason == ""
    assert clauses[0].prefix == "projects/_/buckets/ehr'storage/objects/tenants/tenant-a/"
    assert clauses[1].prefix == "tenants/tenant-a/"
    # Anything outside the modelled subset is a refusal, never an allow.
    unreadable, why = parse_condition("resource.name.contains('tenant-a')")
    assert unreadable == () and why


def test_the_prefix_root_is_held_to_the_same_standard_as_the_tenant():
    # Both values are concatenated into the same CEL literal, so validating
    # only one of them leaves the defence covering half the string.
    assert validate_root("tenants")[0] is True
    for hostile in ("", "..", "tenants/../", "/tenants", "a' || true || '"):
        ok, why = validate_root(hostile)
        assert ok is False, f"{hostile!r} was accepted as a prefix root"
        assert why
        try:
            tenant_boundary(TENANT_A, root=hostile)
        except ValueError as exc:
            assert str(exc)
        else:
            raise AssertionError(f"{hostile!r} built a boundary anyway")


def test_prefix_confusion_is_counted_only_where_it_happened():
    boundary = tenant_boundary(TENANT_A)
    summary = boundary_summary(evaluate_attempts(boundary), TENANT_A)
    assert summary["prefix_confusion_caught"] == 2
    # A denial on the bucket or on the permission is not the sibling prefix
    # bug. Counting them turned a headline number into fabricated evidence.
    assert evaluate_attempt(boundary, ATTEMPT_OTHER_BUCKET).prefix_confusion is False
    assert evaluate_attempt(boundary, ATTEMPT_DELETE).prefix_confusion is False
    assert naive_prefix_match("anything at all", "") is False
    assert cel_starts_with("anything at all", "") is True
    # The display field cannot manufacture catches either.
    blanked = boundary_summary(
        evaluate_attempts(replace(boundary, prefix="")), TENANT_A)
    assert blanked == summary


def test_a_listing_is_authorized_by_the_list_clause_and_nothing_else():
    # The toggle in the console has to change a verdict, or it is decoration.
    listing = SAMPLE_ATTEMPTS[-1]
    assert listing.permission == LIST_PERMISSION
    assert listing.is_list is True
    with_clause = evaluate_attempt(tenant_boundary(TENANT_A), listing)
    assert with_clause.allowed is True
    assert with_clause.resource_name == bucket_resource_name(SAMPLE_BUCKET)
    assert OBJECT_LIST_PREFIX_ATTRIBUTE in \
        tenant_boundary(TENANT_A).rules[0].availability_condition.expression
    without = evaluate_attempt(
        tenant_boundary(TENANT_A, include_list_clause=False), listing)
    assert without.allowed is False
    assert OBJECT_LIST_PREFIX_ATTRIBUTE in without.reason
    # A caller who omits the prefix is refused, which is the fail closed
    # behaviour the clause is chosen for.
    blind = evaluate_attempt(tenant_boundary(TENANT_A), replace(
        listing, list_prefix=""))
    assert blind.allowed is False
    # And a listing of another tenant's prefix is refused.
    across = evaluate_attempt(tenant_boundary(TENANT_A), replace(
        listing, list_prefix=tenant_prefix(TENANT_B)))
    assert across.allowed is False


def test_a_rule_that_grants_no_permission_is_rejected():
    # Google's Go client refuses this client side: all rules must provide at
    # least one permission. It is reachable from the console, because the roles
    # control can be emptied.
    empty = tenant_boundary(TENANT_A, SAMPLE_BUCKET, roles=())
    permissions = {f.key: f for f in validate_boundary(empty)}["permission_format"]
    assert permissions.status == STATUS_FAIL
    assert empty.rules[0].available_permissions == ()
    # And nothing is authorized by it either.
    assert evaluate_attempt(empty, ATTEMPT_OWN).allowed is False


def test_an_object_key_keeps_the_bytes_it_was_given():
    # A key may legitimately begin or end with a space, so a trimmed key is not
    # the key the storage API would be asked for.
    assert parse_gs_uri("gs://ehr-storage/tenants/tenant-a/ p.json") == (
        SAMPLE_BUCKET, "tenants/tenant-a/ p.json")
    assert parse_gs_uri("  gs://ehr-storage/a") == ("  gs:", "/ehr-storage/a")
    assert parse_gs_uri("") == ("", "")


# ---------------------------------------------------------------------------
# House style
# ---------------------------------------------------------------------------

def test_no_em_or_en_dashes_anywhere_in_the_engine_prose():
    # Spelled by code point so the detector itself carries no dash.
    banned = (chr(8212), chr(8211))
    prose: list[tuple[str, str]] = []
    prose += [(f"fault label {key}", label) for key, label, _ in FAULTS]
    prose += [(f"fault note {key}", note) for key, _, note in FAULTS]
    prose += [(f"verdict {key}", text) for key, text in VERDICT_LABEL.items()]
    prose += [(f"decision {row.resource}", row.mechanism) for row in SAMPLE_DECISIONS]
    prose += [(f"reason {row.resource}", row.reason) for row in SAMPLE_DECISIONS]
    prose += [(f"resource {i}", row.resource)
              for i, row in enumerate(SAMPLE_DECISIONS)]
    for risk in SAMPLE_KEY_RISKS:
        prose += [(f"{risk.key} vector", risk.vector),
                  (f"{risk.key} exploit", risk.exploit),
                  (f"{risk.key} blast radius", risk.blast_radius),
                  (f"{risk.key} replacement", risk.wif_replacement),
                  (f"{risk.key} control", risk.control)]
    prose += [(f"key file {name}", detail) for name, detail in key_file_locations()]
    prose.append(("key posture statement", key_posture()["statement"]))

    for key, _, _ in FAULTS:
        run = run_scenario(key, now=NOW)
        prose.append((f"{key} failure reason", run.failure_reason))
        for step in run.steps:
            prose += [(f"{key} step {step.key} title", step.title),
                      (f"{key} step {step.key} detail", step.detail),
                      (f"{key} step {step.key} reason", step.reason)]
            prose += [(f"{key} step {step.key} evidence {i}", text)
                      for i, text in enumerate(step.evidence)]
            if step.request is not None:
                prose += [(f"{key} step {step.key} purpose", step.request.purpose),
                          (f"{key} step {step.key} note", step.request.note)]
        prose += [(f"{key} lifetime {label}", reason)
                  for label, _, reason in run.lifetime_proof()]

    prose += [(f"provider finding {f.key}", f.title + " " + f.detail)
              for f in validate_provider(SAMPLE_PROVIDER)]
    boundary = tenant_boundary(TENANT_A)
    prose += [(f"boundary finding {f.key}", f.title + " " + f.detail)
              for f in validate_boundary(boundary)]
    prose.append(("boundary description",
                  boundary.rules[0].availability_condition.description))
    prose += [(f"decision {d.attempt.label}", d.reason)
              for d in evaluate_attempts(boundary)]
    prose += [(f"attempt {a.label}", a.label) for a in SAMPLE_ATTEMPTS]

    offenders = [name for name, text in prose if any(b in (text or "") for b in banned)]
    assert not offenders, f"em or en dashes found in: {offenders}"

    source = (Path(__file__).resolve().parents[1]
              / "tools" / "cloud_security_wif_console" / "core.py").read_text(
                  encoding="utf-8")
    assert not any(b in source for b in banned), "a dash is present in the source"


def test_the_engine_never_imports_streamlit():
    source = (Path(__file__).resolve().parents[1]
              / "tools" / "cloud_security_wif_console" / "core.py").read_text(
                  encoding="utf-8")
    assert "import streamlit" not in source
    assert "streamlit" not in source


def test_the_engine_takes_its_clock_as_an_argument():
    # A pure engine cannot read the wall clock, or the same inputs would give
    # different answers to the console and to this suite.
    later = run_scenario(FAULT_NONE, now=shift(NOW, 600))
    assert later.now == shift(NOW, 600)
    assert later.gcs_token.issued_at == shift(NOW, 600)
    assert run_scenario(FAULT_NONE, now=NOW).gcs_token.redacted_value == \
        run_scenario(FAULT_NONE, now=NOW).gcs_token.redacted_value
    assert later.gcs_token.redacted_value != \
        run_scenario(FAULT_NONE, now=NOW).gcs_token.redacted_value
    # And a request is built, never sent.
    assert imds_request(SAMPLE_PROVIDER.audience).method == "GET"
    assert sts_exchange_request(SAMPLE_ASSERTION, SAMPLE_PROVIDER).method == "POST"
    assert generate_access_token_request(
        SAMPLE_SERVICE_ACCOUNT, run_scenario(FAULT_NONE, now=NOW).sts_token
    ).method == "POST"


# ---------------------------------------------------------------------------
# Standalone runner, so the suite reports the same way as the rest of the repo
# ---------------------------------------------------------------------------

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
        print(f"WIF RESULT: FAIL {len(failures)} of {len(tests)} checks failed")
        for name in failures:
            print(f"  - {name}")
        sys.exit(1)
    print(f"WIF RESULT: PASS {len(tests)}/{len(tests)}")
    sys.exit(0)
