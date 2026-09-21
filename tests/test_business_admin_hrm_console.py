"""Tests for the Business Admin and HRM Architecture Console.

The organisation tests measure the generated rows rather than reading the
diagram, because a self referencing manager column cannot be constrained
into being a tree. The access tests enumerate the whole grid, since the
hole in an access matrix is always in the cell nobody wrote a rule for.
The bridge tests forge a token and require it to be refused.
"""

from __future__ import annotations

import base64
import itertools
import json
import pathlib
import subprocess
import sys

import pytest

from tools.business_admin_hrm_console.core import (
    ACTION_READ,
    ACTION_WRITE,
    ACTIONS,
    ALGORITHM,
    ALLOW,
    ALLOW_SCOPED,
    AUDIENCE,
    DENY,
    ENGINE_VERSION,
    FIXED_ISSUED_AT,
    ISSUER,
    LEVEL_EXECUTIVE,
    RESOURCES,
    ROLE_ADMIN,
    ROLE_EMPLOYEE,
    ROLE_PROJECT_MANAGER,
    ROLES,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    UNVERIFIED,
    VERIFIED,
    WP_TO_EMPLOYEE,
    forge_alg_none,
    get_organization_schema,
    issue_token,
    rbac_matrix,
    read_claims_without_verifying,
    simulate_rbac_access,
    simulate_wordpress_jwt_bridge,
    verify_token,
)
from tools.registry import all_tools

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL_DIR = ROOT / "tools" / "business_admin_hrm_console"

SCHEMA = get_organization_schema()


# ---------------------------------------------------------------------------
# Organisation schema
# ---------------------------------------------------------------------------

def test_the_organisation_carries_more_than_three_hundred_staff():
    assert SCHEMA.headcount >= 300
    assert len(SCHEMA.departments) == 8


def test_the_reporting_tree_has_exactly_one_root():
    roots = [e for e in SCHEMA.employees if e.manager_id is None]
    assert len(roots) == 1
    assert SCHEMA.root_count == 1
    assert roots[0].level == LEVEL_EXECUTIVE


def test_the_tree_is_acyclic_and_that_is_measured_not_asserted():
    """No foreign key, CHECK, or unique index can express this, so it is
    checked by walking every chain to a root."""
    assert SCHEMA.cycles == ()
    assert SCHEMA.acyclic
    assert "ORG-ACYCLIC" in {f.code for f in SCHEMA.findings}


def test_every_employee_reaches_the_root_within_the_stated_depth():
    managers = {e.employee_id: e.manager_id for e in SCHEMA.employees}
    for employee_id in managers:
        depth = 0
        current = managers[employee_id]
        while current is not None:
            depth += 1
            assert depth <= SCHEMA.max_depth, employee_id
            current = managers[current]
    assert SCHEMA.max_depth == 3


def test_no_employee_points_at_a_manager_that_is_not_in_the_table():
    ids = {e.employee_id for e in SCHEMA.employees}
    for employee in SCHEMA.employees:
        if employee.manager_id is not None:
            assert employee.manager_id in ids, employee.employee_id
    assert SCHEMA.orphans == ()


def test_the_cycle_detector_actually_finds_a_cycle_when_there_is_one():
    """A detector that never fires is indistinguishable from no detector,
    so it is shown catching one."""
    from tools.business_admin_hrm_console.core import Employee, _find_cycles
    looped = (
        Employee(1, "A", "D", "T", "Manager", 2, True),
        Employee(2, "B", "D", "T", "Manager", 3, True),
        Employee(3, "C", "D", "T", "Manager", 1, True),
    )
    cycles = _find_cycles(looped)
    assert cycles
    assert set(cycles[0]) == {1, 2, 3}


def test_the_department_headcounts_sum_to_the_generated_rows():
    assert sum(SCHEMA.headcount_by_department.values()) == SCHEMA.headcount


def test_every_employee_identifier_is_unique():
    ids = [e.employee_id for e in SCHEMA.employees]
    assert len(ids) == len(set(ids))


def test_no_employee_is_assigned_to_the_same_project_twice():
    """The composite key on the junction table, proved on the data."""
    pairs = [(a.employee_id, a.project_id) for a in SCHEMA.assignments]
    assert len(pairs) == len(set(pairs))


def test_every_assignment_and_project_references_a_real_row():
    people = {e.employee_id for e in SCHEMA.employees}
    projects = {p.project_id for p in SCHEMA.projects}
    clients = {c.client_id for c in SCHEMA.clients}
    for assignment in SCHEMA.assignments:
        assert assignment.employee_id in people
        assert assignment.project_id in projects
    for project in SCHEMA.projects:
        assert project.manager_id in people
        assert project.client_id in clients


def test_the_generation_is_deterministic():
    again = get_organization_schema()
    assert again.headcount == SCHEMA.headcount
    assert [e.employee_id for e in again.employees] == \
        [e.employee_id for e in SCHEMA.employees]


