"""Enterprise AI pipeline engine.

Four things sit between a document arriving and a dashboard answering: an
extractor that has to say how sure it is, a queue that has to survive being
watched, and a query that has to stay fast once the table is large.

Pure logic, no Streamlit import, so this is unit testable on its own.

Deterministic by construction. Nothing here reads the clock or a random source:
every figure is derived from a hash of the input, so the same document scores
the same confidence on a test runner and in the console, and a demonstration
cannot be a different demonstration each time somebody looks at it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"


def _digest(text: str) -> int:
    """A stable integer from any input, so every figure is reproducible."""
    return int(hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:12], 16)


def _spread(text: str, low: int, high: int, salt: str = "") -> int:
    """A stable value in [low, high], varied by input rather than by luck."""
    if high <= low:
        return low
    return low + _digest(f"{salt}:{text}") % (high - low + 1)


# ---------------------------------------------------------------------------
# Part one: intelligent document extraction
# ---------------------------------------------------------------------------

FIELD_INVOICE = "invoice_number"
FIELD_DATE = "issue_date"
FIELD_TOTAL = "total_amount"
FIELD_VENDOR = "vendor_name"
FIELD_CURRENCY = "currency"

# Each field carries the pattern that finds it and the weight it contributes to
# the document's confidence. A field found by a pattern is worth more than one
# the model had to infer, which is the distinction a single overall score hides.
EXTRACTION_FIELDS: tuple[tuple[str, str, str, int], ...] = (
    (FIELD_INVOICE, "string", r"(?:invoice|inv)[^A-Za-z0-9]{0,3}([A-Z0-9][A-Z0-9-]{3,})", 25),
    (FIELD_DATE, "string", r"(\d{4}-\d{2}-\d{2})", 20),
    (FIELD_TOTAL, "number", r"(?:total|amount due)[^0-9]{0,12}([0-9][0-9,]*\.?\d{0,2})", 30),
    (FIELD_VENDOR, "string", r"(?:from|vendor|billed by)[:\s]+([A-Z][A-Za-z&.\s]{2,40})", 15),
    (FIELD_CURRENCY, "string", r"\b(GBP|USD|EUR|NGN)\b", 10),
)

MAX_WEIGHT = sum(field_weight for _, _, _, field_weight in EXTRACTION_FIELDS)

# Below this a human has to look at it. A pipeline that routes everything
# straight through is not automated, it is unsupervised.
REVIEW_THRESHOLD = 0.75


@dataclass(frozen=True)
class ExtractedField:
    name: str
    json_type: str
    value: str
    confidence: float
    found: bool

    @property
    def source(self) -> str:
        return "pattern match" if self.found else "inferred, nothing matched"


@dataclass
class Extraction:
    fields: list = field(default_factory=list)
    characters: int = 0
    confidence: float = 0.0

    @property
    def found(self) -> list:
        return [f for f in self.fields if f.found]

    @property
    def missing(self) -> list:
        return [f for f in self.fields if not f.found]

    @property
    def needs_review(self) -> bool:
        return self.confidence < REVIEW_THRESHOLD

    @property
    def verdict(self) -> str:
        return ("Route to a human reviewer" if self.needs_review
                else "Safe to process without review")

    def schema(self) -> dict:
        """The JSON schema an LLM is asked to fill, with what it filled."""
        return {
            "type": "object",
            "required": [f.name for f in self.fields if f.found],
            "properties": {
                f.name: {
                    "type": f.json_type,
                    "value": f.value,
                    "confidence": round(f.confidence, 3),
                }
                for f in self.fields
            },
            "document_confidence": round(self.confidence, 3),
            "needs_review": self.needs_review,
        }

    def pretty_schema(self) -> str:
        return json.dumps(self.schema(), indent=2)

    def rows(self) -> list:
        return [
            {
                "Field": f.name,
                "Type": f.json_type,
                "Value": f.value or "(not found)",
                "Confidence": f"{f.confidence:.0%}",
                "Source": f.source,
            }
            for f in self.fields
        ]


SAMPLE_DOCUMENT = (
    "ACME LOGISTICS LTD\n"
    "Vendor: Northwind Freight\n"
    "Invoice INV-2026-0041\n"
    "Issue date 2026-09-15\n"
    "Currency GBP\n"
    "Line 1  Pallet haulage, Leeds to Bristol   1,240.00\n"
    "Line 2  Overnight storage                     86.50\n"
    "Total due 1326.50\n"
)


def simulate_document_extraction(text: str = SAMPLE_DOCUMENT) -> Extraction:
    """Extract a fixed schema from a document and say how sure it is.

    The confidence is not a decoration. It is the weighted share of the schema
    actually found by a pattern, so a document missing the total scores far
    worse than one missing the currency, and a field that was inferred rather
    than matched is reported as inferred instead of being quietly averaged in.
    """
    body = str(text or "")
    found_weight = 0
    fields: list = []

    for name, json_type, pattern, weight in EXTRACTION_FIELDS:
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            found_weight += weight
            value = match.group(1).strip()
            # A matched field is still not certain: a pattern can match the
            # wrong line. The spread is stable per value rather than random.
            confidence = _spread(value, 88, 99, salt=name) / 100
            fields.append(ExtractedField(name, json_type, value, confidence, True))
        else:
            confidence = _spread(name + body[:40], 8, 34, salt="miss") / 100
            fields.append(ExtractedField(name, json_type, "", confidence, False))

    overall = found_weight / MAX_WEIGHT if MAX_WEIGHT else 0.0
    return Extraction(fields=fields, characters=len(body),
                      confidence=round(overall, 4))


# ---------------------------------------------------------------------------
# Part two: the background queue
# ---------------------------------------------------------------------------

STATE_PENDING = "pending"
STATE_PROCESSING = "processing"
STATE_COMPLETED = "completed"

QUEUE_STATES: tuple[str, ...] = (STATE_PENDING, STATE_PROCESSING, STATE_COMPLETED)


@dataclass(frozen=True)
class QueueStage:
    state: str
    entered_ms: int
    duration_ms: int
    note: str

    @property
    def left_ms(self) -> int:
        return self.entered_ms + self.duration_ms


@dataclass
class Dispatch:
    task_name: str
    task_id: str
    stages: list = field(default_factory=list)

    @property
    def states(self) -> list:
        return [stage.state for stage in self.stages]

    @property
    def latency_ms(self) -> int:
        """Wall clock from enqueue to done, which is what a caller waits."""
        return sum(stage.duration_ms for stage in self.stages)

    @property
    def queue_wait_ms(self) -> int:
        """Time spent waiting rather than working. The number to reduce."""
        return sum(s.duration_ms for s in self.stages if s.state == STATE_PENDING)

    @property
    def work_ms(self) -> int:
        return sum(s.duration_ms for s in self.stages if s.state == STATE_PROCESSING)

    @property
    def completed(self) -> bool:
        return bool(self.stages) and self.stages[-1].state == STATE_COMPLETED

    def rows(self) -> list:
        return [
            {
                "State": stage.state,
                "Entered at": f"{stage.entered_ms} ms",
                "Duration": f"{stage.duration_ms} ms",
                "Note": stage.note,
            }
            for stage in self.stages
        ]


def simulate_queue_dispatch(task_name: str = "extract_invoice") -> Dispatch:
    """Put one task through the queue and report every state it passed.

    Three states, always in this order, because a worker that reports completed
    without ever reporting processing is a worker nobody can debug: the state a
    task is stuck in is the whole diagnosis when a queue backs up.
    """
    name = str(task_name or "task").strip() or "task"
    task_id = f"job_{hashlib.sha256(name.encode()).hexdigest()[:10]}"

    waiting = _spread(name, 40, 900, salt="wait")
    working = _spread(name, 120, 2400, salt="work")
    finishing = _spread(name, 5, 60, salt="ack")

    stages = [
        QueueStage(STATE_PENDING, 0, waiting,
                   "Enqueued and waiting for a free worker."),
        QueueStage(STATE_PROCESSING, waiting, working,
                   f"Claimed by a worker and running {name}."),
        QueueStage(STATE_COMPLETED, waiting + working, finishing,
                   "Result written and the job acknowledged."),
    ]
    return Dispatch(task_name=name, task_id=task_id, stages=stages)


# ---------------------------------------------------------------------------
# Part three: the PostgreSQL query benchmark
# ---------------------------------------------------------------------------

SEQ_SCAN = "Seq Scan"
INDEX_SCAN = "Index Scan"

DEFAULT_ROWS = 2_000_000
# A sequential scan reads every row. An index scan walks a B tree, so its cost
# grows with the logarithm of the table rather than with the table.
SEQ_MS_PER_MILLION = 415.0
INDEX_BASE_MS = 0.18
# Even a one row table costs one page read. Without a floor the model reported
# a sequential scan of a tiny table as taking exactly zero milliseconds, which
# is both wrong and the kind of number that makes a whole demonstration
# untrustworthy.
SEQ_FLOOR_MS = 0.012


@dataclass(frozen=True)
class QueryBenchmark:
    indexed: bool
    rows_scanned: int
    rows_returned: int
    plan: str
    execution_ms: float
    planning_ms: float
    buffers_hit: int

    @property
    def total_ms(self) -> float:
        return round(self.execution_ms + self.planning_ms, 3)

    def rows_table(self) -> list:
        return [
            {"Metric": "Plan", "Value": self.plan},
            {"Metric": "Rows scanned", "Value": f"{self.rows_scanned:,}"},
            {"Metric": "Rows returned", "Value": f"{self.rows_returned:,}"},
            {"Metric": "Planning time", "Value": f"{self.planning_ms:.3f} ms"},
            {"Metric": "Execution time", "Value": f"{self.execution_ms:.3f} ms"},
            {"Metric": "Shared buffers hit", "Value": f"{self.buffers_hit:,}"},
        ]

    def explain(self) -> str:
        """The shape EXPLAIN ANALYZE prints, so the plan is recognisable."""
        target = "documents_status_created_idx" if self.indexed else "documents"
        return (
            f"{self.plan} on documents"
            f"{'  using ' + target if self.indexed else ''}\n"
            f"  (cost=0.00..{self.execution_ms * 2.4:.2f} "
            f"rows={self.rows_returned} width=148)\n"
            f"  (actual time=0.021..{self.execution_ms:.3f} "
            f"rows={self.rows_returned} loops=1)\n"
            f"  Filter: (status = 'pending'::text)\n"
            f"  Rows Removed by Filter: "
            f"{max(self.rows_scanned - self.rows_returned, 0):,}\n"
            f"Planning Time: {self.planning_ms:.3f} ms\n"
            f"Execution Time: {self.execution_ms:.3f} ms"
        )


def benchmark_query(indexed: bool = True, rows: int = DEFAULT_ROWS,
                    selectivity: float = 0.002) -> QueryBenchmark:
    """Time the same filtered query with and without the index.

    Without an index PostgreSQL reads every row and throws nearly all of them
    away, so the cost tracks the table size. With one it descends a B tree and
    touches only the matching rows, so the cost tracks the answer instead. That
    difference is why a query that was instant at ten thousand rows is a
    timeout at two million, having changed not at all.
    """
    total = max(int(rows), 1)
    returned = max(int(total * max(selectivity, 0.0)), 1)

    if indexed:
        import math
        height = max(math.log(total, 200), 1.0)
        execution = INDEX_BASE_MS * height + returned * 0.0021
        scanned = returned
        plan = INDEX_SCAN
        buffers = returned + int(height) + 2
        planning = 0.142
    else:
        execution = max((total / 1_000_000) * SEQ_MS_PER_MILLION, SEQ_FLOOR_MS)
        scanned = total
        plan = SEQ_SCAN
        buffers = total // 58          # roughly one 8 kB page per 58 rows
        planning = 0.061

    return QueryBenchmark(
        indexed=indexed, rows_scanned=scanned, rows_returned=returned,
        plan=plan, execution_ms=round(execution, 3),
        planning_ms=planning, buffers_hit=buffers,
    )


def benchmark_comparison(rows: int = DEFAULT_ROWS,
                         selectivity: float = 0.002) -> list:
    """Both plans side by side, with the speedup stated rather than implied."""
    without = benchmark_query(False, rows, selectivity)
    with_index = benchmark_query(True, rows, selectivity)
    speedup = (without.execution_ms / with_index.execution_ms
               if with_index.execution_ms else 0.0)
    return [
        {"Measure": "Plan", "No index": without.plan,
         "With index": with_index.plan},
        {"Measure": "Rows scanned", "No index": f"{without.rows_scanned:,}",
         "With index": f"{with_index.rows_scanned:,}"},
        {"Measure": "Execution time",
         "No index": f"{without.execution_ms:.3f} ms",
         "With index": f"{with_index.execution_ms:.3f} ms"},
        {"Measure": "Speedup", "No index": "1x",
         "With index": f"{speedup:,.0f}x"},
    ]


INDEX_DDL = (
    "CREATE INDEX CONCURRENTLY documents_status_created_idx\n"
    "    ON documents (status, created_at DESC)\n"
    "    WHERE status = 'pending';"
)
