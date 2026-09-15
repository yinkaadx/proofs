"""Premium creator membership engine.

Four things decide whether a paid publication holds together: whether the
paywall actually withholds anything, whether a private feed can be traced when
it leaks, whether a member can see where they are in a challenge, and whether a
failed card takes their access away on the wrong day.

Pure logic, no Streamlit import, so this is unit testable on its own and could
sit behind the real site. Deterministic: nothing reads the clock or a random
source, so a preview renders identically in a test and in the console.

The one that matters most is the first. The common way to build a paywall is to
send the whole article to the browser and hide the rest with CSS, or to blur it
and put a modal on top. That is not a paywall. It is a decoration over a
document anyone can read with View Source, and it is why so many publications
leak. The gate here truncates before the response leaves, so the withheld
paragraphs are never in the payload at all.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------

TIER_ANONYMOUS = "anonymous"
TIER_FREE = "free"
TIER_MEMBER = "member"
TIER_FOUNDING = "founding"

TIERS: tuple = (TIER_ANONYMOUS, TIER_FREE, TIER_MEMBER, TIER_FOUNDING)

# Ordered, so an entitlement check is a comparison rather than a list of cases.
# A founding member gets everything a member gets without anybody writing that
# down twice.
TIER_LEVEL = {name: index for index, name in enumerate(TIERS)}

TIER_LABEL = {
    TIER_ANONYMOUS: "Not signed in",
    TIER_FREE: "Free account",
    TIER_MEMBER: "Member",
    TIER_FOUNDING: "Founding member",
}

# How many paragraphs of a premium article each tier may read. The free tier
# gets a real excerpt rather than a sentence, because an excerpt that sells
# nothing converts nobody.
TIER_PARAGRAPHS = {
    TIER_ANONYMOUS: 1,
    TIER_FREE: 2,
    TIER_MEMBER: None,      # None means the whole thing
    TIER_FOUNDING: None,
}


def tier_level(tier: str) -> int:
    """Unknown tiers sort as the least privileged rather than crashing."""
    return TIER_LEVEL.get(tier, -1)


def has_access(tier: str, required: str) -> bool:
    return tier_level(tier) >= tier_level(required) >= 0


# ---------------------------------------------------------------------------
# Part one: the paywall preview
# ---------------------------------------------------------------------------

ARTICLE_TITLE = "The quiet economics of a thousand true fans"
ARTICLE_DECK = ("Why a small paid audience beats a large free one, and what it "
                "costs to keep.")

ARTICLE_BODY: tuple = (
    "Every creator eventually meets the same arithmetic. A hundred thousand "
    "followers who pay nothing are worth less than a thousand who pay ten "
    "pounds a month, and the second group is far harder to lose.",
    "The difficulty is that the two audiences want different things. The free "
    "audience wants to be entertained on the way to somewhere else. The paid "
    "audience wants to be somewhere, and the publication has to be a place "
    "rather than a feed.",
    "That distinction decides the product. A place has a front door, a room "
    "people recognise, and a reason to come back on a Tuesday when nothing is "
    "happening. A feed has an algorithm, and the algorithm is not yours.",
    "So the first architectural decision is not the paywall. It is what a "
    "member gets that cannot be screenshotted: a private feed, a running "
    "cohort, a reply from a person. Those are the things that survive being "
    "shared, because sharing them does not transfer them.",
    "The paywall matters second, and it matters technically rather than "
    "commercially. A gate that hides text in the browser is not a gate, and a "
    "publication that leaks its archive has no product left to sell.",
)

MODAL_NONE = "none"
MODAL_SIGN_UP = "sign_up"
MODAL_UPGRADE = "upgrade"


@dataclass
class PaywallPreview:
    tier: str
    visible: list = field(default_factory=list)
    withheld_count: int = 0
    gated: bool = False
    modal: str = MODAL_NONE
    call_to_action: str = ""
    reason: str = ""

    @property
    def total_paragraphs(self) -> int:
        return len(ARTICLE_BODY)

    @property
    def visible_count(self) -> int:
        return len(self.visible)

    @property
    def payload(self) -> dict:
        """Exactly what the server sends. The withheld text is not in here.

        This property is the whole proof. A test can assert that no withheld
        sentence appears anywhere in the response, which is a claim a CSS based
        paywall could never pass.
        """
        return {
            "title": ARTICLE_TITLE,
            "deck": ARTICLE_DECK,
            "tier": self.tier,
            "paragraphs": list(self.visible),
            "withheld_paragraph_count": self.withheld_count,
            "gated": self.gated,
            "modal": self.modal,
        }

    def pretty_payload(self) -> str:
        return json.dumps(self.payload, indent=2)

    def rows(self) -> list:
        return [
            {"Paragraph": str(index + 1),
             "Delivered": "yes" if index < self.visible_count else "no",
             "Opening": (ARTICLE_BODY[index][:48] + "..."
                         if index < self.visible_count else "(not sent)")}
            for index in range(self.total_paragraphs)
        ]


def simulate_paywall_preview(user_tier: str = TIER_FREE) -> PaywallPreview:
    """Build the response for one reader, gating before it is serialised.

    The allowance is applied by slicing the source, not by marking paragraphs
    hidden, so the withheld text has no path into the payload. A caller cannot
    accidentally leak it because it was never handed to them.
    """
    tier = user_tier if user_tier in TIERS else TIER_ANONYMOUS
    allowance = TIER_PARAGRAPHS[tier]

    if allowance is None:
        return PaywallPreview(
            tier=tier, visible=list(ARTICLE_BODY), withheld_count=0,
            gated=False, modal=MODAL_NONE, call_to_action="",
            reason=(f"{TIER_LABEL[tier]} entitlement covers the full archive, "
                    f"so the whole article is sent."))

    visible = list(ARTICLE_BODY[:allowance])
    withheld = len(ARTICLE_BODY) - len(visible)
    if tier == TIER_ANONYMOUS:
        modal, cta = MODAL_SIGN_UP, "Create a free account to read on"
        reason = ("Not signed in, so one paragraph is sent as an excerpt and "
                  "the rest never leaves the server.")
    else:
        modal, cta = MODAL_UPGRADE, "Become a member to read the full archive"
        reason = ("A free account gets a real excerpt rather than a sentence, "
                  "because an excerpt that sells nothing converts nobody.")

    return PaywallPreview(tier=tier, visible=visible, withheld_count=withheld,
                          gated=True, modal=modal, call_to_action=cta,
                          reason=reason)


def paywall_comparison() -> list:
    """Every tier side by side, so the gate is visible as a number."""
    rows = []
    for tier in TIERS:
        preview = simulate_paywall_preview(tier)
        rows.append({
            "Tier": TIER_LABEL[tier],
            "Paragraphs sent": f"{preview.visible_count} of "
                               f"{preview.total_paragraphs}",
            "Withheld": str(preview.withheld_count),
            "Modal": preview.modal,
        })
    return rows


# ---------------------------------------------------------------------------
# Part two: the private audio feed
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Episode:
    number: int
    title: str
    duration_seconds: int
    topics: tuple
    required_tier: str
    summary: str

    @property
    def duration(self) -> str:
        minutes, seconds = divmod(self.duration_seconds, 60)
        return f"{minutes}:{seconds:02d}"

    def available_to(self, tier: str) -> bool:
        return has_access(tier, self.required_tier)


PRIVATE_EPISODES: tuple = (
    Episode(1, "Pricing a membership without guessing", 1_842,
            ("pricing", "positioning"), TIER_MEMBER,
            "Working through three real price ladders and why the middle tier "
            "usually does the selling."),
    Episode(2, "The first hundred members are not a funnel", 2_215,
            ("growth", "community"), TIER_MEMBER,
            "Why cohort one has to be recruited by hand, and what that buys "
            "you later."),
    Episode(3, "Writing an archive people pay to keep", 1_567,
            ("craft", "retention"), TIER_MEMBER,
            "Evergreen against timely, and the maintenance cost of each."),
    Episode(4, "Reading the churn report without flinching", 2_930,
            ("retention", "metrics"), TIER_FOUNDING,
            "Involuntary against voluntary churn, and why the first is a "
            "billing problem rather than a product one."),
    Episode(5, "A live teardown of a member onboarding email", 1_284,
            ("craft", "onboarding"), TIER_FOUNDING,
            "Line by line, with the version that doubled activation."),
)

FEED_HOST = "https://feeds.example-press.com"


def feed_token(member_id: str) -> str:
    """A per member token, so a leaked feed points at whoever leaked it.

    Derived rather than random, which is what makes it reproducible here. In
    production it would be stored, not recomputed, so it can be revoked without
    changing the member's id.
    """
    return hashlib.sha256(f"feed:{member_id}".encode("utf-8")).hexdigest()[:20]


def get_private_audio_feed(tier: str = TIER_MEMBER,
                           member_id: str = "mem_8241") -> dict:
    """The private podcast feed, filtered to what this tier may play.

    A private feed is not a public feed with a password. Every member gets
    their own URL, so a feed found in the wild identifies the account it came
    from and can be revoked without disturbing anybody else.
    """
    episodes = [ep for ep in PRIVATE_EPISODES if ep.available_to(tier)]
    locked = [ep for ep in PRIVATE_EPISODES if not ep.available_to(tier)]
    token = feed_token(member_id)
    return {
        "member_id": member_id,
        "tier": tier,
        "feed_url": f"{FEED_HOST}/private/{token}/rss.xml",
        "episodes": episodes,
        "locked": locked,
        "total_seconds": sum(ep.duration_seconds for ep in episodes),
        "revocable": True,
    }


def feed_rows(tier: str = TIER_MEMBER) -> list:
    feed = get_private_audio_feed(tier)
    rows = []
    for episode in PRIVATE_EPISODES:
        playable = episode in feed["episodes"]
        rows.append({
            "No.": str(episode.number),
            "Episode": episode.title,
            "Duration": episode.duration,
            "Topics": ", ".join(episode.topics),
            "Needs": TIER_LABEL[episode.required_tier],
            "Playable": "yes" if playable else "no",
        })
    return rows


def feed_runtime(tier: str = TIER_MEMBER) -> str:
    total = get_private_audio_feed(tier)["total_seconds"]
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


# ---------------------------------------------------------------------------
# Part three: challenge progress
# ---------------------------------------------------------------------------

MILESTONE_NOT_STARTED = "Not started"
MILESTONE_UNDER_WAY = "Under way"
MILESTONE_QUARTER = "Quarter done"
MILESTONE_HALFWAY = "Halfway"
MILESTONE_HOME_STRAIGHT = "Home straight"
MILESTONE_COMPLETE = "Complete"

# Thresholds as a proportion, checked from the top down so a value sits in
# exactly one band.
MILESTONES: tuple = (
    (1.0, MILESTONE_COMPLETE),
    (0.75, MILESTONE_HOME_STRAIGHT),
    (0.5, MILESTONE_HALFWAY),
    (0.25, MILESTONE_QUARTER),
    (0.0001, MILESTONE_UNDER_WAY),
    (0.0, MILESTONE_NOT_STARTED),
)


@dataclass(frozen=True)
class ChallengeProgress:
    completed_days: int
    total_days: int
    percentage: float
    milestone: str
    days_remaining: int

    @property
    def complete(self) -> bool:
        return self.milestone == MILESTONE_COMPLETE

    @property
    def tone(self) -> str:
        if self.complete:
            return "ok"
        return "warn" if self.percentage < 25 else "info"

    def rows(self) -> list:
        return [
            {"Measure": "Days completed", "Value": str(self.completed_days)},
            {"Measure": "Days in the challenge", "Value": str(self.total_days)},
            {"Measure": "Days remaining", "Value": str(self.days_remaining)},
            {"Measure": "Progress", "Value": f"{self.percentage:.1f}%"},
            {"Measure": "Milestone", "Value": self.milestone},
        ]


def track_challenge_progress(completed_days: int = 12,
                             total_days: int = 30) -> ChallengeProgress:
    """Where a member is in a challenge, without ever reporting nonsense.

    Two guards, both because the inputs come from a database rather than from a
    form. A total of zero would divide by zero, and a completed count above the
    total would report progress over a hundred percent, which is the kind of
    number that makes a member distrust everything else on the page.
    """
    total = max(int(total_days), 0)
    completed = max(int(completed_days), 0)
    if total == 0:
        return ChallengeProgress(0, 0, 0.0, MILESTONE_NOT_STARTED, 0)

    completed = min(completed, total)
    ratio = completed / total
    percentage = round(ratio * 100, 2)
    milestone = next(name for threshold, name in MILESTONES
                     if ratio >= threshold)
    return ChallengeProgress(completed, total, percentage, milestone,
                             total - completed)


# ---------------------------------------------------------------------------
# Part four: the Stripe membership webhook
# ---------------------------------------------------------------------------

EVENT_SUBSCRIPTION_CREATED = "customer.subscription.created"
EVENT_INVOICE_PAID = "invoice.paid"
EVENT_PAYMENT_FAILED = "invoice.payment_failed"
EVENT_SUBSCRIPTION_DELETED = "customer.subscription.deleted"
EVENT_REFUNDED = "charge.refunded"

WEBHOOK_EVENTS: tuple = (EVENT_SUBSCRIPTION_CREATED, EVENT_INVOICE_PAID,
                         EVENT_PAYMENT_FAILED, EVENT_SUBSCRIPTION_DELETED,
                         EVENT_REFUNDED)

LEDGER_ACTIVE = "active"
LEDGER_PAST_DUE = "past_due"
LEDGER_CANCELED = "canceled"
LEDGER_REFUNDED = "refunded"

ACCESS_GRANTED = "granted"
ACCESS_RETAINED = "retained"
ACCESS_REVOKED = "revoked"


@dataclass(frozen=True)
class WebhookResult:
    event_type: str
    ledger_state: str
    access: str
    tier: str
    note: str
    dunning: bool = False

    @property
    def authorized(self) -> bool:
        return self.access in (ACCESS_GRANTED, ACCESS_RETAINED)

    @property
    def tone(self) -> str:
        if self.access == ACCESS_REVOKED:
            return "crit"
        return "warn" if self.dunning else "ok"

    def rows(self) -> list:
        return [
            {"Field": "Event", "Value": self.event_type},
            {"Field": "Ledger state", "Value": self.ledger_state},
            {"Field": "Access", "Value": self.access},
            {"Field": "Tier after event", "Value": TIER_LABEL[self.tier]},
            {"Field": "In dunning", "Value": "yes" if self.dunning else "no"},
        ]


def simulate_stripe_membership_webhook(
        event_type: str = EVENT_INVOICE_PAID) -> WebhookResult:
    """Decide the ledger state and the access grant for one webhook.

    The important case is the failed payment. The obvious handler revokes
    access the moment a card is declined, and that is wrong: Stripe retries on
    a dunning schedule, most declines clear on the retry, and a member locked
    out over a card that worked two days later cancels on principle. So a
    failure moves the ledger to past due and leaves access in place, and only
    the cancellation that ends dunning takes it away.
    """
    if event_type not in WEBHOOK_EVENTS:
        raise ValueError(f"unhandled event type {event_type!r}")

    if event_type == EVENT_SUBSCRIPTION_CREATED:
        return WebhookResult(
            event_type, LEDGER_ACTIVE, ACCESS_GRANTED, TIER_MEMBER,
            "Subscription opened. Access is granted on the created event so "
            "the member is not staring at a paywall they have just paid to "
            "pass while the first invoice settles.")

    if event_type == EVENT_INVOICE_PAID:
        return WebhookResult(
            event_type, LEDGER_ACTIVE, ACCESS_RETAINED, TIER_MEMBER,
            "Invoice settled and the period rolls forward. If the account was "
            "past due, this clears it and dunning stops.")

    if event_type == EVENT_PAYMENT_FAILED:
        return WebhookResult(
            event_type, LEDGER_PAST_DUE, ACCESS_RETAINED, TIER_MEMBER,
            "Payment failed, so the ledger goes past due and dunning starts. "
            "Access deliberately stays: most declines clear on a retry, and a "
            "member locked out over a card that worked two days later cancels "
            "on principle.", dunning=True)

    if event_type == EVENT_SUBSCRIPTION_DELETED:
        return WebhookResult(
            event_type, LEDGER_CANCELED, ACCESS_REVOKED, TIER_FREE,
            "Subscription ended, either by the member or because dunning ran "
            "out. This is the event that removes access, and the account drops "
            "to free rather than being deleted.")

    return WebhookResult(
        event_type, LEDGER_REFUNDED, ACCESS_REVOKED, TIER_FREE,
        "Charge refunded, so the period that was paid for no longer is. "
        "Access is removed and the account drops to free.")


def webhook_ledger() -> list:
    """Every handled event and what it does, as one readable table."""
    return [
        {
            "Event": result.event_type,
            "Ledger": result.ledger_state,
            "Access": result.access,
            "Still authorised": "yes" if result.authorized else "no",
        }
        for result in (simulate_stripe_membership_webhook(event)
                       for event in WEBHOOK_EVENTS)
    ]


def membership_summary() -> dict:
    """The numbers a creator would put on a dashboard."""
    member_feed = get_private_audio_feed(TIER_MEMBER)
    founding_feed = get_private_audio_feed(TIER_FOUNDING)
    return {
        "article_paragraphs": len(ARTICLE_BODY),
        "free_sees": simulate_paywall_preview(TIER_FREE).visible_count,
        "episodes_total": len(PRIVATE_EPISODES),
        "episodes_for_member": len(member_feed["episodes"]),
        "episodes_for_founding": len(founding_feed["episodes"]),
        "events_handled": len(WEBHOOK_EVENTS),
        "events_revoking": len([e for e in WEBHOOK_EVENTS
                                if not simulate_stripe_membership_webhook(e)
                                .authorized]),
    }
