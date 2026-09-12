"""Ground truth test: the inventory flow in a real browser, across reruns.

The page test harness drives Streamlit's AppTest, which re-executes the script
directly. That missed a real defect: a selectbox whose option labels carried a
live stock count stored a stale formatted label under its key, so after a sale
the widget silently fell back to the first product and the next order went
against the wrong SKU. Only a browser, clicking twice in sequence, showed it.

This test therefore drives Chromium: it selects the one unit product, sells it,
then sells again, and asserts the selection held and the guard blocked the
second order.

Needs a running hub. Start one first, or pass a base URL:
    streamlit run streamlit_app.py --server.port 8503 --server.headless true
    python3 tests/test_browser_inventory_flow.py [http://127.0.0.1:8503]

Pass marker: final line is exactly "BROWSER RESULT: PASS <n>/<n>", exit 0.
Fail marker: any line starting "FAIL", plus exit code 1.
Skips cleanly (exit 0) when the browser or the hub is unavailable.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8503"
CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
TOOL = "/multi-channel-inventory-sync"
ONE_UNIT_SKU = "CEN-TST-007"

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("BROWSER RESULT: SKIP (playwright not installed)")
    sys.exit(0)

try:
    urllib.request.urlopen(f"{BASE}/_stcore/health", timeout=10).read()
except (urllib.error.URLError, OSError) as exc:
    print(f"BROWSER RESULT: SKIP (no hub running at {BASE}: {exc})")
    sys.exit(0)

failures: list[str] = []
checks = 0


def expect(condition: bool, label: str) -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


print("== The inventory flow survives repeated sales in a real browser ==")
with sync_playwright() as p:
    try:
        browser = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    except Exception as exc:  # noqa: BLE001
        print(f"BROWSER RESULT: SKIP (browser unavailable: {exc})")
        sys.exit(0)

    page = browser.new_context(viewport={"width": 1440, "height": 1100}).new_page()
    page.goto(BASE + TOOL, wait_until="networkidle", timeout=60000)
    page.wait_for_selector("text=Central SKU ledger", timeout=30000)
    page.wait_for_timeout(2500)

    expect("Multi Channel Inventory Sync Engine" in page.content(),
           "the tool loads at its own URL path")

    page.get_by_role("tab", name="Channel Sales").click()
    page.wait_for_timeout(1500)

    combo = page.get_by_role("combobox", name="Product to sell", exact=True)
    expect(combo.count() == 1,
           f"exactly one control answers to 'Product to sell' (got {combo.count()})")

    combo.click()
    page.wait_for_timeout(1200)
    page.get_by_role("option", name=ONE_UNIT_SKU, exact=False).click()
    page.wait_for_timeout(2500)
    selected = page.get_by_role("combobox", name="Product to sell", exact=True).input_value()
    expect(ONE_UNIT_SKU in selected,
           f"the one unit product is selected (got {selected!r})")

    outcomes = []
    for attempt in (1, 2, 3):
        page.get_by_role("button", name="Order from eBay").click()
        page.wait_for_timeout(4500)
        held = page.get_by_role("combobox", name="Product to sell", exact=True).input_value()
        expect(ONE_UNIT_SKU in held,
               f"after sale {attempt} the selection still reads {ONE_UNIT_SKU} "
               f"(got {held!r})")
        page.get_by_role("tab", name="Sync Broadcast").click()
        page.wait_for_timeout(1800)
        content = page.content()
        outcomes.append({
            "blocked": "INSUFFICIENT_STOCK" in content,
            "guard": "Negative stock guard tripped" in content,
        })
        page.get_by_role("tab", name="Channel Sales").click()
        page.wait_for_timeout(1200)

    expect(not outcomes[0]["blocked"],
           "the first sale of the only unit is accepted")
    expect(outcomes[1]["blocked"],
           "the second sale is blocked, because stock is now zero")
    expect(outcomes[1]["guard"],
           "the block is attributed to the negative stock guard by name")
    expect(outcomes[2]["blocked"],
           "a third attempt is blocked too, so the guard is not a one shot")

    page.get_by_role("tab", name="Inventory Ledger").click()
    page.wait_for_timeout(2000)
    ledger_text = page.inner_text("body")
    expect("No SKU has ever gone below zero" in ledger_text,
           "the ledger still reports that nothing went negative")

    browser.close()

print()
if failures:
    print(f"BROWSER RESULT: FAIL {len(failures)} of {checks} checks failed")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"BROWSER RESULT: PASS {checks}/{checks}")
sys.exit(0)
