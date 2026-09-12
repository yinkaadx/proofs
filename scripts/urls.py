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

DEFAULT_BASE = "https://proofs-toolbench.streamlit.app"


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE).rstrip("/")
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
