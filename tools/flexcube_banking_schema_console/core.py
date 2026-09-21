"""Core banking schema, reconciliation, and regulatory extraction engine.

No Streamlit import lives in this file, and no float touches a monetary
amount anywhere in it. Money is Decimal from end to end, because the
cheapest way to lose a reconciliation is to add up a day of postings in
binary floating point and then wonder where the eleven pence went.

Three properties carry the whole file:

* a schema map is only useful if it says which columns are the record and
  which are a cache. In every core banking system the account balance is a
  derived position over an append only movement log, and any denormalised
  balance column is a cache that is allowed to drift;
* a reconciliation that reports a net variance near zero is not reconciled.
  Offsetting errors net out, so the gross variance is reported beside the
  net one and a small net over a large gross is raised as the worst result
  in this file rather than the best;
* a regulatory figure you cannot trace to a table and a column cannot be
  defended to a regulator, so an extract with any field missing lineage is
  refused whole rather than filed with a gap in it.

The table and column names here are representative of the pattern that
core banking schemas share. They are not a transcription of any vendor's
catalogue, and the engine says so on every map it returns, because a
migration written against guessed table names fails on the first run
against the real installation.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal, getcontext
from typing import Sequence

getcontext().prec = 34

ENGINE_VERSION = "1.0.0"

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"

_SEVERITY_ORDER = {SEVERITY_OK: 0, SEVERITY_WARN: 1, SEVERITY_CRITICAL: 2}

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


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


def money(value) -> Decimal:
    """Quantise to minor units with banker's rounding.

    ROUND_HALF_EVEN is the default here and it is a policy choice, not a
    law. It has to match whatever the core posts with, because a report
    that rounds differently from the ledger it reports on will disagree
    with it by a few pence a day forever.
    """
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_EVEN)


# ---------------------------------------------------------------------------
# 1. Schema mapping
# ---------------------------------------------------------------------------

MODULE_CASA = "CASA"
MODULE_TERM_DEPOSIT = "Term Deposits"
MODULE_GL = "General Ledger"

MODULES: tuple[str, ...] = (MODULE_CASA, MODULE_TERM_DEPOSIT, MODULE_GL)

ROLE_MASTER = "Master"
ROLE_MOVEMENT = "Append only movement log"
ROLE_DERIVED = "Derived position, cache only"
ROLE_REFERENCE = "Reference data"


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str
    note: str


@dataclass(frozen=True)
class Table:
    name: str
    role: str
    purpose: str
    primary_key: tuple[str, ...]
    foreign_keys: tuple[str, ...]
    columns: tuple[Column, ...]

    @property
    def is_record_of_truth(self) -> bool:
        return self.role in (ROLE_MASTER, ROLE_MOVEMENT)


@dataclass(frozen=True)
class SchemaMap:
    module: str
    tables: tuple[Table, ...]
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def movement_tables(self) -> tuple[Table, ...]:
        return tuple(t for t in self.tables if t.role == ROLE_MOVEMENT)

    @property
    def cache_tables(self) -> tuple[Table, ...]:
        return tuple(t for t in self.tables if t.role == ROLE_DERIVED)


_AMOUNT = "NUMBER(22,3)"
_CCY = "CHAR(3)"

_CASA_TABLES: tuple[Table, ...] = (
    Table(
        name="CB_CUSTOMER_MASTER", role=ROLE_MASTER,
        purpose="One row per legal customer, the anchor for every account.",
        primary_key=("CUSTOMER_NO",),
        foreign_keys=(),
        columns=(
            Column("CUSTOMER_NO", "VARCHAR2(20)", "Surrogate, never reused."),
            Column("CUSTOMER_TYPE", "CHAR(1)", "Individual or corporate."),
            Column("KYC_STATUS", "VARCHAR2(20)",
                   "Drives whether posting is permitted at all."),
            Column("RESIDENT_FLAG", "CHAR(1)",
                   "Resident status changes the regulatory treatment."),
        )),
    Table(
        name="CB_CUST_ACCOUNT", role=ROLE_MASTER,
        purpose="One row per current or savings account.",
        primary_key=("CUST_AC_NO",),
        foreign_keys=("CUSTOMER_NO to CB_CUSTOMER_MASTER",
                      "GL_CODE to CB_GL_MASTER"),
        columns=(
            Column("CUST_AC_NO", "VARCHAR2(20)", "Account identifier."),
            Column("CUSTOMER_NO", "VARCHAR2(20)", "Owning customer."),
            Column("ACCOUNT_CLASS", "VARCHAR2(10)",
                   "Product class, which is what maps to a GL line."),
            Column("CCY", _CCY,
                   "Account currency. No amount is meaningful without it."),
            Column("GL_CODE", "VARCHAR2(9)",
                   "The GL line this account rolls up into."),
            Column("AC_STAT_NO_DR", "CHAR(1)",
                   "Debit block. A blocked account still accrues."),
        )),
    Table(
        name="CB_ACCOUNT_MOVEMENT", role=ROLE_MOVEMENT,
        purpose=("Every posting that ever touched an account. Corrections "
                 "are contra entries, never updates in place."),
        primary_key=("MOVEMENT_ID",),
        foreign_keys=("CUST_AC_NO to CB_CUST_ACCOUNT",
                      "GL_CODE to CB_GL_MASTER"),
        columns=(
            Column("MOVEMENT_ID", "NUMBER(18)", "Monotonic, gapless."),
            Column("CUST_AC_NO", "VARCHAR2(20)", "Account posted to."),
            Column("DRCR_IND", "CHAR(1)",
                   "D or C. Every entry has a matching opposite entry."),
            Column("LCY_AMOUNT", _AMOUNT,
                   "Local currency amount, scaled, never a float."),
            Column("FCY_AMOUNT", _AMOUNT,
                   "Foreign currency amount where the posting is not local."),
            Column("CCY", _CCY, "Currency of FCY_AMOUNT."),
            Column("VALUE_DATE", "DATE",
                   "When the money is effective. Drives interest."),
            Column("BOOKING_DATE", "DATE",
                   "When the entry was made. Drives the GL period."),
        )),
    Table(
        name="CB_ACCOUNT_BALANCE", role=ROLE_DERIVED,
        purpose=("A running balance kept for speed. It is a cache over the "
                 "movement log and it is allowed to be wrong."),
        primary_key=("CUST_AC_NO", "BALANCE_DATE"),
        foreign_keys=("CUST_AC_NO to CB_CUST_ACCOUNT",),
        columns=(
            Column("CUST_AC_NO", "VARCHAR2(20)", "Account."),
            Column("BALANCE_DATE", "DATE", "As at date."),
            Column("LCY_BALANCE", _AMOUNT,
                   "Recompute from movements before reporting on it."),
        )),
)

_TD_TABLES: tuple[Table, ...] = (
    Table(
        name="CB_TD_CONTRACT", role=ROLE_MASTER,
        purpose="One row per term deposit contract.",
        primary_key=("TD_CONTRACT_REF",),
        foreign_keys=("CUSTOMER_NO to CB_CUSTOMER_MASTER",
                      "SETTLE_AC_NO to CB_CUST_ACCOUNT"),
        columns=(
            Column("TD_CONTRACT_REF", "VARCHAR2(16)", "Contract reference."),
            Column("PRINCIPAL_AMOUNT", _AMOUNT, "Amount placed."),
            Column("CCY", _CCY, "Contract currency."),
            Column("VALUE_DATE", "DATE", "Start of the term."),
            Column("MATURITY_DATE", "DATE",
                   "Drives the liquidity maturity ladder."),
            Column("ROLLOVER_TYPE", "VARCHAR2(20)",
                   "Principal only, or principal plus interest."),
        )),
    Table(
        name="CB_TD_RATE_SCHEDULE", role=ROLE_REFERENCE,
        purpose=("Rate applying over a period. A contract can carry several "
                 "rows when the rate steps."),
        primary_key=("TD_CONTRACT_REF", "EFFECTIVE_FROM"),
        foreign_keys=("TD_CONTRACT_REF to CB_TD_CONTRACT",),
        columns=(
            Column("EFFECTIVE_FROM", "DATE", "Inclusive start."),
            Column("INTEREST_RATE", "NUMBER(10,6)",
                   "Six decimals, because basis points matter."),
            Column("DAY_BASIS", "VARCHAR2(10)",
                   "Actual/365 and 30/360 give different money."),
        )),
    Table(
        name="CB_TD_ACCRUAL", role=ROLE_MOVEMENT,
        purpose=("Daily interest accrual. The liability exists before it is "
                 "paid, and the GL has to carry it."),
        primary_key=("TD_CONTRACT_REF", "ACCRUAL_DATE"),
        foreign_keys=("TD_CONTRACT_REF to CB_TD_CONTRACT",
                      "GL_CODE to CB_GL_MASTER"),
        columns=(
            Column("ACCRUAL_DATE", "DATE", "One row per day per contract."),
            Column("ACCRUED_AMOUNT", _AMOUNT, "Interest earned that day."),
            Column("GL_CODE", "VARCHAR2(9)", "Interest expense line."),
        )),
    Table(
        name="CB_TD_MATURITY_POSITION", role=ROLE_DERIVED,
        purpose=("Maturity ladder built for the liquidity report. A cache, "
                 "rebuilt from contracts."),
        primary_key=("BUCKET_CODE", "AS_AT_DATE"),
        foreign_keys=(),
        columns=(
            Column("BUCKET_CODE", "VARCHAR2(10)",
                   "Overnight, eight to thirty days, and so on."),
            Column("BUCKET_AMOUNT", _AMOUNT, "Sum of contracts in bucket."),
        )),
)

_GL_TABLES: tuple[Table, ...] = (
    Table(
        name="CB_GL_MASTER", role=ROLE_MASTER,
        purpose="The chart of accounts. Every posting lands on one of these.",
        primary_key=("GL_CODE",),
        foreign_keys=("PARENT_GL to CB_GL_MASTER",),
        columns=(
            Column("GL_CODE", "VARCHAR2(9)", "Leaf or node."),
            Column("GL_TYPE", "CHAR(1)",
                   "Asset, liability, income, expense, or contingent."),
            Column("PARENT_GL", "VARCHAR2(9)",
                   "Self referencing, which is how the hierarchy rolls up."),
            Column("LEAF_FLAG", "CHAR(1)",
                   "Only a leaf may be posted to. Posting to a node "
                   "double counts on roll up."),
            Column("CCY_RESTRICTION", _CCY,
                   "Some lines are single currency by design."),
        )),
    Table(
        name="CB_GL_POSTING", role=ROLE_MOVEMENT,
        purpose=("The double entry journal. Debits and credits for one "
                 "transaction reference always sum to zero."),
        primary_key=("POSTING_ID",),
        foreign_keys=("GL_CODE to CB_GL_MASTER",),
        columns=(
            Column("POSTING_ID", "NUMBER(18)", "Monotonic."),
            Column("TRN_REF_NO", "VARCHAR2(16)",
                   "Groups the legs of one transaction."),
            Column("GL_CODE", "VARCHAR2(9)", "Leaf line posted to."),
            Column("DRCR_IND", "CHAR(1)", "D or C."),
            Column("LCY_AMOUNT", _AMOUNT, "Scaled decimal, never a float."),
            Column("FIN_CYCLE", "VARCHAR2(9)",
                   "Financial year. Reopening a closed cycle is an event."),
            Column("PERIOD_CODE", "VARCHAR2(3)",
                   "Accounting period, driven by booking date."),
        )),
    Table(
        name="CB_GL_SUSPENSE", role=ROLE_MOVEMENT,
        purpose=("Where a posting lands when no GL mapping matched. It is "
                 "balanced, so the ledger still ties while being wrong."),
        primary_key=("SUSPENSE_ID",),
        foreign_keys=("TRN_REF_NO to CB_GL_POSTING",),
        columns=(
            Column("SUSPENSE_ID", "NUMBER(18)", "Monotonic."),
            Column("REASON_CODE", "VARCHAR2(20)",
                   "Why the mapping failed. The list of these is the "
                   "backlog."),
            Column("LCY_AMOUNT", _AMOUNT, "Signed against the suspense line."),
            Column("AGE_DAYS", "NUMBER(5)",
                   "How long it has sat there. Age is the real metric."),
        )),
    Table(
        name="CB_GL_BALANCE", role=ROLE_DERIVED,
        purpose="Period balances per GL line, rebuilt from postings.",
        primary_key=("GL_CODE", "FIN_CYCLE", "PERIOD_CODE"),
        foreign_keys=("GL_CODE to CB_GL_MASTER",),
        columns=(
            Column("OPENING_BALANCE", _AMOUNT, "Carried from prior period."),
            Column("PERIOD_MOVEMENT", _AMOUNT, "Sum of postings in period."),
            Column("CLOSING_BALANCE", _AMOUNT,
                   "Opening plus movement. Check it, do not trust it."),
        )),
)

_MODULE_TABLES = {
    MODULE_CASA: _CASA_TABLES,
    MODULE_TERM_DEPOSIT: _TD_TABLES,
    MODULE_GL: _GL_TABLES,
}


def map_core_banking_schema(module_type: str) -> SchemaMap:
    """Return the relational shape for one module, with the caches named.

    The part of this that matters is not the column list. It is the role
    on each table. A reporting query written against a balance column
    returns a number instantly and returns a wrong one whenever the cache
    has drifted, and nothing in the result set says which happened.
    """
    module = str(module_type or "").strip()
    if module not in _MODULE_TABLES:
        raise ValueError(f"unknown module {module_type!r}")

    tables = _MODULE_TABLES[module]
    findings: list[Finding] = [
        Finding(
            code="SCH-NAMES", severity=SEVERITY_WARN,
            title="These names are the pattern, not a vendor catalogue",
            detail=("Core banking schemas share this shape across vendors, "
                    "and the names differ in every installation. A migration "
                    "written against names taken from a document instead of "
                    "from the installed catalogue fails on its first run."),
            fix=("Read the real names from ALL_TAB_COLUMNS on the target "
                 "database and map onto this shape.")),
    ]

    caches = [t.name for t in tables if t.role == ROLE_DERIVED]
    if caches:
        findings.append(Finding(
            code="SCH-CACHE", severity=SEVERITY_CRITICAL,
            title=f"{len(caches)} table(s) here are caches, not the record",
            detail=(f"{', '.join(caches)} hold derived positions over the "
                    f"movement log. They exist for speed, they are allowed "
                    f"to drift, and a report that reads them returns a fast "
                    f"answer that says nothing about whether it is the right "
                    f"one."),
            fix=("Report from the movement log and use the cache only to "
                 "check yourself against it.")))

    movements = [t.name for t in tables if t.role == ROLE_MOVEMENT]
    if movements:
        findings.append(Finding(
            code="SCH-APPEND", severity=SEVERITY_OK,
            title=f"{', '.join(movements)} is append only",
            detail=("A correction is a contra entry, never an update in "
                    "place. That is what makes the ledger reconstructable "
                    "to any date, and it is the first thing an auditor "
                    "tests."),
            fix="Deny UPDATE and DELETE on the movement tables in the role."))

    amount_columns = [(t.name, c.name) for t in tables for c in t.columns
                      if c.sql_type == _AMOUNT]
    currency_tables = {t.name for t in tables for c in t.columns
                       if c.sql_type == _CCY}
    naked = sorted({table for table, _ in amount_columns
                    if table not in currency_tables})
    if naked:
        findings.append(Finding(
            code="SCH-CCY", severity=SEVERITY_WARN,
            title=f"{len(naked)} table(s) carry an amount with no currency "
                  f"column beside it",
            detail=(f"{', '.join(naked)} store a scaled amount and rely on "
                    f"the currency being implied by the parent row. That "
                    f"holds until the first multi currency product, and "
                    f"then a sum across the table silently adds pounds to "
                    f"euros."),
            fix="Carry the currency on every row that carries an amount."))

    findings.append(Finding(
        code="SCH-TYPE", severity=SEVERITY_OK,
        title=f"Every monetary column is {_AMOUNT}, not a binary float",
        detail=("A scaled decimal is exact for the arithmetic a ledger "
                "does. A binary float is not able to represent a tenth "
                "exactly, and a day of postings summed in one drifts."),
        fix="Refuse a schema review that proposes FLOAT or BINARY_DOUBLE."))

    return SchemaMap(
        module=module, tables=tables,
        headline=(f"{module}: {len(tables)} table(s), "
                  f"{len(movements)} append only, {len(caches)} cache"),
        findings=tuple(findings))


# ---------------------------------------------------------------------------
# 2. General ledger reconciliation
# ---------------------------------------------------------------------------

KIND_UNMAPPED = "Unmapped GL code, posted to suspense"
KIND_TIMING = "Timing, booked in source and not yet in the GL period"
KIND_FX = "Foreign currency revaluation difference"
KIND_ROUNDING = "Sub unit rounding accumulated across legs"
KIND_DUPLICATE = "Duplicate posting of the same transaction reference"

EXCEPTION_KINDS: tuple[str, ...] = (KIND_UNMAPPED, KIND_TIMING, KIND_FX,
                                    KIND_ROUNDING, KIND_DUPLICATE)

_KIND_WEIGHTS = {
    KIND_UNMAPPED: 0.34,
    KIND_TIMING: 0.28,
    KIND_FX: 0.18,
    KIND_ROUNDING: 0.14,
    KIND_DUPLICATE: 0.06,
}

_KIND_SCALE = {
    KIND_UNMAPPED: (Decimal("250.00"), Decimal("18000.00")),
    KIND_TIMING: (Decimal("100.00"), Decimal("9000.00")),
    KIND_FX: (Decimal("5.00"), Decimal("900.00")),
    KIND_ROUNDING: (Decimal("0.01"), Decimal("0.09")),
    KIND_DUPLICATE: (Decimal("400.00"), Decimal("25000.00")),
}

RECON_SEED = 20261221
NET_BLIND_SPOT_RATIO = Decimal("0.10")


@dataclass(frozen=True)
class ExceptionBucket:
    kind: str
    count: int
    net_amount: Decimal
    gross_amount: Decimal


@dataclass(frozen=True)
class ReconResult:
    transaction_volume: int
    discrepancy_rate: float
    matched: int
    exceptions: int
    buckets: tuple[ExceptionBucket, ...]
    total_debits: Decimal
    total_credits: Decimal
    net_variance: Decimal
    gross_variance: Decimal
    suspense_balance: Decimal
    suspense_count: int
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def balanced(self) -> bool:
        return self.total_debits == self.total_credits

    @property
    def netting_hides_errors(self) -> bool:
        if self.gross_variance == ZERO:
            return False
        return abs(self.net_variance) < self.gross_variance * \
            NET_BLIND_SPOT_RATIO


def simulate_gl_reconciliation(transaction_volume: int,
                               discrepancy_rate: float,
                               seed: int = RECON_SEED) -> ReconResult:
    """Reconcile a day of postings and report gross as well as net.

    The number every pack leads with is the net variance, and it is the
    number that hides the problem. Offsetting errors cancel, so a ledger
    holding a large unmapped debit and a large unmapped credit nets to
    almost nothing and reads as clean. The gross variance does not cancel,
    and the ratio between them is what says whether the net figure means
    anything.

    Every amount here is Decimal. Debits and credits are equal by
    construction, including for the exceptions, because an unmapped
    posting still lands as a balanced pair against suspense. A ledger that
    ties is not a ledger that is right, and that is the point.
    """
    volume = int(transaction_volume)
    rate = float(discrepancy_rate)
    if volume < 0:
        raise ValueError("a transaction volume cannot be negative")
    if not 0.0 <= rate <= 1.0:
        raise ValueError("a discrepancy rate is a fraction between 0 and 1")

    rng = random.Random(seed)
    exceptions = int(round(volume * rate))
    exceptions = min(exceptions, volume)
    matched = volume - exceptions

    counts: dict[str, int] = {kind: 0 for kind in EXCEPTION_KINDS}
    nets: dict[str, Decimal] = {kind: ZERO for kind in EXCEPTION_KINDS}
    grosses: dict[str, Decimal] = {kind: ZERO for kind in EXCEPTION_KINDS}

    kinds = list(EXCEPTION_KINDS)
    weights = [_KIND_WEIGHTS[k] for k in kinds]

    suspense = ZERO
    suspense_count = 0

    for _ in range(exceptions):
        kind = rng.choices(kinds, weights=weights, k=1)[0]
        low, high = _KIND_SCALE[kind]
        span = high - low
        amount = money(low + span * Decimal(str(rng.random())))
        sign = 1 if rng.random() < 0.5 else -1
        signed = amount if sign > 0 else -amount

        counts[kind] += 1
        nets[kind] += signed
        grosses[kind] += amount
        if kind == KIND_UNMAPPED:
            suspense += signed
            suspense_count += 1

    buckets = tuple(
        ExceptionBucket(kind=kind, count=counts[kind],
                        net_amount=nets[kind], gross_amount=grosses[kind])
        for kind in EXCEPTION_KINDS if counts[kind])

    net_variance = sum((b.net_amount for b in buckets), ZERO)
    gross_variance = sum((b.gross_amount for b in buckets), ZERO)

    # Every posting, exception or not, is a balanced pair. The ledger ties
    # whatever the exceptions are, which is exactly why tying proves little.
    leg_total = money(Decimal(volume) * Decimal("1250.00")) + gross_variance
    total_debits = leg_total
    total_credits = leg_total

    findings: list[Finding] = []

    findings.append(Finding(
        code="REC-TIE", severity=SEVERITY_OK,
        title="Debits equal credits exactly",
        detail=("Every posting is a balanced pair, including the ones that "
                "landed in suspense, so the ledger ties. This proves the "
                "journal is well formed. It proves nothing about whether "
                "the money is on the right line."),
        fix="Never report a tied ledger as a reconciled one."))

    if exceptions == 0:
        findings.append(Finding(
            code="REC-CLEAN", severity=SEVERITY_OK,
            title=f"All {matched} transaction(s) matched",
            detail=("No unmapped codes, no timing breaks, no revaluation "
                    "differences. Worth checking that the matching rule is "
                    "actually discriminating rather than matching on "
                    "something always true."),
            fix="Inject a known break weekly and confirm it is caught."))
    else:
        findings.append(Finding(
            code="REC-EXCEPT", severity=SEVERITY_WARN,
            title=f"{exceptions} exception(s) across {len(buckets)} kind(s)",
            detail=("Each kind needs a different owner. A timing break "
                    "clears itself, an unmapped code does not, and a "
                    "duplicate is the only one that moved money that should "
                    "never have moved."),
            fix="Route by kind, not by amount."))

    if suspense_count:
        findings.append(Finding(
            code="REC-SUSPENSE", severity=SEVERITY_CRITICAL,
            title=(f"{suspense_count} posting(s) sit in suspense at "
                   f"{suspense:+.2f}"),
            detail=("Suspense is a balanced line, so the ledger ties with "
                    "money sitting on it. The metric that matters is the "
                    "age of the oldest item, not the balance, because a "
                    "suspense line that nets to zero can hold an item from "
                    "last quarter."),
            fix=("Report suspense by age bucket and put a hard limit on the "
                 "oldest item, not on the balance.")))

    gross_ratio = (abs(net_variance) / gross_variance
                   if gross_variance > ZERO else Decimal("1"))
    if gross_variance > ZERO and gross_ratio < NET_BLIND_SPOT_RATIO:
        findings.append(Finding(
            code="REC-NETTING", severity=SEVERITY_CRITICAL,
            title=(f"Net variance {net_variance:+.2f} against gross "
                   f"{gross_variance:.2f}"),
            detail=("The net figure is under a tenth of the gross, which "
                    "means the errors are cancelling rather than absent. "
                    "This is the reading that gets signed off, and it is "
                    "the one that should not be."),
            fix=("Set the tolerance on gross variance. A net tolerance "
                 "passes a ledger holding two large opposite errors.")))

    if KIND_DUPLICATE in counts and counts[KIND_DUPLICATE]:
        findings.append(Finding(
            code="REC-DUP", severity=SEVERITY_CRITICAL,
            title=f"{counts[KIND_DUPLICATE]} duplicate posting(s) detected",
            detail=("A duplicate is the only exception kind here that moved "
                    "customer money twice. It does not clear itself and it "
                    "is visible to the customer before it is visible in the "
                    "reconciliation."),
            fix=("Enforce a unique constraint on the transaction reference "
                 "in the journal, so the second leg is refused at the "
                 "database rather than caught at month end.")))

    findings.append(Finding(
        code="REC-DECIMAL", severity=SEVERITY_OK,
        title="Every amount in this result is a Decimal",
        detail=("The variances are exact to the penny. Summing a day of "
                "postings in binary floating point introduces a drift that "
                "looks exactly like a real break and wastes the "
                "investigation that follows it."),
        fix="Keep money out of float in the extract as well as in the core."))

    headline = (f"{matched} matched, {exceptions} exception(s), net "
                f"{net_variance:+.2f} against gross {gross_variance:.2f}")

    return ReconResult(
        transaction_volume=volume, discrepancy_rate=rate, matched=matched,
        exceptions=exceptions, buckets=buckets, total_debits=total_debits,
        total_credits=total_credits, net_variance=net_variance,
        gross_variance=gross_variance, suspense_balance=suspense,
        suspense_count=suspense_count, headline=headline,
        findings=tuple(findings))


# ---------------------------------------------------------------------------
# 3. Regulatory extraction
# ---------------------------------------------------------------------------

STANDARD_BASEL = "Basel III"
STANDARD_LOCAL = "Local central bank"

STANDARDS: tuple[str, ...] = (STANDARD_BASEL, STANDARD_LOCAL)

REPORT_CAPITAL = "Capital adequacy"
REPORT_LIQUIDITY = "Liquidity coverage"
REPORT_EXPOSURE = "Large exposures"
REPORT_DEPOSITS = "Deposit maturity ladder"

REPORT_TYPES: tuple[str, ...] = (REPORT_CAPITAL, REPORT_LIQUIDITY,
                                 REPORT_EXPOSURE, REPORT_DEPOSITS)

READY = "Extract ready to file"
BLOCKED = "Blocked, a field has no lineage"

# Basel III framework minima. National implementations add on top of these
# and the local regulator's number is the one that governs.
BASEL_MINIMA: tuple[tuple[str, str, str], ...] = (
    ("Common equity tier 1", "4.5% of risk weighted assets",
     "7.0% once the 2.5% capital conservation buffer is included"),
    ("Tier 1 capital", "6.0% of risk weighted assets",
     "8.5% with the conservation buffer"),
    ("Total capital", "8.0% of risk weighted assets",
     "10.5% with the conservation buffer"),
    ("Countercyclical buffer", "0% to 2.5% of risk weighted assets",
     "Set by the national authority, in common equity tier 1"),
    ("Liquidity coverage ratio", "100%",
     "High quality liquid assets over thirty day net cash outflows"),
    ("Net stable funding ratio", "100%",
     "Available stable funding over required stable funding"),
    ("Leverage ratio", "3.0%",
     "Tier 1 capital over total exposure, not risk weighted"),
)


@dataclass(frozen=True)
class ExtractField:
    name: str
    source_table: str
    source_column: str
    rule: str

    @property
    def has_lineage(self) -> bool:
        return bool(self.source_table.strip() and self.source_column.strip())


@dataclass(frozen=True)
class RegulatoryExtract:
    report_type: str
    compliance_standard: str
    fields: tuple[ExtractField, ...]
    missing_lineage: tuple[str, ...]
    status: str
    thresholds: tuple[tuple[str, str, str], ...]
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        return self.status == READY

    @property
    def severity(self) -> str:
        return _worst(self.findings)


_REPORT_FIELDS: dict[str, tuple[ExtractField, ...]] = {
    REPORT_CAPITAL: (
        ExtractField("Common equity tier 1", "CB_GL_BALANCE",
                     "CLOSING_BALANCE",
                     "Sum of the equity GL lines at period close."),
        ExtractField("Risk weighted assets", "CB_CUST_ACCOUNT",
                     "ACCOUNT_CLASS",
                     "Exposure by class, weighted per the standardised "
                     "approach."),
        ExtractField("Total capital", "CB_GL_BALANCE", "CLOSING_BALANCE",
                     "Tier 1 plus eligible tier 2."),
        ExtractField("Leverage exposure", "CB_ACCOUNT_MOVEMENT",
                     "LCY_AMOUNT",
                     "Total exposure with no risk weighting applied."),
    ),
    REPORT_LIQUIDITY: (
        ExtractField("High quality liquid assets", "CB_GL_BALANCE",
                     "CLOSING_BALANCE",
                     "Level one and level two assets after haircuts."),
        ExtractField("Thirty day outflows", "CB_TD_MATURITY_POSITION",
                     "BUCKET_AMOUNT",
                     "Contractual maturities inside thirty days, with "
                     "behavioural runoff applied to CASA."),
        ExtractField("Thirty day inflows", "CB_ACCOUNT_MOVEMENT",
                     "VALUE_DATE",
                     "Capped at 75% of outflows under the framework."),
    ),
    REPORT_EXPOSURE: (
        ExtractField("Counterparty group", "CB_CUSTOMER_MASTER",
                     "CUSTOMER_NO",
                     "Connected counterparties are one exposure."),
        ExtractField("Gross exposure", "CB_ACCOUNT_MOVEMENT", "LCY_AMOUNT",
                     "Before credit risk mitigation."),
        ExtractField("Eligible collateral", "", "",
                     "Collateral is held outside the core in most "
                     "installations, so this field has no lineage here."),
    ),
    REPORT_DEPOSITS: (
        ExtractField("Bucket code", "CB_TD_MATURITY_POSITION", "BUCKET_CODE",
                     "Regulator defined maturity buckets."),
        ExtractField("Contractual amount", "CB_TD_CONTRACT",
                     "PRINCIPAL_AMOUNT",
                     "Principal at contractual maturity."),
        ExtractField("Accrued interest", "CB_TD_ACCRUAL", "ACCRUED_AMOUNT",
                     "The liability exists before it is paid."),
        ExtractField("Rollover assumption", "CB_TD_CONTRACT",
                     "ROLLOVER_TYPE",
                     "Behavioural, and the assumption has to be disclosed."),
    ),
}


def extract_regulatory_audit(report_type: str,
                             compliance_standard: str) -> RegulatoryExtract:
    """Build a filing extract, and refuse it whole if a field cannot be
    traced to a table and a column.

    A regulator does not ask whether a number is right. It asks where the
    number came from, and a figure that cannot be walked back to a column
    in a named table is a figure that cannot be defended. Emitting the
    extract with the untraceable field blank, or filled with an estimate,
    produces a pack that looks complete and fails on the first question.
    """
    report = str(report_type or "").strip()
    standard = str(compliance_standard or "").strip()
    if report not in _REPORT_FIELDS:
        raise ValueError(f"unknown report type {report_type!r}")
    if standard not in STANDARDS:
        raise ValueError(f"unknown compliance standard {compliance_standard!r}")

    fields = _REPORT_FIELDS[report]
    missing = tuple(f.name for f in fields if not f.has_lineage)
    findings: list[Finding] = []

    if missing:
        status = BLOCKED
        findings.append(Finding(
            code="REG-LINEAGE", severity=SEVERITY_CRITICAL,
            title=f"{len(missing)} field(s) cannot be traced to a column",
            detail=(f"{', '.join(missing)} has no source table and column in "
                    f"this schema. The extract is refused whole rather than "
                    f"filed with that field blank, because a pack with a gap "
                    f"in it still looks complete to the person signing it."),
            fix=("Source the field from the system that actually holds it "
                 "and record the lineage, or declare it out of scope in "
                 "writing before the filing date.")))
    else:
        status = READY
        findings.append(Finding(
            code="REG-TRACED", severity=SEVERITY_OK,
            title=f"All {len(fields)} field(s) trace to a table and column",
            detail=("Every figure in this extract can be walked back to the "
                    "row it came from, which is the question a regulator "
                    "actually asks."),
            fix="Keep the lineage in the extract, not in a separate note."))

    if standard == STANDARD_BASEL:
        thresholds = BASEL_MINIMA
        findings.append(Finding(
            code="REG-BASEL", severity=SEVERITY_WARN,
            title="These are framework minima, not your requirement",
            detail=("Basel III is a standard that national authorities "
                    "implement, and they implement it with add ons. The "
                    "number your supervisor holds you to is at least these "
                    "and is frequently higher."),
            fix=("Take the binding number from the local rulebook and "
                 "record which paragraph it came from.")))
    else:
        thresholds = ()
        findings.append(Finding(
            code="REG-LOCAL", severity=SEVERITY_CRITICAL,
            title="No local thresholds are shipped with this engine",
            detail=("Central bank requirements differ by jurisdiction, "
                    "change on their own schedule, and are the binding "
                    "ones. Shipping a default here would be guessing at the "
                    "number a bank is measured against."),
            fix=("Load the thresholds from your regulator's current "
                 "rulebook and version them with the filing.")))

    findings.append(Finding(
        code="REG-ASOF", severity=SEVERITY_WARN,
        title="Booking date and value date give different answers",
        detail=("A period end extract built on value date includes entries "
                "booked after the close, and one built on booking date "
                "excludes entries effective inside the period. Both are "
                "defensible and only one matches the return."),
        fix="State which date drives the extract on the face of the pack."))

    headline = (f"{report} under {standard}: {len(fields) - len(missing)} of "
                f"{len(fields)} field(s) traced")

    return RegulatoryExtract(
        report_type=report, compliance_standard=standard, fields=fields,
        missing_lineage=missing, status=status, thresholds=thresholds,
        headline=headline, findings=tuple(findings))
