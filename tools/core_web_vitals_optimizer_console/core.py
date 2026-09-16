"""Core Web Vitals and Lighthouse scoring engine.

No Streamlit import lives here, so the same engine could sit behind a CI
budget check or a reporting job.

The Lighthouse score this module produces is not an approximation of the real
one. The log normal scoring function, the inverse erfc constant, the erf
polynomial and every metric curve below were read from the Lighthouse source
before this file was written, and the two properties the curve is defined by
are asserted in the test suite: a metric exactly at its p10 scores 0.90, and a
metric exactly at its median scores 0.50.

Three things here are deliberately not what a first pass would write.

1. INP is not in the Lighthouse performance score. Not underweighted, not
   approximated by Total Blocking Time for scoring purposes: the Lighthouse
   default config lists interaction-to-next-paint with weight 0. So the number
   everyone screenshots cannot move when the one Core Web Vital that measures
   responsiveness gets worse, and cannot fail when it is terrible. A console
   that reports a score jump without saying this is selling a number that does
   not mean what the buyer thinks it means.

2. Time to First Byte is not a Core Web Vital. It is a diagnostic metric. It
   has weight 0 in the Lighthouse score and it is not part of the Google
   assessment. This matters commercially, because caching is the fix most often
   sold for Core Web Vitals work and TTFB is mostly what caching moves.

3. A Lighthouse run is one lab sample. The Google assessment is the 75th
   percentile of real Chrome users over a rolling 28 day window, and a URL
   passes only when all three of LCP, INP and CLS are Good at that percentile.
   Two of three is a fail. A perfect lab score on a page whose field data is
   Poor is a normal, common outcome rather than a contradiction, so this engine
   reports the lab score and the field assessment as two separate answers and
   raises a finding when they disagree.

Thresholds are the published ones: LCP 2.5s and 4.0s, INP 200ms and 500ms,
CLS 0.1 and 0.25, TTFB 800ms and 1800ms.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

ENGINE_VERSION = "1.0.0"

LIGHTHOUSE_PROFILE = "Lighthouse 12, mobile, simulated throttling"
FIELD_WINDOW_DAYS = 28
FIELD_PERCENTILE = 75

GOOD = "Good"
NEEDS_IMPROVEMENT = "Needs Improvement"
POOR = "Poor"

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_CRIT = "crit"


@dataclass(frozen=True)
class Threshold:
    """One metric's two published boundaries."""

    key: str
    name: str
    unit: str
    good_at_or_below: float
    poor_above: float
    # Only LCP, INP and CLS decide whether a URL passes.
    is_core_web_vital: bool
    note: str

    def status(self, value: float) -> str:
        if value <= self.good_at_or_below:
            return GOOD
        if value <= self.poor_above:
            return NEEDS_IMPROVEMENT
        return POOR


THRESHOLDS: tuple[Threshold, ...] = (
    Threshold(
        key="lcp", name="Largest Contentful Paint", unit="s",
        good_at_or_below=2.5, poor_above=4.0, is_core_web_vital=True,
        note=("When the largest element in the viewport finished rendering. "
              "Carries 25 percent of the Lighthouse score as well."),
    ),
    Threshold(
        key="inp", name="Interaction to Next Paint", unit="ms",
        good_at_or_below=200.0, poor_above=500.0, is_core_web_vital=True,
        note=("The responsiveness Core Web Vital, and the one with weight 0 in "
              "the Lighthouse performance score."),
    ),
    Threshold(
        key="cls", name="Cumulative Layout Shift", unit="",
        good_at_or_below=0.1, poor_above=0.25, is_core_web_vital=True,
        note=("Unitless. Carries 25 percent of the Lighthouse score, and it is "
              "the vital a careless font fix makes worse."),
    ),
    Threshold(
        key="ttfb", name="Time to First Byte", unit="ms",
        good_at_or_below=800.0, poor_above=1800.0, is_core_web_vital=False,
        note=("A diagnostic metric, not a Core Web Vital. It does not appear "
              "in the Google assessment and has weight 0 in the score."),
    ),
)

