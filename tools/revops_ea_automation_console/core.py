"""RevOps executive assistant and automation engine.

No Streamlit import lives in this file. Each of the three parts is built
around the mistake that actually costs a founder money, rather than around
the happy path:

* inbox triage only has one unrecoverable verdict. A wrongly escalated
  newsletter costs fifteen seconds. A wrongly archived contract is never
  seen again, so archiving requires positive evidence and everything
  unrecognised goes to the founder;
* a chase without a terminal state is not a process, it is a habit. The
  ladder here ends in a decision to pull the task, and it never softens as
  the wait grows;
* a decision logged without an owner is a wish, and a deliverable without
  a check a third party could evaluate cannot honestly be marked done, so
  the log refuses to emit an artifact rather than emitting a hollow one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

ENGINE_VERSION = "1.0.0"

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"

_SEVERITY_ORDER = {SEVERITY_OK: 0, SEVERITY_WARN: 1, SEVERITY_CRITICAL: 2}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


def _worst(findings: Sequence[Finding]) -> str:
    if not findings:
        return SEVERITY_OK
    return max((f.severity for f in findings), key=lambda s: _SEVERITY_ORDER[s])


def _sentences(text: str) -> tuple[str, ...]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", str(text or ""))
    return tuple(p.strip() for p in parts if p.strip())


# ---------------------------------------------------------------------------
# 1. Inbox triage
# ---------------------------------------------------------------------------

FOUNDER_ACTION = "Founder Action"
DRAFT_READY = "Draft Ready"
ARCHIVE = "Archive"

ROUTES: tuple[str, ...] = (FOUNDER_ACTION, DRAFT_READY, ARCHIVE)

# Signals that a human with authority has to see it. Money, commitment,
# legal exposure, and anything with a clock on it.
ESCALATE_SIGNALS: tuple[tuple[str, str], ...] = (
    (r"\bcontract\b|\bmsa\b|\bsow\b|\bterms\b", "a contract or terms"),
    (r"\binvoice\b|\bpayment\b|\brefund\b|\bchargeback\b", "money moving"),
    (r"\blegal\b|\bcounsel\b|\bliabilit", "legal exposure"),
    (r"\bnotice\b|\bterminat|\bcancel(?:ling|lation)?\b", "a cancellation"),
    (r"\burgent\b|\basap\b|\bby (?:today|tomorrow|eod|cob)\b",
     "an explicit deadline"),
    (r"\brenew(?:al|ing)?\b|\bpricing\b|\bquote\b|\bproposal\b",
     "a commercial decision"),
    (r"\bescalat|\bcomplaint\b|\bunhappy\b|\bdisappoint",
     "an unhappy counterparty"),
    (r"\bsign\b|\bsignature\b|\bapprov(?:e|al)\b|\bauthoris|\bauthoriz",
     "an approval only the founder can give"),
)

# Signals that a competent assistant can answer with a template and a
# founder only has to press send.
DRAFTABLE_SIGNALS: tuple[tuple[str, str], ...] = (
    (r"\bavailab|\bcalendar\b|\bschedul|\bbook a (?:call|time)\b|\breschedul",
     "a scheduling request"),
    (r"\bintro(?:duction|duce)?\b|\bconnect you\b", "an introduction"),
    (r"\bcase study\b|\bdeck\b|\bone pager\b|\bportfolio\b",
     "a request for collateral"),
    (r"\bthanks\b|\bthank you\b|\bappreciate\b", "a courtesy reply"),
    (r"\bfollow(?:ing)? up\b|\bcheck(?:ing)? in\b", "a follow up"),
)

# Positive evidence that nothing is being asked of anyone.
NOISE_SIGNALS: tuple[tuple[str, str], ...] = (
    (r"\bunsubscribe\b", "an unsubscribe footer"),
    (r"\bno[-\s]?reply@|\bdo not reply\b|\bdonotreply\b",
     "a no reply sender"),
    (r"\bnewsletter\b|\bweekly digest\b|\bwebinar\b",
     "a bulk mailing"),
    (r"\bthis is an automated\b|\bautomated (?:message|notification)\b",
     "an automated notification"),
    (r"\byour receipt\b|\breceipt for\b|\border confirmation\b",
     "a receipt"),
)


@dataclass(frozen=True)
class TriageResult:
    route: str
    summary: str
    escalate_signals: tuple[str, ...]
    draftable_signals: tuple[str, ...]
    noise_signals: tuple[str, ...]
    grounded: bool
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def _match_signals(text: str,
                   table: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    found: list[str] = []
    for pattern, label in table:
        if re.search(pattern, text, flags=re.IGNORECASE):
            if label not in found:
                found.append(label)
    return tuple(found)


def simulate_inbox_triage(email_content: str) -> TriageResult:
    """Route one message, and summarise it without inventing anything.

    The routing rule is not symmetric, because the costs are not. A
    newsletter sent to the founder costs fifteen seconds of attention. A
    contract sent to the archive is never read at all. So Archive is the
    only verdict that requires positive evidence, and anything the rules do
    not recognise goes to the founder rather than quietly away.

    The summary is extractive. Every sentence in it is a sentence that was
    in the message. A generated summary that invents a date or a figure is
    worse than no summary, because the founder acts on it.
    """
    body = str(email_content or "")
    findings: list[Finding] = []

    escalate = _match_signals(body, ESCALATE_SIGNALS)
    draftable = _match_signals(body, DRAFTABLE_SIGNALS)
    noise = _match_signals(body, NOISE_SIGNALS)

    if not body.strip():
        route = FOUNDER_ACTION
        summary = ""
        findings.append(Finding(
            code="TRI-EMPTY", severity=SEVERITY_WARN,
            title="An empty message was routed to the founder, not archived",
            detail=("Nothing was read, so nothing is known. The rule that "
                    "protects the inbox is that absence of evidence is never "
                    "grounds for archiving."),
            fix="Check the connector before trusting the next batch."))
    elif escalate:
        route = FOUNDER_ACTION
        findings.append(Finding(
            code="TRI-ESCALATE", severity=SEVERITY_CRITICAL,
            title=f"Escalated on {len(escalate)} signal(s): "
                  f"{escalate[0]}",
            detail=("A signal in this class names a commitment, a sum of "
                    "money, or a clock. None of those can be answered by a "
                    "template, and an assistant guessing at one commits the "
                    "founder to something."),
            fix="Put it in front of the founder with the signal named."))
    elif draftable:
        route = DRAFT_READY
        findings.append(Finding(
            code="TRI-DRAFT", severity=SEVERITY_OK,
            title=f"Draftable: {draftable[0]}",
            detail=("This is answerable from a template with no new "
                    "commitment. The founder reads and presses send, which "
                    "is a recoverable mistake if the routing was wrong."),
            fix="Attach the draft. Do not send it without a read."))
    elif noise:
        route = ARCHIVE
        findings.append(Finding(
            code="TRI-ARCHIVE", severity=SEVERITY_WARN,
            title=f"Archived on positive evidence: {noise[0]}",
            detail=("Archive is the one verdict the founder never sees, so "
                    "it is the one verdict that is never reached by default. "
                    "It was reached here because the message carries a mark "
                    "of bulk mail and carries no escalation signal at all."),
            fix=("Keep archived mail searchable rather than deleted, so a "
                 "wrong archive is recoverable at all.")))
    else:
        route = FOUNDER_ACTION
        findings.append(Finding(
            code="TRI-UNKNOWN", severity=SEVERITY_WARN,
            title="Nothing recognised, so it goes to the founder",
            detail=("No escalation signal, no draftable pattern, and no mark "
                    "of bulk mail. An unrecognised message is not a quiet "
                    "message, it is a message the rules have not learned "
                    "yet."),
            fix=("Review what lands here weekly. That queue is the list of "
                 "rules still missing.")))

    lead = _sentences(body)
    summary = lead[0] if lead else ""
    if len(lead) > 1 and escalate:
        summary = f"{lead[0]} {lead[1]}"

    grounded = summary == "" or summary.replace(" ", "") in \
        body.replace("\n", " ").replace(" ", "")
    if summary:
        findings.append(Finding(
            code="TRI-GROUNDED", severity=SEVERITY_OK,
            title="The summary is extractive, not generated",
            detail=("Every character of the summary appears in the message. "
                    "A summary that paraphrases a figure or a date is a "
                    "summary the founder will act on and cannot check."),
            fix="Keep the original one click away from the summary."))

    headline = f"{route}: {len(escalate)} escalation signal(s)"
    return TriageResult(
        route=route, summary=summary, escalate_signals=escalate,
        draftable_signals=draftable, noise_signals=noise, grounded=grounded,
        headline=headline, findings=tuple(findings))


SAMPLE_EMAILS: tuple[tuple[str, str], ...] = (
    ("Renewal with a clock on it",
     "Hi, our MSA renewal is due and we need pricing confirmed by EOD "
     "Friday. Can you sign off on the revised terms?"),
    ("Scheduling request",
     "Are you available for a call next week? Happy to work around your "
     "calendar."),
    ("Vendor newsletter",
     "This is an automated message from our weekly digest. Unsubscribe at "
     "any time."),
    ("Unrecognised, sent to the founder",
     "Following on from the thing we discussed, I had a thought about the "
     "shape of it."),
    ("Complaint wearing a polite hat",
     "Thanks for the update. I have to say we are quite disappointed with "
     "how the last sprint landed."),
)


# ---------------------------------------------------------------------------
# 2. Contractor chase ladder
# ---------------------------------------------------------------------------

CHASE_WAIT = "Wait"
CHASE_NUDGE = "Nudge"
CHASE_CHASE = "Chase with a deadline"
CHASE_WARN = "Warn and prepare the handover"
CHASE_PULL = "Pull the task"

CHASE_STAGES: tuple[str, ...] = (CHASE_WAIT, CHASE_NUDGE, CHASE_CHASE,
                                 CHASE_WARN, CHASE_PULL)

_STAGE_RANK = {stage: index for index, stage in enumerate(CHASE_STAGES)}

PULL_THRESHOLD_DAYS = 7
MAX_CHASE_MESSAGES = 3


@dataclass(frozen=True)
class ChaseVerdict:
    days_waiting: int
    stage: str
    rank: int
    recommendation: str
    chase_messages_sent: int
    terminal: bool
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)


_LADDER: tuple[tuple[int, str, int, str], ...] = (
    (0, CHASE_WAIT, 0,
     "Nothing is late. The clock started today, so there is nothing to "
     "say yet."),
    (1, CHASE_WAIT, 0,
     "One day. Still inside anyone's normal turnaround, so I would leave "
     "it alone."),
    (2, CHASE_NUDGE, 1,
     "Two days. I would send a short nudge today, no deadline attached, "
     "just a visible ping so the silence is not mutual."),
    (3, CHASE_CHASE, 2,
     "Three days. Nudge went unanswered, so I would chase properly and put "
     "a date on it: I need this by end of tomorrow."),
    (4, CHASE_CHASE, 3,
     "Four days. Still nothing, I would give it until Monday then pull the "
     "task."),
    (5, CHASE_WARN, 3,
     "Five days. I would tell them plainly that we are lining up a "
     "replacement, and start briefing that replacement today rather than "
     "after the deadline passes."),
    (6, CHASE_WARN, 3,
     "Six days. The handover brief should already be written. I would send "
     "one last message stating that Monday is the cutoff."),
)

_TERMINAL = (
    "Seven days or more. Pull it. Whatever the explanation turns out to "
    "be, a week of silence has already cost more than reassigning would "
    "have, and every further day is spent on hope rather than on the "
    "deliverable.")


def evaluate_contractor_chase(days_waiting: int) -> ChaseVerdict:
    """Say the decision out loud, and make sure the ladder ends.

    The failure mode of a chase is not that it is too aggressive. It is
    that it has no terminal state, so it runs forever, and the task quietly
    stays assigned to someone who is not doing it. This ladder reaches a
    decision to pull, the recommendation never softens as the wait grows,
    and the number of chase messages is capped so the founder is not asked
    to keep paying attention to the same silence.
    """
    days = int(days_waiting)
    if days < 0:
        raise ValueError("a wait cannot be negative")

    findings: list[Finding] = []

    if days >= PULL_THRESHOLD_DAYS:
        stage, sent, recommendation = CHASE_PULL, MAX_CHASE_MESSAGES, _TERMINAL
        findings.append(Finding(
            code="CHS-PULL", severity=SEVERITY_CRITICAL,
            title=f"Day {days}: the ladder is finished, reassign the task",
            detail=("A chase with no terminal state is not a process, it is "
                    "a habit. This is the state that makes it a process."),
            fix=("Reassign today and tell the contractor it has moved. A "
                 "silent reassignment creates two people who think they own "
                 "it.")))
    else:
        _, stage, sent, recommendation = _LADDER[days]

    if stage in (CHASE_NUDGE, CHASE_CHASE):
        findings.append(Finding(
            code="CHS-COST", severity=SEVERITY_WARN,
            title="Each chase spends founder attention, not just time",
            detail=("Three messages is the cap here because a fourth teaches "
                    "the contractor that the deadline is soft, and teaches "
                    "the founder to stop reading the thread."),
            fix="Attach a date to the second message, never to the first."))

    if stage == CHASE_WARN:
        findings.append(Finding(
            code="CHS-PARALLEL", severity=SEVERITY_WARN,
            title="Brief the replacement before the deadline, not after",
            detail=("Waiting for the cutoff to pass before finding cover "
                    "adds the search time to the delay that already "
                    "happened."),
            fix="Line up cover on day five even if the work still lands."))

    if stage == CHASE_WAIT:
        findings.append(Finding(
            code="CHS-QUIET", severity=SEVERITY_OK,
            title=f"Day {days}: no action is the right action",
            detail=("Chasing inside a normal turnaround window buys nothing "
                    "and spends the credibility the day three chase will "
                    "need."),
            fix="Log the expected date so day three is not a judgement call."))

    terminal = stage == CHASE_PULL
    headline = f"Day {days}: {stage}"
    return ChaseVerdict(
        days_waiting=days, stage=stage, rank=_STAGE_RANK[stage],
        recommendation=recommendation, chase_messages_sent=sent,
        terminal=terminal, headline=headline, findings=tuple(findings))


# ---------------------------------------------------------------------------
# 3. Call decision ledger
# ---------------------------------------------------------------------------

LOGGED = "Logged as a decision"
REFUSED = "Refused, this is not a decision yet"

DECISION_PATTERN = re.compile(
    r"^\s*(?:[-*•]\s*)?"
    r"(?P<owner>[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*)?)"
    r"\s+(?:will|to|is going to|owns|takes)\s+"
    r"(?P<deliverable>.+?)"
    r"(?:\s+by\s+(?P<due>[^,;]+?))?\s*$")

# A check is verifiable when a third party can evaluate it without asking
# the owner whether they did it.
VERIFIABLE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"https?://\S+", "a link that either opens or does not"),
    (r"\b[\w./-]+\.(?:pdf|csv|xlsx|docx|md|py|json|sql)\b",
     "a named file that either exists or does not"),
    (r"\b[A-Z]{2,}-\d+\b", "a ticket identifier"),
    (r"\b(?:hubspot|drive|notion|github|stripe|slack|airtable)\b",
     "a named system someone else can open"),
    (r"\b\d+(?:\.\d+)?\s*(?:%|percent|k|users?|rows?|leads?|days?|hours?)\b",
     "a number that can be counted"),
)

UNVERIFIABLE_WORDS = ("done", "sorted", "handled", "looked at", "actioned",
                      "circled back", "touched base")


@dataclass(frozen=True)
class DecisionEntry:
    line: str
    owner: str
    deliverable: str
    due: str
    check_marker: str
    verifiable: bool
    status: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def logged(self) -> bool:
        return self.status == LOGGED


@dataclass(frozen=True)
class DecisionLedger:
    entries: tuple[DecisionEntry, ...]
    unparsed: tuple[str, ...]
    logged: int
    refused: int
    lines_in: int
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def _check_marker(deliverable: str) -> tuple[str, bool]:
    for pattern, label in VERIFIABLE_PATTERNS:
        match = re.search(pattern, deliverable, flags=re.IGNORECASE)
        if match:
            return (f"CHECK: {match.group(0)} is present, which is "
                    f"{label}."), True
    return "", False


def log_call_decision(raw_notes: str) -> DecisionLedger:
    """Turn call notes into decisions, and refuse the ones that are wishes.

    Three things make an entry a decision rather than a note: a named
    owner, a deliverable, and a check a third party could run without
    asking the owner whether they did it. An entry missing any of the three
    is refused with the specific gap named, and no partial artifact is
    emitted, because a half filled decision in a ledger reads as a decision
    that was made.
    """
    lines = [line for line in str(raw_notes or "").splitlines()
             if line.strip()]
    entries: list[DecisionEntry] = []
    unparsed: list[str] = []

    for line in lines:
        match = DECISION_PATTERN.match(line)
        if not match:
            unparsed.append(line.strip())
            continue

        owner = match.group("owner").strip()
        deliverable = match.group("deliverable").strip().rstrip(".")
        due = (match.group("due") or "").strip().rstrip(".")
        marker, verifiable = _check_marker(deliverable)
        findings: list[Finding] = []

        vague = [word for word in UNVERIFIABLE_WORDS
                 if word in deliverable.lower()]

        if not verifiable:
            findings.append(Finding(
                code="DEC-CHECK", severity=SEVERITY_CRITICAL,
                title=f"{owner} has no check anyone else could run",
                detail=("The deliverable names no artifact, no system, and "
                        "no number, so the only way to know whether it "
                        "happened is to ask the person who owns it. That is "
                        "not a status, it is a conversation."
                        + (f" The wording leans on {vague[0]!r}, which "
                           f"describes effort rather than a result."
                           if vague else "")),
                fix=("Name the thing that will exist: a link, a file, a "
                     "ticket, a system to look in, or a number to count.")))
            status = REFUSED
            marker = ""
        else:
            status = LOGGED
            findings.append(Finding(
                code="DEC-OK", severity=SEVERITY_OK,
                title=f"{owner} owns a deliverable with a runnable check",
                detail=("Owner, deliverable, and check are all present, so "
                        "this entry can be closed by someone other than the "
                        "person who took it on."),
                fix=("Put the check in the ledger, not in the follow up "
                     "email, where it will not be found again.")))

        if not due:
            findings.append(Finding(
                code="DEC-DUE", severity=SEVERITY_WARN,
                title="No date was said on the call",
                detail=("A decision without a date is not late until someone "
                        "decides it is, which in practice means never."),
                fix="Ask for the date on the call, not in the recap."))

        entries.append(DecisionEntry(
            line=line.strip(), owner=owner, deliverable=deliverable,
            due=due, check_marker=marker, verifiable=verifiable,
            status=status, findings=tuple(findings)))

    ledger_findings: list[Finding] = []
    if unparsed:
        ledger_findings.append(Finding(
            code="DEC-UNPARSED", severity=SEVERITY_WARN,
            title=f"{len(unparsed)} line(s) named no owner",
            detail=("A line with no name in it is a note, not a decision. It "
                    "is kept and shown rather than dropped, because the "
                    "dangerous version of this tool is the one that silently "
                    "discards what it could not read."),
            fix=("Say the name out loud on the call: who is doing it, not "
                 "that it will get done.")))

    logged = sum(1 for e in entries if e.logged)
    refused = sum(1 for e in entries if not e.logged)
    return DecisionLedger(
        entries=tuple(entries), unparsed=tuple(unparsed), logged=logged,
        refused=refused, lines_in=len(lines),
        findings=tuple(ledger_findings))


SAMPLE_NOTES = (
    "Yinka will send the revised pricing sheet as pricing_v3.xlsx by Friday\n"
    "Sam to update the HubSpot deal stage for the three open renewals\n"
    "Dele will look at the onboarding thing by next week\n"
    "We agreed the new positioning is stronger\n"
    "Priya owns RVP-482 through to close"
)
