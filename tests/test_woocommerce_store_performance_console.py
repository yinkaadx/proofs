"""Tests for the WooCommerce Store Performance and Checkout Console.

The bloat tests are about the deferral classification rather than about
the score, because the score is a model and the classification is a
property of how WordPress prints inline scripts. The benchmark tests
check that the cold path is reported as slower under caching, since that
is the fact a warm only quote hides.
"""

from __future__ import annotations

import itertools
import pathlib
import subprocess
import sys

import pytest

from tools.woocommerce_store_performance_console.core import (
    ASSETS_PER_PLUGIN,
    BUILDERS,
    BUILDER_CLASSIC,
    BUILDER_ELEMENTOR_FREE,
    BUILDER_ELEMENTOR_PRO,
    DEFER_ALREADY,
    DEFER_REMOVE,
    DEFER_SAFE,
    DEFER_UNSAFE,
    ENGINE_VERSION,
    GATEWAYS,
    GATEWAY_BANK_TRANSFER,
    GATEWAY_STRIPE,
    KIND_CSS,
    KIND_JS,
    MODEL_NOTE,
    ORDER_AWAITING,
    ORDER_ON_HOLD,
    ORDER_PAID,
    ORDER_PENDING,
    SCORE_CEILING,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    WEBHOOK_BAD_SIGNATURE,
    WEBHOOK_MISSING,
    WEBHOOK_REPLAYED,
    WEBHOOK_STATES,
    WEBHOOK_UNSIGNED,
    WEBHOOK_VERIFIED,
    analyze_builder_bloat,
    benchmark_catalog_queries,
    request_cost,
    validate_checkout_gateway,
)
from tools.registry import all_tools

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL_DIR = ROOT / "tools" / "woocommerce_store_performance_console"


# ---------------------------------------------------------------------------
# Builder and plugin bloat
# ---------------------------------------------------------------------------

def test_jquery_is_never_classified_as_safe_to_defer():
    """Deferring it throws on the first inline handler, on the pages a
    developer does not open."""
    for builder in BUILDERS:
        bloat = analyze_builder_bloat(builder, 10)
        assert "jquery-core" in bloat.undeferrable, builder
        assert "jquery-core" not in bloat.deferrable, builder
        assert "JQUERY" in " ".join(f.code for f in bloat.findings), builder


def test_the_undeferrable_finding_is_critical_and_not_advice():
    bloat = analyze_builder_bloat(BUILDER_ELEMENTOR_PRO, 20)
    jquery = [f for f in bloat.findings if f.code == "BLT-JQUERY"]
    assert jquery
    assert jquery[0].severity == SEVERITY_CRITICAL


def test_the_sitewide_woocommerce_assets_are_marked_for_removal():
    bloat = analyze_builder_bloat(BUILDER_CLASSIC, 0)
    for handle in ("woocommerce-general", "woocommerce-layout",
                   "wc-cart-fragments", "select2"):
        assert handle in bloat.removable, handle


def test_cart_fragments_is_called_out_as_defeating_page_caching():
    bloat = analyze_builder_bloat(BUILDER_CLASSIC, 5)
    assert "BLT-FRAGMENTS" in {f.code for f in bloat.findings}


def test_every_named_asset_carries_a_deferral_verdict_and_a_reason():
    valid = {DEFER_SAFE, DEFER_REMOVE, DEFER_UNSAFE, DEFER_ALREADY}
    for builder in BUILDERS:
        for asset in analyze_builder_bloat(builder, 0).named_assets:
            assert asset.deferral in valid, asset.handle
            assert asset.reason.strip(), asset.handle
            assert asset.kind in (KIND_CSS, KIND_JS), asset.handle


def test_the_three_verdict_lists_never_overlap():
    for builder in BUILDERS:
        bloat = analyze_builder_bloat(builder, 12)
        assert not set(bloat.deferrable) & set(bloat.removable)
        assert not set(bloat.deferrable) & set(bloat.undeferrable)
        assert not set(bloat.removable) & set(bloat.undeferrable)


