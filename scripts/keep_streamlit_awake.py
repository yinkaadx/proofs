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

Two questions, two exit codes. "Is the app awake" is what the three hourly
schedule exists to answer, and its failure means the app is down. "Is the live
app current with the deploy branch" is a different question with a different
owner, Community Cloud's deploy path, which is known to apply a push late or
not at all. Answering the second question with the first question's exit code
produced a failure email every three hours for a condition that was already
known, was not a sleeping app, and did not change between runs. Alert fatigue
is how the next real outage gets deleted unread.

So the tool check has two modes:

  strict (default)     a stale or unproven deploy fails the run. Kept for a
                       hand run that wants a plain yes or no. Neither job in
                       keep-awake.yml uses it any more.
  --tools-as-warnings  a stale deploy is reported as a warning annotation and
                       in the job summary, and the run passes as long as the
                       app is awake and serving. Used by both jobs, because a
                       change that Community Cloud has not applied yet is the
                       normal state for up to two hours and is not news.
  --stale-grace-seconds with --deployed-at: a deploy that is positively stale,
                       a tool missing or a build label behind the branch, and
                       whose branch head has been out longer than the grace,
                       fails after all. That is a stuck deploy, the one deploy
                       condition worth an email, because the fix is a person
                       pressing Reboot on share.streamlit.io, and it repeats
                       every scheduled run until that button is pressed. Only
                       the schedule passes these two: a push run is inside any
                       sensible grace by definition, and the commit time of a
                       pushed head can be hours old when an old commit is
                       pushed back on purpose. A page that merely could not
                       be read never escalates: the first draft of this let
                       it, and one flaky read on a quiet branch brought the
                       every three hours email straight back. Nor does an
                       empty main column, a pass that ran out of time, or a
                       build label that is not in the branch history at all:
                       each is the prober failing to look, not the deploy
                       failing to land.

Two states that are not "stale". An app whose every registered tool falls
back to the landing page is not running an old build, it is not serving tools
at all, and that is reported as down in both modes. An app whose sidebar
reports a build that is not the deploy branch head is stale even when every
tool key resolves, because a push that changes an existing tool adds no key.

Run:
    python3 scripts/keep_streamlit_awake.py
    python3 scripts/keep_streamlit_awake.py --verify-tools
    python3 scripts/keep_streamlit_awake.py --verify-tools --tools-as-warnings
    python3 scripts/keep_streamlit_awake.py --verify-tools \
        --retry-until-current 480 --retry-every 60

