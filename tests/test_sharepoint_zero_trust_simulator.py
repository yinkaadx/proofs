"""Engine tests for the Secure SharePoint Architecture Console.

Written for pytest. Deterministic throughout: the engine reads no clock and no
random source, so every run reproduces exactly.

Run: pytest tests/test_sharepoint_zero_trust_simulator.py
"""

from __future__ import annotations

import sys
from dataclasses import fields, replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.sharepoint_zero_trust_simulator.core import (  # noqa: E402
    ACTIONS,
    ADMIN,
    BAND_BLOCKED,
    BAND_EXCEPTIONS,
    BAND_READY,
    BLOCK,
    BLOCKED_COUNTRIES,
    CLIENT_LEGACY,
    CLIENT_MODERN,
    CRITICAL,
    DEFAULT_TENANT,
    DELETE,
    DEVICE_BLOCK,
    DEVICE_FULL,
    DEVICE_LIMITED,
    EMPLOYEE,
    GUEST,
    HARDENED_TENANT,
    HIGH,
    LIBRARIES,
    LINK_ANYONE,
    LINK_SPECIFIC,
    MEDIUM,
    MEMBER,
    NO_ACCESS,
    NOT_APPLIED,
    OWNER,
    READ,
    RISK_HIGH,
    RISK_MEDIUM,
    RISK_NONE,
    ROLE_BY_USER_TYPE,
    ROLES,
    SHARING_ANYONE,
    SHARING_DISABLED,
    SHARING_EXISTING,
    SHARING_NEW_AND_EXISTING,
    TRUSTED_COUNTRIES,
    UNAUTHORIZED,
    USER_TYPES,
    VISITOR,
    WRITE,
    SignInContext,
    TenantConfig,
    apply_hardening,
    audit_findings,
    audit_rows,
    audit_score,
    can,
    describe_permissions,
    evaluate_sign_in,
    library_by_name,
    matrix_rows,
    permissions_for,
    remediation_script,
    role_summary_rows,
)


def decision_for(user_type=EMPLOYEE, config=HARDENED_TENANT, **signals):
    return evaluate_sign_in(SignInContext(user_type=user_type, **signals),
                            config)


def policy(decision, name):
    for result in decision.policies:
        if result.name == name:
            return result
    raise AssertionError(f"no policy named {name!r}; "
                         f"have {[p.name for p in decision.policies]}")


def finding(config, key):
    for item in audit_findings(config):
        if item.key == key:
            return item
    raise AssertionError(f"no finding keyed {key!r}")


# ---------------------------------------------------------------------------
# Sign in authorization
# ---------------------------------------------------------------------------

def test_a_clean_employee_sign_in_is_granted_as_member():
    decision = decision_for(EMPLOYEE)
    assert decision.allowed is True
    assert decision.role == MEMBER
    assert decision.blocking_policy is None


def test_a_clean_admin_sign_in_is_granted_as_owner():
    assert decision_for(ADMIN).role == OWNER


@pytest.mark.parametrize("user_type", USER_TYPES)
def test_every_user_type_reaches_a_decision_with_a_reason_for_each_policy(user_type):
    decision = decision_for(user_type)
    assert decision.policies
    assert all(result.reason for result in decision.policies)
    assert decision.headline and decision.summary


def test_an_unauthorized_user_is_blocked_at_the_directory():
    decision = decision_for(UNAUTHORIZED)
    assert decision.allowed is False
    assert decision.role == NO_ACCESS
    assert policy(decision, "Directory lookup").decision == BLOCK


def test_an_unauthorized_user_is_not_challenged_by_policies_it_never_reaches():
    """A block at the directory happens before conditional access runs, so the
    later policies must not claim to have granted anything."""
    decision = decision_for(UNAUTHORIZED)
    for name in ("Require multi factor authentication",
                 "Named location restriction", "Sign in risk"):
        assert policy(decision, name).decision == NOT_APPLIED


def test_a_blocked_sign_in_carries_no_permission_level_and_no_controls():
    decision = decision_for(UNAUTHORIZED)
    assert decision.role == NO_ACCESS
    assert decision.session_controls == []


def test_missing_mfa_blocks_when_mfa_is_enforced():
    decision = decision_for(EMPLOYEE, mfa_completed=False)
    assert decision.allowed is False
    assert policy(decision, "Require multi factor authentication").decision == BLOCK