def test_the_request_count_is_named_assets_plus_the_stated_assumption():
    for plugins in (0, 7, 24, 60):
        bloat = analyze_builder_bloat(BUILDER_ELEMENTOR_FREE, plugins)
        assert bloat.plugin_assets == plugins * ASSETS_PER_PLUGIN
        assert bloat.total_requests == (len(bloat.named_assets)
                                        + bloat.plugin_assets)


def test_the_score_never_saturates_and_stays_ordered():
    """A linear subtraction reaches zero on any real store, and a score
    that reads zero for every site distinguishes nothing."""
    for plugins in (0, 12, 24, 45, 80, 200):
        exact = [analyze_builder_bloat(b, plugins).mobile_score_exact
                 for b in BUILDERS]
        assert all(0.0 < score <= SCORE_CEILING for score in exact), plugins
        assert len(set(exact)) == len(BUILDERS), plugins


def test_the_displayed_score_still_separates_builders_at_a_real_plugin_count():
    """Past a heavy enough plugin load the builder genuinely stops
    mattering, and the engine says so rather than pretending otherwise."""
    for plugins in (0, 12, 24, 45):
        scores = {analyze_builder_bloat(b, plugins).mobile_score
                  for b in BUILDERS}
        assert len(scores) > 1, plugins
    heavy = analyze_builder_bloat(BUILDER_ELEMENTOR_PRO, 80)
    assert "BLT-DOMINANCE" in {f.code for f in heavy.findings}
    light = analyze_builder_bloat(BUILDER_ELEMENTOR_PRO, 2)
    assert "BLT-DOMINANCE" not in {f.code for f in light.findings}


def test_a_leaner_builder_never_scores_worse_than_a_heavier_one():
    for plugins in (0, 15, 40):
        classic = analyze_builder_bloat(BUILDER_CLASSIC, plugins)
        pro = analyze_builder_bloat(BUILDER_ELEMENTOR_PRO, plugins)
        assert classic.mobile_score > pro.mobile_score, plugins


def test_more_plugins_never_improve_the_score():
    previous = None
    for plugins in range(0, 61, 5):
        score = analyze_builder_bloat(BUILDER_ELEMENTOR_FREE,
                                      plugins).mobile_score
        if previous is not None:
            assert score <= previous, plugins
        previous = score


def test_the_fix_list_always_improves_the_modelled_score():
    for builder, plugins in itertools.product(BUILDERS, (0, 20, 50)):
        bloat = analyze_builder_bloat(builder, plugins)
        assert bloat.score_after >= bloat.mobile_score, (builder, plugins)
        assert bloat.gain >= 0


def test_the_score_is_always_declared_as_a_model():
    for builder in BUILDERS:
        codes = {f.code for f in analyze_builder_bloat(builder, 3).findings}
        assert "BLT-MODEL" in codes, builder


def test_the_request_cost_weights_blocking_above_non_blocking():
    assert request_cost(1, 0) > request_cost(0, 1)
    assert request_cost(0, 0) == 0.0
    assert request_cost(-5, -5) == 0.0


def test_an_unknown_builder_or_negative_plugin_count_raises():
    with pytest.raises(ValueError):
        analyze_builder_bloat("Bricks", 10)
    with pytest.raises(ValueError):
        analyze_builder_bloat(BUILDER_CLASSIC, -1)


# ---------------------------------------------------------------------------
# Checkout gateway
# ---------------------------------------------------------------------------

def test_only_a_verified_webhook_marks_an_order_payable():
    for gateway in GATEWAYS:
        for state in WEBHOOK_STATES:
            check = validate_checkout_gateway(gateway, state)
            if check.safe_to_fulfil:
                assert state == WEBHOOK_VERIFIED, gateway
                assert check.webhook_verified, gateway
                assert gateway != GATEWAY_BANK_TRANSFER


