"""Print on Demand Automation Router.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine can sit behind the real WooCommerce webhook.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone

import streamlit as st

from shared.theme import esc, inject
from tools.pod_automation_router.core import (
    CATALOGUE,
    CHANNELS,
    ENGINE_VERSION,
    ERR_OUT_OF_STOCK,
    ERROR_HEADLINE,
    Address,
    LineItem,
    RouterLog,
    WooOrder,
    catalogue_rows,
    find_variant,
    product_by_sku,
    route_order,
    router_kpis,
    sync_targets,
    sync_tracking,
    tracking_rows,
)

STATE = "pod_state"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _state() -> dict:
    if STATE not in st.session_state:
        st.session_state[STATE] = {
            "log": RouterLog(),
            "rng": random.Random(),
            "orders": 0,
            "order": None,
            "fulfilment": None,
            "tracking": None,
        }
    return st.session_state[STATE]


def _receive(sku: str, size: str, quantity: int, origin: str,
             recipient: str, postcode: str) -> None:
    """Build the incoming WooCommerce payload from the controls on screen."""
    state = _state()
    state["orders"] += 1
    state["order"] = WooOrder(
        order_id=f"WC-{state['orders']:05d}",
        origin=origin,
        email=f"{recipient.split()[0].lower() if recipient.split() else 'buyer'}@example.com",
        items=(LineItem(sku, size, int(quantity)),),
        shipping=Address(recipient, "14 Bramble Way", "Leeds", postcode, "GB"),
        placed_at=_now(),
    )
    state["fulfilment"] = None
    state["tracking"] = None


def _route() -> None:
    state = _state()
    order = state["order"]
    if order is None:
        return
    state["fulfilment"] = route_order(order, state["log"], _now())
    state["tracking"] = None


def _ship() -> None:
    state = _state()
    fulfilment, order = state["fulfilment"], state["order"]
    if fulfilment is None or order is None or not fulfilment.routed:
        return
    state["tracking"] = sync_tracking(fulfilment, order, state["log"],
                                      state["rng"], _now())


def render() -> None:
    inject()
    state = _state()
    log: RouterLog = state["log"]

    st.markdown(
        """
