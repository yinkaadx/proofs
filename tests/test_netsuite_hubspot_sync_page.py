"""UI smoke tests for the NetSuite HubSpot sync page, via Streamlit's AppTest.

Pass and fail markers are declared before execution. Every scenario asserts the
script ran with zero uncaught exceptions and that the named content rendered.

Run: python3 tests/test_netsuite_hubspot_sync_page.py
Pass marker: final line is exactly "SYNC UI RESULT: PASS <n>/<n>", exit code 0.
Fail marker: any line starting "FAIL", plus exit code 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from streamlit.testing.v1 import AppTest  # noqa: E402

from tools.netsuite_hubspot_sync.core import SAMPLE_DEAL, payload_hash  # noqa: E402

failures: list[str] = []
checks = 0

HARNESS = Path(__file__).resolve().parent / "_page_harness_netsuite_hubspot_sync.py"


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
                 "caption", "code", "header", "subheader", "title"):
        try:
            parts += [str(getattr(el, "value", "")) for el in getattr(at, attr)]
        except Exception:
            pass
    # Tables carry the audit feed and the ledger, so their cells count as
    # rendered content too. Without this, a table could go empty unnoticed.
    try:
        for frame in at.get("dataframe"):
            value = getattr(frame, "value", None)
            if value is not None and hasattr(value, "to_numpy"):
                parts += [str(cell) for cell in value.to_numpy().ravel()]
                parts += [str(col) for col in value.columns]
    except Exception:
        pass
    return "\n".join(parts)


def run(timeout: int = 120) -> AppTest:
    at = AppTest.from_file(str(HARNESS), default_timeout=timeout)
    at.run()
    return at


def widget(at: AppTest, kind: str, label: str):
    for el in getattr(at, kind):
        if el.label == label:
            return el
    raise AssertionError(f"no {kind} labelled {label!r}; "
                         f"available: {[e.label for e in getattr(at, kind)]}")


print("== Cold start ==")
at = run()
expect(not at.exception,
       f"page renders with no exception (got {[str(e.value) for e in at.exception]})")
body = text_of(at)
expect("NetSuite HubSpot Idempotent Sync Console" in body, "hero renders")
expect(len(at.tabs) == 5, f"five tabs present (got {len(at.tabs)})")
expect("Data retention" in body, "the executive KPI tiles render")
expect("100%" in body, "retention shows 100 percent for the sample deal")

print("== The sample event is already synced, so every tab has content ==")
expect(payload_hash(SAMPLE_DEAL) in body,
       "the canonical payload hash for the sample deal is shown")
expect("NS-1001" in body, "the matched NetSuite customer is shown")
expect("Exact tax ID" in body, "the winning match strategy is named")
expect("SO-" in body, "a NetSuite sales order reference was created")

print("== Financial feeds and the audit trail render ==")
for system in ("ADP", "Ramp", "Chase"):
    expect(system in body, f"the {system} feed line renders")
expect("/sites/Finance/Shared Documents/NetSuite Sync" in body,
       "the SharePoint audit library path renders")
expect("Awaiting settlement" in body, "the pending bank deposit is shown")

print("== Replaying the same event is rejected, not posted twice ==")
at = run()
widget(at, "button", "Replay the same event").click().run()
expect(not at.exception,
       f"replay runs clean (got {[str(e.value) for e in at.exception]})")
body = text_of(at)
expect("Rejected, duplicate" in body, "the replay is marked as a rejected duplicate")
expect("No second sales order exists" in body,
       "the page states plainly that nothing was posted twice")
expect("Duplicates prevented" in body, "the prevented duplicate reaches the KPI tiles")

print("== A genuinely different deal posts ==")
at = run()
widget(at, "number_input", "Deal amount").set_value(60000.0).run()
widget(at, "button", "Trigger Closed Won sync").click().run()
expect(not at.exception,
       f"a changed deal runs clean (got {[str(e.value) for e in at.exception]})")
body = text_of(at)
expect("Accepted" in body, "the changed payload is accepted")

print("== An unknown customer recommends creating, never guessing ==")
at = run()
widget(at, "text_input", "Tax ID").set_value("ZZ 000 111 22").run()
widget(at, "text_input", "Customer domain").set_value("brand-new-prospect.example").run()
widget(at, "text_input", "Company name").set_value("Brand New Prospect").run()
widget(at, "button", "Trigger Closed Won sync").click().run()
expect(not at.exception,
       f"an unknown customer runs clean (got {[str(e.value) for e in at.exception]})")
body = text_of(at)
expect("Create a new customer" in body, "the decision is to create a new customer")
expect("Create a new NetSuite customer" in body, "the next steps say to create it")

print("== Rollback reverses a posting ==")
at = run()
widget(at, "button", "Roll back this posting").click().run()
expect(not at.exception,
       f"rollback runs clean (got {[str(e.value) for e in at.exception]})")
body = text_of(at)
expect("Rolled back" in body, "the entry shows as rolled back")

print("== Typed input is escaped, never rendered as live HTML ==")
at = run()
hostile = '<img src=x onerror="window.__probe=1">Acme'
widget(at, "text_input", "Company name").set_value(hostile).run()
widget(at, "button", "Trigger Closed Won sync").click().run()
expect(not at.exception, "hostile input runs clean")
rendered = "\n".join(str(getattr(el, "value", "")) for el in at.markdown)
expect("<img src=x onerror=" not in rendered,
       "the raw payload is not emitted into the page")
expect("&lt;img" in rendered, "the typed markup is HTML escaped")

print()
if failures:
    print(f"SYNC UI RESULT: FAIL {len(failures)} of {checks} checks failed")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"SYNC UI RESULT: PASS {checks}/{checks}")
sys.exit(0)
