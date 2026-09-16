"""Supply chain planning QA and regression engine.

A planning defect is not like a UI defect. Nothing crashes, nothing looks
wrong, and the number is simply the wrong number, so it ships and is believed
for a quarter until somebody counts the shelf. That makes the arithmetic the
test, and it makes a defect ticket worthless unless it carries the two numbers
and the formula that separates them.

Pure logic, no Streamlit import, so this is unit testable on its own.
Deterministic: nothing reads the clock or a random source, so the expected
figure in a ticket is the same figure an engineer reproduces.

THE DEFECT THIS ENGINE IS BUILT AROUND

The widely used safety stock formula is

    SS = Z * sigma_demand * sqrt(lead_time)

It is correct only when lead time is fixed. The moment a supplier is
unreliable it is wrong, because it accounts for demand varying and pretends
lead time does not. The complete form, King's formula, carries both:

    SS = Z * sqrt(lead_time * sigma_demand^2 + demand^2 * sigma_lead_time^2)

The gap between the two is not a rounding difference. On a supplier whose lead
time swings by a few days it is often more than double, and it is always in the
direction of holding too little. So the engine computes both, and a generated
ticket states the expected value, the actual value, and the term that was
dropped.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Severity and modules
# ---------------------------------------------------------------------------

SEV_BLOCKER = "S1 Blocker"
SEV_CRITICAL = "S2 Critical"
SEV_MAJOR = "S3 Major"
SEV_MINOR = "S4 Minor"

SEVERITIES: tuple = (SEV_BLOCKER, SEV_CRITICAL, SEV_MAJOR, SEV_MINOR)
SEVERITY_RANK = {name: index + 1 for index, name in enumerate(SEVERITIES)}
SEVERITY_TONE = {SEV_BLOCKER: "crit", SEV_CRITICAL: "crit",
                 SEV_MAJOR: "warn", SEV_MINOR: "ok"}

MODULE_PLANNING = "Inventory Planning"
MODULE_SAFETY_STOCK = "Safety Stock Engine"
MODULE_LEAD_TIME = "Lead Time Variance"
MODULE_REPLENISH = "Replenishment Scheduler"

MODULES: tuple = (MODULE_PLANNING, MODULE_SAFETY_STOCK, MODULE_LEAD_TIME,
                  MODULE_REPLENISH)


def severity_rank(severity: str) -> int:
    """An unknown severity sorts last rather than crashing a board."""
    return SEVERITY_RANK.get(severity, len(SEVERITIES) + 1)


def severity_tone(severity: str) -> str:
    return SEVERITY_TONE.get(severity, "warn")


# ---------------------------------------------------------------------------
# The planning arithmetic the tests are about
# ---------------------------------------------------------------------------

# Standard normal quantiles for the service levels a planner actually sets.
SERVICE_LEVELS = {0.90: 1.282, 0.95: 1.645, 0.98: 2.054, 0.99: 2.326}
DEFAULT_SERVICE = 0.95


def z_for(service_level: float = DEFAULT_SERVICE) -> float:
    """The z score for a service level, refusing to guess between the rungs."""
    if service_level not in SERVICE_LEVELS:
        raise ValueError(
            f"no z score defined for a service level of {service_level}. "
            f"Defined levels are {sorted(SERVICE_LEVELS)}.")
    return SERVICE_LEVELS[service_level]


def safety_stock_demand_only(demand_sigma: float, lead_time_days: float,
                             service_level: float = DEFAULT_SERVICE) -> float:
    """The common formula. Correct only when lead time never varies."""
    return z_for(service_level) * demand_sigma * math.sqrt(max(lead_time_days, 0))


def safety_stock_full(demand_mean: float, demand_sigma: float,
                      lead_time_days: float, lead_time_sigma: float,
                      service_level: float = DEFAULT_SERVICE) -> float:
    """King's formula. Carries demand variance and lead time variance both."""
    variance = (max(lead_time_days, 0) * demand_sigma ** 2
                + demand_mean ** 2 * lead_time_sigma ** 2)
    return z_for(service_level) * math.sqrt(max(variance, 0))


def reorder_point(demand_mean: float, lead_time_days: float,
                  safety_stock: float) -> float:
    """Average demand over the lead time, plus the buffer."""
    return demand_mean * max(lead_time_days, 0) + max(safety_stock, 0)