Pass marker: final line is exactly "KEEPALIVE RESULT: PASS <n>/<n> apps awake"
and exit code 0. Fail marker: any line starting "FAIL", plus exit code 1. The
line before the blank line that precedes the result is always
"DEPLOY STATUS: <CURRENT|STALE|UNPROVEN|UNCHECKED> ..." when tools were asked
about, so the deploy question can be parsed on its own.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
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
# The sidebar carries the running build, as "build <sha>", which is the one
# fact that turns "a tool is missing" into "the app is running d611e14 and the
# branch is at c06bbb6". The prober reads it rather than inferring it.
SIDEBAR_SELECTOR = '[data-testid="stSidebar"]'
BUILD_LABEL = re.compile(r"\bbuild ([0-9a-f]{7,40}|unknown)\b")

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
    # What the running app says it is, read from its own sidebar. Empty when it
    # could not be read, "unknown" when the app itself does not know.
    live_build: str = ""
    # How many extra passes the tool check made waiting for the deploy to
    # catch up. Zero on a single look.
    retries: int = 0
    # The commit the deploy branch is at, when the caller knows it. A live
    # build that is neither this nor "unknown" is a checkout that has not been
    # updated, which is staleness with no missing tool to show for it.
    expected_build: str = ""
    # Seconds since the deploy branch head was committed, when the caller
    # knows it. Only ever read together with positive staleness.
    stale_for: float | None = None
    # Commits on the deploy branch behind the head, when the caller knows
    # them. With this list a mismatch counts only when the live label is one
    # of these, an old checkout of this very branch. Without it any label
    # that is not the head counts, which is the deploy-current job's view a
    # minute after a push and is never escalated.
    known_builds: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        """Every tool this run was asked about was positively resolved."""
        if not self.tools_requested:
            return True
        return (not self.missing_tools and not self.unchecked_tools
                and not self.build_unrecognised)

    @property
    def unproven(self) -> bool:
        """Awake, nothing found missing, and something not confirmed."""
        return bool(self.tools_requested
                    and (self.unchecked_tools or self.build_unrecognised)
                    and not self.missing_tools and not self.build_mismatch)

    @property
    def build_mismatch(self) -> bool:
        """The sidebar names an older commit of this branch than the head.

        A match proves nothing: the label is read from the checkout at render
        time and a git pull updates the checkout before the process reloads.
        A mismatch is proof, because an old checkout cannot be running new
        code. So only the mismatch is ever acted on, and when the branch
        history is known, only a label found in that history counts: a label
        that is on no known commit (a merge Community Cloud made on its side,
        a detached checkout) says nothing about whether the code is old, and
        treating it as proof would fail every scheduled run until a Reboot.
        """
        live = self.live_build
        if not (self.expected_build and live and live != "unknown"):
            return False
        if self.expected_build.startswith(live):
            return False
        if self.known_builds:
            return any(sha.startswith(live) for sha in self.known_builds)
        return True

    @property
    def build_unrecognised(self) -> bool:
        """A real label that is neither the head nor any known ancestor."""
        live = self.live_build
        return bool(self.expected_build and self.known_builds and live
                    and live != "unknown"
                    and not self.expected_build.startswith(live)
                    and not any(sha.startswith(live) for sha in self.known_builds))

    @property
    def ok(self) -> bool:
        return (self.awake and not self.error and not self.missing_tools
                and self.verified and not self.build_mismatch)

    @property
    def up(self) -> bool:
        """The question the schedule asks: is the app awake and serving."""
        return self.awake and not self.error

    @property
    def deploy_status(self) -> str:
        """CURRENT, STALE, UNPROVEN or UNCHECKED. Never a boolean.

        UNCHECKED covers both "not asked" and "asked but never got to look",
        because an app that never rendered has told the prober nothing about
        its deploy, and reporting CURRENT there was a lie the JSON carried.
        """
        if not self.tools_requested or not self.up:
            return "UNCHECKED"
        if self.missing_tools or self.build_mismatch:
            return "STALE"
        if self.unchecked_tools or self.build_unrecognised:
            return "UNPROVEN"
        return "CURRENT"


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


def _optional_float(text: str) -> float | None:
    """argparse type: a number, or None for blank. A registry step whose git
    log printed nothing must not turn into a usage error, exit 2, which is a
    failure that is neither a down app nor a stuck deploy."""
    text = str(text or "").strip()
    return float(text) if text else None


def load_known_builds(path: Path) -> tuple[str, ...]:
    """Commit shas, one per line, as written by `git log --format=%H`."""
    if not path.is_file():
        return ()
    return tuple(line.strip().lower() for line in path.read_text().splitlines()
                 if re.fullmatch(r"[0-9a-f]{7,40}", line.strip().lower()))


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


def parse_build_label(text: str) -> str:
    """The sha the app prints in its sidebar, or "" when there is none."""
    match = BUILD_LABEL.search(str(text or ""))
    return match.group(1) if match else ""


def _tool_problem(result: AppResult) -> str:
    """The sentence that says what is wrong with the deploy, or ""."""
    if result.missing_tools:
        text = ("registered tools missing from the live app: "
                + ", ".join(result.missing_tools))
        if result.unchecked_tools:
            # Still named, so the reader knows how much of the deploy was
            # actually examined rather than only what was found wanting.
            text += (f"; and {len(result.unchecked_tools)} tool page(s) could "
                     f"not be read: " + ", ".join(result.unchecked_tools))
        return text
    if result.build_mismatch:
        return (f"the live app reports build {result.live_build}, but the "
                f"deploy branch is at {result.expected_build[:7]}, so the push "
                f"has not been applied even though every tool key resolves")
    if result.unproven:
        # Deliberately not "stale" and deliberately not a pass. The prober
        # failed to look, so the only honest report is that the deploy is
        # unproven, and a job asked to prove it must not exit 0 having proved
        # nothing.
        parts = []
        if result.unchecked_tools:
            confirmed = result.tools_requested - len(result.unchecked_tools)
            parts.append(f"could not read {len(result.unchecked_tools)} of "
                         f"{result.tools_requested} tool page(s) "
                         f"({confirmed} confirmed): "
                         + ", ".join(result.unchecked_tools))
        if result.build_unrecognised:
            parts.append(f"the live app reports build {result.live_build}, "
                         f"which is neither the deploy branch head "
                         f"{result.expected_build[:7]} nor any of the "
                         f"{len(result.known_builds)} known commits behind "
                         f"it, so the label proves nothing either way")
        return "; and ".join(parts) + ", so the deploy is UNPROVEN rather than stale"
    return ""


