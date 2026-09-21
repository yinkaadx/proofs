"""JOOR ApparelMagic and Extensiv Sync Console.

Rendered inside the hub app. All logic lives in core.py, which has no
Streamlit dependency and keeps money in Decimal, so the same engine could
sit behind the real integration.

Everything on this page is evaluated live from the controls. There is no
session state to go stale, which removes the class of bug where a card on
screen describes an order that two interactions ago stopped being true.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.joor_apparel_sync_console.core import (
    ACCEPTED,
    ENGINE_VERSION,
    LINESHEET_PUBLISH,
    LINESHEET_PULL,
    SAMPLE_INVENTORY,
    SAMPLE_MATRIX_BROKEN,
    SAMPLE_MATRIX_CLEAN,
    SAMPLE_TRACKING,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    STYLE_CATALOGUE,
    process_fulfillment_tracking,
    reconcile_extensiv_inventory,
    simulate_joor_order_ingestion,
)

_TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}

_LINESHEET_TONE = {
    LINESHEET_PUBLISH: "ok",
    LINESHEET_PULL: "crit",
}

_MATRICES = {
    "Clean size run": SAMPLE_MATRIX_CLEAN,
    "Size and colour the style does not carry": SAMPLE_MATRIX_BROKEN,
}


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


def _stage(state: str, name: str, detail: str) -> str:
    return (f'<div class="app-stage"><span class="s {state}">'
            f"{esc(state.upper())}</span>"
            f'<span class="n">{esc(name)}</span>'
            f'<span class="d">{esc(detail)}</span></div>')


def _badge(tone: str, text: str) -> str:
    return f'<span class="app-tag {tone}">{esc(text)}</span>'


def render() -> None:
    inject()
    st.markdown(
        '<div class="app-hero">\n'
        "  <h1>JOOR, ApparelMagic and Extensiv Sync Console</h1>\n"
        "  <p>Three joins in a wholesale apparel stack, each built so the\n"
        "  quiet failure cannot happen. A size run is a list of labels and\n"
        "  two systems rarely agree on the list, so sizes map by label and\n"
        "  never by position. Available to sell can go negative, and when it\n"
        "  does the units are already promised to somebody, so the shortfall\n"
        "  is published beside the clamped zero rather than instead of it.\n"
        "  And a wholesale order is completed after it is invoiced, which is\n"
        "  after it ships, in that order and no other.</p>\n"
        "</div>",
        unsafe_allow_html=True)

    order_tab, stock_tab, ship_tab = st.tabs(
        ["Order ingestion", "Available to sell", "Fulfillment"])

    # -- 1. order ingestion ------------------------------------------------
    with order_tab:
        st.markdown(
            "#### Match on the label, never on the position\n\n"
            "A six label alpha run lines up neatly against the first six of "
            "an eight label numeric run, so position matching turns size L "
            "into size 6 and passes every validation the import has. The "
            "buyer finds out when the box arrives.")

        col_a, col_b = st.columns(2)
        with col_a:
            style_code = st.selectbox(
                "Style", [s.style_code for s in STYLE_CATALOGUE], index=0)
        with col_b:
            matrix_name = st.selectbox("JOOR size matrix", list(_MATRICES),
                                       index=0)
        po_number = st.text_input("Purchase order", value="PO-44812")

        style = next(s for s in STYLE_CATALOGUE
                     if s.style_code == style_code)
        payload = simulate_joor_order_ingestion(
            po_number, style_code, _MATRICES[matrix_name])
        tone = _TONE[payload.severity]
        status_tone = "ok" if payload.accepted else "crit"

        _kpis([
            (payload.status, "Status", status_tone),
            (f"{payload.units_mapped}", "Units mapped",
             "ok" if payload.accepted else "crit"),
            (f"{payload.units_rejected}", "Units rejected",
             "crit" if payload.units_rejected else "ok"),
            (f"{payload.order_value:,.2f}", "Order value", ""),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f"  <h4>{_badge(status_tone, payload.status)}\n"
            f"  {esc(payload.headline)}</h4>\n"
            f"  <p>{esc(style.description)} on the {esc(payload.scale)} "
            f"scale: {esc(', '.join(style.sizes))}.</p>\n"
            f'  <div class="app-ev">Colours on this style: '
            f"{esc(', '.join(name for name, _ in style.colors))}</div>\n"
            f"</div>",
            unsafe_allow_html=True)

        if payload.lines:
            st.table({
                "SKU": [line.sku for line in payload.lines],
                "Colour": [line.color_name for line in payload.lines],
                "Size": [line.size for line in payload.lines],
                "Units": [line.quantity for line in payload.lines],
                "Extended": [f"{line.extended_price:,.2f}"
                             for line in payload.lines],
            })
            sizes = payload.size_breakdown
            colors = payload.color_breakdown
            st.caption(
                "Size breakdown: "
                + ", ".join(f"{size} {units}"
                            for size, units in sizes.items())
                + ". Colour breakdown: "
                + ", ".join(f"{name} {units}"
                            for name, units in colors.items())
                + f". Lines sum to {payload.units_mapped} unit(s) and "
                  f"{payload.order_value:,.2f}.")
        else:
            rows = "".join(
                [_stage("fail", "Size not on the style", size)
                 for size in payload.unmapped_sizes]
                + [_stage("fail", "Colour not on the style", colour)
                   for colour in payload.unmapped_colors])
            st.markdown(f'<div class="app-card crit">{rows}</div>',
                        unsafe_allow_html=True)
            st.caption(
                f"No lines were created, so none are shown. All "
                f"{payload.units_submitted} submitted unit(s) are counted as "
                f"rejected rather than partly accepted.")

        for finding in payload.findings:
            _finding_card(finding)

    # -- 2. available to sell ---------------------------------------------
    with stock_tab:
        st.markdown(
            "#### A negative available to sell is not a sold out item\n\n"
            "When physical minus allocated goes below zero the units are "
            "already promised to somebody. Publishing the clamped zero "
            "without the shortfall beside it reads as sold out, which reads "
            "as normal, and nobody investigates until allocation.")

        col_c, col_d = st.columns(2)
        with col_c:
            physical = st.slider("Extensiv physical count", 0, 400, 18, 1)
        with col_d:
            allocated = st.slider("Allocated on open orders", 0, 400, 44, 1)
        sku = st.text_input("SKU", value="SS26-DRS-221-BON-4")

        position = reconcile_extensiv_inventory(sku, physical, allocated)
        tone = _TONE[position.severity]
        sheet_tone = _LINESHEET_TONE.get(position.linesheet_status, "warn")

        _kpis([
            (str(position.available_to_sell), "Available to sell",
             "ok" if position.available_to_sell else "warn"),
            (str(position.oversold_units), "Oversold",
             "crit" if position.oversold else "ok"),
            (str(position.physical_count), "Physical at the 3PL", ""),
            (str(position.allocated_orders), "Allocated", ""),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f"  <h4>{_badge(sheet_tone, position.linesheet_status)}\n"
            f"  {esc(position.headline)}</h4>\n"
            f"  <p>{position.physical_count} physical less "
            f"{position.allocated_orders} allocated gives "
            f"{position.physical_count - position.allocated_orders}, "
            f"published as {position.available_to_sell}.</p>\n"
            f'  <div class="app-ev">'
            f"{min(position.physical_count, position.allocated_orders)} "
            f"allocated unit(s) covered by stock plus "
            f"{position.oversold_units} oversold equals "
            f"{position.allocated_orders} allocated</div>\n"
            f"</div>",
            unsafe_allow_html=True)

        rows = []
        for item, phys, alloc in SAMPLE_INVENTORY:
            sample = reconcile_extensiv_inventory(item, phys, alloc)
            state = ("fail" if sample.oversold
                     else "warn" if sample.available_to_sell == 0
                     else "pass")
            rows.append(_stage(
                state, item,
                f"{phys} physical, {alloc} allocated, "
                f"{sample.available_to_sell} available to sell, "
                f"{sample.oversold_units} oversold. "
                f"{sample.linesheet_status}."))
        st.markdown(f'<div class="app-card">{"".join(rows)}</div>',
                    unsafe_allow_html=True)

        for finding in position.findings:
            _finding_card(finding)

    # -- 3. fulfillment ----------------------------------------------------
    with ship_tab:
        st.markdown(
            "#### Ship, then invoice, then complete\n\n"
            "Completing on a tracking number nobody validated closes the "
            "order in the buyer's portal while the box is still on the "
            "packing bench, which removes the one signal the buyer's "
            "merchandiser uses to chase it.")

        col_e, col_f = st.columns(2)
        with col_e:
            order_id = st.text_input("JOOR order", value="JO-99120")
        with col_f:
            sample_name = st.selectbox(
                "Tracking number", [label for label, _ in SAMPLE_TRACKING],
                index=0)
        tracking = st.text_input(
            "As received from the warehouse",
            value=dict(SAMPLE_TRACKING)[sample_name])

        result = process_fulfillment_tracking(order_id, tracking)
        tone = _TONE[result.severity]
        state_tone = "ok" if result.completed else "crit"

        _kpis([
            (result.carrier, "Carrier",
             "ok" if result.tracking_valid else "crit"),
            (result.joor_status, "JOOR order", state_tone),
            (result.invoice_reference or "none", "ApparelMagic invoice",
             "ok" if result.invoice_reference else "crit"),
            (f"{sum(1 for s in result.stages if s.reached)} of "
             f"{len(result.stages)}", "Stages reached", state_tone),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f"  <h4>{_badge(state_tone, result.state)}\n"
            f"  {esc(result.headline)}</h4>\n"
            f"</div>",
            unsafe_allow_html=True)

        rows = "".join(
            _stage("pass" if stage.reached else "fail", stage.name,
                   stage.detail)
            for stage in result.stages)
        st.markdown(f'<div class="app-card">{rows}</div>',
                    unsafe_allow_html=True)

        st.markdown("#### Every sample number through the same gate")
        st.table({
            "Source": [label for label, _ in SAMPLE_TRACKING],
            "Number": [number or "(empty)" for _, number in SAMPLE_TRACKING],
            "Carrier": [process_fulfillment_tracking(order_id, number).carrier
                        for _, number in SAMPLE_TRACKING],
            "Order": [process_fulfillment_tracking(order_id, number).joor_status
                      for _, number in SAMPLE_TRACKING],
        })

        for finding in result.findings:
            _finding_card(finding)

    st.markdown(
        f'<div class="app-foot">Engine {esc(ENGINE_VERSION)}. Sizes map by '
        f"label and never by position, and a purchase order carrying a size "
        f"or colour the style does not have reaches no status other than "
        f"rejected, with no lines emitted at all. An order reaches "
        f"{esc(ACCEPTED)} only when every submitted unit mapped. Carrier "
        f"detection is a format match and not a confirmation that the "
        f"shipment exists, which is why the carrier API stays the gate "
        f"before completion in a live integration.</div>",
        unsafe_allow_html=True)