@dataclass(frozen=True)
class PlanningScenario:
    """One item, with the numbers a planner would actually have."""
    sku: str
    demand_mean: float          # units a day
    demand_sigma: float         # standard deviation of daily demand
    lead_time_days: float
    lead_time_sigma: float      # standard deviation of lead time, in days
    service_level: float = DEFAULT_SERVICE

    @property
    def naive_safety_stock(self) -> float:
        return round(safety_stock_demand_only(
            self.demand_sigma, self.lead_time_days, self.service_level), 2)

    @property
    def correct_safety_stock(self) -> float:
        return round(safety_stock_full(
            self.demand_mean, self.demand_sigma, self.lead_time_days,
            self.lead_time_sigma, self.service_level), 2)

    @property
    def shortfall(self) -> float:
        """Units the naive formula leaves off the shelf."""
        return round(self.correct_safety_stock - self.naive_safety_stock, 2)

    @property
    def understated_by(self) -> float:
        if self.naive_safety_stock <= 0:
            return 0.0
        return round(self.correct_safety_stock / self.naive_safety_stock, 3)

    @property
    def naive_reorder_point(self) -> float:
        return round(reorder_point(self.demand_mean, self.lead_time_days,
                                   self.naive_safety_stock), 2)

    @property
    def correct_reorder_point(self) -> float:
        return round(reorder_point(self.demand_mean, self.lead_time_days,
                                   self.correct_safety_stock), 2)

    def payload(self) -> dict:
        return {
            "sku": self.sku,
            "demand": {"mean_per_day": self.demand_mean,
                       "std_dev": self.demand_sigma},
            "lead_time": {"mean_days": self.lead_time_days,
                          "std_dev_days": self.lead_time_sigma},
            "service_level": self.service_level,
            "z_score": z_for(self.service_level),
        }


# A stable supplier and an unstable one. The point of the pair is that the two
# formulas agree on the first and diverge badly on the second, so a test suite
# that only covers the stable case passes while the bug is live.
STABLE_SUPPLIER = PlanningScenario("SKU-1001", 120.0, 18.0, 14.0, 0.0)
VOLATILE_SUPPLIER = PlanningScenario("SKU-2044", 120.0, 18.0, 14.0, 4.5)


# ---------------------------------------------------------------------------
# Part one: the test case matrix
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TestCase:
    case_id: str
    module: str
    title: str
    severity_if_failed: str
    preconditions: tuple
    steps: tuple
    expected: str
    is_regression: bool

    @property
    def rank(self) -> int:
        return severity_rank(self.severity_if_failed)