THRESHOLD_BY_KEY = {t.key: t for t in THRESHOLDS}
CORE_WEB_VITALS = tuple(t.key for t in THRESHOLDS if t.is_core_web_vital)


# ---------------------------------------------------------------------------
# The real Lighthouse scoring curve
# ---------------------------------------------------------------------------

# Read from the Lighthouse source rather than approximated. The constant is
# the closest double to erfc inverse of one fifth, and the polynomial is the
# Abramowitz and Stegun erf approximation Lighthouse ships.
INVERSE_ERFC_ONE_FIFTH = 0.9061938024368232

_ERF_A = (0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429)
_ERF_P = 0.3275911


def erf(x: float) -> float:
    """The error function approximation Lighthouse uses, reproduced exactly.

    math.erf would be more accurate and would disagree with Lighthouse in the
    last digits. Agreeing with the tool people screenshot matters more here
    than agreeing with the true value.
    """
    sign = 1.0 if x > 0 else (-1.0 if x < 0 else 0.0)
    x = abs(x)
    a1, a2, a3, a4, a5 = _ERF_A
    t = 1 / (1 + _ERF_P * x)
    y = t * (a1 + t * (a2 + t * (a3 + t * (a4 + t * a5))))
    return sign * (1 - y * math.exp(-x * x))


def log_normal_score(value: float, p10: float, median: float) -> float:
    """Lighthouse's metric score, from 0 to 1.

    The curve is defined by two points: a value at p10 scores 0.90 and a value
    at the median scores 0.50. Everything between is the complementary log
    normal cumulative distribution.
    """
    if median <= 0:
        raise ValueError("median must be greater than zero")
    if p10 <= 0:
        raise ValueError("p10 must be greater than zero")
    if p10 >= median:
        raise ValueError("p10 must be less than the median")
    if value <= 0:
        return 1.0

    x_log_ratio = math.log(value / median)
    p10_log_ratio = -math.log(p10 / median)
    standardized = x_log_ratio * INVERSE_ERFC_ONE_FIFTH / p10_log_ratio
    complementary = (1 - erf(standardized)) / 2
    return max(0.0, min(1.0, complementary))


@dataclass(frozen=True)
class LabMetric:
    """One of the five metrics the Lighthouse performance score is made of."""

    key: str
    name: str
    acronym: str
    unit: str
    weight: int
    p10: float
    median: float


# Mobile curves and category weights, both from the Lighthouse source.
LAB_METRICS: tuple[LabMetric, ...] = (
    LabMetric("fcp", "First Contentful Paint", "FCP", "ms", 10, 1800, 3000),
    LabMetric("si", "Speed Index", "SI", "ms", 10, 3387, 5800),
    LabMetric("lcp", "Largest Contentful Paint", "LCP", "ms", 25, 2500, 4000),
    LabMetric("tbt", "Total Blocking Time", "TBT", "ms", 30, 200, 600),
    LabMetric("cls", "Cumulative Layout Shift", "CLS", "", 25, 0.1, 0.25),
)

LAB_METRIC_BY_KEY = {m.key: m for m in LAB_METRICS}
TOTAL_WEIGHT = sum(m.weight for m in LAB_METRICS)

# Lighthouse reports these and scores none of them.
UNWEIGHTED_METRICS: Mapping[str, str] = {
    "inp": "Interaction to Next Paint",
    "ttfb": "Time to First Byte",
}


def lighthouse_score(metrics: Mapping[str, float]) -> int:
    """The Lighthouse mobile performance score, 0 to 100.

    metrics takes fcp, si, lcp and tbt in milliseconds and cls unitless.
    """
    missing = [m.key for m in LAB_METRICS if m.key not in metrics]
    if missing:
        raise ValueError(f"missing lab metrics: {', '.join(missing)}")
    total = 0.0
    for metric in LAB_METRICS:
        total += (log_normal_score(metrics[metric.key], metric.p10,
                                   metric.median) * metric.weight)
    # Lighthouse rounds the weighted average to the nearest whole point.
    return round(total / TOTAL_WEIGHT * 100)