def test_missing_mfa_is_allowed_through_when_the_tenant_does_not_enforce_it():
    """The hole this models: the sign in succeeds, so nothing looks wrong."""
    decision = decision_for(EMPLOYEE, config=DEFAULT_TENANT, mfa_completed=False)
    assert decision.allowed is True
    assert "not enforced" in policy(
        decision, "Require multi factor authentication").reason


def test_legacy_authentication_is_blocked_on_a_hardened_tenant():
    decision = decision_for(EMPLOYEE, client_app=CLIENT_LEGACY)
    assert decision.allowed is False
    assert policy(decision, "Block legacy authentication").decision == BLOCK


def test_legacy_authentication_bypasses_mfa_when_it_is_not_blocked():
    """Legacy protocols cannot present a second factor, so leaving them open
    exempts the account from the MFA policy without appearing to."""
    config = replace(HARDENED_TENANT, legacy_auth_blocked=False)
    decision = decision_for(EMPLOYEE, config=config, client_app=CLIENT_LEGACY,
                            mfa_completed=True)
    assert decision.allowed is True
    assert policy(decision, "Block legacy authentication").decision != BLOCK


def test_modern_authentication_leaves_the_legacy_policy_unapplied():
    decision = decision_for(EMPLOYEE, client_app=CLIENT_MODERN)
    assert policy(decision, "Block legacy authentication").decision == NOT_APPLIED


def test_an_admin_on_a_noncompliant_device_is_always_blocked():
    """An owner session from an unmanaged machine is the highest value target in
    the tenant, so it is refused whatever the device policy says."""
    for device_policy in (DEVICE_FULL, DEVICE_LIMITED, DEVICE_BLOCK):
        config = replace(HARDENED_TENANT, unmanaged_device_policy=device_policy)
        decision = decision_for(ADMIN, config=config, device_compliant=False)
        assert decision.allowed is False, device_policy


def test_an_employee_on_a_noncompliant_device_is_downgraded_not_refused():
    decision = decision_for(EMPLOYEE, device_compliant=False)
    assert decision.allowed is True
    assert any("Web only" in control for control in decision.session_controls)


def test_an_employee_on_a_noncompliant_device_is_refused_when_the_policy_blocks():
    config = replace(HARDENED_TENANT, unmanaged_device_policy=DEVICE_BLOCK)
    decision = decision_for(EMPLOYEE, config=config, device_compliant=False)
    assert decision.allowed is False


def test_full_device_access_grants_a_noncompliant_device_a_full_session():
    config = replace(HARDENED_TENANT, unmanaged_device_policy=DEVICE_FULL)
    decision = decision_for(EMPLOYEE, config=config, device_compliant=False)
    assert decision.allowed is True
    assert not any("Web only" in control
                   for control in decision.session_controls)


def test_a_compliant_device_leaves_the_device_policy_unapplied():
    decision = decision_for(EMPLOYEE, device_compliant=True)
    assert policy(decision, "Unmanaged device policy").decision == NOT_APPLIED


@pytest.mark.parametrize("country", BLOCKED_COUNTRIES)
def test_a_sign_in_from_outside_every_named_location_is_blocked(country):
    decision = decision_for(EMPLOYEE, country=country)
    assert decision.allowed is False
    assert policy(decision, "Named location restriction").decision == BLOCK


@pytest.mark.parametrize("country", TRUSTED_COUNTRIES)
def test_a_sign_in_from_a_trusted_location_is_granted(country):
    assert decision_for(EMPLOYEE, country=country).allowed is True


def test_high_risk_is_refused_rather_than_challenged():
    decision = decision_for(EMPLOYEE, risk_level=RISK_HIGH)
    assert decision.allowed is False
    assert policy(decision, "Sign in risk").decision == BLOCK


def test_medium_risk_is_allowed_through_and_flagged_for_review():
    decision = decision_for(EMPLOYEE, risk_level=RISK_MEDIUM)
    assert decision.allowed is True
    assert any("risk report" in control for control in decision.session_controls)


def test_no_risk_adds_no_review_control():
    decision = decision_for(EMPLOYEE, risk_level=RISK_NONE)
    assert not any("risk report" in control
                   for control in decision.session_controls)


def test_a_guest_is_granted_visitor_when_external_sharing_is_on():
    decision = decision_for(GUEST)
    assert decision.allowed is True
    assert decision.role == VISITOR


