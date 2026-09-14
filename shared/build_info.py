"""Which commit is this app actually running.

A deployed Streamlit app can silently serve an old build: the platform does not
always redeploy on a push, and nothing on the page says so. That has already
cost real confusion here, where a tool was reported as live at a URL while the
running app predated it by four commits.

So the app states its own build. The stamp is read from the checkout at runtime
with no subprocess, because a hosted environment may not expose git, and falls
back to a committed BUILD.txt and then to an environment variable. If none of
those answer, it says "unknown" rather than inventing a value.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNKNOWN = "unknown"


def _from_git(root: Path) -> str:
    """Resolve HEAD by reading the files, not by running git."""
    head_file = root / ".git" / "HEAD"
    if not head_file.is_file():
        return ""
    try:
        head = head_file.read_text().strip()
    except OSError:
        return ""
    if not head.startswith("ref: "):
        return head  # detached HEAD already holds the sha
    ref = head[5:].strip()
    loose = root / ".git" / ref
    try:
        if loose.is_file():
            return loose.read_text().strip()
        packed = root / ".git" / "packed-refs"
        if packed.is_file():
            for line in packed.read_text().splitlines():
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.split()
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    except OSError:
        return ""
    return ""


def _from_file(root: Path) -> str:
    stamp = root / "BUILD.txt"
    try:
        return stamp.read_text().strip() if stamp.is_file() else ""
    except OSError:
        return ""


def commit(root: Path | None = None) -> str:
    """The full commit this app is running, or "unknown"."""
    base = root or ROOT
    for candidate in (os.environ.get("TOOLBENCH_COMMIT", ""),
                      _from_git(base), _from_file(base)):
        value = (candidate or "").strip()
        if value:
            return value
    return UNKNOWN


def short_commit(root: Path | None = None) -> str:
    value = commit(root)
    return value if value == UNKNOWN else value[:7]


def build_label(root: Path | None = None) -> str:
    """What the page shows, so a stale deploy is visible rather than silent."""
    return f"build {short_commit(root)}"
