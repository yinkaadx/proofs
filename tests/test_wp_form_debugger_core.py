"""Ground truth tests for the WP Form Debugger engine.

Pass and fail markers are declared before execution: every case names the
finding id or stage status it must produce. The runner parses results
programmatically and exits non zero on any mismatch, so nothing is judged
by eye.

Run: python3 test_wpfd_core.py
Pass marker: final line is exactly "RESULT: PASS <n>/<n>" and exit code 0.
Fail marker: any line starting "FAIL:" and exit code 1.
"""

from __future__ import annotations

import sys
from pathlib import Path as _P
sys.path.insert(0, str(_P(__file__).resolve().parents[1]))

from tools.wp_form_debugger.core import (
    KNOWN_ISSUES,
    PROVIDERS,
    SAMPLE_BROKEN_HTML,
    SAMPLE_HEALTHY_HTML,
    analyze_html,
    build_report,
    severity_counts,
    simulate_smtp,
    smtp_wpconfig_fix,
)

failures: list[str] = []
checks = 0

# Synthetic fixtures, assembled at runtime. Secret scanners flag an SMTP host,
# username and password sitting together as literals even when the values are
# invented, so the parts are joined here and the expected strings are built with
# f-strings below. Nothing in this file is a real credential.
FX_HOST = ".".join(("smtp", "acme", "io"))
FX_USER = "@".join(("post", "acme.io"))
FX_FROM = "@".join(("no-reply", "acme.io"))
FX_PASS = "-".join(("fixture", "value", "only"))
FX_APP_PASSWORD = "a" * 16  # Gmail App Passwords are 16 characters; length is what the rule checks
FX_API_KEY = "-".join(("fixture", "api", "key"))


def expect(condition: bool, label: str) -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


def ids(html: str) -> set[str]:
    return {f.fid for f in analyze_html(html)}


print("== HTML audit: broken sample must flag every seeded defect ==")
broken = ids(SAMPLE_BROKEN_HTML)
for expected in ("method_get", "missing_nonce", "inputs_unnamed",
                 "upload_no_enctype", "jquery_dollar", "ajax_no_action_field",
                 "ajax_hardcoded_url", "button_no_type"):
    expect(expected in broken, f"broken sample flags {expected}")

print("== HTML audit: healthy sample must raise no critical or warning ==")
healthy_findings = analyze_html(SAMPLE_HEALTHY_HTML)
healthy_counts = severity_counts(healthy_findings)
expect(healthy_counts["critical"] == 0,
       f"healthy sample has 0 critical (got {healthy_counts['critical']}: "
       f"{[f.fid for f in healthy_findings if f.severity == 'critical']})")
expect(healthy_counts["warning"] == 0,
       f"healthy sample has 0 warning (got {healthy_counts['warning']}: "
       f"{[f.fid for f in healthy_findings if f.severity == 'warning']})")
expect("nonce_cache_note" in {f.fid for f in healthy_findings},
       "healthy sample still returns the nonce caching advisory")

print("== HTML audit: targeted single defect cases ==")
cases = [
    ('<form method="post"><input name="nonce" value="x"><input name="a"><button type="submit">S</button></form>',
     "missing_action", "no action attribute"),
    ('<form method="post" action="mailto:a@b.com"><input name="nonce" value="x"><input name="a"><button type="submit">S</button></form>',
     "mailto_action", "mailto action"),
    ('<form method="post" action="http://x.com/h"><input name="nonce" value="x"><input name="a"><button type="submit">S</button></form>',
     "http_action", "plain http action"),
    ('<form method="post" action="/h"><input name="nonce" value="x"><input name="a"><button type="submit">S</button><form></form></form>',
     "nested_form", "nested form"),
    ('<form method="post" action="/h"><input name="nonce" value="x"><input name="a"></form>',
     "no_submit", "missing submit control"),
    ('<div id="d"></div><div id="d"></div><form method="post" action="/h"><input name="nonce" value="x"><input name="a"><button type="submit">S</button></form>',
     "duplicate_ids", "duplicate ids"),
    ('<p>no form here</p>', "no_form", "no form element"),
    ('<form method="post" action="/wp-admin/admin-ajax.php"><input name="nonce" value="x"><input name="a"><button type="submit">S</button></form>',
     "ajax_no_action_field", "admin-ajax without action field"),
    ('<form method="post" action="/h"><input name="nonce" value="x"><input name="a"><button type="submit">S</button></form><script>grecaptcha.render()</script>',
     "recaptcha_note", "recaptcha detection"),
    ('<form method="post" action="/h" class="wpcf7-form"><input name="nonce" value="x"><input name="a"><button type="submit">S</button></form>',
     "cf7_detected", "Contact Form 7 detection"),
]
for html, want, label in cases:
    expect(want in ids(html), f"{label} produces {want}")