def metric_score_rows(metrics: Mapping[str, float]) -> list[dict[str, str]]:
    """Arrow safe: what each metric contributed, and what it could have."""
    rows: list[dict[str, str]] = []
    for metric in LAB_METRICS:
        raw = log_normal_score(metrics[metric.key], metric.p10, metric.median)
        rows.append({
            "Metric": f"{metric.name} ({metric.acronym})",
            "Value": (f"{metrics[metric.key]:,.0f}{metric.unit}"
                      if metric.unit else f"{metrics[metric.key]:.3f}"),
            "Metric score": f"{raw * 100:.0f}",
            "Weight": f"{metric.weight}%",
            "Points contributed": f"{raw * metric.weight:.1f}",
            "Points available": f"{metric.weight:.1f}",
        })
    for key, name in UNWEIGHTED_METRICS.items():
        value = metrics.get(key)
        rows.append({
            "Metric": f"{name} (not scored)",
            "Value": "not supplied" if value is None else f"{value:,.0f}ms",
            "Metric score": "not scored",
            "Weight": "0%",
            "Points contributed": "0.0",
            "Points available": "0.0",
        })
    return rows


# ---------------------------------------------------------------------------
# Evaluating the vitals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricVerdict:
    key: str
    name: str
    value: float
    display: str
    status: str
    is_core_web_vital: bool
    good_at_or_below: str
    distance: str


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


@dataclass(frozen=True)
class VitalsAssessment:
    verdicts: Sequence[MetricVerdict]
    passes_core_web_vitals: bool
    worst_status: str
    findings: Sequence[Finding] = field(default_factory=tuple)

    @property
    def seo_indicator(self) -> str:
        return "PASS" if self.passes_core_web_vitals else "FAIL"

    def by_key(self, key: str) -> MetricVerdict:
        return next(v for v in self.verdicts if v.key == key)

    def rows(self) -> list[dict[str, str]]:
        return [{
            "Metric": v.name,
            "Value": v.display,
            "Status": v.status,
            "Good at or below": v.good_at_or_below,
            "Counts toward the assessment": "yes" if v.is_core_web_vital
                                            else "no, diagnostic only",
            "Distance from Good": v.distance,
        } for v in self.verdicts]


_STATUS_RANK = {GOOD: 0, NEEDS_IMPROVEMENT: 1, POOR: 2}


def _display(threshold: Threshold, value: float) -> str:
    if threshold.key == "cls":
        return f"{value:.3f}"
    if threshold.unit == "s":
        return f"{value:.2f} s"
    return f"{value:,.0f} ms"


def _distance(threshold: Threshold, value: float) -> str:
    if value <= threshold.good_at_or_below:
        return "already Good"
    gap = value - threshold.good_at_or_below
    if threshold.key == "cls":
        return f"{gap:.3f} over"
    if threshold.unit == "s":
        return f"{gap:.2f} s over"
    return f"{gap:,.0f} ms over"


def evaluate_web_vitals(lcp_sec: float, inp_ms: float, cls_score: float,
                        ttfb_ms: float) -> VitalsAssessment:
    """Grade the four metrics and say whether the URL passes.

    The pass is decided by LCP, INP and CLS only, and it needs all three to be
    Good. TTFB is graded because it is worth knowing and excluded from the
    verdict because Google excludes it.
    """
    values = {"lcp": float(lcp_sec), "inp": float(inp_ms),
              "cls": float(cls_score), "ttfb": float(ttfb_ms)}
    for key, value in values.items():
        if value < 0:
            raise ValueError(
                f"{THRESHOLD_BY_KEY[key].name} cannot be negative, got {value}")

    verdicts: list[MetricVerdict] = []
    for threshold in THRESHOLDS:
        value = values[threshold.key]
        verdicts.append(MetricVerdict(
            key=threshold.key, name=threshold.name, value=value,
            display=_display(threshold, value),
            status=threshold.status(value),
            is_core_web_vital=threshold.is_core_web_vital,
            good_at_or_below=_display(threshold, threshold.good_at_or_below),
            distance=_distance(threshold, value),
        ))

    core = [v for v in verdicts if v.is_core_web_vital]
    passes = all(v.status == GOOD for v in core)
    worst = max((v.status for v in core), key=lambda s: _STATUS_RANK[s])

    assessment = VitalsAssessment(verdicts=tuple(verdicts),
                                  passes_core_web_vitals=passes,
                                  worst_status=worst)
    return VitalsAssessment(verdicts=tuple(verdicts),
                            passes_core_web_vitals=passes, worst_status=worst,
                            findings=tuple(audit_vitals(assessment)))


