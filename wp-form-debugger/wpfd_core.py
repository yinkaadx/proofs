"""WP Form Debugger core engine.

Pure logic module: HTML form auditing, known issue triage, SMTP delivery
simulation and fix generation. No Streamlit imports here so the engine is
unit testable on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

ENGINE_VERSION = "1.0.0"

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
SEVERITY_LABEL = {"critical": "Critical", "warning": "Warning", "info": "Info"}

REDACTED = "REDACTED-SEE-THE-APP-FOR-THE-REAL-VALUE"

# Every generated PHP block is a fragment pasted into a file that is already
# inside a PHP block, so none of them carry their own opening tag.
PASTE_NOTE = "/* Paste inside the existing PHP block. Do not add another opening tag. */"


def php_quote(value: str) -> str:
    """Render a Python string as a PHP single quoted literal.

    Inside single quotes PHP only gives backslash and the quote itself a
    special meaning, so those two are the only characters to escape. Without
    this, a password containing an apostrophe produces a wp-config.php that
    will not parse, and a backslash silently changes the stored value.
    """
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def php_fragment(body: str) -> str:
    """Prefix a PHP snippet with the paste note. Every snippet here is pasted
    into a file that is already inside a PHP block."""
    return f"{PASTE_NOTE}\n{body}"


@dataclass
class Finding:
    fid: str
    severity: str
    title: str
    evidence: str
    explanation: str
    fix_php: str = ""
    fix_js: str = ""
    fix_wpconfig: str = ""
    fix_notes: str = ""


# ---------------------------------------------------------------------------
# Reusable fix snippets (exact, complete, copy paste ready)
# ---------------------------------------------------------------------------

FIX_PHP_AJAX = php_fragment(r"""/**
 * functions.php: correct AJAX form wiring.
 * 1) Enqueue the form script with jQuery declared as a dependency.
 * 2) Pass the AJAX URL and a fresh nonce to the script (never hardcode either).
 * 3) Register the handler for logged in AND logged out visitors.
 */
add_action( 'wp_enqueue_scripts', function () {
    wp_enqueue_script(
        'acme-contact-form',
        get_stylesheet_directory_uri() . '/js/contact-form.js',
        array( 'jquery' ),
        '1.0.2',
        true
    );
    wp_localize_script( 'acme-contact-form', 'acmeForm', array(
        'ajaxUrl' => admin_url( 'admin-ajax.php' ),
        'nonce'   => wp_create_nonce( 'acme_contact_form' ),
    ) );
} );

add_action( 'wp_ajax_acme_contact', 'acme_handle_contact' );
add_action( 'wp_ajax_nopriv_acme_contact', 'acme_handle_contact' );

function acme_handle_contact() {
    check_ajax_referer( 'acme_contact_form', 'nonce' );

    $name    = sanitize_text_field( wp_unslash( $_POST['name'] ?? '' ) );
    $email   = sanitize_email( wp_unslash( $_POST['email'] ?? '' ) );
    $message = sanitize_textarea_field( wp_unslash( $_POST['message'] ?? '' ) );

    if ( ! is_email( $email ) || '' === $message ) {
        wp_send_json_error( array( 'message' => 'Invalid input.' ), 400 );
    }

    $sent = wp_mail(
        get_option( 'admin_email' ),
        sprintf( 'Website enquiry from %s', $name ),
        $message,
        array( 'Reply-To: ' . $email )
    );

    if ( $sent ) {
        wp_send_json_success( array( 'message' => 'Thank you, your message was sent.' ) );
    }
    wp_send_json_error( array( 'message' => 'Mail could not be sent.' ), 500 );
}
""")

FIX_JS_AJAX = r"""/* js/contact-form.js: submit through admin-ajax with the localized URL and nonce.
   The jQuery(function ($) { ... }) wrapper restores $ safely under WordPress
   noConflict mode, which is the usual cause of "$ is not defined". */
jQuery(function ($) {
  $('#contact-form').on('submit', function (e) {
    e.preventDefault();
    $.post(acmeForm.ajaxUrl, {
      action: 'acme_contact',
      nonce: acmeForm.nonce,
      name: $('#cf-name').val(),
      email: $('#cf-email').val(),
      message: $('#cf-message').val()
    })
      .done(function (res) {
        $('#cf-status').text(res.data.message);
      })
      .fail(function (xhr) {
        var msg = (xhr.responseJSON && xhr.responseJSON.data)
          ? xhr.responseJSON.data.message
          : 'Request failed (HTTP ' + xhr.status + ')';
        $('#cf-status').text(msg);
      });
  });
});
"""

FIX_PHP_NONCE_REFRESH = php_fragment(r"""/**
 * functions.php: expired nonce repair for cached pages.
 * A page cache serves HTML older than the nonce lifetime (12 to 24 hours),
 * so the embedded nonce fails with 403 or a bare "-1" response.
 * Fix: fetch a fresh nonce over AJAX on page load instead of trusting the
 * one baked into the cached HTML.
 */
add_action( 'wp_ajax_acme_fresh_nonce', 'acme_fresh_nonce' );
add_action( 'wp_ajax_nopriv_acme_fresh_nonce', 'acme_fresh_nonce' );

