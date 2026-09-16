"""Irish Wine Wholesale Operating Console.

Rendered inside the hub. All logic lives in core.py, which holds no Streamlit
import. Nothing is held in session state, so every figure on screen is
computed from the controls as they stand.
"""

from __future__ import annotations

from decimal import Decimal

import streamlit as st

from shared.theme import esc, inject
from tools.irish_wine_distribution_console.core import (
    BOTTLE_SIZES_ML,
    BOTTLE_TYPES,
    CATALOGUE,
    CUSTOMER_TYPES,
    DEFAULT_SIZE,
    ENGINE_VERSION,
    PRODUCT_BY_ID,
    RATES_SOURCE,
    SAMPLE_ORDERS,
    SEVERITY_CRIT,
    SEVERITY_OK,
    SEVERITY_WARN,
    VAT_RATE,
    WAREHOUSE_TYPES,
    band_cliffs,
    calculate_irish_wine_duty,
    dispatch_summary_rows,
    duty_band_rows,
    duty_per_bottle_cents,
    euros,
    generate_3pl_dispatch_payload,
    get_customer_tier_price,
    margin_erosion,
    tier_rows,
)

TONE = {SEVERITY_CRIT: "crit", SEVERITY_WARN: "warn", SEVERITY_OK: "ok"}

PRODUCT_BY_LABEL = {f"{p.product_id} {p.name}": p for p in CATALOGUE}
ORDER_BY_LABEL = {f"{o.order_id} {o.customer}": o for o in SAMPLE_ORDERS}

DISCLAIMER = (
    "This console files nothing and moves no stock. It is a calculator against "
    "the published rates, not tax advice, and the duty position on a real "
    "consignment is the one your own returns and your warehousekeeper record."
)


def _finding_card(finding) -> None:
    tone = TONE.get(finding.severity, "warn")
    st.markdown(
        f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(finding.code)}</span>
  {esc(finding.title)}</h4>
  <p>{esc(finding.detail)}</p>
  <div class="app-ev">{esc(finding.fix)}</div>
