"""Row Level Security engine for multi tenant PostgreSQL.

No Streamlit import lives here, so the same engine could sit behind a
migration linter or a CI check on a real schema.

Every behaviour this module reproduces was measured against a live PostgreSQL
16.13 server before the file was written, not recalled. The probes and their
results are recorded in PROBES below, because a simulator that disagrees with
the database is worse than no simulator: it teaches the wrong lesson with
confidence.

The five results that shape the whole design:

1. ENABLE ROW LEVEL SECURITY does nothing to the table owner. Measured: the
   owner saw all 3 rows with the policy in place and the tenant context set to
   a tenant owning 2 of them. A non owner saw 2. This is the failure that
   matters most in practice, because the application usually connects as the
   role that ran the migrations, which owns the tables. FORCE ROW LEVEL
   SECURITY is the missing line, and its absence produces no error, no warning
   and no log entry.

2. Policies are PERMISSIVE by default and permissive policies are ORed. Adding
   a second policy widens access. Measured: 2 rows visible, one extra policy
   added, 3 rows visible. Teams add a policy meaning to narrow and they widen.
   AS RESTRICTIVE is the one that ANDs; measured, it held the count at 2.

3. SET leaks across transactions and SET LOCAL does not. Measured: after
   COMMIT, a value set with SET LOCAL was gone and a value set with plain SET
   was still there. Behind a pooler in transaction mode that surviving value is
   the previous tenant's, and the next tenant's query runs under it.

4. RESET does not undefine a custom parameter, it empties it. Measured:
   current_setting on a never set parameter raises SQLSTATE 42704; after SET
   then RESET the same call returns the empty string with no error. A guard
   written as "wrap current_setting and catch the error" therefore fires on a
   fresh connection and stays silent on a recycled one, which is the opposite
   of useful.

5. One thing feared and measured safe. A FOR ALL policy written with only
   USING was expected to leave INSERT unguarded. It does not: PostgreSQL uses
   the USING expression as the WITH CHECK expression when WITH CHECK is
   omitted, and the measured insert of another tenant's row was rejected with
   "new row violates row-level security policy". The generated DDL still writes
   WITH CHECK explicitly, because relying on a default that a later FOR SELECT
   split would silently remove is not worth the saved line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

ENGINE_VERSION = "1.0.0"

SERVER_MEASURED = "PostgreSQL 16.13"
TENANT_GUC = "app.tenant_id"

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_CRIT = "crit"


@dataclass(frozen=True)
class Probe:
    """One thing that was run against a real server, and what came back."""

    name: str
    statement: str
    observed: str
    lesson: str


PROBES: tuple[Probe, ...] = (
    Probe(
        name="Owner with ENABLE only",
        statement=("ALTER TABLE companies ENABLE ROW LEVEL SECURITY; "
                   "SET app.tenant_id = 'acme'; SELECT count(*) FROM companies;"),
        observed="3 of 3 rows, with a policy in place and the context set",
        lesson=("Row Level Security is inert for the table owner until FORCE "
                "is added. Nothing warns you."),
    ),
    Probe(
        name="Non owner with ENABLE only",
        statement=("SET ROLE app_user; SELECT count(*) FROM companies;"),
        observed="2 of 3 rows",
        lesson="The same policy works correctly for a role that owns nothing.",
    ),
    Probe(
        name="Owner after FORCE",
        statement=("ALTER TABLE companies FORCE ROW LEVEL SECURITY; "
                   "SELECT count(*) FROM companies;"),
        observed="2 of 3 rows",
        lesson="FORCE is the line that makes the owner obey its own policy.",
    ),
    Probe(
        name="A second permissive policy",
        statement=("CREATE POLICY reporting_readonly ON companies "
                   "FOR SELECT USING (true); SELECT count(*) FROM companies;"),
        observed="3 of 3 rows, up from 2",
        lesson=("Permissive policies are ORed, so an added policy widens "
                "access. This is the default."),
    ),
    Probe(
        name="The same policy as restrictive",
        statement=("CREATE POLICY reporting_restrictive ON companies "
                   "AS RESTRICTIVE FOR SELECT USING (true); "
                   "SELECT count(*) FROM companies;"),
        observed="2 of 3 rows, unchanged",
        lesson="Restrictive policies are ANDed. This is the one that narrows.",
    ),
    Probe(
        name="SET LOCAL after COMMIT",
        statement=("BEGIN; SET LOCAL app.tenant_id='acme'; COMMIT; "
                   "SELECT current_setting('app.tenant_id', true);"),
        observed="empty, the value did not survive the transaction",
        lesson="SET LOCAL is the scope a pooled connection can be trusted with.",
    ),
    Probe(
        name="Plain SET after COMMIT",
        statement=("BEGIN; SET app.tenant_id='globex'; COMMIT; "
                   "SELECT current_setting('app.tenant_id', true);"),
        observed="globex, the value survived the transaction",
        lesson=("On a pooled connection this is the previous tenant's context "
                "waiting for the next tenant's query."),
    ),
    Probe(
        name="current_setting on a never set parameter",
        statement="SELECT current_setting('app.tenant_id');",
        observed="ERROR 42704 unrecognized configuration parameter",
        lesson="Without the missing_ok argument an unset context is an error.",
    ),
    Probe(
        name="current_setting after SET then RESET",
        statement="SET app.tenant_id='v'; RESET app.tenant_id; "
                  "SELECT current_setting('app.tenant_id');",
        observed="the empty string, and no error",
        lesson=("RESET empties the parameter rather than undefining it, so an "
                "error based guard goes quiet on a recycled connection."),
    ),
    Probe(
        name="A table whose only policies are restrictive",
        statement=("CREATE POLICY ... AS RESTRICTIVE ...; "
                   "SET LOCAL app.tenant_id='acme'; SELECT count(*) "
                   "FROM companies;"),
        observed="0 of the tenant's own 2 rows",
        lesson=("Restrictive policies only narrow what a permissive policy "
                "granted. With none to grant, the table denies everything. "
                "This engine's own first draft of the DDL had that bug and the "
                "server found it."),
    ),
    Probe(
        name="Insert of another tenant's row under FOR ALL USING",
        statement=("INSERT INTO companies VALUES (9, 'globex', 'Sneaky');"),
        observed="ERROR new row violates row-level security policy",
        lesson=("USING is reused as WITH CHECK when WITH CHECK is omitted on "
                "FOR ALL, so this one fails closed."),
    ),
)


# ---------------------------------------------------------------------------
# The sample estate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    name: str
    plan: str


TENANTS: tuple[Tenant, ...] = (
    Tenant("acme", "Acme Industries", "Enterprise"),
    Tenant("globex", "Globex Corporation", "Growth"),
    Tenant("initech", "Initech Systems", "Starter"),
)

TENANT_BY_ID = {t.tenant_id: t for t in TENANTS}


@dataclass(frozen=True)
class Company:
    company_id: int
    tenant_id: str
    name: str
    annual_value_cents: int


COMPANIES: tuple[Company, ...] = (
    Company(101, "acme", "Northwind Trading", 4_800_000),
    Company(102, "acme", "Contoso Freight", 1_250_000),
    Company(103, "globex", "Fabrikam Metals", 9_600_000),
    Company(104, "globex", "Tailspin Logistics", 320_000),
    Company(105, "initech", "Woodgrove Bank", 15_400_000),
)

COMPANY_BY_ID = {c.company_id: c for c in COMPANIES}


# ---------------------------------------------------------------------------
# How the connection is configured, which is what actually decides the outcome
# ---------------------------------------------------------------------------

SCOPE_LOCAL = "SET LOCAL"
SCOPE_SESSION = "SET"
SCOPES = (SCOPE_LOCAL, SCOPE_SESSION)


@dataclass(frozen=True)
class RlsConfig:
    """Everything about the connection that changes whether RLS holds."""

    connect_as_owner: bool = False
    rls_enabled: bool = True
    force_rls: bool = True
    bypassrls_role: bool = False
    # A second permissive policy, of the kind added for a reporting dashboard.
    extra_permissive_policy: bool = False
    extra_policy_restrictive: bool = False
    scope: str = SCOPE_LOCAL
    pooled: bool = True
    missing_ok: bool = True
    set_context: bool = True

    def __post_init__(self) -> None:
        if self.scope not in SCOPES:
            raise ValueError(
                f"scope must be one of {SCOPES}, got {self.scope!r}")


# The configuration a correct deployment uses. Every default here is the safe
# value, so a caller that passes nothing gets the version that works.
SAFE_CONFIG = RlsConfig()

OUTCOME_RETURNED = "Returned"
OUTCOME_BLOCKED = "Blocked by RLS"
OUTCOME_LEAKED = "Returned across tenants"
OUTCOME_ERROR = "Error"

BYPASS_OWNER = "table owner, and FORCE ROW LEVEL SECURITY is not set"
BYPASS_ATTRIBUTE = "the role carries the BYPASSRLS attribute"
BYPASS_DISABLED = "row level security is not enabled on the table"
BYPASS_PERMISSIVE = "a second permissive policy ORs the restriction away"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


@dataclass(frozen=True)
class QueryResult:
    tenant_id: str
    target_company_id: int
    config: RlsConfig
    sql: str
    active_context: str | None
    context_source: str
    rows: Sequence[Company]
    outcome: str
    bypass_reason: str | None
    error: str | None
    findings: Sequence[Finding] = field(default_factory=tuple)

    @property
    def returned(self) -> bool:
        return bool(self.rows)

    @property
    def cross_tenant(self) -> bool:
        """True when a row belonging to another tenant came back."""
        return any(row.tenant_id != self.tenant_id for row in self.rows)

    def row_dicts(self) -> list[dict[str, str]]:
        """Arrow safe: every cell a string."""
        return [{
            "company_id": str(row.company_id),
            "tenant_id": row.tenant_id,
            "name": row.name,
            "annual_value": f"${row.annual_value_cents / 100:,.2f}",
            "Belongs to caller": "yes" if row.tenant_id == self.tenant_id
                                 else "NO",
        } for row in self.rows]


def _policy_predicate(config: RlsConfig) -> str:
    call = (f"current_setting('{TENANT_GUC}', true)" if config.missing_ok
            else f"current_setting('{TENANT_GUC}')")
    return f"tenant_id = {call}"


def build_sql(tenant_id: str, target_company_id: int,
              config: RlsConfig = SAFE_CONFIG) -> str:
    """The statements the application would actually send, in order."""
    lines: list[str] = []
    if config.pooled:
        lines.append("-- connection checked out of the pool")
    if config.scope == SCOPE_LOCAL:
        lines.append("BEGIN;")
    if config.set_context:
        lines.append(f"{config.scope} {TENANT_GUC} = '{tenant_id}';")
    else:
        lines.append(f"-- the application forgot to {config.scope} "
                     f"{TENANT_GUC}")
    lines.append(f"SELECT company_id, tenant_id, name, annual_value_cents")
    lines.append(f"  FROM companies WHERE company_id = {target_company_id};")
    if config.scope == SCOPE_LOCAL:
        lines.append("COMMIT;")
    else:
        lines.append("-- no COMMIT, so the context stays on the connection")
    return "\n".join(lines)


def simulate_rls_query(tenant_id: str, target_company_id: int,
                       config: RlsConfig = SAFE_CONFIG,
                       previous_tenant_id: str | None = None) -> QueryResult:
    """Run one tenant's read of one company through the policy.

    previous_tenant_id is whoever last used this pooled connection. It only
    matters when the context was set with plain SET, which is exactly the
    point: with SET LOCAL it cannot matter, and the engine shows that by
    producing the same answer whatever is passed.
    """
    if tenant_id not in TENANT_BY_ID:
        raise ValueError(f"unknown tenant {tenant_id!r}")
    if target_company_id not in COMPANY_BY_ID:
        raise ValueError(f"unknown company id {target_company_id!r}")
    if previous_tenant_id is not None and previous_tenant_id not in TENANT_BY_ID:
        raise ValueError(f"unknown previous tenant {previous_tenant_id!r}")

    target = COMPANY_BY_ID[target_company_id]
    sql = build_sql(tenant_id, target_company_id, config)

    # What the session variable actually holds when the SELECT runs.
    if config.set_context:
        active = tenant_id
        source = f"{config.scope} on this request"
    elif (config.pooled and config.scope == SCOPE_SESSION
            and previous_tenant_id is not None):
        # Measured: a plain SET survives COMMIT, so the value left behind by
        # the previous checkout is still live.
        active = previous_tenant_id
        source = (f"left on the pooled connection by tenant "
                  f"{previous_tenant_id}")
    else:
        active = None
        source = "never set on this connection"

    error: str | None = None
    if active is None and not config.missing_ok:
        # Measured: SQLSTATE 42704 on a parameter that was never set.
        error = (f'ERROR: 42704 unrecognized configuration parameter '
                 f'"{TENANT_GUC}"')
        result = QueryResult(
            tenant_id=tenant_id, target_company_id=target_company_id,
            config=config, sql=sql, active_context=None, context_source=source,
            rows=(), outcome=OUTCOME_ERROR, bypass_reason=None, error=error,
        )
        return _with_findings(result)

    # Does the policy get consulted at all?
    bypass: str | None = None
    if not config.rls_enabled:
        bypass = BYPASS_DISABLED
    elif config.bypassrls_role:
        bypass = BYPASS_ATTRIBUTE
    elif config.connect_as_owner and not config.force_rls:
        bypass = BYPASS_OWNER

    if bypass is not None:
        rows: tuple[Company, ...] = (target,)
    elif config.extra_permissive_policy and not config.extra_policy_restrictive:
        # Measured: permissive policies are ORed, so the added USING (true)
        # makes every row visible regardless of the tenant predicate.
        bypass = BYPASS_PERMISSIVE
        rows = (target,)
    else:
        rows = (target,) if active == target.tenant_id else ()

    if not rows:
        outcome = OUTCOME_BLOCKED
    elif any(row.tenant_id != tenant_id for row in rows):
        outcome = OUTCOME_LEAKED
    else:
        outcome = OUTCOME_RETURNED

    result = QueryResult(
        tenant_id=tenant_id, target_company_id=target_company_id,
        config=config, sql=sql, active_context=active, context_source=source,
        rows=rows, outcome=outcome, bypass_reason=bypass, error=error,
    )
    return _with_findings(result)


def _with_findings(result: QueryResult) -> QueryResult:
    return QueryResult(
        tenant_id=result.tenant_id,
        target_company_id=result.target_company_id,
        config=result.config, sql=result.sql,
        active_context=result.active_context,
        context_source=result.context_source, rows=result.rows,
        outcome=result.outcome, bypass_reason=result.bypass_reason,
        error=result.error, findings=tuple(audit_query(result)),
    )


def audit_query(result: QueryResult) -> list[Finding]:
    config = result.config
    findings: list[Finding] = []

    if result.bypass_reason == BYPASS_OWNER:
        findings.append(Finding(
            code="RLS-OWNER",
            severity=SEVERITY_CRIT,
            title="The policy is in place and the owner ignores it",
            detail=(
                "ENABLE ROW LEVEL SECURITY does not apply to the table owner. "
                "The application connects as the role that ran the migrations, "
                "that role owns the tables, and so every policy on them is "
                f"advisory. Measured on {SERVER_MEASURED}: the owner saw all 3 "
                "rows while a non owner saw 2 under the same policy."
            ),
            fix=("ALTER TABLE companies FORCE ROW LEVEL SECURITY, or connect "
                 "the application as a role that owns nothing. Both are one "
                 "line, and the absence of either is silent."),
        ))

    if result.bypass_reason == BYPASS_ATTRIBUTE:
        findings.append(Finding(
            code="RLS-BYPASSRLS",
            severity=SEVERITY_CRIT,
            title="The role carries BYPASSRLS",
            detail=("FORCE does not help here. The attribute is on the role, "
                    "so every policy on every table is skipped for this "
                    "connection."),
            fix=("ALTER ROLE app_user NOBYPASSRLS, and keep the attribute for "
                 "migrations and backups only."),
        ))

    if result.bypass_reason == BYPASS_DISABLED:
        findings.append(Finding(
            code="RLS-DISABLED",
            severity=SEVERITY_CRIT,
            title="The policies exist and are not enforced",
            detail=("A CREATE POLICY statement does not enable row level "
                    "security. The policy sits in pg_policy doing nothing "
                    "until the table is switched on."),
            fix="ALTER TABLE companies ENABLE ROW LEVEL SECURITY, then FORCE.",
        ))

    if result.bypass_reason == BYPASS_PERMISSIVE:
        findings.append(Finding(
            code="RLS-PERMISSIVE",
            severity=SEVERITY_CRIT,
            title="The second policy widened access instead of narrowing it",
            detail=(
                "Policies are PERMISSIVE unless declared otherwise, and "
                "permissive policies are combined with OR. The reporting "
                "policy grants USING (true), so it grants everything. "
                f"Measured on {SERVER_MEASURED}: 2 rows visible, one policy "
                "added, 3 rows visible."
            ),
            fix=("Declare it AS RESTRICTIVE so it is ANDed, or scope its USING "
                 "clause to the same tenant predicate."),
        ))

    if config.scope == SCOPE_SESSION and config.pooled:
        findings.append(Finding(
            code="RLS-POOL-LEAK",
            severity=SEVERITY_CRIT,
            title="A session scoped SET outlives the request on a pooled "
                  "connection",
            detail=(
                "Plain SET survives COMMIT, measured. In transaction pooling "
                "the connection goes back to the pool with the last tenant's "
                "value still on it, so a request that forgets to set its own "
                "context runs under someone else's."
            ),
            fix=("SET LOCAL inside an explicit transaction. The value is gone "
                 "at COMMIT, so a forgotten context can only ever fail closed."),
        ))

    if not config.missing_ok:
        findings.append(Finding(
            code="RLS-MISSING-OK",
            severity=SEVERITY_WARN,
            title="current_setting without missing_ok turns a gap into an error",
            detail=(
                "On a connection that never set the parameter this raises "
                "SQLSTATE 42704. After a SET followed by a RESET the same call "
                "returns the empty string with no error, measured, so the "
                "error based guard fires on a fresh connection and stays quiet "
                "on a recycled one."
            ),
            fix=("Pass true as the second argument and let the comparison to "
                 "NULL return no rows, then assert the context separately at "
                 "the start of the transaction."),
        ))

    if not config.set_context and result.outcome == OUTCOME_BLOCKED:
        findings.append(Finding(
            code="RLS-SILENT-ZERO",
            severity=SEVERITY_WARN,
            title="A missing context looks exactly like no data",
            detail=("The comparison against NULL returned no rows, which is "
                    "the safe answer and an indistinguishable one. The page "
                    "renders an empty table and nobody is paged."),
            fix=("Raise on a missing context at the start of the transaction "
                 "so the empty result can only mean empty."),
        ))

    if result.outcome == OUTCOME_LEAKED:
        owner = TENANT_BY_ID[COMPANY_BY_ID[result.target_company_id].tenant_id]
        findings.append(Finding(
            code="RLS-CROSS-TENANT",
            severity=SEVERITY_CRIT,
            title="This query returned another tenant's row",
            detail=(f"{TENANT_BY_ID[result.tenant_id].name} asked for company "
                    f"{result.target_company_id}, which belongs to "
                    f"{owner.name}, and received it."),
            fix="Fix the bypass named above. The predicate itself is correct.",
        ))

    if not findings:
        findings.append(Finding(
            code="RLS-HELD",
            severity=SEVERITY_OK,
            title="The policy held",
            detail=(f"Context {result.active_context} was applied with "
                    f"{config.scope}, the predicate was evaluated, and the "
                    f"result was {result.outcome.lower()}."),
            fix="Nothing to change on this path.",
        ))
    return findings


def isolation_matrix(
        config: RlsConfig = SAFE_CONFIG,
        previous_tenant_id: str | None = None) -> list[dict[str, str]]:
    """Every tenant against every company, which is the only honest test.

    A single query proves nothing about isolation. The whole grid does, and a
    leak shows up as a cell rather than as a claim.
    """
    rows: list[dict[str, str]] = []
    for tenant in TENANTS:
        row: dict[str, str] = {"Querying tenant": tenant.name}
        for company in COMPANIES:
            result = simulate_rls_query(tenant.tenant_id, company.company_id,
                                        config, previous_tenant_id)
            if result.outcome == OUTCOME_LEAKED:
                mark = "LEAK"
            elif result.outcome == OUTCOME_ERROR:
                mark = "error"
            elif result.returned:
                mark = "visible"
            else:
                mark = "blocked"
            row[f"{company.company_id} {company.name}"] = mark
        rows.append(row)
    return rows


def leak_count(config: RlsConfig = SAFE_CONFIG,
               previous_tenant_id: str | None = None) -> int:
    """How many of the tenant by company pairs cross a tenant boundary."""
    return sum(
        1 for tenant in TENANTS for company in COMPANIES
        if simulate_rls_query(tenant.tenant_id, company.company_id, config,
                              previous_tenant_id).outcome == OUTCOME_LEAKED)


# ---------------------------------------------------------------------------
# Architecture trade offs
# ---------------------------------------------------------------------------

MODEL_SHARED = "Shared Schema RLS"
MODEL_SCHEMA = "Schema per Tenant"
MODEL_DATABASE = "Database per Tenant"


@dataclass(frozen=True)
class ArchitectureModel:
    name: str
    isolation: str
    operational_complexity: str
    cost: str
    # Objects added to the catalog for each tenant onboarded.
    catalog_objects_per_tenant: int
    # Connection pools the application has to keep open.
    pools_required: str
    migration_cost: str
    blast_radius: str
    noisy_neighbour: str
    breaks_at: str
    best_for: str


TABLES_PER_TENANT = 40


ARCHITECTURE_MODELS: tuple[ArchitectureModel, ...] = (
    ArchitectureModel(
        name=MODEL_SHARED,
        isolation="Logical, enforced by the database on every row",
        operational_complexity="Low, one schema and one migration",
        cost="Lowest, one database and one pool",
        catalog_objects_per_tenant=0,
        pools_required="One, shared by every tenant",
        migration_cost="One ALTER TABLE, whatever the tenant count",
        blast_radius=("A policy mistake exposes every tenant at once, which is "
                      "why the policy is the thing to test"),
        noisy_neighbour="Real. One tenant's heavy query touches everyone",
        breaks_at=("Nothing structural. The limit is whether every query path "
                   "is covered by a policy"),
        best_for="Many small tenants and a team that can hold one schema right",
    ),
    ArchitectureModel(
        name=MODEL_SCHEMA,
        isolation="Namespace, enforced by search_path and GRANT",
        operational_complexity="High, one migration run per tenant",
        cost="Moderate, one database and a larger catalog",
        catalog_objects_per_tenant=TABLES_PER_TENANT,
        pools_required="One, with search_path set per checkout",
        migration_cost="One ALTER TABLE per tenant, run in a loop",
        blast_radius="A mistake usually touches one tenant",
        noisy_neighbour="Real, the tenants still share one server",
        breaks_at=("Catalog size. Each tenant adds its own tables to pg_class, "
                   "and autovacuum, pg_dump and planning all slow with it"),
        best_for="Tens to low hundreds of tenants with per tenant variation",
    ),
    ArchitectureModel(
        name=MODEL_DATABASE,
        isolation="Physical, nothing shared but the server",
        operational_complexity="Highest, one database to run per tenant",
        cost="Highest, per tenant connections, backups and idle capacity",
        catalog_objects_per_tenant=TABLES_PER_TENANT,
        pools_required="One per tenant, and that is the constraint",
        migration_cost="One deploy per database, with partial failure to handle",
        blast_radius="One tenant, and restores are per tenant",
        noisy_neighbour=("Reduced, though shared CPU and disk still bite on one "
                         "server"),
        breaks_at=("Connections. Every database needs its own pool, so the "
                   "backend count rises with the tenant count"),
        best_for="Few large tenants with contractual isolation requirements",
    ),
)

MODEL_BY_NAME = {m.name: m for m in ARCHITECTURE_MODELS}


def get_architecture_tradeoffs() -> list[ArchitectureModel]:
    return list(ARCHITECTURE_MODELS)


def tradeoff_rows() -> list[dict[str, str]]:
    return [{
        "Model": m.name,
        "Isolation": m.isolation,
        "Operational complexity": m.operational_complexity,
        "Cost": m.cost,
        "Blast radius": m.blast_radius,
        "Breaks at": m.breaks_at,
    } for m in ARCHITECTURE_MODELS]


def scaling_rows(tenant_count: int,
                 pool_size: int = 10) -> list[dict[str, str]]:
    """What each model costs at a given tenant count.

    The two numbers that decide this in practice are catalog objects and
    backend connections, and neither appears on a feature comparison chart.
    """
    if tenant_count < 1:
        raise ValueError("tenant_count must be at least 1")
    if pool_size < 1:
        raise ValueError("pool_size must be at least 1")
    rows: list[dict[str, str]] = []
    for model in ARCHITECTURE_MODELS:
        objects = model.catalog_objects_per_tenant * tenant_count
        if model.name == MODEL_DATABASE:
            backends = pool_size * tenant_count
            migrations = tenant_count
        elif model.name == MODEL_SCHEMA:
            backends = pool_size
            migrations = tenant_count
        else:
            objects = TABLES_PER_TENANT
            backends = pool_size
            migrations = 1
        rows.append({
            "Model": model.name,
            "Tables in the catalog": f"{objects:,}",
            "Backend connections": f"{backends:,}",
            "Migration runs per release": f"{migrations:,}",
            "Pools to operate": model.pools_required,
        })
    return rows


# ---------------------------------------------------------------------------
# The DDL
# ---------------------------------------------------------------------------

SAMPLE_RLS_DDL = """\
-- Multi tenant isolation for PostgreSQL, verified on PostgreSQL 16.
--
-- Three lines here are the ones that get left out, and none of the three
-- produces an error when missing:
--   FORCE ROW LEVEL SECURITY   without it the table owner ignores the policy
--   AS RESTRICTIVE on additions  without it an added policy widens access,
--                                and with it on the FIRST policy the table
--                                denies everything to everyone
--   WITH CHECK                 without it a later FOR SELECT split loses writes

