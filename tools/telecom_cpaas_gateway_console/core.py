"""Telecom CPaaS, USSD, and SMPP gateway engine.

No Streamlit import lives in this file.

Three failures shape it, and all three are quiet ones that log as
successes:

* a USSD session is held open by the network, not by the handset. Once
  the operator's timer fires the session is gone, and anything the
  application sends after that is dropped before it reaches the phone.
  The application sees its own write succeed and the subscriber sees the
  operator's timeout text, so the logs say delivered and the customer
  says nothing happened;
* failing over to a second carrier moves the traffic and does not move
  the message identifiers. A delivery receipt for a message submitted to
  the primary arrives on the primary's session, keyed to the primary's
  identifier, and an application correlating on the identifier alone will
  never match it. Those messages sit as sent with no receipt forever;
* coming back from an outage at full rate is how the reconnection fails.
  The backlog is dumped at the licensed rate into a bind the carrier has
  only just accepted, the carrier throttles, and the retry logic reads
  the throttle as a delivery failure and sends everything twice.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# 1. USSD session lifecycle
# ---------------------------------------------------------------------------

STATE_ACTIVE = "Active"
STATE_TIMED_OUT = "Timed Out"
STATE_COMPLETED = "Completed"

# The application's own budget, which has to sit inside the operator's.
APPLICATION_TIMEOUT_SECONDS = 10
# A USSD string is limited to 182 alphanumeric characters on the air
# interface, so a menu longer than that is cut rather than wrapped.
USSD_MAX_CHARACTERS = 182
# The whole session is capped by the operator, not by the application.
NETWORK_SESSION_BUDGET_SECONDS = 180

# The convention every USSD aggregator uses: CON holds the session open
# for another input, END releases it.
PREFIX_CONTINUE = "CON "
PREFIX_END = "END "


@dataclass(frozen=True)
class MenuStep:
    step: int
    prompt: str
    terminal: bool

    @property
    def payload(self) -> str:
        prefix = PREFIX_END if self.terminal else PREFIX_CONTINUE
        return prefix + self.prompt


MENU_TREE: tuple[MenuStep, ...] = (
    MenuStep(0, ("Welcome to Sahel Mobile\n"
                 "1. Check balance\n"
                 "2. Buy airtime\n"
                 "3. Buy a data bundle\n"
                 "4. Send money"), False),
    MenuStep(1, ("Buy airtime\n"
                 "1. 500\n"
                 "2. 1000\n"
                 "3. 2000\n"
                 "4. Another amount\n"
                 "0. Back"), False),
    MenuStep(2, ("Confirm 1000 airtime for this line\n"
                 "1. Confirm\n"
                 "2. Cancel"), False),
    MenuStep(3, ("Enter your four digit PIN to confirm"), False),
    MenuStep(4, ("Airtime of 1000 is on its way. "
                 "You will receive an SMS receipt shortly."), True),
)


def validate_menu_payload(payload: str) -> tuple[bool, str]:
    """Check one USSD payload on its own, without running a session.

    Usable against a menu string straight out of an existing codebase:
    it is the same check the simulator applies, exposed so the result can
    be reproduced without this engine.
    """
    text = str(payload or "")
    if not text.startswith((PREFIX_CONTINUE, PREFIX_END)):
        return False, (f"A payload has to start with {PREFIX_CONTINUE!r} to "
                       f"hold the session or {PREFIX_END!r} to release it. "
                       f"Without a prefix the aggregator has no way to know "
                       f"which, and most of them close the session.")
    if len(text) > USSD_MAX_CHARACTERS:
        return False, (f"{len(text)} characters against the "
                       f"{USSD_MAX_CHARACTERS} character limit. The string "
                       f"is cut rather than wrapped, so the options past "
                       f"the cut are invisible and the subscriber selects a "
                       f"number they cannot see.")
    return True, (f"{len(text)} characters, "
                  f"{'holds' if text.startswith(PREFIX_CONTINUE) else 'releases'}"
                  f" the session.")


@dataclass(frozen=True)
class UssdSession:
    session_id: str
    step: int
    response_delay_seconds: float
    elapsed_seconds: float
    state: str
    menu_payload: str
    payload_delivered: bool
    intercept_message: str
    characters: int
    truncated: bool
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def active(self) -> bool:
        return self.state == STATE_ACTIVE

    @property
    def timed_out(self) -> bool:
        return self.state == STATE_TIMED_OUT

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def simulate_ussd_session(session_id: str, step: int,
                          response_delay_seconds: float,
                          elapsed_seconds: float = 0.0) -> UssdSession:
    """Advance one USSD step, and refuse to pretend a late reply arrives.

    The session lives on the operator's gateway. When its timer fires the
    session is released, and a response the application writes afterwards
    is discarded before it reaches the handset. The application's socket
    write still succeeds, which is why this failure reads as a success in
    every log the application keeps.

    So a timed out step returns no deliverable payload at all. The text
    the application would have sent is returned separately as an intercept
    message, to be written to the log rather than to the subscriber.
    """
    identifier = str(session_id or "").strip()
    index = int(step)
    delay = float(response_delay_seconds)
    elapsed = float(elapsed_seconds)
    findings: list[Finding] = []

    if not identifier:
        raise ValueError("a USSD session identifier is required")
    if delay < 0:
        raise ValueError("a response delay cannot be negative")
    if elapsed < 0:
        raise ValueError("elapsed session time cannot be negative")
    if not 0 <= index < len(MENU_TREE):
        raise ValueError(f"step {step} is outside the menu tree")

    node = MENU_TREE[index]
    payload = node.payload
    characters = len(payload)
    truncated = characters > USSD_MAX_CHARACTERS
    total_elapsed = elapsed + delay

    over_application = delay > APPLICATION_TIMEOUT_SECONDS
    over_network = total_elapsed > NETWORK_SESSION_BUDGET_SECONDS

    if over_application or over_network:
        state = STATE_TIMED_OUT
        delivered = False
        reason = ("the application budget of "
                  f"{APPLICATION_TIMEOUT_SECONDS} second(s)" if over_application
                  else f"the operator session cap of "
                       f"{NETWORK_SESSION_BUDGET_SECONDS} second(s)")
        intercept = (
            f"Session {identifier} exceeded {reason} at step {index}. "
            f"The network released the session, so the {characters} "
            f"character payload below was never delivered. The subscriber "
            f"saw the operator's own timeout text.")
        findings.append(Finding(
            code="USD-TIMEOUT", severity=SEVERITY_CRITICAL,
            title=f"{delay:.1f}s reply on a "
                  f"{APPLICATION_TIMEOUT_SECONDS}s budget",
            detail=("The session was released before the response was "
                    "written. The write itself succeeds, because the "
                    "application is writing to its own gateway connection "
                    "rather than to the handset, so this failure appears in "
                    "no application log as a failure."),
            fix=("Budget every downstream call inside the USSD step and "
                 "return a holding menu rather than waiting. A menu that "
                 "says checking is a session that is still alive.")))
        findings.append(Finding(
            code="USD-NORESUME", severity=SEVERITY_CRITICAL,
            title="A released session cannot be resumed",
            detail=("All state is held server side against the session "
                    "identifier, and the identifier changes when the "
                    "subscriber dials again. There is no reconnect, no "
                    "retry, and no way to put them back where they were. "
                    "They start at the root menu."),
            fix=("Persist the partial transaction against the subscriber "
                 "number, not the session, so a redial can offer to "
                 "continue.")))
    elif node.terminal:
        state = STATE_COMPLETED
        delivered = True
        intercept = ""
        findings.append(Finding(
            code="USD-END", severity=SEVERITY_OK,
            title="The session closed cleanly with an END payload",
            detail=("The END prefix releases the session on the operator's "
                    "gateway. Leaving it as CON holds a session open until "
                    "the operator's cap expires, which consumes a session "
                    "slot the subscriber cannot use for anything else."),
            fix="Always terminate with END. Never rely on the cap."))
    else:
        state = STATE_ACTIVE
        delivered = True
        intercept = ""
        findings.append(Finding(
            code="USD-CON", severity=SEVERITY_OK,
            title=f"Step {index} delivered in {delay:.1f}s, session held",
            detail=("The CON prefix holds the session open for the next "
                    "input. Every step spends from the same operator cap, "
                    "so a menu three levels deep needs the sum of its steps "
                    "to fit inside it."),
            fix="Count the steps, not just the seconds in each one."))

    if truncated:
        findings.append(Finding(
            code="USD-LENGTH", severity=SEVERITY_CRITICAL,
            title=f"{characters} characters against a {USSD_MAX_CHARACTERS} "
                  f"limit",
            detail=("A USSD string is cut at the limit rather than wrapped "
                    "to a second screen, so the options past the cut are "
                    "invisible and the subscriber selects a number they "
                    "cannot see."),
            fix="Split the menu across a step rather than trusting the cut."))

    remaining = NETWORK_SESSION_BUDGET_SECONDS - total_elapsed
    if state != STATE_TIMED_OUT and remaining < 30:
        findings.append(Finding(
            code="USD-BUDGET", severity=SEVERITY_WARN,
            title=f"{remaining:.0f}s left of the operator session cap",
            detail=("The cap covers the whole session and not each step. A "
                    "flow that fits comfortably in testing runs out on a "
                    "subscriber who reads slowly."),
            fix="Test the flow at a realistic reading speed, not at yours."))

    findings.append(Finding(
        code="USD-NORECEIPT", severity=SEVERITY_WARN,
        title="USSD has no delivery receipt",
        detail=("There is no acknowledgement that the subscriber saw the "
                "menu, only that the gateway accepted it. A transaction "
                "confirmed over USSD has to be confirmed again over SMS, "
                "which is the only leg that reports back."),
        fix="Send an SMS receipt for anything that moved money."))

    headline = f"{identifier} step {index}: {state}"
    return UssdSession(
        session_id=identifier, step=index, response_delay_seconds=delay,
        elapsed_seconds=total_elapsed, state=state,
        menu_payload=payload if delivered else "",
        payload_delivered=delivered, intercept_message=intercept,
        characters=characters, truncated=truncated, headline=headline,
        findings=tuple(findings))


# ---------------------------------------------------------------------------
# 2. SMPP carrier failover
# ---------------------------------------------------------------------------

PRIMARY_BOUND = "Bound and accepting"
PRIMARY_THROTTLED = "Bound, returning ESME_RTHROTTLED"
PRIMARY_QUEUE_FULL = "Bound, returning ESME_RMSGQFUL"
PRIMARY_UNBOUND = "Unbound, the carrier closed the session"
PRIMARY_LINK_DEAD = "Link dead, enquire_link unanswered"

PRIMARY_STATES: tuple[str, ...] = (PRIMARY_BOUND, PRIMARY_THROTTLED,
                                   PRIMARY_QUEUE_FULL, PRIMARY_UNBOUND,
                                   PRIMARY_LINK_DEAD)

ROUTE_PRIMARY = "Submitting on the primary bind"
ROUTE_SECONDARY = "Failed over to the secondary carrier"
ROUTE_HELD = "Held in the queue, no route available"
ROUTE_BACKPRESSURE = "Backpressure, submitting below the licensed rate"

LICENSED_TPS = 100
RECONNECT_START_FRACTION = 0.10
RAMP_STEP_SECONDS = 30
RAMP_MULTIPLIER = 2.0
ENQUIRE_LINK_SECONDS = 30
SMPP_WINDOW_SIZE = 10


@dataclass(frozen=True)
class RampStep:
    at_second: int
    tps: int


@dataclass(frozen=True)
class FailoverResult:
    pending_sms_count: int
    primary_status: str
    secondary_available: bool
    route: str
    submitting_tps: int
    queued: int
    in_flight: int
    drain_seconds: float
    ramp: tuple[RampStep, ...]
    dlr_at_risk: int
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def routed(self) -> bool:
        return self.route in (ROUTE_PRIMARY, ROUTE_SECONDARY,
                              ROUTE_BACKPRESSURE)

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def reconnect_ramp(licensed_tps: int = LICENSED_TPS) -> tuple[RampStep, ...]:
    """The rate schedule after a bind is re established.

    Dumping a backlog at the licensed rate into a session the carrier has
    only just accepted is how the reconnection fails. The carrier
    throttles, the retry logic reads the throttle as a delivery failure,
    and the backlog goes out twice.
    """
    steps: list[RampStep] = []
    rate = max(1, int(licensed_tps * RECONNECT_START_FRACTION))
    second = 0
    while True:
        capped = min(rate, licensed_tps)
        steps.append(RampStep(second, capped))
        if capped >= licensed_tps:
            break
        rate = int(rate * RAMP_MULTIPLIER)
        second += RAMP_STEP_SECONDS
    return tuple(steps)


def simulate_smpp_carrier_failover(pending_sms_count: int,
                                   primary_status: str,
                                   secondary_available: bool
                                   ) -> FailoverResult:
    """Route the backlog, and count the receipts the failover strands.

    Failing over moves the traffic and does not move the message
    identifiers. A delivery receipt for a message already submitted to the
    primary arrives on the primary's session and carries the primary's
    identifier, so an application correlating on that identifier alone
    never matches it once the traffic has moved.
    """
    pending = int(pending_sms_count)
    status = str(primary_status or "").strip()
    secondary = bool(secondary_available)
    findings: list[Finding] = []

    if pending < 0:
        raise ValueError("a pending count cannot be negative")
    if status not in PRIMARY_STATES:
        raise ValueError(f"unknown primary status {primary_status!r}")

    ramp = reconnect_ramp()

    if status == PRIMARY_BOUND:
        route = ROUTE_PRIMARY
        tps = LICENSED_TPS
        dlr_at_risk = 0
        findings.append(Finding(
            code="SMP-PRIMARY", severity=SEVERITY_OK,
            title=f"Submitting on the primary at {tps} TPS",
            detail=(f"The bind is healthy and enquire_link is answering "
                    f"inside {ENQUIRE_LINK_SECONDS} seconds. The window "
                    f"holds {SMPP_WINDOW_SIZE} unacknowledged PDUs, and "
                    f"filling it stalls the submit loop whatever the "
                    f"licensed rate says."),
            fix="Size the window against the round trip, not against TPS."))
    elif status in (PRIMARY_THROTTLED, PRIMARY_QUEUE_FULL):
        route = ROUTE_BACKPRESSURE
        tps = ramp[0].tps
        dlr_at_risk = 0
        code = ("ESME_RTHROTTLED" if status == PRIMARY_THROTTLED
                else "ESME_RMSGQFUL")
        findings.append(Finding(
            code="SMP-BACKPRESSURE", severity=SEVERITY_CRITICAL,
            title=f"{code} is backpressure, not a delivery failure",
            detail=("The carrier is telling the sender to slow down. Retry "
                    "logic that treats this status as a failed send "
                    "resubmits the same message, which is how a throttle "
                    "becomes a duplicate. The subscriber gets two one time "
                    "codes and the second one invalidates the first."),
            fix=(f"Back off and resubmit the same message identifier. Never "
                 f"generate a new one for a {code} response.")))
    elif secondary:
        route = ROUTE_SECONDARY
        tps = ramp[0].tps
        dlr_at_risk = pending
        findings.append(Finding(
            code="SMP-DLR", severity=SEVERITY_CRITICAL,
            title=f"{dlr_at_risk} message(s) will never match a receipt",
            detail=("Message identifiers are issued by the carrier that "
                    "accepted the submit. Everything already on the primary "
                    "will have its receipt delivered on the primary's "
                    "session, under the primary's identifier, and an "
                    "application keyed on the identifier alone will not "
                    "match it once the traffic has moved. Those messages "
                    "sit as sent with no receipt forever."),
            fix=("Key the correlation on the carrier and the identifier "
                 "together, and keep the primary's receiver bind open "
                 "through the failover even when the transmitter has "
                 "moved.")))
        findings.append(Finding(
            code="SMP-FAILOVER", severity=SEVERITY_WARN,
            title=f"Secondary accepted the route at {tps} TPS",
            detail=("The secondary is a different carrier with a different "
                    "licensed rate, a different sender identifier policy, "
                    "and frequently a different delivery receipt format. "
                    "Routing to it is the easy part."),
            fix=("Test the secondary monthly with real traffic. A failover "
                 "path first exercised during an outage is not a failover "
                 "path.")))
    else:
        route = ROUTE_HELD
        tps = 0
        dlr_at_risk = 0
        findings.append(Finding(
            code="SMP-HELD", severity=SEVERITY_CRITICAL,
            title=f"{pending} message(s) held with nowhere to go",
            detail=("The primary is not accepting and no secondary is "
                    "configured. The queue is the only thing keeping these "
                    "messages, so its durability is now the whole delivery "
                    "guarantee, and an in memory queue has just become the "
                    "single point of failure."),
            fix=("Persist the queue before the outage, not during one, and "
                 "alert on queue depth rather than on bind state.")))

    if status == PRIMARY_LINK_DEAD:
        findings.append(Finding(
            code="SMP-ENQUIRE", severity=SEVERITY_CRITICAL,
            title=f"enquire_link unanswered past {ENQUIRE_LINK_SECONDS}s",
            detail=("A TCP socket stays open long after the far end has "
                    "stopped processing, so a dead SMPP link looks "
                    "connected. Without the keepalive the submit loop "
                    "writes into a socket nobody is reading and every "
                    "message is lost with no error at all."),
            fix=(f"Send enquire_link every {ENQUIRE_LINK_SECONDS} seconds "
                 f"and unbind after two missed responses.")))

    if status == PRIMARY_UNBOUND:
        findings.append(Finding(
            code="SMP-UNBIND", severity=SEVERITY_WARN,
            title="The carrier closed the session, which is often deliberate",
            detail=("Carriers unbind for a nightly restart, for a credit "
                    "limit, and for exceeding the licensed rate. The "
                    "reconnect logic cannot tell those apart, so a bind "
                    "loop against a credit stop becomes a denial of service "
                    "against the carrier's own gateway."),
            fix=("Back off exponentially with a cap, and alert a human "
                 "after the third failed bind rather than retrying "
                 "forever.")))

    if tps > 0:
        drain = round(pending / tps, 1)
        in_flight = min(pending, SMPP_WINDOW_SIZE)
        queued = max(0, pending - in_flight)
    else:
        drain = float("inf")
        in_flight = 0
        queued = pending

    if route in (ROUTE_SECONDARY, ROUTE_BACKPRESSURE) and pending > 0:
        findings.append(Finding(
            code="SMP-RAMP", severity=SEVERITY_WARN,
            title=f"Ramping from {ramp[0].tps} to {ramp[-1].tps} TPS over "
                  f"{ramp[-1].at_second}s",
            detail=(f"Starting at the licensed {LICENSED_TPS} TPS into a "
                    f"bind the carrier has just accepted gets the sender "
                    f"throttled inside a second, and the backlog is exactly "
                    f"the traffic shape that triggers it. The drain "
                    f"estimate of {drain} seconds assumes the ramp is "
                    f"respected."),
            fix="Ramp on every reconnect, including the ones that look fine."))

    findings.append(Finding(
        code="SMP-IDEMPOTENT", severity=SEVERITY_CRITICAL,
        title="A resubmit without an idempotency key is a second SMS",
        detail=("Nothing in SMPP deduplicates. A message submitted to the "
                "primary and then again to the secondary is delivered "
                "twice, charged twice, and for a one time code the second "
                "one usually invalidates the first."),
        fix=("Carry your own identifier on every message and check it "
             "before any resubmit, on either carrier.")))

    headline = (f"{pending} pending, {route.lower()}, {tps} TPS")
    return FailoverResult(
        pending_sms_count=pending, primary_status=status,
        secondary_available=secondary, route=route, submitting_tps=tps,
        queued=queued, in_flight=in_flight, drain_seconds=drain, ramp=ramp,
        dlr_at_risk=dlr_at_risk, headline=headline,
        findings=tuple(findings))


# ---------------------------------------------------------------------------
# 3. Architecture matrix
# ---------------------------------------------------------------------------

STACK_KANNEL = "Kannel"
STACK_JASMIN = "Jasmin"
STACK_CUSTOM = "Custom build"


@dataclass(frozen=True)
class StackOption:
    name: str
    language: str
    speaks_smpp_34: bool
    protocol_compliance: str
    scalability: str
    delivery_weeks_low: int
    delivery_weeks_high: int
    operational_cost: str
    choose_when: str
    avoid_when: str
    edge_cases_owned: str


_MATRIX: tuple[StackOption, ...] = (
    StackOption(
        name=STACK_KANNEL, language="C",
        speaks_smpp_34=True,
        protocol_compliance=("SMPP 3.4 and the WAP stack, mature and long "
                             "proven against a wide range of carrier "
                             "quirks."),
        scalability=("Scales vertically. Horizontal scaling means running "
                     "several instances and splitting the routing outside "
                     "the gateway, which is work the gateway does not do "
                     "for you."),
        delivery_weeks_low=2, delivery_weeks_high=5,
        operational_cost=("Configuration in a single file, few moving "
                          "parts, and an operator who has to read C to "
                          "diagnose anything unusual."),
        choose_when=("The volume is known, the carrier list is short, and "
                     "the team values a component that has survived twenty "
                     "years of carriers."),
        avoid_when=("The routing rules will change often, or the plan "
                    "needs several instances behind one logical sender."),
        edge_cases_owned=("Almost none. Two decades of carriers have "
                          "already been handled.")),
    StackOption(
        name=STACK_JASMIN, language="Python on Twisted",
        speaks_smpp_34=True,
        protocol_compliance=("SMPP 3.4 with an HTTP API in front, plus a "
                             "router with filters, backed by a message "
                             "broker and a cache."),
        scalability=("Designed to be clustered, with the broker carrying "
                     "the queue rather than the process, so an instance "
                     "can be lost without losing the backlog."),
        delivery_weeks_low=3, delivery_weeks_high=7,
        operational_cost=("More parts to run: the gateway, the broker, and "
                          "the cache. Each is ordinary to operate and "
                          "there are three of them."),
        choose_when=("Routing rules change, several senders share the "
                     "platform, or the queue has to survive an instance "
                     "dying."),
        avoid_when=("The team has nobody who will own a broker, in which "
                    "case the queue's durability is theoretical."),
        edge_cases_owned=("Few. The common carrier behaviours are handled "
                          "and the router is where your own logic goes.")),
    StackOption(
        name=STACK_CUSTOM, language="Whatever the team already runs",
        speaks_smpp_34=True,
        protocol_compliance=("Exactly what you implement. Speaking SMPP is "
                             "a fortnight. The edges are the year."),
        scalability=("Whatever you design, which is the honest advantage: "
                     "nothing constrains the shape."),
        delivery_weeks_low=16, delivery_weeks_high=52,
        operational_cost=("You are the vendor. Every carrier quirk arrives "
                          "as a production incident first and a code change "
                          "second."),
        choose_when=("The product is the gateway, or a carrier requires "
                     "something no existing stack does."),
        avoid_when=("The gateway is a means to an end, which it is in "
                    "almost every project that considers building one."),
        edge_cases_owned=("All of them: window management, unbind mid "
                          "window, receipts with unknown identifiers, "
                          "non standard receipt formats, sequence number "
                          "wrap, and reconnect storms.")),
)


def get_telecom_architecture_matrix() -> tuple[StackOption, ...]:
    """The three options, compared on the axis that decides.

    Every one of them speaks SMPP 3.4, so protocol support is not the
    question. The question is who owns the edges: a receipt carrying an
    identifier the gateway never issued, a carrier that unbinds with the
    window half full, a sequence number that wraps. A custom build
    re earns every one of those, one production incident at a time.
    """
    return _MATRIX


def get_stack(name: str) -> StackOption:
    for option in _MATRIX:
        if option.name == name:
            return option
    raise KeyError(name)


def compare_stacks() -> dict:
    """Facts about the matrix that the matrix exists to demonstrate."""
    by_speed = sorted(_MATRIX, key=lambda s: s.delivery_weeks_low)
    custom = get_stack(STACK_CUSTOM)
    fastest = by_speed[0]
    return {
        "by_delivery": tuple(s.name for s in by_speed),
        "fastest": fastest.name,
        "custom_multiple": round(
            custom.delivery_weeks_low / fastest.delivery_weeks_low, 1),
        "all_speak_smpp_34": all(s.speaks_smpp_34 for s in _MATRIX),
        "the_axis_that_decides": (
            "All three speak SMPP 3.4, so protocol support decides "
            "nothing. What decides is who owns the edges, and a custom "
            "build owns every one of them."),
    }