def get_supply_chain_test_cases() -> list:
    """The matrix, ranked by what it costs when the case fails.

    Every case names what it expects as a number or a rule rather than as
    "works correctly", because a planning case judged by eye is not a case.
    """
    cases = [
        TestCase(
            "SC-101", MODULE_SAFETY_STOCK,
            "Safety stock carries lead time variance, not just demand variance",
            SEV_BLOCKER,
            (f"{VOLATILE_SUPPLIER.sku} has lead time sigma of "
             f"{VOLATILE_SUPPLIER.lead_time_sigma} days.",
             "Service level is set to 95 percent."),
            ("Open the planning parameters for the SKU.",
             "Trigger a safety stock recalculation.",
             "Read the computed safety stock."),
            (f"{VOLATILE_SUPPLIER.correct_safety_stock} units, from King's "
             f"formula. A result of {VOLATILE_SUPPLIER.naive_safety_stock} "
             f"means the lead time term was dropped."),
            True),
        TestCase(
            "SC-102", MODULE_SAFETY_STOCK,
            "A fixed lead time makes both formulas agree",
            SEV_MAJOR,
            (f"{STABLE_SUPPLIER.sku} has lead time sigma of zero.",),
            ("Recalculate safety stock.",
             "Compare against the demand only formula."),
            (f"Both give {STABLE_SUPPLIER.correct_safety_stock} units. This "
             f"case passes even when SC-101 fails, which is why a suite "
             f"containing only this one is misleading."),
            True),
        TestCase(
            "SC-103", MODULE_LEAD_TIME,
            "Lead time sigma is read in days, not in the planning calendar unit",
            SEV_BLOCKER,
            ("Supplier lead time is held in days.",
             "The planning calendar is set to weeks."),
            ("Set lead time sigma to 4.5 days.",
             "Recalculate.",
             "Inspect the variance term in the audit log."),
            ("The variance term uses 4.5 days. If it reads 4.5 weeks the "
             "buffer is overstated roughly sevenfold, which hides as excess "
             "stock rather than as an error."),
            True),
        TestCase(
            "SC-104", MODULE_PLANNING,
            "Reorder point is demand over lead time plus safety stock",
            SEV_CRITICAL,
            (f"{VOLATILE_SUPPLIER.sku} parameters as configured.",),
            ("Read the reorder point after recalculation.",),
            (f"{VOLATILE_SUPPLIER.correct_reorder_point} units. Omitting "
             f"safety stock gives "
             f"{VOLATILE_SUPPLIER.demand_mean * VOLATILE_SUPPLIER.lead_time_days:,.0f}"
             f", which looks plausible and orders too late every time."),
            True),
        TestCase(
            "SC-105", MODULE_SAFETY_STOCK,
            "Raising the service level raises the buffer monotonically",
            SEV_MAJOR,
            ("Same SKU, service level swept across the defined rungs.",),
            ("Recalculate at 90, 95, 98 and 99 percent.",
             "Compare the four results."),
            ("Each is strictly larger than the last. A flat or falling series "
             "means the z lookup is keyed wrongly."),
            True),
        TestCase(
            "SC-106", MODULE_LEAD_TIME,
            "An undefined service level is refused rather than interpolated",
            SEV_MAJOR,
            ("Service level set to 96.5 percent, which has no defined z.",),
            ("Attempt a recalculation.",),
            ("The run is rejected with the defined rungs named. Silently "
             "rounding to the nearest rung changes the buffer without telling "
             "anyone."),
            False),
        TestCase(
            "SC-107", MODULE_REPLENISH,
            "Zero demand variance still produces a lead time buffer",
            SEV_CRITICAL,
            ("Demand sigma is zero and lead time sigma is 4.5 days.",),
            ("Recalculate safety stock.",),
            ("A positive buffer. Zero would mean the formula multiplies the "
             "two variances instead of adding them, which is the error that "
             "leaves a perfectly predictable product stocked out."),
            True),
        TestCase(
            "SC-108", MODULE_REPLENISH,
            "A negative or absent lead time does not produce a complex result",
            SEV_MINOR,
            ("Lead time is missing on a newly created SKU.",),
            ("Recalculate.",),
            ("Treated as zero rather than producing a square root of a "
             "negative number. A crash here is preferable to a silent NaN, "
             "and neither is acceptable in a nightly batch."),
            False),
    ]
    return sorted(cases, key=lambda case: (case.rank, case.case_id))


def case_matrix_rows() -> list:
    return [
        {
            "Case": case.case_id,
            "Module": case.module,
            "Title": case.title,
            "If it fails": case.severity_if_failed,
            "Steps": str(len(case.steps)),
            "Regression": "yes" if case.is_regression else "no",
        }
        for case in get_supply_chain_test_cases()
    ]


# ---------------------------------------------------------------------------
# Part two: the defect report
# ---------------------------------------------------------------------------

@dataclass
class DefectReport:
    ticket_id: str
    module: str
    severity: str
    title: str
    summary: str
    preconditions: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    expected_value: float = 0.0
    actual_value: float = 0.0
    expected_label: str = ""
    actual_label: str = ""
    formula_expected: str = ""
    formula_actual: str = ""
    payload: dict = field(default_factory=dict)
    business_impact: str = ""

    @property
    def tone(self) -> str:
        return severity_tone(self.severity)

    @property
    def variance(self) -> float:
        return round(self.actual_value - self.expected_value, 2)

    @property
    def variance_percent(self) -> float:
        if self.expected_value == 0:
            return 0.0
        return round((self.variance / self.expected_value) * 100, 2)

    def payload_json(self) -> str:
        return json.dumps(self.payload, indent=2)

    def rows(self) -> list:
        return [
            {"Field": "Ticket", "Value": self.ticket_id},
            {"Field": "Module", "Value": self.module},
            {"Field": "Severity", "Value": self.severity},
            {"Field": self.expected_label or "Expected",
             "Value": f"{self.expected_value:,.2f}"},
            {"Field": self.actual_label or "Actual",
             "Value": f"{self.actual_value:,.2f}"},
            {"Field": "Variance",
             "Value": f"{self.variance:,.2f} ({self.variance_percent:,.1f}%)"},
        ]

    def as_markdown(self) -> str:
        """The ticket, ready to paste into a tracker."""
        steps = "\n".join(f"{index}. {step}"
                          for index, step in enumerate(self.steps, start=1))
        pre = "\n".join(f"- {item}" for item in self.preconditions)
        return (
            f"# {self.ticket_id} {self.title}\n\n"
            f"**Module** {self.module}  \n"
            f"**Severity** {self.severity}\n\n"
            f"## Summary\n{self.summary}\n\n"
            f"## Preconditions\n{pre}\n\n"
            f"## Steps to reproduce\n{steps}\n\n"
            f"## Expected\n{self.expected_value:,.2f} "
            f"({self.expected_label})\n"
            f"`{self.formula_expected}`\n\n"
            f"## Actual\n{self.actual_value:,.2f} ({self.actual_label})\n"
            f"`{self.formula_actual}`\n\n"
            f"## Variance\n{self.variance:,.2f} units, "
            f"{self.variance_percent:,.1f} percent\n\n"
            f"## Business impact\n{self.business_impact}\n\n"
            f"## Sample payload\n```json\n{self.payload_json()}\n```\n")