def test_the_missing_webhook_is_the_only_silent_failure():
    """The only cell where the store holds a customer's money and reports
    no problem at all."""
    silent = {(gateway, state) for gateway in GATEWAYS
              for state in WEBHOOK_STATES
              if validate_checkout_gateway(gateway, state).silent_failure}
    assert silent == {(gateway, WEBHOOK_MISSING) for gateway in GATEWAYS
                      if gateway != GATEWAY_BANK_TRANSFER}


def test_a_missing_webhook_leaves_the_order_pending_after_a_capture():
    check = validate_checkout_gateway(GATEWAY_STRIPE, WEBHOOK_MISSING)
    assert check.order_state == ORDER_PENDING
    assert not check.safe_to_fulfil
    assert check.silent_failure
    assert "PAY-SILENT" in {f.code for f in check.findings}
    assert check.severity == SEVERITY_CRITICAL


def test_an_unverified_webhook_is_treated_as_an_anonymous_request():
    for state in (WEBHOOK_UNSIGNED, WEBHOOK_BAD_SIGNATURE,
                  WEBHOOK_REPLAYED):
        check = validate_checkout_gateway(GATEWAY_STRIPE, state)
        assert check.order_state == ORDER_ON_HOLD, state
        assert not check.webhook_verified, state
        assert check.severity == SEVERITY_CRITICAL, state


def test_every_verdict_states_that_the_redirect_is_not_a_payment():
    """An integration that marks an order paid on the return URL can be
    paid with a bookmark."""
    for gateway, state in itertools.product(GATEWAYS, WEBHOOK_STATES):
        codes = {f.code for f in
                 validate_checkout_gateway(gateway, state).findings}
        assert "PAY-REDIRECT" in codes, (gateway, state)


def test_bank_transfer_never_reaches_paid_whatever_the_webhook_says():
    """No webhook exists for it, so no webhook state can change it."""
    for state in WEBHOOK_STATES:
        check = validate_checkout_gateway(GATEWAY_BANK_TRANSFER, state)
        assert check.order_state == ORDER_AWAITING, state
        assert not check.safe_to_fulfil, state
        assert not check.silent_failure, state


def test_the_verified_path_scores_the_healthiest_funnel():
    scores = {state: validate_checkout_gateway(
        GATEWAY_STRIPE, state).funnel_health for state in WEBHOOK_STATES}
    assert scores[WEBHOOK_VERIFIED] == max(scores.values())
    assert scores[WEBHOOK_MISSING] == min(scores.values())


def test_every_gateway_names_its_signature_scheme():
    for gateway in GATEWAYS:
        check = validate_checkout_gateway(gateway, WEBHOOK_VERIFIED)
        assert check.signature_scheme.strip(), gateway


def test_an_unknown_gateway_or_webhook_state_raises():
    with pytest.raises(ValueError):
        validate_checkout_gateway("Klarna", WEBHOOK_VERIFIED)
    with pytest.raises(ValueError):
        validate_checkout_gateway(GATEWAY_STRIPE, "probably fine")


# ---------------------------------------------------------------------------
# Catalog query benchmarking
# ---------------------------------------------------------------------------

def test_caching_makes_the_cold_path_slower_not_faster():
    """The cache read is added to the miss, and the cold path is what a
    crawler and a first time visitor get."""
    for count in (200, 5000, 40000):
        bench = benchmark_catalog_queries(count, True)
        assert bench.cold_ms > bench.baseline_ms, count


def test_the_warm_only_claim_always_overstates_the_real_gain():
    """The number that gets quoted against the number a visitor gets."""
    for count in (200, 5000, 40000):
        for object_cache in (False, True):
            bench = benchmark_catalog_queries(count, True,
                                              object_cache=object_cache)
            assert bench.warm_only_claim_percent > \
                bench.effective_gain_percent, (count, object_cache)
            assert "QRY-TWOPATHS" in {f.code for f in bench.findings}


