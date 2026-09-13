"""Microsoft 365 intranet engine.

An intranet built on SharePoint, Power Apps and Power Automate fails in ways
that are quiet rather than loud. A module hidden from an employee but still
reachable by its link. Two PTO requests that each pass validation and together
overdraw the balance. A request approved by the person who raised it. A weekly
report nobody reads because it is a table rather than an answer.

So this engine treats those four as the product rather than as edge cases:

  Access is decided by a check, not by what the navigation happens to render.
  A pending request reserves its days, so the second one sees the balance the
  first one already spent. Approval requires a different person with the right
  role. And the insight module turns hours and deadlines into a stated risk,
  with the arithmetic attached.

Pure logic, no Streamlit import, so this is unit testable on its own and can sit
behind the real Graph and Dataverse calls. Deterministic: nothing here reads the
clock or a random source unless the caller passes one in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

ENGINE_VERSION = "1.0.0"

TENANT = "contoso-pro.sharepoint.com"
TODAY = "2026-09-14"


# ---------------------------------------------------------------------------
# Roles and modules
# ---------------------------------------------------------------------------

ROLE_EMPLOYEE = "Employee"
ROLE_MANAGER = "Manager"
ROLES: tuple[str, ...] = (ROLE_EMPLOYEE, ROLE_MANAGER)


@dataclass(frozen=True)
class Module:
    key: str
    label: str
    summary: str
    roles: tuple[str, ...]
    entra_group: str
    sharepoint_list: str


MODULES: tuple[Module, ...] = (
    Module("timesheet", "My Timesheet",
           "Log hours against the projects this person is assigned to.",
           ROLES, "SG-Intranet-AllStaff", "Timesheets"),
    Module("pto_request", "Request Time Off",
           "Raise a PTO request against the current balance.",
           ROLES, "SG-Intranet-AllStaff", "PTO Requests"),
    Module("handbook", "Company Handbook",
           "Policies, org chart and the onboarding pack.",
           ROLES, "SG-Intranet-AllStaff", "Handbook"),
    Module("approvals", "Team Approvals",
           "Approve or reject PTO raised by direct reports.",
           (ROLE_MANAGER,), "SG-Intranet-Managers", "PTO Requests"),
    Module("insights", "Project Insights",
           "Budget burn, deadlines at risk and the weekly executive summary.",
           (ROLE_MANAGER,), "SG-Intranet-Managers", "Projects"),
    Module("utilisation", "Team Utilisation",
           "Hours billed per person against capacity.",
           (ROLE_MANAGER,), "SG-Intranet-Managers", "Timesheets"),
)

DENIED_NOT_IN_GROUP = "Not in the Entra ID group this module requires"


def module_by_key(key: str, modules=MODULES) -> Module | None:
    for module in modules:
        if module.key == key:
            return module
    return None


def visible_modules(role: str, modules=MODULES) -> list[Module]:
    return [module for module in modules if role in module.roles]


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    module: Module | None
    role: str
    reason: str


def can_open(role: str, module_key: str, modules=MODULES) -> AccessDecision:
    """Decide access by a check rather than by what the navigation rendered.

    This is the point of the gateway. Hiding a tile is a convenience, not a
    control: the module still answers a direct link, and a Power App that
    trusts the navigation is one bookmarked URL away from showing an employee
    the whole team's leave. So every open is authorised here, and the UI simply
    asks the same function the back end does.
    """
    module = module_by_key(module_key, modules)
    if module is None:
        return AccessDecision(False, None, role,
                              f"There is no module named {module_key!r}, so "
                              f"there is nothing to authorise.")
    if role not in module.roles:
        return AccessDecision(
            False, module, role,
            f"{DENIED_NOT_IN_GROUP}. {module.label} is granted through "
            f"{module.entra_group}, and this session holds {role}. Hiding the "
            f"tile would not be enough: the link still resolves.")
    return AccessDecision(
        True, module, role,
        f"{role} is a member of {module.entra_group}, so {module.label} opens "
        f"against the {module.sharepoint_list} list.")


def module_rows(role: str, modules=MODULES) -> list[dict]:
    """Every module, and whether this role reaches it. The whole matrix is
    shown rather than only the permitted half, because a client reviewing the
    design needs to see what is refused as much as what is granted."""
    return [
        {"Module": module.label,
         "Entra ID group": module.entra_group,
         "SharePoint list": module.sharepoint_list,
         "This role": "Open" if role in module.roles else "Denied"}
        for module in modules
    ]


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Person:
    upn: str
    name: str
    role: str
    manager_upn: str = ""


EMPLOYEE = Person("amara.okafor@contoso-pro.com", "Amara Okafor",
                  ROLE_EMPLOYEE, "dele.adeyemi@contoso-pro.com")
MANAGER = Person("dele.adeyemi@contoso-pro.com", "Dele Adeyemi", ROLE_MANAGER)
OTHER_MANAGER = Person("ruth.nwosu@contoso-pro.com", "Ruth Nwosu", ROLE_MANAGER)
PEOPLE: tuple[Person, ...] = (EMPLOYEE, MANAGER, OTHER_MANAGER)


# ---------------------------------------------------------------------------
# PTO
# ---------------------------------------------------------------------------

STATUS_DRAFT = "Draft"
STATUS_PENDING = "Pending approval"
STATUS_APPROVED = "Approved"
STATUS_REJECTED = "Rejected"
STATUS_CANCELLED = "Cancelled"

ERR_NONE = ""
ERR_ZERO_DAYS = "ZERO_DAYS"
ERR_HALF_DAY = "NOT_A_HALF_DAY"
ERR_IN_THE_PAST = "STARTS_IN_THE_PAST"
ERR_NOTICE = "NOT_ENOUGH_NOTICE"
ERR_BALANCE = "EXCEEDS_BALANCE"
ERR_OVERLAP = "OVERLAPS_AN_EXISTING_REQUEST"

# Policy, stated once so the rules and the messages cannot drift apart.
ANNUAL_ALLOWANCE = 25.0
LONG_REQUEST_DAYS = 5.0
LONG_REQUEST_NOTICE_DAYS = 14
MIN_NOTICE_DAYS = 1


@dataclass(frozen=True)
class PtoRequest:
    request_id: str
    person: Person
    start: str
    days: float
    reason: str
    status: str = STATUS_PENDING

    @property
    def end(self) -> str:
        return _iso(_day(self.start) + timedelta(days=max(0, int(self.days) - 1)))

    @property
    def open(self) -> bool:
        return self.status in (STATUS_DRAFT, STATUS_PENDING)


@dataclass(frozen=True)
class PtoBalance:
    allowance: float
    taken: float
    pending: float

    @property
    def available(self) -> float:
        """What a new request may actually draw on.

        Pending days are held back. Without that, two requests raised the same
        morning each pass validation against the same balance and together
        overdraw it, which the client discovers in payroll rather than in the
        app.
        """
        return round(self.allowance - self.taken - self.pending, 2)

    def rows(self) -> list[dict]:
        return [
            {"Line": "Annual allowance", "Days": f"{self.allowance:.1f}"},
            {"Line": "Already taken", "Days": f"{self.taken:.1f}"},
            {"Line": "Held by pending requests", "Days": f"{self.pending:.1f}"},
            {"Line": "Available to request", "Days": f"{self.available:.1f}"},
        ]


@dataclass
class PtoLedger:
    requests: list = field(default_factory=list)
    sequence: int = 0

    def for_person(self, upn: str) -> list:
        return [request for request in self.requests
                if request.person.upn == upn]

    def balance(self, person: Person,
                allowance: float = ANNUAL_ALLOWANCE) -> PtoBalance:
        mine = self.for_person(person.upn)
        taken = sum(r.days for r in mine if r.status == STATUS_APPROVED)
        pending = sum(r.days for r in mine if r.status == STATUS_PENDING)
        return PtoBalance(allowance, round(taken, 2), round(pending, 2))

    def rows(self) -> list[dict]:
        return [
            {"Request": request.request_id, "Person": request.person.name,
             "Starts": request.start, "Days": f"{request.days:.1f}",
             "Status": request.status, "Reason": request.reason}
            for request in self.requests
        ]


@dataclass(frozen=True)
class PtoOutcome:
    accepted: bool
    request: PtoRequest | None
    code: str
    message: str
    balance: PtoBalance | None = None


def request_pto(ledger: PtoLedger, person: Person, start: str, days: float,
                reason: str = "Annual leave", today: str = TODAY,
                allowance: float = ANNUAL_ALLOWANCE) -> PtoOutcome:
    """Validate a PTO request the way the Power App has to, and say why not.

    Every refusal names the rule it broke. A Power App that answers "invalid"
    produces a support ticket; one that answers "you have 3.5 days available
    and asked for 5" produces a corrected request.
    """
    balance = ledger.balance(person, allowance)

    if days <= 0:
        return PtoOutcome(False, None, ERR_ZERO_DAYS,
                          "A request has to be for at least half a day.",
                          balance)
    if (days * 2) % 1 != 0:
        return PtoOutcome(False, None, ERR_HALF_DAY,
                          f"{days} days is not a whole or half day. The "
                          f"timesheet and payroll both round to halves, so "
                          f"anything finer disagrees with one of them.",
                          balance)

    notice = (_day(start) - _day(today)).days
    if notice < 0:
        return PtoOutcome(False, None, ERR_IN_THE_PAST,
                          f"{start} has already passed. Leave taken without a "
                          f"request is a payroll correction rather than a "
                          f"request.", balance)
    if notice < MIN_NOTICE_DAYS:
        return PtoOutcome(False, None, ERR_NOTICE,
                          f"{start} is today. The policy asks for at least "
                          f"{MIN_NOTICE_DAYS} day of notice so cover can be "
                          f"arranged.", balance)
    if days > LONG_REQUEST_DAYS and notice < LONG_REQUEST_NOTICE_DAYS:
        return PtoOutcome(False, None, ERR_NOTICE,
                          f"A request longer than {LONG_REQUEST_DAYS:.0f} days "
                          f"needs {LONG_REQUEST_NOTICE_DAYS} days of notice, "
                          f"and this one gives {notice}.", balance)

    if days > balance.available:
        held = (f", including {balance.pending:.1f} held by a request already "
                f"waiting for approval" if balance.pending else "")
        return PtoOutcome(False, None, ERR_BALANCE,
                          f"{days:.1f} days requested against "
                          f"{balance.available:.1f} available{held}.", balance)

    clash = _overlapping(ledger, person, start, days)
    if clash is not None:
        return PtoOutcome(False, None, ERR_OVERLAP,
                          f"This overlaps {clash.request_id}, which already "
                          f"covers {clash.start} to {clash.end}.", balance)

    ledger.sequence += 1
    request = PtoRequest(f"PTO-{ledger.sequence:04d}", person, start, days,
                         reason, STATUS_PENDING)
    ledger.requests.append(request)
    after = ledger.balance(person, allowance)
    return PtoOutcome(True, request, ERR_NONE,
                      f"{request.request_id} raised for {days:.1f} day(s) from "
                      f"{start}. {after.available:.1f} days remain available "
                      f"while it waits for approval.", after)


def _overlapping(ledger: PtoLedger, person: Person, start: str,
                 days: float) -> PtoRequest | None:
    new_start = _day(start)
    new_end = new_start + timedelta(days=max(0, int(days) - 1))
    for request in ledger.for_person(person.upn):
        if not request.open and request.status != STATUS_APPROVED:
            continue
        existing_start = _day(request.start)
        existing_end = _day(request.end)
        if new_start <= existing_end and existing_start <= new_end:
            return request
    return None


# ---------------------------------------------------------------------------
# Timesheets
# ---------------------------------------------------------------------------

ERR_HOURS_RANGE = "HOURS_OUT_OF_RANGE"
ERR_HOURS_INCREMENT = "NOT_A_QUARTER_HOUR"
ERR_FUTURE = "DATE_IN_THE_FUTURE"
ERR_CLOSED_PROJECT = "PROJECT_CLOSED"
ERR_WEEK_CAP = "WEEK_OVER_CAP"

MAX_DAILY_HOURS = 12.0
MAX_WEEKLY_HOURS = 50.0


@dataclass(frozen=True)
class TimesheetEntry:
    entry_id: str
    person: Person
    project_code: str
    day: str
    hours: float
    note: str


@dataclass(frozen=True)
class TimesheetOutcome:
    accepted: bool
    entry: TimesheetEntry | None
    code: str
    message: str
    week_total: float


@dataclass
class TimesheetLedger:
    entries: list = field(default_factory=list)
    sequence: int = 0

    def week_total(self, upn: str, day: str) -> float:
        monday = _day(day) - timedelta(days=_day(day).weekday())
        sunday = monday + timedelta(days=6)
        return round(sum(
            entry.hours for entry in self.entries
            if entry.person.upn == upn and monday <= _day(entry.day) <= sunday
        ), 2)

    def hours_for_project(self, code: str) -> float:
        return round(sum(entry.hours for entry in self.entries
                         if entry.project_code == code), 2)

    def rows(self) -> list[dict]:
        return [
            {"Entry": entry.entry_id, "Person": entry.person.name,
             "Project": entry.project_code, "Day": entry.day,
             "Hours": f"{entry.hours:.2f}", "Note": entry.note}
            for entry in self.entries
        ]


def log_hours(ledger: TimesheetLedger, person: Person, project_code: str,
              day: str, hours: float, note: str = "", today: str = TODAY,
              projects=None) -> TimesheetOutcome:
    """Validate a timesheet line before it reaches the list."""
    projects = PROJECTS if projects is None else projects
    week_total = ledger.week_total(person.upn, day)

    project = project_by_code(project_code, projects)
    if project is None:
        return TimesheetOutcome(False, None, ERR_CLOSED_PROJECT,
                                f"There is no project coded {project_code!r}.",
                                week_total)
    if not project.open:
        return TimesheetOutcome(False, None, ERR_CLOSED_PROJECT,
                                f"{project.name} is closed. Hours logged to a "
                                f"closed project never reach an invoice.",
                                week_total)
    if hours <= 0 or hours > MAX_DAILY_HOURS:
        return TimesheetOutcome(False, None, ERR_HOURS_RANGE,
                                f"{hours} hours is outside the 0 to "
                                f"{MAX_DAILY_HOURS:.0f} a day the policy "
                                f"allows.", week_total)
    if (hours * 4) % 1 != 0:
        return TimesheetOutcome(False, None, ERR_HOURS_INCREMENT,
                                f"{hours} is not a quarter hour. Billing "
                                f"rounds to fifteen minutes, so anything "
                                f"finer is lost on the invoice.", week_total)
    if _day(day) > _day(today):
        return TimesheetOutcome(False, None, ERR_FUTURE,
                                f"{day} has not happened yet. Hours logged "
                                f"ahead of time are the ones nobody corrects.",
                                week_total)
    if week_total + hours > MAX_WEEKLY_HOURS:
        return TimesheetOutcome(False, None, ERR_WEEK_CAP,
                                f"That would take the week to "
                                f"{week_total + hours:.2f} hours, above the "
                                f"{MAX_WEEKLY_HOURS:.0f} cap. The cap exists "
                                f"so an overrun is a conversation rather than "
                                f"a surprise.", week_total)

    ledger.sequence += 1
    entry = TimesheetEntry(f"TS-{ledger.sequence:04d}", person, project_code,
                           day, hours, note or project.name)
    ledger.entries.append(entry)
    return TimesheetOutcome(True, entry, ERR_NONE,
                            f"{hours:.2f} hours logged to {project.name} on "
                            f"{day}. The week now stands at "
                            f"{week_total + hours:.2f} hours.",
                            round(week_total + hours, 2))


# ---------------------------------------------------------------------------
# Power Automate approval routing
# ---------------------------------------------------------------------------

CONNECTOR_SHAREPOINT = "SharePoint"
CONNECTOR_APPROVALS = "Approvals"
CONNECTOR_OUTLOOK = "Office 365 Outlook"
CONNECTOR_TEAMS = "Microsoft Teams"

ROUTE_DENIED_SELF = "A request cannot be approved by the person who raised it"
ROUTE_DENIED_ROLE = "Only a manager may approve"
ROUTE_DENIED_CLOSED = "That request is no longer waiting for a decision"


@dataclass(frozen=True)
class FlowStep:
    sequence: int
    at: str
    connector: str
    action: str
    detail: str


@dataclass(frozen=True)
class RoutingResult:
    ok: bool
    request: PtoRequest | None
    steps: list
    message: str
    balance: PtoBalance | None = None

    def rows(self) -> list[dict]:
        return [
            {"#": step.sequence, "At": step.at, "Connector": step.connector,
             "Action": step.action, "Detail": step.detail}
            for step in self.steps
        ]


STEP_MINUTES = 4


def submit_to_flow(request: PtoRequest, at: str = "2026-09-14T09:00:00") -> list:
    """The steps Power Automate runs the moment the item is created."""
    moment = _moment(at)
    return [
        FlowStep(1, _stamp(moment, 0), CONNECTOR_SHAREPOINT,
                 "When an item is created",
                 f"{request.request_id} created in PTO Requests by "
                 f"{request.person.name}."),
        FlowStep(2, _stamp(moment, 1), CONNECTOR_SHAREPOINT, "Get manager",
                 f"Manager resolved from the profile: "
                 f"{request.person.manager_upn}."),
        FlowStep(3, _stamp(moment, 2), CONNECTOR_APPROVALS,
                 "Start and wait for an approval",
                 f"Approve or reject sent to {request.person.manager_upn} for "
                 f"{request.days:.1f} day(s) from {request.start}."),
        FlowStep(4, _stamp(moment, 3), CONNECTOR_TEAMS, "Post a card",
                 "Adaptive card posted to the manager's Approvals chat."),
    ]


def decide(ledger: PtoLedger, request: PtoRequest, approver: Person,
           approve: bool, at: str = "2026-09-14T09:00:00",
           allowance: float = ANNUAL_ALLOWANCE) -> RoutingResult:
    """Apply a manager's decision, with the separation of duties enforced.

    The refusals here are the ones that matter in an audit. A person approving
    their own leave is the finding that reaches the board, and a Power App that
    only hides the approve button still accepts the request the flow posts.
    """
    steps = submit_to_flow(request, at)
    moment = _moment(at)

    if approver.role != ROLE_MANAGER:
        return RoutingResult(False, request, steps,
                             f"{ROUTE_DENIED_ROLE}. {approver.name} holds "
                             f"{approver.role}, so the Approvals action refuses "
                             f"the response even though the flow delivered it.")
    if approver.upn == request.person.upn:
        return RoutingResult(False, request, steps,
                             f"{ROUTE_DENIED_SELF}. {approver.name} raised "
                             f"{request.request_id}, and approving it would be "
                             f"the audit finding that reaches the board.")
    if request.status != STATUS_PENDING:
        return RoutingResult(False, request, steps,
                             f"{ROUTE_DENIED_CLOSED}. {request.request_id} is "
                             f"already {request.status}, and a second decision "
                             f"would overwrite the first silently.")

    status = STATUS_APPROVED if approve else STATUS_REJECTED
    decided = PtoRequest(request.request_id, request.person, request.start,
                         request.days, request.reason, status)
    ledger.requests = [decided if r.request_id == request.request_id else r
                       for r in ledger.requests]

    steps = steps + [
        FlowStep(5, _stamp(moment, 4), CONNECTOR_APPROVALS, "Response received",
                 f"{approver.name} chose {status.lower()}."),
        FlowStep(6, _stamp(moment, 5), CONNECTOR_SHAREPOINT, "Update item",
                 f"{request.request_id} set to {status}."),
        FlowStep(7, _stamp(moment, 6), CONNECTOR_OUTLOOK, "Send an email",
                 f"{request.person.name} told the request was "
                 f"{status.lower()}."),
    ]
    balance = ledger.balance(request.person, allowance)
    if approve:
        message = (f"{request.request_id} approved by {approver.name}. The "
                   f"{request.days:.1f} day(s) move from pending to taken, and "
                   f"{balance.available:.1f} days remain.")
    else:
        message = (f"{request.request_id} rejected by {approver.name}. The "
                   f"{request.days:.1f} day(s) return to the balance, which is "
                   f"back to {balance.available:.1f}.")
    return RoutingResult(True, decided, steps, message, balance)


# ---------------------------------------------------------------------------
# Projects and the insight module
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Project:
    code: str
    name: str
    client: str
    budget_hours: float
    hours_billed: float
    rate: float
    start: str
    due: str
    open: bool = True

    def burn(self) -> float:
        return round(self.hours_billed / self.budget_hours, 4) \
            if self.budget_hours else 0.0

    def elapsed(self, today: str = TODAY) -> float:
        total = (_day(self.due) - _day(self.start)).days
        if total <= 0:
            return 1.0
        gone = (_day(today) - _day(self.start)).days
        return round(min(max(gone / total, 0.0), 2.0), 4)

    def days_left(self, today: str = TODAY) -> int:
        return (_day(self.due) - _day(today)).days

    def variance(self, today: str = TODAY) -> float:
        """Budget spent minus schedule elapsed. Positive means the money is
        going faster than the calendar, which is the number that matters."""
        return round(self.burn() - self.elapsed(today), 4)

    def value_billed(self) -> float:
        return round(self.hours_billed * self.rate, 2)


PROJECTS: tuple[Project, ...] = (
    Project("PRJ-118", "Intranet rebuild", "Ridgeway Group", 480, 401, 95,
            "2026-07-06", "2026-10-02"),
    Project("PRJ-121", "Finance dashboard", "Ridgeway Group", 160, 62, 110,
            "2026-08-17", "2026-11-13"),
    Project("PRJ-124", "Field app rollout", "Halden Utilities", 320, 318, 105,
            "2026-06-15", "2026-09-25"),
    Project("PRJ-127", "Compliance portal", "Bevan Legal", 240, 44, 120,
            "2026-09-07", "2027-01-15"),
    Project("PRJ-109", "Migration wave one", "Halden Utilities", 200, 196, 105,
            "2026-04-06", "2026-07-31", open=False),
)

RISK_OVER_BUDGET = "Over budget"
RISK_BURNING_FAST = "Burning faster than the calendar"
RISK_OVERDUE = "Past its due date"
RISK_NONE = "On track"

VARIANCE_TOLERANCE = 0.10
# A project a week old has no trend. Below this much of the schedule the burn
# ratio swings on a single day's work, so flagging it produces a report that
# cries wolf on every new project and stops being read.
MIN_ELAPSED_FOR_TREND = 0.15


def project_by_code(code: str, projects=PROJECTS) -> Project | None:
    for project in projects:
        if project.code == code:
            return project
    return None


@dataclass(frozen=True)
class ProjectRisk:
    project: Project
    level: str
    headline: str
    detail: str

    @property
    def at_risk(self) -> bool:
        return self.level != RISK_NONE


def assess(project: Project, today: str = TODAY) -> ProjectRisk:
    burn, elapsed = project.burn(), project.elapsed(today)
    variance = project.variance(today)
    days_left = project.days_left(today)

    if burn > 1.0:
        return ProjectRisk(
            project, RISK_OVER_BUDGET,
            f"{project.code} has billed {burn:.0%} of its budget.",
            f"{project.hours_billed:.0f} of {project.budget_hours:.0f} hours "
            f"with {days_left} day(s) left. Anything further is unbilled "
            f"unless the scope is reopened with the client.")
    if days_left < 0:
        return ProjectRisk(
            project, RISK_OVERDUE,
            f"{project.code} passed its due date {abs(days_left)} day(s) ago.",
            f"Budget is at {burn:.0%}. A project past its date with budget "
            f"left is usually waiting on the client rather than on the team.")
    if elapsed < MIN_ELAPSED_FOR_TREND:
        return ProjectRisk(
            project, RISK_NONE,
            f"{project.code} is too new to read.",
            f"{elapsed:.0%} of the schedule has passed, so the burn ratio "
            f"swings on a single day's work. It is at {burn:.0%} of budget "
            f"with {days_left} day(s) left, and it will be worth reading once "
            f"{MIN_ELAPSED_FOR_TREND:.0%} of the calendar has gone.")
    if variance > VARIANCE_TOLERANCE:
        return ProjectRisk(
            project, RISK_BURNING_FAST,
            f"{project.code} is {variance:.0%} ahead of its schedule on spend.",
            f"{burn:.0%} of the budget against {elapsed:.0%} of the calendar. "
            f"At this rate it runs out around "
            f"{_projected_exhaustion(project, today)}.")
    return ProjectRisk(
        project, RISK_NONE,
        f"{project.code} is tracking to plan.",
        f"{burn:.0%} of budget against {elapsed:.0%} of the calendar, "
        f"{days_left} day(s) left.")


def _projected_exhaustion(project: Project, today: str) -> str:
    """When the budget runs out at the rate it has been spent so far."""
    gone = max((_day(today) - _day(project.start)).days, 1)
    per_day = project.hours_billed / gone
    if per_day <= 0:
        return "no measurable date, because nothing has been billed"
    remaining = max(project.budget_hours - project.hours_billed, 0)
    return _iso(_day(today) + timedelta(days=int(remaining / per_day)))


def project_rows(projects=PROJECTS, today: str = TODAY) -> list[dict]:
    return [
        {"Project": project.code, "Name": project.name,
         "Client": project.client,
         "Budget": f"{project.budget_hours:.0f}h",
         "Billed": f"{project.hours_billed:.0f}h",
         "Burn": f"{project.burn():.0%}",
         "Schedule": f"{project.elapsed(today):.0%}",
         "Due": project.due,
         "Status": assess(project, today).level}
        for project in projects if project.open
    ]


@dataclass(frozen=True)
class Insight:
    headline: str
    summary: str
    findings: list
    actions: list
    hours_billed: float
    value_billed: float
    at_risk: int


def generate_insight(projects=PROJECTS, today: str = TODAY,
                     capacity_hours: float = 1400.0) -> Insight:
    """The weekly executive summary, computed from the same figures on screen.

    Written as a rule rather than generated, deliberately. A real deployment
    hands these numbers to Claude through the API for the wording; doing the
    arithmetic here means the summary cannot describe a project the table does
    not show, which is the failure that matters when somebody forwards it to a
    client.
    """
    live = [project for project in projects if project.open]
    if not live:
        return Insight("No live projects", "Nothing is open, so there is "
                       "nothing to report.", [], [], 0.0, 0.0, 0)

    risks = [assess(project, today) for project in live]
    at_risk = [risk for risk in risks if risk.at_risk]
    hours = round(sum(project.hours_billed for project in live), 2)
    value = round(sum(project.value_billed() for project in live), 2)
    utilisation = round(hours / capacity_hours, 4) if capacity_hours else 0.0

    worst = max(risks, key=lambda risk: risk.project.variance(today))
    soonest = min(live, key=lambda project: project.days_left(today))

    if at_risk:
        # The subject is the count, so a single project needs rather than need.
        # An executive summary that cannot conjugate gets read as a machine
        # wrote it, which is the one thing this module cannot afford.
        verb = "needs" if len(at_risk) == 1 else "need"
        headline = (f"{len(at_risk)} of {len(live)} live projects {verb} a "
                    f"decision this week.")
    else:
        headline = f"All {len(live)} live projects are tracking to plan."

    summary = (
        f"{hours:,.0f} hours are billed across {len(live)} live projects, "
        f"worth {value:,.0f} at current rates, which is "
        f"{utilisation:.0%} of the {capacity_hours:,.0f} hour capacity for the "
        f"period. {headline} The nearest deadline is {soonest.code} on "
        f"{soonest.due}, {soonest.days_left(today)} day(s) away at "
        f"{soonest.burn():.0%} of budget.")

    findings = [f"{risk.headline} {risk.detail}" for risk in risks]

    actions = []
    for risk in at_risk:
        if risk.level == RISK_OVER_BUDGET:
            actions.append(
                f"{risk.project.code}: stop unbilled work and put a change "
                f"request to {risk.project.client} before the next sprint.")
        elif risk.level == RISK_OVERDUE:
            actions.append(
                f"{risk.project.code}: agree a new date with "
                f"{risk.project.client} or close it out, because an open "
                f"project past its date holds a slot nobody can plan against.")
        else:
            actions.append(
                f"{risk.project.code}: review scope with "
                f"{risk.project.client} this week, while there is still budget "
                f"left to negotiate with.")
    if not actions:
        actions.append(
            "Nothing needs a decision. Keep the weekly check and spend the "
            "time on the pipeline instead.")

    return Insight(headline=headline, summary=summary, findings=findings,
                   actions=actions, hours_billed=hours, value_billed=value,
                   at_risk=len(at_risk))


def insight_prompt(projects=PROJECTS, today: str = TODAY) -> str:
    """The message a real deployment would send to the Claude API.

    Shown on screen so the client can see exactly what would leave their
    tenant, which is the first question a security reviewer asks about an AI
    feature. Names and client identifiers are the only fields sent, and no
    document content is.
    """
    lines = [
        "You are writing the weekly project summary for a professional "
        "services team. Use only the figures below. State the risk plainly and "
        "name the project.",
        "",
        f"Reporting date: {today}",
        "",
    ]
    for project in projects:
        if not project.open:
            continue
        lines.append(
            f"- {project.code} {project.name} for {project.client}: "
            f"{project.hours_billed:.0f} of {project.budget_hours:.0f} hours "
            f"billed, due {project.due}, {project.days_left(today)} days left.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def _day(value: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return date.fromisoformat(TODAY)


def _iso(value: date) -> str:
    return value.isoformat()


def _moment(value: str) -> str:
    return str(value)


def _stamp(at: str, index: int) -> str:
    """A flow step timestamp, minutes after the trigger."""
    text = str(at)
    try:
        hours, minutes = int(text[11:13]), int(text[14:16])
    except (ValueError, IndexError):
        hours, minutes = 9, 0
    total = hours * 60 + minutes + index * STEP_MINUTES
    return f"{text[:10]}T{total // 60 % 24:02d}:{total % 60:02d}:00"