function acme_fresh_nonce() {
    wp_send_json_success( array( 'nonce' => wp_create_nonce( 'acme_contact_form' ) ) );
}
""")

FIX_JS_NONCE_REFRESH = r"""/* js/contact-form.js addition: replace the page's stale nonce at load time. */
jQuery(function ($) {
  $.post(acmeForm.ajaxUrl, { action: 'acme_fresh_nonce' })
    .done(function (res) {
      acmeForm.nonce = res.data.nonce;
    });
});
"""

FIX_NOTES_NONCE_CACHE = (
    "Also exclude the form page from full page caching: WP Rocket under "
    "Settings, Advanced Rules, Never cache URLs; W3 Total Cache under Page "
    "Cache, Never cache the following pages; LiteSpeed Cache under Cache, "
    "Excludes, Do Not Cache URIs. Extending nonce_life is possible but "
    "weakens CSRF protection, so prefer the refresh endpoint above."
)

FIX_PHP_MAIL_LOGGER = php_fragment(r"""/**
 * functions.php: make silent mail drop offs visible.
 * wp_mail() can return true while the message never leaves the server,
 * because PHP mail() hands off to the local MTA with no delivery feedback.
 * This logger records every PHPMailer failure to wp-content/debug.log.
 */
add_action( 'wp_mail_failed', function ( $error ) {
    error_log( 'wp_mail failed: ' . $error->get_error_message() );
} );
""")


def smtp_php_fix() -> str:
    return php_fragment(r"""/**
 * functions.php: route ALL WordPress mail through authenticated SMTP.
 * Reads its credentials from wp-config.php constants so secrets stay
 * out of the theme.
 */
add_action( 'phpmailer_init', function ( $phpmailer ) {
    $phpmailer->isSMTP();
    $phpmailer->Host       = SMTP_HOST;
    $phpmailer->Port       = SMTP_PORT;
    $phpmailer->SMTPSecure = SMTP_SECURE; // 'tls' means STARTTLS, 'ssl' means implicit SSL
    $phpmailer->SMTPAuth   = true;
    $phpmailer->Username   = SMTP_USER;
    $phpmailer->Password   = SMTP_PASS;
    $phpmailer->From       = SMTP_FROM;
    $phpmailer->FromName   = SMTP_FROM_NAME;
} );

add_action( 'wp_mail_failed', function ( $error ) {
    error_log( 'wp_mail failed: ' . $error->get_error_message() );
} );
""")


def smtp_wpconfig_fix(host: str, port: int, encryption: str, username: str,
                      password: str, from_addr: str, from_name: str,
                      redact_password: bool = True) -> str:
    """Build the wp-config.php block.

    redact_password defaults to True so that any caller which forgets to think
    about it cannot leak a live credential. Pass False only where the output
    goes straight to the operator on screen, never into a shareable file.
    """
    secure = "tls" if encryption == "STARTTLS" else ("ssl" if encryption == "SSL" else "")
    if redact_password:
        shown_pass = REDACTED
    else:
        shown_pass = password if password else "SET-YOUR-SMTP-PASSWORD-HERE"
    return (
        "/* wp-config.php: add ABOVE the line that says 'That's all, stop editing!'. */\n"
        f"{PASTE_NOTE}\n"
        f"define( 'SMTP_HOST', {php_quote(host)} );\n"
        f"define( 'SMTP_PORT', {int(port)} );\n"
        f"define( 'SMTP_SECURE', {php_quote(secure)} );\n"
        f"define( 'SMTP_USER', {php_quote(username)} );\n"
        f"define( 'SMTP_PASS', {php_quote(shown_pass)} );\n"
        f"define( 'SMTP_FROM', {php_quote(from_addr)} );\n"
        f"define( 'SMTP_FROM_NAME', {php_quote(from_name)} );\n"
        "\n"
        "/* Delivery debugging: failures land in wp-content/debug.log. */\n"
        "define( 'WP_DEBUG', true );\n"
        "define( 'WP_DEBUG_LOG', true );\n"
        "define( 'WP_DEBUG_DISPLAY', false );\n"
    )


# ---------------------------------------------------------------------------
# HTML form audit
# ---------------------------------------------------------------------------

SAMPLE_BROKEN_HTML = """<form id="contact-form" method="get" action="/wp-admin/admin-ajax.php">
  <input type="text" id="cf-name" placeholder="Your name">
  <input type="email" name="email" id="cf-email" placeholder="Email">
  <textarea name="message" id="cf-message"></textarea>
  <input type="file" name="attachment">
  <button>Send</button>
</form>
<script>
  $('#contact-form').on('submit', function (e) {
    e.preventDefault();
    $.post('/wp-admin/admin-ajax.php', $(this).serialize());
  });
</script>"""

SAMPLE_HEALTHY_HTML = """<form id="contact-form" method="post" action="https://example.com/wp-admin/admin-ajax.php" enctype="multipart/form-data">
  <input type="hidden" name="action" value="acme_contact">
  <input type="hidden" name="nonce" value="a1b2c3d4e5">
  <input type="text" name="name" id="cf-name">
  <input type="email" name="email" id="cf-email">
  <textarea name="message" id="cf-message"></textarea>
  <button type="submit">Send</button>
