"""Ground truth test: one coherent, legible design in light and in dark.

Streamlit exposes no CSS variables for its theme, but it stamps the active
theme onto `.stApp` as `color-scheme`, which inherits, so our `light-dark()`
tokens resolve against the theme Streamlit actually painted. A
`prefers-color-scheme` rule here once produced dark cards on Streamlit's light
page beside a light sidebar. This test drives a real browser under both device
settings and checks two things that would have caught it: every surface agrees
on one scheme, and the text on those surfaces stays readable.

Needs a running hub. Start one first, or pass a base URL:
    streamlit run streamlit_app.py --server.port 8503 --server.headless true
    python3 tests/test_theme.py [http://127.0.0.1:8503]

Pass marker: final line is exactly "THEME RESULT: PASS <n>/<n>" and exit 0.
Fail marker: any line starting "FAIL", plus exit code 1.
Skips cleanly (exit 0) when the browser or the server is unavailable.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8503"
CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

try:
    from playwright.sync_api import sync_playwright

    from tests.browser_util import act, open_tool
except ImportError:
    print("THEME RESULT: SKIP (playwright not installed)")
    sys.exit(0)

try:
    urllib.request.urlopen(f"{BASE}/_stcore/health", timeout=10).read()
except (urllib.error.URLError, OSError) as exc:
    print(f"THEME RESULT: SKIP (no hub running at {BASE}: {exc})")
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


def luminance(css_colour: str) -> float:
    parts = css_colour.replace("rgba(", "").replace("rgb(", "").rstrip(")").split(",")
    r, g, b = (int(float(p)) for p in parts[:3])
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def family(value: float) -> str:
    return "light" if value >= 0.5 else "dark"


def channels(css_colour: str) -> tuple[int, int, int]:
    parts = css_colour.replace("rgba(", "").replace("rgb(", "").rstrip(")").split(",")
    return tuple(int(float(p)) for p in parts[:3])


def relative_luminance(css_colour: str) -> float:
    """WCAG relative luminance."""
    out = []
    for raw in channels(css_colour):
        c = raw / 255
        out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = out
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(foreground: str, background: str) -> float:
    """WCAG contrast ratio, 1.0 (invisible) to 21.0 (black on white)."""
    a, b = relative_luminance(foreground), relative_luminance(background)
    lighter, darker = max(a, b), min(a, b)
    return round((lighter + 0.05) / (darker + 0.05), 2)


MEASURE = """() => {
  const bg = n => n ? getComputedStyle(n).backgroundColor : null;
  const fg = n => n ? getComputedStyle(n).color : null;
  return {
    page: bg(document.querySelector('.stApp') || document.body),
    sidebar: bg(document.querySelector('[data-testid="stSidebar"]')),
    hero: bg(document.querySelector('.app-hero')),
    card: bg(document.querySelector('.app-card')),
    kpi: bg(document.querySelector('.app-kpi')),
  };
}"""

MEASURE_TEXT = """() => {
  const bg = n => n ? getComputedStyle(n).backgroundColor : null;
  const fg = n => n ? getComputedStyle(n).color : null;
  return {
    cardBg: bg(document.querySelector('.app-card')),
    bodyText: fg(document.querySelector('.app-card p')),
    heading: fg(document.querySelector('.app-card h4')),
    severityTag: fg(document.querySelector('.app-tag')),
    evidenceBg: bg(document.querySelector('.app-ev')),
    evidenceText: fg(document.querySelector('.app-ev')),
  };
}"""

print("== One coherent, legible design under either device colour setting ==")
page_text: dict[str, dict] = {}
launch_error = None
with sync_playwright() as p:
    try:
        browser = p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    except Exception as exc:  # noqa: BLE001
        launch_error = exc
        browser = None

    if browser is None:
        print(f"THEME RESULT: SKIP (browser unavailable: {launch_error})")
        sys.exit(0)

    for scheme in ("light", "dark"):
        context = browser.new_context(viewport={"width": 1440, "height": 1000},
                                      color_scheme=scheme)
        page = context.new_page()
        open_tool(page, BASE, "/wp-form-debugger", "Paste your form HTML")
        act(page, page.get_by_text("Load broken sample").first)
        measured = page.evaluate(MEASURE)
        page_text[scheme] = page.evaluate(MEASURE_TEXT)
        context.close()

        present = {k: v for k, v in measured.items() if v}
        expect(len(present) == len(measured),
               f"[{scheme}] every measured surface rendered (missing: "
               f"{[k for k, v in measured.items() if not v]})")

        families = {k: family(luminance(v)) for k, v in present.items()}
        expect(len(set(families.values())) == 1,
               f"[{scheme}] page, sidebar and our components agree on one scheme "
               f"(got {families})")
        expect(families.get("page") == families.get("card"),
               f"[{scheme}] cards match the page they sit on "
               f"(page={families.get('page')}, card={families.get('card')})")

        text = page_text.get(scheme, {})
        card_bg = text.get("cardBg")
        if card_bg:
            for label, minimum in (("bodyText", 4.5), ("heading", 4.5), ("severityTag", 3.0)):
                value = text.get(label)
                if value:
                    ratio = contrast(value, card_bg)
                    expect(ratio >= minimum,
                           f"[{scheme}] {label} is readable on the card "
                           f"(contrast {ratio}, needs {minimum})")
        if text.get("evidenceBg") and text.get("evidenceText"):
            ratio = contrast(text["evidenceText"], text["evidenceBg"])
            expect(ratio >= 4.5,
                   f"[{scheme}] evidence text is readable on its panel "
                   f"(contrast {ratio}, needs 4.5)")

    browser.close()

print()
if failures:
    print(f"THEME RESULT: FAIL {len(failures)} of {checks} checks failed")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"THEME RESULT: PASS {checks}/{checks}")
sys.exit(0)
