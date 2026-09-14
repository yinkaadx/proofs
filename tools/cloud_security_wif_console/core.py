"""EHR cloud security and Workload Identity Federation engine.

Pure logic behind four things a security architect has to be able to prove
rather than assert: that an Azure managed identity can reach a GCS bucket with
no key on disk, that a downscoped token cannot cross a tenant boundary, that
every stored secret which can be removed has been removed, and that the static
service account JSON key on the IIS host is the single worst artefact in the
estate.

No Streamlit import. Where a real implementation would read the clock or open a
socket, this module takes a `now` argument or returns a fully described request
object, so a test, the console and a production broker all agree on the same
answer for the same input.

Two deliberate limits on what is asserted here. The CEL evaluators are a small
modelled subset, not Google's evaluator: they cover equality, inequality and
prefix tests, which is what these flows use, and they fail closed on anything
they do not understand. And the flow never fabricates a success: a step passes
only because the input actually satisfies the rule being checked, which is why
the fault scenarios mutate the inputs rather than forcing a status.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

ENGINE_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Endpoints and protocol constants
#
# These are transcribed from the live API discovery documents and from Google's
# and Microsoft's own client libraries. They are spelled out as constants
# because every one of them is a place where a plausible looking guess produces
# a 400 that is then debugged for a day.
# ---------------------------------------------------------------------------

# Azure instance metadata. The Metadata header is not decoration: it is the
# documented mitigation against server side request forgery, and IMDS refuses
# the request without it.
IMDS_TOKEN_URL = "http://169.254.169.254/metadata/identity/oauth2/token"
IMDS_API_VERSION = "2018-02-01"

# GCP Security Token Service. The discovery document is explicit that this call
# must NOT carry an Authorization header, and that sending one can make the
# request fail.
STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"
STS_NO_AUTH_NOTE = (
    "Do not send the Authorization HTTP header on this request. The method "
    "does not require it and sending it can cause the request to fail."
)

# IAM Credentials impersonation. The '-' project wildcard is required, and
# replacing it with a real project ID is invalid.
IAM_CREDENTIALS_URL = (
    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
    "{service_account}:generateAccessToken"
)

GRANT_TYPE_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
TOKEN_TYPE_JWT = "urn:ietf:params:oauth:token-type:jwt"
TOKEN_TYPE_ID_TOKEN = "urn:ietf:params:oauth:token-type:id_token"
TOKEN_TYPE_ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"
TOKEN_TYPE_ACCESS_BOUNDARY = (
    "urn:ietf:params:oauth:token-type:access_boundary_intermediary_token"
)

SCOPE_CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"
SCOPE_IAM = "https://www.googleapis.com/auth/iam"
SCOPE_GCS_READ = "https://www.googleapis.com/auth/devstorage.read_only"

ROLE_WORKLOAD_IDENTITY_USER = "roles/iam.workloadIdentityUser"
ROLE_TOKEN_CREATOR = "roles/iam.serviceAccountTokenCreator"

# The lifetime ceilings that make the phrase "short lived" checkable. An
# impersonated access token defaults to one hour and is capped at one hour
# unless the organization policy below allows the service account up to twelve.
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600
MAX_TOKEN_LIFETIME_SECONDS = 3600
LIFETIME_EXTENSION_CONSTRAINT = (
    "constraints/iam.allowServiceAccountCredentialLifetimeExtension"
)
# The subject token's own clock bounds, from the STS discovery document: iat
# must be in the past and no more than 24 hours in the past, and Google
# recommends an exp under 6 hours.
MAX_ASSERTION_AGE_SECONDS = 24 * 3600
RECOMMENDED_ASSERTION_LIFETIME_SECONDS = 6 * 3600
ACCEPTED_ASSERTION_ALGORITHMS = ("RS256", "ES256")

# allowedAudiences limits, from the workload identity pool provider resource.
MAX_ALLOWED_AUDIENCES = 10
MAX_AUDIENCE_CHARS = 256
# ManagedIdentityCredential strips this suffix before calling IMDS, so an
# allowedAudiences entry that still carries it can never match the aud claim
# that arrives. Named as a constant because the check and the warning text
# have to mean the same string.
DEFAULT_SCOPE_SUFFIX = "/.default"

# Credential Access Boundary limits, from the STS discovery document.
MAX_ACCESS_BOUNDARY_RULES = 10
ACCESS_BOUNDARY_SIZE_BUDGET = 2048
MAX_CONDITION_EXPRESSION_CHARS = 2048
MAX_ATTRIBUTE_CONDITION_CHARS = 4096
MAX_GOOGLE_SUBJECT_BYTES = 127

# Cloud Storage is the only service that honours a Credential Access Boundary.
# Stated here so nobody reaches for one as a general confinement mechanism.
CAB_SUPPORTED_SERVICES = ("Cloud Storage",)

# The list attribute the documented boundary pattern for LIST calls reads,
# where the resource being authorized is the bucket rather than an object.
#
# Sourcing note, because a proof artefact is only worth what its weakest
# citation is worth: this attribute name is reconstructed from search extracts
# of Google's access boundary documentation rather than read from the page
# itself, which was not reachable when this was written. It is corroborated by
# several independent write ups, and it is still the one identifier here that
# should be confirmed against the live document before anyone ships a boundary
# that depends on it.
OBJECT_LIST_PREFIX_ATTRIBUTE = "storage.googleapis.com/objectListPrefix"

# The permission a bucket listing asks for. It is called out because a LIST is
# the one call in this flow whose resource is the bucket rather than an object,
# which is why the boundary needs a second clause to authorize it at all.
LIST_PERMISSION = "storage.objects.list"

# ---------------------------------------------------------------------------
# Statuses and named failure reasons
# ---------------------------------------------------------------------------

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"
STATUS_WARN = "warn"

# The third outcome of an access attempt, alongside 200 and 403. A path whose
# bytes are not the bytes the storage API would compare cannot be answered by
# a byte comparison, and answering it anyway is how an evaluator ends up
# certifying a traversal. There is no HTTP status for "this question is not
# well formed", so it carries none.
STATUS_CODE_UNDECIDABLE = 0
STATUS_TEXT_UNDECIDABLE = "No verdict"

FAULT_NONE = "none"
FAULT_AUDIENCE = "wrong_audience"
FAULT_ISSUER = "wrong_issuer"
FAULT_EXPIRED = "expired_assertion"
FAULT_SUBJECT = "subject_not_mapped"
FAULT_CONDITION = "attribute_condition_rejects"
FAULT_BINDING = "missing_workload_identity_user"

# Each fault is a real misconfiguration someone has shipped, named where it
# actually bites rather than where it is noticed.
FAULTS: tuple[tuple[str, str, str], ...] = (
    (FAULT_NONE, "Correct configuration",
     "Every check is satisfied and a short lived GCS token is issued."),
    (FAULT_AUDIENCE, "Wrong audience on the Entra token",
     "allowedAudiences keeps the /.default form and the token carries the "
     "bare one, because ManagedIdentityCredential strips the suffix before "
     "calling IMDS, so the aud claim fails the allowedAudiences check."),
    (FAULT_ISSUER, "Wrong issuer on the provider",
     "The provider issuerUri does not match the iss the Entra tenant "
     "actually stamps, including its trailing slash."),
    (FAULT_EXPIRED, "Expired assertion",
     "The Entra token exp is already in the past when STS evaluates it."),
    (FAULT_SUBJECT, "Subject claim absent from the assertion",
     "The attribute mapping reads assertion.sub, and the claim it names is "
     "not present, so google.subject cannot be derived."),
    (FAULT_CONDITION, "Attribute condition rejects the principal",
     "The token is valid and correctly mapped, and the condition pinning the "
     "Entra tenant and object ID still refuses it."),
    (FAULT_BINDING, "Missing roles/iam.workloadIdentityUser",
     "Federation succeeds and impersonation is refused, because the "
     "principal:// member is not bound on the target service account."),
)

FAULT_LABEL = {key: label for key, label, _ in FAULTS}

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact(secret: str) -> str:
    """Reduce a credential to a shape you can correlate but not use.

    Applied inside every generator in this module rather than at the point of
    display, so a caller that forgets to redact cannot leak one. Nothing in
    this file ever holds a full token value after the moment it is minted.
    """
    text = str(secret or "")
    if not text:
        return ""
    if len(text) <= 12:
        return "*" * len(text)
    return f"{text[:6]}{'.' * 8}{text[-4:]}"


def _mint(kind: str, seed: str) -> str:
    """A deterministic stand in for a token value, redacted on the way out.

    Deterministic so two runs of the same scenario produce the same evidence,
    and never returned in full so the console cannot print one.
    """
    digest = hashlib.sha256(f"{kind}:{seed}".encode("utf-8")).hexdigest()
    return redact(f"{kind}.{digest}")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def parse_time(value: str) -> datetime:
    """Read an RFC 3339 stamp, with or without the trailing Z."""
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shift(value: str, seconds: float) -> str:
    return iso(parse_time(value) + timedelta(seconds=seconds))


def seconds_between(start: str, end: str) -> float:
    return (parse_time(end) - parse_time(start)).total_seconds()


def human_duration(seconds: float) -> str:
    total = int(round(float(seconds)))
    if total <= 0:
        return "already expired"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs and not hours:
        parts.append(f"{secs}s")
    return " ".join(parts) or "0s"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpCall:
    """A fully described outbound request, built but never sent.

    The engine returns these so the console, the tests and a real broker all
    exercise the same construction path. Anything credential shaped in here has
    already been through redact().
    """
    method: str
    url: str
    headers: dict
    body: dict
    purpose: str
    encoding: str = "json"
    note: str = ""

    def as_json(self) -> str:
        return json.dumps(
            {"method": self.method, "url": self.url,
             "headers": self.headers, "body": self.body},
            indent=2, sort_keys=False,
        )

    def as_form(self) -> str:
        """The urlencoded body STS actually receives, one field per line.

        Rendered as lines rather than a single joined string because the
        interesting part is which fields are present, and options in
        particular is a JSON document squeezed into one form field.

        Encoded with quote_plus rather than quote, because that is what
        urlencode does and urlencode is what google-auth builds this body with.
        The difference shows up in exactly one field that matters: scope is
        space delimited, and a space is + on the wire, not %20.
        """
        return "\n".join(
            f"{key}={urllib.parse.quote_plus(str(value))}"
            for key, value in self.body.items()
        )


@dataclass(frozen=True)
class EntraToken:
    """The v1.0 access token an Azure managed identity gets from IMDS.

    Modelled by its claims rather than by a signature, because every check that
    matters downstream reads a claim: iss against the provider issuerUri, aud
    against allowedAudiences, exp and iat against the clock, sub against the
    attribute mapping, tid and oid against the attribute condition.
    """
    issuer: str
    subject: str
    audience: str
    tenant_id: str
    object_id: str
    application_id: str
    algorithm: str
    key_id: str
    issued_at: str
    expires_at: str
    redacted_value: str

    def claims(self) -> dict:
        """The assertion as the CEL keyword `assertion` sees it.

        Claims with an empty value are omitted, because an absent claim and a
        claim set to the empty string are different failures and the mapping
        has to be able to tell them apart.
        """
        raw = {
            "iss": self.issuer,
            "sub": self.subject,
            "aud": self.audience,
            "tid": self.tenant_id,
            "oid": self.object_id,
            "appid": self.application_id,
            "iat": self.issued_at,
            "exp": self.expires_at,
        }
        return {key: value for key, value in raw.items() if value != ""}


@dataclass(frozen=True)
class ProviderConfig:
    """One workload identity pool provider, as IAM stores it."""
    project_number: str
    pool_id: str
    provider_id: str
    issuer_uri: str
    attribute_mapping: dict
    attribute_condition: str = ""
    allowed_audiences: tuple[str, ...] = ()
    location: str = "global"

    @property
    def resource_name(self) -> str:
        return (
            f"projects/{self.project_number}/locations/{self.location}"
            f"/workloadIdentityPools/{self.pool_id}/providers/{self.provider_id}"
        )

    @property
    def audience(self) -> str:
        """The canonical audience string: leading double slash, no scheme."""
        return f"//iam.googleapis.com/{self.resource_name}"

    @property
    def https_audience(self) -> str:
        """The same name with the HTTPS prefix, which is also accepted."""
        return f"https://iam.googleapis.com/{self.resource_name}"

    @property
    def pool_resource(self) -> str:
        return (
            f"projects/{self.project_number}/locations/{self.location}"
            f"/workloadIdentityPools/{self.pool_id}"
        )


@dataclass(frozen=True)
class ServiceAccount:
    """The GCP service account the federated principal impersonates."""
    email: str
    project_id: str
    workload_identity_user: tuple[str, ...] = ()
    token_creator: tuple[str, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class Token:
    """A minted credential, described by its lifetime rather than its bytes."""
    kind: str
    redacted_value: str
    token_type: str
    issued_token_type: str
    issued_at: str
    expires_at: str
    scopes: tuple[str, ...]
    ceiling_reason: str

    @property
    def lifetime_seconds(self) -> float:
        return max(0.0, seconds_between(self.issued_at, self.expires_at))

    @property
    def lifetime_label(self) -> str:
        return human_duration(self.lifetime_seconds)


@dataclass(frozen=True)
class FlowStep:
    """One validated hop of the federation flow.

    `status` carries the four states the console renders, and `reason` is the
    named cause when it is not a pass. A step with nothing to say still says
    which rule it checked, because a check that reports nothing is a check
    nobody can audit.
    """
    index: int
    key: str
    title: str
    status: str
    detail: str
    reason: str = ""
    evidence: tuple[str, ...] = ()
    request: HttpCall | None = None
    token: Token | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_PASS


@dataclass(frozen=True)
class FederationRun:
    """The whole Azure to GCP flow, pass or fail, with its reason named."""
    steps: tuple[FlowStep, ...]
    ok: bool
    failed_step: str
    failure_reason: str
    entra_token: EntraToken
    sts_token: Token | None
    gcs_token: Token | None
    now: str
    fault: str = FAULT_NONE

    @property
    def passed(self) -> int:
        return len([s for s in self.steps if s.status == STATUS_PASS])

    @property
    def final_token(self) -> Token | None:
        return self.gcs_token

    def lifetime_proof(self) -> tuple[tuple[str, str, str], ...]:
        """Every credential in the chain with its lifetime and its ceiling.

        This is the evidence for the word "short lived". A claim about lifetime
        that is not a number next to the rule that bounds it is a slogan.
        """
        rows: list[tuple[str, str, str]] = [(
            "Entra ID assertion (IMDS)",
            human_duration(seconds_between(self.entra_token.issued_at,
                                           self.entra_token.expires_at)),
            "IMDS issues a managed identity token with expires_in 3599.",
        )]
        if self.sts_token is not None:
            rows.append(("GCP STS federated token",
                         self.sts_token.lifetime_label,
                         self.sts_token.ceiling_reason))
        if self.gcs_token is not None:
            rows.append(("Impersonated GCS access token",
                         self.gcs_token.lifetime_label,
                         self.gcs_token.ceiling_reason))
        return tuple(rows)


@dataclass(frozen=True)
class ConfigFinding:
    """One thing the provider configuration gets right or wrong."""
    key: str
    status: str
    title: str
    detail: str


@dataclass(frozen=True)
class AvailabilityCondition:
    """The google.type.Expr inside an access boundary rule."""
    expression: str
    title: str = ""
    description: str = ""

    def to_dict(self) -> dict:
        out = {"expression": self.expression}
        if self.title:
            out["title"] = self.title
        if self.description:
            out["description"] = self.description
        return out


@dataclass(frozen=True)
class AccessBoundaryRule:
    """One rule of a Credential Access Boundary.

    Note the asymmetry that breaks most hand written boundaries:
    availableResource carries the //storage.googleapis.com prefix, and the
    resource.name inside the CEL expression does not.
    """
    available_resource: str
    available_permissions: tuple[str, ...]
    availability_condition: AvailabilityCondition | None = None

    def to_dict(self) -> dict:
        out: dict = {
            "availableResource": self.available_resource,
            "availablePermissions": list(self.available_permissions),
        }
        if self.availability_condition is not None:
            out["availabilityCondition"] = self.availability_condition.to_dict()
        return out


@dataclass(frozen=True)
class AccessBoundary:
    """The full options envelope: accessBoundary wrapping accessBoundaryRules.

    Rules are unioned, never intersected. A second, tighter rule cannot claw
    back what a broad first rule already granted, which is why a boundary is
    reviewed as a whole rather than rule by rule.

    `tenant`, `bucket` and `prefix` are labels for the console and nothing
    more. No decision reads them. They are what the boundary was built from,
    and the only thing STS is ever sent is the rules, so a verdict taken from
    these fields would be a verdict about a string that never left the
    building: widen `prefix` and the token is unchanged, blank it and the
    rules still authorize exactly what they authorized.
    """
    rules: tuple[AccessBoundaryRule, ...]
    tenant: str = ""
    bucket: str = ""
    prefix: str = ""

    def to_dict(self) -> dict:
        return {"accessBoundary": {
            "accessBoundaryRules": [rule.to_dict() for rule in self.rules]}}

    def as_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @property
    def serialized_size(self) -> int:
        """Size of the value that goes into the options form field."""
        return len(json.dumps(self.to_dict(), separators=(",", ":")))


@dataclass(frozen=True)
class AccessAttempt:
    """One call a downscoped token is going to be used for.

    `list_prefix` is only read when the permission is a bucket listing. A LIST
    authorizes the bucket, so resource.name carries no object key to test and
    the only thing the boundary can gate on is the prefix the caller asked to
    list. Modelling it as its own field keeps that distinction explicit rather
    than inferring a listing from the shape of a path.
    """
    path: str
    permission: str
    label: str
    tenant: str = ""
    list_prefix: str = ""

    @property
    def is_list(self) -> bool:
        return self.permission == LIST_PERMISSION


@dataclass(frozen=True)
class AccessDecision:
    """The structured verdict for one path.

    Deliberately not a boolean. The reason is the product: a 403 that cannot
    say which clause refused it is indistinguishable from an outage, and a 200
    that cannot say which rule allowed it cannot be reviewed.
    """
    attempt: AccessAttempt
    allowed: bool
    status_code: int
    status_text: str
    reason: str
    bucket: str
    object_name: str
    resource_name: str
    rule_index: int = -1
    matched_prefix: str = ""
    naive_allowed: bool = False

    @property
    def prefix_confusion(self) -> bool:
        """True where a boundary without the trailing delimiter would leak.

        This is the single case the whole evaluator exists to catch: the
        guarded boundary denies, the naive one allows, and the difference is
        one character in a CEL string.
        """
        return self.naive_allowed and not self.allowed


@dataclass(frozen=True)
class DecisionRow:
    """One row of the Key Vault versus managed identity matrix."""
    resource: str
    platform: str
    verdict: str
    mechanism: str
    reason: str
    secret_before: bool
    secret_after: bool


@dataclass(frozen=True)
class KeyRisk:
    """One attack vector of a static service account JSON key on IIS."""
    key: str
    vector: str
    severity: str
    exploit: str
    blast_radius: str
    wif_replacement: str
    control: str


@dataclass(frozen=True)
class Kpis:
    steps_passed: int
    steps_total: int
    final_lifetime_seconds: float
    paths_denied: int
    cross_tenant_denied: int
    prefix_confusion_caught: int
    secrets_removed: int
    secrets_remaining: int
    critical_key_risks: int

    @property
    def short_lived(self) -> bool:
        return 0 < self.final_lifetime_seconds <= MAX_TOKEN_LIFETIME_SECONDS

    @property
    def secrets_total(self) -> int:
        return self.secrets_removed + self.secrets_remaining


# ---------------------------------------------------------------------------
# Sample fixtures
#
# One fixture set shared by the console and the tests, so a behaviour the
# console demonstrates is the same behaviour a test pins.
# ---------------------------------------------------------------------------

SAMPLE_NOW = "2026-09-14T09:00:00Z"

SAMPLE_TENANT_GUID = "9c4f8d21-6b17-4a53-8f2e-71d0c5a9e3b4"
SAMPLE_IDENTITY_OID = "3f7b2c68-41da-4e0c-9b55-8a2f6d147c90"
SAMPLE_IDENTITY_SUB = "b7d1e0a4-25c3-4f81-a6de-0c93b45e7f12"
SAMPLE_APP_ID = "0b1a77e5-9c34-4d2f-8e17-5ab6c0d92f38"

SAMPLE_PROVIDER = ProviderConfig(
    project_number="482913077461",
    pool_id="ehr-azure-pool",
    provider_id="entra-iis-prod",
    # IMDS issues v1.0 tokens, so the issuer is the sts.windows.net form and it
    # carries a trailing slash. The slash is the most likely single point of
    # failure when the provider is created, so it is pinned in the fixture.
    issuer_uri=f"https://sts.windows.net/{SAMPLE_TENANT_GUID}/",
    attribute_mapping={
        "google.subject": "assertion.sub",
        "attribute.tenant_id": "assertion.tid",
        "attribute.identity_oid": "assertion.oid",
    },
    # Evaluated against the raw assertion, so it can pin claims that were never
    # mapped. Pinning tid and oid is what stops any other identity in any other
    # Entra tenant from presenting a structurally valid token.
    attribute_condition=(
        f"assertion.tid == '{SAMPLE_TENANT_GUID}' "
        f"&& assertion.oid == '{SAMPLE_IDENTITY_OID}'"
    ),
    allowed_audiences=(),
)

SAMPLE_SERVICE_ACCOUNT = ServiceAccount(
    email="ehr-gcs-reader@ehr-prod-4821.iam.gserviceaccount.com",
    project_id="ehr-prod-4821",
    workload_identity_user=(
        f"principal://iam.googleapis.com/{SAMPLE_PROVIDER.pool_resource}"
        f"/subject/{SAMPLE_IDENTITY_SUB}",
    ),
    token_creator=(),
    description="Read only access to the EHR document bucket, no key issued.",
)

SAMPLE_ASSERTION = EntraToken(
    issuer=f"https://sts.windows.net/{SAMPLE_TENANT_GUID}/",
    subject=SAMPLE_IDENTITY_SUB,
    # Google's own library fixtures use the provider resource name as the IMDS
    # resource parameter, which lands in aud and matches an empty
    # allowedAudiences with no extra configuration.
    audience=SAMPLE_PROVIDER.audience,
    tenant_id=SAMPLE_TENANT_GUID,
    object_id=SAMPLE_IDENTITY_OID,
    application_id=SAMPLE_APP_ID,
    algorithm="RS256",
    key_id="kid-8f2a41c7",
    issued_at=SAMPLE_NOW,
    # IMDS returns expires_in 3599, as a string rather than a number.
    expires_at=shift(SAMPLE_NOW, 3599),
    redacted_value=_mint("entra", SAMPLE_IDENTITY_SUB),
)

SAMPLE_BUCKET = "ehr-storage"
SAMPLE_TENANTS: tuple[tuple[str, str], ...] = (
    ("tenant-a", "St Alban's Trust"),
    ("tenant-b", "Brackenmoor Health"),
)

# The paths the boundary is tested against. Four of these are the cases that
# make the feature worth building: the sibling prefix, the other tenant, the
# prefix buried mid string, and the bare prefix with no delimiter.
SAMPLE_ATTEMPTS: tuple[AccessAttempt, ...] = (
    AccessAttempt("gs://ehr-storage/tenants/tenant-a/patients/p-1001.json",
                  "storage.objects.get", "Own tenant, patient record", "tenant-a"),
    AccessAttempt("gs://ehr-storage/tenants/tenant-a/exports/2026-09/discharge.csv",
                  "storage.objects.get", "Own tenant, nested export", "tenant-a"),
    AccessAttempt("gs://ehr-storage/tenants/tenant-a-archive/patients/p-0042.json",
                  "storage.objects.get",
                  "Sibling prefix, the prefix confusion case", "tenant-a-archive"),
    AccessAttempt("gs://ehr-storage/tenants/tenant-b/patients/p-2001.json",
                  "storage.objects.get", "Cross tenant read", "tenant-b"),
    AccessAttempt("gs://ehr-storage/audit/tenants/tenant-a/access.log",
                  "storage.objects.get",
                  "Prefix present but not at the start", "tenant-a"),
    AccessAttempt("gs://ehr-storage/tenants/tenant-a",
                  "storage.objects.get",
                  "Bare prefix with no delimiter", "tenant-a"),
    AccessAttempt("gs://ehr-archive/tenants/tenant-a/patients/p-1001.json",
                  "storage.objects.get", "Right prefix, wrong bucket", "tenant-a"),
    AccessAttempt("gs://ehr-storage/tenants/tenant-a/patients/p-1001.json",
                  "storage.objects.delete",
                  "Own tenant, delete instead of read", "tenant-a"),
    # The listing case, which is the reason the boundary carries a second
    # clause at all. Its resource is the bucket, so the object clause cannot
    # authorize it and dropping the objectListPrefix clause turns this row
    # from a 200 into a 403.
    AccessAttempt("gs://ehr-storage", LIST_PERMISSION,
                  "Bucket listing confined to the tenant prefix", "tenant-a",
                  list_prefix="tenants/tenant-a/"),
)

# Only the roles the sample boundaries name. Kept small on purpose: the point
# is that availablePermissions names roles, and only the permissions inside
# those roles become available.
ROLE_PERMISSIONS: dict[str, frozenset] = {
    "roles/storage.objectViewer": frozenset({
        "storage.objects.get", "storage.objects.list",
    }),
    "roles/storage.objectCreator": frozenset({"storage.objects.create"}),
    "roles/storage.objectUser": frozenset({
        "storage.objects.get", "storage.objects.list",
        "storage.objects.create", "storage.objects.delete",
    }),
}

VERDICT_VAULT = "vault"
VERDICT_IDENTITY = "identity"
VERDICT_FEDERATION = "federation"

VERDICT_LABEL = {
    VERDICT_VAULT: "Key Vault is mandatory",
    VERDICT_IDENTITY: "Managed identity replaces the stored secret",
    VERDICT_FEDERATION: "Workload Identity Federation replaces the stored key",
}

VERDICT_TONE = {
    VERDICT_VAULT: "warn",
    VERDICT_IDENTITY: "ok",
    VERDICT_FEDERATION: "ok",
}

SAMPLE_DECISIONS: tuple[DecisionRow, ...] = (
    DecisionRow(
        resource="Azure SQL Database, clinical schema",
        platform="Azure",
        verdict=VERDICT_IDENTITY,
        mechanism=("Connection string with Authentication=Active Directory "
                   "Default and User Id set to the user assigned identity "
                   "client ID, plus a contained user created FROM EXTERNAL "
                   "PROVIDER"),
        reason=("The driver acquires an Entra access token for the SQL "
                "resource and presents it in place of a password. Microsoft "
                "documents that no password is required in this mode and that "
                "the Credential property cannot be set, so there is no secret "
                "left for a vault to hold."),
        secret_before=True,
        secret_after=False,
    ),
    DecisionRow(
        resource="Azure Blob Storage, document ingest",
        platform="Azure",
        verdict=VERDICT_IDENTITY,
        mechanism=("Entra OAuth token plus the Storage Blob Data Contributor "
                   "data role, with AllowSharedKeyAccess set to false"),
        reason=("Microsoft recommends Entra ID over Shared Key and supports "
                "disabling account key access outright, after which Shared Key "
                "requests fail with 403. That is what makes the stored account "
                "key dead rather than merely unused."),
        secret_before=True,
        secret_after=False,
    ),
    DecisionRow(
        resource="Azure Event Grid, clinical event publishing",
        platform="Azure",
        verdict=VERDICT_IDENTITY,
        mechanism=("Entra token with audience https://eventgrid.azure.net and "
                   "the EventGrid Data Sender role"),
        reason=("Event Grid validates the token audience and the sender role, "
                "which carries Microsoft.EventGrid/events/send/action. Key and "
                "SAS publishing is local authentication and can be disabled "
                "once every publisher has moved."),
        secret_before=True,
        secret_after=False,
    ),
    DecisionRow(
        resource="Azure Service Bus, HL7 message queue",
        platform="Azure",
        verdict=VERDICT_IDENTITY,
        mechanism=("Managed identity plus Azure Service Bus Data Sender or "
                   "Data Receiver, with SAS key authentication disabled on "
                   "the namespace"),
        reason=("Managed identities let the application authenticate without "
                "storing credentials, and SAS key authentication can be "
                "switched off namespace wide so only Entra authentication "
                "remains."),
        secret_before=True,
        secret_after=False,
    ),
    DecisionRow(
        resource="Google Cloud Storage, from the IIS host",
        platform="Azure to GCP",
        verdict=VERDICT_FEDERATION,
        mechanism=("Workload Identity Federation: Entra token from IMDS "
                   "exchanged at GCP STS, then impersonation of the target "
                   "service account"),
        reason=("Federation lets a workload outside Google Cloud use its "
                "existing identity instead of a Google issued private key, "
                "which removes the JSON key file from the web server disk "
                "entirely. There is nothing left to store, back up or leak."),
        secret_before=True,
        secret_after=False,
    ),
    DecisionRow(
        resource="Third party pathology results API",
        platform="External SaaS",
        verdict=VERDICT_VAULT,
        mechanism=("Key Vault secret, read at runtime by the application's "
                   "managed identity"),
        reason=("The platform cannot mint a credential for a vendor that does "
                "not federate with Entra. Microsoft's own guidance is to store "
                "the credential in Key Vault and use the managed identity to "
                "retrieve it, so the identity replaces the bootstrap secret "
                "rather than the stored one."),
        secret_before=True,
        secret_after=True,
    ),
    DecisionRow(
        resource="Client certificate for the HL7 mutual TLS partner",
        platform="External partner",
        verdict=VERDICT_VAULT,
        mechanism=("Certificate stored and lifecycle managed in Key Vault, "
                   "retrieved by the managed identity at runtime"),
        reason=("A private key the partner's trust store is pinned to is not "
                "something the platform can issue on demand. Key Vault is the "
                "certificate store and the rotation mechanism, and the managed "
                "identity is only the authentication to the vault."),
        secret_before=True,
        secret_after=True,
    ),
    DecisionRow(
        resource="Legacy SQL login on the on premise reporting server",
        platform="On premise",
        verdict=VERDICT_VAULT,
        mechanism=("Key Vault secret with a short rotation interval and an "
                   "audited break glass path"),
        reason=("The target does not support Entra authentication, so there is "
                "no token to present. The argument moves from whether a secret "
                "exists to where it lives, how short its life is and who can "
                "read it."),
        secret_before=True,
        secret_after=True,
    ),
    DecisionRow(
        resource="COTS analytics product requiring a pasted GCP key",
        platform="GCP",
        verdict=VERDICT_VAULT,
        mechanism=("Key held in Key Vault, issued against a dedicated service "
                   "account with an organization policy expiry, rotated on a "
                   "schedule"),
        reason=("Google names this as the honest exception: a key may be the "
                "only feasible option for a product that requires one pasted "
                "into its own interface. It is scoped to one service account "
                "so the blast radius is bounded and the rotation is owned."),
        secret_before=True,
        secret_after=True,
    ),
)

SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"

SEVERITY_TONE = {
    SEVERITY_CRITICAL: "crit",
    SEVERITY_HIGH: "warn",
    SEVERITY_MEDIUM: "info",
}

SEVERITY_ORDER = (SEVERITY_CRITICAL, SEVERITY_HIGH, SEVERITY_MEDIUM)

SAMPLE_KEY_RISKS: tuple[KeyRisk, ...] = (
    KeyRisk(
        key="file_exposure",
        vector="File traversal and backup exposure",
        severity=SEVERITY_CRITICAL,
        exploit=(
            "Application Default Credentials resolve the key from the "
            "GOOGLE_APPLICATION_CREDENTIALS path or from "
            "%APPDATA%\\gcloud\\application_default_credentials.json, so the "
            "credential is an ordinary file readable by the application pool "
            "identity. A traversal bug, a copied web root, a file level backup "
            "or a VM snapshot carries the whole credential out with it, and "
            "Google warns that keys end up in places they were never meant to "
            "be stored."
        ),
        blast_radius=(
            "Every permission the service account holds, from anywhere in the "
            "world, with nothing else required. Google states that unlike "
            "other credential files, a compromised service account key can be "
            "used by an attacker without any additional information."
        ),
        wif_replacement=(
            "There is no file. The credential is an Entra token IMDS issues to "
            "this host only, valid for under an hour, exchanged for a GCP "
            "token that expires with it. A stolen backup contains nothing to "
            "replay."
        ),
        control="constraints/iam.disableServiceAccountKeyCreation at the org root",
    ),
    KeyRisk(
        key="lifecycle",
        vector="Unmanaged lifecycle, a key that never expires",
        severity=SEVERITY_CRITICAL,
        exploit=(
            "A user managed service account key never expires by default. A "
            "key dropped onto this host years ago authenticates today. Nothing "
            "in the platform forces a rotation, so the only thing standing "
            "between the estate and a leaked key is somebody remembering, and "
            "Google's own recommendation is rotation at least every 90 days."
        ),
        blast_radius=(
            "Unbounded in time. Every copy ever taken of the file stays valid "
            "until a human notices and revokes it, including copies on "
            "laptops, in ticket attachments and in old images."
        ),
        wif_replacement=(
            "Lifetime is a property of the mechanism rather than of a process. "
            "The impersonated token is capped at one hour, and the assertion "
            "behind it is capped by its own exp claim, so expiry happens "
            "whether or not anybody is paying attention."
        ),
        control=("An organization policy expiry on any key that must exist, "
                 "and Service Account Key Exposure Response set to DISABLE_KEY"),
    ),
    KeyRisk(
        key="non_repudiation",
        vector="No identity non repudiation",
        severity=SEVERITY_HIGH,
        exploit=(
            "Every call made with the key logs as the service account. Where a "
            "workload authenticates as a service account with no human behind "
            "it, the audit log records only the service account, which Google "
            "names as a privilege escalation and non repudiation risk. In an "
            "EHR estate that is also a records problem: an access to patient "
            "data with no attributable actor."
        ),
        blast_radius=(
            "Any investigation stops at the service account. An attacker using "
            "a stolen key is indistinguishable in the log from the "
            "application's own legitimate traffic."
        ),
        wif_replacement=(
            "Impersonation restores the second identity. When short lived "
            "credentials are used to impersonate a service account, most "
            "Google Cloud services log both the service account and the "
            "identity that created the credential, so the federated principal "
            "appears in the log line alongside it."
        ),
        control=("Attribute mapping to google.subject, so the pool subject is "
                 "the value that appears in Cloud Logging"),
    ),
    KeyRisk(
        key="memory_dump",
        vector="Memory and crash dump extraction",
        severity=SEVERITY_HIGH,
        exploit=(
            "Windows Error Reporting can be configured to write local crash "
            "dumps, and DumpType 2 produces a full user mode memory dump of "
            "the process. A full dump of w3wp.exe is a copy of that process's "
            "memory, so key material loaded by the client library is inside "
            "it. Dumping process memory is a documented and actively exploited "
            "credential theft primitive on Windows."
        ),
        blast_radius=(
            "The dump lands in a directory on disk, is routinely collected by "
            "support tooling and is sometimes sent to a third party for "
            "analysis, which moves the credential outside the estate entirely."
        ),
        wif_replacement=(
            "What sits in memory is a token that expires within the hour and "
            "is bound to the audience it was minted for, not a permanent "
            "private key. A dump taken today is useless tomorrow."
        ),
        control=("Disable LocalDumps for the application pool, and treat any "
                 "dump of a credential handling process as an incident"),
    ),
)


# ---------------------------------------------------------------------------
# Provider configuration checks
# ---------------------------------------------------------------------------


def validate_provider(provider: ProviderConfig) -> tuple[ConfigFinding, ...]:
    """Check the provider against the constraints IAM actually enforces.

    Run before anything else in the console, because most federation failures
    are decided at configuration time and only discovered at exchange time.
    """
    findings: list[ConfigFinding] = []

    provider_id = provider.provider_id
    if re.fullmatch(r"[a-z0-9-]{4,32}", provider_id) and not provider_id.startswith("gcp-"):
        findings.append(ConfigFinding(
            "provider_id", STATUS_PASS, "Provider ID is well formed",
            f"{provider_id} is 4 to 32 characters of [a-z0-9-] and does not "
            f"use the reserved gcp- prefix."))
    else:
        findings.append(ConfigFinding(
            "provider_id", STATUS_FAIL, "Provider ID is not accepted",
            "A provider ID must be 4 to 32 characters from [a-z0-9-], and the "
            "gcp- prefix is reserved."))

    issuer = provider.issuer_uri
    if issuer.startswith("https://"):
        findings.append(ConfigFinding(
            "issuer_scheme", STATUS_PASS, "Issuer URI is HTTPS",
            "Google resolves issuerUri plus /.well-known/openid-configuration "
            "to find jwks_uri, and the endpoint must be HTTPS."))
    else:
        findings.append(ConfigFinding(
            "issuer_scheme", STATUS_FAIL, "Issuer URI is not HTTPS",
            "Oidc.issuerUri is required and must be an HTTPS endpoint."))

    # The three cases are decided separately rather than falling through to a
    # pass, because the only failure worth catching here is the one where the
    # issuer is a perfectly valid Entra URI of the wrong version. A check that
    # calls every issuer it does not recognise the v1.0 form is worse than no
    # check: it certifies the exact mistake it was written to find.
    lowered = issuer.lower()
    if "sts.windows.net" in lowered and issuer.endswith("/"):
        findings.append(ConfigFinding(
            "issuer_slash", STATUS_PASS, "Issuer URI matches the v1.0 form",
            "IMDS issues v1.0 tokens, whose iss is the sts.windows.net form "
            "with a trailing slash, and this issuerUri is that form."))
    elif "sts.windows.net" in lowered:
        findings.append(ConfigFinding(
            "issuer_slash", STATUS_WARN, "Issuer URI has no trailing slash",
            "An Entra tenant's discovery document returns the v1.0 issuer as "
            "https://sts.windows.net/TENANT_ID/ with a trailing slash, and "
            "IMDS issues v1.0 tokens. iss is compared byte for byte, so test "
            "both forms before going live."))
    elif "login.microsoftonline.com" in lowered:
        findings.append(ConfigFinding(
            "issuer_slash", STATUS_FAIL, "Issuer URI is the v2.0 form",
            "This is the v2.0 issuer. IMDS issues v1.0 tokens, whose iss is "
            "https://sts.windows.net/TENANT_ID/, so every token the managed "
            "identity presents is refused at the issuer comparison. This is "
            "the single most common way a provider is created wrong."))
    else:
        findings.append(ConfigFinding(
            "issuer_slash", STATUS_WARN, "Issuer URI is not an Entra form",
            "This check knows the two issuers an Entra tenant stamps, the "
            "v1.0 sts.windows.net form and the v2.0 login.microsoftonline.com "
            "form, and this is neither. Nothing here has verified which iss "
            "the issuer actually mints, so confirm it against the discovery "
            "document rather than reading this card as a pass."))

    mapping = dict(provider.attribute_mapping or {})
    if "google.subject" in mapping:
        findings.append(ConfigFinding(
            "subject_mapping", STATUS_PASS, "google.subject is mapped",
            f"An OIDC provider must supply a custom mapping including "
            f"google.subject. This one maps it to "
            f"{mapping['google.subject']}."))
    else:
        findings.append(ConfigFinding(
            "subject_mapping", STATUS_FAIL, "google.subject is not mapped",
            "For OIDC providers the custom mapping must include "
            "google.subject, and no other mapping compensates for it."))

    bad_keys = [key for key in mapping
                if key.startswith("attribute.")
                and not re.fullmatch(r"[a-z0-9_]{1,100}", key.split(".", 1)[1])]
    if bad_keys:
        findings.append(ConfigFinding(
            "attribute_keys", STATUS_FAIL, "Custom attribute key is invalid",
            f"A mapped attribute key may contain only [a-z0-9_] and may be at "
            f"most 100 characters. Rejected: {', '.join(sorted(bad_keys))}."))
    else:
        findings.append(ConfigFinding(
            "attribute_keys", STATUS_PASS, "Custom attribute keys are valid",
            "Every custom key is [a-z0-9_] within 100 characters, and the "
            "provider is well inside the limit of 50 custom attributes."))

    condition = provider.attribute_condition or ""
    if len(condition) > MAX_ATTRIBUTE_CONDITION_CHARS:
        findings.append(ConfigFinding(
            "condition_length", STATUS_FAIL, "Attribute condition is too long",
            f"The maximum length of an attribute condition is "
            f"{MAX_ATTRIBUTE_CONDITION_CHARS} characters."))
    elif condition:
        findings.append(ConfigFinding(
            "condition_length", STATUS_PASS, "Attribute condition is present",
            f"{len(condition)} of {MAX_ATTRIBUTE_CONDITION_CHARS} characters "
            f"used. Without a condition, every valid credential the issuer "
            f"mints is accepted."))
    else:
        findings.append(ConfigFinding(
            "condition_length", STATUS_WARN, "No attribute condition set",
            "If the condition is unspecified, all valid credentials from the "
            "issuer are accepted, including identities in the same Entra "
            "tenant that were never meant to reach this pool."))

    audiences = tuple(provider.allowed_audiences or ())
    # Each branch checks the thing the sentence underneath it claims. The
    # /.default clause in particular was a comment describing a trap while the
    # card passed any list at all, including a list made entirely of the trap.
    oversized = [aud for aud in audiences if len(aud) > MAX_AUDIENCE_CHARS]
    defaulted = [aud for aud in audiences
                 if aud.endswith(DEFAULT_SCOPE_SUFFIX)]
    if len(audiences) > MAX_ALLOWED_AUDIENCES:
        findings.append(ConfigFinding(
            "audience", STATUS_FAIL, "Too many allowedAudiences entries",
            f"At most {MAX_ALLOWED_AUDIENCES} audiences may be configured, "
            f"and this provider names {len(audiences)}."))
    elif oversized:
        findings.append(ConfigFinding(
            "audience", STATUS_FAIL, "An allowedAudiences entry is too long",
            f"Each audience may be at most {MAX_AUDIENCE_CHARS} characters. "
            f"Rejected: {', '.join(f'{len(aud)} characters' for aud in oversized)}."))
    elif defaulted:
        findings.append(ConfigFinding(
            "audience", STATUS_FAIL,
            "An allowedAudiences entry still carries /.default",
            f"ManagedIdentityCredential strips {DEFAULT_SCOPE_SUFFIX} before "
            f"calling IMDS, so the bare form is what lands in aud and an entry "
            f"that keeps the suffix can never match. Rejected: "
            f"{', '.join(defaulted)}."))
    elif audiences:
        findings.append(ConfigFinding(
            "audience", STATUS_PASS, "allowedAudiences is set explicitly",
            f"{len(audiences)} of {MAX_ALLOWED_AUDIENCES} audiences, each "
            f"within {MAX_AUDIENCE_CHARS} characters and none of them ending "
            f"in {DEFAULT_SCOPE_SUFFIX}. The aud claim must match one of them "
            f"exactly."))
    else:
        findings.append(ConfigFinding(
            "audience", STATUS_PASS, "allowedAudiences is empty, so the "
                                     "canonical name applies",
            "With an empty list the token audience must equal the provider's "
            "full canonical resource name, with or without the HTTPS prefix. "
            "Using that name as the IMDS resource needs no extra config."))

    return tuple(findings)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def imds_request(resource: str, client_id: str = "") -> HttpCall:
    """The IMDS call ManagedIdentityCredential makes on an Azure VM.

    The resource parameter is the whole audience story: it lands directly in
    the aud claim of the issued v1.0 token, which is the value GCP checks
    against allowedAudiences.

    Two spellings of that resource both satisfy GCP's empty allowedAudiences
    rule, and they are not equally attested. Google's own Azure flow fixture,
    url_sourced_external_account_credential.json in google-api-dotnet-client,
    uses the https prefixed form
    https://iam.googleapis.com/projects/.../providers/... . The scheme less
    form //iam.googleapis.com/projects/... appears in google-cloud-rust's
    example. The console passes the scheme less form because it is the
    canonical resource name the provider reports, which keeps one string in
    the configuration; if an Entra App ID URI has to be registered for it,
    the https form is the better attested choice.
    """
    params = {"api-version": IMDS_API_VERSION, "resource": resource}
    if client_id:
        params["client_id"] = client_id
    return HttpCall(
        method="GET",
        url=f"{IMDS_TOKEN_URL}?{urllib.parse.urlencode(params)}",
        headers={"Metadata": "true"},
        body={},
        purpose="Acquire an Entra ID token for the workload identity pool",
        encoding="query",
        note=("The Metadata header is required and must be the literal string "
              "true. It is the documented mitigation against server side "
              "request forgery, not a formality."),
    )


def sts_exchange_request(assertion: EntraToken, provider: ProviderConfig,
                         scope: str = SCOPE_IAM) -> HttpCall:
    """The STS token exchange, as an urlencoded form.

    Two fields look alike and mean different things, which is the most common
    configuration mistake in this flow. `audience` identifies the provider to
    STS. The aud claim inside `subject_token` is what the provider checks
    against allowedAudiences. They happen to hold the same string here only
    because the IMDS resource was set to the provider resource name.
    """
    return HttpCall(
        method="POST",
        url=STS_TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body={
            "grant_type": GRANT_TYPE_TOKEN_EXCHANGE,
            "audience": provider.audience,
            "scope": scope,
            "requested_token_type": TOKEN_TYPE_ACCESS_TOKEN,
            "subject_token": assertion.redacted_value,
            "subject_token_type": TOKEN_TYPE_JWT,
        },
        purpose="Exchange the Entra assertion for a GCP federated access token",
        encoding="form",
        note=STS_NO_AUTH_NOTE,
    )


def generate_access_token_request(
        service_account: ServiceAccount, federated_token: Token,
        scopes: tuple[str, ...] = (SCOPE_GCS_READ,),
        lifetime_seconds: int = DEFAULT_TOKEN_LIFETIME_SECONDS) -> HttpCall:
    """The impersonation call, carrying the federated token as the bearer.

    Note the shape changes between the two legs: STS takes a urlencoded form
    with a space delimited scope string, and this takes JSON with scope as an
    array. Copying one body into the other is a silent 400.
    """
    return HttpCall(
        method="POST",
        url=IAM_CREDENTIALS_URL.format(service_account=service_account.email),
        headers={
            "Authorization": f"Bearer {federated_token.redacted_value}",
            "Content-Type": "application/json",
        },
        body={
            "scope": list(scopes),
            "lifetime": f"{int(lifetime_seconds)}s",
        },
        purpose=("Impersonate the target service account and mint a short "
                 "lived GCS access token"),
        note=("The '-' project wildcard in the path is required. Replacing it "
              "with a project ID is invalid. `delegates` is omitted because "
              "this is a direct request, not a delegation chain."),
    )


def sts_downscope_request(source_token: Token,
                          boundary: AccessBoundary) -> HttpCall:
    """The second STS exchange that applies a Credential Access Boundary.

    Neither `audience` nor `scope` is sent here: those belong to the flow that
    exchanges an external credential. This one downscopes a Google token that
    already exists, and the whole boundary rides in the `options` field as one
    serialized JSON document.
    """
    return HttpCall(
        method="POST",
        url=STS_TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body={
            "grant_type": GRANT_TYPE_TOKEN_EXCHANGE,
            "subject_token_type": TOKEN_TYPE_ACCESS_TOKEN,
            "requested_token_type": TOKEN_TYPE_ACCESS_TOKEN,
            "subject_token": source_token.redacted_value,
            "options": json.dumps(boundary.to_dict(), separators=(",", ":")),
        },
        purpose=("Downscope the token to one tenant's object prefix before it "
                 "ever reaches the request handler"),
        encoding="form",
        note=STS_NO_AUTH_NOTE,
    )


# ---------------------------------------------------------------------------
# A. Workload Identity Federation simulator
# ---------------------------------------------------------------------------


def principal_member(provider: ProviderConfig, subject: str) -> str:
    """The IAM member string a mapped google.subject becomes.

    principal:// is singular: it names one subject. Custom attributes and
    groups become principalSet://, which names a set, and confusing the two is
    how a binding silently matches nobody.
    """
    return (f"principal://iam.googleapis.com/{provider.pool_resource}"
            f"/subject/{subject}")


def principal_set_member(provider: ProviderConfig, attribute: str,
                         value: str) -> str:
    return (f"principalSet://iam.googleapis.com/{provider.pool_resource}"
            f"/attribute.{attribute}/{value}")


def audience_accepted(assertion: EntraToken,
                      provider: ProviderConfig) -> tuple[bool, str]:
    """Check the aud claim the way an OIDC provider does.

    With allowedAudiences empty the aud must equal the provider's full
    canonical resource name, with or without the HTTPS prefix. With the list
    populated, aud must match one entry exactly.
    """
    aud = assertion.audience
    if provider.allowed_audiences:
        if aud in provider.allowed_audiences:
            return True, (f"aud matches a configured allowedAudiences entry: "
                          f"{aud}")
        return False, (
            f"aud is {aud or 'empty'}, which is not in allowedAudiences "
            f"({', '.join(provider.allowed_audiences)}). Note that "
            f"ManagedIdentityCredential strips a /.default suffix before "
            f"calling IMDS, so the bare form is what arrives here.")
    if aud in (provider.audience, provider.https_audience):
        return True, (f"allowedAudiences is empty, and aud equals the "
                      f"provider's canonical resource name: {aud}")
    return False, (
        f"allowedAudiences is empty, so aud must equal {provider.audience} "
        f"(or the same name with the https: prefix). The token carries "
        f"{aud or 'no aud claim'}.")


def issuer_accepted(assertion: EntraToken,
                    provider: ProviderConfig) -> tuple[bool, str]:
    """Compare iss to issuerUri byte for byte, trailing slash included."""
    if assertion.issuer == provider.issuer_uri:
        return True, f"iss matches the provider issuerUri exactly: {assertion.issuer}"
    return False, (
        f"iss is {assertion.issuer or 'empty'} and the provider issuerUri is "
        f"{provider.issuer_uri}. These are compared as strings, so a missing "
        f"or extra trailing slash is a rejection.")


def assertion_clock_ok(assertion: EntraToken,
                       now: str) -> tuple[bool, str]:
    """Apply the exp and iat rules STS documents for the subject token."""
    moment = parse_time(now)
    expires = parse_time(assertion.expires_at)
    issued = parse_time(assertion.issued_at)
    if expires <= moment:
        return False, (
            f"The assertion expired at {assertion.expires_at} and the exchange "
            f"is being attempted at {now}. An expired subject token is "
            f"refused, which is the mechanism doing its job.")
    if issued > moment:
        return False, (
            f"iat is {assertion.issued_at}, which is in the future relative to "
            f"{now}. iat must be in the past.")
    age = (moment - issued).total_seconds()
    if age > MAX_ASSERTION_AGE_SECONDS:
        return False, (
            f"iat is {human_duration(age)} in the past. It must be no more "
            f"than 24 hours in the past or the token is rejected.")
    remaining = (expires - moment).total_seconds()
    lifetime = (expires - issued).total_seconds()
    # The 6 hour figure is a recommendation rather than a rule, so a longer
    # token is still accepted. What it must not do is claim compliance with a
    # bound it just broke, which is what an unconditional sentence here did.
    if lifetime <= RECOMMENDED_ASSERTION_LIFETIME_SECONDS:
        verdict = (f"inside the recommended maximum of "
                   f"{human_duration(RECOMMENDED_ASSERTION_LIFETIME_SECONDS)}")
    else:
        verdict = (f"beyond the recommended maximum of "
                   f"{human_duration(RECOMMENDED_ASSERTION_LIFETIME_SECONDS)}, "
                   f"which STS still accepts and a reviewer should still "
                   f"question")
    return True, (
        f"Valid for another {human_duration(remaining)}. Total lifetime is "
        f"{human_duration(lifetime)}, {verdict}.")


def evaluate_attribute_mapping(assertion: EntraToken,
                               provider: ProviderConfig) -> tuple[dict, str]:
    """Apply the attribute mapping to the assertion.

    Returns the mapped attributes and an empty reason on success, or an empty
    mapping and a named reason on failure. Only `assertion.<claim>` expressions
    are modelled, and anything else is refused rather than guessed at: a
    mapping this module cannot evaluate is a mapping it must not pretend to
    have evaluated.
    """
    claims = assertion.claims()
    mapped: dict = {}
    for key, expression in sorted((provider.attribute_mapping or {}).items()):
        text = str(expression).strip()
        match = re.fullmatch(r"assertion\.([A-Za-z_][A-Za-z0-9_]*)", text)
        if not match:
            return {}, (
                f"The mapping for {key} is {text!r}. This engine models only "
                f"assertion.<claim> expressions and refuses anything it cannot "
                f"evaluate rather than assuming it would have passed.")
        claim = match.group(1)
        if claim not in claims:
            return {}, (
                f"The mapping for {key} reads assertion.{claim}, and the "
                f"assertion carries no {claim} claim. Present claims: "
                f"{', '.join(sorted(claims))}.")
        mapped[key] = str(claims[claim])

    if "google.subject" not in mapped:
        return {}, ("The mapping produced no google.subject. For an OIDC "
                    "provider the custom mapping must include it.")
    subject = mapped["google.subject"]
    if len(subject.encode("utf-8")) > MAX_GOOGLE_SUBJECT_BYTES:
        return {}, (
            f"google.subject is {len(subject.encode('utf-8'))} bytes and "
            f"cannot exceed {MAX_GOOGLE_SUBJECT_BYTES}. Map a GUID rather "
            f"than a verbose claim.")
    return mapped, ""


def evaluate_attribute_condition(expression: str, assertion: EntraToken,
                                 mapped: dict) -> tuple[bool, str]:
    """Evaluate the attribute condition against the raw assertion.

    A modelled subset of CEL: clauses joined by && , each one an equality or
    inequality against a single quoted literal, referencing assertion.*,
    google.* or attribute.*. Anything outside that subset fails closed, so a
    condition this engine cannot read never reads as an allow.
    """
    text = str(expression or "").strip()
    if not text:
        return True, ("No attribute condition is set, so every valid "
                      "credential from this issuer is accepted.")

    claims = assertion.claims()
    for clause in [part.strip() for part in text.split("&&")]:
        match = re.fullmatch(r"(\S+)\s*(==|!=)\s*'([^']*)'", clause)
        if not match:
            return False, (
                f"The clause {clause!r} is outside the subset this engine "
                f"models, so it is treated as a refusal rather than an "
                f"allow.")
        reference, operator, literal = match.groups()
        if reference.startswith("assertion."):
            claim = reference.split(".", 1)[1]
            if claim not in claims:
                return False, (
                    f"The condition reads {reference} and the assertion "
                    f"carries no {claim} claim.")
            actual = str(claims[claim])
        elif reference in mapped:
            actual = str(mapped[reference])
        else:
            return False, (
                f"The condition references {reference}, which is neither an "
                f"assertion claim nor a mapped attribute.")
        matches = actual == literal
        if (operator == "==" and not matches) or (operator == "!=" and matches):
            return False, (
                f"The condition requires {reference} {operator} '{literal}' "
                f"and the value presented is '{actual}'. The principal is "
                f"refused at the pool, before any IAM binding is consulted.")
    return True, f"Every clause of the attribute condition is satisfied: {text}"


def federated_token_lifetime(assertion: EntraToken, now: str) -> tuple[int, str]:
    """Bound the STS token by the clock and by the input token.

    The federated token is modelled as capped at one hour, and it is
    additionally bounded by the assertion's own remaining life. An Entra IMDS
    token starts at 3599 seconds, so the result is an hour or less, never more.

    Sourcing note: the one hour cap on the STS leg is reconstructed from
    search extracts and from client library behaviour rather than read from
    Google's own page, which was not reachable when this was written. The
    bound that is directly attested is the one on the impersonated token, and
    the assertion bound below is arithmetic on the token in hand. Treat the
    STS ceiling as the claim to confirm first.
    """
    remaining = int(max(0.0, seconds_between(now, assertion.expires_at)))
    lifetime = min(MAX_TOKEN_LIFETIME_SECONDS, remaining)
    if remaining <= MAX_TOKEN_LIFETIME_SECONDS:
        reason = (f"Bounded by the assertion, which has {human_duration(remaining)} "
                  f"left. The exchange cannot outlive its input.")
    else:
        reason = (f"Capped at the one hour ceiling on a federated token, even "
                  f"though the assertion has {human_duration(remaining)} left.")
    return lifetime, reason


def impersonated_token_lifetime(requested_seconds: int,
                                federated: Token, now: str) -> tuple[int, str]:
    """Bound the impersonated token, one hour by default and by policy."""
    requested = int(max(0, requested_seconds))
    federated_remaining = int(max(0.0, seconds_between(now, federated.expires_at)))
    if requested > MAX_TOKEN_LIFETIME_SECONDS:
        lifetime = MAX_TOKEN_LIFETIME_SECONDS
        reason = (f"A lifetime of {human_duration(requested)} was requested. "
                  f"The maximum is one hour unless the service account is an "
                  f"allowed value in an organization policy enforcing "
                  f"{LIFETIME_EXTENSION_CONSTRAINT}.")
        return lifetime, reason
    lifetime = min(requested or DEFAULT_TOKEN_LIFETIME_SECONDS,
                   MAX_TOKEN_LIFETIME_SECONDS)
    reason = (f"Requested {human_duration(lifetime)}, inside the one hour "
              f"default maximum. The federated bearer behind it has "
              f"{human_duration(federated_remaining)} left.")
    return lifetime, reason


def _skip(index: int, key: str, title: str, because: str) -> FlowStep:
    return FlowStep(index=index, key=key, title=title, status=STATUS_SKIP,
                    detail="Not attempted.", reason=because)


def run_federation(assertion: EntraToken, provider: ProviderConfig,
                   service_account: ServiceAccount, now: str = SAMPLE_NOW,
                   target_scopes: tuple[str, ...] = (SCOPE_GCS_READ,),
                   lifetime_seconds: int = DEFAULT_TOKEN_LIFETIME_SECONDS,
                   fault: str = FAULT_NONE) -> FederationRun:
    """Walk the whole Azure to GCP flow, stopping at the first real failure.

    Nothing here is decorative. Every step either satisfies a rule that a real
    provider enforces or it fails with the name of the rule it broke, and once
    one fails the rest are skipped rather than reported as passing on inputs
    that never reached them.
    """
    steps: list[FlowStep] = []
    failure_key = ""
    failure_reason = ""
    sts_token: Token | None = None
    gcs_token: Token | None = None

    # 1. The Azure side. ManagedIdentityCredential selects its source from the
    # environment and only falls back to IMDS, so the step names the assumption
    # rather than hiding it.
    imds = imds_request(assertion.audience)
    steps.append(FlowStep(
        index=1, key="imds", title="ManagedIdentityCredential acquires an Entra token",
        status=STATUS_PASS,
        detail=(f"IMDS returns a v1.0 access token for the managed identity, "
                f"expires_in 3599 as a string. The resource parameter lands in "
                f"aud, which is why it is set to the provider resource name."),
        evidence=(
            f"iss: {assertion.issuer}",
            f"aud: {assertion.audience}",
            f"sub: {assertion.subject}",
            f"tid: {assertion.tenant_id}",
            f"oid: {assertion.object_id}",
            f"alg: {assertion.algorithm}, kid: {assertion.key_id}",
            f"token: {assertion.redacted_value}",
        ),
        request=imds,
    ))

    # 2. STS validates the assertion itself: algorithm, issuer, audience, clock.
    alg_ok = assertion.algorithm in ACCEPTED_ASSERTION_ALGORITHMS
    iss_ok, iss_reason = issuer_accepted(assertion, provider)
    aud_ok, aud_reason = audience_accepted(assertion, provider)
    clock_ok, clock_reason = assertion_clock_ok(assertion, now)

    if not alg_ok:
        ok, reason = False, (
            f"The JWT header declares alg {assertion.algorithm}. The signing "
            f"algorithm must be RS256 or ES256.")
    elif not iss_ok:
        ok, reason = False, iss_reason
    elif not aud_ok:
        ok, reason = False, aud_reason
    elif not clock_ok:
        ok, reason = False, clock_reason
    else:
        ok, reason = True, ""

    steps.append(FlowStep(
        index=2, key="assertion", title="GCP STS validates the assertion",
        status=STATUS_PASS if ok else STATUS_FAIL,
        detail=("The provider checks the signature algorithm, resolves "
                "issuerUri to the JWKS, then compares iss, aud, iat and exp."),
        reason=reason,
        evidence=(
            f"alg {assertion.algorithm}: "
            f"{'accepted' if alg_ok else 'rejected, must be RS256 or ES256'}",
            f"issuer: {iss_reason}",
            f"audience: {aud_reason}",
            f"clock: {clock_reason}",
        ),
    ))
    if not ok:
        failure_key, failure_reason = "assertion", reason

    # 3. Attribute mapping.
    mapped: dict = {}
    if failure_key:
        steps.append(_skip(3, "mapping", "Pool applies the attribute mapping",
                           "The assertion was refused before mapping."))
    else:
        mapped, map_reason = evaluate_attribute_mapping(assertion, provider)
        mapped_ok = not map_reason
        steps.append(FlowStep(
            index=3, key="mapping", title="Pool applies the attribute mapping",
            status=STATUS_PASS if mapped_ok else STATUS_FAIL,
            detail=("Each mapping expression is evaluated against the "
                    "assertion. google.subject is the value that becomes the "
                    "IAM principal and the subject in Cloud Logging."),
            reason=map_reason,
            evidence=tuple(
                [f"{key} = {expr}" for key, expr in
                 sorted((provider.attribute_mapping or {}).items())]
                + ([f"principal: {principal_member(provider, mapped['google.subject'])}"]
                   if mapped_ok else [])),
        ))
        if not mapped_ok:
            failure_key, failure_reason = "mapping", map_reason

    # 4. Attribute condition.
    if failure_key:
        steps.append(_skip(4, "condition", "Pool evaluates the attribute condition",
                           "The mapping did not complete, so there was nothing "
                           "to evaluate the condition against."))
    else:
        cond_ok, cond_reason = evaluate_attribute_condition(
            provider.attribute_condition, assertion, mapped)
        steps.append(FlowStep(
            index=4, key="condition", title="Pool evaluates the attribute condition",
            status=STATUS_PASS if cond_ok else STATUS_FAIL,
            detail=("The condition runs against the raw assertion, so it can "
                    "pin claims that were never mapped. Pinning tid and oid is "
                    "what stops any other identity in the tenant."),
            reason=cond_reason,
            evidence=(provider.attribute_condition or "no condition set",),
        ))
        if not cond_ok:
            failure_key, failure_reason = "condition", cond_reason

    # 5. The exchange itself.
    if failure_key:
        steps.append(_skip(5, "exchange", "STS issues the federated access token",
                           "No token is issued for a principal the pool refused."))
    else:
        lifetime, ceiling = federated_token_lifetime(assertion, now)
        sts_token = Token(
            kind="GCP STS federated access token",
            redacted_value=_mint("sts", f"{assertion.subject}:{now}"),
            token_type="Bearer",
            issued_token_type=TOKEN_TYPE_ACCESS_TOKEN,
            issued_at=now,
            expires_at=shift(now, lifetime),
            scopes=(SCOPE_IAM,),
            ceiling_reason=ceiling,
        )
        steps.append(FlowStep(
            index=5, key="exchange", title="STS issues the federated access token",
            status=STATUS_PASS,
            detail=("The response is snake_case JSON carrying access_token, "
                    "token_type Bearer, issued_token_type and expires_in. The "
                    "STS leg requests the IAM scope because the caller's real "
                    "scopes are passed to generateAccessToken instead."),
            evidence=(
                "token_type: Bearer",
                f"issued_token_type: {TOKEN_TYPE_ACCESS_TOKEN}",
                f"expires_in: {lifetime}",
                f"access_token: {sts_token.redacted_value}",
                ceiling,
            ),
            request=sts_exchange_request(assertion, provider),
            token=sts_token,
        ))

    # 6. The IAM binding on the target service account.
    if failure_key or sts_token is None:
        steps.append(_skip(6, "binding", "Target service account checks the binding",
                           "No federated principal reached the service account."))
    else:
        member = principal_member(provider, mapped["google.subject"])
        bound = member in service_account.workload_identity_user
        steps.append(FlowStep(
            index=6, key="binding",
            title="Target service account checks the binding",
            status=STATUS_PASS if bound else STATUS_FAIL,
            detail=(f"Impersonation requires the federated principal to be "
                    f"bound on the service account resource itself, not at "
                    f"project level."),
            reason="" if bound else (
                f"{member} does not hold {ROLE_WORKLOAD_IDENTITY_USER} on "
                f"{service_account.email}. Federation succeeded and "
                f"impersonation is refused, which is the correct order: a "
                f"valid identity is still not an authorized one."),
            evidence=(
                f"member: {member}",
                f"role: {ROLE_WORKLOAD_IDENTITY_USER}",
                f"bound members: "
                f"{', '.join(service_account.workload_identity_user) or 'none'}",
                f"related role carrying iam.serviceAccounts.getAccessToken: "
                f"{ROLE_TOKEN_CREATOR}",
            ),
        ))
        if not bound:
            failure_key = "binding"
            failure_reason = (
                f"The principal is not bound with {ROLE_WORKLOAD_IDENTITY_USER} "
                f"on {service_account.email}.")

    # 7. Impersonation.
    if failure_key or sts_token is None:
        steps.append(_skip(7, "impersonate",
                           "IAM Credentials mints the GCS access token",
                           "Impersonation was never attempted."))
    else:
        lifetime, ceiling = impersonated_token_lifetime(
            lifetime_seconds, sts_token, now)
        gcs_token = Token(
            kind="Impersonated GCS access token",
            redacted_value=_mint("gcs", f"{service_account.email}:{now}"),
            token_type="Bearer",
            issued_token_type=TOKEN_TYPE_ACCESS_TOKEN,
            issued_at=now,
            expires_at=shift(now, lifetime),
            scopes=tuple(target_scopes),
            ceiling_reason=ceiling,
        )
        steps.append(FlowStep(
            index=7, key="impersonate",
            title="IAM Credentials mints the GCS access token",
            status=STATUS_PASS,
            detail=("generateAccessToken returns camelCase accessToken and "
                    "expireTime, and the expiration time is always set. This "
                    "is the credential the storage client uses, and it is the "
                    "only credential that ever touches the web tier."),
            evidence=(
                f"accessToken: {gcs_token.redacted_value}",
                f"expireTime: {gcs_token.expires_at}",
                f"scopes: {', '.join(target_scopes)}",
                ceiling,
            ),
            request=generate_access_token_request(
                service_account, sts_token, tuple(target_scopes), lifetime),
            token=gcs_token,
        ))

    return FederationRun(
        steps=tuple(steps),
        ok=not failure_key,
        failed_step=failure_key,
        failure_reason=failure_reason,
        entra_token=assertion,
        sts_token=sts_token,
        gcs_token=gcs_token,
        now=now,
        fault=fault,
    )


def scenario_inputs(fault: str, now: str = SAMPLE_NOW
                    ) -> tuple[EntraToken, ProviderConfig, ServiceAccount]:
    """Build the inputs for a named fault, without touching the flow logic.

    The faults live here rather than inside run_federation on purpose: a
    simulator that can be told to report a failure proves nothing. This one
    breaks the input and lets the same unmodified checks find it.
    """
    assertion = SAMPLE_ASSERTION
    provider = SAMPLE_PROVIDER
    account = SAMPLE_SERVICE_ACCOUNT

    if fault == FAULT_AUDIENCE:
        # The classic mismatch, modelled on both sides rather than described.
        # allowedAudiences keeps the /.default form somebody pasted out of an
        # app registration, and the token carries the bare form, because
        # ManagedIdentityCredential strips the suffix before calling IMDS.
        # Two strings that differ by nine characters nobody can see.
        assertion = replace(assertion, audience="api://ehr-gcp-federation")
        provider = replace(
            provider,
            allowed_audiences=(
                f"api://ehr-gcp-federation{DEFAULT_SCOPE_SUFFIX}",))
    elif fault == FAULT_ISSUER:
        # The v2.0 issuer configured against a v1.0 token from IMDS.
        provider = replace(
            provider,
            issuer_uri=f"https://login.microsoftonline.com/{SAMPLE_TENANT_GUID}/v2.0")
    elif fault == FAULT_EXPIRED:
        assertion = replace(assertion,
                            issued_at=shift(now, -7200),
                            expires_at=shift(now, -3601))
    elif fault == FAULT_SUBJECT:
        assertion = replace(assertion, subject="")
    elif fault == FAULT_CONDITION:
        # A different managed identity in the same Entra tenant: the token is
        # genuine, the mapping works, and the pool still refuses it.
        other = "a1c95e07-3b64-4f92-8d10-6e7f2b8c4d55"
        assertion = replace(assertion, object_id=other,
                            subject="f0e8d7c6-1234-4a9b-8c7d-2e5f9a0b3c41")
    elif fault == FAULT_BINDING:
        account = replace(account, workload_identity_user=())

    return assertion, provider, account


def run_scenario(fault: str = FAULT_NONE, now: str = SAMPLE_NOW,
                 lifetime_seconds: int = DEFAULT_TOKEN_LIFETIME_SECONDS
                 ) -> FederationRun:
    """Run the flow under a named fault, or clean when the fault is none."""
    assertion, provider, account = scenario_inputs(fault, now)
    return run_federation(assertion, provider, account, now=now,
                          lifetime_seconds=lifetime_seconds, fault=fault)


# ---------------------------------------------------------------------------
# B. Multi tenant Credential Access Boundary evaluator
# ---------------------------------------------------------------------------

TENANT_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


def validate_tenant_id(tenant: str) -> tuple[bool, str]:
    """Refuse anything that could escape a CEL string literal.

    The boundary expression is built by concatenation, so a tenant identifier
    containing a quote can close the literal and append arbitrary CEL. A value
    like x') || true || (' turns the condition into a tautology that grants the
    entire bucket. An allowlist shaped identifier is the cheap defence.
    """
    text = str(tenant or "")
    if not text:
        return False, "A tenant identifier is required."
    if not TENANT_ID_PATTERN.fullmatch(text):
        return False, (
            f"{text!r} is not an accepted tenant identifier. Only lowercase "
            f"letters, digits and hyphens are allowed, because this value is "
            f"interpolated into a CEL string literal and a quote in it would "
            f"rewrite the boundary.")
    return True, f"{text} is a safe literal to interpolate."


def cel_quote(value: str) -> str:
    """Quote a value as a CEL string literal, escaping what can break out.

    Google's libraries take the expression as an opaque string and escape
    nothing for you, so escaping is the caller's job or it is nobody's.
    """
    escaped = (str(value)
               .replace("\\", "\\\\")
               .replace("'", "\\'")
               .replace("\n", "\\n")
               .replace("\r", "\\r"))
    return f"'{escaped}'"


# One CEL single quoted string literal, escapes included. Every place that
# reads a boundary expression uses this one pattern, because a second,
# slightly different idea of where a literal ends is how a validator comes to
# disagree with the evaluator about what the boundary says.
CEL_LITERAL = r"'((?:[^'\\]|\\.)*)'"

CEL_UNESCAPE = {"\\": "\\", "'": "'", '"': '"', "n": "\n", "r": "\r", "t": "\t"}


def cel_unquote(literal: str) -> str:
    """Read back the value inside a CEL string literal, escapes resolved."""
    out: list[str] = []
    pending = False
    for char in str(literal or ""):
        if pending:
            out.append(CEL_UNESCAPE.get(char, char))
            pending = False
        elif char == "\\":
            pending = True
        else:
            out.append(char)
    return "".join(out)


@dataclass(frozen=True)
class ConditionClause:
    """One clause of an availabilityCondition this engine can actually read.

    `kind` is "object" for a resource.name test and "list" for the bucket
    listing attribute. `prefix` is the value inside the literal, already
    unescaped, so the caller compares against the string CEL compares against
    rather than against the source text of the expression.
    """
    kind: str
    prefix: str


# resource.name.startsWith('...')
OBJECT_CLAUSE = re.compile(
    r"^resource\.name\.startsWith\(\s*" + CEL_LITERAL + r"\s*\)$")
# api.getAttribute('...', '').startsWith('...')
LIST_CLAUSE = re.compile(
    r"^api\.getAttribute\(\s*" + CEL_LITERAL + r"\s*,\s*" + CEL_LITERAL
    + r"\s*\)\.startsWith\(\s*" + CEL_LITERAL + r"\s*\)$")


def parse_condition(expression: str) -> tuple[tuple[ConditionClause, ...], str]:
    """Read an availabilityCondition into the clauses it is made of.

    The modelled subset is the one these boundaries use: prefix tests on
    resource.name and on the object list attribute, joined by ||. Anything
    else returns no clauses and a named reason, and every caller treats that
    as a refusal, because a condition this engine cannot read is a condition
    it must not pretend to have evaluated.

    Both the evaluator and the validator go through here. That is the point:
    the string the decision is made from and the string the safety checks are
    made from are then guaranteed to be the same string, read the same way.
    """
    text = str(expression or "").strip()
    if not text:
        return (), "The availability condition is empty."
    clauses: list[ConditionClause] = []
    for part in text.split("||"):
        clause = part.strip()
        match = OBJECT_CLAUSE.match(clause)
        if match:
            clauses.append(ConditionClause("object", cel_unquote(match.group(1))))
            continue
        match = LIST_CLAUSE.match(clause)
        if match:
            attribute = cel_unquote(match.group(1))
            if attribute != OBJECT_LIST_PREFIX_ATTRIBUTE:
                return (), (
                    f"The clause reads the attribute {attribute!r}, which is "
                    f"not {OBJECT_LIST_PREFIX_ATTRIBUTE}. This engine models "
                    f"only that one and refuses the rest.")
            clauses.append(ConditionClause("list", cel_unquote(match.group(3))))
            continue
        return (), (
            f"The clause {clause!r} is outside the subset this engine models, "
            f"so it is treated as a refusal rather than an allow.")
    return tuple(clauses), ""


def parse_gs_uri(uri: str) -> tuple[str, str]:
    """Split gs://bucket/object/key into its bucket and its object name.

    Nothing is trimmed or tidied on the way through. A Cloud Storage object key
    may legitimately begin or end with a space, so a stripped key is not the
    key the storage API would be asked for, and every argument this module
    makes rests on comparing the exact bytes that travel. Whitespace a human
    typed into a text box is the caller's to remove, before it gets here.
    """
    text = str(uri or "")
    if text.startswith("gs://"):
        text = text[len("gs://"):]
    bucket, _, object_name = text.partition("/")
    return bucket, object_name


def object_key_traversal(object_name: str) -> str:
    """Name the reason an object key cannot be decided by a byte comparison.

    A Credential Access Boundary tests resource.name with startsWith, which is
    a byte comparison with no path awareness. The request that carries the key
    is a URL, and ordinary HTTP stacks, proxies and caches normalise a URL path
    before it is served: tenants/tenant-a/../tenant-b/p.json is resolved to
    tenants/tenant-b/p.json, and %2e%2e is decoded to .. first. So a key
    containing a dot segment can pass the boundary as written and still fetch
    an object the boundary never authorized.

    This engine refuses to return a verdict on such a key instead of returning
    the byte answer, because the byte answer is the one that would be wrong.
    Returns an empty string when the key means exactly what it says.
    """
    raw = str(object_name or "")
    decoded = urllib.parse.unquote(raw)
    if decoded != raw:
        return (f"The key {raw!r} is percent encoded, and decodes to "
                f"{decoded!r}. The boundary compares the bytes it is given "
                f"while the request is served against the decoded path, so no "
                f"verdict from a byte comparison here is trustworthy.")
    for segment in decoded.split("/"):
        if segment in (".", ".."):
            return (f"The key {raw!r} contains a {segment!r} path segment. "
                    f"startsWith has no path awareness, so a boundary can "
                    f"authorize this string while the normalised path it "
                    f"resolves to belongs to another prefix entirely.")
    return ""


def cel_starts_with(value: str, literal: str) -> bool:
    """CEL's startsWith, which is a byte prefix test and nothing more.

    Spelled out as its own function so every place that models the boundary
    uses the same semantics Google's evaluator uses, rather than a slightly
    kinder version of them.
    """
    return str(value or "").startswith(str(literal or ""))


def guarded_prefix_match(object_name: str, prefix: str) -> bool:
    """The test a correct boundary performs: anchored, delimiter terminated.

    Three failures this must not have, all of them real. A boundary on
    tenants/tenant-a/ must refuse tenants/tenant-a-archive/, because that is a
    different tenant's data. It must refuse audit/tenants/tenant-a/access.log,
    where the prefix appears only later in the string, because startsWith is
    anchored and anything that ignores the anchor authorizes half the bucket.
    And it must refuse a key carrying a dot segment, which is a path this
    comparison cannot decide at all.

    An unterminated prefix returns False rather than being repaired into a
    terminated one. Repairing it was worse than the bug it hid: the console
    showed a 403 for the sibling folder while the token that was actually
    minted returned a 200, which is the exact leak this module exists to catch.
    """
    name = str(object_name or "")
    guard = str(prefix or "")
    if not guard or not guard.endswith("/"):
        return False
    if object_key_traversal(name):
        return False
    return cel_starts_with(name, guard)


def naive_prefix_match(object_name: str, prefix: str) -> bool:
    """The same test with the delimiter dropped, which is the common mistake.

    Kept so the console can show the difference rather than assert it. This is
    what Google's own published example expressions do, and it is why a prefix
    copied from a docstring into a multi tenant broker leaks the sibling
    folder.

    An empty prefix returns False rather than True. CEL's startsWith('') is a
    tautology, and counting every denial as a prefix confusion catch would turn
    a headline number into fabricated evidence: a boundary cannot claim credit
    for catching an attack that a wrong bucket or a missing permission refused.
    """
    guard = str(prefix or "").rstrip("/")
    if not guard:
        return False
    return cel_starts_with(object_name, guard)


def bucket_resource(bucket: str) -> str:
    """availableResource form: double slash, literal underscore for project."""
    return f"//storage.googleapis.com/projects/_/buckets/{bucket}"


def bucket_resource_name(bucket: str) -> str:
    """resource.name for the bucket itself, which is what a LIST authorizes.

    A listing names no object, so this is the string the condition is given,
    and it is why an object prefix clause can never authorize a LIST on its
    own however carefully the prefix is written.
    """
    return f"projects/_/buckets/{bucket}"


def object_resource_name(bucket: str, object_name: str) -> str:
    """resource.name form, which carries no //storage.googleapis.com prefix.

    This asymmetry with availableResource is what silently breaks hand written
    boundaries: the condition never matches, and the failure looks like an
    authorization problem rather than a typo.
    """
    return f"projects/_/buckets/{bucket}/objects/{object_name}"


ROOT_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def validate_root(root: str) -> tuple[bool, str]:
    """Hold the prefix root to the same standard as the tenant identifier.

    Both values are concatenated into one CEL string literal, so treating only
    one of them as untrusted leaves the defence covering half the string.
    cel_quote does hold against a quote in either, which is why this is about
    the second failure mode rather than injection: an empty root produces the
    prefix /tenant-a/, a root of .. produces ../tenant-a/, and both build a
    boundary that matches no object in the bucket while the caller believes the
    token is confined to one. Failing closed is not the same as being right.
    """
    text = str(root or "")
    if not text:
        return False, ("A prefix root is required. An empty root produces a "
                       "prefix starting with a slash, which matches nothing.")
    if text.endswith("/") or text.startswith("/"):
        return False, (f"{text!r} starts or ends with a delimiter. The root is "
                       f"joined to the tenant with one slash and no more.")
    if not all(ROOT_SEGMENT_PATTERN.fullmatch(part) for part in text.split("/")):
        return False, (
            f"{text!r} is not an accepted prefix root. Each segment must begin "
            f"with a letter or a digit and carry only letters, digits, "
            f"underscores, dots and hyphens, which is what rules out the empty "
            f"segment and the dot segment a traversal is made of.")
    return True, f"{text} is a safe prefix root."


def tenant_prefix(tenant: str, root: str = "tenants") -> str:
    """The object key prefix for one tenant, terminated by the delimiter.

    The trailing slash is the entire security property. Without it the prefix
    is a byte comparison that also matches tenant-a-archive, tenant-a-backup
    and tenant-aardvark, and CEL's startsWith has no path awareness to save
    you. It also excludes an object keyed exactly tenants/tenant-a with no
    slash, which is what you want: that is not a folder.
    """
    return f"{root}/{tenant}/"


def boundary_expression(bucket: str, prefix: str,
                        include_list_clause: bool = True) -> str:
    """Build the availabilityCondition for one tenant prefix.

    The first clause authorizes objects. The second is the pattern for LIST
    calls, where the resource being authorized is the bucket rather than an
    object, so resource.name carries no object prefix to test. It reads the
    caller supplied list prefix, which means it gates which listings are
    permitted rather than filtering the results: a caller who omits the prefix
    gets an empty string that fails the test, which is the fail closed
    behaviour you want.
    """
    object_prefix = f"projects/_/buckets/{bucket}/objects/{prefix}"
    expression = f"resource.name.startsWith({cel_quote(object_prefix)})"
    if include_list_clause:
        expression += (
            f" || api.getAttribute("
            f"{cel_quote(OBJECT_LIST_PREFIX_ATTRIBUTE)}, '')"
            f".startsWith({cel_quote(prefix)})")
    return expression


def tenant_boundary(tenant: str, bucket: str = SAMPLE_BUCKET,
                    roles: tuple[str, ...] = ("roles/storage.objectViewer",),
                    root: str = "tenants",
                    include_list_clause: bool = True) -> AccessBoundary:
    """Build the Credential Access Boundary for one tenant.

    Raises rather than returning a boundary built from an unvalidated
    identifier, because a boundary that was going to be wrong is more dangerous
    than no boundary at all: the caller believes it is confined.
    """
    ok, reason = validate_tenant_id(tenant)
    if not ok:
        raise ValueError(reason)
    root_ok, root_reason = validate_root(root)
    if not root_ok:
        raise ValueError(root_reason)
    prefix = tenant_prefix(tenant, root)
    rule = AccessBoundaryRule(
        available_resource=bucket_resource(bucket),
        available_permissions=tuple(f"inRole:{role}" for role in roles),
        availability_condition=AvailabilityCondition(
            expression=boundary_expression(bucket, prefix, include_list_clause),
            title=f"Objects under {prefix}",
            description=(f"Confines the token to the {tenant} prefix in "
                         f"{bucket}. The trailing delimiter is what keeps "
                         f"{tenant}-archive out."),
        ),
    )
    return AccessBoundary(rules=(rule,), tenant=tenant, bucket=bucket,
                          prefix=prefix)


def validate_boundary(boundary: AccessBoundary) -> tuple[ConfigFinding, ...]:
    """Check the boundary against the limits STS documents.

    Client libraries validate almost nothing here: an availableResource in the
    wrong format is not caught before the network call, and surfaces either as
    a 400 or, worse, as a boundary that quietly matches nothing you intended.
    """
    findings: list[ConfigFinding] = []
    rules = boundary.rules

    if not rules:
        findings.append(ConfigFinding(
            "rule_count", STATUS_FAIL, "No rules in the boundary",
            "A boundary needs at least one rule."))
    elif len(rules) > MAX_ACCESS_BOUNDARY_RULES:
        findings.append(ConfigFinding(
            "rule_count", STATUS_FAIL, "Too many rules",
            f"One access boundary can contain at most "
            f"{MAX_ACCESS_BOUNDARY_RULES} rules, and this one has "
            f"{len(rules)}."))
    else:
        findings.append(ConfigFinding(
            "rule_count", STATUS_PASS, "Rule count is within the limit",
            f"{len(rules)} of {MAX_ACCESS_BOUNDARY_RULES} rules. Remember the "
            f"rules are unioned: a second, tighter rule cannot narrow what a "
            f"broad first rule already granted."))

    bad_resources = [r.available_resource for r in rules
                     if not r.available_resource.startswith(
                         "//storage.googleapis.com/projects/_/buckets/")]
    if bad_resources:
        findings.append(ConfigFinding(
            "resource_format", STATUS_FAIL, "availableResource is malformed",
            f"The form is //storage.googleapis.com/projects/_/buckets/BUCKET, "
            f"with the literal underscore for the project and no trailing "
            f"slash. Rejected: {', '.join(bad_resources)}."))
    else:
        findings.append(ConfigFinding(
            "resource_format", STATUS_PASS, "availableResource is well formed",
            "Leading double slash, literal underscore for the project, no "
            "trailing slash. The client libraries do not check this for you."))

    bad_permissions = [permission for rule in rules
                       for permission in rule.available_permissions
                       if not permission.startswith("inRole:")]
    empty_permissions = [index for index, rule in enumerate(rules)
                         if not rule.available_permissions]
    if empty_permissions:
        # Google's own Go client refuses this before the network call: all
        # rules must provide at least one permission. It is the one thing the
        # client libraries do check, so a boundary that fails it would be
        # rejected on the caller's own machine and never reach STS at all.
        findings.append(ConfigFinding(
            "permission_format", STATUS_FAIL, "A rule grants no permissions",
            f"Every rule must name at least one permission. Rule "
            f"{', '.join(str(index) for index in empty_permissions)} names "
            f"none, which the client libraries reject before the request is "
            f"sent."))
    elif bad_permissions:
        findings.append(ConfigFinding(
            "permission_format", STATUS_FAIL, "availablePermissions is malformed",
            f"Every entry must be prefixed with inRole: and name an IAM role. "
            f"Rejected: {', '.join(bad_permissions)}."))
    else:
        findings.append(ConfigFinding(
            "permission_format", STATUS_PASS, "availablePermissions names roles",
            "Each entry is inRole: followed by a role. Only the permissions "
            "contained in those roles become available."))

    # Read through the same parser the evaluator uses rather than pattern
    # matching the source text. A regex over the raw expression was defeated by
    # any earlier quote in it, because cel_quote escapes a quote to \' and that
    # ends the regex's run of non quote characters: a bucket named ehr'storage
    # turned the only automated check for the sibling prefix leak from fail
    # into pass, on a boundary that really was leaking.
    unreadable: list[str] = []
    tautologies: list[str] = []
    unterminated: list[str] = []
    for rule in rules:
        if rule.availability_condition is None:
            continue
        clauses, reason = parse_condition(rule.availability_condition.expression)
        if reason:
            unreadable.append(reason)
            continue
        for clause in clauses:
            if clause.kind == "object":
                # Everything before /objects/ names the bucket. A clause that
                # stops there confines the token to nothing narrower than the
                # bucket, which is the same finding as an empty prefix.
                _, separator, key = clause.prefix.partition("/objects/")
                if not separator:
                    key = ""
            else:
                key = clause.prefix
            if not key:
                tautologies.append(clause.kind)
            elif not key.endswith("/"):
                unterminated.append(key)

    if unreadable:
        findings.append(ConfigFinding(
            "prefix_delimiter", STATUS_FAIL,
            "A condition cannot be read by this engine",
            f"A boundary nobody can evaluate is a boundary nobody can review, "
            f"so it is reported rather than assumed correct. {unreadable[0]}"))
    elif tautologies:
        # startsWith('') is true for every string in CEL, so a clause with an
        # empty prefix does not confine the token to anything: it grants the
        # whole multi tenant bucket while looking like a boundary.
        findings.append(ConfigFinding(
            "prefix_delimiter", STATUS_FAIL,
            "A prefix is empty, so the condition is a tautology",
            f"An empty prefix makes startsWith true for every object in the "
            f"bucket, which grants the whole bucket rather than one tenant's "
            f"folder. Empty clauses: {', '.join(sorted(set(tautologies)))}."))
    elif unterminated:
        findings.append(ConfigFinding(
            "prefix_delimiter", STATUS_FAIL,
            "A prefix is not terminated by the delimiter",
            f"startsWith is a plain byte prefix test with no path awareness, "
            f"so a prefix ending mid segment also matches the sibling folder. "
            f"Terminate every prefix with a slash. Rejected: "
            f"{', '.join(sorted(set(unterminated)))}."))
    else:
        findings.append(ConfigFinding(
            "prefix_delimiter", STATUS_PASS,
            "Every prefix is terminated by the delimiter",
            "This is what keeps tenant-a-archive out of a boundary written for "
            "tenant-a."))

    size = boundary.serialized_size
    if size > ACCESS_BOUNDARY_SIZE_BUDGET:
        findings.append(ConfigFinding(
            "size", STATUS_WARN, "The boundary exceeds the safe size budget",
            f"{size} characters. Treat {ACCESS_BOUNDARY_SIZE_BUDGET} as the "
            f"design budget for the accessBoundary object: with ten rules that "
            f"is roughly 200 characters each, which a long bucket name plus a "
            f"dual clause condition can exceed."))
    else:
        findings.append(ConfigFinding(
            "size", STATUS_PASS, "The boundary is inside the size budget",
            f"{size} of {ACCESS_BOUNDARY_SIZE_BUDGET} characters."))

    long_expressions = [rule for rule in rules
                        if rule.availability_condition is not None
                        and len(rule.availability_condition.expression)
                        > MAX_CONDITION_EXPRESSION_CHARS]
    if long_expressions:
        findings.append(ConfigFinding(
            "expression_length", STATUS_FAIL, "A condition expression is too long",
            f"The maximum length of the expression field is "
            f"{MAX_CONDITION_EXPRESSION_CHARS} characters."))
    else:
        findings.append(ConfigFinding(
            "expression_length", STATUS_PASS, "Condition expressions fit",
            f"Each expression is within {MAX_CONDITION_EXPRESSION_CHARS} "
            f"characters."))

    return tuple(findings)


def permissions_permit(entries: tuple[str, ...],
                       permission: str) -> tuple[bool, str]:
    """Resolve a requested permission against a set of inRole: entries."""
    granted: set = set()
    named: list[str] = []
    for entry in entries:
        role = entry.split("inRole:", 1)[-1]
        named.append(role)
        granted |= set(ROLE_PERMISSIONS.get(role, frozenset()))
    roles = ", ".join(sorted(set(named))) or "none"
    if permission in granted:
        return True, f"{permission} is carried by {roles}."
    return False, (
        f"{permission} is not carried by the roles available here ({roles}). "
        f"availablePermissions names roles, and only the permissions inside "
        f"those roles become available.")


def rule_permits(rule: AccessBoundaryRule, permission: str) -> tuple[bool, str]:
    """Resolve the permission against one rule, which is how STS resolves it.

    Rules are unioned as resources, not as permissions: a rule authorizes the
    permissions it names on the resource it names, and a writer role on some
    other bucket does not travel. Asking the whole boundary this question from
    inside a per rule decision silently upgraded a read only rule on the
    production bucket to delete because an unrelated rule elsewhere named a
    writer role.
    """
    return permissions_permit(rule.available_permissions, permission)


def boundary_permits(boundary: AccessBoundary, permission: str) -> tuple[bool, str]:
    """Whether any rule in the boundary carries the permission at all.

    This is the question a reviewer asks of the document as a whole. It is
    deliberately not the question a decision asks, because a decision is always
    made against one rule.
    """
    return permissions_permit(
        tuple(entry for rule in boundary.rules
              for entry in rule.available_permissions),
        permission)


def clause_key(clause: ConditionClause) -> str:
    """The object key prefix a clause confines the token to.

    An object clause carries the whole resource.name prefix, bucket included,
    and a list clause carries the bare key prefix. Both are reported to the
    console as the same kind of thing, so the difference is resolved here once.
    """
    if clause.kind != "object":
        return clause.prefix
    _, separator, key = clause.prefix.partition("/objects/")
    return key if separator else ""


def evaluate_attempt(boundary: AccessBoundary,
                     attempt: AccessAttempt) -> AccessDecision:
    """Decide one call against the boundary, with the reason as the product.

    The order mirrors the real evaluation, and every part of it is read off the
    rule that is being considered rather than off the boundary as a whole: the
    rule has to name the bucket, the permission has to be inside the roles that
    rule names, and that rule's own availabilityCondition has to be satisfied.
    Rules are unioned, so a refusal by one rule is not a decision until every
    other rule has been asked.

    Three earlier shortcuts are gone, and each of them was a wrong answer. The
    permission was resolved against every rule in the boundary at once, so a
    writer role on another bucket upgraded a read only rule on this one. The
    condition was never read: the verdict came from an AccessBoundary.prefix
    field that no rule mentions, so a boundary whose CEL says tenant-a and
    whose field says tenants/ authorized every tenant, and a hand built
    boundary with the field left empty refused everything it had authorized.
    And the object key was byte compared without asking whether it was a key
    that means what it says, so a dot segment walked straight out of the tenant
    prefix with a 200.
    """
    bucket, object_name = parse_gs_uri(attempt.path)
    listing = attempt.is_list
    resource = (bucket_resource_name(bucket) if listing
                else object_resource_name(bucket, object_name))

    if not listing:
        traversal = object_key_traversal(object_name)
        if traversal:
            return AccessDecision(
                attempt=attempt, allowed=False,
                status_code=STATUS_CODE_UNDECIDABLE,
                status_text=STATUS_TEXT_UNDECIDABLE,
                reason=(f"{traversal} This evaluator returns no verdict rather "
                        f"than the byte answer, because the byte answer here "
                        f"is a 200 on a path that resolves into another "
                        f"prefix. Normalise the key and test that instead."),
                bucket=bucket, object_name=object_name, resource_name=resource,
                rule_index=-1, matched_prefix="", naive_allowed=False)

    # Denials are collected rather than returned, because the rules are a union
    # and the next one may authorize what this one refused. A condition denial
    # is the one worth reporting when there is a choice: it is the clause that
    # actually looked at the object.
    denials: list[tuple[str, int, str, bool]] = []

    for index, rule in enumerate(boundary.rules):
        if rule.available_resource != bucket_resource(bucket):
            continue

        allowed_permission, permission_reason = rule_permits(
            rule, attempt.permission)
        if not allowed_permission:
            denials.append(("permission", index, permission_reason, False))
            continue

        condition = rule.availability_condition
        if condition is None:
            return AccessDecision(
                attempt=attempt, allowed=True, status_code=200,
                status_text="200 OK",
                reason=("The rule covers the bucket and carries no "
                        "availability condition, so the whole bucket is inside "
                        "the boundary. For a multi tenant bucket that is a "
                        "finding, not a pass."),
                bucket=bucket, object_name=object_name, resource_name=resource,
                rule_index=index, matched_prefix="", naive_allowed=False)

        clauses, unreadable = parse_condition(condition.expression)
        if unreadable:
            denials.append(("condition", index, (
                f"Denied. {unreadable} A condition this engine cannot read is "
                f"treated as a refusal, never as an allow."), False))
            continue

        # The value each clause tests. An object call presents resource.name
        # and no list attribute, so api.getAttribute returns the empty default
        # and the list clause can only match a caller who asked for a prefix.
        tested = {"object": resource,
                  "list": attempt.list_prefix if listing else ""}
        matched = ""
        naive_here = False
        for clause in clauses:
            value = tested.get(clause.kind, "")
            if cel_starts_with(value, clause.prefix) and not matched:
                matched = clause_key(clause) or clause.prefix
            naive_here = naive_here or (
                not cel_starts_with(value, clause.prefix)
                and naive_prefix_match(value, clause.prefix))

        if matched:
            return AccessDecision(
                attempt=attempt, allowed=True, status_code=200,
                status_text="200 OK",
                reason=(f"{'The requested list prefix' if listing else 'resource.name'} "
                        f"satisfies the availability condition of rule {index}, "
                        f"which confines the token to {matched}."),
                bucket=bucket, object_name=object_name, resource_name=resource,
                rule_index=index, matched_prefix=matched,
                naive_allowed=naive_here)

        keys = ", ".join(clause_key(clause) or "the whole bucket"
                         for clause in clauses) or "nothing"
        if naive_here:
            reason = (
                f"Denied by rule {index}. The key is {object_name or attempt.list_prefix!r}, "
                f"which shares the leading characters of {keys} but is not "
                f"inside it. A boundary written without the trailing delimiter "
                f"would have returned 200 here and handed over another "
                f"tenant's data.")
        elif listing:
            reason = (
                f"Denied by rule {index}. A LIST authorizes the bucket, so "
                f"resource.name is {resource} and carries no object key to "
                f"test. The only clause that can allow it reads "
                f"{OBJECT_LIST_PREFIX_ATTRIBUTE}, and the prefix asked for is "
                f"{attempt.list_prefix or 'empty'} against {keys}.")
        else:
            reason = (
                f"Denied by rule {index}. resource.name is {resource}, which "
                f"does not start with {keys} under "
                f"{bucket_resource_name(bucket)}/objects/. startsWith is "
                f"anchored, so a prefix appearing later in the key does not "
                f"match.")
        denials.append(("condition", index, reason, naive_here))

    if denials:
        chosen = next((d for d in denials if d[0] == "condition"), denials[0])
        return AccessDecision(
            attempt=attempt, allowed=False, status_code=403,
            status_text="403 Forbidden", reason=chosen[2],
            bucket=bucket, object_name=object_name, resource_name=resource,
            rule_index=chosen[1], matched_prefix="",
            naive_allowed=any(d[3] for d in denials))

    resources = ", ".join(sorted({rule.available_resource
                                  for rule in boundary.rules})) or "nothing"
    return AccessDecision(
        attempt=attempt, allowed=False, status_code=403,
        status_text="403 Forbidden",
        reason=(f"No rule in the boundary names bucket {bucket}. The token "
                f"carries an upper bound of {resources} and cannot reach "
                f"anything outside it."),
        bucket=bucket, object_name=object_name, resource_name=resource,
        rule_index=-1, matched_prefix="", naive_allowed=False)


def evaluate_attempts(boundary: AccessBoundary,
                      attempts: tuple[AccessAttempt, ...] = SAMPLE_ATTEMPTS
                      ) -> tuple[AccessDecision, ...]:
    return tuple(evaluate_attempt(boundary, attempt) for attempt in attempts)


def boundary_summary(decisions: tuple[AccessDecision, ...],
                     tenant: str = "") -> dict:
    """Counts the console shows above the table, and the tests assert on.

    `tenant` is the tenant the boundary was built for, so a denial can be
    reported as a cross tenant refusal rather than merely a refusal. Left empty
    it still counts denials, it just cannot attribute them.
    """
    denied = [d for d in decisions if not d.allowed]
    return {
        "total": len(decisions),
        "allowed": len([d for d in decisions if d.allowed]),
        "denied": len(denied),
        "cross_tenant_denied": len([
            d for d in denied
            if d.attempt.tenant and tenant and d.attempt.tenant != tenant]),
        "prefix_confusion_caught": len([d for d in decisions if d.prefix_confusion]),
    }


# ---------------------------------------------------------------------------
# C. Key Vault versus managed identity decision matrix
# ---------------------------------------------------------------------------


def decisions_by_verdict(verdict: str,
                         rows: tuple[DecisionRow, ...] = SAMPLE_DECISIONS
                         ) -> tuple[DecisionRow, ...]:
    return tuple(row for row in rows if row.verdict == verdict)


def decision_summary(rows: tuple[DecisionRow, ...] = SAMPLE_DECISIONS) -> dict:
    """What the matrix adds up to, which is the only number a board asks for.

    The honest framing is the one Microsoft and Google both use: the platform
    removes the secrets it can mint a token for, and a vault still holds the
    residue. Claiming zero secrets would be the claim that fails an audit.
    """
    removed = [row for row in rows if row.secret_before and not row.secret_after]
    remaining = [row for row in rows if row.secret_after]
    return {
        "rows": len(rows),
        "secrets_before": len([row for row in rows if row.secret_before]),
        "secrets_removed": len(removed),
        "secrets_remaining": len(remaining),
        "vault_mandatory": len(decisions_by_verdict(VERDICT_VAULT, rows)),
        "identity_replaces": len(decisions_by_verdict(VERDICT_IDENTITY, rows)),
        "federation_replaces": len(decisions_by_verdict(VERDICT_FEDERATION, rows)),
        "removal_rate": round(len(removed) / len(rows), 4) if rows else 0.0,
    }


def decision_matrix_rows(rows: tuple[DecisionRow, ...] = SAMPLE_DECISIONS
                         ) -> tuple[tuple[str, ...], ...]:
    """The matrix flattened for a table, verdict label already resolved."""
    return tuple(
        (row.resource, row.platform, VERDICT_LABEL[row.verdict], row.mechanism,
         "yes" if row.secret_after else "no")
        for row in rows
    )


# ---------------------------------------------------------------------------
# D. IIS static key failure mode inspector
# ---------------------------------------------------------------------------


def risks_by_severity(severity: str,
                      risks: tuple[KeyRisk, ...] = SAMPLE_KEY_RISKS
                      ) -> tuple[KeyRisk, ...]:
    return tuple(risk for risk in risks if risk.severity == severity)


def sorted_risks(risks: tuple[KeyRisk, ...] = SAMPLE_KEY_RISKS
                 ) -> tuple[KeyRisk, ...]:
    order = {name: index for index, name in enumerate(SEVERITY_ORDER)}
    return tuple(sorted(risks, key=lambda r: order.get(r.severity, 99)))


def key_posture(risks: tuple[KeyRisk, ...] = SAMPLE_KEY_RISKS) -> dict:
    """The posture of the IIS host before and after the key is removed.

    Every vector here is a property of the artefact rather than of the host, so
    removing the artefact removes all of them at once. That is the argument:
    not that the file can be defended better, but that it does not need to
    exist.
    """
    return {
        "vectors": len(risks),
        "critical": len(risks_by_severity(SEVERITY_CRITICAL, risks)),
        "high": len(risks_by_severity(SEVERITY_HIGH, risks)),
        "medium": len(risks_by_severity(SEVERITY_MEDIUM, risks)),
        "vectors_closed_by_wif": len(risks),
        "residual_after_wif": 0,
        "statement": (
            "All four vectors are properties of a long lived private key "
            "sitting on disk. Federation removes the key, so the vectors close "
            "together rather than one control at a time."
        ),
    }


def key_file_locations() -> tuple[tuple[str, str], ...]:
    """Where Application Default Credentials look for the key on Windows.

    Listed because an inspection that cannot say where the file is has not
    inspected anything, and because both locations end up inside a file level
    backup without anyone deciding that they should.
    """
    return (
        ("GOOGLE_APPLICATION_CREDENTIALS",
         "An environment variable holding the full path to the key file, read "
         "first by Application Default Credentials."),
        ("%APPDATA%\\gcloud\\application_default_credentials.json",
         "The well known file location on Windows, used when the environment "
         "variable is not set."),
        ("Application pool identity",
         "Whatever identity runs w3wp.exe must be able to read the file, so "
         "anything that can read as that identity obtains the credential."),
    )


# ---------------------------------------------------------------------------
# Executive KPIs
# ---------------------------------------------------------------------------


def console_kpis(run: FederationRun,
                 decisions: tuple[AccessDecision, ...],
                 tenant: str = "",
                 rows: tuple[DecisionRow, ...] = SAMPLE_DECISIONS,
                 risks: tuple[KeyRisk, ...] = SAMPLE_KEY_RISKS) -> Kpis:
    summary = boundary_summary(decisions, tenant)
    matrix = decision_summary(rows)
    lifetime = run.gcs_token.lifetime_seconds if run.gcs_token else 0.0
    return Kpis(
        steps_passed=run.passed,
        steps_total=len(run.steps),
        final_lifetime_seconds=lifetime,
        paths_denied=summary["denied"],
        cross_tenant_denied=summary["cross_tenant_denied"],
        prefix_confusion_caught=summary["prefix_confusion_caught"],
        secrets_removed=matrix["secrets_removed"],
        secrets_remaining=matrix["secrets_remaining"],
        critical_key_risks=len(risks_by_severity(SEVERITY_CRITICAL, risks)),
    )