</form>
<script>
jQuery(function ($) {
  $('#contact-form').on('submit', function (e) {
    e.preventDefault();
    $.post(acmeForm.ajaxUrl, $(this).serialize());
  });
});
</script>"""


def analyze_html(html: str) -> list[Finding]:
    """Run every static diagnostic against pasted form HTML."""
    findings: list[Finding] = []
    soup = BeautifulSoup(html or "", "html.parser")
    forms = soup.find_all("form")
    scripts_text = "\n".join(s.get_text() for s in soup.find_all("script"))

    if not forms:
        findings.append(Finding(
            fid="no_form", severity="critical", title="No <form> element found",
            evidence="The pasted markup contains no <form> tag.",
            explanation=(
                "Nothing here can submit. If the form is injected by JavaScript "
                "(a page builder or an SPA block), paste the rendered markup from "
                "the browser inspector, not the page source."),
            fix_notes=(
                "In Chrome or Firefox: right click the visible form, choose "
                "Inspect, right click the <form> node, Copy, Copy outerHTML, "
                "then paste that here."),
        ))
        _script_checks(findings, scripts_text, html)
        return _sorted(findings)

    for form in forms:
        action = (form.get("action") or "").strip()
        method = (form.get("method") or "get").strip().lower()

        if form.find("form") is not None:
            findings.append(Finding(
                fid="nested_form", severity="critical", title="Form nested inside another form",
                evidence=_clip(str(form)[:200]),
                explanation=(
                    "HTML forbids nested forms; browsers silently drop the inner "
                    "one, so its fields never submit. This usually happens when a "
                    "form shortcode is placed inside a page builder form widget."),
                fix_notes="Move the inner form outside the outer <form> element, or remove the wrapper form.",
            ))

        if not action:
            findings.append(Finding(
                fid="missing_action", severity="warning", title="Form has no action attribute",
                evidence=_clip(str(form)[:200]),
                explanation=(
                    "With no action the form posts back to the current URL. On "
                    "WordPress that only works when a handler runs on that page "
                    "template; most broken forms in this state were meant to post "
                    "to admin-ajax.php."),
                fix_php=FIX_PHP_AJAX, fix_js=FIX_JS_AJAX,
            ))
        else:
            if action.lower().startswith("mailto:"):
                findings.append(Finding(
                    fid="mailto_action", severity="critical", title="Form submits to a mailto: address",
                    evidence=f'action="{action}"',
                    explanation=(
                        "mailto: forms never touch PHP or wp_mail. They open the "
                        "visitor's local mail client, which on most machines is not "
                        "configured, so submissions silently vanish. This is the "
                        "classic unauthenticated mail drop off."),
                    fix_php=FIX_PHP_AJAX, fix_js=FIX_JS_AJAX,
                    fix_notes="Replace the mailto action with a real AJAX handler, then route delivery through authenticated SMTP (SMTP Delivery Check tab)."),
                )
            elif action.lower().startswith("http://"):
                findings.append(Finding(
                    fid="http_action", severity="critical", title="Form posts to plain http://",
                    evidence=f'action="{action}"',
                    explanation=(
                        "On an https site the browser blocks or downgrades a plain "
                        "http submission as mixed content, and the POST dies before "
                        "reaching WordPress."),
                    fix_notes=(
                        "Change the action to https, then fix the root cause in "
                        "wp-config.php or Settings, General so siteurl and home both "
                        "use https."),
                    fix_wpconfig=php_fragment(
                        "/* wp-config.php: force correct https URLs. */\n"
                        "define( 'WP_HOME', 'https://example.com' );\n"
                        "define( 'WP_SITEURL', 'https://example.com' );\n"),
                ))

            if "admin-ajax.php" in action:
                has_action_field = any(
                    (i.get("name") or "").strip() == "action" for i in form.find_all("input")
                )
                if not has_action_field:
                    findings.append(Finding(
                        fid="ajax_no_action_field", severity="critical",
                        title="Posts to admin-ajax.php without an action field",
                        evidence=f'action="{action}" and no <input name="action"> present',
                        explanation=(
                            "admin-ajax.php routes every request by its action "
                            "parameter. Without one WordPress answers 400 (or the "
                            "legacy bare 0) and the submission is lost. This is the "
                            "standard AJAX script breakage signature."),
                        fix_php=FIX_PHP_AJAX, fix_js=FIX_JS_AJAX,
                    ))

        if method != "post":
            findings.append(Finding(
                fid="method_get", severity="warning", title="Form uses GET instead of POST",
                evidence=f'method="{method or "get"}"',
                explanation=(
                    "GET puts every field, including personal data, into the URL, "
                    "gets truncated by servers, is cached by proxies, and most "
                    "WordPress form handlers only read $_POST, so fields arrive "
                    "empty."),
                fix_notes='Change the form tag to method="post".',
            ))

        nonce_present = any(
            "nonce" in ((i.get("name") or "") + (i.get("id") or "")).lower()
            for i in form.find_all("input")
        )
        if not nonce_present:
            findings.append(Finding(
                fid="missing_nonce", severity="critical", title="No security nonce field in the form",
                evidence="No input whose name or id contains 'nonce' was found.",
                explanation=(
                    "Handlers built with check_ajax_referer or wp_verify_nonce "
                    "reject nonce free requests with 403 or a bare -1. Bots also "
                    "hammer unprotected endpoints."),
                fix_php=FIX_PHP_AJAX, fix_js=FIX_JS_AJAX,
            ))
        else:
            findings.append(Finding(
                fid="nonce_cache_note", severity="info", title="Nonce present: verify it survives caching",
                evidence="A nonce style hidden input exists in the form.",
                explanation=(
                    "Nonces expire after 12 to 24 hours. A page cache that serves "
                    "older HTML ships an expired nonce, producing intermittent 403 "
                    "or -1 failures that vanish after a cache purge."),
                fix_php=FIX_PHP_NONCE_REFRESH, fix_js=FIX_JS_NONCE_REFRESH,
                fix_notes=FIX_NOTES_NONCE_CACHE,
            ))

        unnamed = [
            i for i in form.find_all(["input", "textarea", "select"])
            if not (i.get("name") or "").strip()
            and (i.get("type") or "").lower() not in ("submit", "button", "reset")
        ]
        if unnamed:
            findings.append(Finding(
                fid="inputs_unnamed", severity="critical",
                title=f"{len(unnamed)} field(s) missing a name attribute",
                evidence=_clip(", ".join(str(u)[:80] for u in unnamed[:3])),
                explanation=(
                    "Fields without a name are excluded from the submission "
                    "entirely, so the handler receives blanks and validation "
                    "fails, or empty emails arrive."),
                fix_notes='Add a unique name attribute to every field, for example <input type="text" name="name" id="cf-name">.',
            ))

        has_submit = bool(
            form.find("button")
            or form.find("input", attrs={"type": "submit"})
            or form.find("input", attrs={"type": "image"})
        )
        if not has_submit:
            findings.append(Finding(
                fid="no_submit", severity="warning", title="No submit control in the form",
                evidence="No <button> or <input type=\"submit\"> found inside the form.",
                explanation=(
                    "Visitors on mobile cannot submit at all, and Enter key "
                    "submission is inconsistent across browsers."),
                fix_notes='Add <button type="submit">Send</button> inside the form.',
            ))

        for b in form.find_all("button"):
            if not b.get("type"):
                findings.append(Finding(
                    fid="button_no_type", severity="info", title="Button without an explicit type",
                    evidence=_clip(str(b)[:120]),
                    explanation=(
                        "A typeless button defaults to submit. If it is meant to "
                        "toggle UI it will fire the form instead; if it is the real "
                        "submit, being explicit avoids double binding bugs."),
                    fix_notes='Declare the intent: type="submit" for the sender, type="button" for anything else.',
                ))
                break

        if form.find("input", attrs={"type": "file"}) and \
                (form.get("enctype") or "").lower() != "multipart/form-data":
            findings.append(Finding(
                fid="upload_no_enctype", severity="critical",
                title="File upload without multipart/form-data",
                evidence=f'enctype="{form.get("enctype") or "(none)"}" with a file input present',
                explanation=(
                    "Without the multipart enctype the browser sends only the file "
                    "name, never the bytes, so $_FILES arrives empty and uploads "
                    "silently fail."),
                fix_notes='Add enctype="multipart/form-data" to the form tag.',
            ))

    ids = [t.get("id") for t in soup.find_all(attrs={"id": True})]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        findings.append(Finding(
            fid="duplicate_ids", severity="warning",
            title=f"Duplicate id attribute(s): {', '.join(dupes[:5])}",
            evidence=f"ids repeated in the markup: {', '.join(dupes[:5])}",
            explanation=(
                "getElementById and jQuery # selectors bind to the first match "
                "only, so a second form or field with the same id never gets its "
                "handlers. Common after duplicating a section in a page builder."),
            fix_notes="Rename the duplicates so every id on the page is unique.",
        ))

    lowered = html.lower()
    if "recaptcha" in lowered or "grecaptcha" in lowered:
        findings.append(Finding(
            fid="recaptcha_note", severity="info", title="reCAPTCHA detected: verify server side",
            evidence="reCAPTCHA script or widget markup present.",
            explanation=(
                "The widget alone stops nothing: the token must be verified "
                "server side against https://www.google.com/recaptcha/api/siteverify "
                "inside the PHP handler, and an expired token (over 2 minutes old) "
                "must produce a readable error, not a silent drop."),
        ))

    for plugin_id, needle, name, note in (
        ("cf7_detected", "wpcf7", "Contact Form 7",
         "Check the on screen response class after submit: 'wpcf7-mail-sent-ng' means wp_mail failed (fix delivery via SMTP), 'wpcf7-spam-blocked' means the spam filter ate it, and a spinner that never resolves means the wpcf7 AJAX script did not load (plugin or jQuery conflict)."),
        ("wpforms_detected", "wpforms", "WPForms",
         "Check WPForms, Tools, Logs and enable email logging; delivery failures are almost always the server mail path, fixed by SMTP below."),
        ("gform_detected", "gform_", "Gravity Forms",
         "Check Forms, System Status, and the gform_after_submission entries; if entries save but no mail arrives the mail path is at fault, fixed by SMTP below."),
        ("elementor_detected", "elementor-form", "Elementor form widget",
         "Elementor posts to admin-ajax with its own nonce; a cached page serving an old nonce is the top cause of its 'server error' toast."),
    ):
        if needle in lowered:
            findings.append(Finding(
                fid=plugin_id, severity="info", title=f"{name} markup detected",
                evidence=f"Marker '{needle}' present in the markup.",
                explanation=note,
            ))

    _script_checks(findings, scripts_text, html)
    return _sorted(findings)


def _script_checks(findings: list[Finding], scripts_text: str, full_html: str) -> None:
    uses_dollar = bool(re.search(r"(?<![\w$])\$\s*\(", scripts_text))
    wrapped = bool(
        re.search(r"jQuery\s*\(\s*function\s*\(\s*\$", scripts_text)
        or re.search(r"\(\s*function\s*\(\s*\$\s*\)", scripts_text)
        or re.search(r"jQuery\s*\(\s*document\s*\)\s*\.ready\s*\(\s*function\s*\(\s*\$", scripts_text)
    )
    if uses_dollar and not wrapped:
        findings.append(Finding(
            fid="jquery_dollar", severity="critical",
            title="Inline script uses $ without a noConflict safe wrapper",
            evidence=_clip(next((l.strip() for l in scripts_text.splitlines() if "$(" in l), "$(...)")),
            explanation=(
                "WordPress loads jQuery in noConflict mode, so $ is undefined at "
                "global scope. The script throws '$ is not defined', the submit "
                "handler never binds, and the form either reloads the page or does "
                "nothing. This is the most common AJAX script breakage."),
            fix_js=FIX_JS_AJAX,
            fix_notes="Wrap the code in jQuery(function ($) { ... }); as shown, and enqueue it with array('jquery') as a dependency so load order is guaranteed.",
        ))

    if re.search(r"""['"]/?wp-admin/admin-ajax\.php['"]""", scripts_text) and \
            "ajaxurl" not in scripts_text and "ajaxUrl" not in scripts_text and "ajax_url" not in scripts_text:
        findings.append(Finding(
            fid="ajax_hardcoded_url", severity="warning",
            title="admin-ajax.php URL hardcoded in JavaScript",
            evidence="A string literal '/wp-admin/admin-ajax.php' appears in an inline script.",
            explanation=(
                "A hardcoded path breaks on subdirectory installs, multisite, and "
                "behind some security plugins that relocate wp-admin. The URL "
                "should come from wp_localize_script."),
            fix_php=FIX_PHP_AJAX, fix_js=FIX_JS_AJAX,
        ))


