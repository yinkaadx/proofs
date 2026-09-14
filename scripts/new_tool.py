#!/usr/bin/env python3
"""Scaffold a new Toolbench tool.

Every tool needs the same eight pieces: a package, a Streamlit free core, a
page, a test harness, two test suites, a README and a registry entry. Writing
those by hand each time is the slowest mechanical part of adding a tool, and it
is the part where a detail gets forgotten. This generates all of it, wired up
and passing, so the only work left is the domain logic.

Usage:
    python3 scripts/new_tool.py \\
        --key returns-triage \\
        --title "Returns Triage Console" \\
        --tagline "Sort inbound returns by condition and route each to the right disposition." \\
        --audience "Ecommerce operations" \\
        --icon "\\U0001F4E5"

Then run scripts/check.sh, which should pass with the generated placeholder
checks, and start replacing the placeholders with real logic.
"""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def module_name(key: str) -> str:
    return key.replace("-", "_")


CORE = '''"""{title} engine.

Pure logic: no Streamlit import, so this is unit testable on its own and can be
reused behind a CLI, a webhook handler or a scheduled job. Keep it
deterministic: where a live system would read the clock, take `now` as an
argument instead, so a test and a production run agree.
"""

from __future__ import annotations

from dataclasses import dataclass

ENGINE_VERSION = "1.0.0"


@dataclass(frozen=True)
class Item:
    """Replace with the real record this tool works on."""
    identifier: str
    label: str


SAMPLE_ITEMS: tuple[Item, ...] = (
    Item("SAMPLE-1", "First sample record"),
    Item("SAMPLE-2", "Second sample record"),
)


def summarise(items=SAMPLE_ITEMS) -> dict:
    """Replace with the real entry point. Returns rows fit for a table.

    Every column holds one type: the table widget serialises through Arrow,
    and Arrow refuses a column that mixes types.
    """
    return {{
        "count": len(items),
        "rows": [
            {{"Identifier": item.identifier, "Label": item.label}}
            for item in items
        ],
    }}
'''

PAGE = '''"""{title}.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.{module}.core import ENGINE_VERSION, SAMPLE_ITEMS, summarise

STATE = "{module}_state"


def _state() -> dict:
    if STATE not in st.session_state:
        st.session_state[STATE] = {{"items": SAMPLE_ITEMS}}
    return st.session_state[STATE]


def render() -> None:
    inject()
    state = _state()
    summary = summarise(state["items"])

    st.markdown(
        """
<div class="app-hero">
  <h1>{html_title}</h1>
  <p>{tagline}</p>
</div>
""",
        unsafe_allow_html=True,
    )

    # The hub renders its name plate after the page, so whatever this block
    # writes lands directly under the tool list. Keep "How to use" first: it is
    # what someone reaches for while using the tool.
    with st.sidebar:
        st.subheader("How to use")
        st.markdown("Replace this with the real steps.")
        st.divider()
        st.caption("Nothing here touches a live system.")
        st.caption(f"Engine version {{ENGINE_VERSION}}")

    st.markdown(
        f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{{summary['count']}}</div>
    <div class="l">Records</div></div>
</div>
""",
        unsafe_allow_html=True,
    )

    st.markdown("#### Records")
    st.dataframe(summary["rows"], width="stretch", hide_index=True)

    st.markdown(
        f"""
<div class="app-foot">
{html_title}, engine version {{ENGINE_VERSION}}. A simulator: nothing you enter
leaves this session.
</div>
""",
        unsafe_allow_html=True,
    )
'''

HARNESS = '''"""Test harness: runs the {title} page as a standalone Streamlit script,
the same way the hub renders it. AppTest.from_file needs a real script rather
than a function.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.{module}.page import render  # noqa: E402

render()
'''

CORE_TEST = '''"""Ground truth tests for the {title} engine.

Pass and fail markers are declared before execution and results are parsed
programmatically.

Run: python3 tests/test_{module}.py
Pass marker: final line is exactly "{marker} RESULT: PASS <n>/<n>", exit 0.
Fail marker: any line starting "FAIL", plus exit code 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.{module}.core import SAMPLE_ITEMS, summarise  # noqa: E402

failures: list[str] = []
checks = 0


def expect(condition: bool, label: str) -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok   {{label}}")
    else:
        print(f"  FAIL {{label}}")
        failures.append(label)


print("== Placeholder checks, replace these with real ones ==")
summary = summarise()
expect(summary["count"] == len(SAMPLE_ITEMS), "the summary counts every record")
expect(len(summary["rows"]) == len(SAMPLE_ITEMS), "one row per record")

print("== Table rows survive Arrow serialisation ==")
for column in {{key for row in summary["rows"] for key in row}}:
    kinds = {{type(row[column]).__name__ for row in summary["rows"]}}
    expect(len(kinds) == 1,
           f"column {{column!r}} holds a single type (got {{sorted(kinds)}})")

print("== Punctuation discipline: no dash characters in user facing prose ==")
banned = ("\\u2014", "\\u2013")
offenders = [i.label for i in SAMPLE_ITEMS
             if any(b in i.label for b in banned)]
expect(not offenders, f"no em or en dashes in prose (offenders: {{offenders[:5]}})")

print()
if failures:
    print(f"{marker} RESULT: FAIL {{len(failures)}} of {{checks}} checks failed")
    for f in failures:
        print(f"  - {{f}}")
    sys.exit(1)
print(f"{marker} RESULT: PASS {{checks}}/{{checks}}")
sys.exit(0)
'''

