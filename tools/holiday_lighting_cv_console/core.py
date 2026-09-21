"""Holiday lighting computer vision geometry engine.

No Streamlit import lives in this file, and no numpy either, so the same
maths could run inside a worker behind a real SAM3 call.

Each of the three stages is written around the failure that puts a
installer on a ladder in the wrong place:

* a segmentation mask of a roof contains the ridge, both rakes, and the
  eave, and only one of those carries lights. Taking the lowest points of
  the mask is not the same as isolating the eave, so the extraction
  classifies every edge and says what it dropped;
* a tree in front of the house removes a run of edge pixels and adds a
  run of branch pixels that look like edge. Ordinary least squares is
  dragged by both. RANSAC is not, which is the entire reason it is here,
  and this file proves that rather than asserting it;
* a bulb run has to land exactly on both ends of the eave. Spacing by
  repeated addition of a pitch leaves a remainder gap at one end, so the
  pitch is solved for instead, and it is solved downward so no gap is ever
  wider than the one that was specified.

Every coordinate here is in pixels. A single photograph carries no scale,
so nothing in this file converts pixels to feet without being handed a
measured reference, and it refuses rather than guessing.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Sequence

ENGINE_VERSION = "1.0.0"

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"

_SEVERITY_ORDER = {SEVERITY_OK: 0, SEVERITY_WARN: 1, SEVERITY_CRITICAL: 2}

Point = tuple[float, float]


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


def _as_points(raw) -> tuple[Point, ...]:
    points: list[Point] = []
    for item in raw or ():
        x, y = item
        points.append((float(x), float(y)))
    return tuple(points)


# ---------------------------------------------------------------------------
# 1. Bottom eave extraction from a raw SAM3 polygon
# ---------------------------------------------------------------------------

EDGE_EAVE = "eave"
EDGE_RAKE = "rake"
EDGE_RIDGE = "ridge"
EDGE_VERTICAL = "vertical"

EXTRACTED = "Bottom eave isolated"
REFUSED = "Refused, no eave run survived"

# Image coordinates: y increases downward, so the eave has the LARGER y.
RAKE_SLOPE_TOLERANCE = 0.30
BAND_FRACTION = 0.25


@dataclass(frozen=True)
class EaveExtraction:
    polygon: tuple[Point, ...]
    points: tuple[Point, ...]
    dropped_rake: int
    dropped_ridge: int
    dropped_vertical: int
    edges_in: int
    span_px: float
    mean_slope: float
    status: str
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def extracted(self) -> bool:
        return self.status == EXTRACTED

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def edges_kept(self) -> int:
        return max(0, len(self.points) - 1)


def classify_edge(start: Point, end: Point, y_peak: float, y_base: float,
                  slope_tolerance: float = RAKE_SLOPE_TOLERANCE,
                  band_fraction: float = BAND_FRACTION) -> str:
    """Name one polygon edge as ridge, rake, wall, or eave."""
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if abs(dx) < 1e-9:
        return EDGE_VERTICAL
    slope = abs(dy / dx)
    if slope > slope_tolerance:
        return EDGE_RAKE
    height = max(y_base - y_peak, 1e-9)
    band = height * band_fraction
    mid_y = (start[1] + end[1]) / 2.0
    if mid_y <= y_peak + band:
        return EDGE_RIDGE
    if mid_y >= y_base - band:
        return EDGE_EAVE
    return EDGE_RIDGE


def extract_bottom_eave(polygon_points,
                        slope_tolerance: float = RAKE_SLOPE_TOLERANCE,
                        band_fraction: float = BAND_FRACTION
                        ) -> EaveExtraction:
    """Drop the peak and the rakes, keep the run that carries the lights.

    A SAM3 mask of a roof is one closed polygon holding four kinds of edge.
    Taking the lowest vertices is not the same as isolating the eave: the
    two rake edges both reach the eave height at their lower ends, so a
    lowest points filter keeps their tips and the fitted line is dragged
    upward at both corners. Each edge is classified and the rakes are
    dropped whole.
    """
    polygon = _as_points(polygon_points)
    findings: list[Finding] = []

    if len(polygon) < 3:
        return EaveExtraction(
            polygon=polygon, points=(), dropped_rake=0, dropped_ridge=0,
            dropped_vertical=0, edges_in=0, span_px=0.0, mean_slope=0.0,
            status=REFUSED,
            headline="Refused: a polygon needs at least three vertices",
            findings=(Finding(
                code="EAV-POLY", severity=SEVERITY_CRITICAL,
                title=f"{len(polygon)} vertices is not a polygon",
                detail=("A mask this small is a failed segmentation, not a "
                        "thin roof. Fitting a line to it would produce a "
                        "confident answer from nothing."),
                fix="Re prompt SAM3 on the roof rather than on the house."),))

    y_peak = min(p[1] for p in polygon)
    y_base = max(p[1] for p in polygon)

    edges = list(zip(polygon, polygon[1:] + polygon[:1]))
    kept: list[tuple[Point, Point]] = []
    counts = {EDGE_RAKE: 0, EDGE_RIDGE: 0, EDGE_VERTICAL: 0, EDGE_EAVE: 0}

    for start, end in edges:
        kind = classify_edge(start, end, y_peak, y_base, slope_tolerance,
                             band_fraction)
        counts[kind] += 1
        if kind == EDGE_EAVE:
            kept.append((start, end))

    if not kept:
        findings.append(Finding(
            code="EAV-NONE", severity=SEVERITY_CRITICAL,
            title="No near horizontal run survived at the base of the mask",
            detail=("Every edge was a rake, a wall, or sat in the ridge "
                    "band. That is what a mask of a steeply angled view "
                    "looks like, and the eave in it is not recoverable "
                    "without rectifying the image first."),
            fix=("Reshoot square to the facade, or rectify with four known "
                 "points before segmenting.")))
        return EaveExtraction(
            polygon=polygon, points=(), dropped_rake=counts[EDGE_RAKE],
            dropped_ridge=counts[EDGE_RIDGE],
            dropped_vertical=counts[EDGE_VERTICAL], edges_in=len(edges),
            span_px=0.0, mean_slope=0.0, status=REFUSED,
            headline="Refused: no eave run survived", findings=tuple(findings))

    ordered: list[Point] = []
    for start, end in kept:
        if not ordered or ordered[-1] != start:
            ordered.append(start)
        ordered.append(end)
    ordered.sort(key=lambda p: p[0])
    points = tuple(dict.fromkeys(ordered))

    span = points[-1][0] - points[0][0]
    rise = points[-1][1] - points[0][1]
    mean_slope = rise / span if abs(span) > 1e-9 else 0.0

    polygon_width = (max(p[0] for p in polygon)
                     - min(p[0] for p in polygon))
    coverage = abs(span) / polygon_width if polygon_width > 1e-9 else 0.0

    findings.append(Finding(
        code="EAV-DROP", severity=SEVERITY_OK,
        title=(f"Dropped {counts[EDGE_RAKE]} rake, {counts[EDGE_RIDGE]} "
               f"ridge, and {counts[EDGE_VERTICAL]} wall edge(s)"),
        detail=("The rakes are dropped whole rather than trimmed. Their "
                "lower tips sit at eave height, so a filter that keeps the "
                "lowest pixels keeps those tips and pulls the fitted line "
                "up at both corners."),
        fix="Compare the kept run against the mask outline before fitting."))

    if coverage < 0.6:
        findings.append(Finding(
            code="EAV-SHORT", severity=SEVERITY_WARN,
            title=(f"The eave run covers {coverage * 100:.0f}% of the mask "
                   f"width"),
            detail=("Either the mask is of a hip roof whose eave genuinely "
                    "stops short, or something in front of the house cut "
                    "the mask. Those need opposite responses, and a pixel "
                    "cannot tell you which."),
            fix="Look at the photograph before trusting the run length."))

    if abs(mean_slope) > 0.05:
        findings.append(Finding(
            code="EAV-TILT", severity=SEVERITY_WARN,
            title=f"The extracted eave slopes at {mean_slope:+.3f} per pixel",
            detail=("A real eave is level. A sloped one in the image means "
                    "the camera was not square to the facade, so every "
                    "pixel distance along this run maps to a different real "
                    "distance."),
            fix=("Rectify before measuring, or accept that the spacing is "
                 "uniform in pixels and not on the house.")))

    findings.append(Finding(
        code="EAV-SCALE", severity=SEVERITY_WARN,
        title="This run is in pixels and carries no scale",
        detail=("A single photograph has no metric reference in it. The "
                "span below is a pixel count, and turning it into feet of "
                "lights requires a measured length in the same image."),
        fix=("Photograph a known length, a door or a tape, in the same "
             "frame and plane as the eave.")))

    return EaveExtraction(
        polygon=polygon, points=points, dropped_rake=counts[EDGE_RAKE],
        dropped_ridge=counts[EDGE_RIDGE],
        dropped_vertical=counts[EDGE_VERTICAL], edges_in=len(edges),
        span_px=span, mean_slope=mean_slope, status=EXTRACTED,
        headline=f"Eave isolated: {span:.0f} px across {len(points)} vertex "
                 f"point(s)",
        findings=tuple(findings))


SAMPLE_GABLE: tuple[Point, ...] = (
    (120.0, 300.0), (400.0, 150.0), (680.0, 300.0),
    (680.0, 330.0), (120.0, 330.0),
)

SAMPLE_HIP: tuple[Point, ...] = (
    (120.0, 320.0), (300.0, 190.0), (500.0, 190.0), (680.0, 320.0),
    (680.0, 350.0), (120.0, 350.0),
)


# ---------------------------------------------------------------------------
# 2. RANSAC roofline fitting across an occlusion
# ---------------------------------------------------------------------------

RANSAC_ITERATIONS = 400
INLIER_THRESHOLD_PX = 3.0
SEED = 20261221


@dataclass(frozen=True)
class LineFit:
    origin: Point
    direction: Point
    slope: float
    intercept: float
    inliers: int
    samples: int
    inlier_ratio: float
    largest_gap_px: float
    occlusion_severity: float
    confidence: float
    bridged: bool
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    def y_at(self, x: float) -> float:
        return self.slope * float(x) + self.intercept


def _total_least_squares(points: Sequence[Point]) -> tuple[Point, Point]:
    """Principal direction of a point set, which handles a vertical run."""
    count = len(points)
    mx = sum(p[0] for p in points) / count
    my = sum(p[1] for p in points) / count
    sxx = sum((p[0] - mx) ** 2 for p in points)
    syy = sum((p[1] - my) ** 2 for p in points)
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    angle = 0.5 * math.atan2(2.0 * sxy, sxx - syy)
    return (mx, my), (math.cos(angle), math.sin(angle))


def ordinary_least_squares(points: Sequence[Point]) -> tuple[float, float]:
    """The fit RANSAC is here to beat, kept so the difference is showable."""
    count = len(points)
    if count < 2:
        raise ValueError("a line needs at least two points")
    mx = sum(p[0] for p in points) / count
    my = sum(p[1] for p in points) / count
    sxx = sum((p[0] - mx) ** 2 for p in points)
    if abs(sxx) < 1e-12:
        raise ValueError("a vertical run has no ordinary least squares slope")
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    slope = sxy / sxx
    return slope, my - slope * mx


def _point_line_distance(point: Point, origin: Point,
                         direction: Point) -> float:
    vx = point[0] - origin[0]
    vy = point[1] - origin[1]
    return abs(vx * direction[1] - vy * direction[0])


def _largest_x_gap(points: Sequence[Point]) -> float:
    xs = sorted(p[0] for p in points)
    if len(xs) < 2:
        return 0.0
    return max(b - a for a, b in zip(xs, xs[1:]))


def fit_ransac_line(edge_pixels, occlusion_severity: float = 0.0,
                    threshold_px: float = INLIER_THRESHOLD_PX,
                    iterations: int = RANSAC_ITERATIONS,
                    seed: int = SEED) -> LineFit:
    """Fit the dominant straight line and say what the bridge cost.

    RANSAC is here for one reason. A tree removes a run of eave pixels and
    replaces them with branch pixels that segment as edge. Ordinary least
    squares minimises squared error against every one of those outliers and
    tilts. RANSAC scores a candidate by how many points agree with it, so a
    minority of branch pixels never wins.

    The confidence returned is deliberately not just the inlier ratio. A
    line drawn across a gap is an extrapolation through space nothing was
    observed in, and a bay window or a dormer inside that gap would be
    invisible to a perfect fit, so the width of the bridged gap is a term
    in the score and a bridge is named as an assumption.
    """
    points = _as_points(edge_pixels)
    severity = max(0.0, min(1.0, float(occlusion_severity)))
    findings: list[Finding] = []

    if len(points) < 2:
        raise ValueError("a line fit needs at least two edge pixels")

    rng = random.Random(seed)
    best_inliers: list[Point] = []
    best_origin, best_direction = points[0], (1.0, 0.0)

    for _ in range(max(1, int(iterations))):
        a, b = rng.sample(range(len(points)), 2)
        pa, pb = points[a], points[b]
        dx, dy = pb[0] - pa[0], pb[1] - pa[1]
        norm = math.hypot(dx, dy)
        if norm < 1e-9:
            continue
        direction = (dx / norm, dy / norm)
        inliers = [p for p in points
                   if _point_line_distance(p, pa, direction) <= threshold_px]
        if len(inliers) > len(best_inliers):
            best_inliers, best_origin, best_direction = inliers, pa, direction

    if len(best_inliers) >= 2:
        best_origin, best_direction = _total_least_squares(best_inliers)
    else:
        best_inliers = list(points)
        best_origin, best_direction = _total_least_squares(points)

    dx, dy = best_direction
    if abs(dx) < 1e-9:
        raise ValueError("a vertical roofline is not a lighting run")
    slope = dy / dx
    intercept = best_origin[1] - slope * best_origin[0]

    ratio = len(best_inliers) / len(points)
    gap = _largest_x_gap(best_inliers)
    span = max(p[0] for p in points) - min(p[0] for p in points)
    gap_fraction = gap / span if span > 1e-9 else 0.0
    bridged = gap_fraction > 0.05

    confidence = max(0.0, min(1.0, ratio * (1.0 - gap_fraction)
                              * (1.0 - 0.3 * severity)))

    findings.append(Finding(
        code="FIT-CONSENSUS", severity=SEVERITY_OK,
        title=f"{len(best_inliers)} of {len(points)} pixel(s) agree on one "
              f"line",
        detail=("RANSAC scores a candidate by agreement rather than by "
                "squared error, so branch pixels in front of the eave have "
                "to outnumber the eave to win, not merely be far away."),
        fix="Keep the inlier threshold near the segmentation noise floor."))

    if bridged:
        findings.append(Finding(
            code="FIT-BRIDGE", severity=SEVERITY_CRITICAL,
            title=f"The line crosses a {gap:.0f} px gap with nothing in it",
            detail=("Across that span no pixel was observed. The fit is an "
                    "assumption that the eave continues straight, and a bay "
                    "window, a dormer, or a change of roof plane inside the "
                    "gap would look exactly the same to this fit."),
            fix=("Walk the gap on site, or take a second photograph from an "
                 "angle where the tree does not cover it.")))

    if ratio < 0.5:
        findings.append(Finding(
            code="FIT-MINORITY", severity=SEVERITY_CRITICAL,
            title=f"Only {ratio * 100:.0f}% of the pixels support the line",
            detail=("Under half agreeing means there is no dominant straight "
                    "run in this mask. A fit is still returned, and it "
                    "should not be trusted as a roofline."),
            fix="Re segment, or treat the mask as two separate roof planes."))
    elif severity >= 0.5:
        findings.append(Finding(
            code="FIT-SEVERITY", severity=SEVERITY_WARN,
            title=f"Occlusion severity was declared at {severity:.2f}",
            detail=("Heavy occlusion does not lower the inlier ratio, "
                    "because the pixels that are gone cannot disagree with "
                    "anything. It lowers what the ratio is worth, which is "
                    "why it is a separate term in the confidence."),
            fix="Report the severity alongside the number, never instead."))

    headline = (f"Slope {slope:+.4f}, confidence {confidence:.2f} on "
                f"{len(best_inliers)} inlier(s)")

    return LineFit(
        origin=best_origin, direction=best_direction, slope=slope,
        intercept=intercept, inliers=len(best_inliers), samples=len(points),
        inlier_ratio=ratio, largest_gap_px=gap, occlusion_severity=severity,
        confidence=confidence, bridged=bridged, headline=headline,
        findings=tuple(findings))


def synthesise_eave_pixels(start: Point = (120.0, 330.0),
                           end: Point = (680.0, 330.0),
                           occlusion_severity: float = 0.0,
                           noise_px: float = 1.0,
                           seed: int = SEED) -> tuple[Point, ...]:
    """Build a believable edge pixel run with a tree in front of it.

    Severity does two things at once, which is what a tree does: it removes
    a contiguous run of eave pixels, and it adds branch pixels above the
    eave that segment as edge.
    """
    severity = max(0.0, min(1.0, float(occlusion_severity)))
    rng = random.Random(seed)
    span = end[0] - start[0]
    slope = (end[1] - start[1]) / span if abs(span) > 1e-9 else 0.0

    gap_width = span * 0.35 * severity
    gap_start = start[0] + span * 0.40
    gap_end = gap_start + gap_width

    pixels: list[Point] = []
    steps = 160
    for index in range(steps + 1):
        x = start[0] + span * index / steps
        if gap_start <= x <= gap_end:
            continue
        y = start[1] + slope * (x - start[0]) + rng.uniform(-noise_px,
                                                            noise_px)
        pixels.append((x, y))

    branch_count = int(round(len(pixels) * 0.45 * severity))
    for _ in range(branch_count):
        x = rng.uniform(gap_start - 40.0, gap_end + 40.0)
        y = start[1] - rng.uniform(25.0, 110.0)
        pixels.append((x, y))

    return tuple(pixels)


# ---------------------------------------------------------------------------
# 3. Bulb spacing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BulbRun:
    start: Point
    end: Point
    requested_pitch_px: float
    actual_pitch_px: float
    coordinates: tuple[Point, ...]
    run_length_px: float
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def count(self) -> int:
        return len(self.coordinates)

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def calculate_bulb_spacing(start_coord, end_coord,
                           pitch_px: float) -> BulbRun:
    """Solve for the pitch instead of stepping by it.

    Stepping from one end by a fixed pitch leaves whatever is left over as
    a gap at the far end, and floating point puts the last bulb slightly
    past the eave as often as slightly short of it. The count is solved
    first and the pitch follows from it, so the first and last coordinates
    are the ends of the run exactly.

    The count is rounded up rather than down. Rounding down stretches the
    spacing beyond what was asked for, which is a visible dark gap on the
    house. Rounding up tightens it, which is not.
    """
    start = (float(start_coord[0]), float(start_coord[1]))
    end = (float(end_coord[0]), float(end_coord[1]))
    pitch = float(pitch_px)
    findings: list[Finding] = []

    if pitch <= 0:
        raise ValueError("a bulb pitch has to be a positive number of pixels")

    length = math.hypot(end[0] - start[0], end[1] - start[1])

    if length < 1e-9:
        findings.append(Finding(
            code="BLB-ZERO", severity=SEVERITY_CRITICAL,
            title="The run has no length",
            detail=("Start and end are the same pixel, so there is no line "
                    "to hang. One coordinate is returned rather than a "
                    "division by zero."),
            fix="Check the eave extraction before spacing anything on it."))
        return BulbRun(start=start, end=end, requested_pitch_px=pitch,
                       actual_pitch_px=0.0, coordinates=(start,),
                       run_length_px=0.0, findings=tuple(findings))

    gaps = max(1, math.ceil(length / pitch - 1e-9))
    actual = length / gaps
    count = gaps + 1

    coordinates = tuple(
        (start[0] + (end[0] - start[0]) * index / gaps,
         start[1] + (end[1] - start[1]) * index / gaps)
        for index in range(count))
    coordinates = coordinates[:-1] + (end,)

    findings.append(Finding(
        code="BLB-EXACT", severity=SEVERITY_OK,
        title=f"{count} bulb(s) at {actual:.2f} px, both ends landed exactly",
        detail=("The first and last coordinates are the ends of the run, "
                "not the ends of a stepped sequence that happened to stop "
                "near them. Nothing is left over."),
        fix="Render from these coordinates rather than re deriving them."))

    if actual < pitch - 1e-9:
        findings.append(Finding(
            code="BLB-TIGHTEN", severity=SEVERITY_WARN,
            title=(f"Pitch tightened from {pitch:.2f} to {actual:.2f} px to "
                   f"fit the run"),
            detail=("Rounding the count up shortens the spacing. Rounding it "
                    "down would have stretched the spacing past what was "
                    "asked for, which shows on the house as a dark gap and "
                    "does not show in any number."),
            fix=("Order to the count returned here, not to the length "
                 "divided by the nominal pitch.")))

    findings.append(Finding(
        code="BLB-SCALE", severity=SEVERITY_WARN,
        title="A pixel pitch is not a bulb spacing",
        detail=("These coordinates are for drawing on the photograph. "
                "Turning the pitch into inches on the house needs a measured "
                "length in the same image plane, and this engine will not "
                "invent one."),
        fix="Measure one known length in frame and scale everything by it."))

    return BulbRun(start=start, end=end, requested_pitch_px=pitch,
                   actual_pitch_px=actual, coordinates=coordinates,
                   run_length_px=length, findings=tuple(findings))
