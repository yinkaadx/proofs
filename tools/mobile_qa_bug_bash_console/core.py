"""Mobile QA bug bash engine.

A bug report is only worth writing if a developer can reproduce it without
asking a question. Most cannot, and the missing piece is almost never the
description: it is the build number, the exact device, or the precondition the
reporter did not realise they had set up an hour earlier.

So this engine treats a report as a structured record with required fields
rather than as prose, scores it on what is actually present, and refuses to
call a report complete when the parts that make it reproducible are missing.

Pure logic, no Streamlit import, so this is unit testable on its own and could
sit behind a real Asana or Slack integration. Deterministic: nothing reads the
clock or a random source, so the same bug produces the same payload every time
and a test and a run agree.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Severity, shared by every part
# ---------------------------------------------------------------------------

SEV_BLOCKER = "S1 Blocker"
SEV_CRITICAL = "S2 Critical"
SEV_MAJOR = "S3 Major"
SEV_MINOR = "S4 Minor"

SEVERITIES: tuple = (SEV_BLOCKER, SEV_CRITICAL, SEV_MAJOR, SEV_MINOR)

# Rank is what makes a triage list sortable. Lower is worse, so a plain sort
# puts the thing that stops the release at the top.
SEVERITY_RANK = {name: index + 1 for index, name in enumerate(SEVERITIES)}

SEVERITY_TONE = {SEV_BLOCKER: "crit", SEV_CRITICAL: "crit",
                 SEV_MAJOR: "warn", SEV_MINOR: "ok"}

# What each severity promises about response, so a label means something
# operationally rather than being a feeling about how bad it is.
SEVERITY_SLA_HOURS = {SEV_BLOCKER: 4, SEV_CRITICAL: 24, SEV_MAJOR: 72,
                      SEV_MINOR: 336}

FREQ_ALWAYS = "Always, 5 of 5"
FREQ_OFTEN = "Often, 3 of 5"
FREQ_RARE = "Rare, 1 of 5"
FREQUENCIES: tuple = (FREQ_ALWAYS, FREQ_OFTEN, FREQ_RARE)


def severity_tone(severity: str) -> str:
    return SEVERITY_TONE.get(severity, "warn")


def severity_rank(severity: str) -> int:
    """Unknown severities sort last rather than crashing a triage board."""
    return SEVERITY_RANK.get(severity, len(SEVERITIES) + 1)


# ---------------------------------------------------------------------------
# Part one: the bug report
# ---------------------------------------------------------------------------

# The fields without which a developer has to come back and ask. Keeping this
# list separate from the report is the point: it is the contract a report is
# measured against, not a description of one particular report.
REQUIRED_FIELDS: tuple = (
    "Title", "Severity", "Frequency", "Device", "OS Version", "Build Number",
    "Preconditions", "Steps to Reproduce", "Expected Behavior",
    "Actual Behavior", "Log Snippet",
)

# The three that most often decide whether a bug can be reproduced at all. A
# report missing any of these is not merely incomplete, it is unactionable:
# without them the developer cannot even set up the same conditions.
REPRODUCIBILITY_FIELDS: tuple = ("Device", "OS Version", "Build Number")


def get_sample_bug_report() -> dict:
    """A complete report, of the shape a developer can act on unassisted.

    Written as the worked example a bug bash hands to people who have never
    filed one, so every field carries a real value rather than a placeholder
    telling them what to put there.
    """
    return {
        "Title": ("Checkout crashes on Pay Now when a saved card is the only "
                  "payment method"),
        "Severity": SEV_BLOCKER,
        "Frequency": FREQ_ALWAYS,
        "Device": "Google Pixel 8 Pro",
        "OS Version": "Android 15 (API 35)",
        "Build Number": "4.7.2 (build 2841), release candidate",
        "Preconditions": [
            "Signed in as a returning customer with an active account.",
            "Exactly one saved card on file, and no other payment method.",
            "Basket holds at least one item and delivery is already chosen.",
            "Device language set to English, region United Kingdom.",
        ],
        "Steps to Reproduce": [
            "Open the app and go to the basket.",
            "Tap Checkout.",
            "Leave the saved card selected without editing it.",
            "Tap Pay Now.",
        ],
        "Expected Behavior": (
            "The payment is submitted, a spinner shows while it is "
            "authorised, and the order confirmation screen appears with an "
            "order number."),
        "Actual Behavior": (
            "The app closes immediately on Pay Now with no dialog. Reopening "
            "it lands on the home screen with the basket intact and no order "
            "placed. Reproduced 5 times out of 5."),
        "Log Snippet": (
            "FATAL EXCEPTION: main\n"
            "Process: com.example.shop, PID: 14882\n"
            "java.lang.NullPointerException: Attempt to invoke virtual method\n"
            "  'java.lang.String com.example.shop.payments.Card.getToken()'\n"
            "  on a null object reference\n"
            "    at com.example.shop.checkout.PayNowHandler"
            ".submit(PayNowHandler.kt:142)\n"
            "    at com.example.shop.checkout.CheckoutFragment"
            ".onPayClicked(CheckoutFragment.kt:88)"),
    }


@dataclass
class ReportQuality:
    """How close a report is to being actionable, and what is missing."""
    present: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    empty: list = field(default_factory=list)

    @property
    def score(self) -> float:
        total = len(REQUIRED_FIELDS)
        return round(len(self.present) / total, 4) if total else 0.0

    @property
    def reproducible(self) -> bool:
        """Whether a developer could set up the same conditions at all."""
        gaps = set(self.missing) | set(self.empty)
        return not gaps.intersection(REPRODUCIBILITY_FIELDS)

    @property
    def complete(self) -> bool:
        return not self.missing and not self.empty

    @property
    def verdict(self) -> str:
        if self.complete:
            return "Ready to assign"
        if not self.reproducible:
            return "Cannot be reproduced, send it back"
        return "Actionable with gaps"

    def rows(self) -> list:
        return [
            {
                "Field": name,
                "Status": ("present" if name in self.present
                           else "empty" if name in self.empty else "missing"),
                "Blocks reproduction":
                    "yes" if name in REPRODUCIBILITY_FIELDS else "no",
            }
            for name in REQUIRED_FIELDS
        ]


def assess_report(report: dict | None = None) -> ReportQuality:
    """Score a report against the required fields.

    An empty string counts as missing rather than present. A field somebody
    tabbed through without filling in is exactly as useless as one that was
    never on the form, and scoring it as present is how a template comes to be
    described as complete while nobody can reproduce anything.
    """
    data = get_sample_bug_report() if report is None else dict(report or {})
    quality = ReportQuality()
    for name in REQUIRED_FIELDS:
        if name not in data:
            quality.missing.append(name)
        elif not data[name] or (isinstance(data[name], str)
                                and not data[name].strip()):
            quality.empty.append(name)
        else:
            quality.present.append(name)
    return quality


def report_lines(report: dict | None = None) -> str:
    """The report as a developer would paste it into a ticket."""
    data = get_sample_bug_report() if report is None else report
    parts: list = []
    for name in REQUIRED_FIELDS:
        value = data.get(name, "")
        if isinstance(value, (list, tuple)):
            body = "\n".join(f"  {index}. {item}"
                             for index, item in enumerate(value, start=1))
        else:
            body = "\n".join(f"  {line}" for line in str(value).splitlines())
        parts.append(f"{name}:\n{body}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Part two: reviews to test cases
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TestCase:
    case_id: str
    title: str
    severity: str
    steps: tuple

    @property
    def rank(self) -> int:
        return severity_rank(self.severity)


@dataclass(frozen=True)
class ReviewCategory:
    category: str
    example_review: str
    review_count: int
    average_stars: float
    test_cases: tuple

    @property
    def worst_severity(self) -> str:
        """The severity a triage board should read this category at."""
        return min(self.test_cases, key=lambda case: case.rank).severity

    @property
    def rank(self) -> int:
        return severity_rank(self.worst_severity)


def map_reviews_to_test_cases() -> list:
    """Turn what customers complain about into tests, ranked by severity.

    A one star review is a bug report written by somebody with no vocabulary
    for it, and the work is translating "it keeps logging me out" into the
    specific condition a tester can set up. Ranking by severity is what stops
    the loudest category rather than the worst one going to the top.
    """
    categories = [
        ReviewCategory(
            category="Crashes on payment",
            example_review=("Tried to pay three times, app just vanishes every "
                            "time. Gave up and ordered elsewhere."),
            review_count=48, average_stars=1.1,
            test_cases=(
                TestCase("TC-101", "Pay with a single saved card and no other "
                                   "method", SEV_BLOCKER,
                         ("Sign in as a returning customer.",
                          "Leave exactly one saved card on the account.",
                          "Complete a basket and tap Pay Now.",
                          "Confirm the order confirmation screen appears.")),
                TestCase("TC-102", "Pay after the saved card has expired",
                         SEV_CRITICAL,
                         ("Set the saved card expiry to a past month.",
                          "Tap Pay Now.",
                          "Confirm a clear message rather than a crash.")),
                TestCase("TC-103", "Pay with the network dropping mid request",
                         SEV_CRITICAL,
                         ("Enable airplane mode immediately after Pay Now.",
                          "Confirm the app recovers and the basket is kept.",
                          "Confirm no duplicate charge on reconnect.")),
            )),
        ReviewCategory(
            category="Logged out constantly",
            example_review=("Signs me out every single day, have to enter my "
                            "password again. Infuriating."),
            review_count=132, average_stars=1.6,
            test_cases=(
                TestCase("TC-201", "Session survives closing and reopening the "
                                   "app", SEV_CRITICAL,
                         ("Sign in and force close the app.",
                          "Reopen after ten minutes.",
                          "Confirm the session is still active.")),
                TestCase("TC-202", "Refresh token renews before it expires",
                         SEV_CRITICAL,
                         ("Sign in and leave the app idle past token expiry.",
                          "Return to the app.",
                          "Confirm it refreshes silently without a prompt.")),
                TestCase("TC-203", "Signing out on one device leaves the other "
                                   "signed in", SEV_MAJOR,
                         ("Sign in on two devices.",
                          "Sign out on one.",
                          "Confirm the second stays signed in.")),
            )),
        ReviewCategory(
            category="Slow and unresponsive",
            example_review=("Takes about ten seconds to open anything. My "
                            "phone is two years old, not ancient."),
            review_count=97, average_stars=2.3,
            test_cases=(
                TestCase("TC-301", "Cold start completes within budget on a "
                                   "mid range device", SEV_MAJOR,
                         ("Install fresh on a mid range handset.",
                          "Measure cold start to first interactive frame.",
                          "Confirm it is under three seconds.")),
                TestCase("TC-302", "A long list scrolls without dropping "
                                   "frames", SEV_MAJOR,
                         ("Load an order history of two hundred entries.",
                          "Scroll quickly to the end.",
                          "Confirm no frame takes longer than 16 ms.")),
            )),
        ReviewCategory(
            category="Notifications never arrive",
            example_review=("Never get a single delivery notification even "
                            "though they are turned on."),
            review_count=61, average_stars=2.0,
            test_cases=(
                TestCase("TC-401", "Delivery notification arrives with the app "
                                   "in the background", SEV_MAJOR,
                         ("Grant notification permission.",
                          "Background the app and trigger a delivery update.",
                          "Confirm the notification arrives.")),
                TestCase("TC-402", "Notification permission is requested with "
                                   "a reason, not on launch", SEV_MINOR,
                         ("Install fresh and open the app.",
                          "Confirm no permission prompt on first launch.",
                          "Confirm the prompt appears at the first order.")),
            )),
        ReviewCategory(
            category="Layout broken on small screens",
            example_review=("The Pay button is half off the screen on my "
                            "phone, I have to rotate to tap it."),
            review_count=23, average_stars=2.8,
            test_cases=(
                TestCase("TC-501", "Primary actions stay on screen at the "
                                   "smallest supported width", SEV_CRITICAL,
                         ("Set the device to the smallest supported width.",
                          "Open checkout.",
                          "Confirm Pay Now is fully visible and tappable.")),
                TestCase("TC-502", "Layout holds at the largest font size",
                         SEV_MAJOR,
                         ("Set the system font to its largest setting.",
                          "Open checkout.",
                          "Confirm nothing is clipped or overlapping.")),
            )),
    ]
    return sorted(categories, key=lambda entry: (entry.rank,
                                                 -entry.review_count))


def review_rows() -> list:
    return [
        {
            "Category": entry.category,
            "Reviews": str(entry.review_count),
            "Average stars": f"{entry.average_stars:.1f}",
            "Worst severity": entry.worst_severity,
            "Test cases": str(len(entry.test_cases)),
        }
        for entry in map_reviews_to_test_cases()
    ]


def all_test_cases() -> list:
    """Every case from every category, worst first."""
    cases = [case for entry in map_reviews_to_test_cases()
             for case in entry.test_cases]
    return sorted(cases, key=lambda case: (case.rank, case.case_id))


# ---------------------------------------------------------------------------
# Part three: the Asana and Slack dispatch
# ---------------------------------------------------------------------------

ASANA_PROJECT_GID = "1209887766554433"
ASANA_ENDPOINT = "POST https://app.asana.com/api/1.0/tasks"
SLACK_ENDPOINT = "POST https://hooks.slack.com/services/T000/B000/XXXX"
SLACK_CHANNEL = "#mobile-bug-bash"

# Who gets woken up. A severity that does not change who is notified is a
# severity nobody will bother setting correctly.
SEVERITY_ASSIGNEE = {
    SEV_BLOCKER: "release-captain",
    SEV_CRITICAL: "mobile-oncall",
    SEV_MAJOR: "mobile-triage",
    SEV_MINOR: "mobile-backlog",
}
NOTIFY_ON_CALL = (SEV_BLOCKER, SEV_CRITICAL)


@dataclass(frozen=True)
class Dispatch:
    bug_id: str
    title: str
    severity: str
    asana: dict
    slack: dict
    notified_on_call: bool

    @property
    def tone(self) -> str:
        return severity_tone(self.severity)

    def asana_json(self) -> str:
        return json.dumps(self.asana, indent=2)

    def slack_json(self) -> str:
        return json.dumps(self.slack, indent=2)


def simulate_asana_slack_dispatch(bug_id: str = "BUG-4821",
                                  title: str = "Checkout crashes on Pay Now",
                                  severity: str = SEV_BLOCKER) -> Dispatch:
    """Build the two payloads a triaged bug produces, ready to send.

    The identifier is in the Asana task name rather than only in a custom
    field, because the name is the only part that survives into a Slack
    notification, a search box and somebody reading it aloud on a call.
    """
    if severity not in SEVERITIES:
        raise ValueError(f"unknown severity {severity!r}")

    identifier = str(bug_id or "").strip() or "BUG-UNKNOWN"
    heading = str(title or "").strip() or "Untitled defect"
    assignee = SEVERITY_ASSIGNEE[severity]
    sla = SEVERITY_SLA_HOURS[severity]
    urgent = severity in NOTIFY_ON_CALL

    asana = {
        "data": {
            "name": f"[{identifier}] {heading}",
            "projects": [ASANA_PROJECT_GID],
            "assignee": assignee,
            "notes": (f"Severity {severity}. Response target {sla} hours.\n"
                      f"Filed from the bug bash console.\n"
                      f"Full report attached in the ticket description."),
            "custom_fields": {
                "severity": severity,
                "bug_id": identifier,
                "sla_hours": sla,
                "source": "bug-bash",
            },
        }
    }

    colour = {"crit": "#b91c1c", "warn": "#b45309", "ok": "#047857"}[
        severity_tone(severity)]
    slack = {
        "channel": SLACK_CHANNEL,
        "text": f"{severity}: {identifier} {heading}",
        "attachments": [
            {
                "color": colour,
                "fields": [
                    {"title": "Bug", "value": identifier, "short": True},
                    {"title": "Severity", "value": severity, "short": True},
                    {"title": "Assigned to", "value": assignee, "short": True},
                    {"title": "Response target", "value": f"{sla} hours",
                     "short": True},
                    {"title": "Summary", "value": heading, "short": False},
                ],
            }
        ],
        "notify_on_call": urgent,
    }
    return Dispatch(identifier, heading, severity, asana, slack, urgent)


def dispatch_rows(dispatch: Dispatch) -> list:
    return [
        {"Destination": "Asana", "Endpoint": ASANA_ENDPOINT,
         "Result": f"Task created and assigned to "
                   f"{dispatch.asana['data']['assignee']}"},
        {"Destination": "Slack", "Endpoint": SLACK_ENDPOINT,
         "Result": f"Posted to {SLACK_CHANNEL}"
                   + (", on call paged" if dispatch.notified_on_call else "")},
    ]


# ---------------------------------------------------------------------------
# Part four: the Figma gap auditor
# ---------------------------------------------------------------------------

GAP_MISSING = "Not built"
GAP_DIVERGED = "Built differently"
GAP_UNDESIGNED = "Built with no design"

GAP_SEVERITY = {GAP_MISSING: SEV_CRITICAL, GAP_DIVERGED: SEV_MAJOR,
                GAP_UNDESIGNED: SEV_MAJOR}


@dataclass(frozen=True)
class FigmaGap:
    screen: str
    frame: str
    kind: str
    detail: str

    @property
    def severity(self) -> str:
        return GAP_SEVERITY.get(self.kind, SEV_MINOR)

    @property
    def rank(self) -> int:
        return severity_rank(self.severity)


def audit_figma_gaps() -> list:
    """Where the build and the design file disagree.

    The third kind is the one design reviews miss. A screen built with no
    design at all never appears in a side by side comparison, because there is
    nothing to put beside it, so it is the state most likely to ship.
    """
    gaps = [
        FigmaGap("Checkout", "Checkout / Payment failed", GAP_MISSING,
                 "The designed error state does not exist in the build, so a "
                 "declined card shows a raw toast instead."),
        FigmaGap("Checkout", "Checkout / Pay Now button", GAP_DIVERGED,
                 "Built at 44 pt tall against 56 pt in the frame, which is "
                 "what puts it off screen at the smallest supported width."),
        FigmaGap("Order history", "Orders / Empty state", GAP_MISSING,
                 "A new account sees a blank list rather than the designed "
                 "first order prompt."),
        FigmaGap("Notifications", "Settings / Notification permission",
                 GAP_UNDESIGNED,
                 "The permission prompt was added in code with no frame, so "
                 "nobody reviewed when it fires or what it says."),
        FigmaGap("Sign in", "Auth / Session expired", GAP_UNDESIGNED,
                 "The forced sign in screen exists only in the build, which "
                 "is why its copy blames the customer."),
    ]
    return sorted(gaps, key=lambda gap: (gap.rank, gap.screen))


def figma_rows() -> list:
    return [
        {"Screen": gap.screen, "Frame": gap.frame, "Gap": gap.kind,
         "Severity": gap.severity, "Detail": gap.detail}
        for gap in audit_figma_gaps()
    ]


def bug_bash_summary() -> dict:
    """The board a bug bash actually runs from."""
    quality = assess_report()
    return {
        "report_score": quality.score,
        "report_complete": quality.complete,
        "categories": len(map_reviews_to_test_cases()),
        "test_cases": len(all_test_cases()),
        "blockers": len([c for c in all_test_cases()
                         if c.severity == SEV_BLOCKER]),
        "figma_gaps": len(audit_figma_gaps()),
    }
