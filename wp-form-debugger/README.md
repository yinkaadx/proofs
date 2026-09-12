# WP Form Debugger

Enterprise diagnostics for WordPress forms that stopped submitting or stopped
delivering. Paste the form markup or pick the symptoms, and the tool returns the
root cause with the exact PHP, JavaScript and wp-config.php fix, plus a client
ready report.

## What it does

**Form Audit.** Static analysis of pasted form HTML. Catches missing or wrong
`action` attributes (including `mailto:` and plain `http://` submissions),
`admin-ajax.php` posts with no `action` field, missing security nonces, `GET`
where the handler reads `$_POST`, fields with no `name` attribute, file uploads
without `multipart/form-data`, nested forms, duplicate ids, missing submit
controls, inline jQuery using `$` under noConflict, and hardcoded admin-ajax
URLs. Recognises Contact Form 7, WPForms, Gravity Forms, Elementor and
reCAPTCHA markup and adds the plugin specific checks for each.

**Known Issues.** A triage library covering the failure modes that account for
most support tickets: AJAX script breakage, expired security nonces under page
caching, unauthenticated PHP mail drop offs, SMTP authentication failures,
jQuery conflicts after updates, blocked REST endpoints, hosts blocking outbound
port 25, missing SPF, DKIM and DMARC, and post update plugin conflicts. Each
returns symptoms, diagnosis and a complete fix.

**SMTP Delivery Check.** A simulated handshake across six stages: host
resolution, TCP connect, TLS negotiation, AUTH, envelope and delivery. Each
stage is evaluated against the provider's own requirements and the port and
encryption pairings a real relay enforces, so misconfigurations surface before
anyone edits a production server. Covers Gmail and Google Workspace, Microsoft
365, SendGrid, Mailgun, Amazon SES, Brevo and custom relays. Output includes a
`wp-config.php` block carrying the values you entered and the matching
`phpmailer_init` handler.

**Report.** Everything from the session assembled into one Markdown file,
downloadable with a client label in the header.

No connection is opened and no credential is transmitted or stored. All analysis
runs in the session.

## Run locally

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

The app serves on http://localhost:8501.

## Tests

```bash
python3 test_wpfd_core.py
```

The suite declares its pass and fail markers before running and parses results
programmatically. A clean run prints `RESULT: PASS 120/120` and exits 0; any
failure prints lines beginning `FAIL` and exits 1.

## Layout

| File | Purpose |
| --- | --- |
| `streamlit_app.py` | User interface, four tabs, styling and report download |
| `wpfd_core.py` | Analysis engine: HTML audit, issue library, SMTP simulation, report builder |
| `test_wpfd_core.py` | Ground truth test suite, 120 checks |
| `requirements.txt` | Runtime dependencies |
| `.streamlit/config.toml` | Theme and client settings |

The engine has no Streamlit dependency, so it can be reused behind a CLI, an API
or a scheduled audit without changes.
