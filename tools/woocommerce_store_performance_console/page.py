"""WooCommerce Store Performance and Checkout Console.

Rendered inside the hub app. All logic lives in core.py, which has no
Streamlit dependency.

Every number on this page is produced by a model whose coefficients are
named constants in core.py, and the page says so beside each one. A
modelled figure presented as a measurement of somebody's site is the most
damaging thing a performance report can contain, because a client spends
money on it.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.woocommerce_store_performance_console.core import (
    ASSETS_PER_PLUGIN,
    BUILDERS,
    DEFER_ALREADY,
    DEFER_REMOVE,
    DEFER_SAFE,
    DEFER_UNSAFE,
    ENGINE_VERSION,
    GATEWAYS,
    MODEL_NOTE,
    ORDER_PAID,
    ORDER_PENDING,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    WEBHOOK_STATES,
    WEBHOOK_VERIFIED,
    analyze_builder_bloat,
    benchmark_catalog_queries,
    validate_checkout_gateway,
)

_TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}

_DEFER_STATE = {
    DEFER_SAFE: "pass",
    DEFER_REMOVE: "fail",
    DEFER_UNSAFE: "warn",
    DEFER_ALREADY: "skip",
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


def _score_badge(label: str, score: int, detail: str, tone: str) -> str:
    return (
        f'<div class="app-card {tone}">\n'
        f'  <h4><span class="app-tag {tone}">{esc(label)}</span>\n'
        f"  Modelled mobile score {score} of 100</h4>\n"
        f"  <p>{esc(detail)}</p>\n"
        f"</div>")


def render() -> None:
    inject()
    st.markdown(
        '<div class="app-hero">\n'
        "  <h1>WooCommerce Store Performance and Checkout Console</h1>\n"
        "  <p>Three places a WooCommerce build leaks, each modelled from\n"
        "  named coefficients rather than measured, and each saying so.\n"
        "  Deferring every script is the usual advice and it breaks\n"
        "  WordPress, because inline handlers run before a deferred file\n"
        "  loads. A customer landing on the thank you page is not a\n"
        "  payment, it is a redirect they control. And a cache has two\n"
        "  latencies, so quoting the warm one alone credits a change for\n"
        "  work it did not do.</p>\n"
        "</div>",
        unsafe_allow_html=True)

    bloat_tab, gateway_tab, query_tab = st.tabs(
        ["Asset bloat", "Checkout gateway", "Catalog queries"])

    # -- 1. asset bloat ----------------------------------------------------
    with bloat_tab:
        st.markdown(
            "#### Defer everything is advice that breaks the site\n\n"
            "Core and most plugins print inline script blocks that call "
            "jQuery directly. An inline block runs immediately and a "
            "deferred file has not loaded yet, so the page throws on the "
            "first inline handler, on the pages nobody opens.")

        col_a, col_b = st.columns(2)
        with col_a:
            builder = st.selectbox("Page builder", BUILDERS, index=1)
        with col_b:
            plugins = st.slider("Active plugins", 0, 80, 24, 1)

        bloat = analyze_builder_bloat(builder, plugins)
        tone = _TONE[bloat.severity]

        _kpis([
            (str(bloat.total_requests), "Modelled requests", ""),
            (str(bloat.render_blocking), "Render blocking", "crit"),
            (str(bloat.mobile_score), "Mobile score now",
             "crit" if bloat.mobile_score < 40 else "warn"),
            (f"{bloat.gain:+d}", "Score gain available", "ok"),
        ])

        left, right = st.columns(2)
        with left:
            st.markdown(
                _score_badge(
                    "BEFORE", bloat.mobile_score,
                    f"{bloat.total_requests} request(s), "
                    f"{bloat.render_blocking} of them render blocking.",
                    "crit" if bloat.mobile_score < 40 else "warn"),
                unsafe_allow_html=True)
        with right:
            st.markdown(
                _score_badge(
                    "AFTER", bloat.score_after,
                    f"After deferring {len(bloat.deferrable)} and removing "
                    f"{len(bloat.removable)} named asset(s). Plugin assets "
                    f"are untouched, because nothing here knows what they "
                    f"are.",
                    "ok"),
                unsafe_allow_html=True)

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag {tone}">MODELLED</span>\n'
            f"  {esc(bloat.headline)}</h4>\n"
            f'  <div class="app-ev">{esc(MODEL_NOTE)}</div>\n'
            f"</div>",
            unsafe_allow_html=True)

        st.markdown("#### Every named handle, and what may be done to it")
        rows = "".join(
            _stage(_DEFER_STATE[asset.deferral], asset.handle,
                   f"{asset.kind}, "
                   f"{'render blocking' if asset.render_blocking else 'not render blocking'}. "
                   f"{asset.deferral}. {asset.reason}")
            for asset in bloat.named_assets)
        st.markdown(f'<div class="app-card">{rows}</div>',
                    unsafe_allow_html=True)
        st.caption(
            f"Plus a modelled {bloat.plugin_assets} asset(s) from "
            f"{plugins} plugin(s) at a stated {ASSETS_PER_PLUGIN} each, of "
            f"which {bloat.plugin_blocking} are counted as render blocking. "
            f"Those are an assumption. The handles above are real.")

        for finding in bloat.findings:
            _finding_card(finding)

    # -- 2. checkout gateway -----------------------------------------------
    with gateway_tab:
        st.markdown(
            "#### The worst state in the matrix is the quiet one\n\n"
            "Money captured at the gateway, webhook never delivered, order "
            "sitting in pending, and no alert anywhere, because nothing "
            "failed. A thing that did not happen raises no error.")

        col_c, col_d = st.columns(2)
        with col_c:
            gateway = st.selectbox("Gateway", GATEWAYS, index=0)
        with col_d:
            webhook = st.selectbox("Webhook", WEBHOOK_STATES, index=4)

        check = validate_checkout_gateway(gateway, webhook)
        tone = _TONE[check.severity]
        order_tone = (
            "ok" if check.order_state == ORDER_PAID
            else "crit" if check.order_state == ORDER_PENDING else "warn")

        _kpis([
            (check.order_state, "Order state", order_tone),
            (check.authorization_state, "Authorization", ""),
            ("yes" if check.webhook_verified else "no", "Webhook verified",
             "ok" if check.webhook_verified else "crit"),
            (f"{check.funnel_health} of 100", "Funnel health", order_tone),
        ])

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag {order_tone}">'
            f'{esc(check.order_state)}</span>\n'
            f"  {esc(check.headline)}</h4>\n"
            f'  <div class="app-ev">Signature scheme: '
            f"{esc(check.signature_scheme)}</div>\n"
            f"</div>",
            unsafe_allow_html=True)

        if check.silent_failure:
            st.markdown(
                '<div class="app-card crit">\n'
                '  <h4><span class="app-tag crit">SILENT</span>\n'
                "  Charged, pending, and nobody has been told</h4>\n"
                "  <p>This is the only cell in the grid where the store is "
                "holding a customer's money and reporting no problem at "
                "all. Every other failure is loud enough to be seen.</p>\n"
                "</div>",
                unsafe_allow_html=True)

        st.markdown("#### Every webhook state against this gateway")
        rows = []
        for state in WEBHOOK_STATES:
            outcome = validate_checkout_gateway(gateway, state)
            level = ("pass" if outcome.safe_to_fulfil
                     else "fail" if outcome.silent_failure else "warn")
            rows.append(_stage(
                level, state,
                f"{outcome.order_state}. Funnel health "
                f"{outcome.funnel_health} of 100."))
        st.markdown(f'<div class="app-card">{"".join(rows)}</div>',
                    unsafe_allow_html=True)
        st.caption(
            f"Only {WEBHOOK_VERIFIED} reaches {ORDER_PAID}. Every other "
            f"state needs a human, and one of them needs a human who does "
            f"not yet know there is a problem.")

        for finding in check.findings:
            _finding_card(finding)

    # -- 3. catalog queries ------------------------------------------------
    with query_tab:
        st.markdown(
            "#### A cache has two latencies, and only one gets quoted\n\n"
            "The warm path is what a repeat visitor gets. The cold path is "
            "what a crawler, a first time visitor, and every request after "
            "an expiry get, and adding a cache read makes it slower rather "
            "than faster.")

        col_e, col_f = st.columns(2)
        with col_e:
            products = st.slider("Published products", 100, 60000, 5000, 100)
            joins = st.slider("Meta joins per query", 0, 10, 4, 1)
        with col_f:
            caching = st.toggle("Transient caching on", value=True)
            object_cache = st.toggle("Persistent object cache installed",
                                     value=False)
            hit_rate = st.slider("Cache hit rate", 0.0, 1.0, 0.85, 0.05)

        bench = benchmark_catalog_queries(products, caching,
                                          meta_joins=joins,
                                          object_cache=object_cache,
                                          hit_rate=hit_rate)
        tone = _TONE[bench.severity]

        _kpis([
            (f"{bench.baseline_ms:.2f} ms", "Uncached", "crit"),
            (f"{bench.warm_ms:.2f} ms", "Warm path", "ok"),
            (f"{bench.cold_ms:.2f} ms", "Cold path",
             "crit" if bench.cold_ms > bench.baseline_ms else "warn"),
            (f"{bench.effective_ms:.2f} ms", "Effective at the hit rate",
             "warn"),
        ])

        left, right = st.columns(2)
        with left:
            st.markdown(
                f'<div class="app-card crit">\n'
                f'  <h4><span class="app-tag crit">BEFORE</span>\n'
                f"  {bench.baseline_ms:.2f} ms per query</h4>\n"
                f"  <p>{bench.product_count:,} product(s) with "
                f"{bench.meta_joins} postmeta join(s), no cache.</p>\n"
                f"</div>",
                unsafe_allow_html=True)
        with right:
            st.markdown(
                f'<div class="app-card ok">\n'
                f'  <h4><span class="app-tag ok">AFTER</span>\n'
                f"  {bench.effective_ms:.2f} ms effective</h4>\n"
                f"  <p>A {bench.effective_gain_percent:.0f}% improvement at "
                f"a {bench.hit_rate:.0%} hit rate. Quoting the warm path "
                f"alone would read as "
                f"{bench.warm_only_claim_percent:.0f}%.</p>\n"
                f"</div>",
                unsafe_allow_html=True)

        st.markdown(
            f'<div class="app-card {tone}">\n'
            f'  <h4><span class="app-tag {tone}">MODELLED</span>\n'
            f"  {esc(bench.headline)}</h4>\n"
            f'  <div class="app-ev">{esc(MODEL_NOTE)}</div>\n'
            f"</div>",
            unsafe_allow_html=True)

        st.table({
            "Path": ["Uncached", "Cold, cache miss", "Warm, cache hit",
                     "Effective at the hit rate", "Product lookup table"],
            "Modelled ms": [f"{bench.baseline_ms:.2f}",
                            f"{bench.cold_ms:.2f}",
                            f"{bench.warm_ms:.2f}",
                            f"{bench.effective_ms:.2f}",
                            f"{bench.lookup_table_ms:.2f}"],
        })
        st.caption(
            "The lookup table row is a structural saving rather than a "
            "cached one, so it holds on the cold path too. That is the "
            "difference between fixing the query and hiding it.")

        for finding in bench.findings:
            _finding_card(finding)

    st.markdown(
        f'<div class="app-foot">Engine {esc(ENGINE_VERSION)}. {esc(MODEL_NOTE)} '
        f"The WordPress and WooCommerce handles listed are real, and the "
        f"reason each one can or cannot be deferred is a property of how "
        f"WordPress prints inline scripts rather than an opinion. An order "
        f"reaches {esc(ORDER_PAID)} on a signature verified webhook and on "
        f"nothing else.</div>",
        unsafe_allow_html=True)
