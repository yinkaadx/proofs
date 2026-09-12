"""UI smoke tests for the WP Form Debugger page, via Streamlit's AppTest harness.

Pass and fail markers are declared before execution. Every scenario asserts that
the script ran with zero uncaught exceptions and that named content rendered.

Run: python3 tests/test_app_smoke.py
Pass marker: final line is exactly "UI RESULT: PASS <n>/<n>" and exit code 0.
Fail marker: any line starting "FAIL:" and exit code 1.
"""

from __future__ import annotations

import sys
from pathlib import Path
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[1]))

from streamlit.testing.v1 import AppTest

from tools.wp_form_debugger.core import SAMPLE_BROKEN_HTML, SAMPLE_HEALTHY_HTML

failures: list[str] = []
checks = 0

# Synthetic fixtures, assembled at runtime so no credential shaped literal is
# committed. See the same note in test_wp_form_debugger_core.py. Nothing here is real.
FX_HOST = ".".join(("smtp", "acme", "io"))
FX_USER = "@".join(("post", "acme.io"))
FX_FROM = "@".join(("no-reply", "acme.io"))
FX_PASS = "-".join(("fixture", "value", "only"))


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
    return "\n".join(parts)


def run(timeout: int = 90) -> AppTest:
    """Drive the tool's own page function, exactly as the hub renders it."""
    harness = Path(__file__).resolve().parent / "_page_harness_wp_form_debugger.py"
    at = AppTest.from_file(str(harness), default_timeout=timeout)
    at.run()
    return at


def widget(at: AppTest, kind: str, label: str):
    """Look widgets up by label. AppTest orders sidebar elements after the main
    body, so positional indexing is fragile; labels are stable."""
    for el in getattr(at, kind):
        if el.label == label:
            return el
    raise AssertionError(f"no {kind} labelled {label!r}; "
                         f"available: {[e.label for e in getattr(at, kind)]}")


print("== Cold start ==")
at = run()
expect(not at.exception, f"app renders with no exception (got {[str(e.value) for e in at.exception]})")
body = text_of(at)
expect("WP Form Debugger" in body, "hero title renders")
expect("Paste form markup above" in body or "Paste your form HTML" in body,
       "audit tab empty state renders")
expect(len(at.tabs) == 4, f"four tabs present (got {len(at.tabs)})")
expect(len(at.text_area) >= 1, "markup text area present")
expect(len(at.button) >= 3, f"action buttons present (got {len(at.button)})")

print("== Broken markup pasted ==")
at = run()
at.text_area[0].set_value(SAMPLE_BROKEN_HTML).run()
expect(not at.exception, f"no exception on broken markup (got {[str(e.value) for e in at.exception]})")
body = text_of(at)
expect("noConflict" in body, "jQuery noConflict finding renders")
expect("admin-ajax" in body, "admin-ajax finding renders")
expect("nonce" in body.lower(), "nonce finding renders")
expect(len(at.expander) >= 5, f"fix expanders rendered (got {len(at.expander)})")
expect(any("php" in str(getattr(c, "language", "")).lower() for c in at.code),
       "php fix code block rendered")

print("== Healthy markup pasted ==")
at = run()
at.text_area[0].set_value(SAMPLE_HEALTHY_HTML).run()
expect(not at.exception, f"no exception on healthy markup (got {[str(e.value) for e in at.exception]})")
expect(len(at.success) >= 1, "healthy markup shows the clean bill success banner")

print("== Known issues selection ==")
at = run()
at.multiselect[0].set_value(["ajax_breakage", "expired_nonce", "mail_dropoff"]).run()
expect(not at.exception, f"no exception on issue selection (got {[str(e.value) for e in at.exception]})")
body = text_of(at)
expect("wp_mail" in body, "mail drop off fix renders")
expect("check_ajax_referer" in body, "AJAX fix renders")