def test_the_schema_states_what_no_constraint_can_enforce():
    assert "ORG-CONSTRAINT" in {f.code for f in SCHEMA.findings}
    for table in SCHEMA.tables:
        assert table.constraint_gap.strip(), table.name


# ---------------------------------------------------------------------------
# Role based access control
# ---------------------------------------------------------------------------

def test_an_unknown_resource_is_denied_and_never_raises():
    """A matrix that falls through to allow hands every new table to every
    role until somebody remembers to restrict it."""
    for role, action in itertools.product(ROLES, ACTIONS):
        verdict = simulate_rbac_access(role, "table_added_next_quarter",
                                       action)
        assert verdict.decision == DENY, (role, action)
        assert not verdict.allowed
        assert not verdict.known_resource
        assert "RBAC-UNKNOWN" in {f.code for f in verdict.findings}


def test_an_employee_can_never_read_salary_or_write_permissions():
    for resource in ("employee_salary", "role_permission", "audit_log"):
        for action in ACTIONS:
            verdict = simulate_rbac_access(ROLE_EMPLOYEE, resource, action)
            assert verdict.decision == DENY, (resource, action)


def test_only_the_administrator_may_write_the_permission_table():
    """That grant is equivalent to being an administrator, whatever the
    role is called."""
    writers = {v.user_role for v in rbac_matrix()
               if v.resource_name == "role_permission"
               and v.action == ACTION_WRITE and v.allowed}
    assert writers == {ROLE_ADMIN}
    verdict = simulate_rbac_access(ROLE_ADMIN, "role_permission",
                                   ACTION_WRITE)
    assert "RBAC-ESCALATE" in {f.code for f in verdict.findings}


def test_only_the_administrator_may_touch_salary():
    readers = {v.user_role for v in rbac_matrix()
               if v.resource_name == "employee_salary" and v.allowed}
    assert readers == {ROLE_ADMIN}


def test_a_project_manager_gets_scoped_access_and_not_full_access():
    """Recording this as a plain allow is how a manager who may read their
    own clients ends up reading every client."""
    for resource in ("client_profile", "project", "project_assignment",
                     "timesheet_team"):
        verdict = simulate_rbac_access(ROLE_PROJECT_MANAGER, resource,
                                       ACTION_READ)
        assert verdict.decision == ALLOW_SCOPED, resource
        assert verdict.scoped
        assert verdict.row_predicate, resource
        assert "RBAC-SCOPED" in {f.code for f in verdict.findings}


def test_every_scoped_decision_carries_a_row_predicate():
    for verdict in rbac_matrix():
        if verdict.scoped:
            assert verdict.row_predicate.strip(), (verdict.user_role,
                                                   verdict.resource_name)
            assert "current_setting" in verdict.row_predicate


def test_no_full_allow_is_handed_out_on_a_row_specific_table():
    row_specific = ("client_profile", "project", "project_assignment",
                    "timesheet_team", "employee_assignment_history")
    for verdict in rbac_matrix():
        if verdict.resource_name in row_specific and \
                verdict.user_role != ROLE_ADMIN:
            assert verdict.decision != ALLOW, (verdict.user_role,
                                               verdict.resource_name)


def test_the_grid_is_exactly_as_permissive_as_it_claims():
    """Counted rather than described, so a grant added quietly fails a
    test."""
    grid = rbac_matrix()
    assert len(grid) == len(ROLES) * len(RESOURCES) * len(ACTIONS)
    counts = {role: sum(1 for v in grid
                        if v.user_role == role and v.allowed)
              for role in ROLES}
    assert counts == {ROLE_ADMIN: 19, ROLE_PROJECT_MANAGER: 10,
                      ROLE_EMPLOYEE: 5}


def test_the_employee_role_is_never_more_permissive_than_the_manager():
    for resource, action in itertools.product(RESOURCES, ACTIONS):
        employee = simulate_rbac_access(ROLE_EMPLOYEE, resource, action)
        manager = simulate_rbac_access(ROLE_PROJECT_MANAGER, resource,
                                       action)
        if employee.allowed:
            assert manager.allowed, (resource, action)


def test_an_unknown_role_or_action_raises():
    with pytest.raises(ValueError):
        simulate_rbac_access("Superuser", "project")
    with pytest.raises(ValueError):
        simulate_rbac_access(ROLE_ADMIN, "project", "drop")


# ---------------------------------------------------------------------------
# WordPress JWT bridge
# ---------------------------------------------------------------------------

def test_a_mapped_wordpress_user_gets_a_verified_session():
    bridge = simulate_wordpress_jwt_bridge(101)
    assert bridge.verified
    assert bridge.status == VERIFIED
    assert bridge.employee_id == WP_TO_EMPLOYEE[101]
    assert dict(bridge.session_context)["app.employee_id"] == "2"
    assert len(bridge.session_context) == 3


def test_an_unmapped_wordpress_user_gets_no_context_at_all():
    """A default employee identifier in a session variable is read by every
    policy on the database as a real person."""
    bridge = simulate_wordpress_jwt_bridge(999)
    assert bridge.verified
    assert bridge.employee_id is None
    assert bridge.session_context == ()
    assert bridge.status == UNVERIFIED
    assert "JWT-UNMAPPED" in {f.code for f in bridge.findings}