def audit_vitals(assessment: VitalsAssessment) -> list[Finding]:
    findings: list[Finding] = []
    core = [v for v in assessment.verdicts if v.is_core_web_vital]
    failing = [v for v in core if v.status != GOOD]
    ttfb = assessment.by_key("ttfb")

    if len(failing) == 1:
        one = failing[0]
        findings.append(Finding(
            code="CWV-ONE-AWAY",
            severity=SEVERITY_CRIT,
            title=f"Two of three are Good and the URL still fails",
            detail=(
                f"{one.name} is {one.status} at {one.display}, "
                f"{one.distance}. The assessment needs all three Core Web "
                f"Vitals Good at the {FIELD_PERCENTILE}th percentile. There is "
                f"no partial credit and no averaging."
            ),
            fix=(f"Everything that does not move {one.name} is the wrong work "
                 f"this week, however good it looks on a report."),
        ))
    elif failing:
        findings.append(Finding(
            code="CWV-MULTI",
            severity=SEVERITY_CRIT,
            title=f"{len(failing)} of 3 Core Web Vitals are not Good",
            detail=("Not Good: " + ", ".join(
                f"{v.name} at {v.display} ({v.status})" for v in failing)
                + f". All three must be Good at the {FIELD_PERCENTILE}th "
                f"percentile for the URL to pass."),
            fix=("Order the work by which metric is furthest from its "
                 "threshold, not by which fix is easiest to sell."),
        ))

    inp = assessment.by_key("inp")
    if inp.status != GOOD:
        findings.append(Finding(
            code="CWV-INP-UNSCORED",
            severity=SEVERITY_CRIT,
            title="The metric that is failing is the one the score cannot see",
            detail=(
                f"Interaction to Next Paint is {inp.status} at {inp.display}. "
                f"The Lighthouse default configuration lists "
                f"interaction-to-next-paint with weight 0, so this failure "
                f"contributes nothing to the performance score. A page can "
                f"score 100 and fail the assessment on this metric alone."
            ),
            fix=("Measure INP in the field, from real interactions. Total "
                 "Blocking Time is the lab proxy and it is a proxy, not the "
                 "metric being assessed."),
        ))

    if ttfb.status != GOOD:
        findings.append(Finding(
            code="CWV-TTFB-DIAGNOSTIC",
            severity=SEVERITY_WARN,
            title=f"Time to First Byte is {ttfb.status} and does not count",
            detail=(
                f"TTFB is {ttfb.display}. It is a diagnostic metric: it is not "
                f"one of the three Core Web Vitals, it is not in the Google "
                f"assessment, and it has weight 0 in the Lighthouse score. It "
                f"still delays everything downstream of it, which is why it is "
                f"worth fixing and not worth reporting as a vital."
            ),
            fix=("Fix it for the LCP time it buys back, and report it as a "
                 "cause rather than as a result."),
        ))

    if not failing:
        findings.append(Finding(
            code="CWV-PASS",
            severity=SEVERITY_OK,
            title="All three Core Web Vitals are Good",
            detail=(
                f"LCP {assessment.by_key('lcp').display}, INP "
                f"{inp.display}, CLS {assessment.by_key('cls').display}. If "
                f"these are field values at the {FIELD_PERCENTILE}th "
                f"percentile over {FIELD_WINDOW_DAYS} days then the URL "
                f"passes. If they came from a single lab run they are one "
                f"sample and prove nothing about the assessment."
            ),
            fix="Confirm the numbers are field data before reporting a pass.",
        ))

    findings.append(Finding(
        code="CWV-FIELD-ONLY",
        severity=SEVERITY_WARN,
        title="The assessment is field data, not a lab run",
        detail=(
            f"Google assesses the {FIELD_PERCENTILE}th percentile of real "
            f"Chrome users over a rolling {FIELD_WINDOW_DAYS} day window. A "
            f"Lighthouse run is one visit, on one connection, on one device. "
            f"It is useful for finding causes and it cannot decide a pass."
        ),
        fix=("Quote the Chrome UX Report figures when the question is whether "
             "the URL passes, and Lighthouse when the question is why."),
    ))
    return findings


