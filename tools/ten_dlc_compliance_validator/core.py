"""A2P 10DLC registration engine.

A 10DLC registration fails on details that look like nothing. A legal name that
reads the same to a person but differs from the IRS record by a full stop, a
consent page missing one sentence, a sample message with no opt out in it. Each
one is a rejection, and each rejection costs a vetting fee and days of waiting
while the client's texts stay undeliverable.

So this checks the three things that actually get registrations rejected, and
for each one it says exactly which characters or which sentence is wrong rather
than reporting a score.

Pure logic, no Streamlit import, so this is unit testable on its own and can sit
behind a real TCR submission script. Deterministic: nothing here reads the clock
or a random source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ENGINE_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Brand identity
# ---------------------------------------------------------------------------

ENTITY_PRIVATE = "Private company"
ENTITY_PUBLIC = "Public company"
ENTITY_NON_PROFIT = "Non profit"
ENTITY_SOLE_PROPRIETOR = "Sole proprietor"
ENTITY_GOVERNMENT = "Government"


@dataclass(frozen=True)
class EinRecord:
    """One row as the IRS holds it, which is what the registry checks against."""
    ein: str                 # formatted 12-3456789
    legal_name: str          # exactly as filed, including punctuation
    entity_type: str
    state: str


# A small simulated registry. The names are deliberately the shapes that trip
# registrations up: a full stop in the suffix, an ampersand, a leading The.
EIN_REGISTRY: tuple[EinRecord, ...] = (
    EinRecord("47-1829304", "Northwind Logistics, Inc.", ENTITY_PRIVATE, "WA"),
    EinRecord("82-4471903", "Bright & Early Coaching LLC", ENTITY_PRIVATE, "TX"),
    EinRecord("13-5620991", "The Harbor Clinic", ENTITY_NON_PROFIT, "NY"),
    EinRecord("94-3018872", "Sunridge Home Services Co.", ENTITY_PRIVATE, "CA"),
    EinRecord("36-7712045", "Halcyon Data Systems Corporation", ENTITY_PUBLIC,
              "IL"),
)

EIN_DIGITS = 9


def normalise_ein(text: str) -> tuple[bool, str, str]:
    """Read an EIN however it was typed, or refuse it with a reason."""
    digits = re.sub(r"\D", "", str(text))
    if not digits:
        return False, "", "Enter an EIN. Nine digits, with or without the dash."
    if len(digits) != EIN_DIGITS:
        return False, "", (f"An EIN is {EIN_DIGITS} digits and this has "
                           f"{len(digits)}. A transposed or missing digit is "
                           f"the commonest reason a brand comes back as "
                           f"unverified.")
    return True, f"{digits[:2]}-{digits[2:]}", ""


def record_by_ein(ein: str, registry=EIN_REGISTRY) -> EinRecord | None:
    ok, formatted, _ = normalise_ein(ein)
    if not ok:
        return None
    for record in registry:
        if record.ein == formatted:
            return record
    return None


VERDICT_EXACT = "Exact match"
VERDICT_CASE_ONLY = "Accepted, case and spacing only"
VERDICT_MISMATCH = "Rejected, the name does not match the record"
VERDICT_UNKNOWN_EIN = "Rejected, the EIN is not on file"
VERDICT_BAD_EIN = "Refused before submission"

# Differences that look harmless to a person and are not to the registry.
SUFFIXES = ("inc", "llc", "ltd", "corp", "corporation", "co", "company",
            "incorporated", "lp", "llp", "plc")


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def _casefold(text: str) -> str:
    return _collapse(text).casefold()


def _strip_punctuation(text: str) -> str:
    return _collapse(re.sub(r"[^\w\s]", "", str(text))).casefold()


def _distinctive_words(name: str) -> list[str]:
    """The words that actually identify a company.

    Entity suffixes and a leading article are shared by thousands of names, so
    two names matching only on those are not the same company.
    """
    return [word for word in _strip_punctuation(name).split()
            if word not in SUFFIXES and word != "the"]


@dataclass(frozen=True)
class NameDifference:
    kind: str
    detail: str
    fix: str


@dataclass(frozen=True)
class BrandMatch:
    submitted_name: str
    submitted_ein: str
    record: EinRecord | None
    verdict: str
    accepted: bool
    explanation: str
    differences: list[NameDifference]
    correction: str

    @property
    def exact(self) -> bool:
        return self.verdict == VERDICT_EXACT

    def difference_rows(self) -> list[dict]:
        return [
            {"Difference": difference.kind, "What it is": difference.detail,
             "Fix": difference.fix}
            for difference in self.differences
        ]


def _differences(submitted: str, official: str) -> list[NameDifference]:
    """Name the specific ways the two strings diverge.

    A score would be useless here. The person filling the form needs to know
    that the registry wants a full stop after Inc, not that they are eighty
    seven percent similar.
    """
    found: list[NameDifference] = []
    left, right = _collapse(submitted), _collapse(official)

    # When the two names share no distinctive word, every finer difference is
    # noise: reporting a suffix mismatch would send someone off to fix a comma
    # on a name that was never the right one.
    left_core = _distinctive_words(left)
    right_core = _distinctive_words(right)
    if left_core and right_core and not (set(left_core) & set(right_core)):
        return [NameDifference(
            "Different name",
            f"The record for this EIN reads {official!r}, which shares no word "
            f"with what was submitted.",
            f"Check the CP 575 letter. The registry matches on {official!r}.")]

    if left.casefold() == right.casefold() and left != right:
        found.append(NameDifference(
            "Capitalisation",
            f"{submitted!r} differs from the record only in letter case.",
            f"Either is accepted. The record reads {official!r}."))

    if _strip_punctuation(left) == _strip_punctuation(right) and \
            left.casefold() != right.casefold():
        missing = sorted(set(re.findall(r"[^\w\s]", right))
                         - set(re.findall(r"[^\w\s]", left)))
        extra = sorted(set(re.findall(r"[^\w\s]", left))
                       - set(re.findall(r"[^\w\s]", right)))
        if missing:
            found.append(NameDifference(
                "Missing punctuation",
                f"The record contains {' '.join(missing)} and the submitted "
                f"name does not.",
                f"Type the name exactly as filed: {official!r}."))
        if extra:
            found.append(NameDifference(
                "Extra punctuation",
                f"The submitted name contains {' '.join(extra)} and the record "
                f"does not.",
                f"Remove it. The record reads {official!r}."))

    left_words = _strip_punctuation(left).split()
    right_words = _strip_punctuation(right).split()

    if "and" in left_words and "&" in right:
        found.append(NameDifference(
            "Ampersand written out",
            "The record uses & and the submitted name spells out and.",
            f"Use the ampersand exactly as filed: {official!r}."))
    if "&" in left and "and" in right_words:
        found.append(NameDifference(
            "Ampersand instead of the word",
            "The record spells out and, and the submitted name uses &.",
            f"Spell it out as filed: {official!r}."))

    left_suffixes = [word for word in left_words if word in SUFFIXES]
    right_suffixes = [word for word in right_words if word in SUFFIXES]
    if right_suffixes and not left_suffixes:
        found.append(NameDifference(
            "Missing entity suffix",
            f"The record ends in {right_suffixes[-1].upper()} and the "
            f"submitted name has no entity suffix. This usually means a trade "
            f"name was entered instead of the legal one.",
            f"Submit the legal name: {official!r}."))
    elif left_suffixes and not right_suffixes:
        found.append(NameDifference(
            "Extra entity suffix",
            f"The submitted name ends in {left_suffixes[-1].upper()} and the "
            f"record carries no entity suffix at all.",
            f"Submit it as filed: {official!r}."))
    elif left_suffixes and right_suffixes and \
            left_suffixes[-1] != right_suffixes[-1]:
        found.append(NameDifference(
            "Wrong entity suffix",
            f"The submitted name ends in {left_suffixes[-1].upper()} and the "
            f"record ends in {right_suffixes[-1].upper()}.",
            f"Use the filed suffix: {official!r}."))

    if right_words[:1] == ["the"] and left_words[:1] != ["the"]:
        found.append(NameDifference(
            "Missing leading The",
            "The record begins with The and the submitted name does not.",
            f"Include it: {official!r}."))

    if not found and _casefold(left) != _casefold(right):
        found.append(NameDifference(
            "Different name",
            f"The record for this EIN reads {official!r}, which is not a "
            f"variation of what was submitted.",
            f"Check the CP 575 letter. The registry matches on "
            f"{official!r}."))
    return found


def match_brand(legal_name: str, ein: str, registry=EIN_REGISTRY) -> BrandMatch:
    """Check a brand the way the registry does, before the fee is spent."""
    ok, formatted, ein_error = normalise_ein(ein)
    if not ok:
        return BrandMatch(
            submitted_name=legal_name, submitted_ein=str(ein), record=None,
            verdict=VERDICT_BAD_EIN, accepted=False, explanation=ein_error,
            differences=[], correction="")

    record = record_by_ein(formatted, registry)
    if record is None:
        return BrandMatch(
            submitted_name=legal_name, submitted_ein=formatted, record=None,
            verdict=VERDICT_UNKNOWN_EIN, accepted=False,
            explanation=(f"No IRS record is held for {formatted}. The registry "
                         f"returns unverified, the vetting fee is spent and "
                         f"the campaign cannot be submitted at all."),
            differences=[], correction="")

    submitted = _collapse(legal_name)
    official = record.legal_name

    if submitted == official:
        return BrandMatch(
            submitted_name=submitted, submitted_ein=formatted, record=record,
            verdict=VERDICT_EXACT, accepted=True,
            explanation=(f"The name matches the record for {formatted} "
                         f"character for character, so the brand verifies on "
                         f"the first attempt."),
            differences=[], correction=official)

    differences = _differences(submitted, official)

    if submitted.casefold() == official.casefold():
        return BrandMatch(
            submitted_name=submitted, submitted_ein=formatted, record=record,
            verdict=VERDICT_CASE_ONLY, accepted=True,
            explanation=("Only the capitalisation differs, which the registry "
                         "accepts. Submitting it as filed is still the safer "
                         "habit."),
            differences=differences, correction=official)

    return BrandMatch(
        submitted_name=submitted, submitted_ein=formatted, record=record,
        verdict=VERDICT_MISMATCH, accepted=False,
        explanation=(f"The name does not match the record for {formatted}. "
                     f"The registry compares strings, not intentions, so this "
                     f"is an instant rejection and the vetting fee is not "
                     f"returned."),
        differences=differences, correction=official)


def brand_rows(match: BrandMatch) -> list[dict]:
    official = match.record.legal_name if match.record else "not on file"
    return [
        {"Field": "EIN submitted", "Value": match.submitted_ein or "not read"},
        {"Field": "Legal name submitted", "Value": match.submitted_name},
        {"Field": "Legal name on the IRS record", "Value": official},
        {"Field": "Entity type",
         "Value": match.record.entity_type if match.record else "unknown"},
        {"Field": "Registered state",
         "Value": match.record.state if match.record else "unknown"},
        {"Field": "Verdict", "Value": match.verdict},
    ]


# ---------------------------------------------------------------------------
# Opt in consent
# ---------------------------------------------------------------------------

MANDATORY = "Mandatory"
RECOMMENDED = "Recommended"
DISQUALIFIER = "Disqualifier"


@dataclass(frozen=True)
class Clause:
    key: str
    label: str
    level: str
    patterns: tuple[str, ...]
    why: str
    wording: str
    case_sensitive: bool = False


CONSENT_CLAUSES: tuple[Clause, ...] = (
    Clause(
        key="program_description",
        label="What the messages are for",
        level=MANDATORY,
        patterns=(r"\breceive\b.{0,40}\b(messages|texts|sms|alerts|updates|"
                  r"reminders)\b",
                  r"\b(sign|signing|opt)\s*(up|in)\b.{0,40}\b(messages|texts|"
                  r"sms|alerts)\b"),
        why="The reviewer has to see what the person is agreeing to receive. "
            "A consent page that never says it is for text messages is refused "
            "without reading the rest.",
        wording="By providing your mobile number you agree to receive "
                "appointment reminders by text message.",
    ),
    Clause(
        key="brand_name",
        label="Who is sending",
        level=MANDATORY,
        patterns=(r"\bfrom\s+[A-Z][\w&.,' ]{2,}",
                  r"\bmessages?\s+from\b"),
        why="The brand has to be named on the page and in the messages, and "
            "the two have to be the same name.",
        wording="You will receive messages from Northwind Logistics, Inc.",
        case_sensitive=True,
    ),
    Clause(
        key="frequency",
        label="Message frequency",
        level=MANDATORY,
        patterns=(r"\bmessage\s+frequency\s+(may\s+)?var(y|ies)\b",
                  r"\b(up\s+to\s+)?\d+\s*(msgs?|messages|texts)\s*(per|/)\s*"
                  r"(month|week|day)\b",
                  r"\brecurring\s+(messages|texts)\b"),
        why="Without a stated frequency the consent is open ended, which "
            "carriers treat as no consent at all.",
        wording="Message frequency varies.",
    ),
    Clause(
        key="rates",
        label="Message and data rates",
        level=MANDATORY,
        patterns=(r"\bmsg(sage)?\s*(&|and)\s*data\s+rates\s+may\s+apply\b",
                  r"\bmessage\s+and\s+data\s+rates\s+may\s+apply\b",
                  r"\bmsg\s*&\s*data\s+rates\s+may\s+apply\b"),
        why="This exact disclosure is checked for by name. Paraphrasing it is "
            "the single commonest reason a consent page is sent back.",
        wording="Message and data rates may apply.",
    ),
    Clause(
        key="opt_out",
        label="Opt out instructions",
        level=MANDATORY,
        patterns=(r"\breply\s+stop\b", r"\btext\s+stop\b",
                  r"\bstop\s+to\s+(cancel|unsubscribe|opt\s*out)\b"),
        why="The person has to be told how to stop before they agree, not "
            "only afterwards in the messages.",
        wording="Reply STOP to cancel at any time.",
    ),
    Clause(
        key="help",
        label="Help instructions",
        level=MANDATORY,
        patterns=(r"\breply\s+help\b", r"\btext\s+help\b",
                  r"\bhelp\s+for\s+(help|assistance|support)\b"),
        why="HELP has to be answered by the campaign, and the page has to say "
            "so.",
        wording="Reply HELP for help.",
    ),
    Clause(
        key="privacy_policy",
        label="Privacy policy link",
        level=MANDATORY,
        patterns=(r"privacy\s+(policy|notice)",),
        why="The reviewer follows the link. A page that names a privacy policy "
            "without linking to one is treated as not having one.",
        wording="See our Privacy Policy at example.com/privacy.",
    ),
    Clause(
        key="terms",
        label="Terms of service link",
        level=RECOMMENDED,
        patterns=(r"terms\s+(of\s+(service|use)|and\s+conditions)",),
        why="Not refused without it in every case, and every reviewed "
            "submission goes faster with it.",
        wording="See our Terms of Service at example.com/terms.",
    ),
    Clause(
        key="not_a_condition",
        label="Consent is not a condition of purchase",
        level=RECOMMENDED,
        patterns=(r"not\s+a\s+condition\s+of\s+(any\s+)?purchase",
                  r"consent\s+is\s+not\s+required\s+to\s+(buy|purchase)"),
        why="Required whenever the number is collected during a sale, and "
            "harmless everywhere else.",
        wording="Consent is not a condition of any purchase.",
    ),
    Clause(
        key="third_party_sharing",
        label="Numbers shared with third parties",
        level=DISQUALIFIER,
        patterns=(r"\bshare\b.{0,60}\b(partners|third\s+part(y|ies)|"
                  r"affiliates)\b",
                  r"\b(sell|sold)\b.{0,40}\b(your\s+)?(information|data|"
                  r"number)\b"),
        why="Consent collected on a page that shares numbers onward is not "
            "consent for this brand. This is a refusal, not a warning.",
        wording="Remove the sharing language. Mobile opt in data cannot be "
                "shared with third parties for their own marketing.",
    ),
    Clause(
        key="pre_checked",
        label="Consent pre selected for the user",
        level=DISQUALIFIER,
        patterns=(r"pre\s*-?\s*(checked|selected|ticked)",
                  r"checked\s+by\s+default",
                  r"automatically\s+(opted|enrolled)"),
        why="Consent has to be an action the person takes. A box already "
            "ticked is not an action.",
        wording="Remove it. The box must start empty and be ticked by the "
                "person.",
    ),
)


@dataclass(frozen=True)
class ClauseFinding:
    clause: Clause
    present: bool
    excerpt: str

    @property
    def passing(self) -> bool:
        """A disqualifier passes by being absent. Everything else by being
        present."""
        if self.clause.level == DISQUALIFIER:
            return not self.present
        return self.present


def _find(text: str, patterns: tuple[str, ...],
          case_sensitive: bool = False) -> str:
    flags = re.DOTALL if case_sensitive else re.IGNORECASE | re.DOTALL
    for pattern in patterns:
        found = re.search(pattern, text, flags)
        if found:
            return _collapse(found.group(0))[:120]
    return ""


def _brand_anchor(brand: str) -> str:
    """The part of a brand name worth searching for.

    Entity suffixes and articles are shared by thousands of companies, so
    matching on them would find a brand that is not there.
    """
    words = [word for word in _strip_punctuation(brand).split()
             if word not in SUFFIXES and word != "the"]
    return words[0] if words else ""


URL_PATTERN = re.compile(
    r"(https?://\S+|\b[\w.-]+\.(com|org|net|io|co|us)(/\S*)?)", re.IGNORECASE)


def scan_consent(text: str, brand: str = "",
                 clauses: tuple[Clause, ...] = CONSENT_CLAUSES
                 ) -> list[ClauseFinding]:
    """Read a consent page the way a reviewer does, clause by clause.

    When a brand is given, the page has to name that brand rather than merely
    name somebody. The two have to agree: a consent page for one company does
    not register a campaign for another.
    """
    body = _collapse(text)
    findings = []
    for clause in clauses:
        excerpt = _find(body, clause.patterns, clause.case_sensitive)
        present = bool(excerpt)
        if clause.key == "brand_name" and brand:
            anchor = _brand_anchor(brand)
            present = bool(anchor) and anchor in body.casefold()
            excerpt = brand if present else ""
        if clause.key == "privacy_policy" and present:
            # Naming a privacy policy is not linking to one, and the reviewer
            # follows the link.
            window = body[max(0, body.lower().find("privacy") - 60):
                          body.lower().find("privacy") + 160]
            if not URL_PATTERN.search(window):
                present = False
                excerpt = ""
        findings.append(ClauseFinding(clause, present, excerpt))
    return findings


@dataclass(frozen=True)
class ConsentVerdict:
    passing_mandatory: int
    total_mandatory: int
    missing: list[Clause]
    disqualifiers: list[Clause]
    ready: bool
    headline: str
    summary: str

    @property
    def percent(self) -> int:
        if not self.total_mandatory:
            return 0
        return round(self.passing_mandatory * 100 / self.total_mandatory)


READY = "Ready to submit"
NOT_READY = "Will be rejected"
DISQUALIFIED = "Disqualified"


def consent_verdict(findings: list[ClauseFinding]) -> ConsentVerdict:
    mandatory = [f for f in findings if f.clause.level == MANDATORY]
    missing = [f.clause for f in mandatory if not f.passing]
    disqualifiers = [f.clause for f in findings
                     if f.clause.level == DISQUALIFIER and f.present]
    passing = len(mandatory) - len(missing)

    if disqualifiers:
        headline = DISQUALIFIED
        summary = (f"{len(disqualifiers)} disqualifying phrase(s) on the page. "
                   f"This is refused whatever else the page says, because the "
                   f"consent it collects is not consent for this brand.")
    elif missing:
        names = ", ".join(clause.label.lower() for clause in missing)
        summary = (f"{len(missing)} mandatory clause(s) missing: {names}. Add "
                   f"the wording below and the page passes.")
        headline = NOT_READY
    else:
        headline = READY
        summary = ("Every mandatory clause is present and nothing "
                   "disqualifying is on the page.")
    return ConsentVerdict(passing_mandatory=passing,
                          total_mandatory=len(mandatory), missing=missing,
                          disqualifiers=disqualifiers,
                          ready=not missing and not disqualifiers,
                          headline=headline, summary=summary)


def consent_rows(findings: list[ClauseFinding]) -> list[dict]:
    return [
        {"Clause": finding.clause.label,
         "Level": finding.clause.level,
         "Status": "Pass" if finding.passing else "Fail",
         "Found": finding.excerpt or "not found"}
        for finding in findings
    ]


def consent_fix(findings: list[ClauseFinding]) -> str:
    """The sentences to paste, in the order they should appear."""
    missing = [f.clause for f in findings
               if f.clause.level in (MANDATORY, RECOMMENDED) and not f.passing]
    present_disqualifiers = [f.clause for f in findings
                             if f.clause.level == DISQUALIFIER and f.present]
    if not missing and not present_disqualifiers:
        return "# The page carries every clause. Nothing to add."

    lines = []
    if missing:
        lines.append("# Add these sentences to the consent page:")
        lines += [clause.wording for clause in missing]
    if present_disqualifiers:
        lines.append("")
        lines.append("# Remove this, it disqualifies the page on its own:")
        lines += [clause.wording for clause in present_disqualifiers]
    return "\n".join(lines)


SAMPLE_CONSENT_GOOD = (
    "By providing your mobile number you agree to receive appointment "
    "reminders and delivery updates by text message from Northwind Logistics, "
    "Inc. Message frequency varies. Message and data rates may apply. Reply "
    "STOP to cancel at any time. Reply HELP for help. See our Privacy Policy "
    "at northwindlogistics.com/privacy and our Terms of Service at "
    "northwindlogistics.com/terms. Consent is not a condition of any purchase."
)

SAMPLE_CONSENT_THIN = (
    "Enter your phone number to get updates from us. We will text you when "
    "your order ships. You can unsubscribe whenever you like."
)

SAMPLE_CONSENT_DISQUALIFIED = (
    SAMPLE_CONSENT_GOOD
    + " We may share your information with our marketing partners."
)


# ---------------------------------------------------------------------------
# Sample message
# ---------------------------------------------------------------------------

# GSM 03.38 basic set. A character outside it forces the whole message into
# UCS-2, which is why one emoji cuts the length from 160 to 70.
GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅ"
    "åΔ_ΦΓΛΩΠΨΣΘΞ"
    "ÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# These cost two septets each, because they are sent with an escape.
GSM7_EXTENDED = set("^{}\\[~]|€")

GSM_SINGLE, GSM_MULTI = 160, 153
UCS2_SINGLE, UCS2_MULTI = 70, 67

ENCODING_GSM = "GSM 7 bit"
ENCODING_UCS2 = "UCS 2"


def encoding_of(text: str) -> str:
    for character in str(text):
        if character not in GSM7_BASIC and character not in GSM7_EXTENDED:
            return ENCODING_UCS2
    return ENCODING_GSM


def message_units(text: str) -> int:
    """Septets for GSM, UTF-16 code units for UCS-2.

    An emoji outside the basic plane is a surrogate pair, so it costs two
    units, which is why a single emoji can add a whole segment.
    """
    text = str(text)
    if encoding_of(text) == ENCODING_UCS2:
        return len(text.encode("utf-16-le")) // 2
    return sum(2 if character in GSM7_EXTENDED else 1 for character in text)


def segment_count(text: str) -> int:
    units = message_units(text)
    if units == 0:
        return 0
    encoding = encoding_of(text)
    single, multi = ((GSM_SINGLE, GSM_MULTI) if encoding == ENCODING_GSM
                     else (UCS2_SINGLE, UCS2_MULTI))
    if units <= single:
        return 1
    return -(-units // multi)      # ceiling division


OPT_OUT_PATTERNS = (r"\breply\s+stop\b", r"\btext\s+stop\b",
                    r"\bstop\s*=\s*(cancel|end|quit)\b",
                    r"\bstop\s+to\s+(cancel|unsubscribe|end|opt\s*out)\b")
HELP_PATTERNS = (r"\breply\s+help\b", r"\btext\s+help\b",
                 r"\bhelp\s+for\s+(help|info|assistance)\b")

# Public shorteners are shared by everyone, so carriers filter them wholesale.
PUBLIC_SHORTENERS = ("bit.ly", "tinyurl.com", "goo.gl", "t.co", "ow.ly",
                     "is.gd", "buff.ly", "rebrand.ly")

# Alcohol, firearms and the rest. Present as whole words only, so "hemp" does
# not fire on "hemphill".
SHAFT_TERMS = ("cannabis", "cbd", "vape", "kratom", "casino", "sportsbook",
               "payday loan", "firearm", "ammo", "tobacco")

MESSAGE_OPT_OUT = "opt_out"
MESSAGE_BRAND = "brand"
MESSAGE_HELP = "help"
MESSAGE_SHORTENER = "shortener"
MESSAGE_PLACEHOLDER = "placeholder"
MESSAGE_SHAFT = "shaft"
MESSAGE_LENGTH = "length"


@dataclass(frozen=True)
class MessageCheck:
    key: str
    label: str
    level: str
    passing: bool
    detail: str
    fix: str


@dataclass(frozen=True)
class MessageReport:
    text: str
    brand: str
    encoding: str
    units: int
    segments: int
    checks: list[MessageCheck]
    corrected: str
    corrected_segments: int

    @property
    def ready(self) -> bool:
        return all(check.passing for check in self.checks
                   if check.level == MANDATORY)

    @property
    def failing(self) -> list[MessageCheck]:
        return [check for check in self.checks if not check.passing]

    def rows(self) -> list[dict]:
        return [
            {"Check": check.label, "Level": check.level,
             "Status": "Pass" if check.passing else "Fail",
             "Detail": check.detail}
            for check in self.checks
        ]


OPT_OUT_SENTENCE = "Reply STOP to cancel."


def check_message(text: str, brand: str = "Northwind Logistics",
                  first_message: bool = True) -> MessageReport:
    """Validate a sample message the way a carrier and a reviewer both would.

    The opt out is the one that gets campaigns rejected, and adding it can push
    the message into a second segment, which doubles what every send costs. The
    report says so rather than leaving it to be discovered on the invoice.
    """
    body = _collapse(text)
    lowered = body.lower()
    checks: list[MessageCheck] = []

    has_opt_out = any(re.search(pattern, lowered) for pattern in
                      OPT_OUT_PATTERNS)
    checks.append(MessageCheck(
        MESSAGE_OPT_OUT, "Opt out instruction",
        MANDATORY if first_message else RECOMMENDED, has_opt_out,
        "Found." if has_opt_out else
        "No opt out in the message. On the first message of a campaign this is "
        "a rejection, and on any message it is what a complaint is judged on.",
        f"Append {OPT_OUT_SENTENCE!r}."))

    has_brand = bool(brand) and brand.split()[0].lower() in lowered
    checks.append(MessageCheck(
        MESSAGE_BRAND, "Brand named in the message", MANDATORY, has_brand,
        f"{brand} is named." if has_brand else
        f"The message never says it is from {brand}. A person who cannot tell "
        f"who is texting them reports it as spam.",
        f"Open with the brand: {brand}: ..."))

    has_help = any(re.search(pattern, lowered) for pattern in HELP_PATTERNS)
    checks.append(MessageCheck(
        MESSAGE_HELP, "Help instruction", RECOMMENDED, has_help,
        "Found." if has_help else
        "No HELP instruction. It is required in the help reply rather than in "
        "every message, and including it on the first one is the safer habit.",
        "Append 'Reply HELP for help.'"))

    shortener = next((s for s in PUBLIC_SHORTENERS if s in lowered), "")
    checks.append(MessageCheck(
        MESSAGE_SHORTENER, "No public link shortener", MANDATORY,
        not shortener,
        "No shared shortener." if not shortener else
        f"{shortener} is a shared shortener, used by every sender on it. "
        f"Carriers filter the domain wholesale, so the message is blocked "
        f"before anyone reads it.",
        "Use a branded domain or the full link."))

    placeholders = re.findall(r"[\[{<]\s*\w[\w ]*\s*[\]}>]", body)
    checks.append(MessageCheck(
        MESSAGE_PLACEHOLDER, "Placeholders shown as samples", RECOMMENDED,
        bool(placeholders) or "http" in lowered,
        f"{len(placeholders)} placeholder(s) shown." if placeholders else
        "No placeholder in the sample. A reviewer reads a sample with no "
        "variable content as a message that was never really sent.",
        "Show the variable parts as [FirstName] or [OrderNumber]."))

    shaft = next((term for term in SHAFT_TERMS
                  if re.search(rf"\b{re.escape(term)}\b", lowered)), "")
    checks.append(MessageCheck(
        MESSAGE_SHAFT, "No restricted content", MANDATORY, not shaft,
        "Nothing restricted." if not shaft else
        f"{shaft!r} puts this campaign in a restricted category, which needs a "
        f"different use case and separate carrier approval.",
        "Remove it, or register the campaign under the restricted use case it "
        "belongs to."))

    segments = segment_count(body)
    checks.append(MessageCheck(
        MESSAGE_LENGTH, "Segment count", RECOMMENDED, segments <= 1,
        f"{message_units(body)} units, {segments} segment(s), "
        f"{encoding_of(body)}.",
        "Shorten to one segment. Every segment is billed separately."))

    corrected = body
    if not has_opt_out:
        corrected = f"{corrected} {OPT_OUT_SENTENCE}".strip()
    if not has_brand and brand:
        corrected = f"{brand}: {corrected}"

    return MessageReport(
        text=body, brand=brand, encoding=encoding_of(body),
        units=message_units(body), segments=segments, checks=checks,
        corrected=corrected, corrected_segments=segment_count(corrected))


SAMPLE_MESSAGE_GOOD = (
    "Northwind Logistics: your delivery [OrderNumber] arrives today between "
    "[Window]. Reply STOP to cancel. Reply HELP for help."
)
SAMPLE_MESSAGE_THIN = "Your order is on the way! Track it here: bit.ly/3xKp2"
SAMPLE_MESSAGE_EMOJI = (
    "Northwind Logistics: your delivery [OrderNumber] is out for delivery "
    "\U0001F69A. Reply STOP to cancel."
)
