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
# Community Cloud renders the app inside a frame served under /~/+/, and that
# frame does not exist yet when navigation resolves. Querying the outer shell
# for an app selector therefore waits out the full clock and finds nothing, so
# the frame is waited for separately and briefly.
FRAME_TIMEOUT_MS = 30_000
# A per tool budget, well under the resolve timeout. The first live run spent
# 90 seconds on each of seventeen tools and took 27 minutes to report a result
# that was wrong anyway.
TOOL_TIMEOUT_MS = 45_000
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
    # Tools the prober could not read at all. A failed lookup is not evidence
    # that a tool is absent from the live app. It is not evidence that the tool
    # is present either, and that second half was missing: a run in which every
    # single read failed left missing_tools empty and reported PASS, certifying
    # a deploy it had not looked at once. Asked to prove the deploy is current,
    # a prober that proved nothing has not passed.
    unchecked_tools: list[str] = field(default_factory=list)
    # How many tools this run was asked to verify. Zero means it was not asked,
    # so the two lists below carry no weight either way.
    tools_requested: int = 0
    error: str = ""

    @property
    def verified(self) -> bool:
        """Every tool this run was asked about was positively resolved."""
        if not self.tools_requested:
            return True
        return not self.missing_tools and not self.unchecked_tools

    @property
    def unproven(self) -> bool:
        """Awake, nothing found missing, and nothing actually confirmed."""
        return bool(self.tools_requested and self.unchecked_tools
                    and not self.missing_tools)

    @property
    def ok(self) -> bool:
        return (self.awake and not self.error and not self.missing_tools
                and self.verified)


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
            checked = (f", all {result.tools_requested} tools confirmed"
                       if result.tools_requested else "")
            lines.append(f"  ok   {result.url} {state}{checked}")
        else:
            if result.error:
                reason = result.error
            elif result.missing_tools:
                reason = ("registered tools missing from the live app: "
                          + ", ".join(result.missing_tools))
            elif result.unproven:
                # Deliberately not "stale" and deliberately not a pass. The
                # prober failed to look, so the only honest report is that the
                # deploy is unproven, and a job asked to prove it must not
                # exit 0 having proved nothing.
                confirmed = result.tools_requested - len(result.unchecked_tools)
                reason = (f"could not read {len(result.unchecked_tools)} of "
                          f"{result.tools_requested} tool page(s), so the "
                          f"deploy is UNPROVEN rather than stale "
                          f"({confirmed} confirmed): "
                          + ", ".join(result.unchecked_tools))
            else:
                reason = "the app never rendered"
            lines.append(f"  FAIL {result.url} {reason}")

    passed = len([r for r in results if r.ok])
    total = len(results)
    if passed != total:
        # An app that is awake but whose tools could not be read is a different
        # failure from an app that never came up, and saying "not awake" about
        # a running app sends whoever reads this log looking in the wrong
        # place.
        unproven = len([r for r in results if r.unproven])
        asleep = total - passed - unproven
        parts = []
        if asleep:
            parts.append(f"{asleep} not awake")
        if unproven:
            parts.append(f"{unproven} awake but unproven")
        lines.append("")
        lines.append(f"KEEPALIVE RESULT: FAIL {total - passed} of {total} apps, "
                     + ", ".join(parts))
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


def wait_for_app_frame(page, timeout_ms: int = FRAME_TIMEOUT_MS):
    """Wait until the frame the app renders into exists, then return it.

    On Community Cloud the app lives in a frame under /~/+/ that is created
    after navigation resolves. Looking it up immediately falls back to the
    outer shell, where no app selector will ever appear, so every query then
    burns its whole timeout and reports a tool as missing when the only thing
    that failed was the lookup. Locally there is no frame at all and the page
    itself is the right answer, which is why the fallback stays.
    """
    waited = 0
    step = 500
    while waited < timeout_ms:
        for frame in page.frames:
            if APP_FRAME_PATH in frame.url:
                return frame
        # Running against a local hub there is no frame and never will be, so
        # the page itself is the answer the moment it has painted. Without this
        # the local path would sit out the whole frame timeout on every page.
        try:
            if page.query_selector(AWAKE_SELECTOR) is not None:
                return page
        except Exception:                         # noqa: BLE001
            pass
        page.wait_for_timeout(step)
        waited += step
    return page


def read_main(page, settle_ms: int = 3_500, timeout_ms: int = RESOLVE_TIMEOUT_MS) -> str:
    """Text of the main column, once it has actually painted something.

    Waiting for the app shell is not enough: the shell exists well before
    Streamlit has drawn the page into it, and reading too early reports an
    empty column as a missing tool.
    """
    frame = wait_for_app_frame(page)
    frame.wait_for_selector(AWAKE_SELECTOR, timeout=timeout_ms)
    frame.wait_for_selector(MAIN_SELECTOR, timeout=timeout_ms)
    page.wait_for_timeout(settle_ms)
    return frame.inner_text(MAIN_SELECTOR, timeout=30_000)


def verify_tools(page, app_url: str, tools: list[tuple[str, str]],
                 home_fingerprint: str) -> tuple[list[str], list[str]]:
    """Confirm every registered tool is actually reachable on the live app.

    This is what catches a deploy that never picked up the newest push. A tool
    that exists in the repository but not on the live app means the running
    instance is stale, and saying so loudly beats finding out by hand.

    Two outcomes, kept apart on purpose. A tool is MISSING only when the page
    actually rendered and what rendered was the landing page, which is positive
    evidence that the running app has never heard of it. A tool that could not
    be read at all is UNCHECKED, not missing: a timeout says the prober failed
    to look, not that the tool is absent, and the first live run proved how
    badly that conflation reads by declaring all seventeen tools missing from
    an app that was serving every one of them.
    """
    missing: list[str] = []
    unchecked: list[str] = []
    for key, _title in tools:
        url = tool_url(app_url, key)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            main = read_main(page, timeout_ms=TOOL_TIMEOUT_MS)
        except Exception as exc:                  # noqa: BLE001
            unchecked.append(f"{key} ({type(exc).__name__})")
            print(f"  ?    {url} could not be read: {type(exc).__name__}")
            continue
        if tool_is_live(main, home_fingerprint):
            print(f"  ok   {url}")
        else:
            missing.append(f"{key} (the live app fell back to the landing page)")
            print(f"  FAIL {url} served the landing page, so the deploy is stale")
    return missing, unchecked


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
            # Recorded before the reads, not after, so a run that fails to read
            # every single page still knows how many it was supposed to prove.
            result.tools_requested = len(tools)
            if result.awake and tools:
                # Take the landing page as it stands right now, so a tool page
                # is measured against this app rather than a hardcoded string.
                try:
                    home_fingerprint = fingerprint(read_main(page))
                except Exception as exc:          # noqa: BLE001
                    result.error = f"could not read the landing page: {type(exc).__name__}"
                    results.append(result)
                    continue
                result.missing_tools, result.unchecked_tools = verify_tools(
                    page, app, tools, home_fingerprint)
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
             "unchecked_tools": r.unchecked_tools, "error": r.error}
            for r in results
        ]
    }
    Path("keepalive-summary.json").write_text(json.dumps(summary, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