def _clip(text: str, limit: int = 220) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _sorted(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.fid))


def severity_counts(findings: list[Finding]) -> dict[str, int]:
    counts = {"critical": 0, "warning": 0, "info": 0}
    for f in findings:
        counts[f.severity] += 1
    return counts


# ---------------------------------------------------------------------------
# Known issue triage library
# ---------------------------------------------------------------------------

@dataclass
class KnownIssue:
    key: str
    title: str
    symptoms: str
    diagnosis: str
    fix_php: str = ""
    fix_js: str = ""
    fix_wpconfig: str = ""
    fix_notes: str = ""


KNOWN_ISSUES: dict[str, KnownIssue] = {i.key: i for i in [
    KnownIssue(
        key="ajax_breakage",
        title="AJAX script breakage (admin-ajax returns 400, 0 or nothing)",
        symptoms=(
            "The submit button spins forever or the page reloads; the browser "
            "console shows POST admin-ajax.php 400 (Bad Request), a bare 0 "
            "response, or '$ is not defined'."),
        diagnosis=(
            "One of three wiring faults: the request carries no action parameter "
            "so admin-ajax cannot route it; the handler is registered only for "
            "logged in users (wp_ajax_ without wp_ajax_nopriv_), so visitors get "
            "0 or 400; or the script itself crashed before binding because it "
            "used $ under noConflict or loaded before jQuery."),
        fix_php=FIX_PHP_AJAX, fix_js=FIX_JS_AJAX,
        fix_notes=(
            "Verify order: open DevTools, Network tab, submit once. No request "
            "at all means the JS crashed (check Console). A request with 400 "
            "means action or nonce is missing from the POST body. A request "
            "with 0 means no handler matched the action name."),
    ),
    KnownIssue(
        key="expired_nonce",
        title="Expired security nonce (403 or -1 responses)",
        symptoms=(
            "Submissions fail with HTTP 403, a bare -1, or "
            "rest_cookie_invalid_nonce, especially for visitors who kept the "
            "tab open a while, and the problem disappears right after clearing "
            "the site cache."),
        diagnosis=(
            "WordPress nonces are valid 12 to 24 hours. A page cache (WP Rocket, "
            "W3 Total Cache, LiteSpeed, Cloudflare cache everything) serves HTML "
            "older than that, so the embedded nonce is already dead on arrival."),
        fix_php=FIX_PHP_NONCE_REFRESH, fix_js=FIX_JS_NONCE_REFRESH,
        fix_notes=FIX_NOTES_NONCE_CACHE,
    ),
    KnownIssue(
        key="mail_dropoff",
        title="Unauthenticated PHP mail drop off (wp_mail true, nothing arrives)",
        symptoms=(
            "The form reports success and wp_mail() returns true, but the "
            "message never reaches the inbox, or lands in spam, or only fails "
            "for Gmail and Outlook recipients."),
        diagnosis=(
            "By default wp_mail() uses PHP mail() with no authentication: the "
            "web server injects the message into its local MTA and reports "
            "success at hand off, not delivery. Receiving servers see "
            "unauthenticated mail from a shared IP with no SPF, DKIM or DMARC "
            "alignment and reject or junk it silently."),
        fix_php=smtp_php_fix(),
        fix_wpconfig=smtp_wpconfig_fix("smtp.example.com", 587, "STARTTLS",
                                       "postmaster@example.com", "",
                                       "postmaster@example.com", "Website",
                                       redact_password=False),
        fix_notes=(
            "Use the SMTP Delivery Check tab to validate host, port and "
            "encryption for your provider and to generate this snippet with "
            "your real values filled in. Then publish SPF, DKIM and DMARC DNS "
            "records for the sending domain; without them even authenticated "
            "mail degrades to spam."),
    ),
    KnownIssue(
        key="smtp_auth_fail",
        title="SMTP authentication failure",
        symptoms=(
            "debug.log or the mail plugin shows 'SMTP Error: Could not "
            "authenticate', 535 5.7.8, or 'Username and Password not accepted'."),
        diagnosis=(
            "Wrong credential type or wrong port and encryption pairing. Gmail "
            "and Google Workspace refuse account passwords on SMTP: an App "
            "Password (with 2 step verification on) is required. Office 365 "
            "requires SMTP AUTH enabled per mailbox. SendGrid authenticates "
            "with the literal username 'apikey' plus the key as password."),
        fix_notes=(
            "Correct pairings: port 587 with STARTTLS, or port 465 with "
            "implicit SSL. Gmail: smtp.gmail.com. Office 365: "
            "smtp.office365.com, port 587. SendGrid: smtp.sendgrid.net, user "
            "'apikey'. Amazon SES: email-smtp.REGION.amazonaws.com with SMTP "
            "credentials, not IAM keys. Validate any combination in the SMTP "
            "Delivery Check tab before touching the server."),
    ),
    KnownIssue(
        key="jquery_breakage",
        title="jQuery conflict after a theme or WordPress update",
        symptoms=(
            "Console shows '$ is not defined' or 'jQuery is not defined'; every "
            "jQuery dependent form on the site stopped at the same time."),
        diagnosis=(
            "Either a script uses $ at global scope under noConflict mode, or "
            "it loads before jQuery because it was printed in the template "
            "instead of enqueued with a dependency, or a performance plugin "
            "defers jQuery below the dependent script."),
        fix_php=FIX_PHP_AJAX, fix_js=FIX_JS_AJAX,
        fix_notes=(
            "In the performance plugin, exclude jquery-core and the form "
            "script from defer and delay until the enqueue order fix ships."),
    ),
    KnownIssue(
        key="rest_blocked",
        title="REST API blocked (block editor and modern form plugins fail)",
        symptoms=(
            "Forms built on the REST API return 401 or 403 to /wp-json/ "
            "requests; the browser shows rest_cookie_invalid_nonce or "
            "rest_forbidden."),
        diagnosis=(
            "A security plugin or custom snippet disables REST for logged out "
            "users, which also kills legitimate public form endpoints."),
        fix_php=php_fragment(r"""/* functions.php: allow the specific public form route while keeping the rest locked. */
add_filter( 'rest_authentication_errors', function ( $result ) {
    if ( ! empty( $result ) ) {
        $route = $_SERVER['REQUEST_URI'] ?? '';
        if ( false !== strpos( $route, '/wp-json/contact-form-7/' ) ) {
            return null; // permit this namespace for visitors
        }
    }
    return $result;
} );
"""),
        fix_notes=(
            "Prefer re enabling REST in the security plugin's own settings "
            "(Wordfence: Firewall options; iThemes: WordPress Tweaks, REST "
            "API) and restrict by namespace instead of a blanket block."),
    ),
    KnownIssue(
        key="port25_blocked",
        title="Outbound port 25 blocked by the host",
        symptoms=(
            "PHP mail() and unencrypted SMTP both time out; debug.log shows "
            "'SMTP connect() failed' with no auth error at all."),
        diagnosis=(
            "Nearly all cloud and shared hosts (AWS, Google Cloud, Azure, "
            "DigitalOcean, most cPanel hosts) block outbound port 25 to stop "
            "spam. Nothing on port 25 will ever leave the box."),
        fix_notes=(
            "Switch to authenticated submission ports: 587 with STARTTLS or "
            "465 with SSL, using the SMTP fix from the mail drop off issue. "
            "Port 2525 is a common fallback where 587 is filtered."),
    ),
    KnownIssue(
        key="spf_dkim",
        title="Mail delivered to spam (SPF, DKIM, DMARC missing)",
        symptoms=(
            "Messages arrive but only in the spam folder; Gmail flags 'via' "
            "another domain or 'be careful with this message'."),
        diagnosis=(
            "The sending domain publishes no SPF or DKIM, or the From address "
            "domain does not align with the authenticated domain, so DMARC "
            "fails and providers junk the mail."),
        fix_notes=(
            "Publish three DNS TXT records on the sending domain. SPF at the "
            "root: v=spf1 include:PROVIDER-SPF ~all (one SPF record only; "
            "merge includes). DKIM at selector._domainkey with the key your "
            "SMTP provider issues. DMARC at _dmarc: v=DMARC1; p=quarantine; "
            "rua=mailto:dmarc@yourdomain. Keep the From address on the same "
            "domain you authenticate."),
    ),
    KnownIssue(
        key="plugin_conflict",
        title="Form broke right after a plugin or theme update",
        symptoms=(
            "The form worked until an update; now submissions error or the "
            "form does not render, with a JavaScript error in the console."),
        diagnosis=(
            "Two plugins loading conflicting library versions, or the update "
            "changed a hook the form depended on. The console error names the "
            "file at fault, which names the plugin."),
        fix_notes=(
            "Binary search on a staging copy: deactivate half the plugins, "
            "test, keep halving until the pair that conflicts is isolated. "
            "Then pin or replace the loser and report the conflict upstream. "
            "Never debug by deactivation on the live site during business "
            "hours."),
    ),
]}