def _build_note(result: AppResult) -> str:
    """The label, stated. Never "matches": a match proves nothing."""
    if not result.live_build or result.build_mismatch:
        return ""
    return f" (live app reports build {result.live_build})"


_STATUS_RANK = {"UNCHECKED": 0, "CURRENT": 1, "UNPROVEN": 2, "STALE": 3}


def deploy_status_line(results: list[AppResult]) -> str:
    """One parseable line about the deploy, separate from the wake verdict."""
    asked = [r for r in results if r.tools_requested]
    if not asked:
        return "DEPLOY STATUS: UNCHECKED, tool verification was not requested"
    worst = max(asked, key=lambda r: _STATUS_RANK[r.deploy_status])
    status = worst.deploy_status
    if status == "UNCHECKED":
        detail = "the app never rendered, so nothing was checked"
    elif status == "CURRENT":
        detail = ", ".join(f"all {r.tools_requested} tools confirmed"
                           for r in asked)
    else:
        detail = "; ".join(_tool_problem(r) for r in asked if _tool_problem(r))
    retried = max((r.retries for r in asked), default=0)
    if retried:
        detail += f" (after {retried} retr{'y' if retried == 1 else 'ies'})"
    return f"DEPLOY STATUS: {status}, {detail}{_build_note(worst)}"


def past_grace(result: AppResult, grace_seconds: float | None) -> bool:
    """Positively stale, and out longer than Community Cloud has ever taken.

    Positive means a tool fell back to the landing page or the build label is
    behind the branch. UNPROVEN never qualifies: a page that could not be read
    is the prober's failure, not the deploy's, and escalating on it turned one
    flaky read on a quiet branch into a failure every three hours.
    """
    return bool(grace_seconds is not None and result.stale_for is not None
                and result.stale_for > grace_seconds
                and result.deploy_status == "STALE")


def verdict(results: list[AppResult], stale_is_failure: bool = True,
            stale_grace_seconds: float | None = None) -> tuple[int, list[str]]:
    """Turn results into the exit code and the lines a CI log is parsed for.

    stale_is_failure True is the strict mode: a stale or unproven deploy fails
    the run. False is the warning mode, where the app being up and serving is
    the pass and the deploy is reported without deciding the exit code, with
    one exception: a deploy that past_grace() says is stuck fails anyway.
    """
    lines: list[str] = []
    # Three different failures, counted apart, because saying "not awake"
    # about a running app sends whoever reads this log to the wrong place.
    down = stale = unproven = 0
    for result in results:
        problem = _tool_problem(result)
        state = "woken from sleep" if result.was_asleep else "already awake"
        stuck = past_grace(result, stale_grace_seconds)
        if result.ok:
            checked = (f", all {result.tools_requested} tools confirmed"
                       if result.tools_requested else "")
            lines.append(f"  ok   {result.url} {state}{checked}")
        elif not result.up:
            lines.append(f"  FAIL {result.url} "
                         f"{result.error or 'the app never rendered'}")
            down += 1
        elif stale_is_failure or stuck:
            suffix = ""
            if stuck:
                suffix = (f". The deploy branch head has been out for "
                          f"{result.stale_for / 3600:.1f} hours, past the "
                          f"{stale_grace_seconds / 3600:.0f} hour grace within "
                          f"which Community Cloud has applied every push on "
                          f"its own, so this deploy is stuck. Press Reboot on "
                          f"share.streamlit.io")
            lines.append(f"  FAIL {result.url} {problem}{suffix}")
            if result.unproven:
                unproven += 1
            else:
                stale += 1
        else:
            lines.append(f"  WARN {result.url} {state}, but {problem}")

    total = len(results)
    if any(r.tools_requested for r in results):
        lines.append(deploy_status_line(results))
    failures = down + stale + unproven
    if failures:
        parts = []
        if down:
            parts.append(f"{down} not awake")
        if stale:
            parts.append(f"{stale} awake but stale")
        if unproven:
            parts.append(f"{unproven} awake but unproven")
        lines.append("")
        lines.append(f"KEEPALIVE RESULT: FAIL {failures} of {total} apps, "
                     + ", ".join(parts))
        return 1, lines
    lines.append("")
    lines.append(f"KEEPALIVE RESULT: PASS {total}/{total} apps awake")
    return 0, lines


