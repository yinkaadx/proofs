"""AppSec threat modeling engine.

No Streamlit import lives in this module, so the same engine can sit behind a
scanner, a ticket writer or a CI gate without dragging a UI along.

Four things here are deliberately not what a first pass would write.

1. Not every metric is worth arguing about. metric_sensitivity() walks all 2592
   base vectors, flips one metric at a time, and counts how often the
   qualitative band moves. The answer is lopsided: an impact metric can move a
   vector three bands, Attack Vector and Privileges Required two, while User
   Interaction moves one band and does so in 438 of its 2592 chances. Review
   time spent on the last one is review time not spent where the service level
   agreement actually changes.

2. Impact does not add up. The sub score saturates, so a bug that destroys
   confidentiality, integrity and availability is not three times the bug that
   destroys confidentiality alone. Measured: C:H alone gives impact 3.5952,
   C:H/I:H/A:H gives 5.8731. That is 1.63 times for three times the damage, and
   it is why "it hits all three" is a weaker argument than it sounds.

3. Scope is not an increment. Raising Scope to Changed swaps the entire
   Privileges Required table (Low moves 0.62 to 0.68, High moves 0.27 to 0.50),
   replaces the impact curve with a different one, then multiplies the total by
   1.08. It never lowers a base score, which this module verifies across all
   1296 pairs rather than assuming, but a team that reaches the same number by
   adding a point to a hunch got there by luck.

4. A base score is severity, not likelihood. CVSS says so in its own
   specification and backlogs are sorted by it anyway. This engine returns
   exploit likelihood as its own number and raises a finding when the two
   disagree, because that disagreement is the whole reason the backlog is in
   the wrong order.

One claim was tested and dropped. The CVSS v3.1 specification replaced a naive
ceiling with integer arithmetic in Appendix A, so it was reasonable to expect
math.ceil(score * 10) / 10 to misreport some base vectors. rounding_audit()
checks every one of the 2592 and finds zero disagreements. The Decimal path
stays because the specification defines it that way, but the panel reports the
measured negative result rather than a scare that did not survive contact with
the numbers.

The OWASP mapping is the 2025 edition. That matters more than an edition
number usually does: SSRF lost its own category, Vulnerable and Outdated
Components widened into Software Supply Chain Failures, and A10 is a category
that did not exist in 2021. A backlog labelled against the 2021 list is not
merely old, it is mislabelled.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from itertools import product
from typing import Iterable, Mapping, Sequence

ENGINE_VERSION = "1.0.0"

CVSS_VERSION = "CVSS:3.1"
OWASP_EDITION = "OWASP Top 10:2025"
PRIOR_OWASP_EDITION = "OWASP Top 10:2021"

# ---------------------------------------------------------------------------
# CVSS v3.1 base metric weights
# ---------------------------------------------------------------------------
# Taken from the FIRST CVSS v3.1 specification, section 7.1, and cross checked
# against the Red Hat reference implementation before this file was written.
# Every value is a Decimal string so no weight is ever a repeating binary
# fraction before it reaches the arithmetic.

AV_WEIGHTS = {"N": Decimal("0.85"), "A": Decimal("0.62"),
              "L": Decimal("0.55"), "P": Decimal("0.2")}
AC_WEIGHTS = {"L": Decimal("0.77"), "H": Decimal("0.44")}
# Privileges Required is the only metric whose table depends on Scope.
PR_WEIGHTS_UNCHANGED = {"N": Decimal("0.85"), "L": Decimal("0.62"),
                        "H": Decimal("0.27")}
PR_WEIGHTS_CHANGED = {"N": Decimal("0.85"), "L": Decimal("0.68"),
                      "H": Decimal("0.50")}
UI_WEIGHTS = {"N": Decimal("0.85"), "R": Decimal("0.62")}
CIA_WEIGHTS = {"H": Decimal("0.56"), "L": Decimal("0.22"), "N": Decimal("0")}
SCOPE_VALUES = ("U", "C")

EXPLOITABILITY_COEFFICIENT = Decimal("8.22")
IMPACT_COEFFICIENT_UNCHANGED = Decimal("6.42")
IMPACT_CHANGED_LINEAR = Decimal("7.52")
IMPACT_CHANGED_OFFSET = Decimal("0.029")
IMPACT_CHANGED_TAIL = Decimal("3.25")
IMPACT_CHANGED_TAIL_OFFSET = Decimal("0.02")
IMPACT_CHANGED_TAIL_POWER = 15
SCOPE_CHANGED_MULTIPLIER = Decimal("1.08")
SCORE_CEILING = Decimal("10")

METRIC_ORDER = ("AV", "AC", "PR", "UI", "S", "C", "I", "A")

METRIC_NAMES = {
    "AV": "Attack Vector",
    "AC": "Attack Complexity",
    "PR": "Privileges Required",
    "UI": "User Interaction",
    "S": "Scope",
    "C": "Confidentiality",
    "I": "Integrity",
    "A": "Availability",
}

VALUE_NAMES = {
    "AV": {"N": "Network", "A": "Adjacent", "L": "Local", "P": "Physical"},
    "AC": {"L": "Low", "H": "High"},
    "PR": {"N": "None", "L": "Low", "H": "High"},
    "UI": {"N": "None", "R": "Required"},
    "S": {"U": "Unchanged", "C": "Changed"},
    "C": {"H": "High", "L": "Low", "N": "None"},
    "I": {"H": "High", "L": "Low", "N": "None"},
    "A": {"H": "High", "L": "Low", "N": "None"},
}

# CVSS v3.1 qualitative severity rating scale, specification section 5.
SEVERITY_NONE = "None"
SEVERITY_LOW = "Low"
SEVERITY_MEDIUM = "Medium"
SEVERITY_HIGH = "High"
SEVERITY_CRITICAL = "Critical"

FINDING_OK = "ok"
FINDING_WARN = "warn"
FINDING_CRIT = "crit"


def severity_band(score: Decimal) -> str:
    """The published qualitative band for a base score."""
    if score <= Decimal("0.0"):
        return SEVERITY_NONE
    if score < Decimal("4.0"):
        return SEVERITY_LOW
    if score < Decimal("7.0"):
        return SEVERITY_MEDIUM
    if score < Decimal("9.0"):
        return SEVERITY_HIGH
    return SEVERITY_CRITICAL


def roundup(value: Decimal) -> Decimal:
    """CVSS v3.1 Roundup: the smallest one decimal number not below the input.

    Decimal with ROUND_CEILING gives the specification's answer directly. The
    specification spells the same rule out as integer arithmetic precisely
    because a float implementation does not.
    """
    return value.quantize(Decimal("0.1"), rounding=ROUND_CEILING)


def naive_roundup(value: float) -> float:
    """The implementation almost everyone writes first.

    Kept so the divergence can be measured instead of asserted. Never used to
    produce a score this tool reports.
    """
    return math.ceil(value * 10) / 10


@dataclass(frozen=True)
class ScoreBreakdown:
    """Every intermediate the specification names, kept rather than discarded."""

    metrics: Mapping[str, str]
    impact_subscore: Decimal
    impact: Decimal
    exploitability: Decimal
    raw: Decimal
    score: Decimal
    naive_score: float
    pr_weight: Decimal
    pr_table: str

    @property
    def vector(self) -> str:
        parts = "/".join(f"{key}:{self.metrics[key]}" for key in METRIC_ORDER)
        return f"{CVSS_VERSION}/{parts}"

    @property
    def band(self) -> str:
        return severity_band(self.score)

    @property
    def diverges(self) -> bool:
        """True when the naive float implementation reports a different score."""
        return float(self.score) != self.naive_score

    def rows(self) -> list[dict[str, str]]:
        """Arrow safe: every cell is a string."""
        out: list[dict[str, str]] = []
        for key in METRIC_ORDER:
            value = self.metrics[key]
            if key == "PR":
                weight = f"{self.pr_weight} ({self.pr_table} table)"
            elif key == "S":
                weight = "selects the table and the curve"
            elif key == "AV":
                weight = str(AV_WEIGHTS[value])
            elif key == "AC":
                weight = str(AC_WEIGHTS[value])
            elif key == "UI":
                weight = str(UI_WEIGHTS[value])
            else:
                weight = str(CIA_WEIGHTS[value])
            out.append({
                "Metric": METRIC_NAMES[key],
                "Value": VALUE_NAMES[key][value],
                "Code": f"{key}:{value}",
                "Weight": weight,
            })
        return out

    def arithmetic_rows(self) -> list[dict[str, str]]:
        return [
            {"Step": "Impact sub score",
             "Value": f"{self.impact_subscore:.6f}"},
            {"Step": "Impact", "Value": f"{self.impact:.6f}"},
            {"Step": "Exploitability", "Value": f"{self.exploitability:.6f}"},
            {"Step": "Before rounding", "Value": f"{self.raw:.6f}"},
            {"Step": "Specification roundup", "Value": f"{self.score:.1f}"},
            {"Step": "Naive float ceiling", "Value": f"{self.naive_score:.1f}"},
        ]


def _validate(metrics: Mapping[str, str]) -> None:
    for key in METRIC_ORDER:
        if key not in metrics:
            raise ValueError(f"missing CVSS metric {key}")
    allowed = {
        "AV": set(AV_WEIGHTS), "AC": set(AC_WEIGHTS),
        "PR": set(PR_WEIGHTS_UNCHANGED), "UI": set(UI_WEIGHTS),
        "S": set(SCOPE_VALUES), "C": set(CIA_WEIGHTS),
        "I": set(CIA_WEIGHTS), "A": set(CIA_WEIGHTS),
    }
    for key, values in allowed.items():
        if metrics[key] not in values:
            raise ValueError(
                f"{key}:{metrics[key]} is not a CVSS v3.1 value, "
                f"expected one of {sorted(values)}")


def base_score(metrics: Mapping[str, str]) -> ScoreBreakdown:
    """The CVSS v3.1 base score, with every intermediate preserved."""
    _validate(metrics)
    scope_changed = metrics["S"] == "C"

    iss = Decimal(1) - (
        (Decimal(1) - CIA_WEIGHTS[metrics["C"]])
        * (Decimal(1) - CIA_WEIGHTS[metrics["I"]])
        * (Decimal(1) - CIA_WEIGHTS[metrics["A"]])
    )

    if scope_changed:
        impact = (
            IMPACT_CHANGED_LINEAR * (iss - IMPACT_CHANGED_OFFSET)
            - IMPACT_CHANGED_TAIL
            * (iss - IMPACT_CHANGED_TAIL_OFFSET) ** IMPACT_CHANGED_TAIL_POWER
        )
        pr_table = PR_WEIGHTS_CHANGED
        pr_table_name = "Scope Changed"
    else:
        impact = IMPACT_COEFFICIENT_UNCHANGED * iss
        pr_table = PR_WEIGHTS_UNCHANGED
        pr_table_name = "Scope Unchanged"

    pr_weight = pr_table[metrics["PR"]]
    exploitability = (
        EXPLOITABILITY_COEFFICIENT
        * AV_WEIGHTS[metrics["AV"]]
        * AC_WEIGHTS[metrics["AC"]]
        * pr_weight
        * UI_WEIGHTS[metrics["UI"]]
    )

    if impact <= 0:
        raw = Decimal(0)
        score = Decimal("0.0")
        naive = 0.0
    else:
        total = impact + exploitability
        if scope_changed:
            total = SCOPE_CHANGED_MULTIPLIER * total
        raw = min(total, SCORE_CEILING)
        score = roundup(raw)
        naive = min(naive_roundup(float(raw)), 10.0)

    return ScoreBreakdown(
        metrics=dict(metrics),
        impact_subscore=iss,
        impact=impact,
        exploitability=exploitability,
        raw=raw,
        score=score,
        naive_score=naive,
        pr_weight=pr_weight,
        pr_table=pr_table_name,
    )


def all_metric_combinations() -> Iterable[dict[str, str]]:
    """Every base metric combination the specification allows."""
    for av, ac, pr, ui, scope, conf, integ, avail in product(
        sorted(AV_WEIGHTS), sorted(AC_WEIGHTS), sorted(PR_WEIGHTS_UNCHANGED),
        sorted(UI_WEIGHTS), SCOPE_VALUES, sorted(CIA_WEIGHTS),
        sorted(CIA_WEIGHTS), sorted(CIA_WEIGHTS),
    ):
        yield {"AV": av, "AC": ac, "PR": pr, "UI": ui,
               "S": scope, "C": conf, "I": integ, "A": avail}


def divergent_vectors() -> list[ScoreBreakdown]:
    """Every vector where the naive float ceiling reports the wrong score."""
    return [b for b in (base_score(m) for m in all_metric_combinations())
            if b.diverges]


@dataclass(frozen=True)
class RoundingAudit:
    """The measured result of the check, whichever way it came out."""

    checked: int
    divergent: int
    examples: Sequence[ScoreBreakdown]

    @property
    def clean(self) -> bool:
        return self.divergent == 0

    @property
    def verdict(self) -> str:
        if self.clean:
            return (
                f"All {self.checked} base vectors agree. The float shortcut "
                f"happens to be safe on the base score. That is a measurement, "
                f"not a licence: the specification defines the Decimal rule and "
                f"this engine uses it."
            )
        return (
            f"{self.divergent} of {self.checked} base vectors disagree, so a "
            f"float implementation misreports real severities."
        )

    def rows(self) -> list[dict[str, str]]:
        return [{
            "Vector": b.vector,
            "Specification": f"{b.score:.1f}",
            "Naive float": f"{b.naive_score:.1f}",
            "Specification band": b.band,
            "Naive band": severity_band(Decimal(str(b.naive_score))),
        } for b in self.examples]


def rounding_audit(limit: int = 12) -> RoundingAudit:
    """Compare the specification rounding against the naive float ceiling.

    The result is reported rather than assumed. When this engine was written
    the expectation was that some vectors would disagree; none do.
    """
    checked = 0
    divergent: list[ScoreBreakdown] = []
    for metrics in all_metric_combinations():
        checked += 1
        breakdown = base_score(metrics)
        if breakdown.diverges:
            divergent.append(breakdown)
    return RoundingAudit(checked=checked, divergent=len(divergent),
                         examples=tuple(divergent[:limit]))


BAND_ORDER = (SEVERITY_NONE, SEVERITY_LOW, SEVERITY_MEDIUM,
              SEVERITY_HIGH, SEVERITY_CRITICAL)

_METRIC_ALTERNATIVES: Mapping[str, tuple[str, ...]] = {
    "AV": tuple(sorted(AV_WEIGHTS)),
    "AC": tuple(sorted(AC_WEIGHTS)),
    "PR": tuple(sorted(PR_WEIGHTS_UNCHANGED)),
    "UI": tuple(sorted(UI_WEIGHTS)),
    "S": SCOPE_VALUES,
    "C": tuple(sorted(CIA_WEIGHTS)),
    "I": tuple(sorted(CIA_WEIGHTS)),
    "A": tuple(sorted(CIA_WEIGHTS)),
}


@dataclass(frozen=True)
class MetricSensitivity:
    metric: str
    name: str
    trials: int
    band_flips: int
    max_band_jump: int

    @property
    def flip_rate(self) -> float:
        return self.band_flips / self.trials if self.trials else 0.0


def metric_sensitivity() -> list[MetricSensitivity]:
    """How much one metric disagreement can move the qualitative band.

    Every vector, every single metric substitution, counted. The ordering this
    produces is the ordering a review should argue in.
    """
    scores = {}
    for metrics in all_metric_combinations():
        key = tuple(metrics[m] for m in METRIC_ORDER)
        scores[key] = BAND_ORDER.index(base_score(metrics).band)

    out: list[MetricSensitivity] = []
    for position, metric in enumerate(METRIC_ORDER):
        trials = flips = jump = 0
        for key, band in scores.items():
            for value in _METRIC_ALTERNATIVES[metric]:
                if value == key[position]:
                    continue
                trials += 1
                other = key[:position] + (value,) + key[position + 1:]
                moved = abs(scores[other] - band)
                if moved:
                    flips += 1
                jump = max(jump, moved)
        out.append(MetricSensitivity(metric=metric, name=METRIC_NAMES[metric],
                                     trials=trials, band_flips=flips,
                                     max_band_jump=jump))
    out.sort(key=lambda s: (-s.max_band_jump, -s.flip_rate))
    return out


def sensitivity_rows() -> list[dict[str, str]]:
    return [{
        "Metric": s.name,
        "Code": s.metric,
        "Substitutions tried": str(s.trials),
        "Band changed": str(s.band_flips),
        "Share": f"{s.flip_rate * 100:.0f}%",
        "Largest band jump": str(s.max_band_jump),
    } for s in metric_sensitivity()]


SATURATION_BASE = {"AV": "N", "AC": "L", "PR": "N", "UI": "N", "S": "U"}


def impact_saturation_rows() -> list[dict[str, str]]:
    """What each additional wrecked impact metric is actually worth."""
    steps = (
        ("Confidentiality only", {"C": "H", "I": "N", "A": "N"}),
        ("Confidentiality and integrity", {"C": "H", "I": "H", "A": "N"}),
        ("All three", {"C": "H", "I": "H", "A": "H"}),
    )
    first_impact = None
    rows: list[dict[str, str]] = []
    for label, impacts in steps:
        breakdown = base_score({**SATURATION_BASE, **impacts})
        if first_impact is None:
            first_impact = breakdown.impact
        rows.append({
            "Damage": label,
            "Impact sub score": f"{breakdown.impact_subscore:.4f}",
            "Impact": f"{breakdown.impact:.4f}",
            "Base score": f"{breakdown.score:.1f}",
            "Times the first row": f"{breakdown.impact / first_impact:.2f}x",
        })
    return rows


def scope_is_monotonic() -> tuple[int, int]:
    """Count the Scope pairs where Changed does not raise the score.

    Returns (pairs checked, pairs where Changed scored lower).
    """
    checked = lowered = 0
    for metrics in all_metric_combinations():
        if metrics["S"] != "U":
            continue
        checked += 1
        if base_score({**metrics, "S": "C"}).score < base_score(metrics).score:
            lowered += 1
    return checked, lowered


# ---------------------------------------------------------------------------
# Vulnerability classes and the layers they are found at
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VulnClass:
    key: str
    name: str
    metrics: Mapping[str, str]
    owasp_2025: str
    owasp_2021: str
    cwe: str
    mechanics: str
    # How much public, packaged tooling exists for this class, 0 to 40.
    tooling: int
    # Whether crossing a trust boundary is inherent to the class.
    crosses_boundary: bool


VULN_CLASSES: tuple[VulnClass, ...] = (
    VulnClass(
        key="bola",
        name="Broken object level authorization",
        metrics={"AV": "N", "AC": "L", "PR": "L", "UI": "N",
                 "S": "U", "C": "H", "I": "L", "A": "N"},
        owasp_2025="A01:2025 Broken Access Control",
        owasp_2021="A01:2021 Broken Access Control",
        cwe="CWE-639",
        mechanics=(
            "The handler reads the object identifier from the request and "
            "trusts it. The session is valid, the route is authenticated and "
            "the row belongs to someone else. Nothing in the request looks "
            "malformed, which is why the WAF sees a normal day."
        ),
        tooling=34,
        crosses_boundary=False,
    ),
    VulnClass(
        key="sqli",
        name="SQL injection in a reporting filter",
        metrics={"AV": "N", "AC": "L", "PR": "L", "UI": "N",
                 "S": "U", "C": "H", "I": "H", "A": "H"},
        owasp_2025="A05:2025 Injection",
        owasp_2021="A03:2021 Injection",
        cwe="CWE-89",
        mechanics=(
            "A filter value reaches the query as string concatenation because "
            "the column name is dynamic and the parameter binder will not bind "
            "identifiers. The fix is an allow list of column names, not another "
            "layer of escaping."
        ),
        tooling=38,
        crosses_boundary=False,
    ),
    VulnClass(
        key="ssrf",
        name="Server side request forgery on an image fetcher",
        metrics={"AV": "N", "AC": "L", "PR": "L", "UI": "N",
                 "S": "C", "C": "H", "I": "L", "A": "N"},
        owasp_2025="A01:2025 Broken Access Control",
        owasp_2021="A10:2021 Server-Side Request Forgery (SSRF)",
        cwe="CWE-918",
        mechanics=(
            "The service fetches a caller supplied URL from inside the network. "
            "The request leaves an address the internal plane trusts, so the "
            "metadata endpoint answers it. Blocking by hostname loses to a DNS "
            "record that resolves to a private address after the check."
        ),
        tooling=27,
        crosses_boundary=True,
    ),
    VulnClass(
        key="supply_chain",
        name="Build dependency with a known critical advisory",
        metrics={"AV": "N", "AC": "H", "PR": "N", "UI": "N",
                 "S": "C", "C": "H", "I": "H", "A": "H"},
        owasp_2025="A03:2025 Software Supply Chain Failures",
        owasp_2021="A06:2021 Vulnerable and Outdated Components",
        cwe="CWE-1395",
        mechanics=(
            "The advisory is against a transitive dependency the lockfile pins "
            "and the manifest never names. Reachability decides exploitability: "
            "the package is installed, the vulnerable function may never be "
            "called, and the scanner cannot tell the difference."
        ),
        tooling=22,
        crosses_boundary=True,
    ),
    VulnClass(
        key="secret_leak",
        name="Long lived credential committed to the repository",
        metrics={"AV": "N", "AC": "L", "PR": "N", "UI": "N",
                 "S": "C", "C": "H", "I": "H", "A": "N"},
        owasp_2025="A02:2025 Security Misconfiguration",
        owasp_2021="A05:2021 Security Misconfiguration",
        cwe="CWE-798",
        mechanics=(
            "The key is in an old commit, not the working tree. Deleting the "
            "file changes nothing because the object is still reachable from "
            "history and from every fork. Rotation is the fix and rewriting "
            "history is not."
        ),
        tooling=36,
        crosses_boundary=True,
    ),
    VulnClass(
        key="reset_rate",
        name="Missing rate limit on password reset",
        metrics={"AV": "N", "AC": "L", "PR": "N", "UI": "N",
                 "S": "U", "C": "L", "I": "L", "A": "N"},
        owasp_2025="A07:2025 Authentication Failures",
        owasp_2021="A07:2021 Identification and Authentication Failures",
        cwe="CWE-307",
        mechanics=(
            "The endpoint answers differently for a known and an unknown "
            "address, so it enumerates accounts before anyone tries a token. "
            "A per address limit without a per source limit slows the report "
            "and not the attack."
        ),
        tooling=30,
        crosses_boundary=False,
    ),
    VulnClass(
        key="exception_leak",
        name="Unhandled exception returns an internal stack trace",
        metrics={"AV": "N", "AC": "L", "PR": "N", "UI": "N",
                 "S": "U", "C": "L", "I": "N", "A": "L"},
        owasp_2025="A10:2025 Mishandling of Exceptional Conditions",
        owasp_2021="A04:2021 Insecure Design",
        cwe="CWE-209",
        mechanics=(
            "A malformed field reaches a parser that raises, the framework "
            "renders the trace, and the response names the ORM, the file path "
            "and the driver version. The same path also skips the cleanup that "
            "the success path performs."
        ),
        tooling=14,
        crosses_boundary=False,
    ),
    VulnClass(
        key="stored_xss",
        name="Stored cross site scripting in a comment field",
        metrics={"AV": "N", "AC": "L", "PR": "L", "UI": "R",
                 "S": "C", "C": "L", "I": "L", "A": "N"},
        owasp_2025="A05:2025 Injection",
        owasp_2021="A03:2021 Injection",
        cwe="CWE-79",
        mechanics=(
            "The payload is stored once and rendered to every later reader, so "
            "the victim and the attacker are different people. That is what "
            "moves Scope to Changed, and Scope changes the privilege table "
            "rather than simply adding points."
        ),
        tooling=32,
        crosses_boundary=True,
    ),
)

VULN_BY_KEY = {item.key: item for item in VULN_CLASSES}
VULN_BY_NAME = {item.name: item for item in VULN_CLASSES}


@dataclass(frozen=True)
class TargetLayer:
    key: str
    name: str
    # Metric overrides this layer forces regardless of the class default.
    overrides: Mapping[str, str]
    # Internet reachability, 0 to 40, feeding exploit likelihood.
    exposure: int
    note: str


TARGET_LAYERS: tuple[TargetLayer, ...] = (
    TargetLayer(
        key="edge",
        name="Public edge",
        overrides={"AV": "N", "PR": "N"},
        exposure=40,
        note=("Reachable from the internet with no account, so Privileges "
              "Required drops to None whatever the class normally assumes."),
    ),
    TargetLayer(
        key="api",
        name="Authenticated API",
        overrides={"AV": "N", "PR": "L"},
        exposure=30,
        note=("Any signed up account reaches it. Low privilege is not a "
              "mitigation when signing up is free."),
    ),
    TargetLayer(
        key="mesh",
        name="Internal service mesh",
        overrides={"AV": "A", "PR": "L"},
        exposure=14,
        note=("Adjacent rather than network reachable. This is the layer where "
              "a foothold elsewhere turns into the real incident."),
    ),
    TargetLayer(
        key="data",
        name="Data store",
        overrides={"AV": "A", "PR": "H"},
        exposure=8,
        note=("High privilege to reach, highest impact once reached. The score "
              "falls and the blast radius does not."),
    ),
    TargetLayer(
        key="pipeline",
        name="Build pipeline",
        overrides={"AV": "N", "PR": "L", "S": "C"},
        exposure=20,
        note=("A pipeline compromise crosses into everything the pipeline can "
              "sign or deploy, so Scope is Changed by construction."),
    ),
)

LAYER_BY_KEY = {item.key: item for item in TARGET_LAYERS}
LAYER_BY_NAME = {item.name: item for item in TARGET_LAYERS}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


@dataclass(frozen=True)
class ThreatAnalysis:
    vuln: VulnClass
    layer: TargetLayer
    breakdown: ScoreBreakdown
    baseline: ScoreBreakdown
    exploit_likelihood: int
    likelihood_band: str
    findings: Sequence[Finding] = field(default_factory=tuple)

    @property
    def score(self) -> Decimal:
        return self.breakdown.score

    @property
    def band(self) -> str:
        return self.breakdown.band

    @property
    def vector(self) -> str:
        return self.breakdown.vector

    @property
    def severity_likelihood_inversion(self) -> bool:
        """True when severity and likelihood point in opposite directions."""
        high_severity = self.band in (SEVERITY_HIGH, SEVERITY_CRITICAL)
        low_severity = self.band in (SEVERITY_NONE, SEVERITY_LOW,
                                     SEVERITY_MEDIUM)
        if high_severity and self.exploit_likelihood < 40:
            return True
        return low_severity and self.exploit_likelihood >= 70

    def rows(self) -> list[dict[str, str]]:
        return self.breakdown.rows()


LIKELIHOOD_HIGH = "Likely"
LIKELIHOOD_MEDIUM = "Plausible"
LIKELIHOOD_LOW = "Unlikely"


def likelihood_band(value: int) -> str:
    if value >= 70:
        return LIKELIHOOD_HIGH
    if value >= 40:
        return LIKELIHOOD_MEDIUM
    return LIKELIHOOD_LOW


def exploit_likelihood(vuln: VulnClass, metrics: Mapping[str, str],
                       layer: TargetLayer) -> int:
    """A 0 to 100 estimate of whether this gets exploited, not how bad it is.

    Severity and likelihood are different questions and CVSS only answers the
    first. Exposure, packaged tooling and the two metrics that describe effort
    rather than damage are what decide the second.
    """
    score = layer.exposure + vuln.tooling
    if metrics["PR"] == "N":
        score += 12
    elif metrics["PR"] == "L":
        score += 6
    if metrics["UI"] == "N":
        score += 8
    if metrics["AC"] == "H":
        score -= 18
    if metrics["AV"] == "P":
        score -= 25
    elif metrics["AV"] == "L":
        score -= 12
    return max(0, min(100, score))


def analyze_threat_vector(vuln_type: str, target_layer: str) -> ThreatAnalysis:
    """Score one vulnerability class as it sits at one architectural layer.

    vuln_type and target_layer accept either the stable key or the display
    name, so a UI can pass what the user picked and a script can pass a key.
    """
    vuln = VULN_BY_KEY.get(vuln_type) or VULN_BY_NAME.get(vuln_type)
    if vuln is None:
        raise ValueError(f"unknown vulnerability type {vuln_type!r}")
    layer = LAYER_BY_KEY.get(target_layer) or LAYER_BY_NAME.get(target_layer)
    if layer is None:
        raise ValueError(f"unknown target layer {target_layer!r}")

    metrics = dict(vuln.metrics)
    metrics.update(layer.overrides)
    if vuln.crosses_boundary:
        metrics["S"] = "C"

    breakdown = base_score(metrics)
    baseline = base_score(vuln.metrics)
    likelihood = exploit_likelihood(vuln, metrics, layer)

    analysis = ThreatAnalysis(
        vuln=vuln,
        layer=layer,
        breakdown=breakdown,
        baseline=baseline,
        exploit_likelihood=likelihood,
        likelihood_band=likelihood_band(likelihood),
    )
    return ThreatAnalysis(
        vuln=vuln, layer=layer, breakdown=breakdown, baseline=baseline,
        exploit_likelihood=likelihood,
        likelihood_band=likelihood_band(likelihood),
        findings=tuple(audit_analysis(analysis)),
    )


def audit_analysis(analysis: ThreatAnalysis) -> list[Finding]:
    findings: list[Finding] = []
    breakdown = analysis.breakdown

    if analysis.severity_likelihood_inversion:
        findings.append(Finding(
            code="APPSEC-INVERSION",
            severity=FINDING_CRIT,
            title="Severity and likelihood disagree",
            detail=(
                f"The base score is {breakdown.score:.1f} ({analysis.band}) "
                f"while exploit likelihood is {analysis.exploit_likelihood} "
                f"({analysis.likelihood_band}). A backlog sorted by base score "
                f"alone puts this item in the wrong place, because CVSS base "
                f"measures severity and says so in its own specification."
            ),
            fix=("Sort by severity and likelihood together. Where they "
                 "disagree, the disagreement is the thing to write in the "
                 "ticket, not a number to average away."),
        ))

    if breakdown.metrics["S"] == "C":
        unchanged = dict(breakdown.metrics)
        unchanged["S"] = "U"
        flat = base_score(unchanged)
        findings.append(Finding(
            code="APPSEC-SCOPE",
            severity=FINDING_WARN,
            title="Scope is Changed, which is not an increment",
            detail=(
                f"With Scope Unchanged this vector scores "
                f"{flat.score:.1f}. With Scope Changed it scores "
                f"{breakdown.score:.1f}. The difference is not a bonus: the "
                f"Privileges Required weight moved to {breakdown.pr_weight}, "
                f"the impact curve was replaced, and the total was multiplied "
                f"by {SCOPE_CHANGED_MULTIPLIER}."
            ),
            fix=("Set Scope from whether the exploited component and the "
                 "affected component sit under different security "
                 "authorities, then let the equations do the rest. Changed "
                 "never lowers a base score, which this engine checks across "
                 "every pair rather than assuming."),
        ))

    impacts = [breakdown.metrics[key] for key in ("C", "I", "A")]
    if impacts.count("H") == 3:
        single = base_score({**breakdown.metrics, "I": "N", "A": "N"})
        findings.append(Finding(
            code="APPSEC-SATURATION",
            severity=FINDING_WARN,
            title="Three wrecked impact metrics are not three times one",
            detail=(
                f"Confidentiality alone on this vector scores "
                f"{single.score:.1f} with impact {single.impact:.4f}. All "
                f"three scores {breakdown.score:.1f} with impact "
                f"{breakdown.impact:.4f}, which is "
                f"{breakdown.impact / single.impact:.2f} times, not three. The "
                f"impact sub score saturates by design."
            ),
            fix=("Argue the case on what an attacker gains, not on how many "
                 "impact letters are High. The equation already discounted "
                 "the extra letters."),
        ))

    delta = breakdown.score - analysis.baseline.score
    if delta != 0:
        direction = "raises" if delta > 0 else "lowers"
        findings.append(Finding(
            code="APPSEC-LAYER",
            severity=FINDING_WARN if delta > 0 else FINDING_OK,
            title=f"The layer {direction} the score by {abs(delta):.1f}",
            detail=(
                f"The same class scores {analysis.baseline.score:.1f} at its "
                f"default placement and {breakdown.score:.1f} at "
                f"{analysis.layer.name}. {analysis.layer.note}"
            ),
            fix=("Record the layer in the finding. A class name without a "
                 "placement is not a severity."),
        ))

    if not findings:
        findings.append(Finding(
            code="APPSEC-PLAIN",
            severity=FINDING_OK,
            title="Nothing unusual in the arithmetic",
            detail=(
                f"{breakdown.vector} scores {breakdown.score:.1f} "
                f"({analysis.band}) with likelihood "
                f"{analysis.exploit_likelihood} "
                f"({analysis.likelihood_band}), and the naive and correct "
                f"rounding agree on this vector."
            ),
            fix="Triage on the score and the likelihood together as usual.",
        ))
    return findings


# ---------------------------------------------------------------------------
# Threat data to product backlog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserStory:
    ident: str
    role: str
    want: str
    so_that: str
    points: int


@dataclass(frozen=True)
class AcceptanceCriterion:
    ident: str
    given: str
    when: str
    then: str
    verifies: str


@dataclass(frozen=True)
class ControlMapping:
    framework: str
    control_id: str
    control_name: str
    why: str


@dataclass(frozen=True)
class ProductTranslation:
    headline: str
    priority: str
    stories: Sequence[UserStory]
    criteria: Sequence[AcceptanceCriterion]
    controls: Sequence[ControlMapping]
    guardrail: str

    def story_rows(self) -> list[dict[str, str]]:
        return [{
            "ID": s.ident,
            "As a": s.role,
            "I want": s.want,
            "So that": s.so_that,
            "Points": str(s.points),
        } for s in self.stories]

    def criteria_rows(self) -> list[dict[str, str]]:
        return [{
            "ID": c.ident,
            "Given": c.given,
            "When": c.when,
            "Then": c.then,
            "Verified by": c.verifies,
        } for c in self.criteria]

    def control_rows(self) -> list[dict[str, str]]:
        return [{
            "Framework": m.framework,
            "Control": m.control_id,
            "Name": m.control_name,
            "Why it applies": m.why,
        } for m in self.controls]


SOC2 = "SOC 2 Trust Services Criteria"
ISO = "ISO/IEC 27001:2022 Annex A"

# Controls that apply to every remediation reaching production, whatever the
# class. A change without a review and without a log is not a remediation
# anyone can evidence later.
BASE_CONTROLS: tuple[ControlMapping, ...] = (
    ControlMapping(SOC2, "CC7.1",
                   "Detection of vulnerabilities and configuration changes",
                   "The finding has to be detected and tracked, not only fixed."),
    ControlMapping(SOC2, "CC8.1", "Change management",
                   "The remediation is a change and needs the same approval "
                   "path as any other."),
    ControlMapping(ISO, "A.8.8", "Management of technical vulnerabilities",
                   "The class, the fix and the verification all belong in the "
                   "vulnerability record."),
    ControlMapping(ISO, "A.8.25", "Secure development life cycle",
                   "The acceptance criteria are what put the fix inside the "
                   "life cycle rather than beside it."),
)

CLASS_CONTROLS: Mapping[str, tuple[ControlMapping, ...]] = {
    "bola": (
        ControlMapping(SOC2, "CC6.1", "Logical access security",
                       "Access to an object has to be decided by the server "
                       "against the session, not by the identifier."),
        ControlMapping(ISO, "A.8.3", "Information access restriction",
                       "The restriction is per record, not per route."),
    ),
    "sqli": (
        ControlMapping(SOC2, "CC6.1", "Logical access security",
                       "Injection reaches data the session has no right to."),
        ControlMapping(ISO, "A.8.28", "Secure coding",
                       "Parameter binding and identifier allow lists are the "
                       "coding standard this breaks."),
    ),
    "ssrf": (
        ControlMapping(SOC2, "CC6.6",
                       "Logical access, boundaries and external threats",
                       "The request crosses from the untrusted side to the "
                       "internal plane."),
        ControlMapping(ISO, "A.8.20", "Networks security",
                       "Egress from the service is what has to be constrained, "
                       "not only ingress."),
    ),
    "supply_chain": (
        ControlMapping(SOC2, "CC6.8",
                       "Prevention and detection of unauthorized software",
                       "A transitive dependency is software entering the "
                       "estate without a decision."),
        ControlMapping(ISO, "A.5.23",
                       "Information security for use of cloud services",
                       "Build and registry services are third party services "
                       "in the supply path."),
    ),
    "secret_leak": (
        ControlMapping(SOC2, "CC6.1", "Logical access security",
                       "A committed credential is an access grant nobody "
                       "approved."),
        ControlMapping(ISO, "A.8.24", "Use of cryptography",
                       "Key handling and rotation sit here, and rotation is "
                       "the actual fix."),
    ),
    "reset_rate": (
        ControlMapping(SOC2, "CC6.1", "Logical access security",
                       "Authentication attempts are access attempts."),
        ControlMapping(ISO, "A.8.5", "Secure authentication",
                       "Throttling and uniform responses are authentication "
                       "requirements, not features."),
    ),
    "exception_leak": (
        ControlMapping(SOC2, "CC7.2", "System monitoring for anomalies",
                       "The trace that reaches the user should reach the log "
                       "instead."),
        ControlMapping(ISO, "A.8.26", "Application security requirements",
                       "Error handling is a stated requirement of the "
                       "application, not an implementation detail."),
    ),
    "stored_xss": (
        ControlMapping(SOC2, "CC6.7",
                       "Restricting the transmission and movement of "
                       "information",
                       "The payload moves from one user's input to another "
                       "user's browser."),
        ControlMapping(ISO, "A.8.28", "Secure coding",
                       "Contextual output encoding is the coding rule that "
                       "was missed."),
    ),
}

PRIORITY_NOW = "This sprint"
PRIORITY_NEXT = "Next sprint"
PRIORITY_BACKLOG = "Backlog with a date"

GUARDRAIL = (
    "A control mapping is evidence that the work was aimed at the right "
    "requirement. It is not evidence that the vulnerability is gone. Only the "
    "acceptance criteria, run as tests, carry that claim."
)


def _priority(analysis: ThreatAnalysis) -> str:
    likely = analysis.exploit_likelihood >= 70
    severe = analysis.band in (SEVERITY_HIGH, SEVERITY_CRITICAL)
    if likely and severe:
        return PRIORITY_NOW
    if likely or severe:
        return PRIORITY_NEXT
    return PRIORITY_BACKLOG


def _points(analysis: ThreatAnalysis) -> int:
    """Effort, which tracks the fix not the score.

    A high scoring bug with a one line fix is cheap. Scope Changed classes are
    expensive because the fix touches a boundary rather than a handler.
    """
    points = 3
    if analysis.breakdown.metrics["S"] == "C":
        points += 3
    if analysis.vuln.crosses_boundary:
        points += 2
    if analysis.layer.key == "pipeline":
        points += 2
    return points


def translate_to_product_feature(
        threat_data: ThreatAnalysis) -> ProductTranslation:
    """Turn one scored threat into stories, acceptance criteria and controls."""
    if not isinstance(threat_data, ThreatAnalysis):
        raise TypeError(
            "translate_to_product_feature expects the ThreatAnalysis returned "
            "by analyze_threat_vector")

    vuln = threat_data.vuln
    layer = threat_data.layer
    stem = vuln.key.upper().replace("_", "")
    points = _points(threat_data)

    stories = (
        UserStory(
            ident=f"{stem}-1",
            role=f"customer using the {layer.name.lower()}",
            want=("a request for a record I do not own to be refused by the "
                  "server"
                  if vuln.key == "bola"
                  else f"the product to refuse the {vuln.name.lower()} path"),
            so_that="my data stays mine even when someone edits a request",
            points=points,
        ),
        UserStory(
            ident=f"{stem}-2",
            role="engineer on the owning team",
            want=(f"a regression test that fails today against {layer.name} "
                  f"and passes after the fix"),
            so_that="the class cannot come back through a later refactor",
            points=max(2, points - 1),
        ),
        UserStory(
            ident=f"{stem}-3",
            role="security reviewer",
            want=(f"the {vuln.cwe} finding to carry its layer, its vector and "
                  f"its likelihood"),
            so_that="the backlog order reflects what will actually be attacked",
            points=2,
        ),
    )

    criteria = (
        AcceptanceCriterion(
            ident=f"{stem}-AC1",
            given=f"an account with {VALUE_NAMES['PR'][threat_data.breakdown.metrics['PR']].lower()} privileges at {layer.name}",
            when=f"the {vuln.name.lower()} path is exercised",
            then="the response is refused and the attempt is recorded",
            verifies="automated test in the service suite",
        ),
        AcceptanceCriterion(
            ident=f"{stem}-AC2",
            given="the refusal above is in place",
            when="the same request is replayed from a different source address",
            then="the outcome is identical and no timing difference leaks state",
            verifies="automated test plus one manual replay in staging",
        ),
        AcceptanceCriterion(
            ident=f"{stem}-AC3",
            given="the fix is merged",
            when=f"the {CVSS_VERSION} vector is recalculated",
            then=("the impact metrics fall to None for the path that was "
                  "exploitable, evidenced by the recorded vector"),
            verifies="security review sign off against the stored vector",
        ),
    )

    controls = BASE_CONTROLS + CLASS_CONTROLS.get(vuln.key, ())

    headline = (
        f"{vuln.name} at {layer.name}, "
        f"{threat_data.breakdown.score:.1f} {threat_data.band}, "
        f"likelihood {threat_data.exploit_likelihood} "
        f"{threat_data.likelihood_band}"
    )

    return ProductTranslation(
        headline=headline,
        priority=_priority(threat_data),
        stories=stories,
        criteria=criteria,
        controls=controls,
        guardrail=GUARDRAIL,
    )


# ---------------------------------------------------------------------------
# Capability matrix against the OWASP Top 10
# ---------------------------------------------------------------------------

COVERAGE_STRONG = "Covered"
COVERAGE_PARTIAL = "Partial"
COVERAGE_NONE = "Not covered"


@dataclass(frozen=True)
class CapabilityRow:
    category_2025: str
    category_2021: str
    feature: str
    coverage: str
    drift: str
    note: str


# The 2025 edition is not a renumbering of the 2021 edition. Three of these
# rows describe a category boundary that moved, which is why a backlog tagged
# against 2021 is mislabelled rather than merely dated.
DRIFT_SAME = "Same category"
DRIFT_MOVED = "Moved position"
DRIFT_WIDENED = "Scope widened"
DRIFT_ABSORBED = "Absorbed from another category"
DRIFT_NEW = "New in 2025"

CAPABILITY_MATRIX: tuple[CapabilityRow, ...] = (
    CapabilityRow(
        "A01:2025 Broken Access Control",
        "A01:2021 Broken Access Control and "
        "A10:2021 Server-Side Request Forgery (SSRF)",
        "Server side object ownership check on every record read",
        COVERAGE_PARTIAL,
        DRIFT_ABSORBED,
        ("Still first in both editions, and it now also carries what 2021 "
         "listed separately as SSRF, so an SSRF ticket tagged A10 no longer "
         "maps to anything."),
    ),
    CapabilityRow(
        "A02:2025 Security Misconfiguration",
        "A05:2021 Security Misconfiguration",
        "Baseline configuration check in the deploy gate",
        COVERAGE_STRONG,
        DRIFT_MOVED,
        "Moved from fifth to second, which changes which dashboards lead.",
    ),
    CapabilityRow(
        "A03:2025 Software Supply Chain Failures",
        "A06:2021 Vulnerable and Outdated Components",
        "Lockfile advisory scan with reachability triage",
        COVERAGE_PARTIAL,
        DRIFT_WIDENED,
        ("The 2021 category was about the components you ship. The 2025 "
         "category takes in the build systems and the ecosystem around them, "
         "so a dependency scanner alone no longer covers the row."),
    ),
    CapabilityRow(
        "A04:2025 Cryptographic Failures",
        "A02:2021 Cryptographic Failures",
        "Transport and at rest policy enforced at the platform",
        COVERAGE_STRONG,
        DRIFT_MOVED,
        "Same category, moved down two places.",
    ),
    CapabilityRow(
        "A05:2025 Injection",
        "A03:2021 Injection",
        "Parameter binding with identifier allow lists and output encoding",
        COVERAGE_STRONG,
        DRIFT_MOVED,
        "Same category, moved down two places.",
    ),
    CapabilityRow(
        "A06:2025 Insecure Design",
        "A04:2021 Insecure Design",
        "Threat model recorded per service before build",
        COVERAGE_PARTIAL,
        DRIFT_MOVED,
        "The only row a tool cannot close on its own.",
    ),
    CapabilityRow(
        "A07:2025 Authentication Failures",
        "A07:2021 Identification and Authentication Failures",
        "Throttling, uniform responses and second factor on reset",
        COVERAGE_STRONG,
        DRIFT_SAME,
        "Same position, shorter name.",
    ),
    CapabilityRow(
        "A08:2025 Software or Data Integrity Failures",
        "A08:2021 Software and Data Integrity Failures",
        "Signed artefacts verified at deploy",
        COVERAGE_PARTIAL,
        DRIFT_SAME,
        "Same position in both editions.",
    ),
    CapabilityRow(
        "A09:2025 Security Logging and Alerting Failures",
        "A09:2021 Security Logging and Monitoring Failures",
        "Security events routed to an alert with an owner",
        COVERAGE_PARTIAL,
        DRIFT_SAME,
        ("Monitoring became alerting. A dashboard nobody watches used to "
         "count and now reads as the failure the row describes."),
    ),
    CapabilityRow(
        "A10:2025 Mishandling of Exceptional Conditions",
        "No 2021 equivalent",
        "Error path tested to the same standard as the success path",
        COVERAGE_NONE,
        DRIFT_NEW,
        ("There is no 2021 tag to migrate. Every finding for this row has to "
         "be raised new, which is exactly the row a migration script skips."),
    ),
)


def get_security_capability_matrix() -> list[CapabilityRow]:
    return list(CAPABILITY_MATRIX)


def capability_rows() -> list[dict[str, str]]:
    """Arrow safe rows for the matrix."""
    return [{
        f"{OWASP_EDITION} category": row.category_2025,
        f"{PRIOR_OWASP_EDITION} category": row.category_2021,
        "Defensive feature": row.feature,
        "Coverage": row.coverage,
        "Edition drift": row.drift,
    } for row in CAPABILITY_MATRIX]


def coverage_summary() -> dict[str, int]:
    summary = {COVERAGE_STRONG: 0, COVERAGE_PARTIAL: 0, COVERAGE_NONE: 0}
    for row in CAPABILITY_MATRIX:
        summary[row.coverage] += 1
    return summary


def drifted_categories() -> list[CapabilityRow]:
    """Rows a 2021 tagged backlog cannot be mapped across mechanically."""
    return [row for row in CAPABILITY_MATRIX
            if row.drift in (DRIFT_WIDENED, DRIFT_ABSORBED, DRIFT_NEW)]


def ranked_portfolio(layer_key: str) -> list[dict[str, str]]:
    """Every class at one layer, ordered the two competing ways.

    The two orders are printed side by side because they are not the same
    order, and the difference is the point.
    """
    analyses = [analyze_threat_vector(v.key, layer_key) for v in VULN_CLASSES]
    by_score = sorted(analyses, key=lambda a: (-a.score, a.vuln.name))
    by_likelihood = sorted(
        analyses, key=lambda a: (-a.exploit_likelihood, a.vuln.name))
    score_rank = {id(a): i + 1 for i, a in enumerate(by_score)}
    rows: list[dict[str, str]] = []
    for position, analysis in enumerate(by_likelihood, start=1):
        rows.append({
            "Rank by likelihood": str(position),
            "Rank by base score": str(score_rank[id(analysis)]),
            "Vulnerability": analysis.vuln.name,
            "Base score": f"{analysis.score:.1f}",
            "Likelihood": str(analysis.exploit_likelihood),
            "Moves": str(score_rank[id(analysis)] - position),
        })
    return rows
