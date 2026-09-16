"""TOH Systems and Commerce Launch Console: the engine.

Three questions a launch has to answer before it goes live, and none of them
is a matter of opinion once the rules are written down.

1. Which system owns each entity. Two systems that both believe they own the
   customer record will both write to it, and the last writer wins by accident
   rather than by design. A system of record map names one owner per entity
   and makes every other system a reader.
2. Whether the dashboard on the wall is telling the truth. A number that was
   correct forty minutes ago is not a number, it is a memory, and a count that
   disagrees with its source is worse than no count at all.
3. Whether the customer care agent may send what it drafted. A drafted reply
   is a suggestion. Only the lowest risk path in the policy is allowed to
   leave without a person reading it first, and a kill switch stops every
   path at once.

No Streamlit import lives here on purpose, so the same engine could sit behind
a real integration without a line changing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# 1. System of record map
# ---------------------------------------------------------------------------

DIRECTION_ONE_WAY = "one way out"
DIRECTION_TWO_WAY = "two way"
DIRECTION_READ_ONLY = "read only"


@dataclass(frozen=True)
class OwnedEntity:
    """One entity, one owner, and everyone else reading rather than writing."""

    entity: str
    master_system: str
    sync_direction: str
    readers: tuple[str, ...]
    key_field: str
    conflict_rule: str


SYSTEM_OF_RECORD: tuple[OwnedEntity, ...] = (
    OwnedEntity(
        entity="Customer",
        master_system="CRM",
        sync_direction=DIRECTION_ONE_WAY,
        readers=("Storefront", "Booking", "Learning", "Support desk"),
        key_field="customer_id",
        conflict_rule=(
            "The CRM record wins. A storefront edit raises a change request "
            "on the CRM record and never writes back."
        ),
    ),
    OwnedEntity(
        entity="Order",
        master_system="Commerce platform",
        sync_direction=DIRECTION_ONE_WAY,
        readers=("CRM", "Finance", "Support desk", "Warehouse"),
        key_field="order_id",
        conflict_rule=(
            "The commerce platform wins. Finance reads totals and never "
            "edits a line, because an edited line breaks the payment record."
        ),
    ),
    OwnedEntity(
        entity="Inventory",
        master_system="Warehouse system",
        sync_direction=DIRECTION_TWO_WAY,
        readers=("Commerce platform", "Support desk"),
        key_field="sku",
        conflict_rule=(
            "The warehouse holds the count and the commerce platform holds "
            "the reservation. A sale reserves, a pick confirms, and only a "
            "confirmed pick moves the master count."
        ),
    ),
    OwnedEntity(
        entity="Booking",
        master_system="Booking system",
        sync_direction=DIRECTION_ONE_WAY,
        readers=("CRM", "Commerce platform", "Support desk"),
        key_field="booking_ref",
        conflict_rule=(
            "The booking system owns the calendar. A CRM note never moves a "
            "slot, because two systems moving slots is a double booking."
        ),
    ),
    OwnedEntity(
        entity="Learning",
        master_system="Learning platform",
        sync_direction=DIRECTION_ONE_WAY,
        readers=("CRM", "Support desk"),
        key_field="enrolment_id",
        conflict_rule=(
            "The learning platform owns progress and completion. The CRM "
            "shows the badge and cannot award it."
        ),
    ),
    OwnedEntity(
        entity="Support",
        master_system="Support desk",
        sync_direction=DIRECTION_READ_ONLY,
        readers=("CRM",),
        key_field="ticket_id",
        conflict_rule=(
            "The support desk owns the ticket thread. The CRM shows the last "
            "ticket state and writes nothing, so no reply is ever duplicated."
        ),
    ),
)


def get_system_of_record_map() -> tuple[OwnedEntity, ...]:
    """Entity ownership, master system and sync direction, one row per entity.

    Returned as a tuple because the map is a decision, not a working list.
    Nothing downstream should be able to append an entity with no owner.
    """
    return SYSTEM_OF_RECORD


def owner_of(entity: str) -> OwnedEntity | None:
    """The single owning system for an entity, matched without case fuss."""
    wanted = str(entity or "").strip().lower()
    for row in SYSTEM_OF_RECORD:
        if row.entity.lower() == wanted:
            return row
    return None


def unowned_entities(entities: tuple[str, ...]) -> tuple[str, ...]:
    """Entities a dashboard wants to show that no system has been given."""
    return tuple(name for name in entities if owner_of(name) is None)


# ---------------------------------------------------------------------------
# 2. Stale data prevention
# ---------------------------------------------------------------------------

STATUS_FRESH = "FRESH"
STATUS_LAGGING = "LAGGING"
STATUS_STALE = "STALE"
STATUS_DIVERGENT = "DIVERGENT"

COLOR_OK = "ok"
COLOR_WARN = "warn"
COLOR_CRIT = "crit"

# A tile refreshed inside this window is current enough to act on.
FRESH_WITHIN_MINUTES = 15
# Past this the tile is not late, it is wrong, and it should say so itself.
STALE_AFTER_MINUTES = 60


@dataclass(frozen=True)
class FreshnessReading:
    source: str
    status: str
    alert_color: str
    last_sync_minutes_ago: int
    source_count: int
    target_count: int
    variance: int
    variance_pct: float
    headline: str
    fix: str

    @property
    def blocks_launch(self) -> bool:
        """A red tile is a launch blocker. An amber one is a watch item."""
        return self.alert_color == COLOR_CRIT


def evaluate_dashboard_freshness(source_name: str, last_sync_minutes_ago: int,
                                 source_count: int,
                                 target_count: int) -> FreshnessReading:
    """Rate one dashboard tile against its own source.

    Two failures, kept apart because they need different fixes. A tile can be
    late, which is a scheduling problem, and it can disagree with its source,
    which is a pipeline problem. A tile that disagrees is reported as divergent
    even when it synced a minute ago, because a fresh wrong number is the one
    a person acts on.
    """
    minutes = int(last_sync_minutes_ago)
    source_total = int(source_count)
    target_total = int(target_count)
    if minutes < 0:
        raise ValueError("last_sync_minutes_ago cannot be negative")
    if source_total < 0 or target_total < 0:
        raise ValueError("a record count cannot be negative")

    variance = target_total - source_total
    variance_pct = (abs(variance) / source_total * 100) if source_total else (
        100.0 if variance else 0.0)
    name = str(source_name or "").strip() or "unnamed source"
    age = _age_phrase(minutes)

    if variance:
        direction = "more" if variance > 0 else "fewer"
        return FreshnessReading(
            source=name, status=STATUS_DIVERGENT, alert_color=COLOR_CRIT,
            last_sync_minutes_ago=minutes, source_count=source_total,
            target_count=target_total, variance=variance,
            variance_pct=round(variance_pct, 2),
            headline=(f"{name} shows {abs(variance)} {direction} records than "
                      f"its source, a gap of {variance_pct:.2f} percent"),
            fix=("Stop trusting this tile and replay the sync for the window "
                 "since the last agreed count. A tile that disagrees with its "
                 "source is not late, it is wrong, and the dashboard should "
                 "grey it out rather than paint a number nobody can act on."),
        )

    if minutes > STALE_AFTER_MINUTES:
        return FreshnessReading(
            source=name, status=STATUS_STALE, alert_color=COLOR_CRIT,
            last_sync_minutes_ago=minutes, source_count=source_total,
            target_count=target_total, variance=0, variance_pct=0.0,
            headline=f"{name} last synced {age}, past the {STALE_AFTER_MINUTES} minute limit",
            fix=("Show the sync time on the tile itself and grey the number "
                 "out past the limit. A number with no timestamp is read as "
                 "current no matter how old it is."),
        )

    if minutes > FRESH_WITHIN_MINUTES:
        return FreshnessReading(
            source=name, status=STATUS_LAGGING, alert_color=COLOR_WARN,
            last_sync_minutes_ago=minutes, source_count=source_total,
            target_count=target_total, variance=0, variance_pct=0.0,
            headline=f"{name} last synced {age}, behind the {FRESH_WITHIN_MINUTES} minute target",
            fix=("Fine for a daily figure, not for stock on hand. Raise the "
                 "sync frequency for any tile a person acts on within the "
                 "hour, and leave the rest where they are."),
        )

    return FreshnessReading(
        source=name, status=STATUS_FRESH, alert_color=COLOR_OK,
        last_sync_minutes_ago=minutes, source_count=source_total,
        target_count=target_total, variance=0, variance_pct=0.0,
        headline=f"{name} synced {age} and agrees with its source",
        fix="Nothing to do. Keep the sync time printed on the tile.",
    )


def _age_phrase(minutes: int) -> str:
    if minutes == 0:
        return "less than a minute ago"
    if minutes == 1:
        return "1 minute ago"
    if minutes < 90:
        return f"{minutes} minutes ago"
    return f"{minutes / 60:.1f} hours ago"


SAMPLE_TILES: tuple[tuple[str, int, int, int], ...] = (
    ("Orders today", 4, 1_284, 1_284),
    ("Stock on hand", 38, 9_612, 9_612),
    ("Bookings this week", 12, 317, 317),
    ("Course completions", 95, 2_041, 2_041),
    ("Open tickets", 6, 148, 151),
)


def dashboard_board(tiles: tuple[tuple[str, int, int, int], ...] = SAMPLE_TILES
                    ) -> tuple[FreshnessReading, ...]:
    """Every tile rated, worst first, so the board reads as a queue of work."""
    readings = [evaluate_dashboard_freshness(*tile) for tile in tiles]
    rank = {COLOR_CRIT: 0, COLOR_WARN: 1, COLOR_OK: 2}
    readings.sort(key=lambda r: (rank[r.alert_color], -r.last_sync_minutes_ago))
    return tuple(readings)


# ---------------------------------------------------------------------------
# 3. Supervised customer care agent pilot
# ---------------------------------------------------------------------------

CONDITION_UNOPENED = "unopened"
CONDITION_OPENED = "opened"
CONDITION_DAMAGED = "damaged on arrival"
CONDITION_USED = "used"

ITEM_CONDITIONS: tuple[str, ...] = (
    CONDITION_UNOPENED, CONDITION_OPENED, CONDITION_DAMAGED, CONDITION_USED)

# The published policy, in the two numbers it actually turns on.
RETURN_WINDOW_DAYS = 30
DAMAGE_CLAIM_WINDOW_DAYS = 90

KILL_SWITCH_ARMED = "ARMED"
KILL_SWITCH_ENGAGED = "ENGAGED"


@dataclass(frozen=True)
class AgentDecision:
    order_id: str
    policy_code: str
    eligible: bool
    outcome: str
    drafted_response: str
    requires_human_approval: bool
    auto_send_allowed: bool
    kill_switch: str
    guardrails: tuple[str, ...] = field(default_factory=tuple)

    @property
    def sends_without_a_person(self) -> bool:
        """The only property the pilot is judged on. False is the safe answer."""
        return self.auto_send_allowed and not self.requires_human_approval


def simulate_supervised_agent_pilot(order_id: str, days_since_delivery: int,
                                    item_condition: str,
                                    kill_switch_engaged: bool = False
                                    ) -> AgentDecision:
    """Draft a customer care reply and decide whether a person must read it.

    The pilot is supervised by construction. The agent always drafts. Exactly
    one path may leave without a person reading it first: an unopened item
    inside the published return window, where the policy has no judgement in
    it at all. Every refusal, every goodwill call and every damage claim goes
    to a person, because a refusal sent by a machine is the one a customer
    screenshots.
    """
    reference = str(order_id or "").strip() or "unknown order"
    days = int(days_since_delivery)
    if days < 0:
        raise ValueError("days_since_delivery cannot be negative")
    condition = str(item_condition or "").strip().lower()
    if condition not in ITEM_CONDITIONS:
        raise ValueError(f"unknown item condition: {item_condition!r}")

    switch = KILL_SWITCH_ENGAGED if kill_switch_engaged else KILL_SWITCH_ARMED
    guardrails: list[str] = []

    if condition == CONDITION_DAMAGED:
        eligible = days <= DAMAGE_CLAIM_WINDOW_DAYS
        code = "CARE-DAMAGE" if eligible else "CARE-DAMAGE-LATE"
        outcome = ("Replacement or full refund, once the photograph is seen"
                   if eligible else
                   f"Outside the {DAMAGE_CLAIM_WINDOW_DAYS} day damage window")
        draft = (
            f"Hello, thanks for letting us know about order {reference}. "
            "I am sorry it arrived damaged. Could you send one photograph of "
            "the item and the outer box? As soon as I have that I will get a "
            "replacement out to you, or refund you in full if you would "
            "rather." if eligible else
            f"Hello, thanks for writing about order {reference}. Damage "
            f"claims are covered for {DAMAGE_CLAIM_WINDOW_DAYS} days after "
            f"delivery and this one is on day {days}. I would still like to "
            "look at it, so I am passing this to a colleague who can decide.")
        guardrails.append(
            "A damage claim always goes to a person. The agent cannot see the "
            "photograph, so it cannot be the one deciding on it.")
        return AgentDecision(
            order_id=reference, policy_code=code, eligible=eligible,
            outcome=outcome, drafted_response=_held(draft, kill_switch_engaged),
            requires_human_approval=True, auto_send_allowed=False,
            kill_switch=switch, guardrails=tuple(
                guardrails + _switch_note(kill_switch_engaged)))

    if condition == CONDITION_USED:
        guardrails.append(
            "A refusal is never sent by the agent. A person reads every no, "
            "because a no is the message a customer screenshots.")
        draft = (
            f"Hello, thanks for writing about order {reference}. Returns "
            "cover items that have not been used, and the notes on this one "
            "say it has been. I am passing this to a colleague to look at "
            "properly before we come back to you.")
        return AgentDecision(
            order_id=reference, policy_code="CARE-USED", eligible=False,
            outcome="Outside the returns policy, held for a person",
            drafted_response=_held(draft, kill_switch_engaged),
            requires_human_approval=True, auto_send_allowed=False,
            kill_switch=switch, guardrails=tuple(
                guardrails + _switch_note(kill_switch_engaged)))

    if days > RETURN_WINDOW_DAYS:
        guardrails.append(
            "Past the window the only kind answer is a goodwill one, and "
            "goodwill costs money, so a person decides it and not the agent.")
        draft = (
            f"Hello, thanks for writing about order {reference}. Our returns "
            f"window is {RETURN_WINDOW_DAYS} days after delivery and this "
            f"order is on day {days}. I do not want to close the door on it, "
            "so a colleague is looking at what we can do and will come back "
            "to you today.")
        return AgentDecision(
            order_id=reference, policy_code="CARE-WINDOW", eligible=False,
            outcome=f"Day {days}, past the {RETURN_WINDOW_DAYS} day window",
            drafted_response=_held(draft, kill_switch_engaged),
            requires_human_approval=True, auto_send_allowed=False,
            kill_switch=switch, guardrails=tuple(
                guardrails + _switch_note(kill_switch_engaged)))

    if condition == CONDITION_OPENED:
        guardrails.append(
            "An opened item is an exchange or a credit, and choosing between "
            "them is a judgement call, so it is a person's call.")
        draft = (
            f"Hello, thanks for writing about order {reference}. You are "
            f"inside the {RETURN_WINDOW_DAYS} day window, so we can sort this "
            "out. Because the item has been opened we can offer an exchange "
            "or store credit. A colleague will confirm which suits you and "
            "send the return label today.")
        return AgentDecision(
            order_id=reference, policy_code="CARE-OPENED", eligible=True,
            outcome="Exchange or store credit, confirmed by a person",
            drafted_response=_held(draft, kill_switch_engaged),
            requires_human_approval=True, auto_send_allowed=False,
            kill_switch=switch, guardrails=tuple(
                guardrails + _switch_note(kill_switch_engaged)))

    guardrails.append(
        "The one automatic path in the pilot: unopened, inside the window, "
        "no judgement left in the policy for the agent to get wrong.")
    draft = (
        f"Hello, thanks for writing about order {reference}. You are inside "
        f"the {RETURN_WINDOW_DAYS} day return window and the item is "
        "unopened, so that is a straight refund. Your return label is "
        "attached, and the refund goes back to your original payment method "
        "within five working days of the parcel reaching us.")
    if kill_switch_engaged:
        guardrails.append(
            "The kill switch is engaged, so even this path is held. That is "
            "the whole point of the switch: one control stops every send.")
    return AgentDecision(
        order_id=reference, policy_code="CARE-AUTO", eligible=True,
        outcome=f"Refund and return label, day {days} of {RETURN_WINDOW_DAYS}",
        drafted_response=_held(draft, kill_switch_engaged),
        requires_human_approval=bool(kill_switch_engaged),
        auto_send_allowed=not kill_switch_engaged,
        kill_switch=switch, guardrails=tuple(guardrails))


def _held(draft: str, engaged: bool) -> str:
    if not engaged:
        return draft
    return ("HELD BY KILL SWITCH, nothing was sent. The draft was: " + draft)


def _switch_note(engaged: bool) -> list[str]:
    if not engaged:
        return []
    return ["The kill switch is engaged, so no draft leaves on any path."]


SAMPLE_CASES: tuple[tuple[str, int, str], ...] = (
    ("TOH-10241", 4, CONDITION_UNOPENED),
    ("TOH-10255", 9, CONDITION_OPENED),
    ("TOH-10262", 2, CONDITION_DAMAGED),
    ("TOH-10288", 41, CONDITION_UNOPENED),
    ("TOH-10301", 6, CONDITION_USED),
)


def pilot_supervision_rate(cases: tuple[tuple[str, int, str], ...] = SAMPLE_CASES,
                           kill_switch_engaged: bool = False) -> dict:
    """How much of the sample the pilot hands to a person, stated as a number.

    A pilot that claims supervision without measuring it is a slogan. This
    counts the cases and the totals equal the rows.
    """
    decisions = [simulate_supervised_agent_pilot(ref, days, condition,
                                                 kill_switch_engaged)
                 for ref, days, condition in cases]
    supervised = sum(1 for d in decisions if d.requires_human_approval)
    automatic = sum(1 for d in decisions if d.sends_without_a_person)
    total = len(decisions)
    assert supervised + automatic == total, "every case is one or the other"
    return {
        "total": total,
        "supervised": supervised,
        "automatic": automatic,
        "supervised_pct": round(supervised / total * 100, 1) if total else 0.0,
        "decisions": tuple(decisions),
    }