def test_a_guest_is_blocked_when_external_sharing_is_disabled():
    config = replace(HARDENED_TENANT, external_sharing=SHARING_DISABLED)
    decision = decision_for(GUEST, config=config)
    assert decision.allowed is False
    assert policy(decision, "External sharing").decision == BLOCK


def test_an_employee_is_unaffected_by_external_sharing_being_disabled():
    """Turning off guest access must not lock out the staff."""
    config = replace(HARDENED_TENANT, external_sharing=SHARING_DISABLED)
    assert decision_for(EMPLOYEE, config=config).allowed is True


@pytest.mark.parametrize("sharing", [SHARING_ANYONE, SHARING_NEW_AND_EXISTING,
                                     SHARING_EXISTING])
def test_a_guest_signs_in_on_every_sharing_level_short_of_disabled(sharing):
    config = replace(HARDENED_TENANT, external_sharing=sharing)
    assert decision_for(GUEST, config=config).allowed is True


def test_a_guest_session_carries_a_download_control_and_an_expiry():
    controls = " ".join(decision_for(GUEST).session_controls)
    assert "download blocked" in controls
    assert "30 days" in controls


def test_a_tenant_with_no_idle_timeout_adds_no_timeout_control():
    controls = " ".join(decision_for(EMPLOYEE, config=DEFAULT_TENANT)
                        .session_controls)
    assert "Idle session" not in controls


def test_several_failures_are_all_reported_not_just_the_first():
    decision = decision_for(EMPLOYEE, mfa_completed=False, risk_level=RISK_HIGH,
                            country=BLOCKED_COUNTRIES[0])
    blocked = [p for p in decision.policies if p.decision == BLOCK]
    assert len(blocked) == 3
    assert "3 of" in decision.summary


def test_the_policy_table_holds_one_type_per_column_for_arrow():
    rows = decision_for(GUEST).rows()
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"column {column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Role based access matrix
# ---------------------------------------------------------------------------

def test_every_user_type_maps_to_exactly_one_permission_level():
    assert set(ROLE_BY_USER_TYPE) == set(USER_TYPES)
    assert ROLE_BY_USER_TYPE[UNAUTHORIZED] == NO_ACCESS
    assert set(ROLE_BY_USER_TYPE.values()) - {NO_ACCESS} == set(ROLES)


def test_an_owner_may_read_write_and_delete_everywhere():
    for library in LIBRARIES:
        assert permissions_for(OWNER, library) == tuple(ACTIONS)


def test_a_member_may_write_where_the_work_happens_and_never_delete():
    assert can(MEMBER, "Project Workspaces", WRITE)
    assert not can(MEMBER, "Project Workspaces", DELETE)


def test_a_member_may_only_read_the_policy_library():
    assert can(MEMBER, "Company Policies", READ)
    assert not can(MEMBER, "Company Policies", WRITE)


def test_a_visitor_may_read_and_nothing_else():
    for library in LIBRARIES:
        actions = permissions_for(VISITOR, library)
        assert WRITE not in actions
        assert DELETE not in actions


def test_confidential_libraries_are_closed_to_members_and_visitors():
    for name in ("Finance and Payroll", "HR Records"):
        assert permissions_for(MEMBER, library_by_name(name)) == ()
        assert permissions_for(VISITOR, library_by_name(name)) == ()
        assert permissions_for(OWNER, library_by_name(name)) == tuple(ACTIONS)


def test_no_access_reaches_nothing_at_all():
    for library in LIBRARIES:
        assert permissions_for(NO_ACCESS, library) == ()


def test_a_guest_holding_visitor_still_reaches_nothing_confidential():
    """The boundary is the library, not the person."""
    assert permissions_for(VISITOR, library_by_name("Finance and Payroll"),
                           guest=True) == ()
    assert permissions_for(VISITOR, library_by_name("Company Policies"),
                           guest=True) == (READ,)


def test_disabling_external_sharing_empties_the_guest_column():
    config = replace(HARDENED_TENANT, external_sharing=SHARING_DISABLED)
    for library in LIBRARIES:
        assert permissions_for(VISITOR, library, config, guest=True) == ()
    assert all(row["External guest"] == NO_ACCESS
               for row in matrix_rows(config))


def test_disabling_external_sharing_leaves_the_internal_columns_alone():
    config = replace(HARDENED_TENANT, external_sharing=SHARING_DISABLED)
    hardened_rows = {row["Document library"]: row for row in matrix_rows()}
    for row in matrix_rows(config):
        for column in (OWNER, MEMBER, VISITOR):
            assert row[column] == hardened_rows[row["Document library"]][column]