<div class="app-hero">
  <h1>Print on Demand Automation Router</h1>
  <p>An order lands from WooCommerce, becomes a Printful request, and the
  tracking number that comes back is pushed to the shop and to the marketplace
  the buyer actually ordered from. When the blank is out of stock the error is
  caught and raised, because an order that fails quietly is found by the buyer
  rather than by you.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Order Simulator**: build an incoming WooCommerce payload.\n"
            "2. **Fulfilment Router**: translate it and submit it.\n"
            "3. **Tracking Sync**: push the tracking number outward.\n"
            "4. Pick a size marked out of stock to see the guard fire."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. WooCommerce, Printful and the "
            "marketplaces are all simulated in this session."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_order, tab_route, tab_track, tab_log = st.tabs(
        ["Order Simulator", "Fulfilment Router", "Tracking Sync", "Router Log"]
    )

    # -----------------------------------------------------------------
    # WooCommerce order simulator
    # -----------------------------------------------------------------
    with tab_order:
        st.markdown("#### Incoming WooCommerce order")
        st.caption(
            "This is the payload the shop posts to the webhook the moment an "
            "order is paid."
        )

        c1, c2, c3 = st.columns([2, 1, 1])
        sku = c1.selectbox(
            "Product SKU",
            [p.sku for p in CATALOGUE],
            format_func=lambda s: f"{s} {product_by_sku(s).name}",
            key="pod_sku",
        )
        product = product_by_sku(sku)
        size = c2.selectbox("Size", list(product.sizes), key="pod_size")
        quantity = c3.number_input("Quantity", min_value=1, max_value=10,
                                   value=1, step=1, key="pod_qty")

        variant = find_variant(sku, size)
        if variant is not None and variant.blank_stock == 0:
            st.warning(
                f"The blank for {sku} in size {size} is out of stock. Route this "
                f"order to watch the guard catch the Printful error rather than "
                f"let it pass silently."
            )

        c4, c5, c6 = st.columns([1, 1, 1])
        origin = c4.selectbox("Sales channel", list(CHANNELS), key="pod_origin")
        recipient = c5.text_input("Recipient name", value="Amara Okafor",
                                  key="pod_recipient")
        postcode = c6.text_input("Postcode", value="LS6 2QT", key="pod_postcode")

        st.caption(
            f"Tracking for a {origin} order is pushed to "
            f"{', '.join(sync_targets(origin))}."
        )

        st.button("Generate WooCommerce order", type="primary", on_click=_receive,
                  args=(sku, size, int(quantity), origin, recipient, postcode),
                  key="pod_receive_btn")

        order = state["order"]
        if order is not None:
            st.markdown(
                f"""
<div class="app-card ok">
  <h4><span class="app-tag ok">Received</span>{esc(order.order_id)} from
  {esc(order.origin)}</h4>
  <p>{len(order.items)} line(s), {order.order_total:.2f} total, shipping to
  {esc(order.shipping.name)}, {esc(order.shipping.city)}
  {esc(order.shipping.postcode)}.</p>
</div>
""",
                unsafe_allow_html=True,
            )
            st.markdown("##### The raw payload")
            st.code(json.dumps({
                "id": order.order_id,
                "origin": order.origin,
                "billing": {"email": order.email},
                "line_items": [
                    {"sku": i.sku, "size": i.size, "quantity": i.quantity,
                     "total": f"{i.total:.2f}"} for i in order.items
                ],
                "shipping": {
                    "first_name": order.shipping.name,
                    "address_1": order.shipping.address1,
                    "city": order.shipping.city,
                    "postcode": order.shipping.postcode,
                    "country": order.shipping.country_code,
                },
            }, indent=2), language="json")
        else:
            st.info("No order yet. Generate one to start the flow.")

        st.markdown("##### Catalogue and blank stock")
        st.dataframe(catalogue_rows(), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Printful fulfilment router
    # -----------------------------------------------------------------
    with tab_route:
        order = state["order"]
        if order is None:
            st.info("Generate an order on the first tab, then route it here.")
        else:
            st.markdown("#### Translate and submit to Printful")
            st.caption(
                "WooCommerce speaks in SKUs and sizes. Printful speaks in "
                "numeric variant ids. The translation is where an order is "
                "either made fulfillable or refused."
            )
            st.button("Route to Printful", type="primary", on_click=_route,
                      key="pod_route_btn")

            fulfilment = state["fulfilment"]
            if fulfilment is not None:
                if fulfilment.routed:
                    st.markdown(
                        f"""
<div class="app-card ok">
  <h4><span class="app-tag ok">Routed</span>{esc(fulfilment.order_id)}</h4>
  <div class="app-ev">{esc(fulfilment.printful_order_id)}</div>
  <p>{esc(fulfilment.message)}</p>
</div>
""",
                        unsafe_allow_html=True,
                    )
                    st.markdown("##### The Printful request")
                    st.code(json.dumps(fulfilment.request.as_payload(), indent=2),
                            language="json")
                    st.markdown("##### Line by line translation")
                    st.dataframe(
                        [
                            {"SKU": item.sku, "Size": item.size,
                             "Printful variant": item.variant_id,
                             "Quantity": item.quantity,
                             "Retail price": item.retail_price}
                            for item in fulfilment.request.items
                        ],
                        width="stretch", hide_index=True,
                    )
                else:
                    headline = ERROR_HEADLINE.get(fulfilment.error_code,
                                                  "Order refused")
                    st.markdown(
                        f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">{esc(headline)}</span>
  {esc(fulfilment.order_id)}</h4>
  <div class="app-ev">{esc(fulfilment.error_code)}</div>
  <p>{esc(fulfilment.message)}</p>
</div>
""",
                        unsafe_allow_html=True,
                    )
                    st.error(
                        f"{fulfilment.error_code}: nothing was submitted to "
                        f"Printful, and the refusal is in the router log as an "
                        f"alert. The failure mode this guards against is the "
                        f"silent one, where the shop shows the order as "
                        f"processing and nobody notices until the buyer asks."
                    )

    # -----------------------------------------------------------------
    # Tracking sync
    # -----------------------------------------------------------------
    with tab_track:
        fulfilment = state["fulfilment"]
        order = state["order"]
        if fulfilment is None or not fulfilment.routed:
            st.info(
                "Tracking appears once an order has been accepted by Printful. "
                "An order that was refused never ships, so it has nothing to "
                "synchronise."
            )
        else:
            st.markdown("#### Shipment webhook")
            st.caption(
                "Printful posts back when the parcel leaves. The tracking "
                "number goes to WooCommerce, and to the marketplace the buyer "
                "ordered from, because that is where they will look for it."
            )
            st.button("Simulate shipment webhook", type="primary", on_click=_ship,
                      key="pod_ship_btn")

            tracking = state["tracking"]
            if tracking is not None:
                st.markdown(
                    f"""
<div class="app-card ok">
  <h4><span class="app-tag ok">Shipped</span>{esc(tracking.order_id)}</h4>
  <div class="app-ev">{esc(tracking.tracking_number)} &nbsp; {esc(tracking.carrier)}</div>
  <p>Pushed to {esc(', '.join(tracking.channels))} at
  {esc(tracking.shipped_at)}.</p>
</div>
""",
                    unsafe_allow_html=True,
                )
                st.dataframe(tracking_rows(tracking), width="stretch",
                             hide_index=True)
                st.caption(
                    f"Not pushed to "
                    f"{', '.join(c for c in CHANNELS if c not in tracking.channels) or 'nothing else'}"
                    f", because those channels never saw this order and their "
                    f"APIs would reject the update."
                )

    # -----------------------------------------------------------------
    # Router log
    # -----------------------------------------------------------------
    with tab_log:
        kpis = router_kpis(log)
        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{kpis.received}</div>
    <div class="l">Orders received</div></div>
  <div class="app-kpi ok"><div class="n">{kpis.routed}</div>
    <div class="l">Routed to Printful</div></div>
  <div class="app-kpi crit"><div class="n">{kpis.alerts}</div>
    <div class="l">Alerts raised</div></div>
  <div class="app-kpi ok"><div class="n">{kpis.tracked}</div>
    <div class="l">Tracking pushes</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        if log.alerts:
            st.markdown("#### Alerts")
            for event in log.alerts[-5:]:
                st.markdown(
                    f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">Alert</span>{esc(event.order_id)}
  &middot; {esc(event.stage)}</h4>
  <p>{esc(event.detail)}</p>
</div>
""",
                    unsafe_allow_html=True,
                )
        elif log.events:
            st.success(
                "No alerts. Every order either routed cleanly or has not been "
                "routed yet."
            )

        st.markdown("#### Full router log")
        if log.events:
            st.dataframe(log.rows(), width="stretch", hide_index=True)
        else:
            st.info("The log is empty until the first order is routed.")

    st.markdown(
        f"""
<div class="app-foot">
Print on Demand Automation Router, engine version {ENGINE_VERSION}. A simulator:
no WooCommerce, Printful or marketplace API is contacted and nothing you enter
leaves this session. A refused order is never partially submitted, because a
half fulfilled order ships an incomplete parcel with nothing in the log to
explain it.
</div>
""",
        unsafe_allow_html=True,
    )
