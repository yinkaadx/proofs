"""Holiday Lighting CV Geometry Console.

Rendered inside the hub app. All logic lives in core.py, which imports
neither Streamlit nor numpy, so the same maths could run inside a worker
behind a real SAM3 call.

Everything on this page is evaluated live from the controls. There is no
session state to go stale, which removes the class of bug where a card on
screen describes a fit that two interactions ago stopped being true.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.holiday_lighting_cv_console.core import (
    ENGINE_VERSION,
    SAMPLE_GABLE,
    SAMPLE_HIP,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    calculate_bulb_spacing,
    extract_bottom_eave,
    fit_ransac_line,
    ordinary_least_squares,
    synthesise_eave_pixels,
)

_TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}

_SHAPES = {"Gable front": SAMPLE_GABLE, "Hip roof": SAMPLE_HIP}


def _kpis(pairs) -> None:
    cells = "".join(
        f'<div class="app-kpi {tone}"><b>{esc(value)}</b>'
        f"<span>{esc(label)}</span></div>"
        for value, label, tone in pairs)
    st.markdown(f'<div class="app-kpis">{cells}</div>',
                unsafe_allow_html=True)


def _finding_card(finding) -> None:
    tone = _TONE[finding.severity]
    st.markdown(
        f'<div class="app-card {tone}">\n'
        f'  <h4><span class="app-tag {tone}">{esc(finding.code)}</span>\n'
        f"  {esc(finding.title)}</h4>\n"
        f"  <p>{esc(finding.detail)}</p>\n"
        f'  <div class="app-ev">Fix: {esc(finding.fix)}</div>\n'
        f"</div>",
        unsafe_allow_html=True)


def _svg(body: str, height: int = 260) -> None:
    st.markdown(
        f'<div class="app-card"><svg viewBox="80 120 640 {height}" '
        f'width="100%" height="{height}" '
        f'style="display:block;overflow:visible">{body}</svg></div>',
        unsafe_allow_html=True)


def _poly(points, colour: str, width: float = 2.0, dashed: bool = False) -> str:
    pairs = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    dash = ' stroke-dasharray="7 5"' if dashed else ""
    return (f'<polyline points="{pairs}" fill="none" stroke="{colour}" '
            f'stroke-width="{width}"{dash} stroke-linejoin="round" />')


def render() -> None:
    inject()
    st.markdown(
        '<div class="app-hero">\n'
        "  <h1>Holiday Lighting CV Geometry Console</h1>\n"
        "  <p>Three stages between a segmentation mask and a bulb on a "
        "  house, each written around the failure that puts an installer on "
        "  a ladder in the wrong place. A roof mask holds a ridge, two "
        "  rakes and an eave, and only one of them carries lights. A tree "
        "  both removes eave pixels and adds branch pixels, which is why "
        "  the fit here is RANSAC rather than least squares. And a bulb run "
        "  has to land exactly on both ends, which means solving for the "
        "  pitch rather than stepping by it. Every coordinate on this page "
        "  is in pixels, because a single photograph carries no scale.</p>\n"
        "</div>",
        unsafe_allow_html=True)

    eave_tab, fit_tab, bulb_tab = st.tabs(
        ["Eave extraction", "RANSAC roofline", "Bulb spacing"])

    # -- 1. eave extraction ------------------------------------------------
    with eave_tab:
        st.markdown(
            "#### The lowest pixels of a mask are not the eave\n\n"
            "Both rake edges reach eave height at their lower ends, so a "
            "filter that keeps the lowest pixels keeps those two tips and "
            "pulls the fitted line up at each corner. Every edge is "
            "classified and the rakes are dropped whole.")

        shape = st.selectbox("Raw SAM3 mask", list(_SHAPES), index=0)
        tolerance = st.slider("Rake slope tolerance", 0.05, 1.0, 0.30, 0.05)
        polygon = _SHAPES[shape]
        extraction = extract_bottom_eave(polygon, slope_tolerance=tolerance)

        _kpis([
            (extraction.status, "Status",
             _TONE[extraction.severity]),
            (f"{extraction.span_px:.0f} px", "Eave span", ""),
            (str(extraction.dropped_rake), "Rake edges dropped", ""),
            (f"{extraction.mean_slope:+.4f}", "Slope per pixel",
             "ok" if abs(extraction.mean_slope) <= 0.05 else "warn"),
        ])

        tone = _TONE[extraction.severity]
        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag {tone}">'
            f'{esc(extraction.status)}</span>\n'
            f"  {esc(extraction.headline)}</h4>\n"
            f"  <p>Kept {extraction.edges_kept} edge(s) of "
            f"{extraction.edges_in}, dropping {extraction.dropped_rake} "
            f"rake, {extraction.dropped_ridge} ridge, and "
            f"{extraction.dropped_vertical} wall edge(s).</p>\n"
            f"</div>",
            unsafe_allow_html=True)

        body = _poly(list(polygon) + [polygon[0]], "var(--app-muted)", 2.0,
                     dashed=True)
        if extraction.points:
            body += _poly(extraction.points, "var(--app-accent)", 5.0)
        _svg(body)
        st.caption(
            "Dashed line is the raw mask outline. The solid run is what "
            "survived classification, and it is the only geometry passed "
            "forward to the fit.")

        for finding in extraction.findings:
            _finding_card(finding)

    # -- 2. RANSAC fit -----------------------------------------------------
    with fit_tab:
        st.markdown(
            "#### Why this is RANSAC and not least squares\n\n"
            "A tree does two things at once: it removes a contiguous run of "
            "eave pixels and it adds branch pixels above the eave that "
            "segment as edge. Least squares minimises squared error against "
            "every one of those outliers and tilts. RANSAC scores a "
            "candidate by how many pixels agree with it, so a minority of "
            "branch pixels never wins.")

        severity = st.slider("Occlusion severity", 0.0, 1.0, 0.6, 0.05)
        pixels = synthesise_eave_pixels(occlusion_severity=severity)
        fit = fit_ransac_line(pixels, severity)
        ols_slope, ols_intercept = ordinary_least_squares(pixels)

        _kpis([
            (f"{fit.slope:+.5f}", "RANSAC slope", "ok"),
            (f"{ols_slope:+.5f}", "Least squares slope",
             "crit" if abs(ols_slope) > abs(fit.slope) * 3 + 1e-4 else ""),
            (f"{fit.confidence:.2f}", "Confidence",
             _TONE[fit.severity]),
            (f"{fit.largest_gap_px:.0f} px", "Largest bridged gap",
             "crit" if fit.bridged else "ok"),
        ])

        marks = "".join(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2" '
            f'fill="var(--app-muted)" />' for x, y in pixels)
        left_x, right_x = 120.0, 680.0
        marks += _poly([(left_x, fit.y_at(left_x)),
                        (right_x, fit.y_at(right_x))],
                       "var(--app-ok)", 3.0)
        marks += _poly([(left_x, ols_slope * left_x + ols_intercept),
                        (right_x, ols_slope * right_x + ols_intercept)],
                       "var(--app-crit)", 3.0, dashed=True)
        _svg(marks, height=280)
        st.caption(
            "Dots are the simulated edge pixels, including the branch "
            "pixels the tree adds. The solid line is the RANSAC fit and the "
            "dashed line is ordinary least squares over the same pixels. "
            "The true eave is level, so the correct slope is zero.")

        for finding in fit.findings:
            _finding_card(finding)

    # -- 3. bulb spacing ---------------------------------------------------
    with bulb_tab:
        st.markdown(
            "#### Solve for the pitch, do not step by it\n\n"
            "Stepping from one end by a fixed pitch leaves the remainder as "
            "a gap at the far end, and floating point puts the last bulb "
            "past the eave as often as short of it. The count is solved "
            "first and the pitch follows, so both ends land exactly.")

        pitch = st.slider("Requested pitch in pixels", 8, 120, 40, 1)
        run = calculate_bulb_spacing((120.0, 330.0), (680.0, 330.0), pitch)

        _kpis([
            (str(run.count), "Bulbs", "ok"),
            (f"{run.requested_pitch_px:.2f} px", "Requested pitch", ""),
            (f"{run.actual_pitch_px:.2f} px", "Actual pitch",
             "warn" if run.actual_pitch_px < run.requested_pitch_px - 1e-9
             else "ok"),
            (f"{run.run_length_px:.0f} px", "Run length", ""),
        ])

        body = _poly([run.start, run.end], "var(--app-line)", 3.0)
        body += "".join(
            f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5" '
            f'fill="var(--app-warn)" stroke="var(--app-ink)" '
            f'stroke-width="1" />' for x, y in run.coordinates)
        _svg(body, height=150)

        st.code(
            "[\n" + ",\n".join(
                f"  {{ \"x\": {x:.2f}, \"y\": {y:.2f} }}"
                for x, y in run.coordinates) + "\n]",
            language="json")
        st.caption(
            f"The first coordinate is the start of the run and the last is "
            f"the end, both exactly. Every gap is {run.actual_pitch_px:.2f} "
            f"px, and none is wider than the {run.requested_pitch_px:.2f} px "
            f"that was asked for.")

        for finding in run.findings:
            _finding_card(finding)

    st.markdown(
        f'<div class="app-foot">Engine {esc(ENGINE_VERSION)}, with no numpy '
        f"and no Streamlit inside it. RANSAC is seeded, so the same pixels "
        f"return the same line on every run. Every number on this page is a "
        f"pixel count: a single photograph carries no metric reference, so "
        f"converting a span or a pitch into feet needs a measured length in "
        f"the same image plane, and this engine will not invent one.</div>",
        unsafe_allow_html=True)