def generate_defect_report(module_name: str = MODULE_SAFETY_STOCK,
                           severity: str = SEV_BLOCKER,
                           scenario: PlanningScenario | None = None
                           ) -> DefectReport:
    """Build a ticket an engineer can act on without asking a question.

    The expected and actual figures are computed from the scenario by the two
    formulas rather than typed in, so a developer reproducing the ticket gets
    the same numbers. A planning ticket whose expected value is prose is a
    ticket that will be closed as cannot reproduce.
    """
    if module_name not in MODULES:
        raise ValueError(f"unknown module {module_name!r}. "
                         f"Defined modules are {list(MODULES)}.")
    if severity not in SEVERITIES:
        raise ValueError(f"unknown severity {severity!r}.")

    case = scenario or VOLATILE_SUPPLIER
    # hashlib rather than hash(). Python randomises string hashing per
    # process, so hash() would give this ticket a different number on every
    # run, and an engine that claims determinism cannot hand out an identifier
    # that moves between the report and the reproduction.
    seed = f"{case.sku}|{module_name}|{severity}".encode("utf-8")
    ticket = f"SCQA-{int(hashlib.sha256(seed).hexdigest()[:6], 16) % 9000 + 1000}"

    report = DefectReport(
        ticket_id=ticket, module=module_name, severity=severity,
        title=(f"Safety stock for {case.sku} omits the lead time variance "
               f"term"),
        summary=(
            f"The recalculation for {case.sku} returns "
            f"{case.naive_safety_stock:,.2f} units where "
            f"{case.correct_safety_stock:,.2f} is required at a "
            f"{case.service_level:.0%} service level. Nothing errors and the "
            f"figure looks reasonable, which is why it has survived: the "
            f"formula in use accounts for demand varying and treats lead time "
            f"as fixed, and this supplier's lead time has a standard "
            f"deviation of {case.lead_time_sigma} days."),
        preconditions=[
            f"{case.sku} exists with demand mean {case.demand_mean} a day and "
            f"sigma {case.demand_sigma}.",
            f"Supplier lead time mean is {case.lead_time_days} days with "
            f"sigma {case.lead_time_sigma} days.",
            f"Service level is {case.service_level:.0%}, giving z "
            f"{z_for(case.service_level)}.",
        ],
        steps=[
            f"Load the planning parameters for {case.sku}.",
            "Trigger a safety stock recalculation from the planning module.",
            "Read the computed safety stock and reorder point.",
            "Compare the safety stock against King's formula by hand.",
        ],
        expected_value=case.correct_safety_stock,
        actual_value=case.naive_safety_stock,
        expected_label="units, King's formula with both variances",
        actual_label="units, demand variance only",
        formula_expected=("SS = z * sqrt(LT * sigma_D^2 + D^2 * "
                          "sigma_LT^2)"),
        formula_actual="SS = z * sigma_D * sqrt(LT)",
        payload={
            "request": case.payload(),
            "response_actual": {
                "safety_stock": case.naive_safety_stock,
                "reorder_point": case.naive_reorder_point,
                "formula": "demand_variance_only",
            },
            "response_expected": {
                "safety_stock": case.correct_safety_stock,
                "reorder_point": case.correct_reorder_point,
                "formula": "king_combined_variance",
            },
        },
        business_impact=(
            f"The buffer is short by {abs(case.shortfall):,.2f} units, so the "
            f"reorder point fires "
            f"{abs(case.correct_reorder_point - case.naive_reorder_point):,.2f} "
            f"units too late. The effect is invisible while the supplier is "
            f"on time and appears as a stockout in the first week they are "
            f"not, which is the week it costs the most."),
    )
    return report


