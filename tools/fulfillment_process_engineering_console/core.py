"""Fulfillment Process Engineering Console: the engine.

Three calculations that decide where the next hour of effort goes, and the
thing each one is usually wrong about.

1. The constraint. Throughput on a serial line is set by the slowest station
   and by nothing else, so an hour saved anywhere but the constraint is an
   hour saved nowhere. That is not an opinion about priorities, it is
   arithmetic, and the tests prove it by improving every non constraint step
   in turn and checking throughput does not move.
2. The dispatch queue. Wait time does not rise in proportion to load, it
   rises toward a vertical wall as utilisation approaches one, so a fleet
   run at ninety five percent is not efficient, it is a queue. Past one
   hundred percent the queue has no steady state at all and any average wait
   quoted for it is fiction.
3. The ticket. Automating a step that is not the constraint spends
   engineering and changes no number, so the requirement carries the
   constraint check with it, along with the four edge cases that get left
   out of every automation and found in production.

The queueing section uses the standard multi server model, which assumes
arrivals with no pattern and service times that vary exponentially. Real
delivery is neither: arrivals peak and drive times cluster. The utilisation
figure and the instability threshold hold regardless, the absolute wait
figures are a guide, and the model says so rather than implying a precision
it does not have.

No Streamlit import lives here on purpose, so the same engine could sit behind
a real operations dashboard without a line changing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

SEVERITY_OK = "OK"
SEVERITY_WARN = "WARN"
SEVERITY_CRITICAL = "CRITICAL"


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
# 1. Cycle time and the constraint
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProcessStep:
    name: str
    minutes: float
    parallel_resources: int = 1
    manual: bool = True

    @property
    def effective_minutes(self) -> float:
        """Minutes per unit once the station's parallel resources are used.

        Two pickers on one station halve the station's cycle time. They do
        not halve the work, which is why the labour figure and the cycle
        figure are different numbers and get confused constantly.
        """
        return self.minutes / self.parallel_resources

    @property
    def units_per_hour(self) -> float:
        return 60.0 / self.effective_minutes if self.effective_minutes else 0.0


@dataclass(frozen=True)
class StepReading:
    step: ProcessStep
    effective_minutes: float
    capacity_per_hour: float
    is_constraint: bool
    idle_minutes_per_unit: float
    utilisation_pct: float


@dataclass(frozen=True)
class BottleneckReport:
    readings: tuple[StepReading, ...]
    constraint_name: str
    constraint_minutes: float
    throughput_per_hour: float
    lead_time_minutes: float
    total_work_minutes: float
    line_balance_pct: float
    idle_minutes_per_unit: float
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def reconciles(self) -> bool:
        """The work minutes are the sum of the rows, and the idle time is the
        difference between what the line pays for and what it uses."""
        work = round(sum(r.effective_minutes for r in self.readings), 6)
        idle = round(sum(r.idle_minutes_per_unit for r in self.readings), 6)
        paid = round(self.constraint_minutes * len(self.readings), 6)
        return (work == round(self.total_work_minutes, 6)
                and round(work + idle, 6) == paid)


def analyze_cycle_time_bottleneck(process_steps) -> BottleneckReport:
    """Find the constraint and say what the line can actually do.

    Throughput is one unit per constraint cycle, whatever the other stations
    can manage, because a serial line cannot outrun its slowest station. Lead
    time is the sum of the stations, which is a different number and answers
    a different question: how long one order takes rather than how many
    orders an hour.
    """
    steps: list[ProcessStep] = []
    for entry in process_steps or ():
        if isinstance(entry, ProcessStep):
            step = entry
        elif isinstance(entry, dict):
            step = ProcessStep(
                name=str(entry.get("name", "")).strip(),
                minutes=float(entry.get("minutes", 0)),
                parallel_resources=int(entry.get("parallel_resources", 1)),
                manual=bool(entry.get("manual", True)))
        else:
            name, minutes, *rest = entry
            step = ProcessStep(str(name).strip(), float(minutes),
                               int(rest[0]) if rest else 1,
                               bool(rest[1]) if len(rest) > 1 else True)
        if not step.name:
            raise ValueError("every process step needs a name")
        if step.minutes <= 0:
            raise ValueError(f"{step.name} has no duration to measure")
        if step.parallel_resources < 1:
            raise ValueError(f"{step.name} needs at least one resource")
        steps.append(step)

    if not steps:
        raise ValueError("a line with no steps has no constraint")

    constraint_minutes = max(s.effective_minutes for s in steps)
    constraint = next(s for s in steps
                      if s.effective_minutes == constraint_minutes)

    readings = tuple(
        StepReading(
            step=s, effective_minutes=round(s.effective_minutes, 6),
            capacity_per_hour=round(s.units_per_hour, 4),
            is_constraint=s.name == constraint.name,
            idle_minutes_per_unit=round(
                constraint_minutes - s.effective_minutes, 6),
            utilisation_pct=round(
                s.effective_minutes / constraint_minutes * 100, 2))
        for s in steps)

    throughput = round(60.0 / constraint_minutes, 4)
    lead_time = round(sum(s.effective_minutes for s in steps), 6)
    total_work = lead_time
    paid_minutes = constraint_minutes * len(steps)
    balance = round(total_work / paid_minutes * 100, 2) if paid_minutes else 0.0
    idle = round(paid_minutes - total_work, 6)

    findings: list[Finding] = []

    findings.append(Finding(
        code="BTL-CONSTRAINT", severity=SEVERITY_CRITICAL,
        title=f"{constraint.name} is the constraint at {constraint_minutes:.2f} minutes a unit",
        detail=(f"The line makes {throughput:.2f} units an hour because this "
                f"station does. Every other station is waiting for it for "
                f"part of every cycle."),
        fix=("Spend the next hour of effort here and nowhere else. An "
             "improvement at any other station changes no number on this "
             "page, which the test suite proves by trying it.")))

    non_constraint = [r for r in readings if not r.is_constraint]
    if non_constraint:
        wasted = max(non_constraint, key=lambda r: r.idle_minutes_per_unit)
        findings.append(Finding(
            code="BTL-NON-CONSTRAINT", severity=SEVERITY_WARN,
            title=f"{wasted.step.name} idles {wasted.idle_minutes_per_unit:.2f} minutes a unit",
            detail=(f"It runs at {wasted.utilisation_pct:.0f} percent of the "
                    f"constraint's pace. Speeding it up widens the gap and "
                    f"moves nothing, because the units it produces faster "
                    f"queue in front of {constraint.name}."),
            fix=("Resist improving it. Work in progress piling up in front "
                 "of the constraint looks like productivity and is "
                 "inventory.")))

    if balance < 70:
        findings.append(Finding(
            code="BTL-UNBALANCED", severity=SEVERITY_WARN,
            title=f"The line is {balance:.0f} percent balanced",
            detail=(f"{idle:.2f} minutes of paid station time is idle for "
                    f"every unit produced. A well balanced line has stations "
                    f"of similar duration, so the paid time is used."),
            fix=("Move work from the constraint onto the idle stations "
                 "rather than adding people. Rebalancing is free and hiring "
                 "is not.")))

    manual_constraint = constraint.manual
    if manual_constraint:
        findings.append(Finding(
            code="BTL-MANUAL", severity=SEVERITY_CRITICAL,
            title=f"The constraint is a manual step",
            detail=("A manual constraint is the one worth a software ticket, "
                    "because automating it is the only change that raises "
                    "throughput. Automating anything else is engineering "
                    "spent for no number."),
            fix=("Take this step to the requirement generator and write the "
                 "ticket against it specifically.")))
    else:
        findings.append(Finding(
            code="BTL-AUTOMATED", severity=SEVERITY_WARN,
            title="The constraint is already automated",
            detail=("More software will not help here. The lever is "
                    "capacity: another unit of the same machine, or work "
                    "moved off it onto an idle station."),
            fix="Price a second unit against the throughput it buys."))

    others = [r for r in readings if not r.is_constraint]
    if others:
        runner_up = max(others, key=lambda r: r.effective_minutes)
        findings.append(Finding(
            code="BTL-NEXT", severity=SEVERITY_OK,
            title=f"The constraint moves to {runner_up.step.name} at {runner_up.effective_minutes:.2f} minutes",
            detail=(f"Improve {constraint.name} past that figure and the "
                    f"line stops gaining, because the constraint has moved "
                    f"and the old one is no longer what limits anything."),
            fix=(f"Cap the investment in {constraint.name} at the point it "
                 f"reaches {runner_up.effective_minutes:.2f} minutes. Past "
                 f"that the money buys idle time.")))

    headline = (f"{constraint.name} limits the line to {throughput:.2f} units "
                f"an hour, and one order takes {lead_time:.1f} minutes end to "
                f"end")

    return BottleneckReport(
        readings=readings, constraint_name=constraint.name,
        constraint_minutes=round(constraint_minutes, 6),
        throughput_per_hour=throughput, lead_time_minutes=lead_time,
        total_work_minutes=total_work, line_balance_pct=balance,
        idle_minutes_per_unit=idle, headline=headline,
        findings=tuple(findings),
    )


SAMPLE_STEPS: tuple[dict, ...] = (
    {"name": "Order received and validated", "minutes": 1.5,
     "parallel_resources": 1, "manual": False},
    {"name": "Pick from shelf", "minutes": 7.0, "parallel_resources": 2,
     "manual": True},
    {"name": "Pack and weigh", "minutes": 4.0, "parallel_resources": 1,
     "manual": True},
    {"name": "Manual label and carrier selection", "minutes": 6.5,
     "parallel_resources": 1, "manual": True},
    {"name": "Load to dispatch bay", "minutes": 2.0,
     "parallel_resources": 1, "manual": True},
)


# ---------------------------------------------------------------------------
# 2. Dispatch queue capacity
# ---------------------------------------------------------------------------

def erlang_b(servers: int, offered_load: float) -> float:
    """Blocking probability, by the recursion rather than by factorials.

    The recursion is numerically stable at any server count. The factorial
    form overflows well before an operations problem gets interesting, and
    that overflow is the usual reason a capacity spreadsheet stops working
    at about twenty drivers.
    """
    probability = 1.0
    for index in range(1, int(servers) + 1):
        probability = (offered_load * probability
                       / (index + offered_load * probability))
    return probability


def erlang_c(servers: int, offered_load: float) -> float:
    """Probability an arriving order waits, derived from Erlang B.

    C equals B divided by one minus rho times one minus B. Deriving it from
    the stable recursion keeps it stable, and the test checks it against the
    direct summation form at server counts where that form still works.
    """
    count = int(servers)
    if count <= 0:
        raise ValueError("a queue needs at least one server")
    rho = offered_load / count
    if rho >= 1:
        return 1.0
    blocking = erlang_b(count, offered_load)
    return blocking / (1 - rho * (1 - blocking))


def erlang_c_direct(servers: int, offered_load: float) -> float:
    """The textbook summation form, kept only so a test can cross check."""
    count = int(servers)
    rho = offered_load / count
    if rho >= 1:
        return 1.0
    top = (offered_load ** count / math.factorial(count)) * (count / (count - offered_load))
    bottom = sum(offered_load ** k / math.factorial(k) for k in range(count)) + top
    return top / bottom


UTILISATION_COMFORTABLE = 70.0
UTILISATION_STRAINED = 85.0


@dataclass(frozen=True)
class DispatchCapacity:
    order_volume: float
    active_drivers: int
    average_delivery_time: float
    service_rate_per_driver: float
    total_capacity_per_hour: float
    offered_load: float
    utilisation_pct: float
    stable: bool
    probability_of_waiting: float
    average_wait_minutes: float
    orders_waiting: float
    average_time_in_system_minutes: float
    drivers_needed_for_stability: int
    drivers_needed_for_comfort: int
    headline: str
    assumptions: tuple[str, ...]
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def simulate_dispatch_capacity(order_volume: float, active_drivers: int,
                               average_delivery_time: float) -> DispatchCapacity:
    """Queue wait and utilisation for a fleet, with the wall named.

    Orders arrive at a rate, each driver completes one every average delivery
    time, and utilisation is the ratio of the two against the fleet. At or
    above one hundred percent the queue has no steady state: it grows for as
    long as the arrivals keep coming, and any average wait quoted for it is
    fiction rather than a large number.
    """
    volume = float(order_volume)
    drivers = int(active_drivers)
    service_minutes = float(average_delivery_time)
    if volume < 0:
        raise ValueError("order volume cannot be negative")
    if drivers < 1:
        raise ValueError("a fleet needs at least one driver")
    if service_minutes <= 0:
        raise ValueError("a delivery takes longer than no time at all")

    service_rate = 60.0 / service_minutes
    capacity = drivers * service_rate
    offered = volume / service_rate
    utilisation = (volume / capacity * 100) if capacity else 0.0
    stable = utilisation < 100.0

    findings: list[Finding] = []

    if stable and volume > 0:
        wait_probability = erlang_c(drivers, offered)
        wait_hours = wait_probability / (capacity - volume)
        wait_minutes = wait_hours * 60
        queue_length = volume * wait_hours
        in_system = wait_minutes + service_minutes
    else:
        wait_probability = 1.0 if volume > 0 else 0.0
        wait_minutes = float("inf") if volume > 0 else 0.0
        queue_length = float("inf") if volume > 0 else 0.0
        in_system = float("inf") if volume > 0 else 0.0

    needed_stable = max(1, math.floor(offered) + 1) if volume > 0 else 1
    needed_comfort = max(
        1, math.ceil(volume / (service_rate * UTILISATION_COMFORTABLE / 100))
    ) if volume > 0 else 1

    if not stable:
        findings.append(Finding(
            code="CAP-UNSTABLE", severity=SEVERITY_CRITICAL,
            title=f"At {utilisation:.0f} percent the queue never clears",
            detail=(f"{volume:.0f} orders an hour arrive against a fleet "
                    f"capacity of {capacity:.1f}. The backlog grows for as "
                    f"long as orders keep coming, so there is no average "
                    f"wait to quote: any figure given for it is fiction "
                    f"rather than a large number."),
            fix=(f"Take the arrival rate below the capacity. "
                 f"{needed_stable} driver(s) makes it merely stable and "
                 f"{needed_comfort} makes it workable.")))
    elif utilisation >= UTILISATION_STRAINED:
        findings.append(Finding(
            code="CAP-STRAINED", severity=SEVERITY_CRITICAL,
            title=f"{utilisation:.0f} percent utilisation is a queue, not efficiency",
            detail=(f"Waiting time does not rise in proportion to load, it "
                    f"rises toward a wall as utilisation approaches one. At "
                    f"this level the average order waits "
                    f"{wait_minutes:.1f} minutes before a driver is free, "
                    f"and one sick driver moves that number a long way."),
            fix=(f"Size for {UTILISATION_COMFORTABLE:.0f} percent, which "
                 f"needs {needed_comfort} driver(s) here. The spare capacity "
                 f"is what absorbs a bad morning.")))
    elif utilisation >= UTILISATION_COMFORTABLE:
        findings.append(Finding(
            code="CAP-BUSY", severity=SEVERITY_WARN,
            title=f"{utilisation:.0f} percent is busy but workable",
            detail=(f"An order waits {wait_minutes:.1f} minutes on average "
                    f"and {wait_probability * 100:.0f} percent of them wait "
                    f"at all."),
            fix="Watch it rather than act on it. The next ten percent costs far more than the last."))
    else:
        findings.append(Finding(
            code="CAP-COMFORTABLE", severity=SEVERITY_OK,
            title=f"{utilisation:.0f} percent utilisation with room to absorb a spike",
            detail=(f"An order waits {wait_minutes:.1f} minutes on average. "
                    f"The fleet can take a bad morning without the queue "
                    f"running away."),
            fix=("Do not read this as overstaffing. The headroom is the "
                 "product, and removing it is what produces the strained "
                 "case.")))

    findings.append(Finding(
        code="CAP-NONLINEAR", severity=SEVERITY_WARN,
        title="The relationship between load and wait is not a straight line",
        detail=("Going from seventy to eighty percent adds a little. Going "
                "from ninety to ninety five multiplies the wait. Planning "
                "from a linear assumption is how a fleet that looked fine "
                "last month has a two hour backlog this month."),
        fix=("Model the peak hour rather than the daily average. The average "
             "hour is not the hour that fails.")))

    findings.append(Finding(
        code="CAP-MODEL", severity=SEVERITY_WARN,
        title="This is the standard multi server model and delivery is not standard",
        detail=("It assumes arrivals with no pattern and service times that "
                "vary exponentially. Real orders peak and real drive times "
                "cluster, so the absolute wait figures are a guide. The "
                "utilisation figure and the instability threshold hold "
                "regardless of the distribution."),
        fix=("Use it to size the fleet and to find the wall. Use measured "
             "times for a service level promise.")))

    if stable:
        headline = (f"{volume:.0f} orders an hour across {drivers} driver(s) "
                    f"runs at {utilisation:.0f} percent, with an average wait "
                    f"of {wait_minutes:.1f} minutes")
    else:
        headline = (f"{volume:.0f} orders an hour against a capacity of "
                    f"{capacity:.1f} is {utilisation:.0f} percent, so the "
                    f"queue grows without limit")

    return DispatchCapacity(
        order_volume=volume, active_drivers=drivers,
        average_delivery_time=service_minutes,
        service_rate_per_driver=round(service_rate, 4),
        total_capacity_per_hour=round(capacity, 4),
        offered_load=round(offered, 4), utilisation_pct=round(utilisation, 2),
        stable=stable, probability_of_waiting=round(wait_probability, 4),
        average_wait_minutes=(round(wait_minutes, 2) if stable else wait_minutes),
        orders_waiting=(round(queue_length, 3) if stable else queue_length),
        average_time_in_system_minutes=(round(in_system, 2) if stable else in_system),
        drivers_needed_for_stability=needed_stable,
        drivers_needed_for_comfort=needed_comfort,
        headline=headline,
        assumptions=(
            "Arrivals have no pattern and service times vary exponentially, "
            "which is the standard multi server queueing assumption.",
            "Every driver can take any order, so the fleet is one pool "
            "rather than several.",
            f"Comfortable is treated as below {UTILISATION_COMFORTABLE:.0f} "
            f"percent and strained at or above {UTILISATION_STRAINED:.0f}.",
            "These are this model's assumptions. Measure your own arrival "
            "pattern before promising a service level from them.",
        ),
        findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# 3. The software requirement
# ---------------------------------------------------------------------------

# The four edge cases left out of every automation and found in production.
# They are attached to every ticket rather than left to the author's memory.
UNIVERSAL_EDGE_CASES: tuple[str, ...] = (
    "Runs twice. The trigger fires again after a timeout or a replay, and "
    "the second run must change nothing. Key the action on something "
    "derived from the order rather than on the time it ran.",
    "The downstream service is down. Decide now whether the order waits in "
    "a retry queue or falls back to the manual path, and make sure the "
    "person on the floor is told which happened.",
    "A human has to be able to override it, without a deploy and without a "
    "ticket. An automation nobody can stop becomes an automation somebody "
    "unplugs.",
    "Every automatic decision is written down with its inputs. An action "
    "with no audit line is one that cannot be argued with when it is wrong, "
    "and it will be wrong.",
)


@dataclass(frozen=True)
class BottleneckPattern:
    key: str
    label: str
    triggers: tuple[str, ...]
    trigger_event: str
    automated_action: str
    specific_edges: tuple[str, ...]
    minutes_saved_per_unit: float
    acceptance: tuple[str, ...]


PATTERNS: tuple[BottleneckPattern, ...] = (
    BottleneckPattern(
        key="label",
        label="Manual label and carrier selection",
        triggers=("label", "carrier", "shipping option", "postage"),
        trigger_event=("Order reaches status packed and has a confirmed "
                       "weight and dimensions."),
        automated_action=(
            "Call the carrier rate service with weight, dimensions and "
            "destination, pick the cheapest option that meets the promised "
            "delivery date, buy the label and attach the PDF to the order."),
        specific_edges=(
            "No carrier returns a rate that meets the promise. Do not buy "
            "the slowest one silently: hold the order and tell someone.",
            "The weight is missing or implausible, meaning zero or above the "
            "carrier ceiling. A wrong label costs more than a held order.",
            "The address failed validation earlier and was accepted anyway. "
            "Buying a label for an undeliverable address buys a return.",
            "The cheapest option changes between the quote and the purchase. "
            "Buy against the quoted price or requote, never assume."),
        minutes_saved_per_unit=6.0,
        acceptance=(
            "A packed order with valid weight gets a label with no human "
            "touch, and the chosen carrier and price appear on the order.",
            "An order with no compliant rate is held with a named reason "
            "rather than shipped on a guess.",
            "Firing the trigger twice buys one label.")),
    BottleneckPattern(
        key="pick",
        label="Pick list generation and shelf routing",
        triggers=("pick", "shelf", "walk", "picking"),
        trigger_event="A batch window closes or a pick cart is assigned.",
        automated_action=(
            "Group open orders into batches by zone, sequence each batch "
            "into a single walking route and issue it to a device."),
        specific_edges=(
            "Stock moved since the batch was built. The route has to handle "
            "a miss without sending the picker back to the start.",
            "Two pickers are issued the same item from the same bin. Reserve "
            "at batch time rather than at pick time.",
            "A priority order arrives mid batch. Decide whether it breaks "
            "the batch or waits for the next one, and say so on the device.",
            "The device loses connection halfway. The picks already made "
            "must survive a reconnect."),
        minutes_saved_per_unit=3.5,
        acceptance=(
            "A batch issues as one route with no backtracking between zones.",
            "A reserved item cannot be issued to a second picker.",
            "A dropped connection loses no completed pick.")),
    BottleneckPattern(
        key="dispatch",
        label="Manual driver assignment and dispatch",
        triggers=("dispatch", "assign", "driver", "route"),
        trigger_event="An order reaches status ready for dispatch.",
        automated_action=(
            "Score available drivers on current load, proximity and shift "
            "remaining, assign the best fit and notify the driver."),
        specific_edges=(
            "No driver is available. Queue it visibly rather than assigning "
            "to somebody who cannot take it.",
            "A driver's shift ends before the delivery window. Never assign "
            "across the end of a shift.",
            "The assigned driver declines or does not acknowledge. Reassign "
            "on a timer rather than waiting for a phone call.",
            "Two orders to the same address arrive separately. Group them "
            "or explain why they were not."),
        minutes_saved_per_unit=4.0,
        acceptance=(
            "A ready order is assigned within one minute or is visibly "
            "queued with a reason.",
            "No assignment crosses the end of a driver's shift.",
            "An unacknowledged assignment reassigns automatically.")),
    BottleneckPattern(
        key="status",
        label="Manual status updates and customer notification",
        triggers=("status", "notify", "email", "update", "customer"),
        trigger_event="An order changes state in the fulfillment system.",
        automated_action=(
            "Map the state change to a customer facing message and send it "
            "on the channel the customer chose."),
        specific_edges=(
            "The state changes twice quickly. Debounce, or the customer gets "
            "two messages a second apart and trusts neither.",
            "The change is a correction rather than progress. A message "
            "saying shipped followed by one saying packed reads as chaos.",
            "The customer opted out of one channel and not another. Honour "
            "the choice per channel rather than per customer.",
            "The message would arrive at three in the morning. Hold "
            "non urgent notifications to a sensible window."),
        minutes_saved_per_unit=1.5,
        acceptance=(
            "Each state change produces at most one message per channel.",
            "An opted out channel is never used.",
            "A correction does not produce a contradictory message.")),
)

PATTERN_BY_KEY = {p.key: p for p in PATTERNS}

PRIORITY_CONSTRAINT = "P1, this is the constraint"
PRIORITY_NOT_CONSTRAINT = "P3, this is not the constraint"


@dataclass(frozen=True)
class SoftwareRequirement:
    manual_bottleneck: str
    matched: bool
    pattern_key: str
    title: str
    trigger_event: str
    automated_action: str
    edge_cases: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    minutes_saved_per_unit: float
    priority: str
    ticket: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def edge_case_count(self) -> int:
        return len(self.edge_cases)


def generate_software_requirement(manual_bottleneck: str,
                                  is_the_constraint: bool = True
                                  ) -> SoftwareRequirement:
    """Write the ticket, and carry the constraint check inside it.

    Automating a step that is not the constraint spends engineering time and
    changes no number on the throughput page, so the priority line says which
    case this is rather than leaving it to be argued about in planning.
    """
    text = str(manual_bottleneck or "").strip()
    if not text:
        raise ValueError("a requirement needs a bottleneck to remove")

    lowered = text.lower()
    pattern = None
    best = 0
    for candidate in PATTERNS:
        hits = sum(1 for t in candidate.triggers if t in lowered)
        if hits > best:
            pattern, best = candidate, hits

    findings: list[Finding] = []

    if pattern is None:
        findings.append(Finding(
            code="REQ-UNMATCHED", severity=SEVERITY_WARN,
            title=f"{text} does not match a pattern this generator knows",
            detail=("No trigger or action has been written, because writing "
                    "a generic one would produce a ticket that looks "
                    "complete and specifies nothing."),
            fix=("Describe the step in terms of what fires it and what a "
                 "person does next, then write the ticket by hand using the "
                 "four universal edge cases below.")))
        findings.append(Finding(
            code="REQ-EDGES", severity=SEVERITY_OK,
            title="The four universal edge cases still apply",
            detail=("They are left out of every automation and found in "
                    "production, whatever the step is."),
            fix="Put all four in the ticket before it is estimated."))
        priority = (PRIORITY_CONSTRAINT if is_the_constraint
                    else PRIORITY_NOT_CONSTRAINT)
        ticket = "\n".join([
            f"TITLE: Automate {text}",
            f"PRIORITY: {priority}",
            "",
            "TRIGGER: not specified, this step did not match a known pattern",
            "ACTION: not specified, see above",
            "",
            "EDGE CASES",
        ] + [f"  {index}. {case}"
             for index, case in enumerate(UNIVERSAL_EDGE_CASES, start=1)])
        return SoftwareRequirement(
            manual_bottleneck=text, matched=False, pattern_key="",
            title=f"Automate {text}", trigger_event="", automated_action="",
            edge_cases=UNIVERSAL_EDGE_CASES, acceptance_criteria=(),
            minutes_saved_per_unit=0.0, priority=priority, ticket=ticket,
            findings=tuple(findings))

    edges = pattern.specific_edges + UNIVERSAL_EDGE_CASES
    priority = (PRIORITY_CONSTRAINT if is_the_constraint
                else PRIORITY_NOT_CONSTRAINT)

    if is_the_constraint:
        findings.append(Finding(
            code="REQ-CONSTRAINT", severity=SEVERITY_OK,
            title="This step is the constraint, so automating it raises throughput",
            detail=(f"Removing {pattern.minutes_saved_per_unit:.1f} minutes "
                    f"a unit from the constraint moves the line. The same "
                    f"work anywhere else would not."),
            fix=("Estimate it against the throughput it buys, not against "
                 "how annoying the step is.")))
    else:
        findings.append(Finding(
            code="REQ-NOT-CONSTRAINT", severity=SEVERITY_CRITICAL,
            title="This step is not the constraint, so automating it changes no number",
            detail=("The units it produces faster will queue in front of "
                    "whatever the constraint is. The engineering is spent "
                    "and the throughput page reads the same."),
            fix=("Take the constraint to this generator instead. If this "
                 "step is being automated for a reason other than "
                 "throughput, such as error rate or staff turnover, say so "
                 "in the ticket so it is judged on that.")))

    findings.append(Finding(
        code="REQ-EDGES", severity=SEVERITY_WARN,
        title=f"{len(edges)} edge cases, four of which apply to every automation",
        detail=("Runs twice, downstream down, human override and audit "
                "trail. They are left out of every automation and found in "
                "production."),
        fix=("Estimate the ticket with the edge cases in it. An estimate "
             "for the happy path alone is the reason automation projects "
             "double.")))

    findings.append(Finding(
        code="REQ-MANUAL-PATH", severity=SEVERITY_WARN,
        title="The manual path has to survive the automation",
        detail=("Every automation fails sometimes, and the floor needs the "
                "old way to still work on that day. Deleting the manual "
                "path on launch is how a bad afternoon becomes a bad week."),
        fix="Keep it until the automation has run a full peak without intervention."))

    ticket = "\n".join([
        f"TITLE: Automate {pattern.label}",
        f"PRIORITY: {priority}",
        f"EXPECTED SAVING: {pattern.minutes_saved_per_unit:.1f} minutes per unit",
        "",
        f"TRIGGER",
        f"  {pattern.trigger_event}",
        "",
        f"AUTOMATED ACTION",
        f"  {pattern.automated_action}",
        "",
        "EDGE CASES",
    ] + [f"  {index}. {case}" for index, case in enumerate(edges, start=1)] + [
        "",
        "ACCEPTANCE CRITERIA",
    ] + [f"  {index}. {line}"
         for index, line in enumerate(pattern.acceptance, start=1)])

    return SoftwareRequirement(
        manual_bottleneck=text, matched=True, pattern_key=pattern.key,
        title=f"Automate {pattern.label}",
        trigger_event=pattern.trigger_event,
        automated_action=pattern.automated_action, edge_cases=edges,
        acceptance_criteria=pattern.acceptance,
        minutes_saved_per_unit=pattern.minutes_saved_per_unit,
        priority=priority, ticket=ticket, findings=tuple(findings),
    )