</div>
""",
        unsafe_allow_html=True,
    )


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>Irish Wine Wholesale Operating Console</h1>
  <p>Two things about Irish wine duty catch people out and both cost money in
  the same direction. VAT is charged on the duty inclusive amount, so working
  it out on the goods alone understates the invoice. And duty is charged per
  hectolitre of product rather than per hectolitre of alcohol, so strength does
  not scale the duty, it only picks the band, and the band edges are cliffs a
  label can round across.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Duty and VAT**: price a line, then move the strength across "
            "15 percent and watch the cliff.\n"
            "2. **Tier pricing**: pick the entry level Merlot and read what a "
            "20 percent chain discount actually costs.\n"
            "3. **Dispatch**: switch the warehouse between bonded and duty "
            "paid. The difference is a cash event, not a label."
        )
        st.divider()
        st.caption(DISCLAIMER)
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_duty, tab_tier, tab_dispatch, tab_rates = st.tabs(
        ["Duty and VAT", "Tier Pricing", "3PL Dispatch", "Rate Card"])

    # -----------------------------------------------------------------
    # One line, priced the way the invoice has to read
    # -----------------------------------------------------------------
    with tab_duty:
        c1, c2, c3 = st.columns(3)
        bottle_type = c1.selectbox("Product", BOTTLE_TYPES, key="iwd_type")
        abv = c2.slider("Strength, percent by volume", 0.0, 22.0, 13.0, 0.1,
                        key="iwd_abv")
        size_label = c3.selectbox("Bottle size", list(BOTTLE_SIZES_ML),
                                  index=list(BOTTLE_SIZES_ML).index(
                                      DEFAULT_SIZE),
                                  key="iwd_size")

        c4, c5 = st.columns(2)
        quantity = c4.number_input("Bottles on the line", 1, 20000, 12,
                                   key="iwd_qty")
        unit_price = c5.number_input(
            "Duty suspended unit price, euro", 0.0, 500.0, 5.20, 0.10,
            key="iwd_price")

        calc = calculate_irish_wine_duty(
            bottle_type, f"{abv}", int(quantity), f"{unit_price}",
            BOTTLE_SIZES_ML[size_label])

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi">
    <div class="n">{esc(euros(calc.duty_suspended_cents))}</div>
    <div class="l">Duty suspended cost</div></div>
  <div class="app-kpi warn">
    <div class="n">{esc(euros(calc.duty_cents))}</div>
    <div class="l">Alcohol Products Tax</div></div>
  <div class="app-kpi warn">
    <div class="n">{esc(euros(calc.vat_cents))}</div>
    <div class="l">VAT at {VAT_RATE * 100:.0f}%, on goods plus duty</div></div>
  <div class="app-kpi crit">
    <div class="n">{esc(euros(calc.total_cents))}</div>
    <div class="l">Total invoice price</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.caption(f"Band applied: {esc(calc.band.label)}, at "
                   f"{esc(euros(calc.band.rate_per_hl_cents))} per hectolitre, "
                   f"giving {esc(euros(calc.duty_per_bottle_cents))} a bottle.")

        st.markdown("##### The invoice, and it foots")
        st.dataframe(calc.rows(), width="stretch", hide_index=True)

        for finding in calc.findings:
            _finding_card(finding)

        st.markdown("##### Where 0.1 percent of strength changes the duty")
        st.caption(
            "The rate is per hectolitre of product, so strength selects a band "
            "and does not scale within it. That makes every band edge a step."
        )
        st.dataframe(band_cliffs(BOTTLE_SIZES_ML[size_label]),
                     width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # What a discount really costs when the price contains a fixed tax
    # -----------------------------------------------------------------
    with tab_tier:
        p1, p2 = st.columns([3, 2])
        product = PRODUCT_BY_LABEL[p1.selectbox(
            "Product", list(PRODUCT_BY_LABEL), index=4, key="iwd_product")]
        customer_type = p2.selectbox("Account type", CUSTOMER_TYPES, index=2,
                                     key="iwd_customer")

        price = get_customer_tier_price(product.product_id, customer_type)
        erosion_tone = ("crit" if price.margin_erosion >= Decimal("0.5")
                        else "warn" if price.margin_erosion > 0 else "ok")

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi">
    <div class="n">{esc(euros(price.applied_price_cents))}</div>
    <div class="l">Applied price per bottle</div></div>
  <div class="app-kpi warn">
    <div class="n">{price.headline_discount * 100:.0f}%</div>
    <div class="l">Headline discount off list</div></div>
  <div class="app-kpi {erosion_tone}">
    <div class="n">{price.margin_erosion * 100:.0f}%</div>
    <div class="l">Share of contribution actually given away</div></div>
  <div class="app-kpi">
    <div class="n">{esc(euros(price.contribution_cents))}</div>
    <div class="l">Contribution left per bottle</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown(
            f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">The discount is not the discount</span>
  {price.headline_discount * 100:.0f} percent off list took
  {price.margin_erosion * 100:.0f} percent of the margin</h4>
  <p>The list price of {esc(euros(price.base_price_cents))} already contains
  {esc(euros(price.duty_per_bottle_cents))} of excise and
  {esc(euros(PRODUCT_BY_ID[product.product_id].cost_cents))} of wine. Neither
  of those discounts. The whole of the
  {esc(euros(price.discount_cents))} comes out of what was left.</p>
  <div class="app-ev">{esc(price.tier.note)}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### Every tier on this product")
        st.dataframe(margin_erosion(product.product_id), width="stretch",
                     hide_index=True)

        st.markdown("##### Terms behind each tier")
        st.dataframe(tier_rows(product.product_id), width="stretch",
                     hide_index=True)

    # -----------------------------------------------------------------
    # The warehouse type is a cash decision
    # -----------------------------------------------------------------
    with tab_dispatch:
        d1, d2 = st.columns([3, 2])
        order = ORDER_BY_LABEL[d1.selectbox(
            "Order", list(ORDER_BY_LABEL), key="iwd_order")]
        warehouse = d2.selectbox("Warehouse", WAREHOUSE_TYPES,
                                 key="iwd_warehouse")

        payload = generate_3pl_dispatch_payload(order.order_id, warehouse)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{payload.total_cases}</div>
    <div class="l">Cases</div></div>
  <div class="app-kpi"><div class="n">{payload.total_bottles}</div>
    <div class="l">Bottles</div></div>
  <div class="app-kpi warn">
    <div class="n">{esc(euros(payload.duty_cents))}</div>
    <div class="l">Excise on the consignment</div></div>
  <div class="app-kpi {"crit" if warehouse == WAREHOUSE_TYPES[0] else "ok"}">
    <div class="n">{esc(euros(payload.duty_cents)
                        if warehouse == WAREHOUSE_TYPES[0] else euros(0))}</div>
    <div class="l">Liability created by this release</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for finding in payload.findings:
            _finding_card(finding)

        st.markdown("##### Picking slip")
        st.code(payload.picking_slip, language="text")

        st.markdown("##### Dispatch instructions")
        for index, instruction in enumerate(payload.instructions, start=1):
            st.markdown(f"{index}. {esc(instruction)}")

        st.markdown("##### Lines on this order")
        st.dataframe(payload.line_rows(), width="stretch", hide_index=True)

        st.markdown("##### Every sample order out of both warehouse types")
        st.dataframe(dispatch_summary_rows(), width="stretch",
                     hide_index=True)

    # -----------------------------------------------------------------
    # The rates themselves, in one place
    # -----------------------------------------------------------------
    with tab_rates:
        st.caption(
            "Held in one table so a Budget change is a single edit. The rate "
            "is per hectolitre of product, which is why a 12 percent and a 14 "
            "percent still wine pay exactly the same."
        )
        st.dataframe(duty_band_rows(), width="stretch", hide_index=True)

        st.markdown("##### The same strength, still against sparkling")
        st.dataframe(
            [{"Strength": f"{abv}%",
              "Still, per 75cl": euros(duty_per_bottle_cents("Still", abv)),
              "Sparkling, per 75cl": euros(
                  duty_per_bottle_cents("Sparkling", abv)),
              "Difference": euros(duty_per_bottle_cents("Sparkling", abv)
                                  - duty_per_bottle_cents("Still", abv))}
             for abv in ("4.0", "8.0", "11.0", "13.0", "14.5")],
            width="stretch", hide_index=True)

        st.markdown(
            f"""
<div class="app-foot">{esc(DISCLAIMER)} {esc(RATES_SOURCE)} VAT at
{VAT_RATE * 100:.0f} percent. Engine version {ENGINE_VERSION}.</div>
""",
            unsafe_allow_html=True,
        )
