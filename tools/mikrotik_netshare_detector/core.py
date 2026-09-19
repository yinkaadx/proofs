"""MikroTik NetShare Detection Console: the engine.

Detecting a tethered device on a hotspot is heuristic work, and the honest
version of it says so on every screen. The cost of a false positive here is
not a log line. It is a paying customer cut off mid session, at a counter, in
front of other customers, by a rule nobody can explain to them.

So the engine is built around three things.

1. A MAC address can never detect tethering. Everything downstream of the
   phone is translated behind it, so the access point sees one MAC whatever
   is behind it. The MAC is checked for shape and for a randomised bit, and
   it contributes nothing to the verdict. Saying that plainly is better than
   a tool that quietly scores it.
2. TTL is the one strong signal, because a phone acting as a router
   decrements it, and a value exactly one below a native start is a device
   that has taken exactly one extra hop. It is still not proof: a traveller
   with their own pocket router produces the same reading and is doing
   nothing wrong.
3. Everything else is weak, and weak signals only count in combination. One
   weak signal never reaches a detection, however suggestive it looks on its
   own, because that is the setting that generates the counter argument.

No Streamlit import lives here on purpose, so the same engine could sit behind
a real polling daemon without a line changing.
"""

from __future__ import annotations

import re
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
# 1. Tethering heuristics
# ---------------------------------------------------------------------------

MAC_PATTERN = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")

# Where an operating system starts its TTL before anything touches it. A
# packet arriving at one of these values has taken no extra hop that the
# hotspot can see.
NATIVE_TTLS: tuple[int, ...] = (64, 128, 255)

# One below a native start is exactly one extra hop, which is what a phone
# sharing its connection does to every packet that passes through it.
ONE_HOP_TTLS: tuple[int, ...] = tuple(t - 1 for t in NATIVE_TTLS)

# Weak signals only count together, so the threshold sits above any one of
# them and below any two.
WEAK_SIGNAL_WEIGHT = 20
STRONG_SIGNAL_WEIGHT = 55
DETECTION_THRESHOLD = 40
# Above this the evidence is strong enough to act on without a human first.
# It is set so that no combination of weak signals alone can reach it.
AUTO_ACTION_THRESHOLD = 70

RULE_TTL = "TTL Variance"
RULE_MULTI_OS = "Multi OS Fingerprint"
RULE_PORT_BREADTH = "Concurrent Port Breadth"
RULE_NONE = "No rule triggered"

# Ports open at once from one subscriber. A modern phone alone opens a great
# many, so this bar is deliberately high and the signal is still weak.
PORT_BREADTH_THRESHOLD = 180


@dataclass(frozen=True)
class Signal:
    rule: str
    triggered: bool
    weight: int
    strength: str
    evidence: str
    caveat: str


@dataclass(frozen=True)
class TetheringVerdict:
    mac_address: str
    ttl_value: int
    user_agent_count: int
    connection_ports: int
    detected: bool
    rule_triggered: str
    score: int
    signals: tuple[Signal, ...]
    triggered_rules: tuple[str, ...]
    confidence: str
    safe_to_auto_block: bool
    mac_is_randomised: bool
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def score_from_signals(self) -> int:
        """The total, recomputed from the rows it is printed beside."""
        return sum(s.weight for s in self.signals if s.triggered)


def mac_is_randomised(mac_address: str) -> bool:
    """True when the locally administered bit is set in the first octet.

    Phones randomise their MAC per network by default now. It tells you the
    address is not the factory one. It tells you nothing about tethering.
    """
    first = int(str(mac_address).replace("-", ":").split(":")[0], 16)
    return bool(first & 0b00000010)


