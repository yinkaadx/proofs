"""School Bus Routing and Network Analyst Console: the engine.

Three calculations a transportation department is asked for, and the place
each one is usually got wrong.

1. Walk zone eligibility measured as the crow flies. Street distance is never
   shorter than straight line distance, so a straight line rule always counts
   more children as walkers than can actually walk. The ones it miscounts are
   the ones behind a highway with no crossing, and they are the ones a
   parent calls about.
2. Stop consolidation sold as a pure win. Removing stops takes time out of a
   route by moving the walk onto families, and the walk it creates crosses
   the same roads the route was avoiding.
3. Buses saved reported as money saved. A route removed only becomes cash if
   the bus and the driver actually leave the fleet. Where they do not, the
   saving is capacity, which is worth having and is not the same number.

Every model here states its own assumptions in the value it returns, because
the circuity of a street network, the dwell time of a stop and the length of
a school year are local facts rather than constants, and a figure presented
without them is a figure that will be quoted.

No Streamlit import lives here on purpose, so the same engine could sit behind
a real routing system without a line changing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

ENGINE_VERSION = "1.0.0"

TWO_PLACES = Decimal("0.01")

SEVERITY_OK = "OK"
SEVERITY_WARN = "WARN"
SEVERITY_CRITICAL = "CRITICAL"


def money(value) -> Decimal:
    """Currency, rounded once, at the point it becomes currency."""
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


def _worst(findings) -> str:
    for level in (SEVERITY_CRITICAL, SEVERITY_WARN):
        if any(f.severity == level for f in findings):
            return level
    return SEVERITY_OK


# ---------------------------------------------------------------------------
# 1. Street network against straight line
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NetworkType:
    key: str
    label: str
    circuity: float
    barrier_share: float
    note: str


# Circuity is the ratio of street distance to straight line distance. It is
# never below one, because a street cannot be shorter than the line it
# follows, and that single fact decides the direction of every result here.
# These four values are this model's assumptions, not measured constants for
# any district, and they travel with every answer so they can be replaced.
NETWORK_TYPES: tuple[NetworkType, ...] = (
    NetworkType("grid", "Dense grid", 1.20, 0.02,
                "A regular grid gives the shortest detours. Most trips are "
                "two legs of an L rather than a diagonal."),
    NetworkType("mixed", "Mixed urban and suburban", 1.35, 0.05,
                "Grid near the centre, curved streets further out. The "
                "middle case and the most common one."),
    NetworkType("suburban", "Suburban cul de sac", 1.55, 0.09,
                "Loops and dead ends mean a house two hundred feet away can "
                "be half a mile of walking."),
    NetworkType("rural", "Rural and arterial", 1.75, 0.14,
                "Few connections, long blocks, and arterials that can only "
                "be crossed where there is a signal."),
)

NETWORK_BY_KEY = {n.key: n for n in NETWORK_TYPES}
NETWORK_BY_LABEL = {n.label: n for n in NETWORK_TYPES}


@dataclass(frozen=True)
class EligibilitySplit:
    student_count: int
    radius_miles: float
    network_type: NetworkType
    circuity: float
    effective_radius_miles: float
    walkers_straight_line: int
    walkers_network: int
    barrier_isolated: int
    reclassified_to_bus: int
    bus_eligible_network: int
    walk_zone_shrink_pct: float
    headline: str
    assumptions: tuple[str, ...]
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def reconciles(self) -> bool:
        """Every child is in exactly one group. Checked, not asserted."""
        return self.walkers_network + self.bus_eligible_network == self.student_count


def _resolve_network(value) -> NetworkType:
    if isinstance(value, NetworkType):
        return value
    name = str(value or "").strip()
    if name in NETWORK_BY_KEY:
        return NETWORK_BY_KEY[name]
    if name in NETWORK_BY_LABEL:
        return NETWORK_BY_LABEL[name]
    raise ValueError(f"unknown network type: {value!r}")


def calculate_network_vs_euclidean(student_count: int, radius_miles: float,
                                   network_type="mixed") -> EligibilitySplit:
    """How many children a straight line rule counts as walkers who are not.

    The count is taken over students living inside the radius as the crow
    flies, which is the population a straight line rule calls walkers. A
    child is a walker on the street network only if the walk is inside the
    radius once it follows actual streets, which means their straight line
    distance has to be inside the radius divided by the circuity.

    With students spread evenly, the number inside a distance grows with the
    square of that distance, so dividing the reachable radius by the circuity
    divides the walker count by the circuity squared. Then the children cut
    off by a barrier with no crossing come out as well, because distance was
    never their problem.
    """
    count = int(student_count)
    radius = float(radius_miles)
    if count < 0:
        raise ValueError("a school cannot have a negative number of students")
    if radius <= 0:
        raise ValueError("a walk radius has to be above zero")
    if radius > 10:
        raise ValueError("that is not a walk radius, that is a catchment")
    network = _resolve_network(network_type)

    effective_radius = radius / network.circuity
    # Even spread over a disc: the number within a distance goes with the
    # square of that distance, so the walker count divides by circuity squared.
    by_distance = int(round(count / (network.circuity ** 2)))
    barrier_isolated = int(round(count * network.barrier_share))
    walkers_network = max(0, by_distance - barrier_isolated)
    reclassified = count - walkers_network
    shrink = (reclassified / count * 100) if count else 0.0

    findings: list[Finding] = []

    findings.append(Finding(
        code="NET-DIRECTION", severity=SEVERITY_OK,
        title="The walk zone can only shrink, never grow",
        detail=(f"A street is never shorter than the straight line it "
                f"follows, so circuity is never below one and the network "
                f"walker count is never above the straight line one. At "
                f"circuity {network.circuity} the reachable radius is "
                f"{effective_radius:.2f} miles rather than {radius:.2f}."),
        fix=("If a vendor's network analysis returns more walkers than the "
             "straight line count, the analysis is wrong and it is worth "
             "finding out how before anything is published.")))

    if barrier_isolated:
        findings.append(Finding(
            code="NET-BARRIER", severity=SEVERITY_CRITICAL,
            title=f"{barrier_isolated} student(s) are cut off rather than far away",
            detail=("These children are inside the radius by any measure and "
                    "still cannot walk, because the crossing is not there. "
                    "Distance is not their problem and a distance rule will "
                    "never find them."),
            fix=("List them individually and check each one against the "
                 "hazardous route provision rather than against the radius. "
                 "These are the addresses a parent calls about, and the call "
                 "is usually right.")))

    if reclassified:
        findings.append(Finding(
            code="NET-RECLASSIFY", severity=SEVERITY_WARN,
            title=f"{reclassified} student(s) move from walker to bus eligible",
            detail=(f"That is {shrink:.1f} percent of the {count} inside the "
                    f"straight line radius. Every one of them is a seat that "
                    f"a straight line plan did not budget for."),
            fix=("Run this before the route count is committed to a budget, "
                 "not after the first week of calls.")))
    else:
        findings.append(Finding(
            code="NET-NO-CHANGE", severity=SEVERITY_OK,
            title="No student changes category at this radius",
            detail="The numbers are small enough that the rounding absorbs it.",
            fix="Check again at the radius actually in policy."))

    headline = (f"A straight line rule calls {count} student(s) walkers. On "
                f"the street network {walkers_network} can walk it, so "
                f"{reclassified} need a seat")

    return EligibilitySplit(
        student_count=count, radius_miles=radius, network_type=network,
        circuity=network.circuity, effective_radius_miles=round(effective_radius, 3),
        walkers_straight_line=count, walkers_network=walkers_network,
        barrier_isolated=barrier_isolated, reclassified_to_bus=reclassified,
        bus_eligible_network=reclassified, walk_zone_shrink_pct=round(shrink, 1),
        headline=headline,
        assumptions=(
            f"Circuity of {network.circuity} for a {network.label.lower()} "
            f"network. {network.note}",
            f"{network.barrier_share * 100:.0f} percent of students sit behind "
            f"a barrier with no crossing inside the radius.",
            "Students are spread evenly around the school, so the count "
            "within a distance grows with the square of that distance.",
            "These three figures are this model's assumptions, not measured "
            "constants. Replace them with a routing run against your own "
            "street layer before any of this reaches a board paper.",
        ),
        findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# 2. Stop consolidation
# ---------------------------------------------------------------------------

# The stop spacing the current stop count is assumed to reflect. Roughly two
# blocks, which is what a stop list grown by request rather than by design
# tends to converge on.
BASELINE_WALK_MILES = 0.10

# Seconds lost per stop: slowing, halting, the door, the boarding and getting
# back up to speed. Named because it is the number the whole time saving
# rests on and it differs by vehicle and by age group.
SECONDS_PER_STOP = 45

# Two runs a day, morning and afternoon.
RUNS_PER_DAY = 2

# Above this, a walk to the stop stops being reasonable for the youngest
# children, whatever it does for the route time.
COMFORTABLE_WALK_MILES = 0.30


@dataclass(frozen=True)
class ConsolidationPlan:
    current_stops: int
    max_walk_distance: float
    optimized_stops: int
    stops_removed: int
    removal_pct: float
    seconds_saved_per_run: int
    daily_minutes_saved: float
    annual_hours_saved: float
    headline: str
    assumptions: tuple[str, ...]
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def reconciles(self) -> bool:
        return self.optimized_stops + self.stops_removed == self.current_stops


def simulate_stop_consolidation(current_stops: int, max_walk_distance: float,
                                school_days: int = 180) -> ConsolidationPlan:
    """Merge stops within walking distance of each other and price the time.

    Along a route the stops sit in a line, so the number needed falls in
    proportion to how far a child will walk to reach one: double the walk and
    you halve the stops. The time that buys back is the dwell at each removed
    stop, twice a day.

    The saving is real and it is not free. It is paid for by families, in
    walking, on the same streets the route was arranged to avoid.
    """
    stops = int(current_stops)
    walk = float(max_walk_distance)
    days = int(school_days)
    if stops <= 0:
        raise ValueError("a route with no stops cannot be consolidated")
    if walk <= 0:
        raise ValueError("a walk distance has to be above zero")
    if walk > 2:
        raise ValueError("that is not a walk to a stop, that is a walk to school")
    if days <= 0:
        raise ValueError("a school year has to have days in it")

    ratio = BASELINE_WALK_MILES / walk
    optimized = max(1, int(math.ceil(stops * ratio)))
    optimized = min(optimized, stops)
    removed = stops - optimized
    removal_pct = (removed / stops * 100) if stops else 0.0

    seconds_per_run = removed * SECONDS_PER_STOP
    daily_minutes = round(seconds_per_run * RUNS_PER_DAY / 60, 1)
    annual_hours = round(daily_minutes * days / 60, 1)

    findings: list[Finding] = []

    if walk > COMFORTABLE_WALK_MILES:
        findings.append(Finding(
            code="STOP-WALK-LONG", severity=SEVERITY_CRITICAL,
            title=f"A {walk:.2f} mile walk to the stop is past what the youngest can do",
            detail=(f"Beyond about {COMFORTABLE_WALK_MILES:.2f} miles the "
                    f"walk to the stop is a burden rather than a detail, and "
                    f"it falls hardest on the families least able to drive "
                    f"instead."),
            fix=("Set the walk distance by grade band rather than by route. "
                 "A figure that works for a high school route will not "
                 "survive a kindergarten one.")))
    elif walk > BASELINE_WALK_MILES:
        findings.append(Finding(
            code="STOP-WALK-OK", severity=SEVERITY_WARN,
            title=f"The walk to the stop rises to {walk:.2f} miles",
            detail=("Within the usual range, and still a change families "
                    "will notice on the first cold morning."),
            fix=("Publish the new stop list before the change rather than "
                 "with it. The complaint is about the surprise as often as "
                 "about the distance.")))

    if removed:
        findings.append(Finding(
            code="STOP-HAZARD", severity=SEVERITY_CRITICAL,
            title="Every removed stop creates a walk that has to be checked",
            detail=("Consolidation moves children onto the pavement, and the "
                    "route they now walk may cross the arterial the bus was "
                    "routed around. The time saved on the route is not worth "
                    "a crossing nobody looked at."),
            fix=("Walk audit each merged catchment for crossings, pavement "
                 "and lighting before publishing. This is the step that gets "
                 "skipped and it is the one that matters.")))
        findings.append(Finding(
            code="STOP-TIME", severity=SEVERITY_OK,
            title=f"{removed} stop(s) removed buys back {daily_minutes} minutes a day",
            detail=(f"At {SECONDS_PER_STOP} seconds of dwell per stop across "
                    f"{RUNS_PER_DAY} runs, which is {annual_hours} hours "
                    f"across a {days} day year."),
            fix=("Check the dwell figure against your own vehicles and age "
                 "groups. It is the number the whole saving rests on.")))
    else:
        findings.append(Finding(
            code="STOP-NONE", severity=SEVERITY_OK,
            title="No stop can be removed at this walk distance",
            detail=(f"The walk distance is at or below the "
                    f"{BASELINE_WALK_MILES:.2f} mile spacing the current "
                    f"stop list already reflects."),
            fix="Raise the walk distance if the route time has to come down."))

    headline = (f"{stops} stops become {optimized} at a {walk:.2f} mile walk, "
                f"removing {removed} and buying back {daily_minutes} minutes "
                f"a day")

    return ConsolidationPlan(
        current_stops=stops, max_walk_distance=walk, optimized_stops=optimized,
        stops_removed=removed, removal_pct=round(removal_pct, 1),
        seconds_saved_per_run=seconds_per_run, daily_minutes_saved=daily_minutes,
        annual_hours_saved=annual_hours, headline=headline,
        assumptions=(
            f"The current {stops} stops reflect a "
            f"{BASELINE_WALK_MILES:.2f} mile walk, which is what a stop list "
            f"grown by request rather than by design tends to converge on.",
            f"{SECONDS_PER_STOP} seconds of dwell per stop, covering the "
            f"slowing, the door, the boarding and getting back to speed.",
            f"{RUNS_PER_DAY} runs a day across a {days} day school year.",
            "Stops sit along a route rather than spread over an area, so the "
            "count falls in proportion to the walk rather than with its "
            "square.",
            "These four figures are this model's assumptions. Replace them "
            "with timings from your own runs before quoting the saving.",
        ),
        findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# 3. Cost impact
# ---------------------------------------------------------------------------

DEFAULT_SCHOOL_DAYS = 180

# What a daily bus cost is actually made of. The driver is most of it, which
# is why a bus taken off a route saves nothing until the driver position goes
# with it or gets used somewhere it was needed.
COST_SPLIT: tuple[tuple[str, float, bool], ...] = (
    ("Driver wages and benefits", 0.58, False),
    ("Fuel", 0.16, True),
    ("Maintenance and parts", 0.13, True),
    ("Insurance and depreciation", 0.13, False),
)

DISPOSITION_ELIMINATED = "Position eliminated or not backfilled"
DISPOSITION_REDEPLOYED = "Driver redeployed to an uncovered route"
DISPOSITION_RETAINED = "Bus and driver retained as spare capacity"

DISPOSITIONS: tuple[str, ...] = (DISPOSITION_ELIMINATED, DISPOSITION_REDEPLOYED,
                                 DISPOSITION_RETAINED)

# Which cost components actually stop, by what happens to the bus and the
# driver. The first draft of this treated redeployed and retained as the
# same case and returned the same cash figure for both, which is wrong in a
# way that matters: a redeployed bus is still driving a route and still
# burning fuel, while a parked spare is not. Redeployment therefore saves
# the least cash of the three and buys the most coverage, which is the exact
# distinction this section exists to make.
REALISED_BY_DISPOSITION: dict = {
    # Vehicle and position both go, so every component stops.
    DISPOSITION_ELIMINATED: ("Driver wages and benefits", "Fuel",
                             "Maintenance and parts",
                             "Insurance and depreciation"),
    # Bus and driver move to a route that was going uncovered. Nothing stops.
    DISPOSITION_REDEPLOYED: (),
    # Bus is parked and the driver kept, so the running costs stop and the
    # standing costs do not.
    DISPOSITION_RETAINED: ("Fuel", "Maintenance and parts"),
}


@dataclass(frozen=True)
class CostRow:
    component: str
    share: float
    daily_amount: Decimal
    annual_amount: Decimal
    realised: bool


@dataclass(frozen=True)
class CostImpact:
    buses_saved: int
    daily_cost_per_bus: Decimal
    school_days: int
    disposition: str
    rows: tuple[CostRow, ...]
    gross_daily_saving: Decimal
    gross_annual_saving: Decimal
    cash_annual_saving: Decimal
    capacity_annual_value: Decimal
    headline: str
    leadership_summary: tuple[tuple[str, str], ...]
    assumptions: tuple[str, ...]
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def rows_annual_total(self) -> Decimal:
        return money(sum(row.annual_amount for row in self.rows))

    @property
    def reconciles(self) -> bool:
        """Cash plus capacity equals the gross, and the rows equal the gross."""
        return (self.rows_annual_total == self.gross_annual_saving
                and money(self.cash_annual_saving + self.capacity_annual_value)
                == self.gross_annual_saving)


def generate_cost_impact_report(buses_saved: int, daily_cost_per_bus,
                                disposition: str = DISPOSITION_ELIMINATED,
                                school_days: int = DEFAULT_SCHOOL_DAYS
                                ) -> CostImpact:
    """Turn routes removed into a number a board can be shown without a caveat.

    The caveat is built in instead. A bus taken off a route is a saving only
    where the bus and the driver leave the establishment. Where the driver is
    moved to a route that was going uncovered, the district has bought
    coverage rather than cash, which in a driver shortage is worth more and
    is still not the same line in a budget. Reporting the two as one number
    is how a saving gets promised and then not found.
    """
    buses = int(buses_saved)
    daily = money(daily_cost_per_bus)
    days = int(school_days)
    if buses < 0:
        raise ValueError("a negative number of buses cannot be saved")
    if daily <= 0:
        raise ValueError("a daily cost has to be above zero")
    if days <= 0:
        raise ValueError("a school year has to have days in it")
    if disposition not in DISPOSITIONS:
        raise ValueError(f"unknown disposition: {disposition!r}")

    gross_daily = money(daily * buses)
    realised_components = REALISED_BY_DISPOSITION[disposition]
    rows: list[CostRow] = []
    for component, share, _variable in COST_SPLIT:
        daily_amount = money(gross_daily * Decimal(str(share)))
        rows.append(CostRow(
            component=component, share=share, daily_amount=daily_amount,
            annual_amount=money(daily_amount * days),
            realised=component in realised_components))

    gross_annual = money(sum(row.annual_amount for row in rows))
    cash_annual = money(sum(row.annual_amount for row in rows if row.realised))
    capacity_annual = money(gross_annual - cash_annual)

    findings: list[Finding] = []

    if disposition == DISPOSITION_ELIMINATED:
        findings.append(Finding(
            code="COST-CASH", severity=SEVERITY_OK,
            title="This is a cash saving because the position goes with the bus",
            detail=(f"All {len(rows)} components fall away, so the "
                    f"{gross_annual} is money that leaves the budget rather "
                    f"than money that moves inside it."),
            fix=("Say in the paper which positions and which vehicles, "
                 "because a saving with no names attached gets revisited.")))
    elif disposition == DISPOSITION_REDEPLOYED:
        findings.append(Finding(
            code="COST-CAPACITY", severity=SEVERITY_CRITICAL,
            title="None of this is cash, and that is not the same as nothing",
            detail=(f"The bus and the driver both move to a route that was "
                    f"going uncovered, so nothing stops: the wages are still "
                    f"paid and the new route still burns fuel. The whole "
                    f"{gross_annual} buys coverage of a route that was not "
                    f"running. In a driver shortage that is worth more than "
                    f"the cash would have been, and it is still not cash, "
                    f"and a budget line cannot be written against it."),
            fix=("Report it as a route delivered rather than as a saving. A "
                 "board told about a saving that never appears stops "
                 "believing the next number.")))
    else:
        findings.append(Finding(
            code="COST-RETAINED", severity=SEVERITY_CRITICAL,
            title="Nothing leaves the budget while the bus and driver stay",
            detail=(f"Only the running costs stop, which is "
                    f"{cash_annual} of {gross_annual}. The rest is a spare "
                    f"vehicle and a retained position, and calling that a "
                    f"saving is how the number fails to arrive."),
            fix=("Decide what the spare capacity is for before claiming it. "
                 "Spare capacity with no plan becomes a cost again next "
                 "year.")))

    findings.append(Finding(
        code="COST-DRIVER-SHARE", severity=SEVERITY_WARN,
        title="The driver is most of the cost of a bus",
        detail=(f"Wages and benefits are "
                f"{COST_SPLIT[0][1] * 100:.0f} percent of the daily figure, "
                f"so every conversation about saving money on routes is "
                f"really a conversation about driver positions."),
        fix=("Check the split against your own accounts. If the driver share "
             "differs much from this, every figure on this page moves with "
             "it.")))

    headline = (f"{buses} bus(es) removed at {daily} a day is "
                f"{gross_annual} gross across {days} days, of which "
                f"{cash_annual} is cash")

    summary = (
        ("What changes", f"{buses} route(s) come off the schedule."),
        ("Gross annual value", f"{gross_annual} across a {days} day year."),
        ("Cash leaving the budget", f"{cash_annual}."),
        ("Value retained as capacity", f"{capacity_annual}."),
        ("Disposition assumed", disposition),
        ("The question to settle first",
         "Whether the driver positions actually go. Until that is decided "
         "the cash line is a forecast rather than a saving."),
    )

    return CostImpact(
        buses_saved=buses, daily_cost_per_bus=daily, school_days=days,
        disposition=disposition, rows=tuple(rows),
        gross_daily_saving=gross_daily, gross_annual_saving=gross_annual,
        cash_annual_saving=cash_annual, capacity_annual_value=capacity_annual,
        headline=headline, leadership_summary=summary,
        assumptions=(
            f"A {days} day school year.",
            "A cost split of "
            + ", ".join(f"{c} {s * 100:.0f} percent"
                        for c, s, _v in COST_SPLIT) + ".",
            "Fuel and maintenance stop the day the route stops. Wages and "
            "insurance only stop when the position and the vehicle do.",
            "These figures are this model's assumptions. Replace the split "
            "with your own accounts before the number is presented.",
        ),
        findings=tuple(findings),
    )