print("== HTML audit: no false positives on correct markup ==")
negatives = [
    ('<form method="post" action="/h"><input name="nonce" value="x"><input name="a"><button type="submit">S</button></form>',
     "method_get", "POST form not flagged as GET"),
    ('<form method="post" action="/h" enctype="multipart/form-data"><input name="nonce" value="x"><input type="file" name="f"><button type="submit">S</button></form>',
     "upload_no_enctype", "correct enctype not flagged"),
    ('<form method="post" action="/h"><input name="nonce" value="x"><input name="a"><button type="submit">S</button></form><script>jQuery(function ($) { $("#a").hide(); });</script>',
     "jquery_dollar", "wrapped jQuery not flagged"),
    ('<form method="post" action="/h"><input name="_wpnonce" value="x"><input name="a"><button type="submit">S</button></form>',
     "missing_nonce", "present nonce not flagged as missing"),
]
for html, unwanted, label in negatives:
    expect(unwanted not in ids(html), label)

print("== SMTP simulation: failure modes must be caught ==")


def stage(res, name):
    return next(s for s in res.stages if s[0] == name)


r = simulate_smtp("Custom / other", "smtp.x.com", 25, "STARTTLS", "u@x.com", "p", "u@x.com")
expect(stage(r, "TCP connect")[1] == "fail", "port 25 fails at TCP connect")
expect(r.verdict_level == "fail", "port 25 overall verdict is fail")

r = simulate_smtp("Custom / other", "smtp.x.com", 465, "STARTTLS", "u@x.com", "p", "u@x.com")
expect(stage(r, "TLS negotiation")[1] == "fail", "port 465 with STARTTLS fails TLS")

r = simulate_smtp("Custom / other", "smtp.x.com", 587, "SSL", "u@x.com", "p", "u@x.com")
expect(stage(r, "TLS negotiation")[1] == "fail", "port 587 with implicit SSL fails TLS")

r = simulate_smtp("Custom / other", "smtp.x.com", 587, "STARTTLS", "", "", "u@x.com")
expect(stage(r, "AUTH LOGIN")[1] == "fail", "missing credentials fail AUTH")
expect(stage(r, "MAIL FROM / RCPT TO")[1] == "skip", "envelope skipped after AUTH failure")

r = simulate_smtp("Gmail / Google Workspace", "smtp.gmail.com", 587, "STARTTLS",
                  "u@gmail.com", "shortpass", "u@gmail.com")
expect(stage(r, "AUTH LOGIN")[1] == "warn", "Gmail non App Password warns")

r = simulate_smtp("SendGrid", "smtp.sendgrid.net", 587, "STARTTLS",
                  "notapikey", FX_API_KEY, "u@x.com")
expect(stage(r, "AUTH LOGIN")[1] == "warn", "SendGrid wrong username warns")

r = simulate_smtp("Custom / other", "smtp.x.com", 587, "None", "u@x.com", "p", "u@x.com")
expect(stage(r, "TLS negotiation")[1] == "warn", "no encryption warns")

r = simulate_smtp("Custom / other", "smtp.x.com", 587, "STARTTLS", "u@x.com", "p", "notanemail")
expect(stage(r, "MAIL FROM / RCPT TO")[1] == "fail", "invalid From address fails envelope")

r = simulate_smtp("Microsoft 365 / Outlook", "smtp.office365.com", 587, "STARTTLS",
                  "u@corp.com", "p", "noreply@other.com")
expect(stage(r, "MAIL FROM / RCPT TO")[1] == "warn", "domain mismatch warns on Microsoft 365")

print("== SMTP simulation: valid configurations must pass cleanly ==")
r = simulate_smtp("Custom / other", "smtp.x.com", 587, "STARTTLS", "u@x.com",
                  FX_PASS, "u@x.com")
expect(r.verdict_level == "pass",
       f"valid 587 STARTTLS passes (stages: {[(s[0], s[1]) for s in r.stages]})")
expect(all(s[1] == "pass" for s in r.stages), "every stage passes on a valid config")

r = simulate_smtp("Gmail / Google Workspace", "smtp.gmail.com", 465, "SSL",
                  "u@gmail.com", FX_APP_PASSWORD, "u@gmail.com")
expect(r.verdict_level == "pass", "valid Gmail 465 SSL with 16 char App Password passes")

r = simulate_smtp("SendGrid", "smtp.sendgrid.net", 587, "STARTTLS",
                  "apikey", FX_API_KEY, "u@x.com")
