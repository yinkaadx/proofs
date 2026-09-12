"""Zero Trust Remote Access Console engine.

Three concerns, kept separate: who may see which unattended machines, whether a
login satisfied multi factor authentication, and short lived codes for ad hoc
support sessions.

Pure logic, no Streamlit import, so this is unit testable on its own and can sit
behind a real RMM API. Deterministic by construction: the clock and the random
source are both passed in, so a test and a production run of the same inputs
agree. The caller supplies `secrets.SystemRandom()` in production, which is the
only appropriate source for a code that grants remote access.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

ENGINE_VERSION = "1.0.0"

# Tenants
COMPANY_A = "Company A"
COMPANY_B = "Company B"
TENANTS: tuple[str, ...] = (COMPANY_A, COMPANY_B)

# Roles, from most to least privileged.
MSP_ADMIN = "MSP Admin"
TENANT_ADMIN = "Tenant Admin"
TECHNICIAN = "Technician"
AUDITOR = "Auditor"
ROLES: tuple[str, ...] = (MSP_ADMIN, TENANT_ADMIN, TECHNICIAN, AUDITOR)

# Capabilities each role carries. An auditor can read the estate and the audit
# trail but can never open a session, because the point of the role is to review
# access without holding it.
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    MSP_ADMIN: frozenset({"view", "connect", "generate_code", "manage_users",
                          "cross_tenant"}),
    TENANT_ADMIN: frozenset({"view", "connect", "generate_code", "manage_users"}),
    TECHNICIAN: frozenset({"view", "connect", "generate_code"}),
    AUDITOR: frozenset({"view"}),
}

# MFA outcomes
MFA_OK = "granted"
MFA_TIMEOUT = "mfa_timeout"
MFA_WRONG_CODE = "mfa_wrong_code"
MFA_NOT_ENROLLED = "mfa_not_enrolled"

OUTCOME_LABEL = {
    MFA_OK: "Granted",
    MFA_TIMEOUT: "Denied, MFA timeout",
    MFA_WRONG_CODE: "Denied, wrong MFA code",
    MFA_NOT_ENROLLED: "Denied, MFA not enrolled",
}

# Ad hoc session codes
CODE_LENGTH = 6
CODE_TTL_MINUTES = 15

CODE_VALID = "valid"
CODE_EXPIRED = "expired"
CODE_UNKNOWN = "unknown"
CODE_USED = "already_used"

REDEEM_LABEL = {
    CODE_VALID: "Valid",
    CODE_EXPIRED: "Expired",
    CODE_UNKNOWN: "Unknown code",
    CODE_USED: "Already used",
}


# ---------------------------------------------------------------------------
# Directory
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Machine:
    """An unattended endpoint enrolled in the RMM."""
    machine_id: str
    hostname: str
    tenant: str
    site: str
    operating_system: str


@dataclass(frozen=True)
class User:
    username: str
    display_name: str
    tenant: str          # the tenant this user belongs to
    role: str
    sites: tuple[str, ...] = ()   # technicians are scoped to named sites
    mfa_enrolled: bool = True

    @property
    def capabilities(self) -> frozenset[str]:
        return ROLE_CAPABILITIES.get(self.role, frozenset())

    def can(self, capability: str) -> bool:
        return capability in self.capabilities


SAMPLE_MACHINES: tuple[Machine, ...] = (
    Machine("A-SRV-01", "acme-dc01", COMPANY_A, "London HQ", "Windows Server 2022"),
    Machine("A-SRV-02", "acme-file01", COMPANY_A, "London HQ", "Windows Server 2019"),
    Machine("A-WKS-11", "acme-fin-ws11", COMPANY_A, "Manchester", "Windows 11"),
    Machine("A-WKS-12", "acme-fin-ws12", COMPANY_A, "Manchester", "Windows 11"),
    Machine("B-SRV-01", "beta-app01", COMPANY_B, "Leeds", "Ubuntu 24.04"),
    Machine("B-WKS-21", "beta-ops-ws21", COMPANY_B, "Leeds", "Windows 11"),
    Machine("B-WKS-22", "beta-ops-ws22", COMPANY_B, "Bristol", "macOS 15"),
)

SAMPLE_USERS: tuple[User, ...] = (
    User("msp.oncall", "MSP on call engineer", COMPANY_A, MSP_ADMIN),
    User("a.admin", "Ada, Company A admin", COMPANY_A, TENANT_ADMIN),
    User("a.tech.london", "Femi, Company A technician, London", COMPANY_A,
         TECHNICIAN, ("London HQ",)),
    User("a.auditor", "Priya, Company A auditor", COMPANY_A, AUDITOR),
    User("b.admin", "Ben, Company B admin", COMPANY_B, TENANT_ADMIN),
    User("b.tech.leeds", "Sara, Company B technician, Leeds", COMPANY_B,
         TECHNICIAN, ("Leeds",)),
    User("b.tech.nomfa", "Tom, Company B technician, no MFA", COMPANY_B,
         TECHNICIAN, ("Leeds", "Bristol"), mfa_enrolled=False),
)


def user_by_username(username: str, users=SAMPLE_USERS) -> User | None:
    for user in users:
        if user.username == username:
            return user
    return None


# ---------------------------------------------------------------------------
# Multitenant access control
# ---------------------------------------------------------------------------

def visible_machines(user: User, machines=SAMPLE_MACHINES) -> list[Machine]:
    """Every machine this user may see, and nothing else.

    The tenant boundary is checked first and is absolute: a Company A account
    can never enumerate a Company B endpoint, whatever its role. Only an
    explicit cross tenant capability, held by the managing provider, crosses it.
    Site scoping then narrows a technician to the sites they support, because a
    technician who can see every endpoint is an admin by another name.
    """
    if not user.can("view"):
        return []
    if user.can("cross_tenant"):
        candidates = list(machines)
    else:
        candidates = [m for m in machines if m.tenant == user.tenant]
    if user.role == TECHNICIAN:
        if not user.sites:
            return []
        candidates = [m for m in candidates if m.site in user.sites]
    return candidates


def can_connect_to(user: User, machine: Machine) -> tuple[bool, str]:
    """Whether this user may open a session, and why not when they may not.

    Visibility is necessary but not sufficient: an auditor sees the estate and
    still cannot connect to any of it.
    """
    if not user.can("view"):
        return False, f"{user.role} cannot view the estate."
    if machine not in visible_machines(user):
        if not user.can("cross_tenant") and machine.tenant != user.tenant:
            return False, (
                f"{machine.machine_id} belongs to {machine.tenant} and "
                f"{user.display_name} belongs to {user.tenant}. The tenant "
                f"boundary is absolute.")
        return False, (
            f"{machine.machine_id} is at {machine.site}, which is outside the "
            f"sites {user.display_name} supports.")
    if not user.can("connect"):
        return False, (
            f"The {user.role} role is read only by design, so it can review "
            f"access without holding it.")
    return True, f"{user.display_name} may open a session to {machine.machine_id}."


def access_matrix(users=SAMPLE_USERS, machines=SAMPLE_MACHINES) -> list[dict]:
    """One row per user, for the access matrix table.

    Every column holds one type, because the table widget serialises through
    Arrow and Arrow refuses a mixed column.
    """
    rows = []
    for user in users:
        visible = visible_machines(user, machines)
        rows.append({
            "User": user.username,
            "Name": user.display_name,
            "Tenant": user.tenant,
            "Role": user.role,
            "Sites": ", ".join(user.sites) if user.sites else "all sites",
            "Machines visible": len(visible),
            "Can connect": "Yes" if user.can("connect") else "No",
            "MFA enrolled": "Yes" if user.mfa_enrolled else "No",
        })
    return rows


# ---------------------------------------------------------------------------
# MFA enforcement ledger
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LoginAttempt:
    sequence: int
    timestamp: str
    username: str
    tenant: str
    role: str
    outcome: str
    detail: str
    source_ip: str

    @property
    def granted(self) -> bool:
        return self.outcome == MFA_OK


@dataclass
class MfaLedger:
    """Append only record of every login attempt and its MFA result."""
    attempts: list[LoginAttempt] = field(default_factory=list)

    def record(self, user: User, outcome: str, detail: str, now: str,
               source_ip: str) -> LoginAttempt:
        attempt = LoginAttempt(
            sequence=len(self.attempts) + 1, timestamp=now,
            username=user.username, tenant=user.tenant, role=user.role,
            outcome=outcome, detail=detail, source_ip=source_ip)
        self.attempts.append(attempt)
        return attempt

    @property
    def granted(self) -> list[LoginAttempt]:
        return [a for a in self.attempts if a.granted]

    @property
    def denied(self) -> list[LoginAttempt]:
        return [a for a in self.attempts if not a.granted]

    def rows(self) -> list[dict]:
        return [
            {
                "#": a.sequence,
                "Timestamp": a.timestamp,
                "User": a.username,
                "Tenant": a.tenant,
                "Role": a.role,
                "Outcome": OUTCOME_LABEL.get(a.outcome, a.outcome),
                "Detail": a.detail,
                "Source IP": a.source_ip,
            }
            for a in self.attempts
        ]


# Probability that a push prompt is answered in time, used only when the caller
# does not force an outcome. Real numbers come from the identity provider; this
# is a simulator, so the odds are declared rather than hidden.
PUSH_APPROVAL_RATE = 0.7


def attempt_login(user: User, ledger: MfaLedger, rng, now: str,
                  source_ip: str = "203.0.113.42",
                  force_outcome: str | None = None) -> LoginAttempt:
    """Simulate one login and record what MFA decided.

    A user who is not enrolled is denied outright rather than waved through.
    That is the whole point of enforcement: the weakest account decides the
    security of the tenant, so an unenrolled account must not be able to
    authenticate at all.

    `rng` is injected so a test is exact and production can pass a real random
    source. `force_outcome` exists for the same reason.
    """
    if not user.mfa_enrolled:
        return ledger.record(
            user, MFA_NOT_ENROLLED,
            (f"{user.username} has no second factor enrolled. Access is refused "
             f"outright rather than downgraded to a password, because an "
             f"unenrolled account would otherwise be the way in."),
            now, source_ip)

    if force_outcome is not None:
        outcome = force_outcome
    else:
        roll = rng.random()
        if roll < PUSH_APPROVAL_RATE:
            outcome = MFA_OK
        elif roll < PUSH_APPROVAL_RATE + 0.2:
            outcome = MFA_TIMEOUT
        else:
            outcome = MFA_WRONG_CODE

    details = {
        MFA_OK: (f"Password accepted and the push prompt was approved on the "
                 f"enrolled device. Session opened for {user.role}."),
        MFA_TIMEOUT: ("Password accepted, but the push prompt expired before it "
                      "was answered. No session was opened."),
        MFA_WRONG_CODE: ("Password accepted, but the one time code did not "
                         "match. No session was opened."),
    }
    return ledger.record(user, outcome, details[outcome], now, source_ip)


def compliance(ledger: MfaLedger, users=SAMPLE_USERS) -> dict:
    """Figures a security reviewer would ask for."""
    total = len(ledger.attempts)
    enrolled = [u for u in users if u.mfa_enrolled]
    return {
        "attempts": total,
        "granted": len(ledger.granted),
        "denied": len(ledger.denied),
        "grant_rate": (len(ledger.granted) / total) if total else 0.0,
        "users_total": len(users),
        "users_enrolled": len(enrolled),
        "enrolment_rate": (len(enrolled) / len(users)) if users else 0.0,
        "unenrolled": [u.username for u in users if not u.mfa_enrolled],
        # Every granted session passed a second factor. This is the assertion
        # the whole ledger exists to support.
        "granted_without_mfa": sum(
            1 for a in ledger.granted if a.outcome != MFA_OK),
    }


# ---------------------------------------------------------------------------
# Ad hoc support session codes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionCode:
    code: str
    issued_by: str
    tenant: str
    issued_at: str
    expires_at: str
    redeemed_at: str = ""
    note: str = ""

    @property
    def used(self) -> bool:
        return bool(self.redeemed_at)


def _parse(moment: str) -> datetime:
    text = str(moment).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class CodeVault:
    """Issued ad hoc codes, and what happened to each."""
    codes: list[SessionCode] = field(default_factory=list)

    def active(self, now: str) -> list[SessionCode]:
        moment = _parse(now)
        return [c for c in self.codes
                if not c.used and _parse(c.expires_at) > moment]

    def find(self, code: str) -> SessionCode | None:
        for issued in self.codes:
            if issued.code == code:
                return issued
        return None

    def issue(self, user: User, rng, now: str,
              ttl_minutes: int = CODE_TTL_MINUTES,
              note: str = "") -> SessionCode:
        """Mint a short lived code for one attended support session.

        The code is short because a human reads it down a phone line, which is
        exactly why it must also be short lived and single use. Production
        passes secrets.SystemRandom(): a predictable code here would hand out
        remote access.
        """
        if not user.can("generate_code"):
            raise PermissionError(
                f"The {user.role} role cannot issue session codes, because "
                f"issuing one grants remote access to whoever reads it.")
        if ttl_minutes <= 0:
            raise ValueError("A session code must be valid for a positive number "
                             "of minutes.")

        taken = {c.code for c in self.active(now)}
        code = _mint(rng, taken)
        issued_at = _parse(now)
        issued = SessionCode(
            code=code, issued_by=user.username, tenant=user.tenant,
            issued_at=_format(issued_at),
            expires_at=_format(issued_at + timedelta(minutes=ttl_minutes)),
            note=note or f"Ad hoc support session for {user.tenant}.")
        self.codes.append(issued)
        return issued

    def redeem(self, code: str, now: str) -> tuple[str, SessionCode | None]:
        """Spend a code. Single use, and expiry is checked before use."""
        issued = self.find(code)
        if issued is None:
            return CODE_UNKNOWN, None
        if issued.used:
            return CODE_USED, issued
        if _parse(issued.expires_at) <= _parse(now):
            return CODE_EXPIRED, issued
        spent = replace(issued, redeemed_at=_format(_parse(now)))
        self.codes[self.codes.index(issued)] = spent
        return CODE_VALID, spent

    def rows(self, now: str) -> list[dict]:
        moment = _parse(now)
        rows = []
        for issued in self.codes:
            if issued.used:
                status = "Redeemed"
            elif _parse(issued.expires_at) <= moment:
                status = "Expired"
            else:
                status = "Active"
            rows.append({
                "Code": issued.code,
                "Issued by": issued.issued_by,
                "Tenant": issued.tenant,
                "Issued at": issued.issued_at,
                "Expires at": issued.expires_at,
                "Status": status,
                "Redeemed at": issued.redeemed_at or "not redeemed",
            })
        return rows


def _mint(rng, taken: set[str]) -> str:
    """A code of exactly CODE_LENGTH digits, not currently in use.

    Leading zeros are kept, because a code read aloud as "zero four" must be
    typed as "04". Collisions are retried rather than ignored: two live sessions
    sharing a code would route a stranger to the wrong machine.
    """
    upper = 10 ** CODE_LENGTH
    for _ in range(100):
        candidate = f"{rng.randrange(upper):0{CODE_LENGTH}d}"
        if candidate not in taken:
            return candidate
    raise RuntimeError(
        "Could not mint an unused session code after 100 attempts. Expire some "
        "outstanding codes before issuing more.")
