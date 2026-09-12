"""Ground truth check: every generated PHP snippet must actually parse.

Each snippet is a fragment for a file that already opened a PHP block, so the
check reproduces real use: write a wp-config.php style host file that opens
`<?php`, paste the fragment inside it, and run `php --syntax-check`. Awkward
passwords are included because an unescaped apostrophe is exactly what breaks
a generated wp-config.php on a live site.

Run: python3 test_php_syntax.py
Pass marker: final line is exactly "PHP RESULT: PASS <n>/<n>" and exit code 0.
Fail marker: any line starting "FAIL", plus exit code 1.
Skips cleanly (exit 0) when no php binary is present.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from wpfd_core import (
    KNOWN_ISSUES,
    SAMPLE_BROKEN_HTML,
    SAMPLE_HEALTHY_HTML,
    analyze_html,
    simulate_smtp,
)

PHP = shutil.which("php")
if not PHP:
    print("PHP RESULT: SKIP (no php binary on PATH)")
    sys.exit(0)

failures: list[str] = []
checks = 0

FX_HOST = ".".join(("smtp", "acme", "io"))
FX_USER = "@".join(("post", "acme.io"))
FX_FROM = "@".join(("no-reply", "acme.io"))


def check_parses(label: str, fragment: str) -> None:
    """Paste the fragment into a host file that already opened a PHP block."""
    global checks
    checks += 1
    host_file = (
        "<?php\n"
        "/* Existing wp-config.php content above the paste point. */\n"
        f"{fragment}\n"
        "/* That's all, stop editing! Happy publishing. */\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".php", delete=False) as fh:
        fh.write(host_file)
        path = fh.name
    try:
        proc = subprocess.run([PHP, "--syntax-check", path],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            print(f"  ok   {label}")
        else:
            detail = (proc.stdout + proc.stderr).strip().splitlines()
            print(f"  FAIL {label}: {detail[0] if detail else 'unknown parse error'}")
            failures.append(label)
    finally:
        Path(path).unlink(missing_ok=True)


print("== Generated functions.php and wp-config.php fragments parse ==")
r = simulate_smtp("Custom / other", FX_HOST, 587, "STARTTLS", FX_USER,
                  "-".join(("fixture", "value", "only")), FX_FROM)
check_parses("smtp functions.php handler", r.fix_php)
check_parses("smtp wp-config block", r.fix_wpconfig)
check_parses("smtp wp-config block, redacted", r.fix_wpconfig_redacted)

for key, issue in KNOWN_ISSUES.items():
    if issue.fix_php:
        check_parses(f"issue {key} functions.php", issue.fix_php)
    if issue.fix_wpconfig:
        check_parses(f"issue {key} wp-config", issue.fix_wpconfig)

seen: set[str] = set()
for f in analyze_html(SAMPLE_BROKEN_HTML) + analyze_html(SAMPLE_HEALTHY_HTML):
    for kind, block in (("php", f.fix_php), ("wpconfig", f.fix_wpconfig)):
        if block and (f.fid, kind) not in seen:
            seen.add((f.fid, kind))
            check_parses(f"finding {f.fid} {kind}", block)

print("== Awkward credentials still produce parsable wp-config ==")
for probe in ("plain", "with space", "quote'inside", "back\\slash", "both'\\mixed",
              'double"quote', "semi;colon", "dollar$sign", "brace{}s",
              "newline-free\ttab", "unicode-éü", "trailing\\",
              "'leading", "percent%s", "at@sign", "hash#hash"):
    rp = simulate_smtp("Custom / other", FX_HOST, 587, "STARTTLS", FX_USER,
                       probe, FX_FROM, "O'Brien & Co")
    check_parses(f"wp-config with password {probe!r}", rp.fix_wpconfig)

print("== The paste point is genuinely inside an existing PHP block ==")
# Guard the guard: if the host file were wrong, a stray opening tag would slip
# through unnoticed. A fragment that DOES carry its own tag must fail here.
checks += 1
bad = "<?php\ndefine( 'X', 1 );\n"
with tempfile.NamedTemporaryFile("w", suffix=".php", delete=False) as fh:
    fh.write(f"<?php\n/* existing */\n{bad}\n")
    bad_path = fh.name
try:
    proc = subprocess.run([PHP, "--syntax-check", bad_path],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        print("  ok   a duplicated opening tag is correctly rejected by this harness")
    else:
        print("  FAIL harness cannot detect a duplicated opening tag")
        failures.append("harness detects duplicated opening tag")
finally:
    Path(bad_path).unlink(missing_ok=True)

print()
if failures:
    print(f"PHP RESULT: FAIL {len(failures)} of {checks} checks failed")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PHP RESULT: PASS {checks}/{checks}")
sys.exit(0)
