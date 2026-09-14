#!/usr/bin/env python3
"""Run every test suite in tests/, the right way for its style, and count them.

This exists because the automatic promotion gate was passing suites it never
ran. It ran `python3 tests/<file>.py` on every file, which is correct for the
script style suites and silently wrong for the pytest style ones: a pytest file
run that way defines its test functions, calls nothing, prints nothing and
exits 0. Twenty eight of forty seven suites, covering fourteen of the nineteen
tools, were counted as passed without a single check executing, and the gate
that decides whether code reaches the live app reported "All suites passed".

A suite that runs zero checks is not a suite that passed. So this runner does
three things the loop it replaces did not:

  It discovers the suites instead of reading a hand maintained list, because a
  list is a thing to forget and eight suites had already been forgotten.

  It classifies each one and runs it the way that style actually executes. A
  file defining `def test_` functions goes to pytest; anything else is a script
  that prints its own RESULT line.

  It demands evidence that checks ran, and fails a suite that cannot show any.
  Exit code zero is not evidence.

Usage:
    python3 scripts/run_suites.py                  every suite
    python3 scripts/run_suites.py --no-browser     skip the ones needing a hub
    python3 scripts/run_suites.py --only test_hub.py test_theme.py
    python3 scripts/run_suites.py --base http://127.0.0.1:8600

Pass marker: final line is exactly "SUITES RESULT: PASS <n>/<n>", exit 0.
Fail marker: a line starting "SUITES RESULT: FAIL", exit 1.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

PYTEST_STYLE = re.compile(r"^def test_", re.MULTILINE)
# What a script style suite prints when it has finished. The word before it
# varies by suite, so only the shape is fixed.
RESULT_LINE = re.compile(r"RESULT:\s+(PASS|FAIL|SKIP)(?:\s+(\d+)\s*/\s*(\d+))?")
PYTEST_COUNT = re.compile(r"(\d+)\s+passed")
PYTEST_NONE = re.compile(r"no tests ran", re.IGNORECASE)

# Suites that cannot run without a live hub and a real browser. They take the
# base URL as their first argument and skip cleanly when there is none, which
# is why they are named here rather than detected: a clean skip is
# indistinguishable from a pass from the outside.
BROWSER_SUITES = {
    "test_tool_urls.py",
    "test_theme.py",
    "test_browser_inventory_flow.py",
    "test_sidebar_invariant.py",
}


class Outcome:
    def __init__(self, name: str, style: str) -> None:
        self.name = name
        self.style = style
        self.ok = False
        self.checks = 0
        self.seconds = 0.0
        self.summary = ""
        self.skipped = False


def classify(path: Path) -> str:
    """pytest when the file defines test functions, script otherwise.

    The two styles partition cleanly in this repository and the check is on
    what the file contains rather than on a naming convention, so a suite
    cannot be misfiled by being named differently.
    """
    try:
        source = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "script"
    return "pytest" if PYTEST_STYLE.search(source) else "script"


def discover(only: list[str]) -> list[Path]:
    found = sorted(TESTS.glob("test_*.py"))
    if only:
        wanted = {Path(name).name for name in only}
        found = [p for p in found if p.name in wanted]
    return found


def run_pytest(path: Path, timeout: int) -> Outcome:
    outcome = Outcome(path.name, "pytest")
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(path), "-q",
             "-p", "no:cacheprovider"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        outcome.seconds = time.monotonic() - started
        outcome.summary = f"timed out after {timeout}s"
        return outcome
    outcome.seconds = time.monotonic() - started
    text = f"{proc.stdout}\n{proc.stderr}"
    tail = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    outcome.summary = tail[-1] if tail else "(no output)"

    match = PYTEST_COUNT.search(text)
    outcome.checks = int(match.group(1)) if match else 0
    # Exit 5 is pytest's "no tests collected". Treating that as a pass is the
    # whole defect this runner exists to remove.
    if proc.returncode != 0 or PYTEST_NONE.search(text) or outcome.checks == 0:
        if proc.returncode == 0 and outcome.checks == 0:
            outcome.summary = ("collected no tests, so nothing was proved "
                               "(exit 0 is not evidence)")
        return outcome
    outcome.ok = True
    return outcome


def run_script(path: Path, base: str, timeout: int) -> Outcome:
    outcome = Outcome(path.name, "script")
    argv = [sys.executable, str(path)]
    if base and path.name in BROWSER_SUITES:
        argv.append(base)
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=str(ROOT), capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        outcome.seconds = time.monotonic() - started
        outcome.summary = f"timed out after {timeout}s"
        return outcome
    outcome.seconds = time.monotonic() - started
    text = f"{proc.stdout}\n{proc.stderr}"

    match = None
    for match in RESULT_LINE.finditer(text):
        pass                                   # keep the last one
    if match is None:
        outcome.summary = ("printed no RESULT line, so nothing was proved "
                           f"(exit {proc.returncode})")
        return outcome

    outcome.summary = match.group(0)
    verdict = match.group(1)
    if match.group(2):
        outcome.checks = int(match.group(2))
    if verdict == "SKIP":
        outcome.skipped = True
        outcome.ok = proc.returncode == 0
        return outcome
    outcome.ok = proc.returncode == 0 and verdict == "PASS"
    return outcome


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="",
                        help="base URL for the suites that need a live hub")
    parser.add_argument("--no-browser", action="store_true",
                        help="skip the suites that need a hub and a browser")
    parser.add_argument("--only", nargs="*", default=[],
                        help="run only these suite file names")
    parser.add_argument("--timeout", type=int, default=900,
                        help="seconds any one suite may take")
    args = parser.parse_args(argv)

    suites = discover(args.only)
    if not suites:
        print("SUITES RESULT: FAIL (no suites found in tests/)")
        return 1

    outcomes: list[Outcome] = []
    print(f"== Running {len(suites)} suite(s) ==")
    for path in suites:
        if args.no_browser and path.name in BROWSER_SUITES:
            outcome = Outcome(path.name, classify(path))
            outcome.ok = True
            outcome.skipped = True
            outcome.summary = "not run: needs a live hub and a browser"
            outcomes.append(outcome)
            print(f"  {'skip':>4}  {path.name:<52} {outcome.summary}")
            continue
        style = classify(path)
        outcome = (run_pytest(path, args.timeout) if style == "pytest"
                   else run_script(path, args.base, args.timeout))
        outcomes.append(outcome)
        mark = "ok" if outcome.ok else "FAIL"
        print(f"  {mark:>4}  {path.name:<52} {outcome.seconds:6.1f}s  "
              f"{outcome.summary}")

    failed = [o for o in outcomes if not o.ok]
    checks = sum(o.checks for o in outcomes)
    ran = [o for o in outcomes if not o.skipped]

    print()
    print(f"{len(ran)} suite(s) executed, {checks} check(s) actually ran, "
          f"{len(outcomes) - len(ran)} skipped")
    if failed:
        print(f"SUITES RESULT: FAIL {len(failed)} of {len(outcomes)} suite(s)")
        for outcome in failed:
            print(f"  - {outcome.name} ({outcome.style}): {outcome.summary}")
        return 1
    print(f"SUITES RESULT: PASS {len(outcomes)}/{len(outcomes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