def read_health(app_url: str, timeout: int = 30) -> str:
    """The server's own health endpoint, recorded as evidence in the log and
    the summary. It does not decide the verdict: the rendered app frame does,
    because that is what a visitor sees, and a flaky health read on an app
    that is visibly serving must not become a failure."""
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


def read_build_label(page, timeout_ms: int = 15_000) -> str:
    """What the running app says its build is, from its own sidebar.

    Empty when the sidebar could not be read in time. That is reported as
    "not read" and never as a mismatch, because a prober that failed to look
    has learned nothing about the app.
    """
    try:
        frame = wait_for_app_frame(page)
        frame.wait_for_selector(SIDEBAR_SELECTOR, timeout=timeout_ms)
        return parse_build_label(frame.inner_text(SIDEBAR_SELECTOR,
                                                  timeout=timeout_ms))
    except Exception:                             # noqa: BLE001
        return ""


def _key_of(entry: str) -> str:
    """The tool key from a "key (reason)" entry in a missing or unchecked list."""
    return entry.split(" (", 1)[0]


# One tool read at its worst: navigation, the app frame, the app shell, the
# main column and the text read can each time out in turn. A pass budget is
# overrun by at most this much, which is what the job timeouts are sized to.
WORST_TOOL_READ_S = (NAV_TIMEOUT_MS + FRAME_TIMEOUT_MS + 2 * TOOL_TIMEOUT_MS
                     + 30_000) / 1000