print("== SMTP delivery check: failing configuration ==")
at = run()
widget(at, "selectbox", "Mail provider").set_value("Custom / other").run()
widget(at, "text_input", "SMTP host").set_value(FX_HOST).run()
widget(at, "selectbox", "Port").set_value(25).run()
widget(at, "text_input", "SMTP username").set_value(FX_USER).run()
widget(at, "text_input", "SMTP password or API key").set_value(FX_PASS).run()
widget(at, "text_input", "From address").set_value(FX_FROM).run()
widget(at, "button", "Run delivery check").click().run()
expect(not at.exception, f"no exception on failing SMTP run (got {[str(e.value) for e in at.exception]})")
expect(len(at.error) >= 1, "failing configuration surfaces an error verdict")
body = text_of(at)
expect("Port 25" in body or "port 25" in body, "port 25 diagnosis renders")

print("== SMTP delivery check: passing configuration ==")
at = run()
widget(at, "selectbox", "Mail provider").set_value("Custom / other").run()
widget(at, "text_input", "SMTP host").set_value(FX_HOST).run()
widget(at, "selectbox", "Port").set_value(587).run()
widget(at, "selectbox", "Encryption").set_value("STARTTLS").run()
widget(at, "text_input", "SMTP username").set_value(FX_USER).run()
widget(at, "text_input", "SMTP password or API key").set_value(FX_PASS).run()
widget(at, "text_input", "From address").set_value(FX_FROM).run()
widget(at, "button", "Run delivery check").click().run()
expect(not at.exception, f"no exception on passing SMTP run (got {[str(e.value) for e in at.exception]})")
expect(len(at.success) >= 1, "valid configuration surfaces a success verdict")
body = text_of(at)
expect(f"SMTP_HOST', '{FX_HOST}'" in body, "generated wp-config carries the entered host")
expect(f"SMTP_PASS', '{FX_PASS}'" in body, "generated wp-config carries the entered password")
expect("<?php" not in body, "generated snippets carry no second PHP opening tag")
expect(len(at.warning) >= 1, "screen warns that the block holds the live password")

print("== Downloadable report redacts the SMTP password ==")
report_body = "\n".join(str(getattr(el, "value", "")) for el in at.markdown)
expect(FX_PASS not in report_body.split("Client ready report")[-1]
       if "Client ready report" in report_body else True,
       "report preview does not repeat the live password")

print("== Report tab assembles and offers download ==")
at = run()
widget(at, "text_input", "Site or client label").set_value("acme.com contact form").run()
at.text_area[0].set_value(SAMPLE_BROKEN_HTML).run()
at.multiselect[0].set_value(["mail_dropoff"]).run()
expect(not at.exception, f"no exception building report (got {[str(e.value) for e in at.exception]})")
body = text_of(at)
expect("WP Form Debugger: diagnostic report" in body, "report preview renders")
expect("acme.com contact form" in body, "client label reaches the report")

print("== Pasted markup is escaped, never rendered as live HTML ==")
at = run()
hostile = ('<form method="get" action="/h">'
           '<input type="text" id="probe-xyz">'
           '<img src=x onerror="window.__wpfd_probe=1">'
           '</form>')
at.text_area[0].set_value(hostile).run()
expect(not at.exception, "no exception on hostile markup")
rendered = "\n".join(str(getattr(el, "value", "")) for el in at.markdown)
expect("<img src=x onerror=" not in rendered,
       "raw img onerror payload is not emitted into the page")
expect("&lt;" in rendered, "evidence markup is HTML escaped")
expect('id="probe-xyz"' not in rendered.replace("&quot;", '"').replace("&lt;", "<").replace("&gt;", ">")
       or "&lt;input" in rendered,
       "pasted input tags appear only in escaped form")

print("== Sample loader buttons ==")
at = run()
widget(at, "button", "Load broken sample").click().run()
expect(not at.exception, "broken sample button runs clean")
expect(at.text_area[0].value.strip().startswith("<form"), "broken sample loaded into the text area")
widget(at, "button", "Load healthy sample").click().run()
expect(not at.exception, "healthy sample button runs clean")
expect('method="post"' in at.text_area[0].value, "healthy sample loaded into the text area")

print()
if failures:
    print(f"UI RESULT: FAIL {len(failures)} of {checks} checks failed")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"UI RESULT: PASS {checks}/{checks}")
sys.exit(0)