def test_an_unknown_library_is_refused_rather_than_guessed():
    assert library_by_name("Does not exist") is None
    assert can(OWNER, "Does not exist", READ) is False


def test_empty_permissions_read_as_no_access_not_as_an_empty_cell():
    assert describe_permissions(()) == NO_ACCESS
    assert describe_permissions((READ, WRITE)) == "Read, Write"


def test_the_matrix_has_one_row_per_library_and_one_type_per_column():
    rows = matrix_rows()
    assert len(rows) == len(LIBRARIES)
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"column {column!r} mixes {sorted(kinds)}"


def test_the_role_summary_counts_the_libraries_each_level_reaches():
    rows = {row["Permission level"]: row for row in role_summary_rows()}
    assert rows[OWNER]["Libraries reachable"] == str(len(LIBRARIES))
    assert int(rows[MEMBER]["Libraries reachable"]) < len(LIBRARIES)
    assert int(rows[VISITOR]["Libraries reachable"]) < len(LIBRARIES)


# ---------------------------------------------------------------------------
# Deployment audit
# ---------------------------------------------------------------------------

def test_the_microsoft_default_posture_fails_its_own_audit():
    score = audit_score(audit_findings(DEFAULT_TENANT))
    assert score.band == BAND_BLOCKED
    assert score.passing == 0
    assert score.open_critical >= 1


def test_the_hardened_posture_passes_every_control():
    score = audit_score(audit_findings(HARDENED_TENANT))
    assert score.band == BAND_READY
    assert score.passing == score.total
    assert score.percent == 100


def test_every_config_field_is_covered_by_a_control():
    """A switch nobody audits is a switch that stays wrong."""
    covered = {item.key for item in audit_findings(DEFAULT_TENANT)}
    assert {f.name for f in fields(TenantConfig)} <= covered


def test_open_controls_are_listed_before_passing_ones_worst_first():
    findings = audit_findings(replace(HARDENED_TENANT, mfa_enforced=False,
                                      versioning_enabled=False))
    assert findings[0].key == "mfa_enforced"
    assert findings[0].severity == CRITICAL
    statuses = [item.passing for item in findings]
    assert statuses == sorted(statuses)


def test_anyone_with_the_link_is_the_critical_failure():
    item = finding(replace(HARDENED_TENANT, external_sharing=SHARING_ANYONE),
                   "external_sharing")
    assert item.passing is False
    assert item.severity == CRITICAL


def test_disabling_sharing_entirely_also_passes_the_sharing_control():
    item = finding(replace(HARDENED_TENANT, external_sharing=SHARING_DISABLED),
                   "external_sharing")
    assert item.passing is True


def test_the_default_link_type_must_be_specific_people():
    assert finding(replace(HARDENED_TENANT, default_link_type=LINK_SPECIFIC),
                   "default_link_type").passing is True
    assert finding(replace(HARDENED_TENANT, default_link_type=LINK_ANYONE),
                   "default_link_type").passing is False


@pytest.mark.parametrize("days,expected", [(0, False), (30, True), (60, True),
                                           (61, False), (365, False)])
def test_guest_expiry_passes_only_inside_sixty_days(days, expected):
    assert finding(replace(HARDENED_TENANT, guest_expiry_days=days),
                   "guest_expiry_days").passing is expected


@pytest.mark.parametrize("days,expected", [(0, False), (14, True), (30, True),
                                           (31, False)])
def test_anonymous_link_expiry_passes_only_inside_thirty_days(days, expected):
    assert finding(replace(HARDENED_TENANT, anonymous_link_expiry_days=days),
                   "anonymous_link_expiry_days").passing is expected


@pytest.mark.parametrize("minutes,expected", [(0, False), (60, True),
                                              (61, False)])
def test_idle_timeout_passes_only_inside_an_hour(minutes, expected):
    assert finding(replace(HARDENED_TENANT,
                           idle_session_timeout_minutes=minutes),
                   "idle_session_timeout_minutes").passing is expected


def test_an_open_critical_control_blocks_the_deployment_outright():
    score = audit_score(audit_findings(
        replace(HARDENED_TENANT, legacy_auth_blocked=False)))
    assert score.band == BAND_BLOCKED
    assert score.open_critical == 1


