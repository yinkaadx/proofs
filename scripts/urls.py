#!/usr/bin/env python3
"""Print the canonical URL for every tool, straight from the registry.

Composing a URL by hand is how a wrong one gets handed over. This reads the
same registry the hub builds its navigation from, so the list cannot drift from
what the app actually serves, and prints the build the checkout is on so a
stale deployment is obvious rather than silent.

Usage:
    python3 scripts/urls.py [base-url]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.build_info import short_commit  # noqa: E402
from tools.registry import all_tools  # noqa: E402

# The one place the live hostname is written down is the app list the keep
# awake workflow reads. Everything else derives from it. A second copy here
# would be a second thing to keep in step, and a hostname typed from memory is
# exactly how a dead link was handed over once already.
APP_LIST = Path(__file__).resolve().parents[1] / ".github" / "streamlit-apps.txt"


def canonical_base(app_list: Path = APP_LIST) -> str:
    """The live app URL, read from .github/streamlit-apps.txt.

    That file is the list the keep awake workflow actually opens in a browser,
    so a URL taken from it is one that something has proved reachable, rather
    than one somebody believed.
    """
    for line in app_list.read_text().splitlines():
        entry = line.split("#", 1)[0].strip()
        if entry:
            return entry.rstrip("/")
    raise SystemExit(f"no app URL found in {app_list}")


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else canonical_base()).rstrip("/")
    tools = all_tools()
    print(f"Toolbench, {len(tools)} tool(s), checkout at build {short_commit()}")
    print(f"Home  {base}/")
    width = max(len(t.title) for t in tools)
    for tool in tools:
        print(f"  {tool.icon} {tool.title.ljust(width)}  {base}/{tool.key}")
    print()
    print("These paths come from tools/registry.py, which is what the hub uses "
          "to build its navigation.")
    print("A URL is only proven once tests/test_tool_urls.py has opened it in a "
          "browser against a running hub.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