def defect_backlog() -> list:
    """One ticket per module, so the board is not a single example."""
    pairs = ((MODULE_SAFETY_STOCK, SEV_BLOCKER),
             (MODULE_LEAD_TIME, SEV_BLOCKER),
             (MODULE_PLANNING, SEV_CRITICAL),
             (MODULE_REPLENISH, SEV_MAJOR))
    return [generate_defect_report(module, severity)
            for module, severity in pairs]


# ---------------------------------------------------------------------------
# Part three: execution metrics
# ---------------------------------------------------------------------------

REGRESSION_CLEAN = "Clean"
REGRESSION_AT_RISK = "At risk"
REGRESSION_FAILED = "Failed"


@dataclass(frozen=True)
class ExecutionCycle:
    cycle: str
    planned: int
    passed: int
    failed: int
    blocked: int
    open_blockers: int

    @property
    def executed(self) -> int:
        return self.passed + self.failed

    @property
    def not_run(self) -> int:
        return max(self.planned - self.executed - self.blocked, 0)

    @property
    def pass_rate(self) -> float:
        """Of what ran. This is the number people quote."""
        return round(self.passed / self.executed, 4) if self.executed else 0.0

    @property
    def coverage(self) -> float:
        """Of what was planned. This is the number that matters.

        A cycle can report a pass rate of ninety four percent while a third of
        the suite never ran. A blocked test is not a pass and it is not a
        failure, it is an unknown, and counting it out of the denominator is
        how a release is signed off on evidence nobody gathered.
        """
        return round(self.executed / self.planned, 4) if self.planned else 0.0

    @property
    def verified_rate(self) -> float:
        """Passes as a share of everything planned, unknowns included."""
        return round(self.passed / self.planned, 4) if self.planned else 0.0

    @property
    def regression_status(self) -> str:
        if self.open_blockers > 0 or self.failed > 0:
            return REGRESSION_FAILED
        if self.coverage < 0.9 or self.blocked > 0:
            return REGRESSION_AT_RISK
        return REGRESSION_CLEAN

    @property
    def tone(self) -> str:
        return {REGRESSION_CLEAN: "ok", REGRESSION_AT_RISK: "warn"}.get(
            self.regression_status, "crit")

    @property
    def shippable(self) -> bool:
        return self.regression_status == REGRESSION_CLEAN


def get_execution_metrics() -> dict:
    """Three cycles, so the trend is visible and the last one is judged."""
    cycles = (
        ExecutionCycle("Cycle 1, smoke", 48, 31, 9, 8, 3),
        ExecutionCycle("Cycle 2, full regression", 96, 78, 4, 14, 2),
        ExecutionCycle("Cycle 3, release candidate", 96, 92, 0, 4, 0),
    )
    latest = cycles[-1]
    return {
        "cycles": cycles,
        "latest": latest,
        "total_planned": sum(c.planned for c in cycles),
        "total_passed": sum(c.passed for c in cycles),
        "open_blockers": latest.open_blockers,
        "regression_status": latest.regression_status,
        "shippable": latest.shippable,
    }


def execution_rows() -> list:
    return [
        {
            "Cycle": cycle.cycle,
            "Planned": str(cycle.planned),
            "Passed": str(cycle.passed),
            "Failed": str(cycle.failed),
            "Blocked": str(cycle.blocked),
            "Pass rate of executed": f"{cycle.pass_rate:.1%}",
            "Verified of planned": f"{cycle.verified_rate:.1%}",
            "Status": cycle.regression_status,
        }
        for cycle in get_execution_metrics()["cycles"]
    ]


def console_summary() -> dict:
    metrics = get_execution_metrics()
    cases = get_supply_chain_test_cases()
    return {
        "test_cases": len(cases),
        "regression_cases": len([c for c in cases if c.is_regression]),
        "blocker_cases": len([c for c in cases
                              if c.severity_if_failed == SEV_BLOCKER]),
        "modules": len(MODULES),
        "open_defects": len(defect_backlog()),
        "open_blockers": metrics["open_blockers"],
        "regression_status": metrics["regression_status"],
    }