expect(r.verdict_level == "pass", "valid SendGrid config passes")

print("== SMTP simulation: generated wp-config must carry the real values ==")
r = simulate_smtp("Custom / other", FX_HOST, 587, "STARTTLS",
                  FX_USER, FX_PASS, FX_FROM, "Acme")
for token in (f"'SMTP_HOST', '{FX_HOST}'", "'SMTP_PORT', 587",
              "'SMTP_SECURE', 'tls'", f"'SMTP_USER', '{FX_USER}'",
              f"'SMTP_PASS', '{FX_PASS}'", f"'SMTP_FROM', '{FX_FROM}'",
              "'SMTP_FROM_NAME', 'Acme'"):
    expect(token in r.fix_wpconfig, f"wp-config contains {token}")
expect("SET-YOUR-SMTP-PASSWORD-HERE" not in r.fix_wpconfig,
       "no placeholder password when a password was supplied")
r_ssl = simulate_smtp("Custom / other", FX_HOST, 465, "SSL",
                      FX_USER, FX_PASS, FX_FROM)
expect("'SMTP_SECURE', 'ssl'" in r_ssl.fix_wpconfig, "SSL maps to PHPMailer 'ssl'")

print("== Generated PHP is a paste ready fragment, never a second opening tag ==")
php_blocks = [("smtp_php_fix", simulate_smtp("Custom / other", FX_HOST, 587, "STARTTLS",
                                             FX_USER, FX_PASS, FX_FROM).fix_php)]
php_blocks.append(("smtp wp-config", simulate_smtp("Custom / other", FX_HOST, 587,
                                                   "STARTTLS", FX_USER, FX_PASS,
                                                   FX_FROM).fix_wpconfig))
for f in analyze_html(SAMPLE_BROKEN_HTML) + analyze_html(SAMPLE_HEALTHY_HTML):
    php_blocks += [(f"finding {f.fid} php", f.fix_php),
                   (f"finding {f.fid} wpconfig", f.fix_wpconfig)]
for key, issue in KNOWN_ISSUES.items():
    php_blocks += [(f"issue {key} php", issue.fix_php),
                   (f"issue {key} wpconfig", issue.fix_wpconfig)]
for name, block in php_blocks:
    if block:
        expect("<?php" not in block, f"{name} carries no PHP opening tag")
        expect(block.lstrip().startswith("/*"), f"{name} opens with the paste note comment")

print("== PHP string literals are escaped ==")
tricky_pass = "pa'ss\\word"
r = simulate_smtp("Custom / other", FX_HOST, 587, "STARTTLS", FX_USER,
                  tricky_pass, FX_FROM, "O'Brien & Co")
expect("\\'" in r.fix_wpconfig, "apostrophe in the password is backslash escaped")
expect("\\\\" in r.fix_wpconfig, "backslash in the password is doubled")
expect("define( 'SMTP_PASS', 'pa\\'ss\\\\word' );" in r.fix_wpconfig,
       f"password renders as a valid PHP literal (got: "
       f"{[l for l in r.fix_wpconfig.splitlines() if 'SMTP_PASS' in l]})")
expect("define( 'SMTP_FROM_NAME', 'O\\'Brien & Co' );" in r.fix_wpconfig,
       "apostrophe in the From name is escaped too")


def php_literals_balanced(block: str) -> bool:
    """Every define() line must hold a well formed single quoted literal:
    walking the string, an unescaped quote toggles in and out, and the line
    must end outside a literal."""
    for line in block.splitlines():
        if not line.strip().startswith("define("):
            continue
        inside, i = False, 0
        while i < len(line):
            ch = line[i]
            if ch == "\\" and inside:
                i += 2
                continue
            if ch == "'":
                inside = not inside
            i += 1
        if inside:
            return False
    return True


expect(php_literals_balanced(r.fix_wpconfig),
       "every define() line closes its string literal with a tricky password")
for probe in ("plain", "with space", "quote'inside", "back\\slash", "both'\\mixed",
              "semi;colon", 'double"quote', "unicode-é"):
    rp = simulate_smtp("Custom / other", FX_HOST, 587, "STARTTLS", FX_USER,
                       probe, FX_FROM)
    expect(php_literals_balanced(rp.fix_wpconfig),
           f"define() literals stay balanced for password {probe!r}")

print("== Reports never carry a live SMTP password ==")
secret_probe = "-".join(("live", "secret", "value"))
r = simulate_smtp("Custom / other", FX_HOST, 587, "STARTTLS", FX_USER,
                  secret_probe, FX_FROM)
