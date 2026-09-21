"""Business administration and HRM architecture engine.

No Streamlit import lives in this file. The organisation is generated
rather than described, the JWT is really signed rather than mocked, and
the access matrix is enumerated rather than summarised, because each of
those is the difference between a claim and a proof.

Three failures shape the file:

* a manager column that references the employee table cannot be stopped
  from forming a cycle by any constraint the database offers. A reports to
  B reports to A satisfies every foreign key, and the first recursive
  headcount query hangs. The tree here is generated and then walked, so
  the acyclic property is measured;
* an access matrix that falls through to allow when it does not recognise
  a resource is a matrix with a hole in it. Default is deny, an unknown
  resource is denied by name rather than raising, and write access to the
  permission tables themselves is treated as equivalent to being an
  administrator, because it is;
* a JSON web token is signed, not encrypted. Anyone holding one can read
  every claim in it. A verifier that trusts the algorithm named in the
  token's own header accepts a forgery with no signature at all, so this
  one pins the algorithm server side and proves the forgery is rejected.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
from dataclasses import dataclass, field
from typing import Sequence

ENGINE_VERSION = "1.0.0"

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"

_SEVERITY_ORDER = {SEVERITY_OK: 0, SEVERITY_WARN: 1, SEVERITY_CRITICAL: 2}

ORG_SEED = 20260921


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


def _worst(findings: Sequence[Finding]) -> str:
    if not findings:
        return SEVERITY_OK
    return max((f.severity for f in findings), key=lambda s: _SEVERITY_ORDER[s])


# ---------------------------------------------------------------------------
# 1. Organisation schema
# ---------------------------------------------------------------------------

LEVEL_EXECUTIVE = "Executive"
LEVEL_DIRECTOR = "Director"
LEVEL_MANAGER = "Manager"
LEVEL_INDIVIDUAL = "Individual contributor"

DEPARTMENTS: tuple[str, ...] = (
    "Engineering", "Client Services", "Finance", "People Operations",
    "Sales", "Marketing", "Legal and Compliance", "Facilities",
)

TEAMS_PER_DEPARTMENT = 4


@dataclass(frozen=True)
class Employee:
    employee_id: int
    full_name: str
    department: str
    team: str
    level: str
    manager_id: int | None
    active: bool


@dataclass(frozen=True)
class Project:
    project_id: int
    name: str
    client_id: int
    manager_id: int
    department: str


@dataclass(frozen=True)
class Client:
    client_id: int
    name: str
    account_owner_id: int


@dataclass(frozen=True)
class Assignment:
    employee_id: int
    project_id: int
    allocation_percent: int


@dataclass(frozen=True)
class TableSpec:
    name: str
    purpose: str
    primary_key: tuple[str, ...]
    foreign_keys: tuple[str, ...]
    columns: tuple[str, ...]
    constraint_gap: str


@dataclass(frozen=True)
class OrganizationSchema:
    employees: tuple[Employee, ...]
    departments: tuple[str, ...]
    clients: tuple[Client, ...]
    projects: tuple[Project, ...]
    assignments: tuple[Assignment, ...]
    tables: tuple[TableSpec, ...]
    headcount_by_department: dict[str, int]
    max_depth: int
    root_count: int
    cycles: tuple[tuple[int, ...], ...]
    orphans: tuple[int, ...]
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def headcount(self) -> int:
        return len(self.employees)

    @property
    def acyclic(self) -> bool:
        return not self.cycles

    @property
    def severity(self) -> str:
        return _worst(self.findings)


_TABLES: tuple[TableSpec, ...] = (
    TableSpec(
        name="employee",
        purpose="One row per person the company has ever employed.",
        primary_key=("employee_id",),
        foreign_keys=("manager_id references employee(employee_id)",
                      "department_id references department(department_id)"),
        columns=("employee_id", "full_name", "work_email", "department_id",
                 "team_id", "level", "manager_id", "hired_on",
                 "terminated_on"),
        constraint_gap=("The self reference cannot be stopped from forming "
                        "a cycle. A reports to B reports to A satisfies "
                        "every foreign key in the definition.")),
    TableSpec(
        name="department",
        purpose="Cost centre and reporting unit.",
        primary_key=("department_id",),
        foreign_keys=("head_employee_id references employee(employee_id)",),
        columns=("department_id", "name", "cost_centre",
                 "head_employee_id"),
        constraint_gap=("Circular with employee: a department needs a head "
                        "and a head needs a department, so one side has to "
                        "be nullable or the insert order is impossible.")),
    TableSpec(
        name="employee_assignment_history",
        purpose=("Effective dated department, level, and manager. The "
                 "record of where someone sat, not only where they sit."),
        primary_key=("employee_id", "effective_from"),
        foreign_keys=("employee_id references employee(employee_id)",),
        columns=("employee_id", "effective_from", "effective_to",
                 "department_id", "level", "manager_id"),
        constraint_gap=("Nothing stops two rows overlapping in time. An "
                        "exclusion constraint on a daterange is the only "
                        "thing that does.")),
    TableSpec(
        name="client",
        purpose="External organisation the company bills.",
        primary_key=("client_id",),
        foreign_keys=("account_owner_id references employee(employee_id)",),
        columns=("client_id", "name", "account_owner_id", "tenant_id"),
        constraint_gap=("Client contact details are personal data and sit "
                        "in the same table as commercial fields, so a grant "
                        "that opens the commercials opens the personal data "
                        "with them.")),
    TableSpec(
        name="project",
        purpose="A billable engagement for one client.",
        primary_key=("project_id",),
        foreign_keys=("client_id references client(client_id)",
                      "manager_id references employee(employee_id)"),
        columns=("project_id", "name", "client_id", "manager_id",
                 "department_id", "starts_on", "ends_on"),
        constraint_gap=("Nothing requires the manager to still be active, "
                        "so a project can be owned by someone who left.")),
    TableSpec(
        name="project_assignment",
        purpose=("Junction between employee and project, carrying the "
                 "allocation."),
        primary_key=("employee_id", "project_id"),
        foreign_keys=("employee_id references employee(employee_id)",
                      "project_id references project(project_id)"),
        columns=("employee_id", "project_id", "allocation_percent",
                 "assigned_on"),
        constraint_gap=("The composite key stops a duplicate assignment. It "
                        "does not stop one person being allocated past one "
                        "hundred percent across several projects.")),
    TableSpec(
        name="role_permission",
        purpose="Which role may do what to which resource.",
        primary_key=("role", "resource", "action"),
        foreign_keys=(),
        columns=("role", "resource", "action", "scope"),
        constraint_gap=("Write access to this table is equivalent to being "
                        "an administrator, whatever the role is called.")),
)


def _build_people(seed: int = ORG_SEED) -> tuple[Employee, ...]:
    rng = random.Random(seed)
    people: list[Employee] = []
    next_id = 1

    ceo = Employee(next_id, "Employee 0001", "Executive", "Executive Office",
                   LEVEL_EXECUTIVE, None, True)
    people.append(ceo)
    next_id += 1

    for department in DEPARTMENTS:
        director_id = next_id
        people.append(Employee(director_id, f"Employee {director_id:04d}",
                               department, f"{department} Leadership",
                               LEVEL_DIRECTOR, ceo.employee_id, True))
        next_id += 1

        for team_index in range(1, TEAMS_PER_DEPARTMENT + 1):
            team = f"{department} Team {team_index}"
            manager_id = next_id
            people.append(Employee(manager_id, f"Employee {manager_id:04d}",
                                   department, team, LEVEL_MANAGER,
                                   director_id, True))
            next_id += 1

            for _ in range(rng.randint(7, 10)):
                active = rng.random() > 0.04
                people.append(Employee(next_id, f"Employee {next_id:04d}",
                                       department, team, LEVEL_INDIVIDUAL,
                                       manager_id, active))
                next_id += 1

    return tuple(people)


def _find_cycles(people: Sequence[Employee]) -> tuple[tuple[int, ...], ...]:
    """Walk every chain to a root. A chain that revisits a node is a cycle.

    No foreign key, no CHECK, and no unique index can express this. A
    trigger or a recursive query is the only place it can be caught, which
    is why it has to be caught deliberately.
    """
    managers = {person.employee_id: person.manager_id for person in people}
    cycles: list[tuple[int, ...]] = []
    for start in managers:
        seen: list[int] = []
        current: int | None = start
        while current is not None:
            if current in seen:
                cycle = tuple(seen[seen.index(current):])
                if cycle not in cycles:
                    cycles.append(cycle)
                break
            seen.append(current)
            current = managers.get(current)
    return tuple(cycles)


def _depth_of(employee_id: int, managers: dict[int, int | None]) -> int:
    depth = 0
    current = managers.get(employee_id)
    while current is not None and depth < 64:
        depth += 1
        current = managers.get(current)
    return depth


def get_organization_schema(seed: int = ORG_SEED) -> OrganizationSchema:
    """Generate the org, then measure the properties a diagram only claims.

    The headcount, the depth, the absence of cycles, and the absence of
    orphans are all computed from the generated rows. A schema diagram
    asserts these. A generated organisation either has them or does not.
    """
    people = _build_people(seed)
    managers = {person.employee_id: person.manager_id for person in people}
    ids = set(managers)

    cycles = _find_cycles(people)
    orphans = tuple(sorted(
        person.employee_id for person in people
        if person.manager_id is not None and person.manager_id not in ids))
    roots = [person for person in people if person.manager_id is None]
    max_depth = max(_depth_of(one, managers) for one in ids)

    headcount_by_department: dict[str, int] = {}
    for person in people:
        headcount_by_department[person.department] = (
            headcount_by_department.get(person.department, 0) + 1)

    rng = random.Random(seed + 1)
    directors = [p for p in people if p.level == LEVEL_DIRECTOR]
    team_managers = [p for p in people if p.level == LEVEL_MANAGER]

    clients = tuple(
        Client(index, f"Client {index:03d}",
               rng.choice(directors).employee_id)
        for index in range(1, 25))

    projects = tuple(
        Project(index, f"Project {index:03d}",
                rng.choice(clients).client_id,
                rng.choice(team_managers).employee_id,
                rng.choice(DEPARTMENTS))
        for index in range(1, 61))

    contributors = [p for p in people
                    if p.level == LEVEL_INDIVIDUAL and p.active]
    seen_pairs: set[tuple[int, int]] = set()
    assignments: list[Assignment] = []
    for project in projects:
        for person in rng.sample(contributors, rng.randint(3, 7)):
            pair = (person.employee_id, project.project_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            assignments.append(Assignment(person.employee_id,
                                          project.project_id,
                                          rng.choice((10, 20, 25, 50))))

    findings: list[Finding] = []

    if cycles:
        findings.append(Finding(
            code="ORG-CYCLE", severity=SEVERITY_CRITICAL,
            title=f"{len(cycles)} reporting cycle(s) in the tree",
            detail=("A recursive headcount query against this data does not "
                    "terminate, and no foreign key in the schema could have "
                    "stopped it forming."),
            fix="Add a trigger that walks to a root before accepting a "
                "manager change."))
    else:
        findings.append(Finding(
            code="ORG-ACYCLIC", severity=SEVERITY_OK,
            title=f"{len(people)} chains walked, no cycle found",
            detail=("Every employee reaches the single root. This was "
                    "measured by walking the generated rows, not asserted "
                    "by the diagram, because the self referencing key "
                    "cannot express it."),
            fix=("Keep the walk as a nightly check, since the constraint "
                 "that would enforce it does not exist.")))

    findings.append(Finding(
        code="ORG-CONSTRAINT", severity=SEVERITY_CRITICAL,
        title="No database constraint can prevent a reporting cycle",
        detail=("A foreign key checks that the manager exists. A CHECK "
                "constraint sees one row. A unique index sees one column "
                "set. None of them can see a path, so the only enforcement "
                "available is a trigger running a recursive query on every "
                "manager change."),
        fix=("Write the trigger on day one. Retrofitting it means first "
             "finding the cycles already in the data.")))

    if orphans:
        findings.append(Finding(
            code="ORG-ORPHAN", severity=SEVERITY_CRITICAL,
            title=f"{len(orphans)} employee(s) reference a manager that is "
                  f"not in the table",
            detail="The foreign key is either missing or not validated.",
            fix="Add the key and run VALIDATE CONSTRAINT."))

    findings.append(Finding(
        code="ORG-TEMPORAL", severity=SEVERITY_WARN,
        title="A single department column cannot answer last year",
        detail=("Storing the current department on the employee row makes "
                "headcount by department as at any past date unanswerable, "
                "and that is most of what a people team is asked for. The "
                "effective dated history table exists for exactly this, and "
                "nothing stops two of its rows overlapping in time except "
                "an exclusion constraint on a daterange."),
        fix=("EXCLUDE USING gist (employee_id WITH =, "
             "daterange(effective_from, effective_to) WITH &&)")))

    findings.append(Finding(
        code="ORG-DELETE", severity=SEVERITY_WARN,
        title="A leaver cannot be deleted, only dated out",
        detail=("Timesheets, approvals, and project history all reference "
                "the employee row. Deleting it either fails on the key or "
                "cascades away the record of work that was really done and "
                "really billed."),
        fix="Set terminated_on and revoke the login. Never DELETE a person."))

    headline = (f"{len(people)} employees across {len(DEPARTMENTS)} "
                f"departments, depth {max_depth}, {len(cycles)} cycle(s)")

    return OrganizationSchema(
        employees=people, departments=DEPARTMENTS, clients=clients,
        projects=projects, assignments=tuple(assignments), tables=_TABLES,
        headcount_by_department=headcount_by_department,
        max_depth=max_depth, root_count=len(roots), cycles=cycles,
        orphans=orphans, headline=headline, findings=tuple(findings))


# ---------------------------------------------------------------------------
# 2. Role based access control
# ---------------------------------------------------------------------------

ROLE_ADMIN = "Admin"
ROLE_PROJECT_MANAGER = "Project Manager"
ROLE_EMPLOYEE = "Employee"

ROLES: tuple[str, ...] = (ROLE_ADMIN, ROLE_PROJECT_MANAGER, ROLE_EMPLOYEE)

ACTION_READ = "read"
ACTION_WRITE = "write"

ACTIONS: tuple[str, ...] = (ACTION_READ, ACTION_WRITE)

RESOURCES: tuple[str, ...] = (
    "employee_directory",
    "employee_salary",
    "employee_assignment_history",
    "client_profile",
    "project",
    "project_assignment",
    "timesheet_own",
    "timesheet_team",
    "role_permission",
    "audit_log",
)

ALLOW = "Allowed"
ALLOW_SCOPED = "Allowed, but only for rows the caller owns"
DENY = "Denied"

# Default is deny. A resource absent from this table is denied by name and
# never falls through to allow.
_MATRIX: dict[tuple[str, str, str], str] = {
    (ROLE_ADMIN, "employee_directory", ACTION_READ): ALLOW,
    (ROLE_ADMIN, "employee_directory", ACTION_WRITE): ALLOW,
    (ROLE_ADMIN, "employee_salary", ACTION_READ): ALLOW,
    (ROLE_ADMIN, "employee_salary", ACTION_WRITE): ALLOW,
    (ROLE_ADMIN, "employee_assignment_history", ACTION_READ): ALLOW,
    (ROLE_ADMIN, "employee_assignment_history", ACTION_WRITE): ALLOW,
    (ROLE_ADMIN, "client_profile", ACTION_READ): ALLOW,
    (ROLE_ADMIN, "client_profile", ACTION_WRITE): ALLOW,
    (ROLE_ADMIN, "project", ACTION_READ): ALLOW,
    (ROLE_ADMIN, "project", ACTION_WRITE): ALLOW,
    (ROLE_ADMIN, "project_assignment", ACTION_READ): ALLOW,
    (ROLE_ADMIN, "project_assignment", ACTION_WRITE): ALLOW,
    (ROLE_ADMIN, "timesheet_own", ACTION_READ): ALLOW,
    (ROLE_ADMIN, "timesheet_own", ACTION_WRITE): ALLOW,
    (ROLE_ADMIN, "timesheet_team", ACTION_READ): ALLOW,
    (ROLE_ADMIN, "timesheet_team", ACTION_WRITE): ALLOW,
    (ROLE_ADMIN, "role_permission", ACTION_READ): ALLOW,
    (ROLE_ADMIN, "role_permission", ACTION_WRITE): ALLOW,
    (ROLE_ADMIN, "audit_log", ACTION_READ): ALLOW,

    (ROLE_PROJECT_MANAGER, "employee_directory", ACTION_READ): ALLOW,
    (ROLE_PROJECT_MANAGER, "employee_assignment_history",
     ACTION_READ): ALLOW_SCOPED,
    (ROLE_PROJECT_MANAGER, "client_profile", ACTION_READ): ALLOW_SCOPED,
    (ROLE_PROJECT_MANAGER, "project", ACTION_READ): ALLOW_SCOPED,
    (ROLE_PROJECT_MANAGER, "project", ACTION_WRITE): ALLOW_SCOPED,
    (ROLE_PROJECT_MANAGER, "project_assignment", ACTION_READ): ALLOW_SCOPED,
    (ROLE_PROJECT_MANAGER, "project_assignment", ACTION_WRITE): ALLOW_SCOPED,
    (ROLE_PROJECT_MANAGER, "timesheet_own", ACTION_READ): ALLOW,
    (ROLE_PROJECT_MANAGER, "timesheet_own", ACTION_WRITE): ALLOW,
    (ROLE_PROJECT_MANAGER, "timesheet_team", ACTION_READ): ALLOW_SCOPED,

    (ROLE_EMPLOYEE, "employee_directory", ACTION_READ): ALLOW,
    (ROLE_EMPLOYEE, "project", ACTION_READ): ALLOW_SCOPED,
    (ROLE_EMPLOYEE, "project_assignment", ACTION_READ): ALLOW_SCOPED,
    (ROLE_EMPLOYEE, "timesheet_own", ACTION_READ): ALLOW,
    (ROLE_EMPLOYEE, "timesheet_own", ACTION_WRITE): ALLOW,
}


@dataclass(frozen=True)
class AccessVerdict:
    user_role: str
    resource_name: str
    action: str
    decision: str
    row_predicate: str
    known_resource: bool
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def allowed(self) -> bool:
        return self.decision in (ALLOW, ALLOW_SCOPED)

    @property
    def scoped(self) -> bool:
        return self.decision == ALLOW_SCOPED

    @property
    def severity(self) -> str:
        return _worst(self.findings)


_PREDICATES: dict[str, str] = {
    "employee_assignment_history":
        "employee_id IN (SELECT employee_id FROM project_assignment WHERE "
        "project_id = ANY(current_setting('app.managed_projects')::int[]))",
    "client_profile":
        "client_id IN (SELECT client_id FROM project WHERE manager_id = "
        "current_setting('app.employee_id')::int)",
    "project":
        "manager_id = current_setting('app.employee_id')::int",
    "project_assignment":
        "project_id IN (SELECT project_id FROM project WHERE manager_id = "
        "current_setting('app.employee_id')::int)",
    "timesheet_team":
        "employee_id IN (SELECT employee_id FROM employee WHERE manager_id "
        "= current_setting('app.employee_id')::int)",
}

_EMPLOYEE_PREDICATES: dict[str, str] = {
    "project":
        "project_id IN (SELECT project_id FROM project_assignment WHERE "
        "employee_id = current_setting('app.employee_id')::int)",
    "project_assignment":
        "employee_id = current_setting('app.employee_id')::int",
}


def simulate_rbac_access(user_role: str, resource_name: str,
                         action: str = ACTION_READ) -> AccessVerdict:
    """Decide one access, defaulting to deny and never to allow.

    Two things make this matrix worth anything. First, a resource that is
    not in the table is denied by name rather than raising or falling
    through, because a new table added next quarter must not be readable
    by everybody until somebody remembers to restrict it.

    Second, an allow that is really an allow for some rows is returned as
    its own decision with the predicate attached. Recording it as a plain
    allow is how a project manager who may read their own clients ends up
    reading every client: the role check passes, and the row filter that
    was supposed to follow it lives only in whichever query remembered it.
    """
    role = str(user_role or "").strip()
    resource = str(resource_name or "").strip()
    verb = str(action or "").strip().lower()
    findings: list[Finding] = []

    if role not in ROLES:
        raise ValueError(f"unknown role {user_role!r}")
    if verb not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")

    known = resource in RESOURCES
    decision = _MATRIX.get((role, resource, verb), DENY)
    predicate = ""

    if decision == ALLOW_SCOPED:
        if role == ROLE_EMPLOYEE:
            predicate = _EMPLOYEE_PREDICATES.get(resource, "")
        else:
            predicate = _PREDICATES.get(resource, "")

    if not known:
        findings.append(Finding(
            code="RBAC-UNKNOWN", severity=SEVERITY_WARN,
            title=f"{resource!r} is not a resource this matrix knows",
            detail=("It was denied rather than allowed and rather than "
                    "raising. A matrix that falls through to allow hands "
                    "every new table to every role until somebody remembers "
                    "to restrict it, and nothing fails while that is true."),
            fix=("Add the resource to the matrix in the same change that "
                 "adds the table.")))
    elif decision == DENY:
        findings.append(Finding(
            code="RBAC-DENY", severity=SEVERITY_OK,
            title=f"{role} may not {verb} {resource}",
            detail=("The default is deny, so this needed no rule. A grant "
                    "has to be written down to exist."),
            fix="Keep the matrix as the only source of grants."))
    elif decision == ALLOW_SCOPED:
        findings.append(Finding(
            code="RBAC-SCOPED", severity=SEVERITY_CRITICAL,
            title=f"{role} may {verb} {resource}, but only some rows",
            detail=("The role check alone is not sufficient here. Without "
                    "the row predicate attached, this reads as a plain "
                    "allow and the caller sees every row in the table."),
            fix=("Enforce the predicate as a row level security policy on "
                 "the table, not as a WHERE clause each query has to "
                 "remember.")))
    else:
        findings.append(Finding(
            code="RBAC-ALLOW", severity=SEVERITY_OK,
            title=f"{role} may {verb} {resource} in full",
            detail=("Every row, with no further filter. That is only safe "
                    "where the table holds nothing row specific to a "
                    "caller."),
            fix="Re examine any full allow on a table that gains a tenant "
                "column later."))

    if resource == "role_permission" and verb == ACTION_WRITE and \
            decision != DENY:
        findings.append(Finding(
            code="RBAC-ESCALATE", severity=SEVERITY_CRITICAL,
            title="Write access here is equivalent to being an administrator",
            detail=("A role that can edit the permission table can grant "
                    "itself anything, so the name of the role stops meaning "
                    "anything the moment this grant exists."),
            fix=("Grant it to one break glass account with its use alerted, "
                 "never to a working role.")))

    headline = f"{role} {verb} {resource}: {decision}"
    return AccessVerdict(
        user_role=role, resource_name=resource, action=verb,
        decision=decision, row_predicate=predicate, known_resource=known,
        headline=headline, findings=tuple(findings))


def rbac_matrix() -> tuple[AccessVerdict, ...]:
    """Every role against every resource and action, with nothing hidden."""
    return tuple(
        simulate_rbac_access(role, resource, action)
        for role in ROLES for resource in RESOURCES for action in ACTIONS)


# ---------------------------------------------------------------------------
# 3. WordPress to PostgreSQL JWT bridge
# ---------------------------------------------------------------------------

SIGNING_SECRET = b"demo-signing-secret-not-a-real-one"
ALGORITHM = "HS256"
ISSUER = "wordpress.example"
AUDIENCE = "hrm-api"
DEFAULT_TTL_SECONDS = 900
FIXED_ISSUED_AT = 1758412800

VERIFIED = "Verified, session context set"
UNVERIFIED = "Rejected, no session context set"

# Which WordPress users map to an employee. A WordPress account with no
# mapping gets no context, never a default one.
WP_TO_EMPLOYEE: dict[int, int] = {
    101: 2, 102: 10, 103: 43, 104: 187,
}

WP_ROLES: dict[int, str] = {
    101: ROLE_ADMIN, 102: ROLE_PROJECT_MANAGER, 103: ROLE_EMPLOYEE,
    104: ROLE_EMPLOYEE,
}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(signing_input: bytes, secret: bytes = SIGNING_SECRET) -> str:
    return _b64url(hmac.new(secret, signing_input, hashlib.sha256).digest())


def issue_token(claims: dict, secret: bytes = SIGNING_SECRET,
                algorithm: str = ALGORITHM) -> str:
    header = {"alg": algorithm, "typ": "JWT"}
    header_part = _b64url(json.dumps(header, separators=(",", ":"),
                                     sort_keys=True).encode("utf-8"))
    payload_part = _b64url(json.dumps(claims, separators=(",", ":"),
                                      sort_keys=True).encode("utf-8"))
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    return f"{header_part}.{payload_part}.{_sign(signing_input, secret)}"


def forge_alg_none(token: str) -> str:
    """Strip the signature and rename the algorithm, as the attack does."""
    _, payload_part, _ = token.split(".")
    header = {"alg": "none", "typ": "JWT"}
    header_part = _b64url(json.dumps(header, separators=(",", ":"),
                                     sort_keys=True).encode("utf-8"))
    return f"{header_part}.{payload_part}."


def read_claims_without_verifying(token: str) -> dict:
    """What anyone holding the token can read. A JWT is signed, not
    encrypted, so this needs no secret and no permission."""
    parts = str(token or "").split(".")
    if len(parts) < 2:
        raise ValueError("not a JSON web token")
    return json.loads(_b64url_decode(parts[1]).decode("utf-8"))


def verify_token(token: str, now: int, secret: bytes = SIGNING_SECRET
                 ) -> tuple[bool, str]:
    """Verify with the algorithm pinned server side.

    The header is read for diagnostics and never for the decision. A
    verifier that takes its algorithm from the token's own header accepts
    a token whose header says none and whose signature is empty.
    """
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return False, "The token is not three dot separated segments"

    header_part, payload_part, signature = parts
    try:
        header = json.loads(_b64url_decode(header_part).decode("utf-8"))
        claims = json.loads(_b64url_decode(payload_part).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False, "The header or payload is not decodable JSON"

    if header.get("alg") != ALGORITHM:
        return False, (f"The header asks for {header.get('alg')!r} and this "
                       f"verifier only accepts {ALGORITHM}")

    expected = _sign(f"{header_part}.{payload_part}".encode("ascii"), secret)
    if not hmac.compare_digest(expected, signature):
        return False, "The signature does not match"

    if "exp" not in claims:
        return False, "The token carries no expiry, so it never expires"
    if int(claims["exp"]) <= now:
        return False, "The token has expired"
    if claims.get("iss") != ISSUER:
        return False, f"Issued by {claims.get('iss')!r}, not {ISSUER}"
    if claims.get("aud") != AUDIENCE:
        return False, f"Addressed to {claims.get('aud')!r}, not {AUDIENCE}"

    return True, "Signature, expiry, issuer, and audience all check out"


@dataclass(frozen=True)
class BridgeResult:
    wp_user_id: int
    token: str
    claims: dict
    verified: bool
    reason: str
    employee_id: int | None
    role: str
    session_context: tuple[tuple[str, str], ...]
    forged_token: str
    forgery_accepted: bool
    status: str
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def simulate_wordpress_jwt_bridge(wp_user_id: int,
                                  issued_at: int = FIXED_ISSUED_AT,
                                  ttl_seconds: int = DEFAULT_TTL_SECONDS,
                                  now: int | None = None) -> BridgeResult:
    """Issue, verify, and set the PostgreSQL session context from claims.

    The session context is set from verified claims only, and a WordPress
    account with no employee mapping gets no context at all rather than a
    default one. A default employee identifier in a session variable is
    read by every row level security policy on the database as though it
    were a real person.
    """
    user = int(wp_user_id)
    clock = issued_at if now is None else int(now)
    findings: list[Finding] = []

    employee_id = WP_TO_EMPLOYEE.get(user)
    role = WP_ROLES.get(user, ROLE_EMPLOYEE)

    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": f"wp:{user}",
        "iat": issued_at,
        "exp": issued_at + int(ttl_seconds),
        "role": role,
    }
    if employee_id is not None:
        claims["employee_id"] = employee_id

    token = issue_token(claims)
    verified, reason = verify_token(token, clock)

    forged = forge_alg_none(token)
    forgery_accepted, _ = verify_token(forged, clock)

    context: list[tuple[str, str]] = []
    if verified and employee_id is not None:
        context = [
            ("app.wp_user_id", str(user)),
            ("app.employee_id", str(employee_id)),
            ("app.role", role),
        ]
        status = VERIFIED
        findings.append(Finding(
            code="JWT-CONTEXT", severity=SEVERITY_OK,
            title=f"Session context set for employee {employee_id}",
            detail=("Every value here came from a claim that survived "
                    "signature, expiry, issuer, and audience checks. Row "
                    "level security policies read these variables, so a "
                    "value that arrived unverified would be a value the "
                    "database trusts."),
            fix=("SET LOCAL, so the context dies with the transaction "
                 "rather than outliving it on a pooled connection.")))
    else:
        status = UNVERIFIED
        if verified and employee_id is None:
            findings.append(Finding(
                code="JWT-UNMAPPED", severity=SEVERITY_CRITICAL,
                title=f"WordPress user {user} maps to no employee",
                detail=("The token is valid and the account is real, and "
                        "there is still nobody on the other side of it. No "
                        "context is set at all, because a default employee "
                        "identifier in a session variable is read by every "
                        "policy on the database as a real person."),
                fix=("Refuse the session and ask the people team to map or "
                     "deactivate the WordPress account.")))
        else:
            findings.append(Finding(
                code="JWT-REJECT", severity=SEVERITY_CRITICAL,
                title=f"Token rejected: {reason}",
                detail=("Nothing downstream ran. A failed verification that "
                        "still sets a partial context is worse than one "
                        "that raises, because the partial context looks "
                        "like a logged in user."),
                fix="Return 401 and set no session variable at all."))

    if forgery_accepted:
        findings.append(Finding(
            code="JWT-ALGNONE", severity=SEVERITY_CRITICAL,
            title="A token with no signature was accepted",
            detail=("The verifier took its algorithm from the token's own "
                    "header, so an attacker renames it to none, deletes the "
                    "signature, and edits any claim they like."),
            fix=f"Pin the algorithm to {ALGORITHM} server side."))
    else:
        findings.append(Finding(
            code="JWT-PINNED", severity=SEVERITY_OK,
            title="The algorithm is pinned, so the none forgery is refused",
            detail=("The same payload with its header renamed to none and "
                    "its signature removed is rejected, because the "
                    "verifier never reads the algorithm out of the token "
                    "it is checking."),
            fix="Keep the accepted algorithm a server side constant."))

    findings.append(Finding(
        code="JWT-READABLE", severity=SEVERITY_WARN,
        title="Every claim in this token is readable by anyone holding it",
        detail=("A JSON web token is signed and not encrypted. The payload "
                "is base64url, which is an encoding and not a cipher, so a "
                "salary band or a national identifier placed in a claim is "
                "published to whoever has the token."),
        fix=("Put an identifier in the claims and look the sensitive fields "
             "up behind it.")))

    findings.append(Finding(
        code="JWT-TTL", severity=SEVERITY_WARN,
        title=f"This token lives {ttl_seconds} second(s)",
        detail=("A token cannot be withdrawn once issued, so its lifetime "
                "is the window an attacker keeps access after the account "
                "is disabled in WordPress. A long lived token is a "
                "revocation problem wearing a convenience argument."),
        fix=("Keep the access token short and put the renewal behind a "
             "refresh that checks the account is still active.")))

    headline = (f"WordPress user {user}: {status}"
                + (f", employee {employee_id}" if employee_id else ""))

    return BridgeResult(
        wp_user_id=user, token=token, claims=claims, verified=verified,
        reason=reason, employee_id=employee_id, role=role,
        session_context=tuple(context), forged_token=forged,
        forgery_accepted=forgery_accepted, status=status, headline=headline,
        findings=tuple(findings))