def threshold_rows() -> list[dict[str, str]]:
    return [{
        "Metric": t.name,
        "Good": (f"{t.good_at_or_below:.3f}" if t.key == "cls"
                 else f"{t.good_at_or_below:,.2f} {t.unit}".strip()),
        "Poor above": (f"{t.poor_above:.3f}" if t.key == "cls"
                       else f"{t.poor_above:,.2f} {t.unit}".strip()),
        "Core Web Vital": "yes" if t.is_core_web_vital else "no",
        "Lighthouse weight": (
            f"{LAB_METRIC_BY_KEY[t.key].weight}%"
            if t.key in LAB_METRIC_BY_KEY else "0%"),
        "Note": t.note,
    } for t in THRESHOLDS]


# ---------------------------------------------------------------------------
# Before and after
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SiteProfile:
    """A page's lab metrics plus the field values the lab cannot produce."""

    label: str
    fcp: float
    si: float
    lcp: float
    tbt: float
    cls: float
    inp: float
    ttfb: float
    js_bytes: int
    css_bytes: int
    font_bytes: int
    image_bytes: int
    main_thread_ms: int

    def lab_metrics(self) -> dict[str, float]:
        return {"fcp": self.fcp, "si": self.si, "lcp": self.lcp,
                "tbt": self.tbt, "cls": self.cls}

    @property
    def total_bytes(self) -> int:
        return (self.js_bytes + self.css_bytes + self.font_bytes
                + self.image_bytes)

    @property
    def score(self) -> int:
        return lighthouse_score(self.lab_metrics())

    def assessment(self) -> VitalsAssessment:
        # The field assessment reads LCP in seconds.
        return evaluate_web_vitals(self.lcp / 1000, self.inp, self.cls,
                                   self.ttfb)


# A typical unoptimised WordPress page: a page builder, three sliders, a
# webfont stack and a plugin set nobody has audited. The numbers are a
# plausible worked example, and the score is computed from them rather than
# chosen.
BEFORE = SiteProfile(
    label="Before",
    fcp=3500, si=7000, lcp=4750, tbt=710, cls=0.22,
    inp=640, ttfb=1900,
    js_bytes=1_850_000, css_bytes=640_000, font_bytes=480_000,
    image_bytes=3_200_000, main_thread_ms=6800,
)

# After the four fixes in get_wordpress_remediation_steps. INP improves and is
# deliberately left short of Good, because deferring scripts reduces long tasks
# without removing the handlers that run on interaction.
AFTER = SiteProfile(
    label="After",
    fcp=1250, si=2300, lcp=2000, tbt=125, cls=0.03,
    inp=260, ttfb=420,
    js_bytes=420_000, css_bytes=88_000, font_bytes=190_000,
    image_bytes=760_000, main_thread_ms=1450,
)

# The same work with the font fix applied carelessly: display swap without a
# size adjusted fallback trades a faster paint for a layout shift.
AFTER_CARELESS_FONT = SiteProfile(
    label="After, font swapped without a matched fallback",
    fcp=1250, si=2300, lcp=2000, tbt=125, cls=0.21,
    inp=260, ttfb=420,
    js_bytes=420_000, css_bytes=88_000, font_bytes=190_000,
    image_bytes=760_000, main_thread_ms=1450,
)


