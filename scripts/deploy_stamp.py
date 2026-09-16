#!/usr/bin/env python3
"""Rewrite the deploy stamp line in requirements.txt.

Streamlit Community Cloud applies an ordinary push late, or not for an hour,
or not until the next push happens to land. Its own documentation names one
change it treats differently: a change to a dependency file triggers a full
redeploy, which is a fresh container running the current commit. This writes
a single comment line that changes with the commit being nudged. That is
enough to count as a change, and it installs nothing.

The stamp is the FIRST line of the file. Humans and tools append new
requirements at the bottom, so a stamp at the bottom conflicted with exactly
the push whose redeploy matters most, the one that adds a dependency; a first
line that nothing else edits rebases and merges clean. Any existing stamp,
wherever it sits and however it is indented, is removed, so the file carries
exactly one stamp however many times this runs. Line endings are normalised
to LF, which is what every other file in this repository uses.

Usage:
    python3 scripts/deploy_stamp.py <commit> [requirements.txt]

Pass marker: last line is "STAMP RESULT: WRITTEN <commit>" when the file
changed, "STAMP RESULT: UNCHANGED <commit>" when it already carried that
stamp. Exit 0 either way; exit 2 when the file is missing.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

STAMP_PREFIX = "# deploy stamp:"
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "requirements.txt"


def stamp_line(commit: str, when: str) -> str:
    return f"{STAMP_PREFIX} {commit.strip()} {when.strip()}"


def _is_stamp(line: str) -> bool:
    return line.lstrip().startswith(STAMP_PREFIX)


def apply_stamp(text: str, commit: str, when: str) -> tuple[str, bool]:
    """Return the new file text and whether anything actually changed.

    A stamp for the same commit is rewritten with the new time, because the
    point is that the file differs from what Community Cloud last saw; two
    nudges for one commit must both count. "Changed" is decided by comparing
    the bytes, so moving an existing stamp to the top counts as well.
    """
    original = str(text or "")
    body = [line for line in original.replace("\r\n", "\n")
            .replace("\r", "\n").split("\n") if not _is_stamp(line)]
    # split() leaves one empty string after a trailing newline, and removing
    # a stamp that sat at the bottom leaves the blank line that preceded it.
    while body and body[-1] == "":
        body.pop()
    rebuilt = "\n".join([stamp_line(commit, when)] + body) + "\n"
    return rebuilt, rebuilt != original


def current_stamp(text: str) -> str:
    """The commit named by the stamp in the text, or ""."""
    for existing in str(text or "").splitlines():
        if _is_stamp(existing):
            parts = existing.lstrip()[len(STAMP_PREFIX):].split()
            return parts[0] if parts else ""
    return ""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: deploy_stamp.py <commit> [requirements.txt]")
        return 2
    commit = args[0]
    path = Path(args[1]) if len(args) > 1 else DEFAULT_PATH
    if not path.is_file():
        print(f"STAMP RESULT: MISSING {path}")
        return 2
    when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    text, changed = apply_stamp(path.read_text(encoding="utf-8"), commit, when)
    if changed:
        path.write_text(text, encoding="utf-8")
        print(f"STAMP RESULT: WRITTEN {commit}")
    else:
        print(f"STAMP RESULT: UNCHANGED {commit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
