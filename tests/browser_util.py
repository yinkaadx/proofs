"""Shared helpers for browser driven tests.

The point of this module is to stop waiting by the clock. Streamlit stamps
`data-test-script-state` onto `.stApp`, so a test can wait for the script to
actually finish instead of sleeping for a guessed number of seconds. Fixed
sleeps are both slower and less reliable: too short and the test flakes, too
long and every run pays for the worst case.
"""

from __future__ import annotations

import urllib.error
import urllib.request

CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
DEFAULT_BASE = "http://127.0.0.1:8600"

IDLE = ("() => document.querySelector('.stApp')"
        "?.getAttribute('data-test-script-state') === 'notRunning'")
BUSY = ("() => document.querySelector('.stApp')"
        "?.getAttribute('data-test-script-state') !== 'notRunning'")


def hub_is_up(base: str, timeout: int = 5) -> bool:
    try:
        urllib.request.urlopen(f"{base}/_stcore/health", timeout=timeout).read()
        return True
    except (urllib.error.URLError, OSError):
        return False


def launch(playwright, **context_kwargs):
    """Launch Chromium and return (browser, page). Raises if unavailable."""
    browser = playwright.chromium.launch(executable_path=CHROME,
                                         args=["--no-sandbox"])
    context = browser.new_context(**context_kwargs)
    return browser, context.new_page()


def settle(page, timeout: int = 30000) -> None:
    """Wait until Streamlit has finished running the script."""
    page.wait_for_function(IDLE, timeout=timeout)


def open_tool(page, base: str, path: str, ready_text: str,
              timeout: int = 60000):
    page.goto(base.rstrip("/") + path, wait_until="domcontentloaded",
              timeout=timeout)
    page.wait_for_selector(f"text={ready_text}", timeout=timeout)
    settle(page, timeout)
    return page


def act(page, locator, timeout: int = 30000) -> None:
    """Click something and wait for the resulting rerun to complete.

    A rerun may start and finish faster than we can observe it, so a missed
    busy phase is not an error. What matters is that the page is idle before
    the test reads anything back.
    """
    locator.click()
    try:
        page.wait_for_function(BUSY, timeout=1500)
    except Exception:  # noqa: BLE001
        pass
    settle(page, timeout)


TAB_SELECTED = """(name) => {
  const tab = [...document.querySelectorAll('[role="tab"]')]
      .find(t => t.textContent.trim() === name);
  return !!tab && tab.getAttribute('aria-selected') === 'true';
}"""


def open_tab(page, name: str, timeout: int = 30000) -> None:
    """Switch tabs and wait for the panel to actually be selected.

    Waiting a fixed number of milliseconds here was wrong in both directions:
    long enough to be slow, short enough to miss the switch, and a control in
    an unselected panel is absent from the accessibility tree, so a test would
    fail to find something that exists.
    """
    page.get_by_role("tab", name=name).click()
    page.wait_for_function(TAB_SELECTED, arg=name, timeout=timeout)
    settle(page, timeout)


def choose(page, combobox_name: str, option_fragment: str,
           timeout: int = 30000) -> None:
    page.get_by_role("combobox", name=combobox_name, exact=True).click()
    page.get_by_role("option", name=option_fragment, exact=False).click()
    settle(page, timeout)


SIDEBAR = 'section[data-testid="stSidebar"]'
# The element that actually scrolls. The outer section always reports
# scrollHeight == clientHeight, so measuring against it says everything is
# visible even when six hundred pixels of content sit below the fold.
SIDEBAR_SCROLLER = 'div[data-testid="stSidebarContent"]'

# Find the smallest element in the sidebar whose own text contains a phrase, and
# report where it sits relative to what a person can actually see. Ordering is
# not visibility: an element can be in the right order and still be four hundred
# pixels below the fold, which is how a sidebar shipped with its help invisible.
VISIBILITY = """(phrase) => {
  const sidebar = document.querySelector('div[data-testid="stSidebarContent"]')
      || document.querySelector('section[data-testid="stSidebar"]');
  if (!sidebar) return null;
  const holders = [...sidebar.querySelectorAll('*')]
      .filter(el => (el.innerText || '').includes(phrase));
  if (!holders.length) return {found: false};
  // The last match is the innermost element containing the phrase.
  const el = holders[holders.length - 1];
  const box = el.getBoundingClientRect();
  const frame = sidebar.getBoundingClientRect();
  return {
    found: true,
    top: Math.round(box.top - frame.top),
    bottom: Math.round(box.bottom - frame.top),
    visibleHeight: Math.round(sidebar.clientHeight),
    scrollHeight: Math.round(sidebar.scrollHeight),
    scrollTop: Math.round(sidebar.scrollTop),
  };
}"""


def sidebar_position(page, phrase: str) -> dict:
    """Where a phrase sits in the sidebar, in pixels, right now.

    Returns found/top/bottom/visibleHeight/scrollHeight/scrollTop. `top` is
    measured from the top of the sidebar's visible frame, so a negative value
    means the text is scrolled off above and a value beyond visibleHeight means
    it is below the fold.
    """
    return page.evaluate(VISIBILITY, phrase) or {"found": False}


def visible_without_scrolling(page, phrase: str) -> bool:
    """Whether a person sees this phrase without touching the scrollbar.

    This is the assertion that was missing. A test that only proves the help
    comes before the name plate passes happily while the help sits below the
    fold, which is exactly what shipped.
    """
    where = sidebar_position(page, phrase)
    if not where.get("found"):
        return False
    return 0 <= where["top"] < where["visibleHeight"]


# Where a whole sidebar block ends, not merely where its heading begins.
# A heading can sit comfortably on screen while the steps under it are clipped,
# which is the same mistake as asserting order instead of position: it measures
# something adjacent to what the reader actually needs.
BLOCK_BOTTOM = """(phrase) => {
  const sidebar = document.querySelector('div[data-testid="stSidebarContent"]');
  if (!sidebar) return null;
  const heading = [...sidebar.querySelectorAll('h1,h2,h3,h4,h5,h6')]
      .find(h => (h.innerText || '').trim().startsWith(phrase));
  if (!heading) return {found: false};
  const start = heading.closest('[data-testid="stElementContainer"]') || heading;
  let last = start;
  for (let el = start.nextElementSibling; el; el = el.nextElementSibling) {
    // A divider closes the block. Everything after it belongs to something else.
    if (el.querySelector('[data-testid="stDivider"], hr')) break;
    if (el.querySelector('h1,h2,h3,h4,h5,h6')) break;
    last = el;
  }
  const frame = sidebar.getBoundingClientRect();
  return {
    found: true,
    top: Math.round(start.getBoundingClientRect().top - frame.top),
    bottom: Math.round(last.getBoundingClientRect().bottom - frame.top),
    visibleHeight: Math.round(sidebar.clientHeight),
  };
}"""


def sidebar_block(page, heading: str) -> dict:
    """Measure a sidebar block from its heading through to its last element.

    Returns found/top/bottom/visibleHeight. `bottom` is what matters: a block
    whose heading is at 100px and whose final step ends at 900px in an 800px
    window is a block the reader cannot finish.
    """
    return page.evaluate(BLOCK_BOTTOM, heading) or {"found": False}


def block_fully_visible(page, heading: str) -> bool:
    """Whether every line of a block is on screen without scrolling."""
    where = sidebar_block(page, heading)
    if not where.get("found"):
        return False
    return 0 <= where["top"] and where["bottom"] <= where["visibleHeight"]