def verify_tools(page, app_url: str, tools: list[tuple[str, str]],
                 home_fingerprint: str,
                 only: set[str] | None = None,
                 budget_seconds: float | None = None,
                 clock=time.monotonic) -> tuple[list[str], list[str]]:
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
    an app that was serving every one of them. An empty main column is the
    same: nothing painted in time is the prober's failure, and calling it the
    landing page turned one slow paint into positive staleness.

    The budget is wall clock per pass. A pass in which every page times out
    would otherwise run over an hour and the job would die with a generic
    timeout, which is a failure email about an app that is up. Past the
    budget the rest of the list is UNCHECKED and the pass ends.
    """
    missing: list[str] = []
    unchecked: list[str] = []
    started = clock()
    wanted = [key for key, _title in tools if only is None or key in only]
    for position, key in enumerate(wanted):
        # A retry only needs to look again at what was not confirmed. Reading
        # all thirty tools every pass costs three and a half minutes a pass,
        # which is most of the wait budget spent re proving what is already
        # proved.
        if budget_seconds is not None and clock() - started > budget_seconds:
            rest = wanted[position:]
            unchecked.extend(f"{k} (pass budget of {int(budget_seconds)}s spent)"
                             for k in rest)
            print(f"  ?    pass budget of {int(budget_seconds)}s spent, "
                  f"{len(rest)} tool(s) left unread")
            break
        url = tool_url(app_url, key)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            main = read_main(page, timeout_ms=TOOL_TIMEOUT_MS)
        except Exception as exc:                  # noqa: BLE001
            unchecked.append(f"{key} ({type(exc).__name__})")
            print(f"  ?    {url} could not be read: {type(exc).__name__}")
            continue
        if not fingerprint(main):
            unchecked.append(f"{key} (empty main column)")
            print(f"  ?    {url} painted nothing in time, so it was not read")
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


BROKEN_APP = ("every registered tool fell back to the landing page, so the "
              "app is not serving tools at all; that is a broken build, not a "
              "stale one")


def _merge_passes(previous_missing: list[str], missing_now: list[str],
                  unchecked_now: list[str]) -> tuple[list[str], list[str]]:
    """Fold a retry pass into what is already known.

    A tool proven missing on an earlier pass stays missing until a later pass
    confirms it live. A timeout on the re read is not a confirmation, and
    letting it downgrade a proved STALE into an UNPROVEN was a real bug.
    """
    proved = {_key_of(entry): entry for entry in previous_missing}
    unchecked_keys = {_key_of(entry) for entry in unchecked_now}
    carried = [proved[key] for key in proved if key in unchecked_keys]
    missing = list(missing_now) + carried
    unchecked = [entry for entry in unchecked_now
                 if _key_of(entry) not in proved]
    return missing, unchecked


def run(apps: list[str], tools: list[tuple[str, str]],
        shots: Path | None, chromium: str = "",
        retry_until_current: float = 0, retry_every: float = 60,
        expect_build: str = "", known_builds: tuple[str, ...] = (),
        pass_budget: float | None = None,
        sleep=time.sleep, clock=time.monotonic) -> list[AppResult]:
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
            result.expected_build = expect_build
            result.known_builds = tuple(known_builds)
            if result.awake and tools:
                # Take the landing page as it stands right now, so a tool page
                # is measured against this app rather than a hardcoded string.
                try:
                    home_fingerprint = fingerprint(read_main(page))
                except Exception as exc:          # noqa: BLE001
                    result.error = f"could not read the landing page: {type(exc).__name__}"
                    results.append(result)
                    continue
                result.live_build = read_build_label(page)
                print(f"  .    live app reports build "
                      f"{result.live_build or 'not read'}")
                result.missing_tools, result.unchecked_tools = verify_tools(
                    page, app, tools, home_fingerprint,
                    budget_seconds=pass_budget, clock=clock)
                # Community Cloud applies a push late, sometimes very late, so
                # a look taken a minute after the push reports a stale deploy
                # that is merely a young one. Keep looking, at only the tools
                # not yet confirmed, until they appear or the budget is spent.
                deadline = clock() + retry_until_current
                while ((result.missing_tools or result.unchecked_tools
                        or result.build_mismatch) and clock() < deadline):
                    pending = {_key_of(k) for k in result.missing_tools}
                    pending |= {_key_of(k) for k in result.unchecked_tools}
                    remaining = max(0.0, deadline - clock())
                    wait = min(float(retry_every), remaining)
                    what = (f"{len(pending)} tool(s) not confirmed yet"
                            if pending else "the live build is behind")
                    print(f"  .    {what}, looking again in {int(wait)}s "
                          f"({int(remaining)}s of the wait budget left)")
                    sleep(wait)
                    result.retries += 1
                    if pending:
                        missing, unchecked = verify_tools(
                            page, app, tools, home_fingerprint, only=pending,
                            budget_seconds=pass_budget, clock=clock)
                        result.missing_tools, result.unchecked_tools = (
                            _merge_passes(result.missing_tools, missing,
                                          unchecked))
                    else:
                        # Nothing to re read but the label itself, which only
                        # refreshes on a navigation.
                        try:
                            page.goto(app, wait_until="domcontentloaded",
                                      timeout=NAV_TIMEOUT_MS)
                        except Exception:             # noqa: BLE001
                            pass
                    # Read after the navigation above, not before it, or the
                    # label is one pass stale on the very pass that confirms.
                    result.live_build = read_build_label(page) or result.live_build
                if tools and len(result.missing_tools) == len(tools):
                    # A stale deploy is missing the newest tool or two. An app
                    # missing every one of them is serving no tools: a boot
                    # failure that renders the landing page, or a traceback
                    # under the app shell. That is down, in both modes.
                    result.error = BROKEN_APP
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
    parser.add_argument("--tools-as-warnings", action="store_true",
                        help="Report a stale or unproven deploy as a warning "
                             "and pass as long as the app is awake. For the "
                             "schedule, whose failure must mean the app is "
                             "down.")
    parser.add_argument("--stale-grace-seconds", type=float, default=None,
                        help="With --tools-as-warnings and --deployed-at: a "
                             "positively stale deploy whose branch head has "
                             "been out longer than this fails anyway, because "
                             "it is stuck and needs a person.")
    parser.add_argument("--deployed-at", type=_optional_float, default=None,
                        help="Unix time the deploy branch head was committed, "
                             "so the age of a stale deploy can be stated. "
                             "Blank means unknown, not a usage error.")
    parser.add_argument("--expect-build", default="",
                        help="The commit the deploy branch is at, to compare "
                             "against the build the live app reports.")
    parser.add_argument("--known-builds", default="",
                        help="File of commit shas on the deploy branch, one "
                             "per line, newest first. With it a build label "
                             "counts as stale only when it names one of "
                             "these; a label on no known commit is UNPROVEN.")
    parser.add_argument("--pass-budget", type=float, default=None,
                        help="Wall clock seconds one pass over the tool list "
                             "may take before the rest is left unchecked, so "
                             "a slow app is UNPROVEN rather than a job that "
                             "dies of its own timeout.")
    parser.add_argument("--retry-until-current", type=float, default=0,
                        help="Seconds to keep re checking unconfirmed tools "
                             "before giving a verdict. 0 looks once.")
    parser.add_argument("--retry-every", type=float, default=60,
                        help="Seconds between looks while retrying.")
    args = parser.parse_args(argv)

    apps = load_apps(Path(args.apps))
    tools: list[tuple[str, str]] = []
    if args.verify_tools:
        registry_path = Path(args.registry)
        if registry_path.is_file():
            tools = extract_tools(registry_path.read_text())
        print(f"Verifying {len(tools)} registered tool"
              f"{'' if len(tools) == 1 else 's'} against the live app.")

    known_builds = load_known_builds(Path(args.known_builds)) if args.known_builds else ()
    if known_builds:
        print(f"{len(known_builds)} commit(s) of the deploy branch are known, "
              f"so only a label on one of them counts as an old checkout.")

    shots = Path(args.shots) if args.shots else None
    print(f"Keeping {len(apps)} app{'' if len(apps) == 1 else 's'} awake.")
    results = run(apps, tools, shots, args.chromium,
                  retry_until_current=args.retry_until_current,
                  retry_every=args.retry_every,
                  expect_build=args.expect_build,
                  known_builds=known_builds,
                  pass_budget=args.pass_budget)

    if args.deployed_at is not None:
        age = max(0.0, time.time() - args.deployed_at)
        for result in results:
            result.stale_for = age

    code, lines = verdict(results,
                          stale_is_failure=not args.tools_as_warnings,
                          stale_grace_seconds=args.stale_grace_seconds)
    print()
    for line in lines:
        print(line)
    emit_annotations(lines)

    summary = {
        "expected_build": args.expect_build,
        "apps": [
            {"url": r.url, "was_asleep": r.was_asleep, "awake": r.awake,
             "health": r.health, "missing_tools": r.missing_tools,
             "unchecked_tools": r.unchecked_tools, "error": r.error,
             "live_build": r.live_build, "expected_build": r.expected_build,
             "build_mismatch": r.build_mismatch,
             "build_unrecognised": r.build_unrecognised,
             "known_builds": len(r.known_builds),
             "deploy_status": r.deploy_status, "retries": r.retries,
             "up": r.up, "stale_for_seconds": r.stale_for}
            for r in results
        ]
    }
    Path("keepalive-summary.json").write_text(json.dumps(summary, indent=2))
    write_step_summary(lines)
    return code


def emit_annotations(lines: list[str]) -> None:
    """Surface WARN and FAIL lines where GitHub shows them on the run page.

    A warning annotation is visible without opening the log and does not send
    a failure email, which is exactly the standing a known stale deploy should
    have between one push and the next.
    """
    for line in lines:
        text = line.strip()
        if text.startswith("WARN "):
            print(f"::warning::{text[5:]}")
        elif text.startswith("FAIL "):
            print(f"::error::{text[5:]}")


def write_step_summary(lines: list[str]) -> None:
    """The verdict in the job summary, so nobody has to read the log."""
    path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("### Keep awake\n\n```\n")
            handle.write("\n".join(line for line in lines if line.strip()))
            handle.write("\n```\n")
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
