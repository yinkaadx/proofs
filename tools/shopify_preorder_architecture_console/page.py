"""Shopify Preorder Architecture Console.

Rendered inside the hub app. Every figure on this page is computed from the
controls on each run, so nothing on screen can describe a cart or a variant
that was replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.shopify_preorder_architecture_console.core import (
    AUDIT_CORRECT,
    AUTHORIZATION_DAYS,
    CAPTURE_METHODS,
    CAPTURE_UPFRONT,
    CHARGEBACK_WINDOW_DAYS,
    DEFAULT_FIXED_FEE,
    DEFAULT_RATE,
    ENGINE_VERSION,
    PRODUCT_TYPES,
    ROUTE_DISPATCH_NOW,
    SAMPLE_CART,
    SECOND_PARCEL_COST,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    CartItem,
    audit_current_setting,
    capture_comparison,
    evaluate_inventory_policy,
    simulate_payment_capture,
    simulate_split_cart,
)

SEVERITY_TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn",
                 SEVERITY_CRITICAL: "crit"}


def _kpis(items: list[tuple[str, str]]) -> None:
    cells = "".join(
        f'<div class="app-kpi"><b>{esc(value)}</b><span>{esc(label)}</span></div>'
        for label, value in items)
    st.markdown(f'<div class="app-kpis">{cells}</div>', unsafe_allow_html=True)


def _card(tone: str, tag: str, title: str, body: str, fix: str) -> None:
    st.markdown(
        f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(tag)}</span> {esc(title)}</h4>
  <p>{esc(body)}</p>
  <div class="app-ev">{esc(fix)}</div>
</div>
""",
        unsafe_allow_html=True,
    )


def _notes(notes) -> None:
    for note in notes:
        st.markdown(
            f'<div class="app-card info"><p>{esc(note)}</p></div>',
            unsafe_allow_html=True)


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>Shopify Preorder Architecture Console</h1>
  <p>A preorder breaks three things a normal store assumes. One order stops
  meaning one shipment, because a cart can hold an item on the shelf and an
  item that lands in six weeks. Stock at zero stops meaning stop selling, and
  the switch that fixes that is the same switch that makes an ordinary
  product oversell in silence. And money stops meaning revenue, because cash
  taken before the goods move is the customer's money on loan.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Split cart**: drop the stock on a line below what was "
            "ordered and watch the order become two shipments.\n"
            "2. **Inventory policy**: pick a type, then set the store to the "
            "wrong thing and read the audit.\n"
            "3. **Capture**: compare the two methods on the same amount and "
            "look at what reaches the bank."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live store. No Shopify token is held, no "
            "variant is written and no card is charged."
        )
        st.caption(
            f"Priced with a {DEFAULT_RATE * 100:.1f} percent plus "
            f"{DEFAULT_FIXED_FEE} processing rate and a "
            f"{SECOND_PARCEL_COST} second parcel. Replace all three with the "
            "merchant's own figures before quoting anything from here."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_cart, tab_policy, tab_money = st.tabs(
        ["Split Cart Routing", "Inventory Policy", "Payment Capture"])

    # -----------------------------------------------------------------
    with tab_cart:
        st.subheader("What leaves today and what waits")
        st.caption(
            "A line is split rather than moved whole. Three ordered against "
            "one in stock sends one today and holds two, because holding all "
            "three is a choice the customer did not make."
        )

        rows = []
        for index, seed in enumerate(SAMPLE_CART):
            left, middle, right = st.columns([3, 1, 1])
            with left:
                st.markdown(f"**{seed.title}**")
                st.caption(f"{seed.sku}, ship date {seed.ship_date or 'not published'}")
            with middle:
                quantity = st.number_input(
                    "Ordered", min_value=1, max_value=20,
                    value=seed.quantity, step=1, key=f"qty{index}")
            with right:
                available = st.number_input(
                    "On hand", min_value=0, max_value=20,
                    value=seed.available_now, step=1, key=f"avail{index}")
            rows.append(CartItem(seed.sku, seed.title, int(quantity),
                                 int(available), seed.ship_date))

        plan = simulate_split_cart(tuple(rows))
        _kpis([
            ("Units ordered", str(plan.units_requested)),
            ("Out today", str(plan.units_dispatched_now)),
            ("Held", str(plan.units_held)),
            ("Shipments", str(plan.shipment_count)),
            ("Unrecovered shipping", f"{plan.extra_shipping_cost}"),
        ])
        st.caption(
            f"{plan.units_dispatched_now} dispatched plus {plan.units_held} "
            f"held equals {plan.units_requested} units, which is the whole "
            f"cart."
        )
        _card("warn" if plan.is_split else "ok",
              f"{plan.shipment_count} shipment(s)",
              f"Latest ship date {plan.latest_ship_date}",
              plan.headline + ".", plan.fix)

        for shipment in plan.shipments:
            tone = "ok" if shipment.route == ROUTE_DISPATCH_NOW else "info"
            body = "; ".join(
                f"{line.quantity} of {line.title}" for line in shipment.lines)
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(shipment.route)}</span>
  {esc(shipment.label)}</h4>
  <p>{esc(body)}.</p>
  <div class="app-ev">Ships {esc(shipment.ship_date)},
  {shipment.units} unit(s)</div>
