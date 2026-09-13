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

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.build_info import short_commit  # noqa: E402
from tools.registry import all_tools  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8600"

# A laptop, not a tall desktop. The sidebar failure this suite now guards
# against only appears once the window is a realistic height.
VIEWPORTS = ((1440, 720), (1200, 800))
# The help must start within this many pixels of the top of the sidebar. It is
# a budget, not a preference: at 563px the old layout was still technically on
# screen at this height, and two more tools would have ended that.
HELP_MUST_START_ABOVE = 200

REQUIRE_BROWSER = os.environ.get("TOOLBENCH_REQUIRE_BROWSER") == "1"


def unavailable(reason: str) -> None:
    """Skip, or fail if this run was told the browser guard is mandatory."""
    if REQUIRE_BROWSER:
        print(f"URL RESULT: FAIL (browser guard required but unavailable: "
              f"{reason})")
        sys.exit(1)
    print(f"URL RESULT: SKIP ({reason})")
    sys.exit(0)


try:
    from playwright.sync_api import sync_playwright

    from tests.browser_util import (hub_is_up, launch, open_tool, settle,
                                    sidebar_block)
except ImportError:
    unavailable("playwright not installed")

if not hub_is_up(BASE):
    unavailable(f"no hub running at {BASE}")

failures: list[str] = []
checks = 0

# Streamlit shows this when a url_path does not match any registered page.
NOT_FOUND = "does not seem to exist"


def visible_text(page, timeout: int = 15000) -> str:
    """The text a person would actually read.

    Not `page.content()`. That is the serialised DOM, where an ampersand comes
    back as &amp; and a title has to be matched twice over. Visible text also
    forces the question this test is really asking: can somebody see the name
    the tool is registered under.
    """
    page.wait_for_function(
        "() => (document.body.innerText || '').trim().length > 200",
        timeout=timeout)
    return page.inner_text("body")


def wait_for_title(page, title: str, timeout: int = 15000) -> bool:
    """Wait for the title to appear rather than reading once and hoping.

    `settle()` says the script finished, which is not the same as the page
    having painted. Reading immediately after it passes on a fast render and
    fails on a slow one, which is a flaky test pretending to be a real one.
    """
    try:
        page.wait_for_function(
            "t => (document.body.innerText || '').includes(t)", arg=title,
            timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


def expect(condition: bool, label: str) -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


def check_help_block(page, tool, viewport: str) -> None:
    """Assert the whole How to use block is readable without scrolling.

    Two assertions, and the difference between them is the bug that shipped
    twice. The first is that the block ENDS on screen: a heading at 100px whose
    last step runs past the fold is a block the reader cannot finish, and an
    assertion on the heading alone reports that as fine. The second is that it
    STARTS near the top, which is what stops the block being pushed down one
    registry entry at a time as tools are added.
    """
    where = sidebar_block(page, "How to use")
    expect(where.get("found") is True,
           f"[{viewport}] /{tool.key} has a How to use block in the sidebar")
    if not where.get("found"):
        return
    expect(where["bottom"] <= where["visibleHeight"],
           f"[{viewport}] /{tool.key} shows its whole How to use block without "
           f"scrolling (ends at {where['bottom']}px of {where['visibleHeight']}"
           f"px visible)")
    expect(0 <= where["top"] < HELP_MUST_START_ABOVE,
           f"[{viewport}] /{tool.key} starts its How to use block in the first "
           f"{HELP_MUST_START_ABOVE}px of the sidebar (starts at "
           f"{where['top']}px), so a growing tool list cannot push it down")


TOOLS = all_tools()
print(f"== Every registered tool resolves at its own URL ({len(TOOLS)} tools) ==")

with sync_playwright() as p:
    try:
        browser, page = launch(p, viewport={"width": VIEWPORTS[0][0],
                                            "height": VIEWPORTS[0][1]})
    except Exception as exc:  # noqa: BLE001
        unavailable(f"browser unavailable: {exc}")

    # The landing page must list every tool, so nothing is published without a
    # way in from the front door.
    page.goto(BASE, wait_until="domcontentloaded", timeout=60000)
    settle(page)
    home = visible_text(page)
    for tool in TOOLS:
        expect(wait_for_title(page, tool.title),
               f"the landing page lists {tool.title}")

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
        expect(wait_for_title(page, tool.title),
               f"/{tool.key} renders its own title, {tool.title!r}")
        expect("Traceback" not in content and "StreamlitAPIException" not in content,
               f"/{tool.key} renders without an exception on screen")

        check_help_block(page, tool, f"{VIEWPORTS[0][0]}x{VIEWPORTS[0][1]}")

    # The same help check at a shorter, narrower window. One viewport is not a
    # measurement, it is an anecdote: the old layout cleared 1200x800 by nine
    # pixels.
    width, height = VIEWPORTS[1]
    page.set_viewport_size({"width": width, "height": height})
    for tool in TOOLS:
        page.goto(f"{BASE.rstrip('/')}/{tool.key}",
                  wait_until="domcontentloaded", timeout=60000)
        settle(page)
        check_help_block(page, tool, f"{width}x{height}")

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
