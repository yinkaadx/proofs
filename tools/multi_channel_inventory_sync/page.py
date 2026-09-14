"""Multi Channel Inventory Sync Engine.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine can sit behind the real marketplace webhooks.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.multi_channel_inventory_sync.core import (
    AMAZON,
    BROADCAST_LABEL,
    CHANNELS,
    EBAY,
    ENGINE_VERSION,
    ERR_INSUFFICIENT_STOCK,
    ERR_INVALID_QUANTITY,
    ERR_UNKNOWN_LISTING,
    FAILED,
    IN_STOCK,
    LOW_STOCK,
    OUT_OF_STOCK,
    PUSHED,
    SAMPLE_PRODUCTS,
    SHOPIFY,
    InventorySync,
    Order,
    broadcast_rows,
    product_by_sku,
    stock_status,
    sync_kpis,
)

STATE = "mcis_state"

STATUS_TONE = {OUT_OF_STOCK: "crit", LOW_STOCK: "warn", IN_STOCK: "ok"}
BROADCAST_TONE = {PUSHED: "pass", FAILED: "fail"}
CHANNEL_ICON = {AMAZON: "\U0001F4E6", EBAY: "\U0001F3F7", SHOPIFY: "\U0001F6D2"}

ERROR_HEADLINE = {
    ERR_INSUFFICIENT_STOCK: "Negative stock guard tripped",
    ERR_UNKNOWN_LISTING: "Unmapped listing",
    ERR_INVALID_QUANTITY: "Invalid quantity",
}


def _state() -> dict:
    if STATE not in st.session_state:
        st.session_state[STATE] = {
            "ledger": InventorySync(),
            "last": None,
            "orders": 0,
        }
    return st.session_state[STATE]


def _sell(channel: str, sku: str, quantity: int) -> None:
    """Place an order. Runs as a button callback, before the script re-executes.

    The SKU and quantity are bound at render time and passed in, rather than
    read back from session_state. A selectbox with a format_func stores its
    FORMATTED LABEL under its key, not the raw option, so reading the key here
    would hand this function a display string instead of a SKU.
    """
    state = _state()
    ledger: InventorySync = state["ledger"]
    product = product_by_sku(sku)
    listing = product.listing_for(channel) if product else None
    if listing is None:
        state["last"] = None
        return
    state["orders"] += 1
    order = Order(f"ORD-{state['orders']:04d}", channel, listing.channel_sku, quantity)
    state["last"] = ledger.place_order(order)


def _restock(sku: str, quantity: int) -> None:
    _state()["ledger"].restock(sku, quantity)


def _replay() -> None:
    _state()["ledger"].retry_failed()


def _reset() -> None:
    for widget_key in ("mcis_sku", "mcis_qty", "mcis_restock_sku",
                       "mcis_restock_qty"):
        st.session_state.pop(widget_key, None)
    st.session_state.pop(STATE, None)


def render() -> None:
    inject()
    state = _state()
    ledger: InventorySync = state["ledger"]

    st.markdown(
        """
<div class="app-hero">
  <h1>Multi Channel Inventory Sync Engine</h1>
  <p>One stock figure at the centre, three marketplaces that each call the same
  product something different. Sell on any channel and the engine deducts once,
  then pushes the new quantity to the other two. Ask for more than exists and it
  refuses, because stock below zero is a promise no warehouse can keep.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Channel Sales**: pick a product and sell from a channel.\n"
            "2. **Sync Broadcast**: watch the new figure reach the other two.\n"
            "3. Push a channel offline and sell again to see a failed sync.\n"
            "4. **Status and Errors**: read the audit trail."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live marketplace. Mapping, deduction and "
            "broadcasting run in this session against a sample catalogue."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_ledger, tab_sales, tab_broadcast, tab_log = st.tabs(
        ["Inventory Ledger", "Channel Sales", "Sync Broadcast", "Status and Errors"]
    )

    # -----------------------------------------------------------------
    # Central ledger
    # -----------------------------------------------------------------
    with tab_ledger:
        kpis = sync_kpis(ledger)
        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{kpis.total_units}</div>
    <div class="l">Units in stock</div></div>
  <div class="app-kpi ok"><div class="n">{kpis.oversells}</div>
    <div class="l">Oversells</div></div>
  <div class="app-kpi ok"><div class="n">{kpis.broadcasts_pushed}</div>
    <div class="l">Broadcasts pushed</div></div>
  <div class="app-kpi crit"><div class="n">{kpis.broadcasts_failed}</div>
    <div class="l">Broadcasts failed</div></div>