PAGE_TEST = '''"""UI smoke tests for the {title} page, via Streamlit's AppTest.

Run: python3 tests/test_{module}_page.py
Pass marker: final line is exactly "{marker} UI RESULT: PASS <n>/<n>", exit 0.
Fail marker: any line starting "FAIL", plus exit code 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from streamlit.testing.v1 import AppTest  # noqa: E402

failures: list[str] = []
checks = 0

HARNESS = Path(__file__).resolve().parent / "_page_harness_{module}.py"


def expect(condition: bool, label: str) -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok   {{label}}")
    else:
        print(f"  FAIL {{label}}")
        failures.append(label)


def text_of(at: AppTest) -> str:
    parts: list[str] = []
    for attr in ("markdown", "text", "info", "success", "warning", "error",
                 "caption", "code", "header", "subheader", "title"):
        try:
            parts += [str(getattr(el, "value", "")) for el in getattr(at, attr)]
        except Exception:
            pass
    try:
        for frame in at.get("dataframe"):
            value = getattr(frame, "value", None)
            if value is not None and hasattr(value, "to_numpy"):
                parts += [str(cell) for cell in value.to_numpy().ravel()]
                parts += [str(col) for col in value.columns]
    except Exception:
        pass
    return "\\n".join(parts)


def run(timeout: int = 120) -> AppTest:
    at = AppTest.from_file(str(HARNESS), default_timeout=timeout)
    at.run()
    return at


print("== Cold start ==")
at = run()
expect(not at.exception,
       f"page renders with no exception (got {{[str(e.value) for e in at.exception]}})")
body = text_of(at)
expect("{html_title}" in body, "hero renders")

print("== Widget labels are unambiguous ==")
labels = [str(el.label) for el in at.selectbox]
expect(len(labels) == len(set(labels)),
       f"no two selectboxes share a label (got {{labels}})")

print()
if failures:
    print(f"{marker} UI RESULT: FAIL {{len(failures)}} of {{checks}} checks failed")
    for f in failures:
        print(f"  - {{f}}")
    sys.exit(1)
print(f"{marker} UI RESULT: PASS {{checks}}/{{checks}}")
sys.exit(0)
'''

README = '''# {title}

{tagline}

Live at `/{key}` on the hub.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | The engine. No Streamlit import, so it is testable on its own |
| `page.py` | The page, rendered by the hub |
| `../../tests/test_{module}.py` | Engine tests |
| `../../tests/test_{module}_page.py` | Page tests via AppTest |

## Notes

Replace the placeholder records in `core.py` and the placeholder checks in the
test suites. Two rules worth keeping:

- Every table column holds one type, because the table widget serialises
  through Arrow and Arrow refuses a mixed column.
- Do not fold changing values into a selectbox's option labels. Streamlit
  stores the formatted label under the widget key, so a label that moves makes
  the stored value go stale and the widget silently resets.
'''

REGISTRY_SNIPPET = '''        Tool(
            key="{key}",
            title="{title}",
            icon="{icon}",
            tagline=(
                "{tagline}"
            ),
            audience="{audience}",
            render={module},
        ),'''


def main() -> int:
    parser = argparse.ArgumentParser(description="Scaffold a new Toolbench tool.")
    parser.add_argument("--key", required=True, help="URL slug, lowercase with dashes")
    parser.add_argument("--title", required=True)
    parser.add_argument("--tagline", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--icon", required=True, help="A single emoji")
    args = parser.parse_args()

    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", args.key):
        print(f"key {args.key!r} must be lowercase words separated by dashes")
        return 1

    module = module_name(args.key)
    marker = module.upper().replace("_", " ").split()[0]
    package = ROOT / "tools" / module
    if package.exists():
        print(f"{package} already exists, refusing to overwrite")
        return 1

    fields = {
        "key": args.key, "title": args.title, "tagline": args.tagline,
        "audience": args.audience, "icon": args.icon, "module": module,
        "marker": marker,
        # The hero is raw HTML, so a title carrying an ampersand has to be
        # escaped on the way in. Writing it by hand is how two tools ended up
        # rendering under a different name than the one they are registered
        # with, which the URL proof caught both times.
        "html_title": html.escape(args.title),
    }

    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'"""{args.title}."""\n')
    (package / "core.py").write_text(CORE.format(**fields))
    (package / "page.py").write_text(PAGE.format(**fields))
    (package / "README.md").write_text(README.format(**fields))
    (ROOT / "tests" / f"_page_harness_{module}.py").write_text(HARNESS.format(**fields))
    (ROOT / "tests" / f"test_{module}.py").write_text(CORE_TEST.format(**fields))
    (ROOT / "tests" / f"test_{module}_page.py").write_text(PAGE_TEST.format(**fields))

    print(f"Created tools/{module}/ and its two test suites.")
    print("\nAdd this to all_tools() in tools/registry.py, and import its render:")
    print(f"\n    from tools.{module}.page import render as {module}\n")
    print(REGISTRY_SNIPPET.format(**fields))
    print("\nThen run: scripts/check.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
