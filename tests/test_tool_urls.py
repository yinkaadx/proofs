"""Ground truth test: every registered tool actually resolves at its own URL.

This exists because a URL was reported as live without ever being opened. The
registry is the single source of truth for paths, so this walks it and proves
each one in a real browser: the page loads, it is not Streamlit's "page not
found" fallback, and the tool's own title is on screen.

It is data driven on purpose. Adding a tool to the registry automatically adds
it here, so no future tool can be handed over with an unproven URL.

Needs a running hub. Start one first, or pass a base URL:
    scripts/check.sh
    python3 tests/test_tool_urls.py [http://127.0.0.1:8600]

Pass marker: final line is exactly "URL RESULT: PASS <n>/<n>", exit code 0.
Fail marker: any line starting "FAIL", plus exit code 1.
Skips cleanly (exit 0) when the browser or the hub is unavailable.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.build_info import short_commit  # noqa: E402
from tools.registry import all_tools  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8600"

try:
    from playwright.sync_api import sync_playwright

    from tests.browser_util import hub_is_up, launch, open_tool, settle
except ImportError:
    print("URL RESULT: SKIP (playwright not installed)")
    sys.exit(0)

if not hub_is_up(BASE):
    print(f"URL RESULT: SKIP (no hub running at {BASE})")
    sys.exit(0)

failures: list[str] = []
checks = 0

# Streamlit shows this when a url_path does not match any registered page.
NOT_FOUND = "does not seem to exist"


def expect(condition: bool, label: str) -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


TOOLS = all_tools()
print(f"== Every registered tool resolves at its own URL ({len(TOOLS)} tools) ==")

with sync_playwright() as p:
    try:
        browser, page = launch(p, viewport={"width": 1440, "height": 1000})
    except Exception as exc:  # noqa: BLE001
        print(f"URL RESULT: SKIP (browser unavailable: {exc})")
        sys.exit(0)

    # The landing page must list every tool, so nothing is published without a
    # way in from the front door.
    page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
    settle(page)
    home = page.content()
    for tool in TOOLS:
        expect(tool.title in home, f"the landing page lists {tool.title}")

    # Belt and braces against a stale server. If the hub is serving an older
    # commit than the checkout, every check below would be testing code that is
    # not the code under review, which is exactly how a URL came to be reported
    # as live while the running app had never heard of it.
    expected_build = short_commit()
    expect(f"build {expected_build}" in home,
           f"the running hub serves this checkout (expected build "
           f"{expected_build}; if this fails the server is stale, restart it)")

    for tool in TOOLS:
        url = f"{BASE.rstrip('/')}/{tool.key}"
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        settle(page)
        content = page.content()

        expect(NOT_FOUND not in content,
               f"/{tool.key} is a real page, not the not found fallback")
        expect(tool.title in content,
               f"/{tool.key} renders its own title, {tool.title!r}")
        expect("Traceback" not in content and "StreamlitAPIException" not in content,
               f"/{tool.key} renders without an exception on screen")

    # A path that genuinely does not exist must still be reported as missing,
    # otherwise the two checks above would pass for any URL at all.
    page.goto(f"{BASE.rstrip('/')}/definitely-not-a-tool",
              wait_until="domcontentloaded", timeout=60000)
    settle(page)
    expect(NOT_FOUND in page.content(),
           "an unknown path is reported as not found, so this test can fail")

    browser.close()

print()
if failures:
    print(f"URL RESULT: FAIL {len(failures)} of {checks} checks failed")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"URL RESULT: PASS {checks}/{checks}")
sys.exit(0)
