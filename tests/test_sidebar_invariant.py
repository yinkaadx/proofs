"""Ground truth test: the tool's help does not move when tools are added.

This exists because the sidebar failed twice in the same way. The tool list
lived above each tool's instructions, so every tool added pushed the help
another row down until it left the screen. Both previous guards measured the
layout at the tool count of the day, which is a measurement that passes every
week until the week it does not.

So this one measures the same page twice, against a hub with sixteen tools and
a hub with forty, and asserts the help begins at the SAME pixel. That is the
property the layout actually has to hold, and it is the only assertion that
fails on the day somebody puts a growing element back above the help.

Run: python3 tests/test_sidebar_invariant.py
Pass marker: final line is exactly "INVARIANT RESULT: PASS <n>/<n>", exit 0.
Fail marker: any line starting "FAIL", plus exit code 1.
Skips cleanly (exit 0) when the browser is unavailable, unless
TOOLBENCH_REQUIRE_BROWSER=1, which turns every skip into a failure.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REQUIRE_BROWSER = os.environ.get("TOOLBENCH_REQUIRE_BROWSER") == "1"
PLAIN_PORT = 8781
PADDED_PORT = 8782
PADDED_TOOLS = 24          # on top of the real ones
PROBE_TOOL = "askew-suit-engine"
VIEWPORT = {"width": 1440, "height": 720}


def unavailable(reason: str) -> None:
    if REQUIRE_BROWSER:
        print(f"INVARIANT RESULT: FAIL (browser guard required but "
              f"unavailable: {reason})")
        sys.exit(1)
    print(f"INVARIANT RESULT: SKIP ({reason})")
    sys.exit(0)


try:
    from playwright.sync_api import sync_playwright

    from tests.browser_util import launch, settle, sidebar_block
except ImportError:
    unavailable("playwright not installed")

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


def wait_for_health(port: int, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/_stcore/health", timeout=2).read()
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    return False


def start_hub(port: int, pad: int) -> subprocess.Popen:
    env = dict(os.environ)
    if pad:
        env["TOOLBENCH_PAD_TOOLS"] = str(pad)
    else:
        env.pop("TOOLBENCH_PAD_TOOLS", None)
    return subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", "streamlit_app.py",
         "--server.port", str(port), "--server.headless", "true",
         "--server.fileWatcherType", "none"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def measure(page, port: int) -> dict:
    page.goto(f"http://127.0.0.1:{port}/{PROBE_TOOL}",
              wait_until="domcontentloaded", timeout=60000)
    settle(page)
    return sidebar_block(page, "How to use")


print("== The help does not move when the tool list grows ==")

servers: list[subprocess.Popen] = []
try:
    servers.append(start_hub(PLAIN_PORT, 0))
    servers.append(start_hub(PADDED_PORT, PADDED_TOOLS))
    for port in (PLAIN_PORT, PADDED_PORT):
        if not wait_for_health(port):
            unavailable(f"hub on port {port} never became healthy")

    with sync_playwright() as playwright:
        try:
            browser, page = launch(playwright, viewport=VIEWPORT)
        except Exception as exc:  # noqa: BLE001
            unavailable(f"browser unavailable: {exc}")

        plain = measure(page, PLAIN_PORT)
        padded = measure(page, PADDED_PORT)
        browser.close()

    expect(plain.get("found") and padded.get("found"),
           "the help block is present on both hubs")

    if plain.get("found") and padded.get("found"):
        expect(plain["top"] == padded["top"],
               f"the help starts at the same pixel with 16 tools and with "
               f"{16 + PADDED_TOOLS} (plain {plain['top']}px, padded "
               f"{padded['top']}px)")
        expect(plain["bottom"] == padded["bottom"],
               f"the help ends at the same pixel in both (plain "
               f"{plain['bottom']}px, padded {padded['bottom']}px)")
        expect(padded["bottom"] <= padded["visibleHeight"],
               f"the whole help block is still on screen with "
               f"{16 + PADDED_TOOLS} tools (ends at {padded['bottom']}px of "
               f"{padded['visibleHeight']}px)")
finally:
    for server in servers:
        server.terminate()
    for server in servers:
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

print()
if failures:
    print(f"INVARIANT RESULT: FAIL {len(failures)} of {checks} checks failed")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print(f"INVARIANT RESULT: PASS {checks}/{checks}")
sys.exit(0)
