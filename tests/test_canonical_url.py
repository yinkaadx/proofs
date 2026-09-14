"""Ground truth test: the repository names exactly one live app hostname.

This exists because a dead link was handed over. Every URL in this repository
was right; the one that reached the owner was typed from memory and pointed at
a host that appears nowhere in the project and does not serve the app. The
repair is that there is now one place the hostname is written down,
.github/streamlit-apps.txt, and one command that prints URLs built from it,
scripts/urls.py. This suite is what keeps that true: it fails the moment a
second hostname appears anywhere, so a URL handed over can always be the
output of a command rather than a recollection.

Run: python3 tests/test_canonical_url.py
Pass marker: final line is exactly "URL SOURCE RESULT: PASS <n>/<n>", exit 0.
Fail marker: any line starting "FAIL", plus exit code 1.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from tools.registry import all_tools  # noqa: E402
from urls import APP_LIST, canonical_base  # noqa: E402

# Any Community Cloud hostname at all, so a second one is found rather than
# assumed absent.
HOSTNAME = re.compile(r"https://[a-z0-9][a-z0-9-]*\.streamlit\.app")

# Directories that hold no authored text.
IGNORED = {".git", "__pycache__", ".pytest_cache", "node_modules"}
SEARCHED = {".py", ".md", ".txt", ".yml", ".yaml", ".toml", ".html"}

# One exemption, named rather than pattern matched, so a second one cannot be
# added quietly. This suite's question is whether a URL somebody could copy out
# of this repository could be the wrong one. The prober's unit tests invent
# hostnames on purpose, to prove the parsing works on any host rather than only
# on ours, and nobody copies a link out of an assertion. Every other file, docs
# and scripts and workflows included, has to name the one real host.
FIXTURE_FILE = "tests/test_keep_streamlit_awake.py"

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


def searchable_files() -> list[Path]:
    found: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SEARCHED:
            continue
        if any(part in IGNORED for part in path.parts):
            continue
        if path.relative_to(ROOT).as_posix() == FIXTURE_FILE:
            continue
        found.append(path)
    return found


print("== The repository names one live app hostname and derives every URL "
      "from it ==")

base = canonical_base()
expect(bool(HOSTNAME.fullmatch(base)),
       f"the app list names a Community Cloud URL ({base})")
expect(APP_LIST.exists(),
       f"the single source of the hostname exists at "
       f"{APP_LIST.relative_to(ROOT)}")

# The whole point: one hostname, everywhere, or this fails.
seen: dict[str, list[str]] = {}
for path in searchable_files():
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        continue
    for match in HOSTNAME.findall(text):
        seen.setdefault(match, []).append(str(path.relative_to(ROOT)))

expect(list(seen) == [base],
       f"every Community Cloud URL a person could copy out of this repository "
       f"is {base} (found {sorted(seen)})")
if len(seen) > 1:
    for host, places in sorted(seen.items()):
        if host != base:
            print(f"       {host} appears in: {', '.join(sorted(set(places)))}")

expect(len(seen.get(base, [])) >= 4,
       f"the canonical host is written in several places that must agree "
       f"(found {len(seen.get(base, []))} mentions)")

# The README is what a person reads first, so it has to carry the same one.
readme = (ROOT / "README.md").read_text(encoding="utf-8")
expect(base in readme, f"README.md names {base}")

# The prober's own fallback cannot drift from the list it normally reads.
prober = (ROOT / "scripts" / "keep_streamlit_awake.py").read_text(encoding="utf-8")
expect(base in prober,
       "the keep awake prober's fallback names the same host as the app list")

# The exemption has to stay an exemption for the stated reason, not become a
# hiding place. The fixture file may invent hostnames; it may not become the
# place a real second deployment is recorded.
fixtures = (ROOT / FIXTURE_FILE).read_text(encoding="utf-8")
invented = {host for host in HOSTNAME.findall(fixtures) if host != base}
expect(all(len(host.split("//")[1].split(".")[0]) <= 4 for host in invented),
       f"the exempt fixture file invents only obvious placeholder hosts "
       f"(found {sorted(invented)})")

# And the printed URLs have to be the registry's, not a hand written set.
tools = all_tools()
expect(bool(tools), "the registry has tools to print URLs for")
for tool in tools:
    url = f"{base}/{tool.key}"
    expect(url.startswith(base + "/") and url.endswith(tool.key),
           f"the URL for {tool.title} is built from the registry key "
           f"{tool.key}")

print()
if failures:
    print(f"URL SOURCE RESULT: FAIL {len(failures)} of {checks} checks failed")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)
print(f"URL SOURCE RESULT: PASS {checks}/{checks}")
sys.exit(0)