# ---------------------------------------------------------------------------
# SMTP delivery simulation
# ---------------------------------------------------------------------------

PROVIDERS = {
    "Gmail / Google Workspace": {
        "host": "smtp.gmail.com", "ports": {587: "STARTTLS", 465: "SSL"},
        "note": "Requires a Google App Password (2 step verification on); account passwords are refused.",
    },
    "Microsoft 365 / Outlook": {
        "host": "smtp.office365.com", "ports": {587: "STARTTLS"},
        "note": "SMTP AUTH must be enabled for the mailbox in the Microsoft 365 admin center.",
    },
    "SendGrid": {
        "host": "smtp.sendgrid.net", "ports": {587: "STARTTLS", 465: "SSL", 2525: "STARTTLS"},
        "note": "Username is the literal string apikey; the password is the API key itself.",
    },
    "Mailgun": {
        "host": "smtp.mailgun.org", "ports": {587: "STARTTLS", 465: "SSL", 2525: "STARTTLS"},
        "note": "Use the SMTP credentials from the sending domain page, not the private API key.",
    },
    "Amazon SES": {
        "host": "email-smtp.us-east-1.amazonaws.com", "ports": {587: "STARTTLS", 465: "SSL", 2587: "STARTTLS"},
        "note": "Use SES SMTP credentials (not IAM keys) and verify the sending domain first.",
    },
    "Brevo (Sendinblue)": {
        "host": "smtp-relay.brevo.com", "ports": {587: "STARTTLS", 465: "SSL", 2525: "STARTTLS"},
        "note": "Username is the account login email; password is the SMTP key from the SMTP and API page.",
    },
    "Custom / other": {"host": "", "ports": {}, "note": ""},
}