def analyze_tethering_heuristics(mac_address: str, ttl_value: int,
                                 user_agent_count: int,
                                 connection_ports: int) -> TetheringVerdict:
    """Weigh the signals and say what triggered, without claiming proof.

    The MAC is validated and then deliberately given no weight, because
    everything behind a tethering phone shares that one address and no
    inspection of it can separate them.
    """
    mac = str(mac_address or "").strip()
    if not MAC_PATTERN.match(mac):
        raise ValueError(f"not a MAC address: {mac_address!r}")
    ttl = int(ttl_value)
    if not 1 <= ttl <= 255:
        raise ValueError("a TTL is between 1 and 255")
    agents = int(user_agent_count)
    ports = int(connection_ports)
    if agents < 0 or ports < 0:
        raise ValueError("a count cannot be negative")

    findings: list[Finding] = []
    signals: list[Signal] = []

    # ---- TTL, the only strong signal -------------------------------------
    one_hop = ttl in ONE_HOP_TTLS
    further = ttl not in NATIVE_TTLS and not one_hop
    signals.append(Signal(
        rule=RULE_TTL, triggered=one_hop or further,
        weight=STRONG_SIGNAL_WEIGHT if one_hop else (
            WEAK_SIGNAL_WEIGHT if further else 0),
        strength="strong" if one_hop else ("weak" if further else "none"),
        evidence=(f"TTL {ttl} is exactly one below the native "
                  f"{ttl + 1}, which is one extra hop" if one_hop else
                  f"TTL {ttl} is not a native start and not one hop below "
                  f"one, so the path is longer or something rewrote it"
                  if further else
                  f"TTL {ttl} is a native start, so this packet took no "
                  f"extra hop the hotspot can see"),
        caveat=("A traveller with their own pocket router produces the same "
                "reading and is breaking no rule. Some phones and most VPN "
                "clients also normalise TTL, so its absence proves nothing "
                "either.")))

    # ---- Multiple operating systems, weak and getting weaker -------------
    multi_os = agents >= 2
    signals.append(Signal(
        rule=RULE_MULTI_OS, triggered=multi_os,
        weight=WEAK_SIGNAL_WEIGHT if multi_os else 0,
        strength="weak" if multi_os else "none",
        evidence=(f"{agents} distinct operating system families seen in "
                  f"plain text request headers" if multi_os else
                  f"{agents} operating system family seen"),
        caveat=("Almost all traffic is encrypted now, so this signal only "
                "sees the small unencrypted remainder. One person with a "
                "phone and a work laptop on the same voucher also produces "
                "it, legitimately, on a family plan.")))

    # ---- Port breadth, the weakest of the three --------------------------
    broad = ports >= PORT_BREADTH_THRESHOLD
    signals.append(Signal(
        rule=RULE_PORT_BREADTH, triggered=broad,
        weight=WEAK_SIGNAL_WEIGHT if broad else 0,
        strength="weak" if broad else "none",
        evidence=(f"{ports} concurrent connections, above the "
                  f"{PORT_BREADTH_THRESHOLD} bar" if broad else
                  f"{ports} concurrent connections, inside the "
                  f"{PORT_BREADTH_THRESHOLD} bar"),
        caveat=("One modern phone with a browser, a sync client and a video "
                "call open reaches this on its own. Treat it as a reason to "
                "look, never as a reason to act.")))

    score = sum(s.weight for s in signals if s.triggered)
    triggered = tuple(s.rule for s in signals if s.triggered)
    detected = score >= DETECTION_THRESHOLD

    if not triggered:
        rule = RULE_NONE
    else:
        rule = max((s for s in signals if s.triggered),
                   key=lambda s: s.weight).rule

    if score >= AUTO_ACTION_THRESHOLD:
        confidence = "HIGH"
    elif detected:
        confidence = "MODERATE"
    elif score:
        confidence = "LOW"
    else:
        confidence = "NONE"

    safe_to_auto = score >= AUTO_ACTION_THRESHOLD

    randomised = mac_is_randomised(mac)
    findings.append(Finding(
        code="MAC-NOT-A-SIGNAL", severity=SEVERITY_OK,
        title="The MAC address contributes nothing to this verdict",
        detail=("Everything behind a tethering phone is translated onto that "
                "one address, so the access point sees a single MAC whether "
                "there is one device or six. No inspection of it separates "
                "them."),
        fix=("Use it to identify the subscriber, never to decide whether "
             "they are sharing. A tool that scores the MAC is scoring noise.")))

    if randomised:
        findings.append(Finding(
            code="MAC-RANDOM", severity=SEVERITY_WARN,
            title=f"{mac} is a randomised address rather than a factory one",
            detail=("The locally administered bit is set, which phones do by "
                    "default per network now. It means this address will "
                    "change, so anything keyed to it expires."),
            fix=("Key the session to the voucher rather than to the MAC, or "
                 "the same person reappears as a new device tomorrow.")))

    if detected and not safe_to_auto:
        findings.append(Finding(
            code="DET-REVIEW", severity=SEVERITY_CRITICAL,
            title=f"Detected at {score} points, which is not enough to act alone",
            detail=(f"The evidence passes the {DETECTION_THRESHOLD} point "
                    f"bar for a flag and sits under the "
                    f"{AUTO_ACTION_THRESHOLD} point bar for acting without a "
                    f"person. Cutting a paying customer off on this is the "
                    f"argument at the counter."),
            fix=("Queue it for review with the evidence attached. If nobody "
                 "is ever going to review the queue, lower the bar "
                 "deliberately and own that decision rather than letting the "
                 "threshold drift into it.")))
    elif safe_to_auto:
        findings.append(Finding(
            code="DET-STRONG", severity=SEVERITY_CRITICAL,
            title=f"Strong evidence at {score} points",
            detail=("The TTL signal is present alongside at least one other. "
                    "That combination is hard to produce accidentally."),
            fix=("Even here, send the customer a message before the cut "
                 "rather than after it. A warning converts far more often "
                 "than an appeal does.")))
    elif score:
        findings.append(Finding(
            code="DET-WEAK", severity=SEVERITY_WARN,
            title=f"{score} points, under the {DETECTION_THRESHOLD} point bar",
            detail=("Something is worth watching and nothing is worth doing. "
                    "One weak signal on its own never reaches a detection "
                    "here, on purpose."),
            fix="Log it and move on. Acting on this is how the false positives start."))
    else:
        findings.append(Finding(
            code="DET-CLEAN", severity=SEVERITY_OK,
            title="No rule triggered",
            detail="Nothing in this sample suggests a shared connection.",
            fix=("Absence of a signal is not absence of sharing. A phone "
                 "that normalises TTL defeats the only strong rule here.")))

    findings.append(Finding(
        code="DET-HEURISTIC", severity=SEVERITY_WARN,
        title="Every rule here is a heuristic and none of them is proof",
        detail=("The cost of being wrong is a paying customer cut off at a "
                "counter by a rule nobody can explain to them, which is a "
                "refund and a review rather than a log line."),
        fix=("Write the appeal path before the detection goes live, and make "
             "sure the staff on the desk can unblock without a ticket.")))

    if one_hop:
        headline = (f"{RULE_TTL} triggered: TTL {ttl} is one hop below "
                    f"{ttl + 1}, scoring {score}")
    elif triggered:
        headline = (f"{len(triggered)} weak signal(s) triggered, scoring "
                    f"{score} of the {DETECTION_THRESHOLD} needed")
    else:
        headline = f"No rule triggered on this sample, scoring {score}"

    return TetheringVerdict(
        mac_address=mac, ttl_value=ttl, user_agent_count=agents,
        connection_ports=ports, detected=detected, rule_triggered=rule,
        score=score, signals=tuple(signals), triggered_rules=triggered,
        confidence=confidence, safe_to_auto_block=safe_to_auto,
        mac_is_randomised=randomised, headline=headline,
        findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# 2. The API polling cycle
# ---------------------------------------------------------------------------

API_PORT_PLAIN = 8728
API_PORT_TLS = 8729
POLL_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class PollStep:
    ordinal: int
    phase: str
    detail: str
    holds_socket: bool


@dataclass(frozen=True)
class PollCycle:
    interval_seconds: int
    port: int
    steps: tuple[PollStep, ...]
    log: tuple[str, ...]
    sockets_left_open: int
    stateless: bool
    rationale: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def closes_what_it_opens(self) -> bool:
        """Opened and closed in the same cycle, checked against the steps."""
        opens = sum(1 for s in self.steps if s.phase == "connect")
        closes = sum(1 for s in self.steps if s.phase == "close")
        return opens == closes and self.sockets_left_open == 0


def simulate_api_polling_cycle(use_tls: bool = True,
                               interval_seconds: int = POLL_INTERVAL_SECONDS
                               ) -> PollCycle:
    """One complete poll, opened and closed, with nothing held between runs.

    A minute apart and stateless rather than a held connection, because a
    router that reboots, an interface that flaps or a firewall that ages out
    an idle session all leave a held socket looking alive and reading
    nothing. A connection that is built and torn down every cycle cannot
    silently go stale: if it fails, it fails loudly, this minute, and the
    next minute tries again from nothing.
    """
    interval = int(interval_seconds)
    if interval <= 0:
        raise ValueError("a poll interval has to be above zero")
    if interval > 3600:
        raise ValueError("an hour between polls is not a polling cycle")
    port = API_PORT_TLS if use_tls else API_PORT_PLAIN

    steps = (
        PollStep(1, "connect",
                 f"Open a TCP socket to the router on port {port}", True),
        PollStep(2, "authenticate",
                 "Send the login sentence and read the reply", True),
        PollStep(3, "request",
                 "Send /ip/hotspot/active/print as a length encoded sentence",
                 True),
        PollStep(4, "read",
                 "Read reply sentences until the done sentence arrives", True),
        PollStep(5, "process",
                 "Parse each active session into a record and evaluate the "
                 "heuristics against it", False),
        PollStep(6, "close",
                 "Close the socket, holding nothing for the next cycle", False),
        PollStep(7, "sleep",
                 f"Wait {interval} seconds with no connection open at all",
                 False),
    )

    log = tuple(
        f"[t+{index * 2:02d}s] {step.phase.upper():<12} {step.detail}"
        for index, step in enumerate(steps))

    findings: list[Finding] = []

    if not use_tls:
        findings.append(Finding(
            code="API-PLAINTEXT", severity=SEVERITY_CRITICAL,
            title=f"Port {API_PORT_PLAIN} carries the credentials in the clear",
            detail=("The plain API port sends the login without transport "
                    "encryption, so anyone on the path to the router reads "
                    "the credentials that administer it."),
            fix=(f"Use port {API_PORT_TLS} with a certificate, and close "
                 f"{API_PORT_PLAIN} on the router rather than merely not "
                 f"using it.")))
    else:
        findings.append(Finding(
            code="API-TLS", severity=SEVERITY_OK,
            title=f"Port {API_PORT_TLS} keeps the credentials off the wire",
            detail="The API runs inside TLS, so the login is not readable in transit.",
            fix=("Restrict the API service to the management address as "
                 "well. Encryption does not limit who may try.")))

    findings.append(Finding(
        code="API-STATELESS", severity=SEVERITY_OK,
        title="Nothing is held between cycles, on purpose",
        detail=("A held connection survives a router reboot, an interface "
                "flap and a firewall idle timeout as an object that looks "
                "alive and reads nothing. The failure is silent and the "
                "dashboard keeps showing the last good data."),
        fix=("Keep it stateless. The cost is a handshake a minute and the "
             "benefit is that a failure is loud and immediate.")))

    findings.append(Finding(
        code="API-INTERVAL", severity=SEVERITY_WARN,
        title=f"A {interval} second gap is the detection latency",
        detail=("Anything that starts and finishes inside one gap is never "
                "seen. Shortening it costs router load rather than accuracy "
                "up to a point, and past that point it costs both."),
        fix=("Pick the interval from how long a session has to run before "
             "acting on it matters, rather than from how fast the router can "
             "answer.")))

    findings.append(Finding(
        code="API-READONLY", severity=SEVERITY_WARN,
        title="The polling account should not be able to change anything",
        detail=("Reading the active session list needs read permission. An "
                "account that can also write is an account that can take the "
                "hotspot down if the poller misbehaves."),
        fix=("Give the poller a read only group and put the blocking action "
             "behind a separate credential used by a person.")))

    return PollCycle(
        interval_seconds=interval, port=port, steps=steps, log=log,
        sockets_left_open=0, stateless=True,
        rationale=(
            "Build the connection, use it, tear it down, wait. A cycle that "
            "holds nothing cannot silently go stale, so a failure is this "
            "minute's failure rather than a dashboard that has been wrong "
            "since Tuesday."),
        findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# 3. Voucher status
# ---------------------------------------------------------------------------

STATUS_ACTIVE = "Active"
STATUS_BLOCKED = "Blocked"
STATUSES: tuple[str, ...] = (STATUS_ACTIVE, STATUS_BLOCKED)

ACTION_BLOCK = "block"
ACTION_UNBLOCK = "unblock"
ACTIONS: tuple[str, ...] = (ACTION_BLOCK, ACTION_UNBLOCK)

VOUCHER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")

OUTCOME_CHANGED = "CHANGED"
OUTCOME_ALREADY = "ALREADY IN THAT STATE"
OUTCOME_REJECTED = "REJECTED"

# The action applied to a voucher, whatever it was in before. Both actions
# are idempotent by construction: the result depends on the action and not on
# how many times it has been sent, which is what makes a retrying poller safe.
RESULT_OF_ACTION = {
    ACTION_BLOCK: STATUS_BLOCKED,
    ACTION_UNBLOCK: STATUS_ACTIVE,
}


@dataclass(frozen=True)
class VoucherResult:
    voucher_id: str
    action: str
    previous_status: str
    status: str
    outcome: str
    changed: bool
    audit_line: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def manage_voucher_status(voucher_id: str, action: str,
                          current_status: str = STATUS_ACTIVE) -> VoucherResult:
    """Apply a block or an unblock, and be safe to send twice.

    The result depends on the action rather than on how many times it has
    arrived, so a poller that retries after a timeout cannot flip a voucher
    back and forth. Sending the same action again reports that nothing
    changed rather than raising, because a retry is normal traffic and not
    an error.
    """
    voucher = str(voucher_id or "").strip()
    verb = str(action or "").strip().lower()
    previous = str(current_status or "").strip()

    findings: list[Finding] = []

    if not VOUCHER_PATTERN.match(voucher):
        findings.append(Finding(
            code="VCH-ID", severity=SEVERITY_CRITICAL,
            title=f"{voucher or 'The voucher id'} is not a usable voucher id",
            detail=("An id that is empty or oddly shaped means the action "
                    "either hits nothing or hits the wrong subscriber."),
            fix="Take the id from the session record rather than from a message."))
        return VoucherResult(
            voucher_id=voucher, action=verb, previous_status=previous,
            status=previous if previous in STATUSES else STATUS_ACTIVE,
            outcome=OUTCOME_REJECTED, changed=False, audit_line="",
            findings=tuple(findings))

    if verb not in ACTIONS:
        findings.append(Finding(
            code="VCH-ACTION", severity=SEVERITY_CRITICAL,
            title=f"{action!r} is not an action this interface performs",
            detail=(f"The only actions are {' and '.join(ACTIONS)}. Anything "
                    f"else is a caller bug, and guessing which was meant is "
                    f"how a voucher gets blocked by a typo."),
            fix="Fix the caller rather than widening the interface."))
        return VoucherResult(
            voucher_id=voucher, action=verb, previous_status=previous,
            status=previous if previous in STATUSES else STATUS_ACTIVE,
            outcome=OUTCOME_REJECTED, changed=False, audit_line="",
            findings=tuple(findings))

    if previous not in STATUSES:
        findings.append(Finding(
            code="VCH-STATE", severity=SEVERITY_CRITICAL,
            title=f"{previous or 'The current status'} is not a known status",
            detail=(f"A voucher is {' or '.join(STATUSES)}. An unknown value "
                    f"means the record was read from somewhere that is not "
                    f"the source of truth."),
            fix="Read the status from the router before acting on it."))
        return VoucherResult(
            voucher_id=voucher, action=verb, previous_status=previous,
            status=STATUS_ACTIVE, outcome=OUTCOME_REJECTED, changed=False,
            audit_line="", findings=tuple(findings))

    new_status = RESULT_OF_ACTION[verb]
    changed = new_status != previous
    outcome = OUTCOME_CHANGED if changed else OUTCOME_ALREADY

    if changed and verb == ACTION_BLOCK:
        findings.append(Finding(
            code="VCH-BLOCKED", severity=SEVERITY_CRITICAL,
            title=f"{voucher} is now blocked and a person has been cut off",
            detail=("Whoever was using this voucher lost their connection at "
                    "this moment, wherever they were and whatever they were "
                    "doing on it."),
            fix=("Make sure the redirect page says why and how to appeal. A "
                 "silent block produces a complaint about the network rather "
                 "than a conversation about the policy.")))
    elif changed:
        findings.append(Finding(
            code="VCH-RESTORED", severity=SEVERITY_OK,
            title=f"{voucher} is active again",
            detail="Service is restored from this moment.",
            fix=("Record who restored it and why. An unblock with no reason "
                 "attached is the one the next audit asks about.")))
    else:
        findings.append(Finding(
            code="VCH-IDEMPOTENT", severity=SEVERITY_OK,
            title=f"{voucher} was already {previous.lower()}",
            detail=("Nothing changed. The same action arriving twice is a "
                    "retry rather than an error, and treating it as an error "
                    "is how a retrying poller starts raising alerts."),
            fix="Keep both actions idempotent. A retry must never flip a state."))

    findings.append(Finding(
        code="VCH-DESK", severity=SEVERITY_WARN,
        title="The desk has to be able to reverse this without a ticket",
        detail=("Every block made by a heuristic will sometimes be wrong. If "
                "reversing it needs an engineer, the customer waits, and the "
                "cost of the false positive multiplies."),
        fix=("Give the front desk the unblock action and nothing else. It is "
             "the only action that can be safely handed out, because its "
             "worst case is a customer getting service back.")))

    return VoucherResult(
        voucher_id=voucher, action=verb, previous_status=previous,
        status=new_status, outcome=outcome, changed=changed,
        audit_line=(f"voucher={voucher} action={verb} from={previous} "
                    f"to={new_status} changed={str(changed).lower()}"),
        findings=tuple(findings),
    )


SAMPLE_SUBSCRIBERS: tuple[tuple[str, str, int, int, int], ...] = (
    ("V-1041", "A4:83:E7:11:22:33", 64, 1, 40),
    ("V-1042", "DA:11:9C:55:66:77", 63, 2, 210),
    ("V-1043", "B8:27:EB:AA:BB:CC", 127, 1, 90),
    ("V-1044", "3C:22:FB:01:02:03", 64, 2, 220),
    ("V-1045", "F0:18:98:99:88:77", 64, 1, 195),
)
