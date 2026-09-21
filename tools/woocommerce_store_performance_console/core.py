"""WooCommerce store performance, checkout, and catalog query engine.

No Streamlit import lives in this file.

Everything here that produces a number produces it from a model whose
coefficients are named constants in this file, and every result says so.
That is deliberate. A page that prints a millisecond figure next to a
customer's domain reads as a measurement of their site, and a modelled
figure dressed as a measurement is the single most damaging thing a
performance report can contain, because the client makes a spending
decision on it.

Three failures shape the file:

* the usual advice is to defer every script. Deferring jQuery breaks
  WordPress, because core and most plugins print inline script blocks
  that call jQuery directly, and an inline block runs before a deferred
  file has loaded. So the deferral list here excludes anything an inline
  handler depends on, and names why;
* a customer's return to the thank you page is not a payment. It is a
  redirect they control. Only a signature verified webhook is a payment,
  and the worst state in the whole matrix is the quiet one: money
  captured at the gateway, webhook never delivered, order sitting in
  pending, nobody shipping and nobody alerted;
* a cache has two latencies, not one. Reporting the warm number alone is
  how a fifty times improvement gets quoted for a change that left the
  cold path exactly where it was, and the cold path is what a crawler and
  a first time visitor get.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

ENGINE_VERSION = "1.0.0"

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"

_SEVERITY_ORDER = {SEVERITY_OK: 0, SEVERITY_WARN: 1, SEVERITY_CRITICAL: 2}

MODEL_NOTE = ("Every number below comes from the coefficients in core.py "
              "and the inputs on screen. It is a model of the mechanism, "
              "not a measurement of any site.")


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


# ---------------------------------------------------------------------------
# 1. Builder and plugin asset bloat
# ---------------------------------------------------------------------------

BUILDER_ELEMENTOR_FREE = "Elementor free"
BUILDER_ELEMENTOR_PRO = "Elementor Pro"
BUILDER_DIVI = "Divi Builder"
BUILDER_BLOCKS = "Gutenberg blocks with a block theme"
BUILDER_CLASSIC = "Classic theme, no page builder"

BUILDERS: tuple[str, ...] = (BUILDER_ELEMENTOR_FREE, BUILDER_ELEMENTOR_PRO,
                             BUILDER_DIVI, BUILDER_BLOCKS, BUILDER_CLASSIC)

KIND_CSS = "stylesheet"
KIND_JS = "script"

# A stylesheet in the head blocks rendering. A script blocks parsing
# unless it carries defer or async. Both are stated per asset below.
DEFER_SAFE = "Safe to defer"
DEFER_UNSAFE = "Cannot be deferred while inline handlers depend on it"
DEFER_REMOVE = "Should not be loading here at all"
DEFER_ALREADY = "Already deferred or not render blocking"


@dataclass(frozen=True)
class Asset:
    handle: str
    kind: str
    render_blocking: bool
    deferral: str
    reason: str


# WordPress enqueues jQuery on most themes, and WooCommerce enqueues its
# own bundle on every page rather than only on shop pages.
_CORE_ASSETS: tuple[Asset, ...] = (
    Asset("jquery-core", KIND_JS, True, DEFER_UNSAFE,
          ("Core and most plugins print inline script blocks that call "
           "jQuery directly. An inline block runs immediately and a "
           "deferred file has not loaded yet, so deferring this throws on "
           "the first inline handler on the page.")),
    Asset("jquery-migrate", KIND_JS, True, DEFER_SAFE,
          ("Only exists to shim removed jQuery APIs. If nothing on the "
           "site needs it, the correct move is removing it rather than "
           "deferring it.")),
)

_WOO_ASSETS: tuple[Asset, ...] = (
    Asset("woocommerce-layout", KIND_CSS, True, DEFER_REMOVE,
          "Loads on every page, including pages with no shop content."),
    Asset("woocommerce-smallscreen", KIND_CSS, True, DEFER_REMOVE,
          "Same, and it is media queried so it can at least be split."),
    Asset("woocommerce-general", KIND_CSS, True, DEFER_REMOVE,
          "Same again. Three stylesheets on a contact page."),
    Asset("wc-cart-fragments", KIND_JS, False, DEFER_REMOVE,
          ("Fires an admin ajax request on every page load to refresh the "
           "cart count. It cannot be page cached, so it turns every cached "
           "page into one that still hits PHP. Dequeue it unless the theme "
           "genuinely updates the cart without a reload.")),
    Asset("wc-add-to-cart", KIND_JS, False, DEFER_SAFE,
          "Needed on product and archive pages, nowhere else."),
    Asset("select2", KIND_JS, True, DEFER_REMOVE,
          ("Enqueued for the country dropdown at checkout and left "
           "enqueued everywhere else.")),
)

_BUILDER_ASSETS: dict[str, tuple[Asset, ...]] = {
    BUILDER_ELEMENTOR_FREE: (
        Asset("elementor-frontend", KIND_CSS, True, DEFER_ALREADY,
              "The builder's layout stylesheet. It has to be in the head."),
        Asset("elementor-common", KIND_CSS, True, DEFER_ALREADY,
              "Shared widget styles."),
        Asset("elementor-icons", KIND_CSS, True, DEFER_SAFE,
              "Icon font. Nothing above the fold usually needs it."),
        Asset("eicons", KIND_CSS, True, DEFER_SAFE,
              "A second icon font, loaded alongside the first."),
        Asset("elementor-frontend-js", KIND_JS, True, DEFER_SAFE,
              "Widget behaviour. Defer keeps execution order."),
        Asset("swiper", KIND_JS, True, DEFER_SAFE,
              ("Carousel library, enqueued whether or not the page holds a "
               "carousel.")),
    ),
    BUILDER_ELEMENTOR_PRO: (
        Asset("elementor-frontend", KIND_CSS, True, DEFER_ALREADY,
              "The builder's layout stylesheet."),
        Asset("elementor-common", KIND_CSS, True, DEFER_ALREADY,
              "Shared widget styles."),
        Asset("elementor-pro", KIND_CSS, True, DEFER_ALREADY,
              "Pro widget styles."),
        Asset("elementor-icons", KIND_CSS, True, DEFER_SAFE, "Icon font."),
        Asset("eicons", KIND_CSS, True, DEFER_SAFE, "Second icon font."),
        Asset("font-awesome", KIND_CSS, True, DEFER_SAFE,
              "A third icon font on many builds."),
        Asset("elementor-frontend-js", KIND_JS, True, DEFER_SAFE,
              "Widget behaviour."),
        Asset("elementor-pro-frontend", KIND_JS, True, DEFER_SAFE,
              "Pro widget behaviour."),
        Asset("swiper", KIND_JS, True, DEFER_SAFE, "Carousel library."),
    ),
    BUILDER_DIVI: (
        Asset("divi-style", KIND_CSS, True, DEFER_ALREADY,
              "The theme stylesheet, historically one very large file."),
        Asset("divi-dynamic", KIND_CSS, True, DEFER_ALREADY,
              "Per page critical CSS when dynamic assets are enabled."),
        Asset("et-core-unified", KIND_CSS, True, DEFER_ALREADY,
              "Combined module styles."),
        Asset("divi-custom-script", KIND_JS, True, DEFER_SAFE,
              "Module behaviour, depends on jQuery."),
        Asset("magnific-popup", KIND_JS, True, DEFER_SAFE,
              "Lightbox, enqueued whether or not anything opens one."),
    ),
    BUILDER_BLOCKS: (
        Asset("wp-block-library", KIND_CSS, True, DEFER_ALREADY,
              "Core block styles."),
        Asset("global-styles", KIND_CSS, True, DEFER_ALREADY,
              "Theme JSON output, inline on most block themes."),
    ),
    BUILDER_CLASSIC: (
        Asset("theme-style", KIND_CSS, True, DEFER_ALREADY,
              "One theme stylesheet."),
    ),
}

# Stated modelling assumption, shown on screen rather than buried: an
# average active plugin enqueues one stylesheet and one script sitewide.
ASSETS_PER_PLUGIN = 2
RENDER_BLOCKING_SHARE_PER_PLUGIN = 0.5

# Score model. Mobile is request bound rather than bandwidth bound on a
# constrained connection, so a render blocking request costs more than a
# deferred one and both cost something.
BLOCKING_WEIGHT = 2.4
NON_BLOCKING_WEIGHT = 0.7
SCORE_CEILING = 100.0
# The score decays rather than subtracting, because a linear subtraction
# reaches zero on any real store and a score that reads zero for every
# site distinguishes nothing. Decay keeps the ordering meaningful all the
# way out to a badly overloaded build.
SCORE_DECAY_COST = 60.0


@dataclass(frozen=True)
class BloatAnalysis:
    builder_type: str
    total_plugins: int
    named_assets: tuple[Asset, ...]
    plugin_assets: int
    plugin_blocking: int
    total_requests: int
    render_blocking: int
    mobile_score: int
    mobile_score_exact: float
    deferrable: tuple[str, ...]
    removable: tuple[str, ...]
    undeferrable: tuple[str, ...]
    score_after: int
    score_after_exact: float
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def gain(self) -> int:
        return self.score_after - self.mobile_score


def request_cost(blocking: int, non_blocking: int) -> float:
    """Weighted request cost. Mobile is request bound before it is
    bandwidth bound, and a render blocking request costs more."""
    return (max(0, blocking) * BLOCKING_WEIGHT
            + max(0, non_blocking) * NON_BLOCKING_WEIGHT)


def _score_exact(blocking: int, non_blocking: int) -> float:
    cost = request_cost(blocking, non_blocking)
    return SCORE_CEILING * math.exp(-cost / SCORE_DECAY_COST)


def _score(blocking: int, non_blocking: int) -> int:
    return int(round(_score_exact(blocking, non_blocking)))


def analyze_builder_bloat(builder_type: str,
                          total_plugins: int) -> BloatAnalysis:
    """Count the requests, split them by what blocks rendering, and score.

    The score is a model. What is not a model is the split: every named
    asset below is a real WordPress or WooCommerce handle, and the reason
    each one can or cannot be deferred is a property of how WordPress
    prints inline scripts rather than an opinion about speed.
    """
    builder = str(builder_type or "").strip()
    plugins = int(total_plugins)
    if builder not in _BUILDER_ASSETS:
        raise ValueError(f"unknown builder {builder_type!r}")
    if plugins < 0:
        raise ValueError("a plugin count cannot be negative")

    named = _CORE_ASSETS + _WOO_ASSETS + _BUILDER_ASSETS[builder]

    plugin_assets = plugins * ASSETS_PER_PLUGIN
    plugin_blocking = int(round(plugin_assets
                                * RENDER_BLOCKING_SHARE_PER_PLUGIN))

    named_blocking = sum(1 for asset in named if asset.render_blocking)
    total_requests = len(named) + plugin_assets
    render_blocking = named_blocking + plugin_blocking

    mobile_score = _score(render_blocking,
                          total_requests - render_blocking)
    mobile_score_exact = _score_exact(render_blocking,
                                      total_requests - render_blocking)

    deferrable = tuple(a.handle for a in named
                       if a.deferral == DEFER_SAFE)
    removable = tuple(a.handle for a in named
                      if a.deferral == DEFER_REMOVE)
    undeferrable = tuple(a.handle for a in named
                         if a.deferral == DEFER_UNSAFE)

    # Deferring moves an asset out of the blocking count. Removing takes
    # it out of the request count entirely.
    saved_blocking = sum(1 for a in named
                         if a.deferral == DEFER_SAFE and a.render_blocking)
    removed = len(removable)
    removed_blocking = sum(1 for a in named
                           if a.deferral == DEFER_REMOVE
                           and a.render_blocking)

    after_blocking = render_blocking - saved_blocking - removed_blocking
    after_non_blocking = (total_requests - removed) - after_blocking
    score_after = _score(after_blocking, after_non_blocking)
    score_after_exact = _score_exact(after_blocking, after_non_blocking)

    findings: list[Finding] = []

    findings.append(Finding(
        code="BLT-MODEL", severity=SEVERITY_WARN,
        title="This score is a model and not a measurement",
        detail=(f"It is {SCORE_CEILING:.0f} decayed by a weighted "
                f"request cost of {BLOCKING_WEIGHT} per render blocking "
                f"request and {NON_BLOCKING_WEIGHT} per other request, "
                f"over {len(named)} named handles plus {plugins} plugin(s) "
                f"at a stated {ASSETS_PER_PLUGIN} asset(s) each. Nothing "
                f"here loaded a real page."),
        fix=("Measure the real site with a lab tool and field data before "
             "quoting any number to a client.")))

    if undeferrable:
        findings.append(Finding(
            code="BLT-JQUERY", severity=SEVERITY_CRITICAL,
            title=f"{', '.join(undeferrable)} cannot simply be deferred",
            detail=("The common advice is to defer every script. Core and "
                    "most plugins print inline script blocks that call "
                    "jQuery directly, and an inline block runs immediately "
                    "while a deferred file has not loaded yet. The site "
                    "throws on the first inline handler, and it throws on "
                    "the pages a developer does not open, which is most of "
                    "them."),
            fix=("Move the inline handlers into a deferred file of your "
                 "own first, then defer jQuery. Not the other way round.")))

    if removable:
        findings.append(Finding(
            code="BLT-CONDITIONAL", severity=SEVERITY_CRITICAL,
            title=f"{len(removable)} asset(s) load on pages that never use "
                  f"them",
            detail=(f"{', '.join(removable)} are enqueued sitewide. The "
                    f"largest single win on most WooCommerce builds is not "
                    f"compressing these, it is not requesting them on the "
                    f"pages that have no shop content."),
            fix=("Dequeue conditionally in a must use plugin, keyed on "
                 "is_woocommerce, is_cart, and is_checkout.")))

    if "wc-cart-fragments" in removable:
        findings.append(Finding(
            code="BLT-FRAGMENTS", severity=SEVERITY_CRITICAL,
            title="wc-cart-fragments defeats page caching on every page",
            detail=("It fires an admin ajax request on load to refresh the "
                    "cart count. A fully cached page still hits PHP because "
                    "of it, so the cache hit rate in the dashboard looks "
                    "healthy while the origin keeps working."),
            fix=("Dequeue it unless the theme updates the cart without a "
                 "reload, and confirm the cart count still renders.")))

    icon_fonts = [a.handle for a in named
                  if "icon" in a.handle or a.handle == "font-awesome"]
    if len(icon_fonts) > 1:
        findings.append(Finding(
            code="BLT-ICONS", severity=SEVERITY_WARN,
            title=f"{len(icon_fonts)} icon font(s) on one page",
            detail=(f"{', '.join(icon_fonts)} each load a full glyph set to "
                    f"render a handful of shapes. They are render blocking "
                    f"stylesheets and they are rarely needed above the "
                    f"fold."),
            fix="Subset to the glyphs in use, or inline them as SVG."))

    if plugins >= 30:
        findings.append(Finding(
            code="BLT-PLUGINS", severity=SEVERITY_CRITICAL,
            title=f"{plugins} active plugins at a modelled "
                  f"{plugin_assets} asset(s)",
            detail=("Plugin count is a proxy and a poor one, because one "
                    "badly written plugin outweighs ten careful ones. It is "
                    "used here because it is the only input most site "
                    "owners can give you before an audit."),
            fix=("Profile with Query Monitor and rank by real cost, not by "
                 "count.")))

    builder_cost = request_cost(
        sum(1 for a in _BUILDER_ASSETS[builder] if a.render_blocking),
        sum(1 for a in _BUILDER_ASSETS[builder] if not a.render_blocking))
    plugin_cost = request_cost(plugin_blocking,
                               plugin_assets - plugin_blocking)
    if plugin_cost > builder_cost * 2:
        findings.append(Finding(
            code="BLT-DOMINANCE", severity=SEVERITY_CRITICAL,
            title="The plugin load now outweighs the builder choice",
            detail=(f"Plugin assets model at {plugin_cost:.1f} weighted "
                    f"cost against {builder_cost:.1f} for the builder "
                    f"itself. Past this point, arguing about which builder "
                    f"to use is arguing about the smaller number, and "
                    f"moving off the builder would not be felt."),
            fix=("Audit the plugin list before touching the builder. The "
                 "builder is the visible cost and not the largest one.")))

    findings.append(Finding(
        code="BLT-GAIN", severity=SEVERITY_OK,
        title=f"Modelled score moves from {mobile_score} to {score_after}",
        detail=(f"Deferring {len(deferrable)} asset(s) and removing "
                f"{removed} takes the render blocking count from "
                f"{render_blocking} to {after_blocking}. Plugin assets are "
                f"untouched by this, because nothing here knows what they "
                f"are."),
        fix="Re measure after each change, not once at the end."))

    headline = (f"{builder}: {total_requests} request(s), "
                f"{render_blocking} render blocking, modelled score "
                f"{mobile_score}")

    return BloatAnalysis(
        builder_type=builder, total_plugins=plugins, named_assets=named,
        plugin_assets=plugin_assets, plugin_blocking=plugin_blocking,
        total_requests=total_requests, render_blocking=render_blocking,
        mobile_score=mobile_score, mobile_score_exact=mobile_score_exact,
        deferrable=deferrable, removable=removable,
        undeferrable=undeferrable, score_after=score_after,
        score_after_exact=score_after_exact, headline=headline,
        findings=tuple(findings))


# ---------------------------------------------------------------------------
# 2. Checkout gateway and webhook validation
# ---------------------------------------------------------------------------

GATEWAY_STRIPE = "Stripe"
GATEWAY_PAYPAL = "PayPal"
GATEWAY_MOLLIE = "Mollie"
GATEWAY_BANK_TRANSFER = "Direct bank transfer"

GATEWAYS: tuple[str, ...] = (GATEWAY_STRIPE, GATEWAY_PAYPAL, GATEWAY_MOLLIE,
                             GATEWAY_BANK_TRANSFER)

WEBHOOK_VERIFIED = "Delivered and signature verified"
WEBHOOK_UNSIGNED = "Delivered with no signature checked"
WEBHOOK_BAD_SIGNATURE = "Delivered, signature did not match"
WEBHOOK_REPLAYED = "Delivered, timestamp outside the tolerance"
WEBHOOK_MISSING = "Never delivered"

WEBHOOK_STATES: tuple[str, ...] = (WEBHOOK_VERIFIED, WEBHOOK_UNSIGNED,
                                   WEBHOOK_BAD_SIGNATURE, WEBHOOK_REPLAYED,
                                   WEBHOOK_MISSING)

AUTH_CAPTURED = "Captured at the gateway"
AUTH_UNCONFIRMED = "Unconfirmed, the gateway has told us nothing we trust"

ORDER_PAID = "Processing, safe to fulfil"
ORDER_PENDING = "Pending payment, nobody is shipping"
ORDER_ON_HOLD = "On hold, needs a human"
ORDER_AWAITING = "Awaiting manual reconciliation by design"

REPLAY_TOLERANCE_SECONDS = 300


@dataclass(frozen=True)
class GatewayCheck:
    gateway_name: str
    webhook_status: str
    signature_scheme: str
    authorization_state: str
    webhook_verified: bool
    order_state: str
    funnel_health: int
    silent_failure: bool
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def safe_to_fulfil(self) -> bool:
        return self.order_state == ORDER_PAID

    @property
    def severity(self) -> str:
        return _worst(self.findings)


_SIGNATURE_SCHEMES: dict[str, str] = {
    GATEWAY_STRIPE: ("Stripe-Signature header, HMAC SHA256 over the "
                     "timestamp and the raw body, checked against the "
                     "endpoint signing secret"),
    GATEWAY_PAYPAL: ("Transmission signature verified against PayPal's "
                     "certificate, or a verification API call carrying the "
                     "webhook identifier"),
    GATEWAY_MOLLIE: ("The webhook carries only an identifier, so the "
                     "status is fetched back from the API rather than "
                     "trusted from the body"),
    GATEWAY_BANK_TRANSFER: ("No webhook exists. Reconciliation is a human "
                            "reading a bank statement"),
}


def validate_checkout_gateway(gateway_name: str,
                              webhook_status: str) -> GatewayCheck:
    """Decide whether this order may be fulfilled, and name the quiet state.

    A customer landing on the thank you page is not a payment. It is a
    redirect they control, and a gateway integration that marks an order
    paid on the return URL can be paid with a bookmark.

    The state that costs the most is not the forgery. It is the silent
    one: money captured at the gateway, webhook never delivered, order
    sitting in pending, and no alert anywhere, because nothing failed. A
    thing that did not happen raises no error.
    """
    gateway = str(gateway_name or "").strip()
    status = str(webhook_status or "").strip()
    if gateway not in GATEWAYS:
        raise ValueError(f"unknown gateway {gateway_name!r}")
    if status not in WEBHOOK_STATES:
        raise ValueError(f"unknown webhook status {webhook_status!r}")

    findings: list[Finding] = []
    scheme = _SIGNATURE_SCHEMES[gateway]

    if gateway == GATEWAY_BANK_TRANSFER:
        verified = False
        authorization = AUTH_UNCONFIRMED
        order_state = ORDER_AWAITING
        health = 55
        findings.append(Finding(
            code="PAY-MANUAL", severity=SEVERITY_WARN,
            title="This method has no webhook and never will",
            detail=("Payment arrives as a line on a bank statement, so the "
                    "order sits on hold until somebody reconciles it. That "
                    "is the design rather than a fault, and it is still the "
                    "slowest path in the funnel by a wide margin."),
            fix=("Set expectations at checkout and put a named owner on the "
                 "daily reconciliation.")))
    elif status == WEBHOOK_VERIFIED:
        verified = True
        authorization = AUTH_CAPTURED
        order_state = ORDER_PAID
        health = 96
        findings.append(Finding(
            code="PAY-VERIFIED", severity=SEVERITY_OK,
            title="A signed webhook confirmed the capture",
            detail=(f"{scheme}. This is the only signal in the whole flow "
                    f"that is worth marking an order paid on."),
            fix=("Keep the raw request body for verification. Parsing "
                 "before verifying changes the bytes and breaks the "
                 "signature.")))
    elif status == WEBHOOK_MISSING:
        verified = False
        authorization = AUTH_CAPTURED
        order_state = ORDER_PENDING
        health = 12
        findings.append(Finding(
            code="PAY-SILENT", severity=SEVERITY_CRITICAL,
            title="The customer has been charged and the order says pending",
            detail=("Money left the customer's account at the gateway and "
                    "the store never heard. Nothing errored, because "
                    "nothing ran. The customer chases in a few days, the "
                    "support reply says no order was placed, and the "
                    "chargeback follows that."),
            fix=("Reconcile against the gateway on a schedule rather than "
                 "waiting for webhooks, and alert on any capture with no "
                 "matching order after fifteen minutes.")))
    elif status == WEBHOOK_UNSIGNED:
        verified = False
        authorization = AUTH_UNCONFIRMED
        order_state = ORDER_ON_HOLD
        health = 20
        findings.append(Finding(
            code="PAY-UNSIGNED", severity=SEVERITY_CRITICAL,
            title="An unverified webhook is an anonymous HTTP request",
            detail=("The endpoint is public. Anything that can reach it can "
                    "post a body claiming an order was paid, and without a "
                    "signature check there is nothing to distinguish that "
                    "from the gateway."),
            fix=f"Verify every request. {scheme}."))
    elif status == WEBHOOK_BAD_SIGNATURE:
        verified = False
        authorization = AUTH_UNCONFIRMED
        order_state = ORDER_ON_HOLD
        health = 30
        findings.append(Finding(
            code="PAY-BADSIG", severity=SEVERITY_CRITICAL,
            title="The signature did not match, so this is not the gateway",
            detail=("Either somebody is posting to the endpoint, or the "
                    "signing secret rotated and the store still holds the "
                    "old one. Both look identical from here, and the second "
                    "one means every real payment is now failing too."),
            fix=("Check the secret in the dashboard first, then treat "
                 "unmatched requests as hostile.")))
    else:
        verified = False
        authorization = AUTH_UNCONFIRMED
        order_state = ORDER_ON_HOLD
        health = 35
        findings.append(Finding(
            code="PAY-REPLAY", severity=SEVERITY_CRITICAL,
            title=f"Timestamp outside the {REPLAY_TOLERANCE_SECONDS} second "
                  f"tolerance",
            detail=("A valid signature on an old payload is a replay. "
                    "Without the timestamp check a captured webhook can be "
                    "posted again to mark a second order paid, and the "
                    "signature will verify every time because it is a real "
                    "one."),
            fix=("Keep the tolerance and record processed event "
                 "identifiers, since a legitimate retry is also a repeat.")))

    silent = order_state == ORDER_PENDING and authorization == AUTH_CAPTURED

    findings.append(Finding(
        code="PAY-REDIRECT", severity=SEVERITY_CRITICAL,
        title="The thank you page is not a payment confirmation",
        detail=("A customer returning to the order received URL has done "
                "nothing except load a page they can load again. An "
                "integration that marks an order paid on that redirect can "
                "be paid with a bookmark."),
        fix=("Mark paid on the verified webhook only. Use the redirect for "
             "what it is, which is a message to the customer.")))

    if order_state != ORDER_PAID:
        findings.append(Finding(
            code="PAY-FUNNEL", severity=SEVERITY_WARN,
            title="Every state except the verified one costs a conversion",
            detail=("An order that needs a human is an order that ships "
                    "late, and a customer who has been charged and sees "
                    "pending opens a ticket rather than a repeat order."),
            fix="Measure time from capture to fulfilment, not just revenue."))

    headline = f"{gateway}: {order_state}"
    return GatewayCheck(
        gateway_name=gateway, webhook_status=status, signature_scheme=scheme,
        authorization_state=authorization, webhook_verified=verified,
        order_state=order_state, funnel_health=health, silent_failure=silent,
        headline=headline, findings=tuple(findings))


# ---------------------------------------------------------------------------
# 3. Catalog query benchmarking
# ---------------------------------------------------------------------------

# Cost model coefficients, all named so the arithmetic is reproducible.
BASE_QUERY_MS = 4.0
INDEX_SCAN_COEFFICIENT = 0.55
META_JOIN_MS_PER_THOUSAND = 3.2
DEFAULT_META_JOINS = 4
DB_TRANSIENT_READ_MS = 2.6
OBJECT_CACHE_READ_MS = 0.4
DEFAULT_HIT_RATE = 0.85


@dataclass(frozen=True)
class QueryBenchmark:
    product_count: int
    use_caching: bool
    meta_joins: int
    object_cache: bool
    hit_rate: float
    cold_ms: float
    warm_ms: float
    effective_ms: float
    baseline_ms: float
    lookup_table_ms: float
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def effective_gain_percent(self) -> float:
        if self.baseline_ms <= 0:
            return 0.0
        return (self.baseline_ms - self.effective_ms) / self.baseline_ms * 100

    @property
    def warm_only_claim_percent(self) -> float:
        if self.baseline_ms <= 0:
            return 0.0
        return (self.baseline_ms - self.warm_ms) / self.baseline_ms * 100


def _uncached_ms(product_count: int, meta_joins: int,
                 lookup_table: bool = False) -> float:
    count = max(1, int(product_count))
    scan = INDEX_SCAN_COEFFICIENT * math.log2(count) * math.sqrt(count) / 8
    if lookup_table:
        joins = META_JOIN_MS_PER_THOUSAND * (count / 1000.0) * 1
    else:
        joins = META_JOIN_MS_PER_THOUSAND * (count / 1000.0) * meta_joins
    return round(BASE_QUERY_MS + scan + joins, 2)


def benchmark_catalog_queries(product_count: int, use_caching: bool,
                              meta_joins: int = DEFAULT_META_JOINS,
                              object_cache: bool = False,
                              hit_rate: float = DEFAULT_HIT_RATE
                              ) -> QueryBenchmark:
    """Model the query cost, and report both latencies rather than one.

    A cache has a cold path and a warm path. Reporting the warm number
    alone is how a large improvement gets quoted for a change that left
    the cold path exactly where it was, and the cold path is what a
    crawler, a first time visitor, and every request after an expiry get.

    The second thing modelled here is where the transient lives. Stored in
    the database it is a row in wp_options, which is a read rather than a
    computation saved and is why a database transient helps far less than
    the same code behind a real object cache.
    """
    count = int(product_count)
    joins = int(meta_joins)
    rate = float(hit_rate)
    if count < 0:
        raise ValueError("a product count cannot be negative")
    if joins < 0:
        raise ValueError("a join count cannot be negative")
    if not 0.0 <= rate <= 1.0:
        raise ValueError("a hit rate is a fraction between 0 and 1")

    baseline = _uncached_ms(count, joins)
    lookup = _uncached_ms(count, joins, lookup_table=True)
    findings: list[Finding] = []

    if not use_caching:
        cold = baseline
        warm = baseline
        effective = baseline
        findings.append(Finding(
            code="QRY-NOCACHE", severity=SEVERITY_WARN,
            title=f"Every request pays the full {baseline:.2f} ms",
            detail=(f"Product attributes live in wp_postmeta as key and "
                    f"value rows, so each filter is another join. This "
                    f"model charges {joins} of them at "
                    f"{META_JOIN_MS_PER_THOUSAND} ms per thousand products "
                    f"each."),
            fix=("Use the product lookup table for price, stock, and "
                 "rating before reaching for a cache at all.")))
    else:
        cold = round(baseline + (OBJECT_CACHE_READ_MS if object_cache
                                 else DB_TRANSIENT_READ_MS), 2)
        warm = OBJECT_CACHE_READ_MS if object_cache else DB_TRANSIENT_READ_MS
        effective = round(rate * warm + (1 - rate) * cold, 2)

        findings.append(Finding(
            code="QRY-TWOPATHS", severity=SEVERITY_CRITICAL,
            title=f"Warm is {warm:.2f} ms and cold is {cold:.2f} ms",
            detail=(f"Quoting the warm figure alone reads as a "
                    f"{(baseline - warm) / baseline * 100:.0f}% improvement. "
                    f"At a {rate:.0%} hit rate the number a visitor actually "
                    f"experiences averages {effective:.2f} ms, which is "
                    f"{(baseline - effective) / baseline * 100:.0f}%. The "
                    f"cold path got slower, not faster, because the cache "
                    f"read is added to it."),
            fix=("Report the effective figure and the hit rate together, "
                 "and never the warm one on its own.")))

        if not object_cache:
            findings.append(Finding(
                code="QRY-DBTRANSIENT", severity=SEVERITY_CRITICAL,
                title="A database transient is a row in wp_options",
                detail=(f"Without a persistent object cache, set_transient "
                        f"writes to the same database the query was trying "
                        f"to avoid. It saves the computation and adds a "
                        f"read, which is why the warm path here is "
                        f"{DB_TRANSIENT_READ_MS} ms rather than "
                        f"{OBJECT_CACHE_READ_MS} ms."),
                fix=("Install Redis or Memcached with a persistent object "
                     "cache drop in, then the same code gets the memory "
                     "path.")))
            findings.append(Finding(
                code="QRY-STAMPEDE", severity=SEVERITY_WARN,
                title="Every concurrent request recomputes on expiry",
                detail=("When the transient expires there is no lock, so "
                        "every request that arrives in the rebuild window "
                        "runs the full query at once. Traffic spikes are "
                        "exactly when this happens and exactly when it "
                        "hurts."),
                fix=("Serve the stale value while one request rebuilds, "
                     "keyed on a short lock.")))

    findings.append(Finding(
        code="QRY-LOOKUP", severity=SEVERITY_OK,
        title=f"The lookup table path models at {lookup:.2f} ms",
        detail=(f"wc_product_meta_lookup exists so price, stock, and rating "
                f"filters do not need a postmeta join each. Against "
                f"{baseline:.2f} ms on the meta path that is a structural "
                f"saving rather than a cached one, and it holds on the cold "
                f"path too."),
        fix="Check the lookup table is populated after any bulk import."))

    findings.append(Finding(
        code="QRY-MODEL", severity=SEVERITY_WARN,
        title="These milliseconds are modelled, not measured",
        detail=(f"Computed from named coefficients in core.py: "
                f"{BASE_QUERY_MS} ms base, an index scan term, and "
                f"{META_JOIN_MS_PER_THOUSAND} ms per thousand products per "
                f"join. No database was queried. The shape of the curve is "
                f"the point, not the absolute figure."),
        fix=("Measure the real store with Query Monitor and the slow query "
             "log before quoting anything.")))

    headline = (f"{count:,} products: {baseline:.2f} ms uncached, "
                f"{effective:.2f} ms effective")

    return QueryBenchmark(
        product_count=count, use_caching=bool(use_caching), meta_joins=joins,
        object_cache=bool(object_cache), hit_rate=rate, cold_ms=cold,
        warm_ms=warm, effective_ms=effective, baseline_ms=baseline,
        lookup_table_ms=lookup, headline=headline, findings=tuple(findings))