def test_the_algorithm_none_forgery_is_rejected_on_every_user():
    """The same payload with its header renamed and its signature removed."""
    for wp_user in list(WP_TO_EMPLOYEE) + [999]:
        bridge = simulate_wordpress_jwt_bridge(wp_user)
        assert not bridge.forgery_accepted, wp_user
        assert bridge.forged_token.endswith("."), wp_user
        assert "JWT-PINNED" in {f.code for f in bridge.findings}, wp_user


def test_the_forged_token_really_carries_the_same_claims():
    """Proves the forgery is refused for its signature and not because it
    was mangled into something unreadable."""
    bridge = simulate_wordpress_jwt_bridge(101)
    assert read_claims_without_verifying(bridge.forged_token) == \
        read_claims_without_verifying(bridge.token)
    header = json.loads(base64.urlsafe_b64decode(
        bridge.forged_token.split(".")[0] + "==").decode("utf-8"))
    assert header["alg"] == "none"


def test_a_token_signed_with_the_wrong_secret_is_rejected():
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "wp:101",
              "exp": FIXED_ISSUED_AT + 900}
    token = issue_token(claims, secret=b"a-different-secret")
    accepted, reason = verify_token(token, FIXED_ISSUED_AT)
    assert not accepted
    assert "signature" in reason.lower()


def test_an_expired_token_is_rejected():
    bridge = simulate_wordpress_jwt_bridge(101, ttl_seconds=900)
    accepted, reason = verify_token(bridge.token, FIXED_ISSUED_AT + 901)
    assert not accepted
    assert "expired" in reason.lower()


def test_a_token_with_no_expiry_is_rejected_rather_than_living_forever():
    token = issue_token({"iss": ISSUER, "aud": AUDIENCE, "sub": "wp:101"})
    accepted, reason = verify_token(token, FIXED_ISSUED_AT)
    assert not accepted
    assert "never expires" in reason


def test_a_token_from_another_issuer_or_for_another_audience_is_rejected():
    base = {"sub": "wp:101", "exp": FIXED_ISSUED_AT + 900}
    wrong_issuer = issue_token({**base, "iss": "attacker.example",
                                "aud": AUDIENCE})
    wrong_audience = issue_token({**base, "iss": ISSUER,
                                  "aud": "some-other-api"})
    assert not verify_token(wrong_issuer, FIXED_ISSUED_AT)[0]
    assert not verify_token(wrong_audience, FIXED_ISSUED_AT)[0]


def test_a_rejected_token_sets_no_session_variable():
    bridge = simulate_wordpress_jwt_bridge(101, ttl_seconds=60,
                                           now=FIXED_ISSUED_AT + 600)
    assert not bridge.verified
    assert bridge.session_context == ()
    assert bridge.severity == SEVERITY_CRITICAL


def test_the_claims_are_readable_by_anyone_holding_the_token():
    """A JSON web token is signed and not encrypted, so base64url is an
    encoding and not a cipher."""
    bridge = simulate_wordpress_jwt_bridge(102)
    claims = read_claims_without_verifying(bridge.token)
    assert claims["sub"] == "wp:102"
    assert claims["role"] == ROLE_PROJECT_MANAGER
    assert "JWT-READABLE" in {f.code for f in bridge.findings}


def test_a_token_is_three_dot_separated_segments_signed_with_hmac():
    bridge = simulate_wordpress_jwt_bridge(103)
    assert len(bridge.token.split(".")) == 3
    header = json.loads(base64.urlsafe_b64decode(
        bridge.token.split(".")[0] + "==").decode("utf-8"))
    assert header["alg"] == ALGORITHM
    assert verify_token(bridge.token, FIXED_ISSUED_AT)[0]


def test_a_malformed_token_is_rejected_without_raising():
    for junk in ("", "abc", "a.b", "a.b.c.d", "not.a.token"):
        accepted, reason = verify_token(junk, FIXED_ISSUED_AT)
        assert not accepted, junk
        assert reason.strip(), junk


def test_forging_a_token_that_is_not_a_token_raises_clearly():
    with pytest.raises(ValueError):
        forge_alg_none("nonsense")


# ---------------------------------------------------------------------------
# House invariants
# ---------------------------------------------------------------------------

def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    """The engine has to run outside the app, or it is not an engine."""
    script = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.business_admin_hrm_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT))
    done = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    for name in ("core.py", "page.py"):
        text = (TOOL_DIR / name).read_text(encoding="utf-8")
        assert "—" not in text, name
        assert "–" not in text, name


def test_the_tool_is_registered_with_a_unique_icon():
    tools = [t for t in all_tools() if not t.key.startswith("synthetic-")]
    keys = [t.key for t in tools]
    assert "business-admin-hrm-console" in keys
    icons = [t.icon for t in tools]
    assert len(icons) == len(set(icons))
    assert SEVERITY_OK != SEVERITY_CRITICAL
