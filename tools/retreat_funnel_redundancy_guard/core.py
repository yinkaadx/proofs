"""Retreat funnel redundancy engine.

A retreat sells from a webinar. A lead comes in by DM, gets tagged, registers,
receives a Zoom link, shows up, and is made an offer. Every one of those steps
is a webhook, and a webhook that fails quietly costs a seat: the lead is not
angry, they simply never got the link, and nobody finds out until the show up
rate is read a week later.

So this engine does three things from one run:

  It builds the incoming lead and assigns its tags, because the tags decide
  every automation downstream. It runs the pipeline with a chosen failure
  injected, retries what retrying can fix, and escalates what it cannot to
  Slack before the webinar starts rather than after. And it reads the funnel
  numbers back, flagging a show up rate that has dropped, because a sunk show
  up rate is usually a broken step rather than a bad audience.

The deadline is the point. An alert that arrives after the webinar has started
is a record, not a rescue, so every escalation is measured against the time
left rather than just raised.

Pure logic, no Streamlit import, so this is unit testable on its own and can sit
behind a real FG Funnels webhook. Deterministic: nothing here reads the clock or
a random source unless the caller passes one in.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

ENGINE_VERSION = "1.0.0"

PROGRAM = "Costa Rica Reset Retreat"
TICKET_PRICE = 4800.0
DEPOSIT = 750.0

# ---------------------------------------------------------------------------
# The incoming lead
# ---------------------------------------------------------------------------

SOURCE_DM = "Instagram DM"
SOURCE_COMMENT = "Instagram comment automation"
SOURCE_ADS = "Meta ads lead form"
SOURCE_REFERRAL = "Past attendee referral"
SOURCES: tuple[str, ...] = (SOURCE_DM, SOURCE_COMMENT, SOURCE_ADS,
                            SOURCE_REFERRAL)

READY_NOW = "Ready to book now"
READY_SOON = "Looking at the next cohort"
READY_BROWSING = "Just browsing"
READINESS: tuple[str, ...] = (READY_NOW, READY_SOON, READY_BROWSING)

# Tags drive every automation downstream, so they are assigned by rule rather
# than by hand. A tag applied by hand is a tag that is missing at 2am.
TAG_PROGRAM = "retreat-costa-rica-2026"
TAG_SOURCE = {
    SOURCE_DM: "src-instagram-dm",
    SOURCE_COMMENT: "src-comment-automation",
    SOURCE_ADS: "src-meta-lead-form",
    SOURCE_REFERRAL: "src-referral",
}
TAG_READINESS = {
    READY_NOW: "intent-hot",
    READY_SOON: "intent-warm",
    READY_BROWSING: "intent-nurture",
}
TAG_WEBINAR = "webinar-registered"
TAG_DEPOSIT_ELIGIBLE = "deposit-plan-eligible"
TAG_PRIORITY_CALL = "priority-call-queue"
TAG_REFERRAL_CREDIT = "referral-credit-owed"

FIRST_NAMES = ("Amara", "Priya", "Nadia", "Tomas", "Elena", "Marcus", "Leila",
               "Jonah")
LAST_NAMES = ("Okafor", "Raman", "Haddad", "Silva", "Novak", "Bell", "Aziz",
              "Frost")


@dataclass(frozen=True)
class Lead:
    lead_id: str
    name: str
    handle: str
    email: str
    source: str
    readiness: str
    program: str
    tags: tuple[str, ...]
    message: str
    received_at: str

    @property
    def hot(self) -> bool:
        return TAG_PRIORITY_CALL in self.tags

    def as_payload(self) -> dict:
        """The shape FG Funnels posts to the automation webhook."""
        return {
            "contact_id": self.lead_id,
            "full_name": self.name,
            "instagram_handle": self.handle,
            "email": self.email,
            "source": self.source,
            "custom_fields": {
                "program": self.program,
                "readiness": self.readiness,
                "first_message": self.message,
            },
            "tags": list(self.tags),
            "received_at": self.received_at,
        }


def assign_tags(source: str, readiness: str,
                registered: bool = False) -> tuple[str, ...]:
    """Turn the two answers that matter into the tag set the automations read.

    Kept as a pure function on purpose. Every branch downstream keys off these
    tags, so they are the one thing in the funnel that has to be reproducible
    from the inputs rather than assembled as the lead moves.
    """
    tags = [TAG_PROGRAM, TAG_SOURCE.get(source, "src-unknown"),
            TAG_READINESS.get(readiness, "intent-nurture")]
    if readiness == READY_NOW:
        tags.append(TAG_PRIORITY_CALL)
    if readiness in (READY_NOW, READY_SOON):
        tags.append(TAG_DEPOSIT_ELIGIBLE)
    if source == SOURCE_REFERRAL:
        tags.append(TAG_REFERRAL_CREDIT)
    if registered:
        tags.append(TAG_WEBINAR)
    return tuple(tags)


def generate_lead(rng: random.Random, sequence: int,
                  now: str = "2026-09-12T09:00:00Z",
                  source: str | None = None,
                  readiness: str | None = None) -> Lead:
    """Build a mock inbound DM lead. Seed the rng for a reproducible one."""
    first = rng.choice(FIRST_NAMES)
    last = rng.choice(LAST_NAMES)
    chosen_source = source or rng.choice(SOURCES)
    chosen_readiness = readiness or rng.choice(READINESS)
    handle = f"@{first.lower()}.{last.lower()}"
    return Lead(
        lead_id=f"FG-{sequence:05d}",
        name=f"{first} {last}",
        handle=handle,
        email=f"{first.lower()}.{last.lower()}@example.com",
        source=chosen_source,
        readiness=chosen_readiness,
        program=PROGRAM,
        tags=assign_tags(chosen_source, chosen_readiness),
        message=MESSAGE_BY_READINESS[chosen_readiness],
        received_at=now,
    )


MESSAGE_BY_READINESS = {
    READY_NOW: "Hi, I saw the retreat post. I have been meaning to do this for "
               "two years. How do I hold a place?",
    READY_SOON: "Is there a payment plan for the retreat? I am interested but "
                "the dates are tight for me this time.",
    READY_BROWSING: "What is included in the retreat price?",
}


# ---------------------------------------------------------------------------
# The pipeline and its failure modes
# ---------------------------------------------------------------------------

OK = "Delivered"
RECOVERED = "Recovered on retry"
FAILED = "Failed"
SKIPPED = "Not reached"


@dataclass(frozen=True)
class Step:
    key: str
    label: str
    webhook: str
    detail: str


STEPS: tuple[Step, ...] = (
    Step("contact", "Contact created", "POST /contacts",
         "The DM becomes a contact record in FG Funnels."),
    Step("tags", "Tags applied", "POST /contacts/{id}/tags",
         "Tags decide every automation that follows, so this is the step that "
         "silently breaks the rest."),
    Step("register", "Webinar registration", "POST /webinar/registrants",
         "The lead is added to the webinar the retreat sells from."),
    Step("zoom", "Zoom link sync", "POST /zoom/registrants",
         "Zoom returns the unique join link. Without it the confirmation email "
         "goes out with nothing to click."),
    Step("confirm", "Confirmation email and SMS", "POST /messages/send",
         "Carries the join link and the calendar invite."),
    Step("reminders", "Reminder sequence scheduled", "POST /workflows/enrol",
         "Twenty four hours, one hour and ten minutes before. Most of the show "
         "up rate is made here."),
    Step("offer", "Offer and booking link", "POST /offers/send",
         "The retreat offer and the deposit link go out on the webinar."),
)


def step_by_key(key: str, steps=STEPS) -> Step | None:
    for step in steps:
        if step.key == key:
            return step
    return None


@dataclass(frozen=True)
class FailureMode:
    code: str
    label: str
    step_key: str
    transient: bool          # a retry can fix it
    severity: str            # Critical or High
    consequence: str
    fallback: str


FAILURE_NONE = "none"

FAILURE_MODES: tuple[FailureMode, ...] = (
    FailureMode(
        code="ZOOM_LINK_SYNC_FAILED",
        label="Zoom link sync failed",
        step_key="zoom",
        transient=False,
        severity="Critical",
        consequence="The confirmation email goes out with no join link, so the "
                    "lead registers and then cannot attend. They do not "
                    "complain, they simply do not show up.",
        fallback="Create the Zoom registrant by hand and send the join link "
                 "from the shared inbox before the reminder goes out.",
    ),
    FailureMode(
        code="TAGS_NOT_APPLIED",
        label="Tag assignment failed",
        step_key="tags",
        transient=True,
        severity="Critical",
        consequence="Every automation keys off the tags, so the lead sits in "
                    "the database receiving nothing at all. This is the failure "
                    "nobody sees, because there is no error anywhere.",
        fallback="Apply the tag set by hand from the lead payload, which the "
                 "alert carries in full.",
    ),
    FailureMode(
        code="REMINDER_WORKFLOW_STALLED",
        label="Reminder sequence never scheduled",
        step_key="reminders",
        transient=True,
        severity="High",
        consequence="Registration held, but no reminders go out. Show up rate "
                    "on that cohort falls by roughly half and the webinar looks "
                    "like it underperformed.",
        fallback="Enrol the registrant list in the reminder workflow by hand "
                 "and send the one hour reminder manually.",
    ),
    FailureMode(
        code="OFFER_LINK_DEAD",
        label="Offer and deposit link dead",
        step_key="offer",
        transient=False,
        severity="Critical",
        consequence="The webinar runs, the pitch lands, and the button does "
                    "nothing. This is the most expensive minute in the funnel "
                    "to lose.",
        fallback="Post the backup checkout link in the webinar chat and to "
                 "every attendee by SMS.",
    ),
    FailureMode(
        code="CONFIRMATION_BOUNCED",
        label="Confirmation email bounced",
        step_key="confirm",
        transient=True,
        severity="High",
        consequence="The lead has no join link and no calendar hold, so the "
                    "webinar is not in their day at all.",
        fallback="Resend from the secondary sending domain and send the link "
                 "by SMS as well.",
    ),
)


def failure_by_code(code: str, modes=FAILURE_MODES) -> FailureMode | None:
    for mode in modes:
        if mode.code == code:
            return mode
    return None


@dataclass(frozen=True)
class StepResult:
    step: Step
    status: str
    attempts: int
    elapsed_ms: int
    detail: str

    @property
    def ok(self) -> bool:
        return self.status in (OK, RECOVERED)


@dataclass(frozen=True)
class PipelineRun:
    lead: Lead
    results: list[StepResult]
    failure: FailureMode | None
    started_at: str
    failed_at: str
    total_ms: int
    retries_used: int

    @property
    def delivered(self) -> bool:
        return all(result.ok for result in self.results)

    @property
    def broken_step(self) -> StepResult | None:
        for result in self.results:
            if result.status == FAILED:
                return result
        return None

    def rows(self) -> list[dict]:
        return [
            {"Step": result.step.label, "Webhook": result.step.webhook,
             "Status": result.status, "Attempts": result.attempts,
             "Elapsed ms": result.elapsed_ms, "Detail": result.detail}
            for result in self.results
        ]


STEP_LATENCY_MS = 180
RETRY_BACKOFF_MS = (1000, 4000, 16000)


def run_pipeline(lead: Lead, failure_code: str = FAILURE_NONE,
                 max_retries: int = 3, steps=STEPS) -> PipelineRun:
    """Run every webhook in order, retrying what a retry can fix.

    A transient failure recovers on the last retry it is given, which is the
    honest model: the retry is worth having, and it is also why a run can look
    healthy while taking twenty one seconds longer than it should. A hard
    failure exhausts every retry and stops the pipeline, because sending a
    confirmation email with no join link in it is worse than sending nothing.
    """
    if max_retries < 0:
        raise ValueError("A retry count cannot be negative.")

    failure = failure_by_code(failure_code)
    results: list[StepResult] = []
    elapsed = 0
    retries_used = 0
    broken = False
    failed_at_ms: int | None = None

    for step in steps:
        if broken:
            results.append(StepResult(step, SKIPPED, 0, 0,
                                      "Not reached: the run stopped at the "
                                      "step above."))
            continue

        if failure is None or failure.step_key != step.key:
            elapsed += STEP_LATENCY_MS
            results.append(StepResult(step, OK, 1, elapsed, step.detail))
            continue

        # The failing step. Retry with backoff, and see whether it comes back.
        attempts = 1
        elapsed += STEP_LATENCY_MS
        for index in range(max_retries):
            attempts += 1
            retries_used += 1
            elapsed += RETRY_BACKOFF_MS[min(index, len(RETRY_BACKOFF_MS) - 1)]
            elapsed += STEP_LATENCY_MS
            if failure.transient and index == max_retries - 1:
                results.append(StepResult(
                    step, RECOVERED, attempts, elapsed,
                    f"{failure.label} on the first attempt. Recovered after "
                    f"{attempts - 1} retries, {elapsed} ms behind schedule."))
                break
        else:
            broken = True
            failed_at_ms = elapsed
            if max_retries == 0 and failure.transient:
                detail = (f"{failure.label}. Retries are switched off, so a "
                          f"failure a retry would have cleared is a lost seat. "
                          f"{failure.consequence}")
            else:
                detail = (f"{failure.label}. {attempts} attempt(s), all "
                          f"refused. {failure.consequence}")
            results.append(StepResult(step, FAILED, attempts, elapsed, detail))

    started = _parse(lead.received_at)
    return PipelineRun(
        lead=lead, results=results, failure=failure,
        started_at=lead.received_at,
        failed_at=_format(started + timedelta(
            milliseconds=failed_at_ms if failed_at_ms is not None else elapsed)),
        total_ms=elapsed, retries_used=retries_used)


# ---------------------------------------------------------------------------
# Slack escalation
# ---------------------------------------------------------------------------

CHANNEL_CRITICAL = "#retreat-alerts"
CHANNEL_HIGH = "#retreat-ops"

ALERT_IN_TIME = "Raised in time"
ALERT_TOO_LATE = "Raised too late"
ALERT_NONE = "Nothing to raise"


@dataclass(frozen=True)
class SlackAlert:
    raised: bool
    channel: str
    severity: str
    headline: str
    summary: str
    verdict: str
    minutes_to_webinar: int
    seats_at_risk: int
    revenue_at_risk: float
    fallback: str
    blocks: list[dict] = field(default_factory=list)

    @property
    def in_time(self) -> bool:
        return self.verdict == ALERT_IN_TIME

    def as_payload(self) -> dict:
        return {"channel": self.channel, "text": self.headline,
                "blocks": self.blocks}

    def as_json(self) -> str:
        return json.dumps(self.as_payload(), indent=2)


def escalate(run: PipelineRun, minutes_to_webinar: int = 90,
             seats_at_risk: int = 1,
             ticket_price: float = TICKET_PRICE) -> SlackAlert:
    """Turn a broken run into the Slack message the team can act on.

    An alert that arrives after the webinar has started is a record rather than
    a rescue, so the verdict compares the time the fallback needs against the
    time actually left. That comparison is the whole reason the guard exists.
    """
    broken = run.broken_step
    if broken is None or run.failure is None:
        return SlackAlert(
            raised=False, channel="", severity="", headline="",
            summary="Every webhook in the run was delivered, so there is "
                    "nothing to escalate.",
            verdict=ALERT_NONE, minutes_to_webinar=minutes_to_webinar,
            seats_at_risk=0, revenue_at_risk=0.0, fallback="", blocks=[])

    failure = run.failure
    lead = run.lead
    channel = (CHANNEL_CRITICAL if failure.severity == "Critical"
               else CHANNEL_HIGH)
    revenue = round(seats_at_risk * ticket_price, 2)
    # The fallback is manual, so it needs a person and a few minutes.
    verdict = (ALERT_IN_TIME if minutes_to_webinar >= MANUAL_FALLBACK_MINUTES
               else ALERT_TOO_LATE)

    headline = (f"{failure.severity}: {failure.label} for {lead.name} "
                f"({lead.lead_id})")
    if verdict == ALERT_IN_TIME:
        summary = (f"{minutes_to_webinar} minutes before the webinar, which is "
                   f"enough for the manual fallback. Act now and the seat is "
                   f"kept.")
    else:
        summary = (f"Only {minutes_to_webinar} minutes before the webinar, and "
                   f"the manual fallback needs about "
                   f"{MANUAL_FALLBACK_MINUTES}. Do it anyway, and move the "
                   f"retry budget earlier so the next one is caught sooner.")

    blocks = [
        {"type": "header",
         "text": {"type": "plain_text", "text": headline}},
        {"type": "section",
         "fields": [
             {"type": "mrkdwn", "text": f"*Lead*\n{lead.name} {lead.handle}"},
             {"type": "mrkdwn", "text": f"*Program*\n{lead.program}"},
             {"type": "mrkdwn", "text": f"*Failing step*\n{broken.step.label}"},
             {"type": "mrkdwn", "text": f"*Webhook*\n{broken.step.webhook}"},
             {"type": "mrkdwn",
              "text": f"*Attempts*\n{broken.attempts}, all refused"},
             {"type": "mrkdwn",
              "text": f"*Webinar starts in*\n{minutes_to_webinar} minutes"},
         ]},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*What breaks*\n{failure.consequence}"}},
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*Do this now*\n{failure.fallback}"}},
        {"type": "context",
         "elements": [
             {"type": "mrkdwn",
              "text": f"{failure.code} | {seats_at_risk} seat(s) at risk | "
                      f"{revenue:,.0f} at stake | tags "
                      f"{', '.join(lead.tags)}"},
         ]},
    ]

    return SlackAlert(
        raised=True, channel=channel, severity=failure.severity,
        headline=headline, summary=summary, verdict=verdict,
        minutes_to_webinar=minutes_to_webinar, seats_at_risk=seats_at_risk,
        revenue_at_risk=revenue, fallback=failure.fallback, blocks=blocks)


# How long the manual fallback realistically takes once someone reads the alert.
MANUAL_FALLBACK_MINUTES = 15


# ---------------------------------------------------------------------------
# Funnel metrics
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FunnelMetrics:
    registrations: int = 420
    emails_sent: int = 420
    opens: int = 214
    clicks: int = 63
    show_ups: int = 151
    offers: int = 138
    closes: int = 19

    def _rate(self, numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    @property
    def open_rate(self) -> float:
        return self._rate(self.opens, self.emails_sent)

    @property
    def click_rate(self) -> float:
        return self._rate(self.clicks, self.emails_sent)

    @property
    def show_up_rate(self) -> float:
        return self._rate(self.show_ups, self.registrations)

    @property
    def close_rate(self) -> float:
        return self._rate(self.closes, self.offers)

    @property
    def revenue(self) -> float:
        return round(self.closes * TICKET_PRICE, 2)


@dataclass(frozen=True)
class Thresholds:
    open_rate: float = 0.35
    click_rate: float = 0.08
    show_up_rate: float = 0.40
    close_rate: float = 0.12


DEFAULT_THRESHOLDS = Thresholds()

SEVERITY_CRITICAL = "Critical"
SEVERITY_WARNING = "Warning"
SEVERITY_OK = "On target"


@dataclass(frozen=True)
class MetricFlag:
    name: str
    value: float
    threshold: float
    passing: bool
    severity: str
    note: str

    @property
    def shortfall(self) -> float:
        return round(max(0.0, self.threshold - self.value), 4)


def evaluate_metrics(metrics: FunnelMetrics,
                     thresholds: Thresholds = DEFAULT_THRESHOLDS
                     ) -> list[MetricFlag]:
    """Read the funnel and say which number is the one to act on.

    The show up rate is treated as the load bearing one. When it drops, the
    first suspect is a broken step in the pipeline above rather than a weak
    audience, because a lead who never received a join link is counted here as
    a no show and looks exactly like disinterest.
    """
    checks = [
        ("Open rate", metrics.open_rate, thresholds.open_rate, SEVERITY_WARNING,
         "Opens are a sending reputation and subject line problem before they "
         "are an audience problem."),
        ("Click rate", metrics.click_rate, thresholds.click_rate,
         SEVERITY_WARNING,
         "Clicks below target usually mean the join link is buried or the "
         "reminder went out too far ahead of the webinar."),
        ("Show up rate", metrics.show_up_rate, thresholds.show_up_rate,
         SEVERITY_CRITICAL,
         "Check the reminder sequence and the Zoom link sync before blaming "
         "the audience. A lead who never got a link is counted here as a no "
         "show and looks exactly like disinterest."),
        ("Close rate", metrics.close_rate, thresholds.close_rate,
         SEVERITY_WARNING,
         "A close rate this low with a healthy show up rate points at the "
         "offer or the deposit link rather than the traffic."),
    ]
    flags = []
    for name, value, threshold, severity, note in checks:
        passing = value >= threshold
        flags.append(MetricFlag(
            name=name, value=value, threshold=threshold, passing=passing,
            severity=SEVERITY_OK if passing else severity,
            note="Above target." if passing else note))
    return flags


def metric_rows(flags: list[MetricFlag]) -> list[dict]:
    return [
        {"Metric": flag.name,
         "Actual": f"{flag.value:.1%}",
         "Target": f"{flag.threshold:.1%}",
         "Status": "Pass" if flag.passing else flag.severity,
         "Shortfall": f"{flag.shortfall:.1%}" if not flag.passing else "None"}
        for flag in flags
    ]


def funnel_rows(metrics: FunnelMetrics) -> list[dict]:
    """Stage by stage counts, each with the drop from the stage above it."""
    stages = [
        ("Registered", metrics.registrations),
        ("Email delivered", metrics.emails_sent),
        ("Opened", metrics.opens),
        ("Clicked", metrics.clicks),
        ("Showed up", metrics.show_ups),
        ("Made an offer", metrics.offers),
        ("Closed", metrics.closes),
    ]
    top = stages[0][1] or 1
    rows = []
    previous = None
    for label, count in stages:
        rows.append({
            "Stage": label,
            "People": count,
            "Of registrations": f"{count / top:.1%}",
            "Lost from previous stage": (
                "n/a" if previous is None else str(max(0, previous - count))),
        })
        previous = count
    return rows


def metrics_headline(flags: list[MetricFlag],
                     metrics: FunnelMetrics) -> tuple[str, str]:
    """One line for the top of the dashboard, worst news first."""
    critical = [f for f in flags if not f.passing
                and f.severity == SEVERITY_CRITICAL]
    warnings = [f for f in flags if not f.passing
                and f.severity == SEVERITY_WARNING]
    if critical:
        flag = critical[0]
        seats = round(flag.shortfall * metrics.registrations)
        return (SEVERITY_CRITICAL,
                f"{flag.name} is {flag.value:.1%} against a target of "
                f"{flag.threshold:.1%}. That gap is about {seats} people who "
                f"registered and never arrived.")
    if warnings:
        names = ", ".join(flag.name.lower() for flag in warnings)
        return (SEVERITY_WARNING,
                f"{len(warnings)} metric(s) below target: {names}. Nothing "
                f"here is losing seats today, and each one is worth a look "
                f"before the next cohort.")
    return (SEVERITY_OK,
            f"Every metric is above target and the cohort closed "
            f"{metrics.closes} seats for {metrics.revenue:,.0f}.")


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def _parse(moment: str) -> datetime:
    text = str(moment).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = datetime(2026, 9, 12, 9, 0, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