</div>
""",
                unsafe_allow_html=True,
            )
        _notes(plan.findings)

    # -----------------------------------------------------------------
    with tab_policy:
        st.subheader("What the variant has to be set to")
        st.caption(
            "Two settings do all the work. Continue selling when out of stock "
            "decides whether the buy button survives zero, and tracking "
            "quantity decides whether the shop knows how much it has already "
            "promised."
        )

        left, right = st.columns(2)
        with left:
            product_type = st.selectbox("Product type", list(PRODUCT_TYPES))
            stock_level = st.number_input(
                "Quantity on hand, negative means already owed",
                min_value=-500, max_value=500, value=0, step=1)
        with right:
            current_continue = st.toggle(
                "The store currently has continue selling ticked", value=False)
            st.caption(
                "Set this to what the variant is actually configured to "
                "today. The audit below compares it against the requirement.")

        policy = evaluate_inventory_policy(product_type, int(stock_level))
        _kpis([
            ("Inventory policy", policy.inventory_policy),
            ("Track quantity", "on" if policy.track_quantity else "off"),
            ("Buy button", "live" if policy.buy_button_live else "off"),
            ("Oversell risk", policy.oversell_risk),
        ])
        _card(SEVERITY_TONE.get(policy.severity, "warn"),
              policy.inventory_policy, policy.admin_label,
              policy.headline + ".", policy.fix)
        _notes(policy.findings)

        st.subheader("Audit of what the store is actually set to")
        audit = audit_current_setting(product_type, int(stock_level),
                                      bool(current_continue))
        _card(SEVERITY_TONE.get(audit.severity, "warn"), audit.verdict,
              f"Required {audit.required_policy}, currently {audit.current_policy}",
              audit.headline + ".", audit.fix)
        if audit.verdict != AUDIT_CORRECT:
            st.caption(
                "Neither of these failures produces an error, an alert or a "
                "visible change on the storefront. That is why they survive "
                "so long."
            )

        st.subheader("Every product type at this stock level")
        for kind in PRODUCT_TYPES:
            row = evaluate_inventory_policy(kind, int(stock_level))
            st.markdown(
                f"- **{kind}**: policy {row.inventory_policy}, tracking "
                f"{'on' if row.track_quantity else 'off'}, buy button "
                f"{'live' if row.buy_button_live else 'off'}")

    # -----------------------------------------------------------------
    with tab_money:
        st.subheader("Where the money actually sits")
        st.caption(
            "Revenue belongs to the period the goods move, not the period the "
            "card cleared. Cash taken upfront is a liability of the same size "
            "until the parcel leaves, and the entries below are real double "
            "entry rows whose totals are the sum of them."
        )

        left, right = st.columns(2)
        with left:
            method = st.selectbox("Capture method", list(CAPTURE_METHODS))
        with right:
            amount = st.number_input("Order value", min_value=1.0,
                                     max_value=100_000.0, value=249.00,
                                     step=1.0, format="%.2f")

        ledger = simulate_payment_capture(method, f"{amount:.2f}")
        _kpis([
            ("Cash today", f"{ledger.cash_today}"),
            ("Processing fee", f"{ledger.processing_fee_today}"),
            ("Net to bank", f"{ledger.net_cash_today}"),
            ("Deferred revenue", f"{ledger.deferred_revenue}"),
            ("Revenue recognised", f"{ledger.revenue_recognised_today}"),
        ])
        _card("warn" if method == CAPTURE_UPFRONT else "info",
              ledger.capture_method, ledger.working_capital_effect,
              ledger.headline + ".", ledger.fix)

        st.markdown("**The journal**")
        for entry in ledger.entries:
            side = (f"debit {entry.debit}" if entry.debit
                    else f"credit {entry.credit}")
            st.markdown(f"- {entry.account}: {side}")
        st.caption(
            f"Debits {ledger.total_debits} against credits "
            f"{ledger.total_credits}, so the entry "
            f"{'balances' if ledger.balances else 'does not balance'}."
        )

        _notes(ledger.findings)
        st.markdown(
            f"- Chargeback clock starts {ledger.chargeback_clock_starts}, "
            f"running roughly {CHARGEBACK_WINDOW_DAYS} days")
        if ledger.authorization_expires_in_days:
            st.markdown(
                f"- The authorization lapses in about "
                f"{ledger.authorization_expires_in_days} days unless it is "
                f"captured or re taken")

        st.subheader("Both methods, same order")
        for row in capture_comparison(f"{amount:.2f}"):
            st.markdown(
                f"- **{row.capture_method}**: {row.net_cash_today} to the "
                f"bank today, {row.deferred_revenue} of liability, "
                f"{row.revenue_recognised_today} recognised as revenue")
        st.caption(
            f"An ordinary authorization runs about {AUTHORIZATION_DAYS} days, "
            "which does not reach a six week ship date on its own. Confirm "
            "the merchant's own gateway window before promising one."
        )
