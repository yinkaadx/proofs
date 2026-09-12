"""Ground truth test: the page must look like one design under either device
colour setting.

Streamlit paints its own chrome from `.streamlit/config.toml` and exposes no
CSS variables for it, so a `prefers-color-scheme` rule in our stylesheet cannot
know what the rest of the page looks like. One used to exist, and a visitor
whose device was set to dark saw dark cards floating on Streamlit's light page
next to a light sidebar. This test drives a real browser under both settings
and compares measured background luminance, so the two can never drift apart
again.

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


MEASURE = """() => {
  const bg = n => n ? getComputedStyle(n).backgroundColor : null;
  return {
    page: bg(document.querySelector('.stApp') || document.body),
    sidebar: bg(document.querySelector('[data-testid="stSidebar"]')),
    hero: bg(document.querySelector('.app-hero')),
    card: bg(document.querySelector('.app-card')),
    kpi: bg(document.querySelector('.app-kpi')),
  };
}"""

print("== The page reads as one design under either device colour setting ==")
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
        page.goto(f"{BASE}/wp-form-debugger", wait_until="networkidle", timeout=60000)
        page.wait_for_selector("text=Paste your form HTML", timeout=30000)
        page.get_by_text("Load broken sample").first.click()
        page.wait_for_timeout(3500)
        measured = page.evaluate(MEASURE)
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

    browser.close()

print()
if failures:
    print(f"THEME RESULT: FAIL {len(failures)} of {checks} checks failed")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"THEME RESULT: PASS {checks}/{checks}")
sys.exit(0)
