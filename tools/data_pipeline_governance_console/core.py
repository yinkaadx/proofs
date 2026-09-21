"""Data pipeline and governance engine.

No Streamlit import lives in this file. The three parts each hold one
property that decides whether a pipeline is trustworthy rather than merely
running:

* a quality gate is worth something only if a rejected row lands nowhere,
  and only if it dedupes on the content of a row rather than on an
  identifier the producer is free to regenerate;
* an architecture choice is a trade, and the axis people forget is not
  latency or cost but how each shape fails, because batch fails loudly and
  streaming fails quietly;
* row level security is enforced by the database engine, which means it
  holds against a compromised application, and it also means the ways it
  silently does not hold are engine level facts rather than application
  bugs.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

ENGINE_VERSION = "1.0.0"

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"

_SEVERITY_ORDER = {SEVERITY_OK: 0, SEVERITY_WARN: 1, SEVERITY_CRITICAL: 2}

HASH_PATTERN = re.compile(r"[0-9a-f]{64}")


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


def sha256_of(text: str) -> str:
    """The digest a producer should be sending as the payload hash."""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 1. Data quality gate
# ---------------------------------------------------------------------------

ADMITTED = "Admitted to the warehouse"
REJECTED = "Rejected, quarantined, nothing written"

REASON_MALFORMED = "The payload hash is not a SHA256 digest"
REASON_DUPLICATE = "Duplicate SHA256 hash detected, this exact payload already landed"
REASON_SCHEMA = "Schema mismatch, the payload does not match the contract"
REASON_NULL = "Null count above the tolerance for a non nullable column"

NULL_TOLERANCE = 0


@dataclass(frozen=True)
class GateResult:
    payload_hash: str
    schema_valid: bool
    null_count: int
    null_tolerance: int
    admitted: bool
    status: str
    headline: str
    reasons: tuple[str, ...]
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def short_hash(self) -> str:
        return self.payload_hash[:16] if self.payload_hash else "none"


def simulate_data_quality_gate(payload_hash: str, schema_valid: bool,
                               null_count: int,
                               seen_hashes: Iterable[str] = (),
                               null_tolerance: int = NULL_TOLERANCE
                               ) -> GateResult:
    """Decide whether one row is admitted, and say every reason it is not.

    Two design choices carry the whole value of this gate.

    First, every reason is collected before the verdict is returned. A gate
    that short circuits on the first failure turns a backfill into a queue
    of single fixes: correct the schema, re run for an hour, discover the
    nulls, correct those, re run again. The row is checked once and told
    everything.

    Second, the duplicate test is on the SHA256 of the payload content, not
    on a row identifier. A producer that retries after a timeout sends the
    same content under a fresh identifier, and an identifier based gate
    admits it as new. That is the duplicate that reaches a revenue total.
    """
    digest = str(payload_hash or "").strip().lower()
    nulls = int(null_count)
    tolerance = int(null_tolerance)
    if nulls < 0:
        raise ValueError("a null count cannot be negative")
    if tolerance < 0:
        raise ValueError("a null tolerance cannot be negative")

    known = {str(h).strip().lower() for h in seen_hashes}
    reasons: list[str] = []
    findings: list[Finding] = []

    if not HASH_PATTERN.fullmatch(digest):
        reasons.append(REASON_MALFORMED)
        findings.append(Finding(
            code="DQ-HASH", severity=SEVERITY_CRITICAL,
            title=f"{payload_hash!r} is not a 64 character hex digest",
            detail=("Without a content digest the gate has no way to tell a "
                    "retry from a new row, so deduplication is not merely "
                    "weakened, it is absent."),
            fix=("Hash the canonical payload at the producer and send the "
                 "digest alongside it.")))
    elif digest in known:
        reasons.append(REASON_DUPLICATE)
        findings.append(Finding(
            code="DQ-DUPLICATE", severity=SEVERITY_CRITICAL,
            title=f"Digest {digest[:16]} has already been admitted",
            detail=("The same content arrived twice. A producer retry after "
                    "a network timeout is the usual cause, and it carries a "
                    "new identifier every time, which is why the identifier "
                    "cannot be the dedupe key."),
            fix=("Acknowledge and drop. Do not write, and do not increment "
                 "any counter the row would have touched.")))

    if not bool(schema_valid):
        reasons.append(REASON_SCHEMA)
        findings.append(Finding(
            code="DQ-SCHEMA", severity=SEVERITY_CRITICAL,
            title="The payload does not match the declared contract",
            detail=("A column added, removed, or retyped upstream reaches "
                    "the warehouse as a cast error at best and as a "
                    "silently widened type at worst."),
            fix=("Quarantine the row and alert the producing team. Do not "
                 "coerce the value to make the load succeed.")))

    if nulls > tolerance:
        reasons.append(REASON_NULL)
        findings.append(Finding(
            code="DQ-NULL", severity=SEVERITY_CRITICAL,
            title=f"{nulls} null value(s) against a tolerance of {tolerance}",
            detail=("A null in a join key does not raise. It drops the row "
                    "from the join, and the report simply shows a smaller "
                    "number than the truth."),
            fix=("Reject the row here. A null discovered in a dashboard has "
                 "already been believed.")))

    admitted = not reasons

    if admitted:
        findings.append(Finding(
            code="DQ-PASS", severity=SEVERITY_OK,
            title=f"Digest {digest[:16]} admitted on a clean check",
            detail=("The content digest is well formed and unseen, the "
                    "payload matches the contract, and the null count is "
                    "within tolerance."),
            fix="Record the digest so the next retry of it is caught."))
        findings.append(Finding(
            code="DQ-LEDGER", severity=SEVERITY_WARN,
            title="Deduplication is only as durable as the digest ledger",
            detail=("If the set of seen digests lives in memory, a restart "
                    "empties it and every in flight retry is admitted as "
                    "new. The ledger has to outlive the process that reads "
                    "it."),
            fix=("Keep the digest ledger in the warehouse itself, with a "
                 "unique constraint, so the database refuses the second "
                 "write even if the gate forgets.")))

    status = ADMITTED if admitted else REJECTED
    if admitted:
        headline = f"Admitted: {digest[:16]}"
    else:
        headline = (f"Rejected on {len(reasons)} reason(s): "
                    f"{reasons[0]}")

    return GateResult(
        payload_hash=digest, schema_valid=bool(schema_valid),
        null_count=nulls, null_tolerance=tolerance, admitted=admitted,
        status=status, headline=headline, reasons=tuple(reasons),
        findings=tuple(findings))


@dataclass(frozen=True)
class BatchOutcome:
    results: tuple[GateResult, ...]
    admitted: int
    rejected: int
    total: int
    seen_after: tuple[str, ...]


def run_quality_gate_batch(rows: Sequence[Mapping],
                           seen_hashes: Iterable[str] = (),
                           null_tolerance: int = NULL_TOLERANCE
                           ) -> BatchOutcome:
    """Run the gate over a batch, carrying the digest ledger forward.

    This is where the content digest earns its keep: the second copy of a
    payload inside a single batch is rejected by the first copy, without
    any identifier being consulted.
    """
    known = {str(h).strip().lower() for h in seen_hashes}
    results: list[GateResult] = []
    for row in rows:
        result = simulate_data_quality_gate(
            row.get("payload_hash", ""), row.get("schema_valid", True),
            row.get("null_count", 0), seen_hashes=known,
            null_tolerance=null_tolerance)
        if result.admitted:
            known.add(result.payload_hash)
        results.append(result)

    admitted = sum(1 for r in results if r.admitted)
    rejected = sum(1 for r in results if not r.admitted)
    return BatchOutcome(
        results=tuple(results), admitted=admitted, rejected=rejected,
        total=len(results), seen_after=tuple(sorted(known)))


SAMPLE_ROWS: tuple[dict, ...] = (
    {"label": "Clean order event",
     "payload_hash": sha256_of('{"order":"A-1001","total":42.5}'),
     "schema_valid": True, "null_count": 0},
    {"label": "Producer retry of the same order",
     "payload_hash": sha256_of('{"order":"A-1001","total":42.5}'),
     "schema_valid": True, "null_count": 0},
    {"label": "Upstream added a column",
     "payload_hash": sha256_of('{"order":"A-1002","total":19.0,"tier":"x"}'),
     "schema_valid": False, "null_count": 0},
    {"label": "Null in the customer join key",
     "payload_hash": sha256_of('{"order":"A-1003","customer":null}'),
     "schema_valid": True, "null_count": 1},
    {"label": "Digest field left empty by the producer",
     "payload_hash": "", "schema_valid": True, "null_count": 0},
)


# ---------------------------------------------------------------------------
# 2. Architecture trade off matrix
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Architecture:
    key: str
    name: str
    latency: str
    latency_low_seconds: float
    latency_high_seconds: float
    complexity: str
    complexity_score: int
    cost: str
    cost_shape: str
    fails_as: str
    choose_when: str
    avoid_when: str


BATCH = "batch"
MICRO_BATCH = "micro_batch"
STREAMING = "streaming"

_MATRIX: tuple[Architecture, ...] = (
    Architecture(
        key=BATCH, name="Batch processing",
        latency="Hours. The data is as fresh as the last scheduled run.",
        latency_low_seconds=900.0, latency_high_seconds=86400.0,
        complexity="Low. One scheduler, one job, one window to reason about.",
        complexity_score=1,
        cost="Lowest infrastructure cost, paid in bursts.",
        cost_shape=("Compute is idle between runs, so the bill tracks volume "
                    "rather than uptime."),
        fails_as=("Loudly. The job did not run, or it ran and threw. Someone "
                  "gets a red alert with a stack trace."),
        choose_when=("The question being answered is about yesterday: "
                     "finance close, daily reconciliation, model training "
                     "sets."),
        avoid_when=("A human or a system will act on the number within the "
                    "window, because for that window the number is absent, "
                    "not late.")),
    Architecture(
        key=MICRO_BATCH, name="Micro batching",
        latency="Seconds to minutes. A small window, repeated.",
        latency_low_seconds=10.0, latency_high_seconds=900.0,
        complexity=("Moderate. Still a job, but now with checkpointing and "
                    "late arrival handling."),
        complexity_score=3,
        cost="Moderate and steady, because the cluster stays warm.",
        cost_shape=("You pay for readiness between windows, which is most of "
                    "the time at small volumes."),
        fails_as=("Half loudly. A failed window alerts, but a window that "
                  "silently keeps shrinking its output does not."),
        choose_when=("Freshness of a minute or two is worth real money and "
                     "exactly once semantics still matter more than raw "
                     "latency."),
        avoid_when=("The team cannot yet say what a late arriving event "
                    "should do to a window that already closed, because "
                    "that is the decision micro batching forces on every "
                    "single run.")),
    Architecture(
        key=STREAMING, name="Event streaming",
        latency="Sub second to seconds, per event.",
        latency_low_seconds=0.05, latency_high_seconds=10.0,
        complexity=("High. Ordering, partitioning, replay, schema evolution, "
                    "and consumer lag all become daily concerns."),
        complexity_score=5,
        cost=("Highest total cost, and most of it is engineering time rather "
              "than infrastructure."),
        cost_shape=("The broker bill is often the smaller half. The larger "
                    "half is the people who keep consumers correct."),
        fails_as=("Quietly. Consumer lag grows, the dashboard still renders, "
                  "and the numbers are simply behind. Nothing is red."),
        choose_when=("The reaction has to happen inside the event: fraud "
                     "holds, inventory decrements, live routing."),
        avoid_when=("The consumer of the number is a human reading a report "
                    "once a day, because you have bought latency nobody "
                    "spends.")),
)

def get_pipeline_architecture_matrix() -> tuple[Architecture, ...]:
    """The three shapes, compared on the axes that actually decide.

    Latency and cost are the axes everyone lists. The one that decides
    whether a team can operate what it built is the failure mode, and it
    runs the opposite way to latency: the fastest architecture is the one
    whose failures are hardest to see.
    """
    return _MATRIX


def get_architecture(key: str) -> Architecture:
    for item in _MATRIX:
        if item.key == key:
            return item
    raise KeyError(key)


def compare_architectures() -> dict:
    """Facts about the matrix that the matrix is meant to demonstrate."""
    ordered_latency = sorted(_MATRIX, key=lambda a: a.latency_high_seconds)
    ordered_complexity = sorted(_MATRIX, key=lambda a: a.complexity_score)
    return {
        "by_latency": tuple(a.key for a in ordered_latency),
        "by_complexity": tuple(a.key for a in ordered_complexity),
        "latency_order_reverses_complexity_order": (
            tuple(a.key for a in ordered_latency)
            == tuple(a.key for a in reversed(ordered_complexity))),
        "every_second_of_latency_is_bought_with_complexity": (
            "Ranking the three by latency gives the exact reverse of "
            "ranking them by complexity. There is no cell in this matrix "
            "where a team gets lower latency without taking on more to "
            "operate."),
        "visibility_runs_backwards": (
            "The architecture with the lowest latency has the quietest "
            "failure mode, so the faster the pipeline, the more deliberate "
            "the monitoring has to be."),
    }


# ---------------------------------------------------------------------------
# 3. Row level security governance
# ---------------------------------------------------------------------------

ROLE_TENANT_USER = "tenant_user"
ROLE_ANALYST = "analyst_read_only"
ROLE_APP_SERVICE = "app_service_with_bypassrls"
ROLE_TABLE_OWNER = "table_owner"
ROLE_SUPERUSER = "superuser"

ROLES: tuple[str, ...] = (ROLE_TENANT_USER, ROLE_ANALYST, ROLE_APP_SERVICE,
                          ROLE_TABLE_OWNER, ROLE_SUPERUSER)

TARGET_OWN = "own_tenant_rows"
TARGET_OTHER = "another_tenant_rows"
TARGET_ALL = "every_tenant_row"

TARGETS: tuple[str, ...] = (TARGET_OWN, TARGET_OTHER, TARGET_ALL)

TOTAL_ROWS = 1000
OWN_TENANT_ROWS = 120

_REQUESTED = {
    TARGET_OWN: OWN_TENANT_ROWS,
    TARGET_OTHER: TOTAL_ROWS - OWN_TENANT_ROWS,
    TARGET_ALL: TOTAL_ROWS,
}

ENFORCEMENT_FILTERED = "Enforced by the engine, rows filtered by policy"
ENFORCEMENT_BYPASSED = "Bypassed, the policy was never evaluated"
ENFORCEMENT_INERT = "Inert, the policy exists but is not enabled on the table"

# PostgreSQL: a superuser, and any role holding the BYPASSRLS attribute,
# is exempt from row level security, and FORCE ROW LEVEL SECURITY does not
# reach them. A table owner is exempt by default and is brought back under
# policy by FORCE.
_ALWAYS_EXEMPT = (ROLE_SUPERUSER, ROLE_APP_SERVICE)
_EXEMPT_UNLESS_FORCED = (ROLE_TABLE_OWNER,)


@dataclass(frozen=True)
class RlsVerdict:
    user_role: str
    query_target: str
    rls_enabled: bool
    forced: bool
    enforcement: str
    rows_requested: int
    rows_visible: int
    blocked: bool
    silent: bool
    headline: str
    sql: tuple[str, ...]
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def rows_withheld(self) -> int:
        return self.rows_requested - self.rows_visible


def simulate_data_governance_rls(user_role: str, query_target: str,
                                 rls_enabled: bool = True,
                                 forced: bool = False) -> RlsVerdict:
    """Show what the database engine does, not what the application intends.

    Row level security is worth reaching for precisely because the engine
    applies it to every connection, including one opened by a compromised
    application. The same fact produces its failure modes: the exemptions
    are engine level and cannot be undone from application code.
    """
    role = str(user_role or "").strip()
    target = str(query_target or "").strip()
    if role not in ROLES:
        raise ValueError(f"unknown role {user_role!r}")
    if target not in TARGETS:
        raise ValueError(f"unknown query target {query_target!r}")

    requested = _REQUESTED[target]
    findings: list[Finding] = []

    exempt = role in _ALWAYS_EXEMPT or (
        role in _EXEMPT_UNLESS_FORCED and not forced)

    if not rls_enabled:
        enforcement = ENFORCEMENT_INERT
        visible = requested
        findings.append(Finding(
            code="RLS-INERT", severity=SEVERITY_CRITICAL,
            title="The policy is written and the engine ignores it",
            detail=("CREATE POLICY on its own changes nothing. Until ALTER "
                    "TABLE ... ENABLE ROW LEVEL SECURITY runs, every row is "
                    "returned to every role, and the policy sits in the "
                    "catalogue looking like protection."),
            fix=("ALTER TABLE tenant_data ENABLE ROW LEVEL SECURITY, then "
                 "re run this query as a tenant role and confirm the row "
                 "count drops.")))
    elif exempt:
        enforcement = ENFORCEMENT_BYPASSED
        visible = requested
        if role == ROLE_SUPERUSER:
            findings.append(Finding(
                code="RLS-SUPERUSER", severity=SEVERITY_CRITICAL,
                title="A superuser is exempt and FORCE does not reach it",
                detail=("Row level security is not a boundary a superuser "
                        "can be placed behind. FORCE ROW LEVEL SECURITY "
                        "binds the table owner, and it does not bind a "
                        "superuser or a BYPASSRLS role."),
                fix=("Do not let the application connect as a superuser. "
                     "The tenant boundary has to be a role that is capable "
                     "of being bound.")))
        elif role == ROLE_APP_SERVICE:
            findings.append(Finding(
                code="RLS-BYPASSRLS", severity=SEVERITY_CRITICAL,
                title="This role holds the BYPASSRLS attribute",
                detail=("A role granted BYPASSRLS, often to make a migration "
                        "or a nightly export work, is permanently outside "
                        "every policy on every table. Nothing in the "
                        "application can tell."),
                fix=("ALTER ROLE app_service NOBYPASSRLS, and give the "
                     "export job its own policy instead of an exemption.")))
        else:
            findings.append(Finding(
                code="RLS-OWNER", severity=SEVERITY_CRITICAL,
                title="The table owner is exempt until FORCE is set",
                detail=("This is the exemption that bites in practice: the "
                        "migration role usually owns the table, the "
                        "application usually connects as the migration "
                        "role, and so the application sees every tenant."),
                fix=("ALTER TABLE tenant_data FORCE ROW LEVEL SECURITY, "
                     "which brings the owner under policy.")))
    else:
        enforcement = ENFORCEMENT_FILTERED
        visible = OWN_TENANT_ROWS if target in (TARGET_OWN, TARGET_ALL) else 0
        if role == ROLE_TABLE_OWNER and forced:
            findings.append(Finding(
                code="RLS-FORCED", severity=SEVERITY_OK,
                title="FORCE ROW LEVEL SECURITY has bound the table owner",
                detail=("With FORCE set, the owner is evaluated against the "
                        "same policy as any tenant role, so owning the "
                        "table no longer means reading all of it."),
                fix="Keep FORCE set in the migration that creates the table."))

    blocked = visible < requested
    silent = enforcement == ENFORCEMENT_FILTERED and visible == 0

    if silent:
        findings.append(Finding(
            code="RLS-SILENT", severity=SEVERITY_WARN,
            title="A denial and an empty table are the same response",
            detail=("The engine returns zero rows and raises nothing. An "
                    "application cannot distinguish being refused from "
                    "there being no data, which means a wrong tenant claim "
                    "in a session variable presents as an empty dashboard "
                    "rather than as an error."),
            fix=("Assert the expected tenant claim before the query and "
                 "raise in the application when a query that must return "
                 "rows returns none.")))

    if enforcement == ENFORCEMENT_FILTERED and visible > 0:
        findings.append(Finding(
            code="RLS-ENGINE", severity=SEVERITY_OK,
            title=f"The engine returned {visible} of {requested} row(s)",
            detail=("The filter was applied inside the database, so it holds "
                    "for this query, for a direct psql session, and for any "
                    "ORM that forgets its WHERE clause."),
            fix=("Test it the way it is meant to hold: connect as the tenant "
                 "role in psql and try to read another tenant.")))

    findings.append(Finding(
        code="RLS-CLAIM", severity=SEVERITY_WARN,
        title="The policy is only as honest as the claim it reads",
        detail=("A USING clause that compares against "
                "current_setting('app.tenant_id') is trusting whatever last "
                "set that variable. If the application can set it, so can "
                "anything that reaches the same connection."),
        fix=("Set the claim from the verified session on checkout of a "
             "pooled connection, and RESET it on release.")))

    if enforcement == ENFORCEMENT_BYPASSED:
        headline = f"Bypassed: {role} read {visible} of {requested} row(s)"
    elif enforcement == ENFORCEMENT_INERT:
        headline = f"Not enforced: {role} read all {visible} row(s)"
    elif visible == 0:
        headline = f"Blocked: {role} read 0 of {requested} row(s), no error"
    else:
        headline = f"Enforced: {role} read {visible} of {requested} row(s)"

    sql = (
        "ALTER TABLE tenant_data ENABLE ROW LEVEL SECURITY;"
        if rls_enabled else
        "-- ALTER TABLE tenant_data ENABLE ROW LEVEL SECURITY;  (not run)",
        ("ALTER TABLE tenant_data FORCE ROW LEVEL SECURITY;" if forced else
         "-- ALTER TABLE tenant_data FORCE ROW LEVEL SECURITY;  (not set)"),
        "CREATE POLICY tenant_isolation ON tenant_data",
        "  USING (tenant_id = current_setting('app.tenant_id')::uuid);",
        f"SET ROLE {role};",
        f"-- query target: {target}",
        "SELECT count(*) FROM tenant_data;",
        f"-- engine returned: {visible}",
    )

    return RlsVerdict(
        user_role=role, query_target=target, rls_enabled=bool(rls_enabled),
        forced=bool(forced), enforcement=enforcement,
        rows_requested=requested, rows_visible=visible, blocked=blocked,
        silent=silent, headline=headline, sql=sql,
        findings=tuple(findings))


def rls_truth_table(rls_enabled: bool = True, forced: bool = False
                    ) -> tuple[RlsVerdict, ...]:
    """Every role against every target, so no cell is hidden."""
    return tuple(
        simulate_data_governance_rls(role, target, rls_enabled, forced)
        for role in ROLES for target in TARGETS)