@dataclass(frozen=True)
class ScoreJump:
    before: SiteProfile
    after: SiteProfile
    before_score: int
    after_score: int
    before_passes: bool
    after_passes: bool
    bytes_saved: int
    main_thread_saved_ms: int
    findings: Sequence[Finding] = field(default_factory=tuple)

    @property
    def points_gained(self) -> int:
        return self.after_score - self.before_score

    @property
    def lab_field_gap(self) -> bool:
        """The score says shipped and the assessment says not yet."""
        return self.after_score >= 90 and not self.after_passes

    def metric_rows(self) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for metric in LAB_METRICS:
            b = self.before.lab_metrics()[metric.key]
            a = self.after.lab_metrics()[metric.key]
            fmt = (lambda v: f"{v:.3f}") if metric.key == "cls" \
                else (lambda v: f"{v:,.0f} ms")
            rows.append({
                "Metric": f"{metric.name} ({metric.acronym})",
                "Before": fmt(b),
                "After": fmt(a),
                "Weight": f"{metric.weight}%",
                "Score before": f"{log_normal_score(b, metric.p10, metric.median) * 100:.0f}",
                "Score after": f"{log_normal_score(a, metric.p10, metric.median) * 100:.0f}",
            })
        for key, name in UNWEIGHTED_METRICS.items():
            b = getattr(self.before, key)
            a = getattr(self.after, key)
            rows.append({
                "Metric": f"{name} (weight 0)",
                "Before": f"{b:,.0f} ms",
                "After": f"{a:,.0f} ms",
                "Weight": "0%",
                "Score before": "not scored",
                "Score after": "not scored",
            })
        return rows

    def payload_rows(self) -> list[dict[str, str]]:
        pairs = (("JavaScript", "js_bytes"), ("CSS", "css_bytes"),
                 ("Fonts", "font_bytes"), ("Images", "image_bytes"))
        rows: list[dict[str, str]] = []
        for label, attr in pairs:
            before = getattr(self.before, attr)
            after = getattr(self.after, attr)
            rows.append({
                "Asset": label,
                "Before": f"{before / 1000:,.0f} kB",
                "After": f"{after / 1000:,.0f} kB",
                "Saved": f"{(before - after) / 1000:,.0f} kB",
            })
        rows.append({
            "Asset": "Total",
            "Before": f"{self.before.total_bytes / 1000:,.0f} kB",
            "After": f"{self.after.total_bytes / 1000:,.0f} kB",
            "Saved": f"{self.bytes_saved / 1000:,.0f} kB",
        })
        return rows


def simulate_score_jump(optimization_applied: bool = True,
                        after: SiteProfile | None = None) -> ScoreJump:
    """Before against after, with the lab score and the field verdict kept apart.

    optimization_applied False returns the before state on both sides, which is
    the honest answer to "what did we gain" when nothing shipped.
    """
    end = (after or AFTER) if optimization_applied else BEFORE
    jump = ScoreJump(
        before=BEFORE, after=end,
        before_score=BEFORE.score, after_score=end.score,
        before_passes=BEFORE.assessment().passes_core_web_vitals,
        after_passes=end.assessment().passes_core_web_vitals,
        bytes_saved=BEFORE.total_bytes - end.total_bytes,
        main_thread_saved_ms=BEFORE.main_thread_ms - end.main_thread_ms,
    )
    return ScoreJump(
        before=jump.before, after=jump.after,
        before_score=jump.before_score, after_score=jump.after_score,
        before_passes=jump.before_passes, after_passes=jump.after_passes,
        bytes_saved=jump.bytes_saved,
        main_thread_saved_ms=jump.main_thread_saved_ms,
        findings=tuple(audit_jump(jump)),
    )