def test_an_open_high_control_alone_is_a_named_exception_not_a_block():
    score = audit_score(audit_findings(
        replace(HARDENED_TENANT, default_link_type=LINK_ANYONE)))
    assert score.band == BAND_EXCEPTIONS
    assert score.open_critical == 0
    assert score.open_high == 1


def test_an_open_medium_control_alone_is_also_an_exception():
    score = audit_score(audit_findings(
        replace(HARDENED_TENANT, versioning_enabled=False)))
    assert score.band == BAND_EXCEPTIONS
    assert score.open_critical == 0
    assert score.open_high == 0


def test_every_control_names_a_severity_a_reason_and_a_remediation():
    for item in audit_findings(DEFAULT_TENANT):
        assert item.severity in (CRITICAL, HIGH, MEDIUM)
        assert item.why
        assert item.remediation
        assert item.requirement
        assert item.current


def test_the_score_reports_the_percentage_it_actually_measured():
    findings = audit_findings(replace(HARDENED_TENANT, mfa_enforced=False))
    score = audit_score(findings)
    assert score.passing == score.total - 1
    assert score.percent == round(score.passing * 100 / score.total)


def test_the_remediation_script_covers_the_open_controls_only():
    script = remediation_script(audit_findings(
        replace(HARDENED_TENANT, mfa_enforced=False)))
    assert "mfa" in script.lower()
    assert "SelfServiceSiteCreationDisabled" not in script


def test_the_remediation_script_is_empty_when_nothing_is_open():
    assert "Nothing to remediate" in remediation_script(
        audit_findings(HARDENED_TENANT))


def test_the_remediation_script_lists_the_open_controls_worst_first():
    script = remediation_script(audit_findings(DEFAULT_TENANT))
    assert script.index(CRITICAL) < script.index(MEDIUM)


def test_the_audit_table_holds_one_type_per_column_for_arrow():
    rows = audit_rows(audit_findings(DEFAULT_TENANT))
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"column {column!r} mixes {sorted(kinds)}"


def test_hardening_one_control_leaves_the_others_untouched():
    hardened = apply_hardening(DEFAULT_TENANT, ["mfa_enforced"])
    assert hardened.mfa_enforced is True
    assert hardened.legacy_auth_blocked is False
    assert hardened.external_sharing == DEFAULT_TENANT.external_sharing


def test_hardening_every_control_reaches_the_target_posture():
    keys = [f.name for f in fields(TenantConfig)]
    assert apply_hardening(DEFAULT_TENANT, keys) == HARDENED_TENANT


def test_hardening_an_unknown_control_changes_nothing():
    assert apply_hardening(DEFAULT_TENANT, ["not_a_setting"]) == DEFAULT_TENANT


# ---------------------------------------------------------------------------
# The three features agree with each other
# ---------------------------------------------------------------------------

def test_one_switch_moves_the_audit_the_matrix_and_the_sign_in_together():
    """The whole point of a single configuration object. Turning external
    sharing off has to show up in all three places at once, or the console
    certifies a posture the tenant does not have."""
    config = replace(HARDENED_TENANT, external_sharing=SHARING_DISABLED)

    assert evaluate_sign_in(SignInContext(user_type=GUEST), config).allowed is False
    assert all(row["External guest"] == NO_ACCESS for row in matrix_rows(config))
    assert finding(config, "external_sharing").passing is True


def test_a_granted_session_reaches_exactly_what_the_matrix_says():
    decision = decision_for(EMPLOYEE)
    rows = {row["Document library"]: row for row in matrix_rows()}
    for library in LIBRARIES:
        expected = describe_permissions(permissions_for(decision.role, library))
        assert rows[library.name][decision.role] == expected


# ---------------------------------------------------------------------------
# Prose discipline
# ---------------------------------------------------------------------------

def test_no_dash_characters_in_any_user_facing_prose():
    prose_parts: list[str] = []
    for config in (DEFAULT_TENANT, HARDENED_TENANT):
        for item in audit_findings(config):
            prose_parts += [item.title, item.requirement, item.current,
                            item.why, item.remediation]
        prose_parts.append(audit_score(audit_findings(config)).verdict)
        for user_type in USER_TYPES:
            decision = evaluate_sign_in(SignInContext(user_type=user_type),
                                        config)
            prose_parts += [decision.headline, decision.summary]
            prose_parts += [p.reason for p in decision.policies]
            prose_parts += decision.session_controls
    for row in role_summary_rows():
        prose_parts += list(row.values())
    prose = "\n".join(prose_parts)
    assert "—" not in prose
    assert "–" not in prose
