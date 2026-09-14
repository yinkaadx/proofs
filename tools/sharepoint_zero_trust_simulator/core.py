"""Secure SharePoint architecture engine.

Three things a client asks before a SharePoint rollout is signed off: who can
get in, what they can touch once they are in, and whether the tenant is
configured to keep it that way. This answers all three from one configuration
object, so the answers cannot disagree with each other.

That single source is the point. A sign in decision, the access matrix and the
deployment audit all read the same `TenantConfig`, so turning external sharing
off in the audit also stops the guest signing in and empties the guest column of
the matrix. A console where those three drift apart is worse than no console,
because it certifies a posture the tenant does not have.

Pure logic, no Streamlit import, so this is unit testable on its own and can sit
behind a real Microsoft Graph reader. Deterministic: nothing here reads the
clock or a random source unless the caller passes one in.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

ENGINE_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Who is signing in
# ---------------------------------------------------------------------------

ADMIN = "Internal Admin"
EMPLOYEE = "Internal Employee"
GUEST = "External Guest"
UNAUTHORIZED = "Unauthorized User"
USER_TYPES: tuple[str, ...] = (ADMIN, EMPLOYEE, GUEST, UNAUTHORIZED)

# SharePoint permission levels, plus the absence of one.
OWNER = "Owner"
MEMBER = "Member"
VISITOR = "Visitor"
NO_ACCESS = "No access"
ROLES: tuple[str, ...] = (OWNER, MEMBER, VISITOR)

READ = "Read"
WRITE = "Write"
DELETE = "Delete"
ACTIONS: tuple[str, ...] = (READ, WRITE, DELETE)

# A user type maps to exactly one default permission level. Anything finer than
# this belongs in a group, not in a person.
ROLE_BY_USER_TYPE: dict[str, str] = {
    ADMIN: OWNER,
    EMPLOYEE: MEMBER,
    GUEST: VISITOR,
    UNAUTHORIZED: NO_ACCESS,
}

# Sign in signals
CLIENT_MODERN = "Modern authentication"
CLIENT_LEGACY = "Legacy authentication"
CLIENT_APPS: tuple[str, ...] = (CLIENT_MODERN, CLIENT_LEGACY)

RISK_NONE = "None"
RISK_MEDIUM = "Medium"
RISK_HIGH = "High"
RISK_LEVELS: tuple[str, ...] = (RISK_NONE, RISK_MEDIUM, RISK_HIGH)

TRUSTED_COUNTRIES: tuple[str, ...] = ("United Kingdom", "Ireland", "Germany")
BLOCKED_COUNTRIES: tuple[str, ...] = ("Unlisted country",)
COUNTRIES: tuple[str, ...] = TRUSTED_COUNTRIES + BLOCKED_COUNTRIES

# Policy decisions
BLOCK = "Block"
GRANT = "Grant"
NOT_APPLIED = "Not applied"

# ---------------------------------------------------------------------------
# Tenant configuration
# ---------------------------------------------------------------------------

SHARING_ANYONE = "Anyone with the link"
SHARING_NEW_AND_EXISTING = "New and existing guests"
SHARING_EXISTING = "Existing guests only"
SHARING_DISABLED = "Disabled"
SHARING_LEVELS: tuple[str, ...] = (SHARING_ANYONE, SHARING_NEW_AND_EXISTING,
                                   SHARING_EXISTING, SHARING_DISABLED)

LINK_ANYONE = "Anyone with the link"
LINK_ORGANISATION = "People in the organisation"
LINK_SPECIFIC = "Specific people"
LINK_TYPES: tuple[str, ...] = (LINK_ANYONE, LINK_ORGANISATION, LINK_SPECIFIC)

DEVICE_FULL = "Full access from any device"
DEVICE_LIMITED = "Limited web only access"
DEVICE_BLOCK = "Block unmanaged devices"
DEVICE_POLICIES: tuple[str, ...] = (DEVICE_FULL, DEVICE_LIMITED, DEVICE_BLOCK)

CRITICAL = "Critical"
HIGH = "High"
MEDIUM = "Medium"
SEVERITY_ORDER: dict[str, int] = {CRITICAL: 0, HIGH: 1, MEDIUM: 2}


@dataclass(frozen=True)
class TenantConfig:
    """Every switch the console reasons about, in one immutable object."""
    external_sharing: str = SHARING_ANYONE
    default_link_type: str = LINK_ANYONE
    mfa_enforced: bool = False
    legacy_auth_blocked: bool = False
    unmanaged_device_policy: str = DEVICE_FULL
    guest_expiry_days: int = 0
    anonymous_link_expiry_days: int = 0
    dlp_policy_enabled: bool = False
    versioning_enabled: bool = False
    site_creation_restricted: bool = False
    idle_session_timeout_minutes: int = 0


# A tenant as Microsoft hands it over. Every one of these defaults is permissive,
# which is why a deployment that changes nothing fails its own audit.
DEFAULT_TENANT = TenantConfig()

# The target posture. This is what the audit is measured against.
HARDENED_TENANT = TenantConfig(
    external_sharing=SHARING_NEW_AND_EXISTING,
    default_link_type=LINK_SPECIFIC,
    mfa_enforced=True,
    legacy_auth_blocked=True,
    unmanaged_device_policy=DEVICE_LIMITED,
    guest_expiry_days=30,
    anonymous_link_expiry_days=14,
    dlp_policy_enabled=True,
    versioning_enabled=True,
    site_creation_restricted=True,
    idle_session_timeout_minutes=60,
)


# ---------------------------------------------------------------------------
# Sign in
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SignInContext:
    """The signals Entra ID evaluates a sign in against."""
    user_type: str = EMPLOYEE
    device_compliant: bool = True
    mfa_completed: bool = True
    country: str = TRUSTED_COUNTRIES[0]
    risk_level: str = RISK_NONE
    client_app: str = CLIENT_MODERN


@dataclass(frozen=True)
class PolicyResult:
    name: str
    decision: str        # BLOCK, GRANT or NOT_APPLIED
    reason: str

    @property
    def applied(self) -> bool:
        return self.decision != NOT_APPLIED


@dataclass(frozen=True)
class SignInDecision:
    allowed: bool
    user_type: str
    role: str
    headline: str
    summary: str
    policies: list[PolicyResult]
    session_controls: list[str]

    @property
    def blocking_policy(self) -> PolicyResult | None:
        for policy in self.policies:
            if policy.decision == BLOCK:
                return policy
        return None

    def rows(self) -> list[dict]:
        return [
            {"Policy": policy.name, "Decision": policy.decision,
             "Reason": policy.reason}
            for policy in self.policies
        ]


def evaluate_sign_in(context: SignInContext,
                     config: TenantConfig = HARDENED_TENANT) -> SignInDecision:
    """Run the conditional access stack and say yes or no, with the reason.

    Policies are evaluated in full rather than short circuiting on the first
    block, because the value of this screen is showing every policy that had an
    opinion. A block anywhere in the stack denies the sign in: that is how Entra
    resolves a conflict, and a console that resolved it the other way would
    teach the client the wrong model.
    """
    policies: list[PolicyResult] = []
    controls: list[str] = []
    user_type = context.user_type
    guest = user_type == GUEST
    internal = user_type in (ADMIN, EMPLOYEE)

    # 1. The directory itself. Nothing else matters if the account is not there.
    if user_type == UNAUTHORIZED:
        policies.append(PolicyResult(
            "Directory lookup", BLOCK,
            "The account is not in the tenant directory, so no policy can grant "
            "it anything. Entra ID refuses the sign in before conditional "
            "access runs at all."))
    else:
        policies.append(PolicyResult(
            "Directory lookup", GRANT,
            f"The account resolves to a known {user_type.lower()} in the tenant."))

    # 2. Legacy authentication, which cannot present an MFA challenge.
    if context.client_app == CLIENT_LEGACY:
        if config.legacy_auth_blocked:
            policies.append(PolicyResult(
                "Block legacy authentication", BLOCK,
                "The client is using legacy authentication, which cannot carry "
                "an MFA challenge. It is the route password spraying takes, so "
                "it is refused outright."))
        else:
            policies.append(PolicyResult(
                "Block legacy authentication", GRANT,
                "Legacy authentication is still permitted on this tenant, so "
                "the sign in proceeds without ever being able to prompt for a "
                "second factor. This is the single largest hole on the list."))
    else:
        policies.append(PolicyResult(
            "Block legacy authentication", NOT_APPLIED,
            "The client is using modern authentication, so this policy does "
            "not apply."))

    # 3. Multi factor authentication.
    if user_type == UNAUTHORIZED:
        policies.append(PolicyResult(
            "Require multi factor authentication", NOT_APPLIED,
            "No account to challenge."))
    elif not config.mfa_enforced:
        policies.append(PolicyResult(
            "Require multi factor authentication", GRANT,
            "MFA is not enforced on this tenant, so a password alone is enough. "
            "A stolen password is then a full session."))
    elif context.mfa_completed:
        policies.append(PolicyResult(
            "Require multi factor authentication", GRANT,
            "The second factor was satisfied."))
    else:
        policies.append(PolicyResult(
            "Require multi factor authentication", BLOCK,
            "MFA is enforced and the second factor was not satisfied, so the "
            "sign in is refused however valid the password was."))

    # 4. Device compliance. Admins are held to a harder line than employees,
    #    because an admin session is worth more to an attacker.
    if user_type == ADMIN and not context.device_compliant:
        policies.append(PolicyResult(
            "Require a compliant device for privileged roles", BLOCK,
            "An administrator is signing in from a device that is not enrolled "
            "or not compliant. An owner session from an unmanaged machine is "
            "the highest value target in the tenant."))
    elif internal and not context.device_compliant:
        decision, reason = {
            DEVICE_BLOCK: (BLOCK,
                           "The device is not compliant and unmanaged devices "
                           "are blocked, so the sign in is refused."),
            DEVICE_LIMITED: (GRANT,
                             "The device is not compliant, so access is "
                             "downgraded to web only with download, print and "
                             "sync turned off rather than refused."),
            DEVICE_FULL: (GRANT,
                          "The device is not compliant and the tenant still "
                          "allows full access from any device, so the files can "
                          "be synced to a machine nobody manages."),
        }[config.unmanaged_device_policy]
        policies.append(PolicyResult("Unmanaged device policy", decision, reason))
        if config.unmanaged_device_policy == DEVICE_LIMITED:
            controls.append("Web only session: download, print and sync blocked")
    elif internal:
        policies.append(PolicyResult(
            "Unmanaged device policy", NOT_APPLIED,
            "The device is enrolled and compliant, so this policy does not "
            "apply."))

    # 5. Location.
    if user_type == UNAUTHORIZED:
        policies.append(PolicyResult(
            "Named location restriction", NOT_APPLIED, "No account to place."))
    elif context.country in BLOCKED_COUNTRIES:
        policies.append(PolicyResult(
            "Named location restriction", BLOCK,
            f"The sign in came from {context.country}, which is outside every "
            f"named location the tenant trusts."))
    else:
        policies.append(PolicyResult(
            "Named location restriction", GRANT,
            f"{context.country} is a trusted named location."))

    # 6. Sign in risk.
    if user_type == UNAUTHORIZED:
        policies.append(PolicyResult(
            "Sign in risk", NOT_APPLIED, "No account to score."))
    elif context.risk_level == RISK_HIGH:
        policies.append(PolicyResult(
            "Sign in risk", BLOCK,
            "Entra ID scored this sign in as high risk, which means it matches "
            "a pattern seen in a real compromise. High risk is refused rather "
            "than challenged."))
    elif context.risk_level == RISK_MEDIUM:
        policies.append(PolicyResult(
            "Sign in risk", GRANT,
            "Medium risk is allowed through, and the session is recorded for "
            "review."))
        controls.append("Sign in flagged for review in the risk report")
    else:
        policies.append(PolicyResult(
            "Sign in risk", GRANT, "No risk detected on this sign in."))

    # 7. Guest access, which is a property of the tenant rather than the person.
    if guest:
        if config.external_sharing == SHARING_DISABLED:
            policies.append(PolicyResult(
                "External sharing", BLOCK,
                "External sharing is switched off for the whole tenant, so the "
                "guest account exists but resolves to nothing it may open."))
        elif config.external_sharing == SHARING_EXISTING:
            policies.append(PolicyResult(
                "External sharing", GRANT,
                "The guest was already invited and accepted, so an existing "
                "guest may sign in. No new guest can be added."))
        else:
            policies.append(PolicyResult(
                "External sharing", GRANT,
                f"External sharing is set to {config.external_sharing}, so the "
                f"guest may sign in."))
        controls.append("Guest session: download blocked on confidential "
                        "libraries")
        if config.guest_expiry_days > 0:
            controls.append(f"Guest access expires after "
                            f"{config.guest_expiry_days} days without reinvite")

    if config.idle_session_timeout_minutes > 0:
        controls.append(f"Idle session signs out after "
                        f"{config.idle_session_timeout_minutes} minutes")

    blocked = [p for p in policies if p.decision == BLOCK]
    allowed = not blocked
    role = ROLE_BY_USER_TYPE[user_type] if allowed else NO_ACCESS

    if allowed:
        headline = f"Access granted as {role}"
        summary = (f"{user_type} signed in and holds the {role} permission "
                   f"level. {len(controls)} session control(s) apply.")
    else:
        headline = f"Access blocked by {blocked[0].name}"
        summary = (f"{user_type} was refused by "
                   f"{len(blocked)} of {len(policies)} policies. The account "
                   f"holds no permission level for this session.")

    return SignInDecision(allowed=allowed, user_type=user_type, role=role,
                          headline=headline, summary=summary, policies=policies,
                          session_controls=controls if allowed else [])


# ---------------------------------------------------------------------------
# Role based access matrix
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Library:
    name: str
    sensitivity: str
    guest_accessible: bool
    grants: dict[str, tuple[str, ...]]


LIBRARIES: tuple[Library, ...] = (
    Library("Company Policies", "Internal", True, {
        OWNER: (READ, WRITE, DELETE),
        MEMBER: (READ,),
        VISITOR: (READ,),
    }),
    Library("Project Workspaces", "Internal", True, {
        OWNER: (READ, WRITE, DELETE),
        MEMBER: (READ, WRITE),
        VISITOR: (READ,),
    }),
    Library("Client Deliverables", "Shared with clients", True, {
        OWNER: (READ, WRITE, DELETE),
        MEMBER: (READ, WRITE),
        VISITOR: (READ,),
    }),
    Library("Finance and Payroll", "Confidential", False, {
        OWNER: (READ, WRITE, DELETE),
        MEMBER: (),
        VISITOR: (),
    }),
    Library("HR Records", "Confidential", False, {
        OWNER: (READ, WRITE, DELETE),
        MEMBER: (),
        VISITOR: (),
    }),
    Library("Site Configuration", "Restricted", False, {
        OWNER: (READ, WRITE, DELETE),
        MEMBER: (READ,),
        VISITOR: (),
    }),
)


def library_by_name(name: str, libraries=LIBRARIES) -> Library | None:
    for library in libraries:
        if library.name == name:
            return library
    return None


def permissions_for(role: str, library: Library,
                    config: TenantConfig = HARDENED_TENANT,
                    guest: bool = False) -> tuple[str, ...]:
    """What a permission level may actually do in one library.

    The tenant has the last word. A guest holding Visitor still gets nothing
    when external sharing is off or when the library is confidential, because a
    permission level is a grant inside the tenant's own boundary, never a way
    through it.
    """
    if role == NO_ACCESS:
        return ()
    if guest:
        if config.external_sharing == SHARING_DISABLED:
            return ()
        if not library.guest_accessible:
            return ()
    return library.grants.get(role, ())


def describe_permissions(actions: tuple[str, ...]) -> str:
    return ", ".join(actions) if actions else NO_ACCESS


def can(role: str, library_name: str, action: str,
        config: TenantConfig = HARDENED_TENANT, guest: bool = False) -> bool:
    library = library_by_name(library_name)
    if library is None:
        return False
    return action in permissions_for(role, library, config, guest=guest)


def matrix_rows(config: TenantConfig = HARDENED_TENANT,
                libraries=LIBRARIES) -> list[dict]:
    """One row per library, one column per permission level, plus the guest.

    Every cell is a string so the column holds a single type, which is what the
    table widget needs to serialise through Arrow.
    """
    return [
        {
            "Document library": library.name,
            "Sensitivity": library.sensitivity,
            OWNER: describe_permissions(permissions_for(OWNER, library, config)),
            MEMBER: describe_permissions(permissions_for(MEMBER, library, config)),
            VISITOR: describe_permissions(permissions_for(VISITOR, library, config)),
            "External guest": describe_permissions(
                permissions_for(VISITOR, library, config, guest=True)),
        }
        for library in libraries
    ]


def role_summary_rows(libraries=LIBRARIES) -> list[dict]:
    """What each permission level means, stated once rather than inferred."""
    return [
        {"Permission level": OWNER,
         "Granted to": ADMIN,
         "May": "Read, write and delete in every library, and change the site "
                "itself",
         "Libraries reachable": str(len(libraries))},
        {"Permission level": MEMBER,
         "Granted to": EMPLOYEE,
         "May": "Read and write where the work happens, read only elsewhere, "
                "and never delete",
         "Libraries reachable": str(sum(
             1 for library in libraries if library.grants.get(MEMBER)))},
        {"Permission level": VISITOR,
         "Granted to": GUEST,
         "May": "Read what has been shared, and nothing that is marked "
                "confidential",
         "Libraries reachable": str(sum(
             1 for library in libraries if library.grants.get(VISITOR)))},
    ]


# ---------------------------------------------------------------------------
# Secure deployment audit
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    key: str
    title: str
    severity: str
    requirement: str
    current: str
    passing: bool
    why: str
    remediation: str


def audit_findings(config: TenantConfig) -> list[Finding]:
    """Measure a tenant against the posture the deployment promises.

    Ordered worst first, so the first thing on screen is the thing that keeps
    the deployment from going live.
    """
    findings = [
        Finding(
            key="external_sharing",
            title="External sharing restricted",
            severity=CRITICAL,
            requirement="Anything other than anyone with the link",
            current=config.external_sharing,
            passing=config.external_sharing != SHARING_ANYONE,
            why="Anyone with the link means a file can be opened by a person "
                "who never signed in, from a link forwarded anywhere. The "
                "tenant cannot name who read it.",
            remediation="Set-SPOTenant -SharingCapability "
                        "ExternalUserSharingOnly",
        ),
        Finding(
            key="mfa_enforced",
            title="Multi factor authentication enforced",
            severity=CRITICAL,
            requirement="Enforced for every user",
            current="Enforced" if config.mfa_enforced else "Not enforced",
            passing=config.mfa_enforced,
            why="Without a second factor a stolen password is a full session, "
                "and the sign in log shows nothing unusual.",
            remediation="New-MgIdentityConditionalAccessPolicy with grant "
                        "control mfa for all users",
        ),
        Finding(
            key="legacy_auth_blocked",
            title="Legacy authentication blocked",
            severity=CRITICAL,
            requirement="Blocked",
            current="Blocked" if config.legacy_auth_blocked else "Allowed",
            passing=config.legacy_auth_blocked,
            why="Legacy protocols cannot present an MFA prompt, so leaving them "
                "open quietly exempts every account from the MFA policy above.",
            remediation="Conditional access policy blocking "
                        "clientAppTypes exchangeActiveSync and other",
        ),
        Finding(
            key="default_link_type",
            title="Default sharing link set to specific people",
            severity=HIGH,
            requirement=LINK_SPECIFIC,
            current=config.default_link_type,
            passing=config.default_link_type == LINK_SPECIFIC,
            why="The default is what a busy person clicks. If the default link "
                "is anyone with the link, the safe option has to be chosen "
                "every single time, and one miss is a public file.",
            remediation="Set-SPOTenant -DefaultSharingLinkType Direct",
        ),
        Finding(
            key="unmanaged_device_policy",
            title="Unmanaged device access limited",
            severity=HIGH,
            requirement="Limited web only access or blocked",
            current=config.unmanaged_device_policy,
            passing=config.unmanaged_device_policy != DEVICE_FULL,
            why="Full access from an unmanaged device lets the whole library "
                "sync to a laptop the tenant cannot wipe.",
            remediation="Set-SPOTenant -ConditionalAccessPolicy "
                        "AllowLimitedAccess",
        ),
        Finding(
            key="guest_expiry_days",
            title="Guest access expires",
            severity=HIGH,
            requirement="60 days or fewer",
            current=(f"{config.guest_expiry_days} days"
                     if config.guest_expiry_days > 0 else "Never expires"),
            passing=0 < config.guest_expiry_days <= 60,
            why="A guest invited for one project keeps their access for years "
                "otherwise, long after the contract ends.",
            remediation="Set-SPOTenant -ExternalUserExpirationRequired $true "
                        "-ExternalUserExpireInDays 30",
        ),
        Finding(
            key="anonymous_link_expiry_days",
            title="Anonymous link expiry set",
            severity=HIGH,
            requirement="30 days or fewer",
            current=(f"{config.anonymous_link_expiry_days} days"
                     if config.anonymous_link_expiry_days > 0
                     else "No expiry"),
            passing=0 < config.anonymous_link_expiry_days <= 30,
            why="A link with no expiry outlives the reason it was created, and "
                "nobody goes back to revoke it.",
            remediation="Set-SPOTenant -RequireAnonymousLinksExpireInDays 14",
        ),
        Finding(
            key="dlp_policy_enabled",
            title="Data loss prevention policy active",
            severity=MEDIUM,
            requirement="Enabled",
            current="Enabled" if config.dlp_policy_enabled else "Disabled",
            passing=config.dlp_policy_enabled,
            why="Without it, a card number or a national insurance number in a "
                "document leaves the tenant unremarked.",
            remediation="New-DlpCompliancePolicy scoped to SharePoint and "
                        "OneDrive",
        ),
        Finding(
            key="versioning_enabled",
            title="Version history retained",
            severity=MEDIUM,
            requirement="Enabled",
            current="Enabled" if config.versioning_enabled else "Disabled",
            passing=config.versioning_enabled,
            why="Versioning is what turns a ransomware event or a bad overwrite "
                "into a restore rather than a loss.",
            remediation="Set-SPOSite -EnableVersionExpirationSetting with a "
                        "retained major version limit",
        ),
        Finding(
            key="site_creation_restricted",
            title="Site creation restricted to administrators",
            severity=MEDIUM,
            requirement="Restricted",
            current=("Restricted" if config.site_creation_restricted
                     else "Any user may create a site"),
            passing=config.site_creation_restricted,
            why="Sites created outside the plan inherit none of it, and they "
                "are the ones nobody audits.",
            remediation="Set-SPOTenant -SelfServiceSiteCreationDisabled $true",
        ),
        Finding(
            key="idle_session_timeout_minutes",
            title="Idle session timeout set",
            severity=MEDIUM,
            requirement="60 minutes or fewer",
            current=(f"{config.idle_session_timeout_minutes} minutes"
                     if config.idle_session_timeout_minutes > 0
                     else "No timeout"),
            passing=0 < config.idle_session_timeout_minutes <= 60,
            why="A session left open on a shared or personal machine is an "
                "unlocked door for as long as it lasts.",
            remediation="Set-SPOBrowserIdleSignOut -Enabled $true "
                        "-WarnAfter 00:50:00 -SignOutAfter 01:00:00",
        ),
    ]
    return sorted(findings, key=lambda f: (f.passing, SEVERITY_ORDER[f.severity]))


@dataclass(frozen=True)
class AuditScore:
    passing: int
    total: int
    open_critical: int
    open_high: int
    band: str
    verdict: str

    @property
    def percent(self) -> int:
        return round(self.passing * 100 / self.total) if self.total else 0


BAND_READY = "Ready to deploy"
BAND_EXCEPTIONS = "Deploy with named exceptions"
BAND_BLOCKED = "Not ready to deploy"


def audit_score(findings: list[Finding]) -> AuditScore:
    total = len(findings)
    passing = sum(1 for f in findings if f.passing)
    open_critical = sum(1 for f in findings
                        if not f.passing and f.severity == CRITICAL)
    open_high = sum(1 for f in findings if not f.passing and f.severity == HIGH)

    if open_critical:
        band = BAND_BLOCKED
        verdict = (f"{open_critical} critical control(s) are still open. A "
                   f"tenant in this state exposes content to people who never "
                   f"signed in, so it does not go live.")
    elif open_high:
        band = BAND_EXCEPTIONS
        verdict = (f"No critical control is open, but {open_high} high "
                   f"severity item(s) remain. Each one needs a named owner and "
                   f"a date before handover.")
    elif passing == total:
        band = BAND_READY
        verdict = ("Every control on the checklist passes. The posture matches "
                   "what the deployment promises.")
    else:
        band = BAND_EXCEPTIONS
        verdict = (f"{total - passing} medium severity item(s) remain. Nothing "
                   f"here blocks handover, and each one should still be closed.")
    return AuditScore(passing=passing, total=total, open_critical=open_critical,
                      open_high=open_high, band=band, verdict=verdict)


def audit_rows(findings: list[Finding]) -> list[dict]:
    return [
        {"Status": "Pass" if f.passing else "Fail",
         "Severity": f.severity,
         "Control": f.title,
         "Required": f.requirement,
         "Current": f.current}
        for f in findings
    ]


def remediation_script(findings: list[Finding]) -> str:
    """PowerShell for the open items only, in the order they should be run."""
    failing = [f for f in findings if not f.passing]
    if not failing:
        return "# Every control passes. Nothing to remediate."
    lines = ["# Remediation for the open controls, worst first.",
             "# Run in a SharePoint Online Management Shell connected to the "
             "target tenant.", ""]
    for finding in failing:
        lines.append(f"# {finding.severity}: {finding.title}")
        lines.append(finding.remediation)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def apply_hardening(config: TenantConfig, keys: list[str]) -> TenantConfig:
    """Move the named controls to their hardened value, leaving the rest alone.

    Used by the console's remediation button so a client can watch the score,
    the guest column of the matrix and the sign in decision all move together
    from one change.
    """
    hardened = {key: getattr(HARDENED_TENANT, key) for key in keys
                if hasattr(HARDENED_TENANT, key)}
    return replace(config, **hardened) if hardened else config
