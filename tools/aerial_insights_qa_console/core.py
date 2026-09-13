"""Aerial Insights QA and production launch engine.

Four things stand between a working demo and a production launch, and each one
is invisible until the load arrives:

  Two workers claim the same tile because the claim is a read then a write
  rather than one atomic statement, so the same image is inferred twice and
  written twice. A worker's resident memory climbs a few megabytes per tile
  until the container kills it, and the tile it was holding disappears. Every
  serverless instance opens its own Prisma pool, so the database runs out of
  connections at a concurrency the staging environment never reached. And a
  Stripe webhook is delivered twice, because Stripe retries until it gets a
  2xx.

None of the four is hard to fix. All four are hard to see, so this makes each
one visible, states the error code the real system returns, and shows the same
run with the fix applied.

Pure logic, no Streamlit import, so this is unit testable on its own.
Deterministic: nothing here reads the clock or a random source. The scheduler
is tick based rather than threaded, which is what makes a race reproducible
instead of occasional.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# The YOLO worker queue
# ---------------------------------------------------------------------------

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
LOST = "lost to an OOM kill"

TASK_TICKS = 3               # ticks one tile takes to infer
BASE_RSS_MB = 512            # a worker with the model loaded and nothing else
LEAK_MB_PER_TASK = 180       # tensors and decoded images never released
CONTAINER_LIMIT_MB = 2048


@dataclass
class Tile:
    task_id: str
    tile_ref: str
    megapixels: float
    status: str = QUEUED
    claims: int = 0          # how many workers have ever claimed it
    attempts: int = 0
    worker: str = ""
    finished_tick: int = 0

    @property
    def double_claimed(self) -> bool:
        return self.claims > 1


@dataclass
class Worker:
    name: str
    rss_mb: int = BASE_RSS_MB
    restarts: int = 0
    completed: int = 0
    task_id: str = ""
    busy_until: int = 0

    @property
    def idle(self) -> bool:
        return not self.task_id


@dataclass(frozen=True)
class Finding:
    code: str
    title: str
    severity: str
    detail: str
    fix: str


SEVERITY_CRITICAL = "Critical"
SEVERITY_HIGH = "High"
SEVERITY_OK = "Healthy"

CODE_RACE = "DOUBLE_CLAIM"
CODE_LEAK = "RSS_GROWTH"
CODE_OOM = "OOM_KILL"
CODE_CLEAN = "NO_FINDINGS"


@dataclass(frozen=True)
class QueueRun:
    tiles: list
    workers: list
    ticks: int
    atomic_claim: bool
    releases_memory: bool
    findings: list

    @property
    def double_claimed(self) -> int:
        return sum(1 for tile in self.tiles if tile.double_claimed)

    @property
    def wasted_inferences(self) -> int:
        """Every claim beyond the first is an inference nobody asked for."""
        return sum(max(0, tile.claims - 1) for tile in self.tiles)

    @property
    def lost(self) -> int:
        return sum(1 for tile in self.tiles if tile.status == LOST)

    @property
    def completed(self) -> int:
        return sum(1 for tile in self.tiles if tile.status == DONE)

    @property
    def restarts(self) -> int:
        return sum(worker.restarts for worker in self.workers)

    @property
    def healthy(self) -> bool:
        return not any(finding.severity != SEVERITY_OK
                       for finding in self.findings)

    def tile_rows(self) -> list[dict]:
        return [
            {"Task": tile.task_id, "Tile": tile.tile_ref,
             "Megapixels": f"{tile.megapixels:.1f}",
             "Claims": tile.claims, "Attempts": tile.attempts,
             "Worker": tile.worker or "none", "Status": tile.status}
            for tile in self.tiles
        ]

    def worker_rows(self) -> list[dict]:
        return [
            {"Worker": worker.name, "Resident MB": worker.rss_mb,
             "Tiles completed": worker.completed,
             "OOM restarts": worker.restarts,
             "Headroom MB": CONTAINER_LIMIT_MB - worker.rss_mb}
            for worker in self.workers
        ]

    def finding_rows(self) -> list[dict]:
        return [
            {"Code": finding.code, "Severity": finding.severity,
             "Finding": finding.title, "What to change": finding.fix}
            for finding in self.findings
        ]


def sample_tiles(count: int = 8) -> list[Tile]:
    """A batch of geospatial tiles. Built by rule so a run is reproducible."""
    if count < 0:
        raise ValueError("A tile count cannot be negative.")
    return [
        Tile(task_id=f"TASK-{index + 1:03d}",
             tile_ref=f"OS-{51 + index // 4}N-{(index % 4) * 25:03d}W",
             megapixels=round(12.0 + (index % 5) * 3.5, 1))
        for index in range(count)
    ]


def run_queue(tiles: list[Tile] | None = None, worker_count: int = 3,
              atomic_claim: bool = False, releases_memory: bool = False,
              max_ticks: int = 200) -> QueueRun:
    """Run the batch through the workers, tick by tick.

    The race is not simulated with randomness. It is the actual shape of the
    bug: without an atomic claim, every worker that polls on the same tick
    reads the same head row as queued and takes it. Modelling it as a dice roll
    would be dishonest, because the bug is deterministic given the poll pattern
    and that is exactly why it survives a staging environment with one worker.
    """
    tiles = sample_tiles() if tiles is None else tiles
    if worker_count < 1:
        raise ValueError("A queue needs at least one worker.")
    workers = [Worker(f"worker-{index + 1}") for index in range(worker_count)]

    tick = 0
    while tick < max_ticks and any(tile.status in (QUEUED, RUNNING)
                                   for tile in tiles):
        tick += 1

        # Finish anything that is due.
        for worker in workers:
            if worker.task_id and worker.busy_until <= tick:
                tile = next(t for t in tiles if t.task_id == worker.task_id)
                tile.status = DONE
                tile.finished_tick = tick
                worker.completed += 1
                worker.task_id = ""
                if not releases_memory:
                    worker.rss_mb += LEAK_MB_PER_TASK
                if worker.rss_mb > CONTAINER_LIMIT_MB:
                    worker.restarts += 1
                    worker.rss_mb = BASE_RSS_MB

        # Claim new work. Every idle worker polls on the same tick, which is
        # what a queue of identical workers actually does.
        idle = [worker for worker in workers if worker.idle]
        if atomic_claim:
            for worker in idle:
                pending = next((t for t in tiles if t.status == QUEUED), None)
                if pending is None:
                    break
                _claim(pending, worker, tick)
        else:
            head = next((t for t in tiles if t.status == QUEUED), None)
            if head is not None:
                for worker in idle:
                    _claim(head, worker, tick)

        # A worker that dies mid tile takes the tile with it.
        for worker in workers:
            if worker.task_id and worker.rss_mb + LEAK_MB_PER_TASK > \
                    CONTAINER_LIMIT_MB and not releases_memory:
                tile = next(t for t in tiles if t.task_id == worker.task_id)
                tile.status = LOST
                worker.restarts += 1
                worker.rss_mb = BASE_RSS_MB
                worker.task_id = ""

    return QueueRun(tiles=tiles, workers=workers, ticks=tick,
                    atomic_claim=atomic_claim,
                    releases_memory=releases_memory,
                    findings=_diagnose(tiles, workers, atomic_claim,
                                       releases_memory))


def _claim(tile: Tile, worker: Worker, tick: int) -> None:
    tile.claims += 1
    tile.attempts += 1
    tile.status = RUNNING
    tile.worker = worker.name
    worker.task_id = tile.task_id
    worker.busy_until = tick + TASK_TICKS


def _diagnose(tiles: list[Tile], workers: list[Worker], atomic_claim: bool,
              releases_memory: bool) -> list[Finding]:
    findings: list[Finding] = []
    doubles = [tile for tile in tiles if tile.double_claimed]
    wasted = sum(max(0, tile.claims - 1) for tile in tiles)
    restarts = sum(worker.restarts for worker in workers)
    lost = [tile for tile in tiles if tile.status == LOST]

    if doubles:
        findings.append(Finding(
            CODE_RACE, "Two workers claimed the same tile", SEVERITY_CRITICAL,
            f"{len(doubles)} tile(s) were claimed more than once and "
            f"{wasted} inference(s) ran that nobody asked for. Each one is GPU "
            f"time paid for twice and a second write of the same detections.",
            "Claim in one statement: UPDATE tasks SET status = 'running' "
            "WHERE id = (SELECT id FROM tasks WHERE status = 'queued' ORDER BY "
            "created_at FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING id."))

    if not releases_memory:
        findings.append(Finding(
            CODE_LEAK, "Resident memory grows with every tile", SEVERITY_HIGH,
            f"Each tile adds about {LEAK_MB_PER_TASK} MB that is never "
            f"returned, so a worker crosses the {CONTAINER_LIMIT_MB} MB "
            f"container limit after "
            f"{(CONTAINER_LIMIT_MB - BASE_RSS_MB) // LEAK_MB_PER_TASK} "
            f"tiles. It looks like a slow leak in staging and an hourly crash "
            f"in production.",
            "Release the tensors and the decoded image inside the task scope, "
            "and run inference under torch.inference_mode so the graph is "
            "never retained."))

    if restarts:
        findings.append(Finding(
            CODE_OOM, "Workers were killed and restarted", SEVERITY_CRITICAL,
            f"{restarts} restart(s), and {len(lost)} tile(s) disappeared with "
            f"the worker holding them. A tile lost this way is not retried, "
            f"because the row still reads running.",
            "Fix the leak, and make the claim reclaim anything running for "
            "longer than the visibility timeout so a killed worker cannot "
            "strand a row."))

    if not findings:
        findings.append(Finding(
            CODE_CLEAN, "The batch ran clean", SEVERITY_OK,
            f"{len(tiles)} tile(s), each claimed exactly once, no restarts and "
            f"no tile left running.",
            "Nothing to change."))
    return findings


# ---------------------------------------------------------------------------
# Prisma connection pooling
# ---------------------------------------------------------------------------

# Postgres errors as Prisma surfaces them, which is what appears in the logs.
ERR_TOO_MANY = "P2037"       # too many database connections opened
ERR_POOL_TIMEOUT = "P2024"   # timed out fetching a connection from the pool

POOL_DIRECT = "Direct connections"
POOL_PGBOUNCER = "PgBouncer, transaction mode"


@dataclass(frozen=True)
class PoolConfig:
    max_connections: int = 100
    reserved_for_superuser: int = 3
    instances: int = 40                 # concurrent serverless instances
    connection_limit: int = 5           # Prisma pool per instance
    pool_timeout_s: int = 10
    pgbouncer: bool = False
    pgbouncer_pool_size: int = 20

    @property
    def available(self) -> int:
        return max(0, self.max_connections - self.reserved_for_superuser)

    @property
    def demanded(self) -> int:
        """Connections actually opened against Postgres.

        With PgBouncer in transaction mode the instances talk to the bouncer
        and the bouncer holds a small fixed pool against the database, which is
        the entire point: client count stops being the database's problem.
        """
        if self.pgbouncer:
            return min(self.pgbouncer_pool_size, self.available)
        return self.instances * self.connection_limit

    @property
    def headroom(self) -> int:
        return self.available - self.demanded

    @property
    def exhausted(self) -> bool:
        return self.demanded > self.available

    @property
    def utilisation(self) -> float:
        return round(self.demanded / self.available, 4) if self.available else 0.0

    @property
    def mode(self) -> str:
        return POOL_PGBOUNCER if self.pgbouncer else POOL_DIRECT

    def rows(self) -> list[dict]:
        return [
            {"Measure": "Postgres max_connections",
             "Value": str(self.max_connections)},
            {"Measure": "Reserved for superuser",
             "Value": str(self.reserved_for_superuser)},
            {"Measure": "Available to the application",
             "Value": str(self.available)},
            {"Measure": "Concurrent instances", "Value": str(self.instances)},
            {"Measure": "Prisma connection_limit per instance",
             "Value": str(self.connection_limit)},
            {"Measure": "Connections opened against Postgres",
             "Value": str(self.demanded)},
            {"Measure": "Headroom", "Value": str(self.headroom)},
        ]


PRODUCTION_URL_BROKEN = (
    "postgresql://app:${DB_PASSWORD}@db.internal:5432/aerial?"
    "connection_limit=5")
PRODUCTION_URL_FIXED = (
    "postgresql://app:${DB_PASSWORD}@bouncer.internal:6432/aerial?"
    "pgbouncer=true&connection_limit=1&pool_timeout=20")


def audit_pool(config: PoolConfig = PoolConfig()) -> list[Finding]:
    """Read a pool configuration the way an incident review would."""
    findings: list[Finding] = []

    if config.exhausted:
        findings.append(Finding(
            ERR_TOO_MANY, "The database runs out of connections",
            SEVERITY_CRITICAL,
            f"{config.instances} instance(s) holding {config.connection_limit} "
            f"connection(s) each demand {config.demanded} against "
            f"{config.available} available. Postgres refuses the rest and "
            f"Prisma raises {ERR_TOO_MANY}. Staging never reaches it because "
            f"staging never runs {config.instances} instances at once.",
            f"Put PgBouncer in transaction mode in front and set the URL to "
            f"{PRODUCTION_URL_FIXED}"))
    elif config.utilisation > 0.8:
        findings.append(Finding(
            ERR_POOL_TIMEOUT, "The pool is close to its limit", SEVERITY_HIGH,
            f"{config.demanded} of {config.available} connections are "
            f"committed, which is {config.utilisation:.0%}. A traffic spike or "
            f"one slow query fills the rest and requests start failing with "
            f"{ERR_POOL_TIMEOUT} after {config.pool_timeout_s} seconds.",
            "Add PgBouncer, or lower connection_limit so a spike cannot "
            "exhaust the database."))

    if config.pgbouncer and config.connection_limit > 1:
        findings.append(Finding(
            ERR_POOL_TIMEOUT, "PgBouncer is in front and Prisma still pools",
            SEVERITY_HIGH,
            f"Each instance keeps {config.connection_limit} connections to the "
            f"bouncer, which is pooling on its behalf. Two pools in series "
            f"queue twice and the outer one times out first.",
            "Set connection_limit=1 when pgbouncer=true. The bouncer is the "
            "pool."))

    if not config.pgbouncer and config.pool_timeout_s < 10:
        findings.append(Finding(
            ERR_POOL_TIMEOUT, "The pool timeout is shorter than a cold query",
            SEVERITY_HIGH,
            f"{config.pool_timeout_s} seconds is less than a cold query on a "
            f"loaded instance, so requests fail while connections are still "
            f"being handed out.",
            "Raise pool_timeout to at least 10 seconds while the pool is "
            "under pressure, and fix the pressure."))

    if not findings:
        findings.append(Finding(
            CODE_CLEAN, "The pool configuration holds", SEVERITY_OK,
            f"{config.demanded} of {config.available} connections in use "
            f"through {config.mode.lower()}, leaving {config.headroom} spare.",
            "Nothing to change."))
    return findings


def pool_comparison(config: PoolConfig) -> list[dict]:
    """The same load with and without the bouncer, side by side."""
    direct = PoolConfig(**{**config.__dict__, "pgbouncer": False})
    bounced = PoolConfig(**{**config.__dict__, "pgbouncer": True,
                            "connection_limit": 1})
    return [
        {"Measure": "Connections opened",
         "Direct": str(direct.demanded), "With PgBouncer": str(bounced.demanded)},
        {"Measure": "Available", "Direct": str(direct.available),
         "With PgBouncer": str(bounced.available)},
        {"Measure": "Headroom", "Direct": str(direct.headroom),
         "With PgBouncer": str(bounced.headroom)},
        {"Measure": "Outcome",
         "Direct": ERR_TOO_MANY if direct.exhausted else "Holds",
         "With PgBouncer": ERR_TOO_MANY if bounced.exhausted else "Holds"},
    ]


# ---------------------------------------------------------------------------
# Stripe webhooks
# ---------------------------------------------------------------------------

# A local test string, deliberately not shaped like a real signing secret.
# Nothing in a repository should look like a credential even when it is not
# one, and a scanner refusing the push is right to.
TEST_SIGNING_SECRET = "test-signing-secret-not-a-credential"
SIGNATURE_TOLERANCE_S = 300

ACCEPTED = "Accepted"
REPLAYED = "Duplicate blocked"
BAD_SIGNATURE = "Signature rejected"
STALE = "Timestamp outside tolerance"
WRONG_MODE = "Livemode mismatch"


@dataclass(frozen=True)
class WebhookDelivery:
    event_id: str
    event_type: str
    payload: dict
    timestamp: int
    signature: str = ""
    livemode: bool = True


@dataclass(frozen=True)
class WebhookResult:
    delivery: WebhookDelivery
    outcome: str
    detail: str
    charged_cents: int = 0
    stored_response: str = ""

    @property
    def accepted(self) -> bool:
        return self.outcome == ACCEPTED


def sign(payload: dict, timestamp: int,
         secret: str = TEST_SIGNING_SECRET) -> str:
    """The Stripe-Signature header value, computed the way Stripe computes it.

    The signed payload is the timestamp, a full stop, and the exact request
    body. Signing a re-serialised body is the mistake that makes every
    signature fail in production and pass in a test that serialises the same
    way twice.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hmac.new(secret.encode("utf-8"),
                      f"{timestamp}.{body}".encode("utf-8"),
                      hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify(delivery: WebhookDelivery, now: int,
           secret: str = TEST_SIGNING_SECRET,
           tolerance: int = SIGNATURE_TOLERANCE_S) -> tuple[bool, str]:
    expected = sign(delivery.payload, delivery.timestamp, secret)
    if not hmac.compare_digest(expected, delivery.signature or ""):
        return False, BAD_SIGNATURE
    if abs(now - delivery.timestamp) > tolerance:
        return False, STALE
    return True, ""


@dataclass
class WebhookLedger:
    handled: dict = field(default_factory=dict)
    results: list = field(default_factory=list)
    livemode: bool = True

    def count(self, outcome: str) -> int:
        return sum(1 for result in self.results if result.outcome == outcome)

    @property
    def charged_cents(self) -> int:
        return sum(result.charged_cents for result in self.results)

    def rows(self) -> list[dict]:
        return [
            {"Event": result.delivery.event_id,
             "Type": result.delivery.event_type,
             "Outcome": result.outcome,
             "Charged": f"{result.charged_cents / 100:.2f}",
             "Detail": result.detail}
            for result in self.results
        ]


CHARGING_TYPES = ("invoice.paid", "checkout.session.completed")


def handle_webhook(ledger: WebhookLedger, delivery: WebhookDelivery,
                   now: int, secret: str = TEST_SIGNING_SECRET) -> WebhookResult:
    """Process one delivery exactly once, and refuse everything else.

    Four gates, in the order Stripe's own guidance puts them: verify the
    signature before parsing anything, refuse a timestamp outside tolerance so
    a captured request cannot be replayed later, refuse a livemode that does
    not match the environment, and only then check whether this event id has
    already been handled.
    """
    ok, failure = verify(delivery, now, secret)
    if not ok:
        return _record(ledger, WebhookResult(
            delivery, failure,
            "The signature did not match the body and timestamp."
            if failure == BAD_SIGNATURE else
            f"The timestamp is {abs(now - delivery.timestamp)} seconds away "
            f"from now, outside the {SIGNATURE_TOLERANCE_S} second tolerance. "
            f"This is how a captured request is replayed a day later."))

    if delivery.livemode != ledger.livemode:
        return _record(ledger, WebhookResult(
            delivery, WRONG_MODE,
            f"The event is {'live' if delivery.livemode else 'test'} and this "
            f"environment is {'live' if ledger.livemode else 'test'}. A test "
            f"event granting a live entitlement is the one nobody catches."))

    if delivery.event_id in ledger.handled:
        stored = ledger.handled[delivery.event_id]
        return _record(ledger, WebhookResult(
            delivery, REPLAYED,
            f"{delivery.event_id} was already handled. The stored response is "
            f"returned unchanged and nothing is charged, because Stripe "
            f"retries until it gets a 2xx.",
            stored_response=stored))

    charged = int(delivery.payload.get("amount", 0)) \
        if delivery.event_type in CHARGING_TYPES else 0
    response = f"200 handled {delivery.event_id}"
    ledger.handled[delivery.event_id] = response
    return _record(ledger, WebhookResult(
        delivery, ACCEPTED,
        f"{delivery.event_type} applied for "
        f"{delivery.payload.get('customer', 'unknown customer')}.",
        charged_cents=charged, stored_response=response))


def _record(ledger: WebhookLedger, result: WebhookResult) -> WebhookResult:
    ledger.results.append(result)
    return result


def sample_deliveries(now: int = 1_789_000_000) -> list[WebhookDelivery]:
    """A realistic delivery set: the event, its retry, a forgery and a replay
    of a request captured an hour ago."""
    payload = {"customer": "cus_aerial_8841", "amount": 24900,
               "plan": "survey-pro-monthly"}
    signature = sign(payload, now)
    old_payload = {"customer": "cus_aerial_8841", "amount": 24900,
                   "plan": "survey-pro-monthly", "note": "captured earlier"}
    return [
        WebhookDelivery("evt_1QaA01", "invoice.paid", payload, now, signature),
        # Stripe did not get a 2xx in time, so it sent the same event again.
        WebhookDelivery("evt_1QaA01", "invoice.paid", payload, now, signature),
        # Someone posting to the endpoint without the secret.
        WebhookDelivery("evt_1QaA02", "invoice.paid", payload, now,
                        "t=%d,v1=%s" % (now, "0" * 64)),
        # A request captured an hour ago and sent again.
        WebhookDelivery("evt_1QaA03", "invoice.paid", old_payload,
                        now - 3600, sign(old_payload, now - 3600)),
    ]


def replay_deliveries(deliveries: list[WebhookDelivery], now: int,
                      ledger: WebhookLedger | None = None) -> WebhookLedger:
    target = ledger or WebhookLedger()
    for delivery in deliveries:
        handle_webhook(target, delivery, now)
    return target


# ---------------------------------------------------------------------------
# Deployment closure
# ---------------------------------------------------------------------------

STEP_PENDING = "pending"
STEP_PASS = "pass"
STEP_FAIL = "fail"
STEP_BLOCKED = "blocked"


@dataclass(frozen=True)
class DeployStep:
    key: str
    label: str
    command: str
    output: str
    evidence: str = ""


DEPLOY_STEPS: tuple[DeployStep, ...] = (
    DeployStep(
        "dry_run", "Migration dry run against a restored snapshot",
        "npx prisma migrate diff --from-url $SNAPSHOT_URL "
        "--to-schema-datamodel prisma/schema.prisma --script > migration.sql",
        "3 statements planned. No destructive operation detected.\n"
        "  ALTER TABLE detections ADD COLUMN confidence DOUBLE PRECISION;\n"
        "  CREATE INDEX detections_tile_idx ON detections (tile_ref);\n"
        "  ALTER TABLE tasks ADD COLUMN claimed_at TIMESTAMPTZ;",
        evidence="evidence/migration.sql"),
    DeployStep(
        "evidence", "Evidence captured before anything is touched",
        "pg_dump --schema-only $PROD_URL > evidence/schema-before.sql && "
        "psql $PROD_URL -c '\\dt+' > evidence/tables-before.txt",
        "evidence/schema-before.sql  84 KB\n"
        "evidence/tables-before.txt  3 KB\n"
        "sha256 recorded in evidence/manifest.txt",
        evidence="evidence/manifest.txt"),
    DeployStep(
        "rollback", "Rollback rehearsed on the snapshot, not just written",
        "psql $SNAPSHOT_URL -f rollback.sql && "
        "npx prisma migrate diff --from-url $SNAPSHOT_URL "
        "--to-url $BASELINE_URL --exit-code",
        "rollback.sql applied in 1.9s\n"
        "diff exit code 0: the snapshot matches the baseline again.",
        evidence="evidence/rollback-verified.txt"),
    DeployStep(
        "apply", "Migration applied to production",
        "npx prisma migrate deploy",
        "3 migrations applied in 2.4s. detections_tile_idx built concurrently.",
        evidence="evidence/migrate-deploy.log"),
)

GATE_ORDER: tuple[str, ...] = ("dry_run", "evidence", "rollback", "apply")


@dataclass(frozen=True)
class DeployResult:
    steps: list
    statuses: dict
    blocked_reason: str
    applied: bool

    def rows(self) -> list[dict]:
        return [
            {"Step": step.label, "Status": self.statuses.get(step.key,
                                                             STEP_PENDING),
             "Evidence": step.evidence or "none"}
            for step in self.steps
        ]

    def terminal(self) -> str:
        lines: list[str] = []
        for step in self.steps:
            status = self.statuses.get(step.key, STEP_PENDING)
            lines.append(f"$ {step.command}")
            if status == STEP_PASS:
                lines.append(step.output)
                lines.append(f"[pass] {step.label}")
            elif status == STEP_BLOCKED:
                lines.append(f"[blocked] {self.blocked_reason}")
            else:
                lines.append(f"[{status}] {step.label}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def run_deploy(dry_run_ok: bool = True, evidence_ok: bool = True,
               rollback_ok: bool = True,
               steps: tuple[DeployStep, ...] = DEPLOY_STEPS) -> DeployResult:
    """Run the closure sequence, refusing to touch production out of order.

    The gate is the deliverable. A migration applied without a rehearsed
    rollback is not a deployment, it is a bet, and the evidence has to exist
    before the change rather than be reconstructed afterwards from memory.
    """
    statuses: dict[str, str] = {}
    blocked = ""

    statuses["dry_run"] = STEP_PASS if dry_run_ok else STEP_FAIL
    if not dry_run_ok:
        blocked = ("The dry run did not complete, so nothing is known about "
                   "what the migration would do to production.")

    if blocked:
        statuses["evidence"] = STEP_BLOCKED
    else:
        statuses["evidence"] = STEP_PASS if evidence_ok else STEP_FAIL
        if not evidence_ok:
            blocked = ("No evidence was captured. Without a schema dump taken "
                       "before the change there is nothing to compare against "
                       "when something looks wrong at 2am.")

    if blocked:
        statuses["rollback"] = STEP_BLOCKED
    else:
        statuses["rollback"] = STEP_PASS if rollback_ok else STEP_FAIL
        if not rollback_ok:
            blocked = ("The rollback was not rehearsed. A rollback that has "
                       "never been run is a paragraph, not a plan.")

    if blocked:
        statuses["apply"] = STEP_BLOCKED
        applied = False
    else:
        statuses["apply"] = STEP_PASS
        applied = True

    return DeployResult(list(steps), statuses, blocked, applied)