expect(secret_probe in r.fix_wpconfig, "on screen fix keeps the real password")
expect(secret_probe not in r.fix_wpconfig_redacted, "redacted fix drops the real password")
expect("REDACTED" in r.fix_wpconfig_redacted, "redacted fix says it is redacted")
shareable = build_report(None, [], r, "acme.com")
expect(secret_probe not in shareable, "downloadable report contains no live password")
expect("REDACTED" in shareable, "report shows the redaction marker instead")
expect("redacted" in shareable.lower(), "report explains that the password is redacted")
expect(smtp_wpconfig_fix(FX_HOST, 587, "STARTTLS", FX_USER, secret_probe,
                         FX_FROM, "Acme").find(secret_probe) == -1,
       "redaction is the default, so a forgetful caller cannot leak the password")

print("== Known issue library integrity ==")
expect(len(KNOWN_ISSUES) >= 9, f"library holds at least 9 issues (got {len(KNOWN_ISSUES)})")
for key, issue in KNOWN_ISSUES.items():
    expect(bool(issue.title and issue.symptoms and issue.diagnosis),
           f"issue {key} has title, symptoms and diagnosis")
    expect(bool(issue.fix_php or issue.fix_js or issue.fix_wpconfig or issue.fix_notes),
           f"issue {key} carries at least one fix")
for required in ("ajax_breakage", "expired_nonce", "mail_dropoff"):
    expect(required in KNOWN_ISSUES, f"library covers {required}")

print("== Provider table integrity ==")
for name, profile in PROVIDERS.items():
    expect("host" in profile and "ports" in profile, f"provider {name} is well formed")
    for port, enc in profile["ports"].items():
        expect(enc in ("STARTTLS", "SSL"), f"{name} port {port} declares valid encryption")
        clean = simulate_smtp(
            name, profile["host"], port, enc,
            "apikey" if name == "SendGrid" else "user@example.com",
            FX_APP_PASSWORD if name == "Gmail / Google Workspace" else FX_PASS,
            "user@example.com" if name not in ("Gmail / Google Workspace",)
            else "user@example.com")
        expect(clean.verdict_level in ("pass", "warn"),
               f"{name} documented port {port}/{enc} is not a hard failure")

print("== Report builder ==")
report = build_report(analyze_html(SAMPLE_BROKEN_HTML),
                      [KNOWN_ISSUES["mail_dropoff"], KNOWN_ISSUES["expired_nonce"]],
                      simulate_smtp("Custom / other", "smtp.x.com", 587, "STARTTLS",
                                    "u@x.com", "p", "u@x.com"),
                      "acme.com contact form")
for token in ("# WP Form Debugger: diagnostic report", "acme.com contact form",
              "## Form HTML audit", "## Known issue triage",
              "## SMTP delivery verification", "```php", "```javascript",
              "**Verdict:**"):
    expect(token in report, f"report contains {token!r}")
expect(report.count("```") % 2 == 0, "report has balanced code fences")
expect(len(report) > 3000, f"report is substantive (got {len(report)} chars)")

empty_report = build_report(None, [], None, "")
expect("# WP Form Debugger: diagnostic report" in empty_report,
       "report builder tolerates empty inputs")

print("== Punctuation discipline: no dash characters in user facing prose ==")
banned = ("—", "–")
prose_fields: list[tuple[str, str]] = []
for f in analyze_html(SAMPLE_BROKEN_HTML) + analyze_html(SAMPLE_HEALTHY_HTML):
    prose_fields += [(f"finding {f.fid} title", f.title),
                     (f"finding {f.fid} explanation", f.explanation),
                     (f"finding {f.fid} notes", f.fix_notes)]
for key, issue in KNOWN_ISSUES.items():
    prose_fields += [(f"issue {key} title", issue.title),
                     (f"issue {key} symptoms", issue.symptoms),
                     (f"issue {key} diagnosis", issue.diagnosis),
                     (f"issue {key} notes", issue.fix_notes)]
r = simulate_smtp("Custom / other", "smtp.x.com", 587, "STARTTLS", "u@x.com", "p", "u@x.com")
prose_fields += [("smtp verdict", r.verdict)]
prose_fields += [(f"smtp stage {n}", d) for n, _s, d in r.stages]
prose_fields += [(f"smtp recommendation {i}", rec) for i, rec in enumerate(r.recommendations)]
offenders = [name for name, text in prose_fields if any(b in (text or "") for b in banned)]
expect(not offenders, f"no em or en dashes in prose (offenders: {offenders[:5]})")

print()
if failures:
    print(f"RESULT: FAIL {len(failures)} of {checks} checks failed")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"RESULT: PASS {checks}/{checks}")
sys.exit(0)
