"""HIPAA web and tracking audit engine for a therapy practice.

Educational simulator, not legal advice. It reproduces the reasoning a privacy
review applies and names the source for each conclusion, so a practice owner
can take the output to counsel rather than take it as counsel.

Pure logic, no Streamlit import, so this is unit testable on its own, and
deterministic: nothing reads the clock or a random source.

THE LEGAL POSITION THIS ENCODES, AND WHY THE DATE MATTERS

OCR published a bulletin on online tracking technologies in December 2022 and
revised it in March 2024. It said that individually identifiable health
information collected through tracking on a regulated entity's website is PHI,
and it extended that to unauthenticated public pages through what it called a
proscribed combination: an IP address plus a visit to a page about a health
condition.

On 20 June 2024 the United States District Court for the Northern District of
Texas, in American Hospital Association v. Becerra, held that OCR exceeded its
authority and vacated that proscribed combination nationwide. On 29 August 2024
OCR withdrew its appeal.

So the widely repeated claim that any analytics tag on any health website is an
OCR violation is no longer the law, and an auditor that still says so is giving
a practice owner outdated advice. What did NOT change is the part that matters
most to a therapy practice: on authenticated pages, and wherever a visitor
actually submits booking or intake details, the information is PHI and the
obligations are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

DISCLAIMER = (
    "Educational simulator, not legal advice. Every conclusion names its "
    "source so it can be checked with counsel before anything is changed."
)

BULLETIN = "OCR Bulletin on Online Tracking Technologies, December 2022, revised March 2024"
VACATUR = ("American Hospital Association v. Becerra, N.D. Tex., 20 June 2024, "
           "vacating the proscribed combination nationwide; OCR withdrew its "
           "appeal on 29 August 2024")

# ---------------------------------------------------------------------------
# Part one: Google Tag Manager exposure
# ---------------------------------------------------------------------------

PAGE_AUTHENTICATED = "Authenticated client portal"
PAGE_BOOKING = "Booking or intake form"
PAGE_CONDITION = "Public page about a condition"
PAGE_MARKETING = "General marketing page"

PAGE_TYPES: tuple[str, ...] = (PAGE_AUTHENTICATED, PAGE_BOOKING,
                               PAGE_CONDITION, PAGE_MARKETING)

RISK_CRITICAL = "Critical"
RISK_HIGH = "High"
RISK_MODERATE = "Moderate"
RISK_LOW = "Low"

TONE = {RISK_CRITICAL: "crit", RISK_HIGH: "crit", RISK_MODERATE: "warn",
        RISK_LOW: "ok"}


@dataclass(frozen=True)
class GtmExposure:
    page_type: str
    collects_booking_info: bool
    has_google_ads: bool
    risk: str
    ocr_violation: bool
    headline: str
    explanation: str
    authority: str
    remediation: tuple = ()

    @property
    def tone(self) -> str:
        return TONE.get(self.risk, "warn")

    def rows(self) -> list:
        return [
            {"Factor": "Page type", "Value": self.page_type},
            {"Factor": "Collects booking details",
             "Value": "yes" if self.collects_booking_info else "no"},
            {"Factor": "Google Ads tag present",
             "Value": "yes" if self.has_google_ads else "no"},
            {"Factor": "Risk level", "Value": self.risk},
            {"Factor": "Likely OCR violation",
             "Value": "yes" if self.ocr_violation else "no"},
        ]


def evaluate_gtm_exposure(page_type: str = PAGE_BOOKING,
                          collects_booking_info: bool = True,
                          has_google_ads: bool = True) -> GtmExposure:
    """Judge one page and say plainly whether it is an OCR problem.

    Two things drive the answer and they are kept apart on purpose. Whether the
    page is authenticated or takes booking details decides whether PHI is in
    play at all. Whether a Google Ads tag is on it decides whether that PHI
    leaves for a vendor who will not sign a business associate agreement, which
    is what turns an internal problem into a disclosure.
    """
    if page_type not in PAGE_TYPES:
        raise ValueError(f"unknown page type {page_type!r}")

    phi_in_play = page_type in (PAGE_AUTHENTICATED, PAGE_BOOKING) or collects_booking_info

    if phi_in_play and has_google_ads:
        return GtmExposure(
            page_type, collects_booking_info, has_google_ads,
            risk=RISK_CRITICAL, ocr_violation=True,
            headline="PHI is being disclosed to a vendor with no BAA",
            explanation=(
                "This page handles individually identifiable health "
                "information: a person identifying themselves while seeking "
                "therapy is PHI, and a booking or intake submission is that "
                "person doing exactly that. The Google Ads tag transmits the "
                "event, and with it the identifiers the tag carries, to "
                "Google. Google does not sign a business associate agreement "
                "for Google Ads or Google Analytics, so there is no lawful "
                "basis for the disclosure. This part of the bulletin was not "
                "touched by the 2024 vacatur, which reached only "
                "unauthenticated pages."),
            authority=f"{BULLETIN}. Unaffected by {VACATUR}.",
            remediation=(
                "Remove the Google Ads and Analytics tags from this page and "
                "from every page behind the login.",
                "Move conversion measurement to a server side endpoint you "
                "control, and send only a de identified event with no name, "
                "email, phone, IP or appointment reason.",
                "If the tag has been live, treat it as a potential breach and "
                "run the four factor risk assessment under 45 CFR 164.402.",
            ))

    if phi_in_play:
        return GtmExposure(
            page_type, collects_booking_info, has_google_ads,
            risk=RISK_HIGH, ocr_violation=True,
            headline="PHI is present, so every tag on this page is in scope",
            explanation=(
                "The page handles PHI, so any tracking technology on it is "
                "regulated whether or not a Google tag is among them today. "
                "The obligation attaches to the data, not to the vendor, so "
                "adding any third party tag here later would be a disclosure "
                "unless that vendor has signed a BAA."),
            authority=f"{BULLETIN}. Unaffected by {VACATUR}.",
            remediation=(
                "Keep this page free of third party tags as a standing rule, "
                "written into whoever maintains the site.",
                "Inventory every tag in the container and record, for each "
                "one, which pages it fires on.",
            ))

    if page_type == PAGE_CONDITION and has_google_ads:
        return GtmExposure(
            page_type, collects_booking_info, has_google_ads,
            risk=RISK_MODERATE, ocr_violation=False,
            headline="Not an OCR violation since June 2024, still a real risk",
            explanation=(
                "OCR's bulletin treated an IP address plus a visit to a page "
                "about a health condition as PHI. A federal court vacated that "
                "position nationwide in June 2024 and OCR withdrew its appeal "
                "that August, so this is no longer an OCR violation on its own "
                "and an auditor that still calls it one is out of date. It "
                "remains exposure: state privacy laws, the FTC Health Breach "
                "Notification Rule and plaintiffs' firms have all acted in "
                "this space independently of HIPAA."),
            authority=f"{VACATUR}, modifying {BULLETIN}.",
            remediation=(
                "Decide this on privacy and reputation rather than on HIPAA, "
                "and write down which you chose.",
                "If the tags stay, turn off IP collection and make sure no "
                "page URL or title names a condition.",
            ))

    return GtmExposure(
        page_type, collects_booking_info, has_google_ads,
        risk=RISK_LOW, ocr_violation=False,
        headline="No PHI in play on this page",
        explanation=(
            "A general marketing page that collects no booking or intake "
            "detail and sits outside the login carries no individually "
            "identifiable health information, so ordinary analytics here are "
            "not a HIPAA matter. The care needed is keeping it that way: the "
            "moment a form or a login appears, the answer changes."),
        authority=f"{VACATUR}, modifying {BULLETIN}.",
        remediation=(
            "Re run this audit whenever a form is added to a public page.",
        ))


# ---------------------------------------------------------------------------
# Part two: the EHR handoff
# ---------------------------------------------------------------------------

HANDOFF_QUERY_PARAMS = "URL query parameters"
HANDOFF_API_POST = "Direct API POST"
HANDOFF_METHODS: tuple[str, ...] = (HANDOFF_QUERY_PARAMS, HANDOFF_API_POST)

STATUS_INSECURE = "Insecure"
STATUS_SECURE = "Secure"


@dataclass(frozen=True)
class HandoffAssessment:
    method: str
    status: str
    safe: bool
    summary: str
    leaks: tuple = ()
    controls: tuple = ()
    example: str = ""

    @property
    def tone(self) -> str:
        return "ok" if self.safe else "crit"

    def rows(self) -> list:
        items = self.controls if self.safe else self.leaks
        label = "Control" if self.safe else "Where the data ends up"
        return [{label: item} for item in items]


def evaluate_ehr_handoff(method: str = HANDOFF_QUERY_PARAMS) -> HandoffAssessment:
    """Compare pre filling an intake by URL against posting it server to server.

    The difference is not encryption. Both are over TLS. The difference is that
    a query string is part of the address, and addresses are written down
    everywhere by default, while a request body is not.
    """
    if method not in HANDOFF_METHODS:
        raise ValueError(f"unknown handoff method {method!r}")

    if method == HANDOFF_QUERY_PARAMS:
        return HandoffAssessment(
            method=method, status=STATUS_INSECURE, safe=False,
            summary=(
                "Pre filling the intake by putting the client's details in the "
                "link discloses PHI to everything that records a URL. TLS does "
                "not help: the address is not the payload, it is the "
                "destination, and it is logged in the clear at both ends."),
            leaks=(
                "The web server access log of every host in the path, kept for "
                "months and rarely treated as a PHI store.",
                "The visitor's browser history, and any synced profile it is "
                "shared with on their other devices.",
                "The Referer header sent to every third party asset the next "
                "page loads, which is how it reaches an ad network.",
                "Analytics, which records the full page path including the "
                "query string by default.",
                "Anything the client forwards, since the details travel with "
                "the link when it is pasted into an email or a message.",
            ),
            example=("https://practice.simplepractice.com/intake"
                     "?name=Jane%20Doe&email=jane%40example.com"
                     "&reason=anxiety%20and%20panic"))

    return HandoffAssessment(
        method=method, status=STATUS_SECURE, safe=True,
        summary=(
            "The details go in the request body from your server to the EHR, "
            "so they never appear in an address. The client receives an opaque "
            "single use token that means nothing to anyone who intercepts the "
            "link."),
        controls=(
            "PHI travels in the POST body over TLS, so no intermediary writes "
            "it to a log as part of a URL.",
            "The link the client clicks carries only a random token, which "
            "identifies a session rather than describing a person.",
            "The token expires and is single use, so a forwarded link stops "
            "working rather than exposing an intake.",
            "Credentials live on the server, so nothing sensitive is visible "
            "in the browser.",
            "The EHR vendor has signed a BAA, so the disclosure to them is "
            "permitted in the first place.",
        ),
        example=('POST /v1/intake/prefill\n'
                 'Authorization: Bearer <server side key>\n'
                 'Content-Type: application/json\n\n'
                 '{"name": "Jane Doe", "email": "jane@example.com",\n'
                 ' "reason": "anxiety and panic"}\n\n'
                 '-> 201 {"token": "d41f2c8a9b", "expires_in": 900}\n'
                 'Client opens: https://practice.example.com/i/d41f2c8a9b'))


# ---------------------------------------------------------------------------
# Part three: the BAA matrix
# ---------------------------------------------------------------------------

BAA_AVAILABLE = "Available"
BAA_NOT_OFFERED = "Not offered"
BAA_CONDITIONAL = "Conditional"


@dataclass(frozen=True)
class BaaEntry:
    vendor: str
    role: str
    status: str
    detail: str
    # Whether this is settled public vendor policy or something the practice
    # has to confirm in writing. Stating that difference is the honest part:
    # a matrix that looks equally certain about all four rows invites somebody
    # to rely on the weakest one.
    confirm_with_vendor: bool
    action: str

    @property
    def tone(self) -> str:
        if self.status == BAA_AVAILABLE:
            return "ok"
        return "crit" if self.status == BAA_NOT_OFFERED else "warn"


def get_baa_matrix() -> list:
    """Which vendors in this stack will sign, and which will not."""
    return [
        BaaEntry(
            vendor="Google (Analytics and Ads)",
            role="Website analytics and advertising",
            status=BAA_NOT_OFFERED,
            detail=(
                "Google does not offer a BAA for Google Analytics or Google "
                "Ads. Its BAA covers certain Google Cloud and Workspace "
                "services, and the advertising and analytics products are not "
                "among them. So PHI must never reach these tags."),
            confirm_with_vendor=False,
            action="Remove these tags from every page that handles PHI."),
        BaaEntry(
            vendor="SimplePractice",
            role="Electronic health record",
            status=BAA_AVAILABLE,
            detail=(
                "Built for behavioural health practices and offers a BAA as "
                "standard. Having one available is not the same as having one "
                "signed and filed."),
            confirm_with_vendor=True,
            action="Confirm a countersigned BAA is on file and note its date."),
        BaaEntry(
            vendor="GoHighLevel",
            role="Marketing, funnels and messaging",
            status=BAA_CONDITIONAL,
            detail=(
                "HIPAA support is tied to plan tier and configuration rather "
                "than being on by default, so the answer depends on the "
                "specific account. Treat an unconfirmed account as one with "
                "no BAA."),
            confirm_with_vendor=True,
            action=("Get the BAA and the enabled plan in writing before any "
                    "intake or appointment data enters it.")),
        BaaEntry(
            vendor="Replit",
            role="Application hosting",
            status=BAA_CONDITIONAL,
            detail=(
                "General purpose developer hosting rather than a health "
                "platform, and a BAA is not part of the standard signup. "
                "Anything storing or passing through PHI needs one, whatever "
                "the hosting is."),
            confirm_with_vendor=True,
            action=("Ask directly, and if no BAA is available move any PHI "
                    "handling component to a host that will sign one.")),
    ]


def baa_rows() -> list:
    return [
        {
            "Vendor": entry.vendor,
            "Used for": entry.role,
            "BAA": entry.status,
            "Confirm in writing": "yes" if entry.confirm_with_vendor else "no",
            "Action": entry.action,
        }
        for entry in get_baa_matrix()
    ]


def unsigned_vendors() -> list:
    """Every vendor that is not a settled yes, which is the working list."""
    return [e.vendor for e in get_baa_matrix() if e.status != BAA_AVAILABLE
            or e.confirm_with_vendor]


# ---------------------------------------------------------------------------
# Part four: the plain language summary
# ---------------------------------------------------------------------------

def generate_clinician_summary() -> list:
    """What a practice owner needs, in sentences they can act on.

    Written for somebody who runs a therapy practice rather than a security
    team, so every line says what to do rather than what to be aware of.
    """
    return [
        "If a page asks for a name, an email or a reason for visiting, treat "
        "everything typed into it as clinical information. That is the whole "
        "test, and it is the one that decides the rest.",
        "Take Google Analytics and Google Ads tags off your booking pages and "
        "off anything behind a client login. Google will not sign the "
        "agreement that would make sending them client information lawful, so "
        "there is no version of this that is compliant.",
        "You can keep ordinary analytics on your plain marketing pages. A "
        "court struck down the rule that treated a visit to a public page as "
        "health information in June 2024, so anyone telling you all tracking "
        "is banned is working from guidance that has changed.",
        "Never put a client's details into a link. If your booking flow "
        "pre fills an intake form by putting a name or a reason for visiting "
        "in the web address, that address is saved in server logs, in browser "
        "history and in anything the link is forwarded to. Ask your developer "
        "to send it server to server instead.",
        "Ask every supplier that touches client information for a signed "
        "business associate agreement, and keep the countersigned copy. If a "
        "supplier will not sign one, they cannot hold client information, "
        "however convenient the tool is.",
        "If a tag has already been live on a booking page, do not simply "
        "remove it and move on. Write down what was sent and for how long, "
        "and take that record to a lawyer, because there is a formal "
        "assessment that decides whether it has to be reported.",
        "Put one person in charge of the tag manager. Most of these problems "
        "start with a marketing contractor adding a pixel to the whole site "
        "without knowing which pages take bookings.",
    ]


def audit_everything(page_type: str = PAGE_BOOKING,
                     collects_booking_info: bool = True,
                     has_google_ads: bool = True,
                     method: str = HANDOFF_QUERY_PARAMS) -> dict:
    """One call for the whole audit, so a caller cannot use half of it."""
    exposure = evaluate_gtm_exposure(page_type, collects_booking_info,
                                     has_google_ads)
    handoff = evaluate_ehr_handoff(method)
    return {
        "exposure": exposure,
        "handoff": handoff,
        "baa": get_baa_matrix(),
        "summary": generate_clinician_summary(),
        "open_items": (
            (1 if exposure.ocr_violation else 0)
            + (0 if handoff.safe else 1)
            + len(unsigned_vendors())
        ),
    }
