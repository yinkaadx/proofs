"""Transfer Booking and Routing Console: the engine.

Three parts of a transfer platform, and one of them is the part where the
honest answer is that the thing being asked for cannot be built the way it
was specified.

1. Pricing. A banded per kilometre rate is the normal model and the normal
   bug: applying the band's rate to the whole journey rather than to the
   kilometres inside that band puts a cliff at every boundary, so a 50.1 km
   run quotes less than a 49.9 km one. The bands here are marginal, like tax
   brackets, and the tests prove the curve has no cliff in it.
2. Background GPS in a browser. It does not work, on either platform, and no
   amount of engineering makes it work. A service worker cannot reach the
   Geolocation API at all, and a backgrounded page stops executing. This
   section says so plainly and gives the three things that do work instead.
3. The driver state machine. Every transition written down, terminal states
   with nothing after them, and no state that cannot be reached from the
   start. Those are properties a test can check, and a dispatch board that
   lets a driver jump from Assigned to Completed is a board that bills for
   journeys nobody took.

No Streamlit import lives here on purpose, so the same engine could sit behind
a real booking API without a line changing.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# 1. Distance pricing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DistanceBand:
    upper_km: float          # inclusive upper edge, inf for the last band
    rate_per_km: Decimal
    label: str


# Marginal bands. Each rate applies only to the kilometres inside its own
# band, exactly as an income tax band applies only to the income inside it.
# The long distance rate is lower because the fixed cost of the job, the
# driver turning up and the vehicle being cleaned, is already paid for by the
# first few kilometres.
DISTANCE_BANDS: tuple[DistanceBand, ...] = (
    DistanceBand(10.0, Decimal("2.40"), "the first 10 km"),
    DistanceBand(50.0, Decimal("1.60"), "10 km to 50 km"),
    DistanceBand(float("inf"), Decimal("1.15"), "everything past 50 km"),
)

BASE_FARE = Decimal("4.50")
MINIMUM_FARE = Decimal("12.00")


@dataclass(frozen=True)
class Vehicle:
    key: str
    title: str
    seats: int
    multiplier: Decimal
    note: str


VEHICLES: tuple[Vehicle, ...] = (
    Vehicle("saloon", "Saloon", 4, Decimal("1.00"),
            "Three passengers and two cases, which is most airport runs."),
    Vehicle("estate", "Estate", 4, Decimal("1.15"),
            "Same seats, room for the fourth case that breaks a saloon."),
    Vehicle("mpv", "MPV", 6, Decimal("1.45"),
            "Six seats. Quoted as a saloon and sent as this is where margin dies."),
    Vehicle("executive", "Executive", 4, Decimal("1.80"),
            "Priced for the waiting time and the presentation, not the metal."),
    Vehicle("minibus", "Minibus", 8, Decimal("2.10"),
            "Eight seats and a driver who may need a different licence."),
)

VEHICLE_BY_KEY = {v.key: v for v in VEHICLES}
VEHICLE_BY_TITLE = {v.title: v for v in VEHICLES}


@dataclass(frozen=True)
class FixedRoute:
    name: str
    distance_km: float
    price: Decimal


# The flat prices already published to customers. A fixed price is a promise
# made before the journey is known, which is why comparing it against the
# meter is worth doing before the next print run rather than after.
FIXED_ROUTES: tuple[FixedRoute, ...] = (
    FixedRoute("City centre to the airport", 28.0, Decimal("55.00")),
    FixedRoute("Airport to the north suburbs", 41.5, Decimal("72.00")),
    FixedRoute("Cross county long haul", 96.0, Decimal("120.00")),
    FixedRoute("Station to the conference centre", 6.2, Decimal("18.00")),
)

FIXED_BY_NAME = {route.name: route for route in FIXED_ROUTES}


@dataclass(frozen=True)
class FareRow:
    label: str
    amount: Decimal


@dataclass(frozen=True)
class FareQuote:
    distance_km: float
    vehicle: Vehicle
    rows: tuple[FareRow, ...]
    distance_charge: Decimal
    subtotal: Decimal
    metered_fare: Decimal
    minimum_applied: bool
    fixed_route: FixedRoute | None
    fixed_price: Decimal | None
    difference: Decimal | None
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def rows_total(self) -> Decimal:
        """What the printed rows add up to. Must equal the subtotal."""
        return money(sum(row.amount for row in self.rows))

    @property
    def fixed_route_loses_money(self) -> bool:
        return self.difference is not None and self.difference > 0

    @property
    def severity(self) -> str:
        for level in (SEVERITY_CRITICAL, SEVERITY_WARN):
            if any(f.severity == level for f in self.findings):
                return level
        return SEVERITY_OK


def band_breakdown(distance_km: float) -> tuple[tuple[DistanceBand, float], ...]:
    """How many kilometres fall in each band. The sum is the distance."""
    distance = float(distance_km)
    if distance < 0:
        raise ValueError("a journey cannot be a negative distance")
    remaining = distance
    lower = 0.0
    rows: list[tuple[DistanceBand, float]] = []
    for band in DISTANCE_BANDS:
        if remaining <= 0:
            break
        width = band.upper_km - lower
        used = min(remaining, width)
        if used > 0:
            rows.append((band, used))
        remaining -= used
        lower = band.upper_km
    return tuple(rows)


def _resolve_vehicle(value) -> Vehicle:
    if isinstance(value, Vehicle):
        return value
    name = str(value or "").strip()
    if name in VEHICLE_BY_KEY:
        return VEHICLE_BY_KEY[name]
    if name in VEHICLE_BY_TITLE:
        return VEHICLE_BY_TITLE[name]
    raise ValueError(f"unknown vehicle type: {value!r}")


def calculate_transfer_fare(distance_km: float, vehicle_type,
                            fixed_route_name: str = "") -> FareQuote:
    """Meter the journey, and hold it against the flat price if there is one.

    The bands are marginal. A 50.1 km journey costs the 49.9 km journey plus
    two tenths of a kilometre at the long distance rate, never less, and the
    tests check that across the whole curve rather than at one convenient
    point.
    """
    distance = float(distance_km)
    if distance < 0:
        raise ValueError("a journey cannot be a negative distance")
    if distance > 2000:
        raise ValueError("that is not a transfer, that is a road trip")
    vehicle = _resolve_vehicle(vehicle_type)

    rows: list[FareRow] = [FareRow("Base fare", money(BASE_FARE))]
    distance_charge = Decimal("0")
    for band, kilometres in band_breakdown(distance):
        amount = money(Decimal(str(kilometres)) * band.rate_per_km)
        distance_charge += amount
        rows.append(FareRow(
            f"{kilometres:.1f} km in {band.label} at "
            f"{band.rate_per_km} per km", amount))

    before_vehicle = money(BASE_FARE + distance_charge)
    uplift = money(before_vehicle * (vehicle.multiplier - Decimal("1")))
    if uplift:
        rows.append(FareRow(
            f"{vehicle.title} uplift at {vehicle.multiplier} times", uplift))

    subtotal = money(sum(row.amount for row in rows))
    minimum_applied = subtotal < MINIMUM_FARE
    metered = money(MINIMUM_FARE) if minimum_applied else subtotal

    findings: list[Finding] = []
    if minimum_applied:
        findings.append(Finding(
            code="FARE-MINIMUM", severity=SEVERITY_WARN,
            title=f"The minimum fare of {money(MINIMUM_FARE)} carried this job",
            detail=(f"The meter came to {subtotal}, which does not cover a "
                    f"driver turning up. The minimum is what makes a short "
                    f"job worth accepting."),
            fix=("Check the short job volume. A fleet living on minimum fares "
                 "is a fleet whose bands are wrong for its actual work.")))

    fixed = FIXED_BY_NAME.get(str(fixed_route_name or "").strip())
    fixed_price = fixed.price if fixed else None
    difference = money(metered - fixed.price) if fixed else None

    if fixed and difference is not None:
        if difference > 0:
            findings.append(Finding(
                code="FARE-FIXED-LOSS", severity=SEVERITY_CRITICAL,
                title=f"The flat price is {difference} below the meter",
                detail=(f"{fixed.name} is published at {fixed.price} and this "
                        f"journey meters at {metered} in a {vehicle.title}. "
                        f"Every one of these sold loses the difference."),
                fix=("Either reprice the route or restrict the flat price to "
                     "the vehicle it was costed for. A flat price that is "
                     "silently vehicle blind is the usual cause.")))
        elif difference < 0:
            findings.append(Finding(
                code="FARE-FIXED-MARGIN", severity=SEVERITY_OK,
                title=f"The flat price carries {abs(difference)} over the meter",
                detail=(f"{fixed.name} at {fixed.price} sits above the "
                        f"{metered} this journey would meter."),
                fix=("Keep it, and watch that the margin is not coming from a "
                     "distance customers have noticed is shorter than the "
                     "route you costed.")))
        else:
            findings.append(Finding(
                code="FARE-FIXED-EVEN", severity=SEVERITY_WARN,
                title="The flat price exactly matches the meter",
                detail=("No margin and no loss, which means any delay, any "
                        "diversion and any waiting time is unpaid."),
                fix="Price in the variance, because the variance is the job."))

    findings.append(Finding(
        code="FARE-BANDS", severity=SEVERITY_OK,
        title="The bands are marginal, so there is no cliff at a boundary",
        detail=("Each rate applies only to the kilometres inside its own "
                "band. Charging the whole journey at the band it ends in "
                "makes a 50.1 km run cheaper than a 49.9 km one, which "
                "customers find before finance does."),
        fix="Keep it marginal. The test suite fails if that ever changes."))

    headline = (f"{distance:.1f} km in a {vehicle.title} meters at {metered}")
    if fixed:
        headline += f" against a flat {fixed.price} for {fixed.name}"

    return FareQuote(
        distance_km=distance, vehicle=vehicle, rows=tuple(rows),
        distance_charge=money(distance_charge), subtotal=subtotal,
        metered_fare=metered, minimum_applied=minimum_applied,
        fixed_route=fixed, fixed_price=fixed_price, difference=difference,
        headline=headline, findings=tuple(findings),
    )


def fixed_route_break_even(route: FixedRoute, vehicle_type) -> Decimal:
    """What the flat price would have to be to match the meter today."""
    quote = calculate_transfer_fare(route.distance_km, vehicle_type)
    return quote.metered_fare


# ---------------------------------------------------------------------------
# 2. Background GPS in a browser
# ---------------------------------------------------------------------------

OS_IOS = "iOS Safari"
OS_ANDROID = "Android Chrome"
OS_TYPES: tuple[str, ...] = (OS_IOS, OS_ANDROID)

VERDICT_NOT_POSSIBLE = "NOT POSSIBLE IN A BROWSER"


@dataclass(frozen=True)
class GpsConstraint:
    code: str
    title: str
    detail: str
    workaround: str
    blocking: bool


# The one that decides the answer, on both platforms. The Geolocation API is
# not exposed to service worker scope, so the only thing a browser can run
# while the page is gone cannot ask where the phone is. Everything else is
# detail on top of that.
SHARED_CONSTRAINTS: tuple[GpsConstraint, ...] = (
    GpsConstraint(
        code="GPS-SW",
        title="A service worker cannot read the location at all",
        detail=("navigator.geolocation is not exposed to service worker "
                "scope. The one thing a browser runs when the page is gone "
                "is the one thing that cannot ask where the device is."),
        workaround=("Nothing works around this in a browser. It is the reason "
                    "the rest of the list cannot be engineered past."),
        blocking=True),
    GpsConstraint(
        code="GPS-SECURE",
        title="Geolocation needs a secure context",
        detail=("The API is unavailable over plain HTTP, which catches "
                "staging environments rather than production."),
        workaround="Serve everything over HTTPS, including the test builds.",
        blocking=False),
    GpsConstraint(
        code="GPS-PERMISSION",
        title="Permission is per origin and the driver can revoke it",
        detail=("A driver who taps block once has blocked it until they go "
                "into settings, and the page cannot prompt again."),
        workaround=("Detect the denied state and show the driver the exact "
                    "settings path rather than re prompting into a wall."),
        blocking=False),
)

IOS_CONSTRAINTS: tuple[GpsConstraint, ...] = (
    GpsConstraint(
        code="GPS-IOS-SUSPEND",
        title="Backgrounding or locking the phone suspends the page entirely",
        detail=("Switching apps or locking the screen stops JavaScript. "
                "watchPosition does not fire slowly, it stops, and it "
                "resumes only when the driver looks at the screen again."),
        workaround=("A screen wake lock keeps the page in front while the "
                    "driver is in the vehicle, which is a workaround for the "
                    "screen turning off and not for the driver switching to "
                    "the maps app."),
        blocking=True),
    GpsConstraint(
        code="GPS-IOS-PWA",
        title="Adding it to the home screen does not change this",
        detail=("A home screen web app on iOS is still suspended in the "
                "background. It gains an icon, not a background thread."),
        workaround="Treat the home screen version as a shortcut, not a capability.",
        blocking=True),
)

ANDROID_CONSTRAINTS: tuple[GpsConstraint, ...] = (
    GpsConstraint(
        code="GPS-AND-FREEZE",
        title="A hidden tab is throttled and then frozen",
        detail=("Chrome throttles timers in a backgrounded tab and can "
                "freeze the tab entirely after it has been hidden for a "
                "while, then discard it under memory pressure. Tracking "
                "degrades quietly rather than failing loudly, which is "
                "worse, because the dispatch board still shows a last known "
                "position and no one can tell it is stale."),
        workaround=("Stamp every position with its own timestamp and show "
                    "the age on the dispatch board, so a frozen tab reads as "
                    "stale rather than as parked."),
        blocking=True),
    GpsConstraint(
        code="GPS-AND-BATTERY",
        title="Aggressive battery management varies by manufacturer",
        detail=("Several Android skins kill background work harder than "
                "stock Chrome does, so behaviour differs across the drivers' "
                "own handsets rather than across Android versions."),
        workaround=("Test on the handsets the drivers actually carry, not on "
                    "one reference device."),
        blocking=False),
)

WORKING_ALTERNATIVES: tuple[str, ...] = (
    "A native or wrapped app with a real background location plugin. This "
    "is the only option that tracks continuously, and it is what every "
    "dispatch product that works is doing.",
    "Driver initiated status updates. The driver taps arrived and on board, "
    "and the board moves on the tap rather than on a position. Less data, "
    "and all of it true.",
    "Foreground tracking with a screen wake lock while the vehicle is "
    "moving, and an honest stale marker the moment the page loses focus.",
)


@dataclass(frozen=True)
class GpsAssessment:
    os_type: str
    verdict: str
    constraints: tuple[GpsConstraint, ...]
    blocking_constraints: tuple[GpsConstraint, ...]
    background_tracking_possible: bool
    alternatives: tuple[str, ...]
    headline: str
    fix: str


def evaluate_web_gps_limits(os_type: str) -> GpsAssessment:
    """What a browser can and cannot do about tracking a driver.

    The answer is the same on both platforms and it is no. The reason differs,
    the detail differs, and the honest thing is to give the reason rather than
    a workaround that will be found not to work in month three of the build.
    """
    name = str(os_type or "").strip()
    if name not in OS_TYPES:
        raise ValueError(f"unknown platform: {os_type!r}")

    specific = IOS_CONSTRAINTS if name == OS_IOS else ANDROID_CONSTRAINTS
    constraints = SHARED_CONSTRAINTS + specific
    blocking = tuple(c for c in constraints if c.blocking)

    if name == OS_IOS:
        headline = ("iOS Safari suspends the page the moment the driver "
                    "switches app or the screen locks, and a service worker "
                    "cannot read the location to cover for it")
    else:
        headline = ("Android Chrome throttles and then freezes a hidden tab, "
                    "and a service worker cannot read the location to cover "
                    "for it, so tracking degrades quietly rather than stopping")

    fix = ("Do not build continuous background tracking on the web. Pick one "
           "of the three alternatives, and if the requirement is genuinely "
           "continuous tracking then the requirement is a native app and the "
           "estimate should say so before anyone writes a line.")

    return GpsAssessment(
        os_type=name, verdict=VERDICT_NOT_POSSIBLE, constraints=constraints,
        blocking_constraints=blocking, background_tracking_possible=False,
        alternatives=WORKING_ALTERNATIVES, headline=headline, fix=fix,
    )


# ---------------------------------------------------------------------------
# 3. The driver state machine
# ---------------------------------------------------------------------------

STATUS_UNASSIGNED = "Unassigned"
STATUS_ASSIGNED = "Assigned"
STATUS_EN_ROUTE = "En Route to Pickup"
STATUS_ARRIVED = "Arrived at Pickup"
STATUS_ON_BOARD = "Passenger On Board"
STATUS_COMPLETED = "Completed"
STATUS_CANCELLED = "Cancelled"
STATUS_NO_SHOW = "No Show"

# Every transition, written once. A board that lets a driver jump from
# Assigned to Completed is a board that bills for journeys nobody took, so the
# only moves that exist are the ones on this map.
TRANSITIONS: dict = {
    STATUS_UNASSIGNED: (STATUS_ASSIGNED, STATUS_CANCELLED),
    STATUS_ASSIGNED: (STATUS_EN_ROUTE, STATUS_UNASSIGNED, STATUS_CANCELLED),
    STATUS_EN_ROUTE: (STATUS_ARRIVED, STATUS_CANCELLED),
    STATUS_ARRIVED: (STATUS_ON_BOARD, STATUS_NO_SHOW, STATUS_CANCELLED),
    STATUS_ON_BOARD: (STATUS_COMPLETED,),
    STATUS_COMPLETED: (),
    STATUS_CANCELLED: (),
    STATUS_NO_SHOW: (),
}

TERMINAL_STATUSES: tuple[str, ...] = (
    STATUS_COMPLETED, STATUS_CANCELLED, STATUS_NO_SHOW)

# The move the board should offer first. Everything else on the list is an
# exception the driver has to choose deliberately.
HAPPY_PATH_NEXT: dict = {
    STATUS_UNASSIGNED: STATUS_ASSIGNED,
    STATUS_ASSIGNED: STATUS_EN_ROUTE,
    STATUS_EN_ROUTE: STATUS_ARRIVED,
    STATUS_ARRIVED: STATUS_ON_BOARD,
    STATUS_ON_BOARD: STATUS_COMPLETED,
}

BILLABLE_FROM = STATUS_ON_BOARD


@dataclass(frozen=True)
class StatusTransition:
    current_status: str
    is_terminal: bool
    next_status: str | None
    allowed_next: tuple[str, ...]
    exceptions: tuple[str, ...]
    billable: bool
    headline: str
    fix: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    def permits(self, candidate: str) -> bool:
        return candidate in self.allowed_next


def simulate_driver_status(current_status: str) -> StatusTransition:
    """Where a job can go next, and which move the board should offer first.

    A terminal state returns nothing, not a helpful suggestion. Offering a
    next step from Completed is how a finished job gets reopened and billed
    twice.
    """
    status = str(current_status or "").strip()
    if status not in TRANSITIONS:
        raise ValueError(f"unknown driver status: {current_status!r}")

    allowed = TRANSITIONS[status]
    terminal = status in TERMINAL_STATUSES
    happy = HAPPY_PATH_NEXT.get(status)
    exceptions = tuple(s for s in allowed if s != happy)

    findings: list[Finding] = []

    if terminal:
        findings.append(Finding(
            code="STATE-TERMINAL", severity=SEVERITY_OK,
            title=f"{status} is terminal and offers nothing",
            detail=("A finished job with a next step is a job that gets "
                    "reopened and billed a second time."),
            fix=("Reopening belongs in a supervisor action with its own audit "
                 "row, not in the driver's own status control.")))
    else:
        findings.append(Finding(
            code="STATE-NEXT", severity=SEVERITY_OK,
            title=f"{status} offers {happy} as the obvious move",
            detail=(f"The other {len(exceptions)} option(s) are exceptions "
                    f"the driver chooses deliberately rather than by "
                    f"tapping the biggest button."),
            fix=("Keep the exceptions behind a second tap. A cancel that is "
                 "as easy to hit as an arrive gets hit by accident in a "
                 "moving vehicle.")))

    if status == STATUS_ARRIVED:
        findings.append(Finding(
            code="STATE-NOSHOW", severity=SEVERITY_WARN,
            title="No Show is only reachable from Arrived, on purpose",
            detail=("A no show cannot be claimed by a driver who never "
                    "reported arriving, because the claim rests on having "
                    "been there and waited."),
            fix=("Stamp the arrival time and require the waiting period to "
                 "have elapsed before the no show button becomes live.")))

    if status == STATUS_ON_BOARD:
        findings.append(Finding(
            code="STATE-BILLABLE", severity=SEVERITY_WARN,
            title="The job becomes billable here and cannot be cancelled",
            detail=("Once the passenger is in the vehicle the only way out "
                    "is Completed. A cancel from this state would leave a "
                    "passenger mid journey on a job that no longer exists."),
            fix=("An aborted journey is a Completed job with an adjustment, "
                 "handled by a person, not a state the driver can select.")))

    if terminal:
        headline = f"{status} is the end of the job, with no move after it"
    else:
        headline = (f"{status} moves to {happy}, with "
                    f"{len(exceptions)} exception(s) available")

    fix = ("Offer the obvious move as the primary control and put the rest "
           "behind a confirmation." if not terminal else
           "Show the job as closed and remove the status control entirely.")

    return StatusTransition(
        current_status=status, is_terminal=terminal, next_status=happy,
        allowed_next=allowed, exceptions=exceptions,
        billable=status == BILLABLE_FROM, headline=headline, fix=fix,
        findings=tuple(findings),
    )


def reachable_statuses(start: str = STATUS_UNASSIGNED) -> set:
    """Every state reachable from the start, found by walking the map."""
    seen: set = set()
    queue = [start]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(TRANSITIONS.get(current, ()))
    return seen
