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
