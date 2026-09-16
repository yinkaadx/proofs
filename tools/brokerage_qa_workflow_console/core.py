"""US real estate brokerage QA and workflow engine.

Three kinds of defect cost a brokerage real money, and none of them looks like
a bug on screen. A commission split that reconciles to a penny short. A
transaction approved by the person who wrote it. And a listing that still shows
Active on the MLS after it went under contract.

Pure logic, no Streamlit import, so this is unit testable on its own.
Deterministic: nothing reads the clock or a random source, and money is held as
integer cents throughout, because half of an odd commission is exactly where a
float loses one.

THE PERMISSION HOLE THIS ENGINE IS BUILT AROUND

A role matrix answers "may a Team Lead approve a transaction" and the answer is
yes. It cannot answer the question that matters, which is whether this Team
Lead may approve THIS transaction, and the answer is no when they are the
listing agent on it. Broker supervision exists so that somebody other than the
person who wrote the deal has looked at it, and a role check alone cannot see
that, because the role is correct and the actor is wrong.

So permissions here are evaluated in two passes. The role decides what the
module allows, and a second check compares the actor against the subject. A
matrix that passes the first and skips the second is the most common way
separation of duties is lost in a brokerage system, and it passes every test
written against the matrix.

NOTHING HERE IS LEGAL ADVICE

Supervision requirements, MLS status change deadlines and retention periods all
differ by state and by MLS, so every threshold is a parameter with a stated
default rather than a rule this tool asserts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

ENGINE_VERSION = "1.0.0"

DISCLAIMER = (
    "An educational simulator. Supervision requirements, MLS status deadlines "
    "and retention periods differ by state and by MLS, so every threshold "
    "here is a parameter with a stated default rather than a rule this tool "
    "asserts. Confirm yours against your state licensing authority and your "
    "MLS rules."
)

# ---------------------------------------------------------------------------
# Roles and modules
# ---------------------------------------------------------------------------

ROLE_AGENT = "Agent"
ROLE_TEAM_LEAD = "Team Lead"
ROLE_BROKER = "Managing Broker"

ROLES: tuple = (ROLE_AGENT, ROLE_TEAM_LEAD, ROLE_BROKER)
ROLE_LEVEL = {name: index for index, name in enumerate(ROLES)}

MOD_LEADS = "Leads"
MOD_MLS = "MLS Sync"
MOD_TRANSACTIONS = "Transactions"
MOD_BROKER_REVIEW = "Broker Review"
MOD_COMMISSIONS = "Commission Splits"

MODULES: tuple = (MOD_LEADS, MOD_MLS, MOD_TRANSACTIONS, MOD_BROKER_REVIEW,
                  MOD_COMMISSIONS)

ACTIONS: tuple = ("view", "create", "edit", "approve", "export", "override")


# ---------------------------------------------------------------------------
# Part one: workflow checkpoints
# ---------------------------------------------------------------------------

SEV_BLOCKER = "S1 Blocker"
SEV_CRITICAL = "S2 Critical"
SEV_MAJOR = "S3 Major"
SEV_MINOR = "S4 Minor"

SEVERITIES: tuple = (SEV_BLOCKER, SEV_CRITICAL, SEV_MAJOR, SEV_MINOR)
SEVERITY_RANK = {name: index + 1 for index, name in enumerate(SEVERITIES)}
SEVERITY_TONE = {SEV_BLOCKER: "crit", SEV_CRITICAL: "crit",
                 SEV_MAJOR: "warn", SEV_MINOR: "ok"}

STATUS_PASS = "Pass"
STATUS_FAIL = "Fail"
STATUS_BLOCKED = "Blocked"

# A default MLS status change window. Stated as a default because it differs
# by MLS and is a rule the MLS makes, not one this tool knows.
DEFAULT_MLS_WINDOW_HOURS = 48


def severity_rank(severity: str) -> int:
    return SEVERITY_RANK.get(severity, len(SEVERITIES) + 1)


def severity_tone(severity: str) -> str:
    return SEVERITY_TONE.get(severity, "warn")


@dataclass(frozen=True)
class Checkpoint:
    check_id: str
    module: str
    title: str
    severity: str
    status: str
    expected: str
    why_it_matters: str

    @property
    def rank(self) -> int:
        return severity_rank(self.severity)

    @property
    def passed(self) -> bool:
        return self.status == STATUS_PASS

    @property
    def tone(self) -> str:
        if self.status == STATUS_BLOCKED:
            return "warn"
        return "ok" if self.passed else severity_tone(self.severity)


def get_brokerage_workflows() -> list:
    """Every checkpoint, ranked by what it costs when it fails.

    Each one states the expected result as a rule or a figure rather than as
    "works correctly", because a workflow check judged by eye is not a check.
    """
    checks = [
        Checkpoint(
            "BR-101", MOD_BROKER_REVIEW,
            "An agent cannot approve their own transaction",
            SEV_BLOCKER, STATUS_FAIL,
            "Approval is refused when the approver is the listing or buying "
            "agent on the file, whatever their role says.",
            "Broker supervision exists so somebody other than the person who "
            "wrote the deal has looked at it. A role check alone passes here "
            "because the role is correct and the actor is wrong."),
        Checkpoint(
            "BR-102", MOD_BROKER_REVIEW,
            "A Team Lead cannot approve a file they are an agent on",
            SEV_BLOCKER, STATUS_FAIL,
            "Same refusal as BR-101. Holding a second role does not make a "
            "person a second person.",
            "This is the same violation wearing a different hat, and it is "
            "the one that survives review because the matrix says Team Leads "
            "may approve."),
        Checkpoint(
            "BR-201", MOD_COMMISSIONS,
            "Split legs reconcile exactly to the gross commission",
            SEV_BLOCKER, STATUS_PASS,
            "Every leg in integer cents, summing to the gross to the cent, "
            "with no rounding remainder left unassigned.",
            "A split that reconciles a penny short is not a display problem. "
            "It is a ledger that will not close and a payout somebody has to "
            "explain."),
        Checkpoint(
            "BR-202", MOD_COMMISSIONS,
            "A cap reached mid transaction changes the split correctly",
            SEV_CRITICAL, STATUS_FAIL,
            "Commission above the remaining cap is split at the post cap "
            "rate, and the two portions still sum to the gross.",
            "The cap boundary falls inside a single transaction, so the file "
            "has two rates in it. Applying one rate to the whole amount is "
            "wrong in the agent's favour or the brokerage's, and nobody "
            "notices until year end."),
        Checkpoint(
            "BR-301", MOD_MLS,
            "A status change reaches the MLS inside the configured window",
            SEV_CRITICAL, STATUS_BLOCKED,
            f"Pending is published within {DEFAULT_MLS_WINDOW_HOURS} hours of "
            f"the contract date, or the file is flagged.",
            "A listing showing Active after it is under contract draws "
            "showings that cannot happen and is the kind of thing an MLS "
            "fines for. The window is MLS specific, so it is configurable."),
        Checkpoint(
            "BR-302", MOD_MLS,
            "An agent cannot edit a listing they do not hold",
            SEV_CRITICAL, STATUS_PASS,
            "Edit is refused unless the agent is the listing agent or holds a "
            "supervisory role.",
            "Listing data is the brokerage's regulated record, and an edit by "
            "the wrong agent is both a data problem and a licensing one."),
        Checkpoint(
            "BR-401", MOD_TRANSACTIONS,
            "Required documents are present before a file can close",
            SEV_CRITICAL, STATUS_FAIL,
            "Closing is refused while any required document is missing, and "
            "the refusal names which ones.",
            "A file that closes without its documents is a compliance gap "
            "discovered at audit, long after the commission was paid."),
        Checkpoint(
            "BR-402", MOD_TRANSACTIONS,
            "A cancelled file releases its escrow and its MLS status together",
            SEV_MAJOR, STATUS_PASS,
            "Cancelling sets the MLS status back and releases escrow in one "
            "transaction, or neither happens.",
            "Half a cancellation leaves money held against a listing that is "
            "back on market."),
        Checkpoint(
            "BR-501", MOD_LEADS,
            "A duplicate lead attaches to the existing record",
            SEV_MAJOR, STATUS_PASS,
            "A second enquiry from the same email or phone attaches to the "
            "existing lead rather than creating a second one.",
            "Two agents working the same lead is how a brokerage loses both "
            "the lead and one of the agents."),
        Checkpoint(
            "BR-502", MOD_LEADS,
            "Round robin assignment survives an agent being deactivated",
            SEV_MINOR, STATUS_PASS,
            "Deactivating an agent removes them from the rotation without "
            "dropping the lead that was next in line for them.",
            "The dropped lead is silent. Nobody reports a lead they never "
            "knew arrived."),
    ]
    return sorted(checks, key=lambda c: (c.rank, c.check_id))


def workflow_rows() -> list:
    return [
        {"Check": c.check_id, "Module": c.module, "Title": c.title,
         "Severity": c.severity, "Status": c.status}
        for c in get_brokerage_workflows()
    ]


def workflow_summary() -> dict:
    checks = get_brokerage_workflows()
    executed = [c for c in checks if c.status != STATUS_BLOCKED]
    passed = [c for c in checks if c.passed]
    return {
        "total": len(checks),
        "passed": len(passed),
        "failed": len([c for c in checks if c.status == STATUS_FAIL]),
        "blocked": len([c for c in checks if c.status == STATUS_BLOCKED]),
        "executed": len(executed),
        # Of everything planned, not of what ran. A blocked check is an
        # unknown, and counting it out of the denominator is how a release
        # is signed off on evidence nobody gathered.
        "verified_rate": round(len(passed) / len(checks), 4) if checks else 0.0,
        "blockers_open": len([c for c in checks
                              if c.severity == SEV_BLOCKER and not c.passed]),
    }


# ---------------------------------------------------------------------------
# Part two: permissions, in two passes
# ---------------------------------------------------------------------------

# What each role may do in each module, before the actor is considered.
ROLE_MATRIX = {
    ROLE_AGENT: {
        MOD_LEADS: ("view", "create", "edit"),
        MOD_MLS: ("view", "create", "edit"),
        MOD_TRANSACTIONS: ("view", "create", "edit"),
        MOD_BROKER_REVIEW: ("view",),
        MOD_COMMISSIONS: ("view",),
    },
    ROLE_TEAM_LEAD: {
        MOD_LEADS: ("view", "create", "edit", "export"),
        MOD_MLS: ("view", "create", "edit"),
        MOD_TRANSACTIONS: ("view", "create", "edit"),
        MOD_BROKER_REVIEW: ("view", "approve"),
        MOD_COMMISSIONS: ("view", "export"),
    },
    ROLE_BROKER: {
        MOD_LEADS: ("view", "create", "edit", "export"),
        MOD_MLS: ("view", "create", "edit", "override"),
        MOD_TRANSACTIONS: ("view", "create", "edit", "approve", "override"),
        MOD_BROKER_REVIEW: ("view", "approve", "override", "export"),
        MOD_COMMISSIONS: ("view", "edit", "approve", "export", "override"),
    },
}

# Actions that are a supervisory judgement on somebody else's work. These are
# the ones the second pass applies to; editing your own lead is fine.
SUPERVISORY_ACTIONS: tuple = ("approve", "override")


@dataclass(frozen=True)
class PermissionResult:
    role: str
    module: str
    allowed: tuple
    restricted: tuple
    actor_id: str = ""
    subject_agent_id: str = ""
    self_action_blocked: tuple = ()
    note: str = ""

    @property
    def self_review(self) -> bool:
        return bool(self.actor_id and self.actor_id == self.subject_agent_id)

    @property
    def effective(self) -> tuple:
        """What the actor may actually do on this specific record."""
        return tuple(a for a in self.allowed
                     if a not in self.self_action_blocked)

    def permits(self, action: str) -> bool:
        return action in self.effective

    def rows(self) -> list:
        return [
            {"Action": action,
             "Role allows": "yes" if action in self.allowed else "no",
             "On this record": "yes" if action in self.effective else "no",
             "Reason": ("blocked, self review" if action in
                        self.self_action_blocked
                        else "allowed" if action in self.effective
                        else "not granted to this role")}
            for action in ACTIONS
        ]


def evaluate_role_permissions(user_role: str = ROLE_TEAM_LEAD,
                              target_module: str = MOD_BROKER_REVIEW,
                              actor_id: str = "",
                              subject_agent_id: str = "") -> PermissionResult:
    """Two passes. The role decides the module, the actor decides the record.

    Passing only a role and a module gives the matrix answer, which is what
    most systems check and is not sufficient. Passing the actor and the agent
    on the file applies separation of duties, which is the check that catches
    a Team Lead approving their own deal.
    """
    if user_role not in ROLES:
        raise ValueError(f"unknown role {user_role!r}. Defined: {list(ROLES)}.")
    if target_module not in MODULES:
        raise ValueError(f"unknown module {target_module!r}. "
                         f"Defined: {list(MODULES)}.")

    allowed = ROLE_MATRIX[user_role][target_module]
    restricted = tuple(a for a in ACTIONS if a not in allowed)

    blocked: tuple = ()
    note = (f"{user_role} may {', '.join(allowed) or 'do nothing'} in "
            f"{target_module}.")

    if actor_id and subject_agent_id and actor_id == subject_agent_id:
        blocked = tuple(a for a in allowed if a in SUPERVISORY_ACTIONS)
        if blocked:
            note = (
                f"{user_role} normally may {', '.join(allowed)} in "
                f"{target_module}, but {actor_id} is the agent on this file. "
                f"{', '.join(blocked)} is refused, because supervision means "
                f"somebody other than the person who wrote the deal has "
                f"looked at it, and holding a second role does not make a "
                f"person a second person.")
        else:
            note = (f"{user_role} is the agent on this file, and none of "
                    f"their {target_module} actions are supervisory, so "
                    f"nothing is withheld.")

    return PermissionResult(user_role, target_module, allowed, restricted,
                            actor_id, subject_agent_id, blocked, note)


def permission_matrix(module: str = MOD_BROKER_REVIEW) -> list:
    """The matrix answer for one module, which is the incomplete picture."""
    rows = []
    for role in ROLES:
        result = evaluate_role_permissions(role, module)
        rows.append({
            "Role": role,
            "Allowed": ", ".join(result.allowed) or "none",
            "Restricted": ", ".join(result.restricted) or "none",
        })
    return rows


def separation_of_duties_report(actor_id: str = "AG-4471") -> list:
    """The same actor across every module, with and without self review."""
    rows = []
    for role in ROLES:
        for module in MODULES:
            other = evaluate_role_permissions(role, module, actor_id, "AG-9002")
            own = evaluate_role_permissions(role, module, actor_id, actor_id)
            rows.append({
                "Role": role,
                "Module": module,
                "On another agent's file": ", ".join(other.effective) or "none",
                "On their own file": ", ".join(own.effective) or "none",
                "Withheld": ", ".join(own.self_action_blocked) or "none",
            })
    return rows


# ---------------------------------------------------------------------------
# Part three: commission splits, in integer cents
# ---------------------------------------------------------------------------

def to_cents(amount) -> int:
    """Money as an integer number of cents, rounded half up once."""
    return int(Decimal(str(amount)).quantize(Decimal("0.01"),
                                             rounding=ROUND_HALF_UP) * 100)


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


@dataclass(frozen=True)
class SplitLeg:
    name: str
    basis_points: int
    cents: int

    @property
    def percent(self) -> float:
        return self.basis_points / 100.0


@dataclass
class CommissionSplit:
    gross_cents: int
    legs: list = field(default_factory=list)
    remainder_assigned_to: str = ""

    @property
    def total_cents(self) -> int:
        return sum(leg.cents for leg in self.legs)

    @property
    def reconciles(self) -> bool:
        """The only test that matters. To the cent, not to the dollar."""
        return self.total_cents == self.gross_cents

    @property
    def tone(self) -> str:
        return "ok" if self.reconciles else "crit"

    def rows(self) -> list:
        return [
            {"Leg": leg.name, "Share": f"{leg.percent:.2f}%",
             "Amount": money(leg.cents)}
            for leg in self.legs
        ] + [
            {"Leg": "Total", "Share": "100.00%", "Amount": money(self.total_cents)},
            {"Leg": "Gross commission", "Share": "",
             "Amount": money(self.gross_cents)},
        ]


def split_commission(gross, shares, remainder_to: str = "") -> CommissionSplit:
    """Split a gross commission so the legs sum to it exactly.

    Each leg is floored and the leftover cents are handed to one named leg by
    largest remainder, rather than every leg being rounded independently and
    the total landing a penny out. A penny that does not reconcile is not a
    display problem, it is a ledger that will not close.
    """
    gross_cents = to_cents(gross)
    if not shares:
        raise ValueError("a split needs at least one leg")
    total_bps = sum(int(bps) for _, bps in shares)
    if total_bps != 10_000:
        raise ValueError(
            f"the legs sum to {total_bps} basis points, not 10000. A split "
            f"that does not add to a hundred percent cannot reconcile, so it "
            f"is refused rather than balanced by the rounding.")

    legs = []
    running = 0
    remainders = []
    for name, bps in shares:
        exact = gross_cents * int(bps)
        cents = exact // 10_000
        remainders.append((exact % 10_000, name))
        legs.append(SplitLeg(name, int(bps), cents))
        running += cents

    leftover = gross_cents - running
    target = remainder_to
    if leftover:
        if not target:
            # Largest fractional remainder takes the spare cents, which is the
            # allocation that keeps every leg closest to its exact share.
            target = max(remainders)[1]
        legs = [SplitLeg(leg.name, leg.basis_points,
                         leg.cents + leftover if leg.name == target
                         else leg.cents)
                for leg in legs]

    return CommissionSplit(gross_cents, legs, target if leftover else "")


def split_with_cap(gross, pre_cap_agent_bps: int, post_cap_agent_bps: int,
                   cap_remaining) -> CommissionSplit:
    """Apply the cap boundary inside a single transaction.

    The boundary falls mid file, so the file has two rates in it. Applying one
    rate to the whole amount is wrong in somebody's favour and nobody notices
    until year end.
    """
    gross_cents = to_cents(gross)
    cap_cents = max(to_cents(cap_remaining), 0)
    below = min(gross_cents, cap_cents)
    above = gross_cents - below

    agent = (below * int(pre_cap_agent_bps)) // 10_000
    agent += (above * int(post_cap_agent_bps)) // 10_000
    brokerage = gross_cents - agent

    legs = [SplitLeg("Agent", pre_cap_agent_bps, agent),
            SplitLeg("Brokerage", 10_000 - pre_cap_agent_bps, brokerage)]
    return CommissionSplit(gross_cents, legs, "Brokerage")


# ---------------------------------------------------------------------------
# Part four: the Marker.io defect report
# ---------------------------------------------------------------------------

@dataclass
class BugReport:
    title: str
    screen: str
    role: str
    severity: str
    preconditions: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    expected: str = ""
    actual: str = ""
    workflow_impact: str = ""
    environment: dict = field(default_factory=dict)

    @property
    def tone(self) -> str:
        return severity_tone(self.severity)

    def rows(self) -> list:
        return [
            {"Field": "Title", "Value": self.title},
            {"Field": "Screen and role",
             "Value": f"{self.screen} as {self.role}"},
            {"Field": "Severity", "Value": self.severity},
            {"Field": "Expected", "Value": self.expected},
            {"Field": "Actual", "Value": self.actual},
        ]

    def as_markdown(self) -> str:
        pre = "\n".join(f"- {item}" for item in self.preconditions)
        steps = "\n".join(f"{i}. {s}"
                          for i, s in enumerate(self.steps, start=1))
        env = "\n".join(f"- {k}: {v}" for k, v in self.environment.items())
        return (
            f"# {self.title}\n\n"
            f"**Screen and role** {self.screen} as {self.role}  \n"
            f"**Severity** {self.severity}\n\n"
            f"## Preconditions\n{pre}\n\n"
            f"## Steps to reproduce\n{steps}\n\n"
            f"## Expected\n{self.expected}\n\n"
            f"## Actual\n{self.actual}\n\n"
            f"## Broker workflow impact\n{self.workflow_impact}\n\n"
            f"## Environment\n{env}\n")


def generate_marker_io_bug_report(check_id: str = "BR-102") -> BugReport:
    """Build a report a developer can act on without asking a question.

    Marker.io captures the screen and the session automatically. What it
    cannot capture is which role was signed in and what the brokerage
    consequence is, so both are stated explicitly rather than left to the
    screenshot.
    """
    check = next((c for c in get_brokerage_workflows()
                  if c.check_id == check_id), None)
    if check is None:
        raise ValueError(f"no checkpoint {check_id!r}. Defined: "
                         f"{[c.check_id for c in get_brokerage_workflows()]}.")

    reports = {
        "BR-102": BugReport(
            title=("Team Lead can approve a transaction on which they are the "
                   "listing agent"),
            screen="Broker Review, transaction detail",
            role=ROLE_TEAM_LEAD,
            severity=SEV_BLOCKER,
            preconditions=[
                "User AG-4471 holds the Team Lead role.",
                "Transaction TX-88213 lists AG-4471 as the listing agent.",
                "The file is in Pending Broker Review.",
            ],
            steps=[
                "Sign in as AG-4471.",
                "Open Broker Review and select TX-88213.",
                "Press Approve.",
                "Reopen the file and read the approval record.",
            ],
            expected=("Approve is refused with a message naming AG-4471 as "
                      "the agent on the file. The role permits approval and "
                      "this record does not, because supervision means "
                      "somebody other than the author has looked at it."),
            actual=("The file is approved and the approval record names "
                    "AG-4471 as both the listing agent and the approver. No "
                    "warning is shown and the audit log records it as a "
                    "normal approval."),
            workflow_impact=(
                "Every file a Team Lead writes can be self approved, so broker "
                "supervision is absent on exactly the files where a lead has "
                "the most at stake. It passes every test written against the "
                "role matrix, because the role is correct and the actor is "
                "wrong."),
            environment={"Build": "brokerage-web 4.2.1",
                         "Browser": "Chrome 141 on macOS 15",
                         "Tenant": "demo-brokerage",
                         "Captured with": "Marker.io"}),
        "BR-202": BugReport(
            title="Commission above the annual cap is split at the pre cap rate",
            screen="Commission Splits, transaction detail",
            role=ROLE_BROKER,
            severity=SEV_CRITICAL,
            preconditions=[
                "Agent AG-4471 has $1,200.00 of cap remaining.",
                "Transaction TX-88213 has a gross commission of $9,000.00.",
                "Pre cap split is 70 percent agent, post cap is 100 percent.",
            ],
            steps=[
                "Open TX-88213 in Commission Splits.",
                "Read the agent and brokerage legs.",
                "Compare against the cap remaining on the agent record.",
            ],
            expected=("The first $1,200.00 splits at 70 percent and the "
                      "remaining $7,800.00 at 100 percent, so the agent "
                      "receives $8,640.00 and the brokerage $360.00, summing "
                      "exactly to $9,000.00."),
            actual=("The whole $9,000.00 is split at 70 percent, so the agent "
                    "receives $6,300.00 and the brokerage $2,700.00. The "
                    "totals reconcile, which is why nothing is flagged."),
            workflow_impact=(
                "The cap boundary falls inside a single file, so the file has "
                "two rates in it. Applying one rate is wrong by $2,340.00 on "
                "this transaction alone, in the brokerage's favour here and "
                "in the agent's favour whenever the rates are the other way "
                "round. It surfaces at year end reconciliation, long after "
                "the payout."),
            environment={"Build": "brokerage-web 4.2.1",
                         "Browser": "Chrome 141 on macOS 15",
                         "Tenant": "demo-brokerage",
                         "Captured with": "Marker.io"}),
    }

    report = reports.get(check_id)
    if report is not None:
        return report

    return BugReport(
        title=check.title,
        screen=f"{check.module}, detail view",
        role=ROLE_BROKER,
        severity=check.severity,
        preconditions=[f"A file is open in {check.module}.",
                       "The signed in user holds the role under test."],
        steps=[f"Open {check.module}.",
               "Perform the action described in the checkpoint.",
               "Read the result and the audit record."],
        expected=check.expected,
        actual=(f"The checkpoint is recorded as {check.status}, so the "
                f"expected behaviour was not observed."),
        workflow_impact=check.why_it_matters,
        environment={"Build": "brokerage-web 4.2.1",
                     "Browser": "Chrome 141 on macOS 15",
                     "Tenant": "demo-brokerage",
                     "Captured with": "Marker.io"})


def console_summary() -> dict:
    summary = workflow_summary()
    split = split_with_cap(9000, 7000, 10000, 1200)
    return {
        "checks": summary["total"],
        "blockers_open": summary["blockers_open"],
        "verified_rate": summary["verified_rate"],
        "blocked": summary["blocked"],
        "roles": len(ROLES),
        "modules": len(MODULES),
        "cap_agent": split.legs[0].cents,
        "cap_reconciles": split.reconciles,
    }