BEGIN;

-- 1. A role that owns nothing. The application must not connect as the role
--    that ran this script, because that role owns these tables.
CREATE ROLE app_user LOGIN PASSWORD :'app_password' NOBYPASSRLS;

CREATE TABLE companies (
    company_id          bigserial PRIMARY KEY,
    tenant_id           text        NOT NULL,
    name                text        NOT NULL,
    annual_value_cents  bigint      NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- The tenant column leads every index. A policy predicate that cannot use an
-- index turns isolation into a sequential scan on every request.
CREATE INDEX companies_tenant_idx ON companies (tenant_id, company_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON companies TO app_user;
GRANT USAGE, SELECT ON SEQUENCE companies_company_id_seq TO app_user;

-- 2. Enable, then FORCE. ENABLE alone leaves the owner exempt.
ALTER TABLE companies ENABLE ROW LEVEL SECURITY;
ALTER TABLE companies FORCE  ROW LEVEL SECURITY;

-- 3. One helper, so the predicate is written once and indexed the same way
--    everywhere. STABLE, not IMMUTABLE: the value changes between statements.
CREATE FUNCTION current_tenant_id() RETURNS text
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$ SELECT nullif(current_setting('app.tenant_id', true), '') $$;

-- 4. The isolation policy, and it must be PERMISSIVE. An enabled table with no
--    permissive policy denies every row to everyone: restrictive policies only
--    narrow what a permissive policy already granted. The first draft of this
--    script made both policies restrictive and the tenant saw zero of its own
--    rows, which is how that line came to be commented.
--    WITH CHECK is written out even though FOR ALL would reuse USING, so that
--    splitting this into per command policies later cannot drop the write side.
CREATE POLICY tenant_isolation ON companies
    FOR ALL
    TO app_user
    USING      (tenant_id = current_tenant_id())
    WITH CHECK (tenant_id = current_tenant_id());

-- 5. A restrictive policy, ANDed on top, that refuses to run without a
--    context. Without it a forgotten SET LOCAL returns zero rows, which is
--    safe and indistinguishable from a tenant that genuinely has no data.
--    This is the policy that should be restrictive. Any later addition should
--    be too, because a permissive addition is ORed and widens access.
CREATE POLICY tenant_context_required ON companies
    AS RESTRICTIVE
    FOR ALL
    TO app_user
    USING      (current_tenant_id() IS NOT NULL)
    WITH CHECK (current_tenant_id() IS NOT NULL);

COMMIT;

-- 6. Per request, on a pooled connection. SET LOCAL, never SET: a plain SET
--    survives COMMIT and goes back into the pool carrying this tenant's id.
BEGIN;
SET LOCAL app.tenant_id = :'tenant_id';
SELECT company_id, name, annual_value_cents FROM companies;
COMMIT;
"""


def get_sample_rls_ddl() -> str:
    return SAMPLE_RLS_DDL


def ddl_checklist() -> list[dict[str, str]]:
    """The lines whose absence is silent, and what each one costs."""
    return [
        {"Line": "ALTER TABLE ... FORCE ROW LEVEL SECURITY",
         "If it is missing": "the table owner reads and writes every tenant",
         "Symptom": "none, the query simply succeeds",
         "Measured": "owner saw 3 of 3 rows, non owner saw 2"},
        {"Line": "AS RESTRICTIVE on added policies, never on the first",
         "If it is missing": "the new policy is ORed and widens access",
         "Symptom": "more rows than before, on a change meant to allow fewer",
         "Measured": "2 rows, one permissive policy added, 3 rows"},
        {"Line": "One PERMISSIVE policy must grant before any narrows",
         "If it is missing": "the table denies every row to everyone",
         "Symptom": "the tenant sees zero of its own rows",
         "Measured": "all restrictive, tenant saw 0 of its 2 rows"},
        {"Line": "SET LOCAL rather than SET",
         "If it is missing": "the context returns to the pool with the tenant",
         "Symptom": "correct until traffic overlaps, then wrong intermittently",
         "Measured": "plain SET still readable after COMMIT"},
        {"Line": "nullif(current_setting(..., true), '')",
         "If it is missing": "a RESET leaves the empty string, not an error",
         "Symptom": "an error based guard passes on a recycled connection",
         "Measured": "42704 when never set, empty string after RESET"},
        {"Line": "NOBYPASSRLS on the application role",
         "If it is missing": "FORCE and policies are both skipped",
         "Symptom": "none",
         "Measured": "role attribute overrides table settings"},
        {"Line": "tenant_id first in every index",
         "If it is missing": "the policy predicate cannot use an index",
         "Symptom": "isolation works and latency grows with the whole table",
         "Measured": "not a correctness issue, a cost one"},
    ]


def probe_rows() -> list[dict[str, str]]:
    return [{
        "Probe": p.name,
        "Statement": " ".join(p.statement.split()),
        "Observed": p.observed,
        "What it means": p.lesson,
    } for p in PROBES]