def test_the_effective_latency_sits_between_the_warm_and_cold_paths():
    for hit_rate in (0.0, 0.25, 0.5, 0.85, 1.0):
        bench = benchmark_catalog_queries(8000, True, hit_rate=hit_rate)
        assert bench.warm_ms <= bench.effective_ms <= bench.cold_ms
    assert benchmark_catalog_queries(8000, True, hit_rate=1.0).effective_ms \
        == pytest.approx(benchmark_catalog_queries(8000, True).warm_ms)


def test_a_database_transient_is_slower_than_an_object_cache():
    database = benchmark_catalog_queries(5000, True, object_cache=False)
    memory = benchmark_catalog_queries(5000, True, object_cache=True)
    assert database.warm_ms > memory.warm_ms
    assert "QRY-DBTRANSIENT" in {f.code for f in database.findings}
    assert "QRY-DBTRANSIENT" not in {f.code for f in memory.findings}


def test_the_stampede_is_raised_only_where_there_is_no_object_cache():
    database = benchmark_catalog_queries(5000, True, object_cache=False)
    memory = benchmark_catalog_queries(5000, True, object_cache=True)
    assert "QRY-STAMPEDE" in {f.code for f in database.findings}
    assert "QRY-STAMPEDE" not in {f.code for f in memory.findings}


def test_latency_rises_with_the_catalog_and_with_the_join_count():
    previous = 0.0
    for count in (100, 1000, 10000, 50000):
        current = benchmark_catalog_queries(count, False).baseline_ms
        assert current > previous, count
        previous = current
    joins = [benchmark_catalog_queries(5000, False, meta_joins=n).baseline_ms
             for n in range(0, 8)]
    assert joins == sorted(joins)


def test_the_lookup_table_beats_the_meta_path_on_the_cold_path():
    """A structural saving rather than a cached one, so it holds where a
    cache does not."""
    for count in (500, 5000, 40000):
        bench = benchmark_catalog_queries(count, False)
        assert bench.lookup_table_ms < bench.baseline_ms, count
        assert "QRY-LOOKUP" in {f.code for f in bench.findings}


def test_no_caching_reports_one_latency_for_all_three_paths():
    bench = benchmark_catalog_queries(5000, False)
    assert bench.cold_ms == bench.warm_ms == bench.effective_ms
    assert bench.effective_gain_percent == 0.0


def test_every_benchmark_declares_itself_a_model():
    for count in (100, 9000):
        for caching in (True, False):
            codes = {f.code for f in
                     benchmark_catalog_queries(count, caching).findings}
            assert "QRY-MODEL" in codes, (count, caching)
    assert "not a measurement" in MODEL_NOTE


def test_invalid_benchmark_inputs_raise():
    with pytest.raises(ValueError):
        benchmark_catalog_queries(-1, True)
    with pytest.raises(ValueError):
        benchmark_catalog_queries(100, True, meta_joins=-1)
    with pytest.raises(ValueError):
        benchmark_catalog_queries(100, True, hit_rate=1.5)


# ---------------------------------------------------------------------------
# House invariants
# ---------------------------------------------------------------------------

def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    """The engine has to run outside the app, or it is not an engine."""
    script = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.woocommerce_store_performance_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT))
    done = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    for name in ("core.py", "page.py"):
        text = (TOOL_DIR / name).read_text(encoding="utf-8")
        assert "—" not in text, name
        assert "–" not in text, name


def test_the_tool_is_registered_with_a_unique_icon():
    tools = [t for t in all_tools() if not t.key.startswith("synthetic-")]
    keys = [t.key for t in tools]
    assert "woocommerce-store-performance-console" in keys
    icons = [t.icon for t in tools]
    assert len(icons) == len(set(icons))
    assert SEVERITY_OK != SEVERITY_CRITICAL