OK, WARN, FAIL, SKIP = "pass", "warn", "fail", "skip"


@dataclass
class SmtpResult:
    stages: list[tuple[str, str, str]] = field(default_factory=list)
    verdict: str = ""
    verdict_level: str = OK
    recommendations: list[str] = field(default_factory=list)
    fix_php: str = ""
    # fix_wpconfig carries the live password and is for the operator's screen
    # only. Anything that gets shared, downloaded or attached to a ticket must
    # use fix_wpconfig_redacted instead.
    fix_wpconfig: str = ""
    fix_wpconfig_redacted: str = ""


def simulate_smtp(provider: str, host: str, port: int, encryption: str,
                  username: str, password: str, from_addr: str,
                  from_name: str = "Website") -> SmtpResult:
    """Deterministic handshake simulation. No live connection is made:
    every stage is evaluated against provider rules and RFC port and
    encryption pairings, which is what a real handshake would enforce."""
    r = SmtpResult()
    failed = False
    profile = PROVIDERS.get(provider, PROVIDERS["Custom / other"])

    # Stage 1: host sanity
    if not host.strip():
        r.stages.append(("Resolve SMTP host", FAIL, "No host given: nothing to connect to."))
        failed = True
    elif profile["host"] and host.strip().lower() != profile["host"] and provider != "Amazon SES":
        r.stages.append(("Resolve SMTP host", WARN,
                         f"Host '{host}' differs from {provider}'s documented server "
                         f"'{profile['host']}'; the connection would likely time out or hit the wrong relay."))
        r.recommendations.append(f"Use host {profile['host']} for {provider}.")
    else:
        r.stages.append(("Resolve SMTP host", OK, f"MX and A lookups for {host} assumed resolvable."))

    # Stage 2: TCP connect / port policy
    if failed:
        r.stages.append(("TCP connect", SKIP, "Skipped: no host."))
    elif port == 25:
        r.stages.append(("TCP connect", FAIL,
                         "Port 25 is blocked outbound by nearly every cloud and shared host; "
                         "the connection would hang and time out."))
        r.recommendations.append("Move to port 587 with STARTTLS (or 465 with SSL).")
        failed = True
    elif profile["ports"] and port not in profile["ports"]:
        r.stages.append(("TCP connect", WARN,
                         f"Port {port} is not a documented {provider} submission port "
                         f"({', '.join(str(p) for p in profile['ports'])}); connection would likely be refused."))
        r.recommendations.append(
            f"Use one of {provider}'s ports: {', '.join(str(p) for p in profile['ports'])}.")
    else:
        r.stages.append(("TCP connect", OK, f"220 {host or 'server'} ESMTP service ready (simulated)."))

    # Stage 3: TLS negotiation
    if failed:
        r.stages.append(("TLS negotiation", SKIP, "Skipped: no connection."))
    else:
        expected = profile["ports"].get(port) if profile["ports"] else None
        if port == 465 and encryption != "SSL":
            r.stages.append(("TLS negotiation", FAIL,
                             "Port 465 expects implicit SSL from the first byte; a plaintext or "
                             "STARTTLS client on 465 reads no banner and PHPMailer reports "
                             "'SMTP connect() failed'."))
            r.recommendations.append("Set encryption to SSL for port 465, or move to 587 with STARTTLS.")
            failed = True
        elif port in (587, 2525, 2587) and encryption == "SSL":
            r.stages.append(("TLS negotiation", FAIL,
                             "Port 587 style submission starts in plaintext and upgrades via "
                             "STARTTLS; an implicit SSL client here fails the wrapper handshake."))
            r.recommendations.append("Set encryption to STARTTLS (PHPMailer 'tls') for this port.")
            failed = True
        elif encryption == "None":
            r.stages.append(("TLS negotiation", WARN,
                             "No encryption selected: modern relays refuse AUTH over plaintext, "
                             "and credentials would cross the wire readable."))
            r.recommendations.append("Enable STARTTLS on 587 or SSL on 465.")
        elif expected and encryption != expected:
            r.stages.append(("TLS negotiation", WARN,
                             f"{provider} documents {expected} on port {port}; '{encryption}' may be refused."))
            r.recommendations.append(f"Use {expected} on port {port} for {provider}.")
        else:
            label = "STARTTLS upgrade completed" if encryption == "STARTTLS" else "implicit SSL session established"
            r.stages.append(("TLS negotiation", OK, f"{label} (simulated)."))

    # Stage 4: AUTH
    if failed:
        r.stages.append(("AUTH LOGIN", SKIP, "Skipped: no secure channel."))
    elif not username.strip() or not password.strip():
        r.stages.append(("AUTH LOGIN", FAIL,
                         "530 5.7.0 Authentication required: without credentials the relay "
                         "refuses MAIL FROM. This is exactly how unauthenticated PHP mail "
                         "drop offs look from the receiving side."))
        r.recommendations.append("Supply the SMTP username and password (or provider API key).")
        failed = True
    else:
        if provider == "Gmail / Google Workspace" and len(password.replace(" ", "")) != 16:
            r.stages.append(("AUTH LOGIN", WARN,
                             "Gmail rejects normal account passwords over SMTP (535 5.7.8). "
                             "The password given does not look like a 16 character App Password."))
            r.recommendations.append(
                "Create an App Password: Google Account, Security, 2 Step Verification, App passwords.")
        elif provider == "SendGrid" and username.strip().lower() != "apikey":
            r.stages.append(("AUTH LOGIN", WARN,
                             "SendGrid authenticates with the literal username 'apikey'; "
                             f"'{username}' would be refused with 535."))
            r.recommendations.append("Set the SMTP username to exactly: apikey")
        else:
            r.stages.append(("AUTH LOGIN", OK, "235 2.7.0 Authentication successful (simulated)."))

    # Stage 5: envelope
    if failed:
        r.stages.append(("MAIL FROM / RCPT TO", SKIP, "Skipped: not authenticated."))
    else:
        from_domain = from_addr.split("@")[-1].lower() if "@" in from_addr else ""
        user_domain = username.split("@")[-1].lower() if "@" in username else ""
        if not from_addr or "@" not in from_addr:
            r.stages.append(("MAIL FROM / RCPT TO", FAIL,
                             "The From address is empty or invalid; the relay answers "
                             "501 5.1.7 bad sender address."))
            r.recommendations.append("Set a real From address on the authenticated domain.")
            failed = True
        elif user_domain and from_domain and from_domain != user_domain and \
                provider in ("Gmail / Google Workspace", "Microsoft 365 / Outlook"):
            r.stages.append(("MAIL FROM / RCPT TO", WARN,
                             f"From domain '{from_domain}' differs from the authenticated domain "
                             f"'{user_domain}': {provider} rewrites or refuses mismatched senders, "
                             "and DMARC alignment fails at the recipient."))
            r.recommendations.append("Send from the same domain you authenticate, or add the address as a verified alias.")
        else:
            r.stages.append(("MAIL FROM / RCPT TO", OK, "250 2.1.0 sender ok, 250 2.1.5 recipient ok (simulated)."))

    # Stage 6: delivery
    if failed:
        r.stages.append(("DATA / queue", SKIP, "Skipped: envelope rejected."))
        r.verdict_level = FAIL
        first_fail = next(s for s in r.stages if s[1] == FAIL)
        r.verdict = f"Delivery would FAIL at stage: {first_fail[0]}. {first_fail[2]}"
    else:
        r.stages.append(("DATA / queue", OK, "250 2.0.0 message accepted for delivery (simulated)."))
        warns = [s for s in r.stages if s[1] == WARN]
        if warns:
            r.verdict_level = WARN
            r.verdict = (f"Handshake would complete, with {len(warns)} warning(s) that "
                         "commonly break real delivery. Apply the recommendations, then re run.")
        else:
            r.verdict_level = OK
            r.verdict = ("Configuration is consistent: handshake, authentication and envelope "
                         "all pass. Apply the generated fix below, then send a live test from "
                         "WordPress and confirm receipt at an external inbox.")

    r.recommendations.append(
        "After going live, publish SPF, DKIM and DMARC records for the sending "
        "domain so authenticated mail also lands in the inbox, not spam.")
    if PROVIDERS.get(provider, {}).get("note"):
        r.recommendations.append(PROVIDERS[provider]["note"])

    r.fix_php = smtp_php_fix()
    r.fix_wpconfig = smtp_wpconfig_fix(host, port, encryption, username, password,
                                       from_addr or username, from_name,
                                       redact_password=False)
    r.fix_wpconfig_redacted = smtp_wpconfig_fix(host, port, encryption, username,
                                                password, from_addr or username,
                                                from_name, redact_password=True)
    return r


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def build_report(findings: list[Finding] | None,
                 issues: list[KnownIssue] | None,
                 smtp: SmtpResult | None,
                 site_label: str = "") -> str:
    lines = ["# WP Form Debugger: diagnostic report", ""]
    if site_label:
        lines += [f"**Site:** {site_label}", ""]
    lines += [f"_Engine version {ENGINE_VERSION}. Generated by WP Form Debugger._", ""]

    if findings is not None:
        counts = severity_counts(findings)
        lines += ["## Form HTML audit",
                  f"{counts['critical']} critical, {counts['warning']} warning(s), "
                  f"{counts['info']} informational.", ""]
        for f in findings:
            lines += [f"### [{SEVERITY_LABEL[f.severity]}] {f.title}",
                      f"**Evidence:** {f.evidence}", "", f.explanation, ""]
            if f.fix_php:
                lines += ["**PHP fix (functions.php):**", "```php", f.fix_php.strip(), "```", ""]
            if f.fix_js:
                lines += ["**JavaScript fix:**", "```javascript", f.fix_js.strip(), "```", ""]
            if f.fix_wpconfig:
                lines += ["**wp-config.php fix:**", "```php", f.fix_wpconfig.strip(), "```", ""]
            if f.fix_notes:
                lines += [f"**Notes:** {f.fix_notes}", ""]

    if issues:
        lines += ["## Known issue triage", ""]
        for i in issues:
            lines += [f"### {i.title}",
                      f"**Symptoms:** {i.symptoms}", "",
                      f"**Diagnosis:** {i.diagnosis}", ""]
            if i.fix_php:
                lines += ["**PHP fix (functions.php):**", "```php", i.fix_php.strip(), "```", ""]
            if i.fix_js:
                lines += ["**JavaScript fix:**", "```javascript", i.fix_js.strip(), "```", ""]
            if i.fix_wpconfig:
                lines += ["**wp-config.php fix:**", "```php", i.fix_wpconfig.strip(), "```", ""]
            if i.fix_notes:
                lines += [f"**Notes:** {i.fix_notes}", ""]

    if smtp is not None:
        lines += ["## SMTP delivery verification (simulated handshake)", ""]
        for name, status, detail in smtp.stages:
            lines += [f"- **{name}** [{status.upper()}]: {detail}"]
        lines += ["", f"**Verdict:** {smtp.verdict}", ""]
        if smtp.recommendations:
            lines += ["**Recommendations:**"] + [f"- {rec}" for rec in smtp.recommendations] + [""]
        lines += ["**PHP fix (functions.php):**", "```php", smtp.fix_php.strip(), "```", "",
                  "**wp-config.php fix:**",
                  "The SMTP password is redacted here so this report is safe to "
                  "share or attach to a ticket. Copy the real value from the "
                  "SMTP Delivery Check tab straight into wp-config.php.",
                  "```php", (smtp.fix_wpconfig_redacted or smtp.fix_wpconfig).strip(),
                  "```", ""]

    return "\n".join(lines)
