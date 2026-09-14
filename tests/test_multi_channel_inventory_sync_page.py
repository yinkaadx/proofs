"""UI smoke tests for the multi channel inventory sync page, via AppTest.

Pass and fail markers are declared before execution. Every scenario asserts the
script ran with zero uncaught exceptions and that the named content rendered.

Run: python3 tests/test_multi_channel_inventory_sync_page.py
Pass marker: final line is exactly "INVENTORY UI RESULT: PASS <n>/<n>", exit 0.
Fail marker: any line starting "FAIL", plus exit code 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from streamlit.testing.v1 import AppTest  # noqa: E402

from tools.multi_channel_inventory_sync.core import (  # noqa: E402
    ERR_INSUFFICIENT_STOCK,
    OPENING_STOCK,
    SAMPLE_PRODUCTS,
)

failures: list[str] = []
checks = 0

HARNESS = Path(__file__).resolve().parent / "_page_harness_multi_channel_inventory_sync.py"

KETTLE = "CEN-KTL-001"
TOASTER = "CEN-TST-007"


def expect(condition: bool, label: str) -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


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


def button(at: AppTest, fragment: str):
    for el in at.button:
        if fragment in str(el.label):
            return el
    raise AssertionError(f"no button containing {fragment!r}; "
                         f"available: {[e.label for e in at.button]}")


def select(at: AppTest, label: str):
    for el in at.selectbox:
        if el.label == label:
            return el
    raise AssertionError(f"no selectbox labelled {label!r}; "
                         f"available: {[e.label for e in at.selectbox]}")


def number(at: AppTest, label: str):
    for el in at.number_input:
        if el.label == label:
            return el
    raise AssertionError(f"no number_input labelled {label!r}; "
                         f"available: {[e.label for e in at.number_input]}")


print("== Cold start ==")
at = run()
expect(not at.exception,
       f"page renders with no exception (got {[str(e.value) for e in at.exception]})")
body = text_of(at)
expect("Multi Channel Inventory Sync Engine" in body, "hero renders")
expect(len(at.tabs) == 4, f"four tabs present (got {len(at.tabs)})")
expect("Units in stock" in body, "the KPI tiles render")

print("== The central ledger lists every sample product on every channel ==")
for product in SAMPLE_PRODUCTS:
    expect(product.sku in body, f"{product.sku} appears in the master table")
    expect(product.title in body, f"the title for {product.sku} appears")
for channel in ("Amazon", "eBay", "Shopify"):
    expect(channel in body, f"the {channel} column renders")
expect("AMZ-KETTLE-17" in body, "an Amazon channel SKU is shown in the mapping")
expect("EB_KTL_1700" in body, "an eBay channel SKU is shown in the mapping")
expect("shopify-kettle-1-7l" in body, "a Shopify channel SKU is shown in the mapping")
expect("not listed" in body,
       "a product without a listing on a channel says so rather than inventing one")

print("== Widget labels are unambiguous ==")
labels = [str(el.label) for el in at.selectbox]
expect(len(labels) == len(set(labels)),
       f"no two selectboxes share a label (got {labels})")
for outer in labels:
    others = [l for l in labels if l != outer]
    expect(not any(outer in other for other in others),
           f"no label is a prefix of another, which would make both ambiguous to a "
           f"screen reader and to automation (offender: {outer!r} in {others})")

print("== A sale from Amazon deducts once and broadcasts to the other two ==")
at = run()
button(at, "Order from Amazon").click().run()
expect(not at.exception,
       f"an Amazon order runs clean (got {[str(e.value) for e in at.exception]})")
body = text_of(at)
expect("Accepted" in body, "the order is accepted")
expect(f"{OPENING_STOCK[KETTLE]}" in body and f"{OPENING_STOCK[KETTLE] - 1}" in body,
       "the before and after figures are shown")
expect("eBay" in body and "Shopify" in body, "both remaining channels appear")
expect("Broadcasts pushed" in body, "the pushed count reaches the KPI tiles")

print("== Selling from each channel in turn runs clean ==")
for channel in ("Amazon", "eBay", "Shopify"):
    at = run()
    button(at, f"Order from {channel}").click().run()
    expect(not at.exception,
           f"a {channel} order runs clean (got {[str(e.value) for e in at.exception]})")
    body = text_of(at)
    expect("Accepted" in body, f"the {channel} order is accepted")

print("== The negative stock guard blocks an oversell with a specific error ==")
at = run()
select(at, "Product to sell").set_value(TOASTER).run()
number(at, "Quantity ordered").set_value(5).run()
button(at, "Order from eBay").click().run()
expect(not at.exception,
       f"an oversell attempt runs clean (got {[str(e.value) for e in at.exception]})")
body = text_of(at)
expect(ERR_INSUFFICIENT_STOCK in body,
       f"the specific error code {ERR_INSUFFICIENT_STOCK} is shown")
expect("Negative stock guard tripped" in body, "the headline names the guard")
expect("nothing was written" in body, "the page states that nothing was written")
expect("Orders blocked" in body, "the blocked count reaches the KPI tiles")
expect(len(at.error) >= 1, "a real error banner is raised, not just text")

print("== Stock is genuinely unchanged after a block ==")
expect(f"{OPENING_STOCK[TOASTER]}" in body,
       "the toaster still shows its opening stock after the refused order")

print("== Selling the last unit is allowed, the next one is not ==")
at = run()
select(at, "Product to sell").set_value(TOASTER).run()
button(at, "Order from Amazon").click().run()
expect(not at.exception, "selling the last unit runs clean")
body = text_of(at)
expect("Accepted" in body, "the last unit sells")
button(at, "Order from eBay").click().run()
body = text_of(at)
expect(ERR_INSUFFICIENT_STOCK in body,
       "the very next sale on another channel is blocked by the guard")

print("== An unreachable channel fails its sync and is logged ==")
at = run()
for toggle in at.toggle:
    if "eBay" in str(toggle.label):
        toggle.set_value(False).run()
        break
button(at, "Order from Amazon").click().run()
expect(not at.exception,
       f"a sale with a channel offline runs clean "
       f"(got {[str(e.value) for e in at.exception]})")
body = text_of(at)
expect("Failed" in body, "the failed broadcast is shown")
expect("queued" in body.lower(), "the failed write is described as queued")
expect("Broadcasts failed" in body, "the failure reaches the KPI tiles")

print("== Restocking lifts stock and pushes everywhere ==")
at = run()
button(at, "Receive into stock").click().run()
expect(not at.exception,
       f"a restock runs clean (got {[str(e.value) for e in at.exception]})")
body = text_of(at)
expect("Broadcasts pushed" in body, "the restock broadcast is counted")

print("== The audit trail renders ==")
at = run()
button(at, "Order from Amazon").click().run()
body = text_of(at)
expect("order.accepted" in body, "the accepted order appears in the audit trail")
expect("broadcast.pushed" in body, "the broadcasts appear in the audit trail")
expect("Correlation" in body, "the trail carries a correlation column")

print()
if failures:
    print(f"INVENTORY UI RESULT: FAIL {len(failures)} of {checks} checks failed")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"INVENTORY UI RESULT: PASS {checks}/{checks}")
sys.exit(0)
