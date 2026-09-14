"""Page tests for the Print on Demand Automation Router, via AppTest.

Written for pytest.

Run: pytest tests/test_pod_automation_router_page.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.pod_automation_router.core import (  # noqa: E402
    CATALOGUE,
    ERR_OUT_OF_STOCK,
    WOOCOMMERCE,
    find_variant,
)

HARNESS = Path(__file__).resolve().parent / "_page_harness_pod_automation_router.py"

IN_STOCK_SKU = "TEE-CLASSIC-BLK"
OUT_OF_STOCK_SIZE = "L"


def text_of(at: AppTest) -> str:
    parts: list[str] = []
    for attr in ("markdown", "text", "info", "success", "warning", "error",
                 "caption", "code", "header", "subheader", "title"):
        try:
            parts += [str(getattr(el, "value", "")) for el in getattr(at, attr)]
        except Exception:
            pass
    try:
        for frame in at.get("dataframe"):
            value = getattr(frame, "value", None)
            if value is not None and hasattr(value, "to_numpy"):
                parts += [str(cell) for cell in value.to_numpy().ravel()]
                parts += [str(col) for col in value.columns]
    except Exception:
        pass
    return "\n".join(parts)


def run(timeout: int = 120) -> AppTest:
    at = AppTest.from_file(str(HARNESS), default_timeout=timeout)
    at.run()
    return at


def widget(at: AppTest, kind: str, label: str):
    for el in getattr(at, kind):
        if el.label == label:
            return el
    raise AssertionError(f"no {kind} labelled {label!r}; "
                         f"available: {[e.label for e in getattr(at, kind)]}")


def button(at: AppTest, fragment: str):
    for el in at.button:
        if fragment in str(el.label):
            return el
    raise AssertionError(f"no button containing {fragment!r}; "
                         f"available: {[e.label for e in at.button]}")


@pytest.fixture(scope="module")
def cold() -> AppTest:
    return run()


def test_page_renders_without_exception(cold):
    assert not cold.exception, [str(e.value) for e in cold.exception]


def test_hero_and_tabs_render(cold):
    body = text_of(cold)
    assert "Print on Demand Automation Router" in body
    assert len(cold.tabs) == 4


def test_the_catalogue_and_its_stock_are_shown(cold):
    body = text_of(cold)
    for product in CATALOGUE:
        assert product.sku in body
    assert "Out of stock" in body, "the seeded out of stock blank should be visible"


def test_widget_labels_are_unambiguous(cold):
    labels = [str(el.label) for el in cold.selectbox]
    assert len(labels) == len(set(labels)), labels
    for outer in labels:
        others = [l for l in labels if l != outer]
        assert not any(outer in other for other in others), (outer, others)


def test_generating_an_order_shows_the_woocommerce_payload():
    at = run()
    button(at, "Generate WooCommerce order").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    body = text_of(at)
    assert "Received" in body
    assert "line_items" in body, "the raw payload should be shown"
    assert "WC-00001" in body


def test_routing_a_clean_order_shows_the_printful_request():
    at = run()
    button(at, "Generate WooCommerce order").click().run()
    button(at, "Route to Printful").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    body = text_of(at)
    assert "Routed" in body
    assert "external_id" in body, "the Printful request should be shown"
    assert "variant_id" in body
    assert "PF-" in body


def test_an_out_of_stock_blank_is_caught_and_alerted():
    at = run()
    widget(at, "selectbox", "Size").set_value(OUT_OF_STOCK_SIZE).run()
    button(at, "Generate WooCommerce order").click().run()
    button(at, "Route to Printful").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    body = text_of(at)
    assert ERR_OUT_OF_STOCK in body
    assert "Blank garment out of stock" in body
    assert "nothing was submitted" in body.lower()
    assert len(at.error) >= 1, "a refusal should raise a visible error"


def test_the_out_of_stock_warning_appears_before_routing():
    at = run()
    widget(at, "selectbox", "Size").set_value(OUT_OF_STOCK_SIZE).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    body = text_of(at)
    assert "out of stock" in body.lower()
    assert len(at.warning) >= 1


def test_tracking_is_unavailable_until_an_order_is_routed():
    at = run()
    body = text_of(at)
    assert "Tracking appears once an order has been accepted" in body


def test_the_shipment_webhook_pushes_tracking_to_the_right_channels():
    at = run()
    widget(at, "selectbox", "Sales channel").set_value("Etsy").run()
    button(at, "Generate WooCommerce order").click().run()
    button(at, "Route to Printful").click().run()
    button(at, "Simulate shipment webhook").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    body = text_of(at)
    assert "Shipped" in body
    assert "RM" in body, "a tracking number should be shown"
    assert "Etsy" in body and WOOCOMMERCE in body


def test_the_router_log_records_the_whole_journey():
    at = run()
    button(at, "Generate WooCommerce order").click().run()
    button(at, "Route to Printful").click().run()
    button(at, "Simulate shipment webhook").click().run()
    body = text_of(at)
    for stage in ("receive", "translate", "fulfil", "track"):
        assert stage in body, f"the log should record the {stage} stage"
    assert "Orders received" in body


def test_typed_input_is_escaped_not_rendered_as_live_html():
    at = run()
    widget(at, "text_input", "Recipient name").set_value(
        '<img src=x onerror="window.__probe=1">').run()
    button(at, "Generate WooCommerce order").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    rendered = "\n".join(str(getattr(el, "value", "")) for el in at.markdown)
    assert "<img src=x onerror=" not in rendered