def audit_jump(jump: ScoreJump) -> list[Finding]:
    findings: list[Finding] = []

    if jump.points_gained == 0:
        findings.append(Finding(
            code="CWV-NO-CHANGE",
            severity=SEVERITY_WARN,
            title="Nothing was applied, so nothing moved",
            detail=("The before and after are the same page. The score did not "
                    "change because no work shipped."),
            fix="Apply the remediation steps, then measure again.",
        ))
        return findings

    if jump.lab_field_gap:
        after_inp = jump.after.assessment().by_key("inp")
        findings.append(Finding(
            code="CWV-LAB-FIELD-GAP",
            severity=SEVERITY_CRIT,
            title=f"The score reads {jump.after_score} and the URL still fails",
            detail=(
                f"The Lighthouse score went {jump.before_score} to "
                f"{jump.after_score}, which is the number a report leads with. "
                f"Interaction to Next Paint is {after_inp.display}, which is "
                f"{after_inp.status}, so the assessment fails. INP has weight 0 "
                f"in the score, so the two numbers are free to disagree and "
                f"this one does."
            ),
            fix=("Report both. A client told the score passed and the "
                 "assessment failed will hear about it from Search Console."),
        ))

    after_cls = jump.after.assessment().by_key("cls")
    before_cls = jump.before.assessment().by_key("cls")
    if after_cls.status != GOOD and before_cls.status != GOOD:
        findings.append(Finding(
            code="CWV-CLS-TRADE",
            severity=SEVERITY_CRIT,
            title="The font fix bought paint time and spent layout stability",
            detail=(
                f"Cumulative Layout Shift is {after_cls.display} after the "
                f"work, which is {after_cls.status}. font-display swap paints "
                f"fallback text immediately and then reflows when the webfont "
                f"arrives. Faster First Contentful Paint, worse CLS, and CLS "
                f"is the Core Web Vital."
            ),
            fix=("Keep swap and add size-adjust, ascent-override and "
                 "descent-override on a matched local fallback so the swap "
                 "does not change metrics."),
        ))

    if jump.after_passes and not jump.before_passes:
        findings.append(Finding(
            code="CWV-PASSED",
            severity=SEVERITY_OK,
            title="All three Core Web Vitals moved to Good",
            detail=(
                f"Score {jump.before_score} to {jump.after_score}, "
                f"{jump.bytes_saved / 1000:,.0f} kB less to download and "
                f"{jump.main_thread_saved_ms:,} ms less main thread work. The "
                f"assessment passes on these values."
            ),
            fix=(f"The field figures follow over the next "
                 f"{FIELD_WINDOW_DAYS} days as the window rolls, not on the "
                 f"day of the deploy."),
        ))

    findings.append(Finding(
        code="CWV-WINDOW",
        severity=SEVERITY_WARN,
        title=f"The field data lags the deploy by up to {FIELD_WINDOW_DAYS} days",
        detail=(
            f"The Chrome UX Report window is {FIELD_WINDOW_DAYS} days long. "
            f"The day after a fix ships, {FIELD_WINDOW_DAYS - 1} of those days "
            f"are still the old page. Search Console will keep saying the URL "
            f"is Poor for weeks after the work is genuinely done."
        ),
        fix=("Say this before the invoice rather than after the first "
             "impatient email."),
    ))
    return findings


# ---------------------------------------------------------------------------
# WordPress remediation
# ---------------------------------------------------------------------------

EFFORT_LOW = "Low"
EFFORT_MEDIUM = "Medium"
EFFORT_HIGH = "High"


@dataclass(frozen=True)
class RemediationStep:
    rank: int
    title: str
    action: str
    moves: tuple[str, ...]
    does_not_move: tuple[str, ...]
    effort: str
    risk: str
    wordpress_note: str


