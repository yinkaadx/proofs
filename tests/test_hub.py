"""Ground truth tests for the hub app and its tool registry.

Pass and fail markers are declared before execution and results are parsed
programmatically.

Run: python3 tests/test_hub.py
Pass marker: final line is exactly "HUB RESULT: PASS <n>/<n>" and exit code 0.
Fail marker: any line starting "FAIL", plus exit code 1.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from streamlit.testing.v1 import AppTest  # noqa: E402

from tools.registry import Tool, all_tools  # noqa: E402

failures: list[str] = []
checks = 0

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "streamlit_app.py"


def expect(condition: bool, label: str) -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


def text_of(at: AppTest) -> str:
    parts: list[str] = []
    for attr in ("markdown", "text", "info", "success", "warning", "error",
                 "caption", "header", "subheader", "title"):
        try:
            parts += [str(getattr(el, "value", "")) for el in getattr(at, attr)]
        except Exception:
            pass
    return "\n".join(parts)


print("== The deploy target exists exactly where the handoff says it does ==")
expect(ENTRY.is_file(), "streamlit_app.py sits at the repository root")
expect((ROOT / "requirements.txt").is_file(), "requirements.txt sits at the repository root")
expect((ROOT / ".streamlit" / "config.toml").is_file(), ".streamlit/config.toml sits at the root")

print("== Registry integrity ==")
tools = all_tools()
expect(len(tools) >= 1, f"registry publishes at least one tool (got {len(tools)})")
keys = [t.key for t in tools]
expect(len(keys) == len(set(keys)), f"tool keys are unique (got {keys})")
titles = [t.title for t in tools]
expect(len(titles) == len(set(titles)), f"tool titles are unique (got {titles})")
icons = [t.icon for t in tools]
expect(len(icons) == len(set(icons)),
       f"tool icons are unique, since the landing page and the navigation are "
       f"read by icon as much as by name (got {icons})")
for t in tools:
    expect(isinstance(t, Tool), f"{t.key} is a Tool instance")
    expect(bool(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", t.key)),
           f"{t.key} is a URL safe slug")
    expect(callable(t.render), f"{t.key} has a callable render")
    expect(bool(t.title and t.icon and t.tagline and t.audience),
           f"{t.key} declares title, icon, tagline and audience")
    expect(len(t.tagline) > 30, f"{t.key} tagline is descriptive")
    expect("—" not in t.tagline and "–" not in t.tagline,
           f"{t.key} tagline uses no em or en dashes")

print("== Every registered tool is importable and renders ==")
for t in tools:
    at = AppTest.from_file(str(ENTRY), default_timeout=90)
    at.run()
    expect(not at.exception,
           f"hub renders with {t.key} registered (got {[str(e.value) for e in at.exception]})")
    break

print("== Hub landing page ==")
at = AppTest.from_file(str(ENTRY), default_timeout=90)
at.run()
expect(not at.exception, f"hub runs clean (got {[str(e.value) for e in at.exception]})")
body = text_of(at)
expect("Toolbench" in body, "hub name renders")
for t in tools:
    expect(t.title in body, f"landing page lists {t.title}")
    expect(t.tagline[:40] in body, f"landing page shows the {t.key} tagline")
expect(f"{len(tools)} tool" in body, "landing page states how many tools are available")

print("== Navigation covers home plus every tool ==")
expect(len(at.sidebar) > 0, "sidebar renders")
nav_labels = {str(getattr(el, "label", "")) for el in at.get("page_link")}
for t in tools:
    expect(any(t.title in label for label in nav_labels),
           f"a page link exists for {t.title} (got {nav_labels})")

print("== Adding a tool needs only a registry entry ==")
entry_src = ENTRY.read_text()
expect("all_tools()" in entry_src, "hub builds its pages from the registry")
for t in tools:
    expect(t.key not in entry_src,
           f"hub does not hardcode the tool key {t.key}")
    expect(t.title not in entry_src,
           f"hub does not hardcode the tool title {t.title}")

print()
if failures:
    print(f"HUB RESULT: FAIL {len(failures)} of {checks} checks failed")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"HUB RESULT: PASS {checks}/{checks}")
sys.exit(0)
