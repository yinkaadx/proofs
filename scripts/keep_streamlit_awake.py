"""Keep every Streamlit Community Cloud app in this account awake, and prove it.

Community Cloud puts an app to sleep after a period with no real visitor, and
a plain HTTP request does not count as one. The root URL answers 200 with a
static shell whether the app is running or not, so an uptime pinger reports
green while the app is fast asleep. Only a browser session that opens the
websocket counts, which is why this opens a real headless Chromium.

What one run does, per app:

1. Reads the Streamlit health endpoint, which is the one URL that tells the
   truth about whether the server is up.
2. Opens the app in Chromium.
3. If the sleep screen is showing, clicks "Yes, get this app back up!" and
   waits for the boot to finish.
4. Confirms the app itself rendered inside the app frame, not just the shell.
5. Dwells long enough for the session to register as a visit, which is what
   resets the inactivity clock.
6. Optionally checks that every tool registered in the repository is reachable
   on the live app, which is how a stale deploy gets caught rather than
   discovered by accident.

Pure logic sits at the top with no browser import, so it is unit testable on
its own. Playwright is imported only inside the function that drives a browser.

Run:
    python3 scripts/keep_streamlit_awake.py
    python3 scripts/keep_streamlit_awake.py --verify-tools
    python3 scripts/keep_streamlit_awake.py --apps .github/streamlit-apps.txt

Pass marker: final line is exactly "KEEPALIVE RESULT: PASS <n>/<n> apps awake"
and exit code 0. Fail marker: any line starting "FAIL", plus exit code 1.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP_LIST = ROOT / ".github" / "streamlit-apps.txt"
REGISTRY = ROOT / "tools" / "registry.py"

# The hub is the permanent deploy target, so it is the fallback when no list
# file is present. Adding a future app is one line in the list file.
FALLBACK_APPS = ("https://proofs-toolbench.streamlit.app",)

# Community Cloud serves the running app inside a frame under this path, and
# exposes the Streamlit health endpoint underneath it. The root URL cannot be
# trusted: it answers 200 from a static shell even while the app is asleep.
APP_FRAME_PATH = "/~/+/"
HEALTH_PATH = "/~/+/_stcore/health"

# The sleep screen, and the button on it. Community Cloud shows a different
# test id to the owner than to an ordinary viewer, so both are matched.
SLEEP_SELECTOR = '[data-testid="wakeup-button-viewer"], [data-testid="wakeup-button-owner"]'
AWAKE_SELECTOR = '[data-testid="stApp"], .stApp'
# The main content column, which is the only part of the page that changes
# between one tool and another. The sidebar lists every registered tool on
# every page, so reading the whole body proves nothing about which page ran.
MAIN_SELECTOR = '[data-testid="stMain"]'

# Timeouts in milliseconds. A cold boot pulls the environment and can genuinely
# take minutes, so the wake wait is deliberately generous.
NAV_TIMEOUT_MS = 90_000
RESOLVE_TIMEOUT_MS = 90_000
WAKE_TIMEOUT_MS = 300_000
# Long enough for the session to register as a real visit rather than a bounce.
DWELL_MS = 12_000


# ---------------------------------------------------------------------------
# Pure logic
# ---------------------------------------------------------------------------

@dataclass
class AppResult:
    url: str
    reachable: bool = False
    was_asleep: bool = False
    awake: bool = False
    health: str = ""
    missing_tools: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.awake and not self.missing_tools and not self.error


def normalize_app_url(value: str) -> str:
    """Accept a bare host or a full URL, and never keep a trailing slash."""
    text = str(value or "").strip()
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        text = "https://" + text
    return text.rstrip("/")


def parse_app_list(text: str) -> list[str]:
    """One app per line. Blank lines and comments are ignored.

    Adding a future app to the never sleep guarantee is a single line here,
    which is the whole point of keeping the list outside the workflow.
    """
    apps: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        url = normalize_app_url(line)
        if url and url not in apps:
            apps.append(url)
    return apps


def load_apps(path: Path | None) -> list[str]:
    if path is not None and Path(path).is_file():
        apps = parse_app_list(Path(path).read_text())
        if apps:
            return apps
    return list(FALLBACK_APPS)


def health_url(app_url: str) -> str:
    return normalize_app_url(app_url) + HEALTH_PATH


def tool_url(app_url: str, key: str) -> str:
    return f"{normalize_app_url(app_url)}/{str(key).strip('/')}"


def extract_tools(registry_source: str) -> list[tuple[str, str]]:
    """Read the registered tool keys and titles without importing the registry.

    Importing it would pull in Streamlit and every tool page, which a keep
    awake job has no business doing. A regex over the source is enough,
    because the registry is a literal list by design.
    """
    keys = re.findall(r'key\s*=\s*["\']([^"\']+)["\']', registry_source)
    titles = re.findall(r'title\s*=\s*["\']([^"\']+)["\']', registry_source)
    pairs: list[tuple[str, str]] = []
    for index, key in enumerate(keys):
        pairs.append((key, titles[index] if index < len(titles) else ""))
    return pairs


def fingerprint(text: str) -> str:
    """A short, whitespace stable signature of a rendered main column."""
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()[:200]


def tool_is_live(main_text: str, home_fingerprint: str) -> bool:
    """Decide whether a tool URL really served that tool's own page.

    Streamlit does not error on an unknown page path: it quietly falls back to
    the default page, so the request looks successful while the tool is not
    there at all. The landing page is therefore the tell. If the main column
    of /some-tool is the same as the main column of /, the running app has
    never heard of that tool and the deploy is stale.
    """
    signature = fingerprint(main_text)
    if not signature:
        return False
    return signature != home_fingerprint


def verdict(results: list[AppResult]) -> tuple[int, list[str]]:
    """Turn results into the exit code and the lines a CI log is parsed for."""
    lines: list[str] = []
    for result in results:
        if result.ok:
            state = "woken from sleep" if result.was_asleep else "already awake"
            lines.append(f"  ok   {result.url} {state}")
        else:
            if result.error:
                reason = result.error
            elif result.missing_tools:
                reason = ("registered tools missing from the live app: "
                          + ", ".join(result.missing_tools))
            else:
                reason = "the app never rendered"
            lines.append(f"  FAIL {result.url} {reason}")

    passed = len([r for r in results if r.ok])
    total = len(results)
    if passed != total:
        lines.append("")
        lines.append(f"KEEPALIVE RESULT: FAIL {total - passed} of {total} apps not awake")
        return 1, lines
    lines.append("")
    lines.append(f"KEEPALIVE RESULT: PASS {passed}/{total} apps awake")
    return 0, lines


def read_health(app_url: str, timeout: int = 30) -> str:
    """Ask the one endpoint that cannot lie about whether the server is up."""
    try:
        request = urllib.request.Request(
            health_url(app_url),
            headers={"User-Agent": "toolbench-keepalive/1.0"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace").strip()[:40]
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        return f"unreachable: {type(exc).__name__}"


# ---------------------------------------------------------------------------
# Browser work
# ---------------------------------------------------------------------------

def _app_frame(page):
    """The frame the real app renders into, or the page itself as a fallback."""
    for frame in page.frames:
        if APP_FRAME_PATH in frame.url:
            return frame
    return page


def visit(page, app_url: str, shots: Path | None, label: str) -> AppResult:
    """Open one app, wake it if it is sleeping, and confirm it actually ran."""
    result = AppResult(url=app_url)
    result.health = read_health(app_url)

    try:
        page.goto(app_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        result.reachable = True
    except Exception as exc:                      # noqa: BLE001
        result.error = f"navigation failed: {type(exc).__name__}: {exc}"
        return result

    # Is the sleep screen up? Its presence is the only reliable signal, because
    # the root URL answers 200 either way.
    try:
        page.wait_for_selector(SLEEP_SELECTOR, timeout=15_000)
        result.was_asleep = True
    except Exception:                             # noqa: BLE001
        result.was_asleep = False

    if result.was_asleep:
        try:
            page.locator(SLEEP_SELECTOR).first.click(timeout=30_000)
            print(f"  .    {app_url} was asleep, clicked the wake button")
        except Exception as exc:                  # noqa: BLE001
            result.error = f"could not click the wake button: {type(exc).__name__}"
            return result

    timeout = WAKE_TIMEOUT_MS if result.was_asleep else RESOLVE_TIMEOUT_MS
    try:
        frame = _app_frame(page)
        frame.wait_for_selector(AWAKE_SELECTOR, timeout=timeout)
        result.awake = True
    except Exception:                             # noqa: BLE001
        # The frame may only appear once the boot finishes, so look again.
        try:
            page.wait_for_timeout(5_000)
            frame = _app_frame(page)
            frame.wait_for_selector(AWAKE_SELECTOR, timeout=60_000)
            result.awake = True
        except Exception as exc:                  # noqa: BLE001
            result.error = f"the app never rendered: {type(exc).__name__}"
            return result

    # Dwell, so Community Cloud counts this as a visit rather than a bounce.
    # This is the part that actually resets the inactivity clock.
    page.wait_for_timeout(DWELL_MS)
    result.health = read_health(app_url)

    if shots is not None:
        Path(shots).mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(Path(shots) / f"{label}.png"), full_page=False)
        except Exception:                         # noqa: BLE001
            pass
    return result


def read_main(page, settle_ms: int = 3_500) -> str:
    """Text of the main column, once it has actually painted something.

    Waiting for the app shell is not enough: the shell exists well before
    Streamlit has drawn the page into it, and reading too early reports an
    empty column as a missing tool.
    """
    frame = _app_frame(page)
    frame.wait_for_selector(AWAKE_SELECTOR, timeout=RESOLVE_TIMEOUT_MS)
    frame.wait_for_selector(MAIN_SELECTOR, timeout=RESOLVE_TIMEOUT_MS)
    page.wait_for_timeout(settle_ms)
    return frame.inner_text(MAIN_SELECTOR, timeout=30_000)


def verify_tools(page, app_url: str, tools: list[tuple[str, str]],
                 home_fingerprint: str) -> list[str]:
    """Confirm every registered tool is actually reachable on the live app.

    This is what catches a deploy that never picked up the newest push. A tool
    that exists in the repository but not on the live app means the running
    instance is stale, and saying so loudly beats finding out by hand.
    """
    missing: list[str] = []
    for key, _title in tools:
        url = tool_url(app_url, key)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            main = read_main(page)
        except Exception as exc:                  # noqa: BLE001
            missing.append(f"{key} ({type(exc).__name__})")
            continue
        if tool_is_live(main, home_fingerprint):
            print(f"  ok   {url}")
        else:
            missing.append(f"{key} (the live app fell back to the landing page)")
    return missing


def chromium_executable(explicit: str = "") -> str | None:
    """Where Chromium lives, when it is not where Playwright expects it.

    On the Actions runner `playwright install chromium` puts it in the default
    place and this returns nothing. In a container that already ships a
    browser, KEEPALIVE_CHROMIUM points at it so the job does not download a
    second copy.
    """
    import os
    path = str(explicit or os.environ.get("KEEPALIVE_CHROMIUM", "")).strip()
    return path or None


def run(apps: list[str], tools: list[tuple[str, str]],
        shots: Path | None, chromium: str = "") -> list[AppResult]:
    from playwright.sync_api import sync_playwright   # imported here on purpose

    results: list[AppResult] = []
    launch_kwargs = {"args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    executable = chromium_executable(chromium)
    if executable:
        launch_kwargs["executable_path"] = executable

    with sync_playwright() as driver:
        browser = driver.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        )
        page = context.new_page()
        for index, app in enumerate(apps):
            print(f"== {app} ==")
            result = visit(page, app, shots, label=f"app{index + 1}")
            if result.awake and tools:
                # Take the landing page as it stands right now, so a tool page
                # is measured against this app rather than a hardcoded string.
                try:
                    home_fingerprint = fingerprint(read_main(page))
                except Exception as exc:          # noqa: BLE001
                    result.error = f"could not read the landing page: {type(exc).__name__}"
                    results.append(result)
                    continue
                result.missing_tools = verify_tools(page, app, tools,
                                                    home_fingerprint)
            results.append(result)
            print(f"  .    health endpoint says: {result.health}")
        context.close()
        browser.close()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Keep Streamlit apps awake.")
    parser.add_argument("--apps", default=str(DEFAULT_APP_LIST),
                        help="File listing one app URL per line.")
    parser.add_argument("--verify-tools", action="store_true",
                        help="Also check every registered tool is live, which "
                             "catches a deploy that never picked up a push.")
    parser.add_argument("--registry", default=str(REGISTRY),
                        help="Registry file to read tool keys from.")
    parser.add_argument("--shots", default="",
                        help="Directory for screenshots kept as evidence.")
    parser.add_argument("--chromium", default="",
                        help="Path to an existing Chromium binary. Defaults to "
                             "KEEPALIVE_CHROMIUM, then to Playwright's own.")
    args = parser.parse_args(argv)

    apps = load_apps(Path(args.apps))
    tools: list[tuple[str, str]] = []
    if args.verify_tools:
        registry_path = Path(args.registry)
        if registry_path.is_file():
            tools = extract_tools(registry_path.read_text())
        print(f"Verifying {len(tools)} registered tool"
              f"{'' if len(tools) == 1 else 's'} against the live app.")

    shots = Path(args.shots) if args.shots else None
    print(f"Keeping {len(apps)} app{'' if len(apps) == 1 else 's'} awake.")
    results = run(apps, tools, shots, args.chromium)

    code, lines = verdict(results)
    print()
    for line in lines:
        print(line)

    summary = {
        "apps": [
            {"url": r.url, "was_asleep": r.was_asleep, "awake": r.awake,
             "health": r.health, "missing_tools": r.missing_tools,
             "error": r.error}
            for r in results
        ]
    }
    Path("keepalive-summary.json").write_text(json.dumps(summary, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