# Ordered by points per hour on a typical page builder site, not by how
# familiar the fix is.
WORDPRESS_STEPS: tuple[RemediationStep, ...] = (
    RemediationStep(
        rank=1,
        title="Remove render blocking CSS and JavaScript from the head",
        action=("Inline the critical CSS for the viewport, defer the rest with "
                "a print media swap, and move every script that is not needed "
                "for first paint to defer or async."),
        moves=("FCP", "LCP", "Speed Index"),
        does_not_move=("INP", "CLS"),
        effort=EFFORT_MEDIUM,
        risk=("A wrongly deferred script that other inline code calls will "
              "throw. Defer preserves order, async does not."),
        wordpress_note=("Page builders enqueue their whole stylesheet on every "
                        "page. Dequeue per template with wp_dequeue_style "
                        "before inlining anything."),
    ),
    RemediationStep(
        rank=2,
        title="Cut the JavaScript that runs on every page",
        action=("Audit the plugin set, dequeue the scripts each plugin loads "
                "site wide, and load them only on the templates that use them."),
        moves=("TBT", "INP", "Speed Index"),
        does_not_move=("CLS", "TTFB"),
        effort=EFFORT_HIGH,
        risk="Dequeuing a dependency silently breaks whatever depended on it.",
        wordpress_note=("This is the only step here that moves INP, and INP is "
                        "the vital the Lighthouse score cannot see. It is also "
                        "the step most often skipped because it is the least "
                        "visible on a report."),
    ),
    RemediationStep(
        rank=3,
        title="Strip unused CSS, then serve what is left per template",
        action=("Generate used selectors per template rather than one global "
                "sheet, and keep the critical set under the initial congestion "
                "window."),
        moves=("FCP", "LCP"),
        does_not_move=("INP", "TBT"),
        effort=EFFORT_MEDIUM,
        risk=("Selectors added by JavaScript after load are not in the static "
              "analysis and get stripped. Safelist them explicitly."),
        wordpress_note=("A page builder ships one sheet for every module it "
                        "can render. A single page uses a small fraction of it."),
    ),
    RemediationStep(
        rank=4,
        title="font-display swap with a size adjusted fallback",
        action=("Set font-display: swap, self host the files, preload the one "
                "face used above the fold, and define a local fallback with "
                "size-adjust, ascent-override and descent-override so the swap "
                "changes no metrics."),
        moves=("FCP", "LCP"),
        does_not_move=("INP", "TTFB"),
        effort=EFFORT_LOW,
        risk=("swap without the matched fallback reflows the page when the "
              "webfont arrives and pushes CLS the wrong way. This is the fix "
              "that most often makes a Core Web Vital worse."),
        wordpress_note=("Themes that load fonts from a third party also cost a "
                        "connection setup. Self hosting removes it."),
    ),
    RemediationStep(
        rank=5,
        title="Full page caching and an origin close to the visitor",
        action=("Serve cached HTML from the edge, set a long immutable cache "
                "policy on fingerprinted assets, and keep the database out of "
                "the request path for anonymous visitors."),
        moves=("TTFB", "FCP", "LCP"),
        does_not_move=("INP", "CLS", "TBT"),
        effort=EFFORT_LOW,
        risk=("Caching a logged in view, or a cart, serves one visitor's page "
              "to another."),
        wordpress_note=("This is the fix most often sold as Core Web Vitals "
                        "work. TTFB is not a Core Web Vital and has weight 0 "
                        "in the score. It earns its place by the LCP time it "
                        "buys back, not on its own."),
    ),
)


def get_wordpress_remediation_steps() -> list[RemediationStep]:
    return list(WORDPRESS_STEPS)


def remediation_rows() -> list[dict[str, str]]:
    return [{
        "Priority": str(s.rank),
        "Fix": s.title,
        "Moves": ", ".join(s.moves),
        "Does not move": ", ".join(s.does_not_move),
        "Effort": s.effort,
        "Risk": s.risk,
    } for s in WORDPRESS_STEPS]


def steps_that_move(metric_acronym: str) -> list[RemediationStep]:
    return [s for s in WORDPRESS_STEPS if metric_acronym in s.moves]


def score_sensitivity() -> list[dict[str, str]]:
    """What each lab metric is worth on the before page, all else held.

    Answers the question a client actually asks: if we only had budget for one
    thing, which one moves the number.
    """
    base = BEFORE.lab_metrics()
    baseline = lighthouse_score(base)
    rows: list[dict[str, str]] = []
    for metric in LAB_METRICS:
        perfected = dict(base)
        perfected[metric.key] = metric.p10
        rows.append({
            "Metric": f"{metric.name} ({metric.acronym})",
            "Weight": f"{metric.weight}%",
            "Score if this alone reached its Good boundary":
                str(lighthouse_score(perfected)),
            "Points gained": f"+{lighthouse_score(perfected) - baseline}",
        })
    rows.sort(key=lambda r: -int(r["Points gained"].lstrip("+")))
    return rows