</div>
""",
            unsafe_allow_html=True,
        )
        st.markdown("#### Central SKU ledger")
        st.caption(
            "One row per product. The central SKU is the truth, and the three "
            "channel columns are what each marketplace calls the same item."
        )
        st.dataframe(ledger.ledger_rows(), width="stretch", hide_index=True)

        if kpis.oversells == 0:
            st.success(
                "No SKU has ever gone below zero. The guard blocks the order "
                "before anything is written, so a refused sale leaves the ledger "
                "exactly as it was."
            )

        low = [p for p in SAMPLE_PRODUCTS
               if stock_status(ledger.available(p.sku)) != IN_STOCK]
        if low:
            names = ", ".join(f"{p.sku} ({ledger.available(p.sku)})" for p in low)
            st.warning(f"Low or out of stock, worth restocking before they sell out: {names}.")

        st.markdown("##### Receive stock")
        c1, c2, c3 = st.columns([2, 1, 1])
        restock_sku = c1.selectbox(
            "Product to restock",
            [p.sku for p in SAMPLE_PRODUCTS],
            format_func=lambda s: f"{s} {product_by_sku(s).title}",
            key="mcis_restock_sku",
        )
        restock_qty = c2.number_input("Units received", min_value=1, max_value=500,
                                      value=10, step=1, key="mcis_restock_qty")
        c3.button("Receive into stock", width="stretch", on_click=_restock,
                  args=(restock_sku, int(restock_qty)))

    # -----------------------------------------------------------------
    # Channel sale simulators
    # -----------------------------------------------------------------
    with tab_sales:
        st.markdown("#### Trigger an order")
        st.caption(
            "Each button is the webhook a marketplace fires when an order is "
            "placed. The engine maps that channel's own SKU back to the central "
            "one before it touches stock."
        )

        # Option labels stay constant. Streamlit stores a selectbox's FORMATTED
        # LABEL under its key, so folding a live stock count into the label made
        # the stored value go stale the moment stock moved, and the widget fell
        # back to the first product. The count belongs beside the control, not
        # inside its options.
        skus = [p.sku for p in SAMPLE_PRODUCTS]
        sku = st.selectbox(
            "Product to sell",
            skus,
            format_func=lambda s: f"{s} {product_by_sku(s).title}",
            key="mcis_sku",
        )
        on_hand = ledger.available(sku)
        st.caption(f"{on_hand} in stock at the centre. {stock_status(on_hand)}.")
        quantity = st.number_input("Quantity ordered", min_value=1, max_value=50,
                                   value=1, step=1, key="mcis_qty")

        product = product_by_sku(sku)
        st.markdown("##### Sell from a channel")
        columns = st.columns(len(CHANNELS))
        for column, channel in zip(columns, CHANNELS):
            listing = product.listing_for(channel) if product else None
            with column:
                if listing is None:
                    st.button(f"{CHANNEL_ICON[channel]} {channel}", disabled=True,
                              width="stretch", key=f"mcis_btn_{channel}",
                              help=f"{sku} has no {channel} listing, so no order "
                                   f"can arrive from there.")
                    st.caption("Not listed on this channel.")
                else:
                    st.button(f"{CHANNEL_ICON[channel]} Order from {channel}",
                              width="stretch", key=f"mcis_btn_{channel}",
                              type="primary", on_click=_sell,
                              args=(channel, sku, int(quantity)))
                    st.caption(f"Listed as {listing.channel_sku}")

        st.markdown("##### Channel health")
        st.caption(
            "Push a channel offline, then sell again. The sale still completes "
            "and the centre stays correct, but that channel's update fails and "
            "is queued, which is what fills the error log."
        )
        health_columns = st.columns(len(CHANNELS))
        for column, channel in zip(health_columns, CHANNELS):
            with column:
                reachable = st.toggle(
                    f"{channel} reachable",
                    value=channel not in ledger.degraded,
                    key=f"mcis_health_{channel}",
                )
                if reachable:
                    ledger.degraded.discard(channel)
                else:
                    ledger.degraded.add(channel)

        if ledger.degraded:
            offline = ", ".join(sorted(ledger.degraded))
            st.warning(f"Currently unreachable: {offline}. Updates to these will fail and queue.")
            st.button("Replay queued updates", on_click=_replay,
                      key="mcis_replay_degraded")
        else:
            if any(e.kind == "broadcast.failed" for e in ledger.events):
                st.button("Replay queued updates", type="primary",
                          on_click=_replay, key="mcis_replay")

        st.button("Reset the ledger", on_click=_reset)

    # -----------------------------------------------------------------
    # Sync broadcast
    # -----------------------------------------------------------------
    with tab_broadcast:
        result = state["last"]
        if result is None:
            st.info(
                "No order yet. Trigger one from the Channel Sales tab and the "
                "calculation and every push will appear here."
            )
        elif not result.accepted:
            headline = ERROR_HEADLINE.get(result.error_code, "Order blocked")
            st.markdown(
                f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">{esc(headline)}</span>
  {esc(result.order.channel)} order {esc(result.order.order_id)}</h4>
  <div class="app-ev">{esc(result.error_code)}</div>
  <p>{esc(result.message)}</p>
</div>
""",
                unsafe_allow_html=True,
            )
            st.error(
                f"{result.error_code}: nothing was written. Central stock for "
                f"{result.sku or 'the requested item'} is still "
                f"{result.stock_after}, and no channel was told anything, because "
                f"a partially applied order is worse than a refused one."
            )
        else:
            st.markdown(
                f"""
<div class="app-card ok">
  <h4><span class="app-tag ok">Accepted</span>
  {esc(result.order.channel)} order {esc(result.order.order_id)}</h4>
  <div class="app-ev">{esc(result.sku)} &nbsp; {result.stock_before} &minus;
  {result.order.quantity} = {result.stock_after}</div>
  <p>{esc(result.message)} Resolved and broadcast in
  {result.elapsed_ms:.2f} ms.</p>
</div>
""",
                unsafe_allow_html=True,
            )

            st.markdown("##### Broadcast to the remaining channels")
            rows = "".join(
                f'<div class="app-stage">'
                f'<div class="s {BROADCAST_TONE.get(b.status, "skip")}">'
                f'{esc(BROADCAST_LABEL.get(b.status, b.status)).upper()}</div>'
                f'<div class="n">{esc(b.channel)} &middot; {esc(b.channel_sku)}</div>'
                f'<div class="d">Set to <strong>{b.quantity}</strong>. '
                f'{esc(b.detail)}</div></div>'
                for b in result.broadcasts
            )
            st.markdown(f'<div class="app-card">{rows}</div>', unsafe_allow_html=True)

            if result.failed:
                st.warning(
                    f"{result.failed} channel update(s) failed and are queued. The "
                    f"centre is correct at {result.stock_after}, but those channels "
                    f"still advertise the old figure until the queue replays."
                )
            else:
                st.success(
                    f"All {result.pushed} other channel(s) now show "
                    f"{result.stock_after}. The selling channel is not written back "
                    f"to, because it already decremented its own copy when it took "
                    f"the order."
                )

            st.dataframe(broadcast_rows(result), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Status and errors
    # -----------------------------------------------------------------
    with tab_log:
        kpis = sync_kpis(ledger)
        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{kpis.orders_accepted}</div>
    <div class="l">Orders accepted</div></div>
  <div class="app-kpi warn"><div class="n">{kpis.orders_blocked}</div>
    <div class="l">Orders blocked</div></div>
  <div class="app-kpi ok"><div class="n">{kpis.broadcasts_pushed}</div>
    <div class="l">Broadcasts pushed</div></div>
  <div class="app-kpi crit"><div class="n">{kpis.broadcasts_failed}</div>
    <div class="l">Broadcasts failed</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        failures = ledger.failures
        st.markdown("#### Errors and blocked orders")
        if failures:
            for event in failures[-6:]:
                st.markdown(
                    f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">{esc(event.status)}</span>
  {esc(event.channel)} &middot; {esc(event.sku)}</h4>
  <p>{esc(event.detail)}</p>
</div>
""",
                    unsafe_allow_html=True,
                )
        else:
            st.success(
                "No failures recorded. Every order either completed and "
                "broadcast cleanly, or was refused before it could corrupt the "
                "ledger."
            )

        st.markdown("#### Full audit trail")
        if ledger.events:
            st.dataframe(ledger.event_rows(), width="stretch", hide_index=True)
        else:
            st.info("The trail is empty until the first order is placed.")

    st.markdown(
        f"""
<div class="app-foot">
Multi Channel Inventory Sync Engine, engine version {ENGINE_VERSION}. A
simulator: no Amazon, eBay or Shopify account is contacted and nothing you enter
leaves this session. Stock is deducted once at the centre and broadcast outward,
which is the only ordering that keeps three marketplaces agreeing about one
warehouse.
</div>
""",
        unsafe_allow_html=True,
    )
