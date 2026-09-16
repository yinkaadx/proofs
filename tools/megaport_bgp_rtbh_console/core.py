"""Megaport BGP remotely triggered black hole engine.

This generates configuration that gets pasted into a production router during
an incident, by somebody who is already having a bad morning. So the validator
is strict, it refuses rather than warns, and it uses the standard library's
ipaddress module rather than matching strings, because a prefix check written
with split and startswith is how a /24 gets blackholed instead of a /32.

Pure logic, no Streamlit import, so this is unit testable on its own.
Deterministic: nothing reads the clock or a random source.

THE TWO THINGS THAT MAKE RTBH FAIL SILENTLY

The first is send-community. A route map can set the blackhole community
perfectly and BGP will strip it on the way out unless the neighbour is
configured to send communities. The announcement leaves, the upstream sees an
ordinary prefix, nothing is dropped, and every show command on your side looks
correct. There is no error anywhere.

The second is where the drop happens. Discarding the traffic at your own
border does nothing for a saturated circuit: the packets have already crossed
the link to reach the router that discards them. RTBH works because the
UPSTREAM drops the traffic at their edge, before it is ever put on your access
port. A console that shows a blackhole freeing your circuit without saying
which side is dropping is teaching the wrong lesson, so this one models both.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

# The well known BLACKHOLE community from RFC 7999. Every compliant upstream
# understands this one, which is why it is sent alongside the provider's.
WELL_KNOWN_BLACKHOLE = "65535:666"

# The provider specific community. Held as a constant rather than written into
# the template so it can be changed in one place, and stated as something to
# confirm against the provider's own documentation rather than as a fact this
# tool can vouch for: tagging the wrong community is a silent no operation.
MEGAPORT_BLACKHOLE = "49915:666"

COMMUNITY_NOTE = (
    "Confirm the provider community against your provider's current "
    "documentation or your service order before an incident. A wrong "
    "community is not an error, it is a route that is accepted and not "
    "acted on, which looks identical to success on your side."
)

# IPv4 and IPv6 host prefix lengths. Anything less specific blackholes
# neighbours of the victim as well, which turns one outage into a subnet.
HOST_LEN = {4: 32, 6: 128}

DEFAULT_VICTIM = "203.0.113.45"
DEFAULT_PARENT = "203.0.113.0/24"
DEFAULT_NEIGHBOR = "198.51.100.1"


# ---------------------------------------------------------------------------
# Part one: prefix validation
# ---------------------------------------------------------------------------

OK = "Valid"
REJECTED = "Rejected"


@dataclass(frozen=True)
class PrefixCheck:
    name: str
    passed: bool
    detail: str

    @property
    def tone(self) -> str:
        return "ok" if self.passed else "crit"


@dataclass
class PrefixValidation:
    target: str
    parent: str
    checks: list = field(default_factory=list)
    version: int = 0
    host_length: int = 0
    normalised_target: str = ""
    normalised_parent: str = ""

    @property
    def valid(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def status(self) -> str:
        return OK if self.valid else REJECTED

    @property
    def tone(self) -> str:
        return "ok" if self.valid else "crit"

    @property
    def failures(self) -> list:
        return [c for c in self.checks if not c.passed]

    def rows(self) -> list:
        return [
            {"Check": c.name, "Result": "pass" if c.passed else "FAIL",
             "Detail": c.detail}
            for c in self.checks
        ]


def validate_blackhole_prefix(target_cidr: str = "203.0.113.45/32",
                              parent_cidr: str = DEFAULT_PARENT
                              ) -> PrefixValidation:
    """Refuse anything that is not a single host inside a prefix you originate.

    Every check is a refusal rather than a warning. A warning on a blackhole
    generator is a warning somebody dismisses at three in the morning, and the
    cost of dismissing it is dropping the traffic for a whole subnet instead
    of for one host.
    """
    result = PrefixValidation(str(target_cidr), str(parent_cidr))

    try:
        target = ipaddress.ip_network(str(target_cidr).strip(), strict=True)
    except ValueError as exc:
        result.checks.append(PrefixCheck(
            "Target parses as a prefix", False,
            f"{target_cidr!r} is not a valid network: {exc}. A host address "
            f"with bits set below the mask is rejected rather than silently "
            f"rounded down to the network address."))
        return result
    result.checks.append(PrefixCheck(
        "Target parses as a prefix", True,
        f"{target} parsed as IPv{target.version}."))

    try:
        parent = ipaddress.ip_network(str(parent_cidr).strip(), strict=True)
    except ValueError as exc:
        result.checks.append(PrefixCheck(
            "Parent parses as a prefix", False,
            f"{parent_cidr!r} is not a valid network: {exc}"))
        return result
    result.checks.append(PrefixCheck(
        "Parent parses as a prefix", True, f"{parent} parsed."))

    result.version = target.version
    result.host_length = HOST_LEN[target.version]
    result.normalised_target = str(target)
    result.normalised_parent = str(parent)

    same_family = target.version == parent.version
    result.checks.append(PrefixCheck(
        "Address families match", same_family,
        f"target is IPv{target.version} and parent is IPv{parent.version}."
        + ("" if same_family else " A v4 host cannot sit inside a v6 prefix.")))
    if not same_family:
        return result

    host_len = HOST_LEN[target.version]
    is_host = target.prefixlen == host_len
    result.checks.append(PrefixCheck(
        f"Target is a single host, /{host_len}", is_host,
        f"/{target.prefixlen} announced."
        + ("" if is_host else
           f" A /{target.prefixlen} covers {target.num_addresses:,} addresses, "
           f"so this would discard traffic for every one of them and not only "
           f"for the victim.")))

    contained = target.subnet_of(parent)
    result.checks.append(PrefixCheck(
        "Target sits inside the parent prefix", contained,
        f"{target} inside {parent}."
        + ("" if contained else
           f" {target} is outside {parent}. An upstream filters on the space "
           f"you are authorised to originate, so this is either rejected or "
           f"is an announcement of somebody else's address.")))

    more_specific = parent.prefixlen < target.prefixlen
    result.checks.append(PrefixCheck(
        "Target is more specific than the parent", more_specific,
        f"/{target.prefixlen} against /{parent.prefixlen}."
        + ("" if more_specific else
           " The blackhole must be more specific than the covering route or "
           "it does not win the best path selection.")))

    routable = not (target.is_multicast or target.is_loopback
                    or target.is_unspecified)
    result.checks.append(PrefixCheck(
        "Target is a routable unicast address", routable,
        f"{target.network_address} is unicast."
        if routable else
        f"{target.network_address} is multicast, loopback or unspecified, and "
        f"is not something a blackhole route applies to."))

    return result


def normalise_host(victim_ip: str, version_hint: int = 0) -> str:
    """Turn a bare address into the host prefix an announcement needs.

    Accepts an address or an already formed host prefix, so a person pasting
    either into the field gets the same result rather than a syntax error.
    """
    text = str(victim_ip).strip()
    if "/" in text:
        network = ipaddress.ip_network(text, strict=True)
        if network.prefixlen != HOST_LEN[network.version]:
            raise ValueError(
                f"{text} is a /{network.prefixlen}. A blackhole announcement "
                f"must be a /{HOST_LEN[network.version]} host route.")
        return str(network)
    address = ipaddress.ip_address(text)
    return f"{address}/{HOST_LEN[address.version]}"


# ---------------------------------------------------------------------------
# Part two: the Cisco configuration
# ---------------------------------------------------------------------------

@dataclass
class RtbhConfig:
    victim: str
    parent: str
    neighbor: str
    provider_community: str
    well_known_community: str
    prefix_list_name: str
    route_map_name: str
    lines: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def config(self) -> str:
        return "\n".join(self.lines)

    @property
    def removal(self) -> str:
        """The lines that take the blackhole off again.

        Generated with the configuration rather than afterwards, because the
        rollback for a change that drops traffic has to exist before the
        change does.
        """
        return "\n".join([
            "! Remove the blackhole and return the host to service.",
            "configure terminal",
            f" no ip prefix-list {self.prefix_list_name} seq 5 permit "
            f"{self.victim}",
            f" clear ip bgp {self.neighbor} soft out",
            "end",
            "! Confirm the host is reachable again before closing the ticket.",
        ])


def generate_cisco_rtbh_config(victim_ip: str = DEFAULT_VICTIM,
                               parent_prefix: str = DEFAULT_PARENT,
                               neighbor_ip: str = DEFAULT_NEIGHBOR,
                               provider_community: str = MEGAPORT_BLACKHOLE,
                               local_asn: int = 65001) -> RtbhConfig:
    """Build the IOS and IOS XE lines for one blackhole announcement.

    The victim is validated before a single line is emitted. A generator that
    hands back a config for a /24 and leaves the checking to the operator has
    moved the failure from the tool to the router, which is the wrong place
    for it.
    """
    victim = normalise_host(victim_ip)
    validation = validate_blackhole_prefix(victim, parent_prefix)
    if not validation.valid:
        raise ValueError(
            "refusing to generate a configuration: "
            + " ".join(c.detail for c in validation.failures))

    family = ipaddress.ip_network(victim).version
    ip_kw = "ip" if family == 4 else "ipv6"
    prefix_list = "RTBH-BLACKHOLE-HOSTS"
    route_map = "RTBH-TAG-OUT"
    discard = "Null0"

    lines = [
        f"! Remotely triggered black hole for {victim}",
        f"! Parent prefix {parent_prefix}, upstream neighbour {neighbor_ip}",
        "!",
        "configure terminal",
        "!",
        "! 1. The host route to be blackholed. Only a host route is permitted,",
        "!    so a mistyped mask fails to match rather than widening the drop.",
        f" {ip_kw} prefix-list {prefix_list} seq 5 permit {victim}",
        "!",
        "! 2. A discard route so the prefix exists in the table and can be",
        "!    redistributed. Null0 is the discard interface.",
        f" {ip_kw} route {_static_route(victim, family)} {discard}",
        "!",
        "! 3. Tag the announcement with both communities. The well known one",
        f"!    is {WELL_KNOWN_BLACKHOLE} from RFC 7999 and the provider one is",
        f"!    {provider_community}. no-export stops the announcement",
        "!    travelling past the upstream that is meant to act on it.",
        f" route-map {route_map} permit 10",
        f"  match {ip_kw} address prefix-list {prefix_list}",
        f"  set community {provider_community} {WELL_KNOWN_BLACKHOLE} "
        f"no-export",
        f"  set origin igp",
        f" route-map {route_map} permit 20",
        "!",
        "! 4. Apply it, and send communities. Without send-community the",
        "!    communities above are stripped on the way out, the upstream",
        "!    sees an ordinary prefix, nothing is dropped, and every show",
        "!    command on this side still looks correct.",
        f" router bgp {local_asn}",
        f"  address-family {'ipv4' if family == 4 else 'ipv6'} unicast",
        f"   neighbor {neighbor_ip} send-community both",
        f"   neighbor {neighbor_ip} route-map {route_map} out",
        "  exit-address-family",
        "!",
        "end",
        "!",
        "! 5. Verify before you believe it.",
        f"show {ip_kw} bgp neighbors {neighbor_ip} advertised-routes | include "
        f"{victim.split('/')[0]}",
        f"show {ip_kw} bgp {victim} | include Community",
    ]

    warnings = [
        COMMUNITY_NOTE,
        ("This drops every packet to the host, including legitimate traffic. "
         "RTBH saves the circuit and the other services on it. It does not "
         "save the service under attack, which stays down either way."),
        ("The rollback is generated below with the configuration, because a "
         "change that drops traffic needs its removal written before it is "
         "applied rather than recalled afterwards."),
    ]

    return RtbhConfig(victim, parent_prefix, neighbor_ip, provider_community,
                      WELL_KNOWN_BLACKHOLE, prefix_list, route_map, lines,
                      warnings)


def _static_route(victim: str, family: int) -> str:
    """IOS wants a dotted mask for v4 and the prefix itself for v6."""
    network = ipaddress.ip_network(victim)
    if family == 4:
        return f"{network.network_address} {network.netmask}"
    return str(network)


# ---------------------------------------------------------------------------
# Part three: where the traffic is actually dropped
# ---------------------------------------------------------------------------

DROP_NONE = "No blackhole, attack traversing the circuit"
DROP_LOCAL = "Discarded at your border, circuit still saturated"
DROP_UPSTREAM = "Discarded by the upstream, circuit recovered"


@dataclass(frozen=True)
class EdgeDrop:
    circuit_gbps: float
    baseline_gbps: float
    attack_gbps: float
    is_blackholed: bool
    upstream_honours: bool
    offered_gbps: float
    delivered_gbps: float
    utilisation_before: float
    utilisation_after: float
    state: str
    note: str

    @property
    def saturated_before(self) -> bool:
        return self.utilisation_before >= 1.0

    @property
    def saturated_after(self) -> bool:
        return self.utilisation_after >= 1.0

    @property
    def recovered(self) -> bool:
        return self.saturated_before and not self.saturated_after

    @property
    def tone(self) -> str:
        if not self.saturated_after:
            return "ok"
        return "crit" if self.saturated_before else "warn"

    def rows(self) -> list:
        return [
            {"Measure": "Circuit capacity",
             "Value": f"{self.circuit_gbps:,.2f} Gbps"},
            {"Measure": "Legitimate traffic",
             "Value": f"{self.baseline_gbps:,.2f} Gbps"},
            {"Measure": "Attack traffic",
             "Value": f"{self.attack_gbps:,.2f} Gbps"},
            {"Measure": "Offered at the access port",
             "Value": f"{self.offered_gbps:,.2f} Gbps"},
            {"Measure": "Utilisation before",
             "Value": f"{self.utilisation_before:.1%}"},
            {"Measure": "Utilisation after",
             "Value": f"{self.utilisation_after:.1%}"},
            {"Measure": "Where the drop happens", "Value": self.state},
        ]


def simulate_megaport_edge_drop(is_blackholed: bool = True,
                                incoming_attack_gbps: float = 18.0,
                                circuit_gbps: float = 10.0,
                                baseline_gbps: float = 2.4,
                                upstream_honours: bool = True) -> EdgeDrop:
    """Model the circuit before and after, and say which side is dropping.

    This is the distinction the whole tool turns on. Discarding at your own
    border does nothing for a saturated circuit, because the packets crossed
    the link to reach the router that discards them. Only the upstream acting
    on the community keeps them off the access port in the first place.
    """
    capacity = max(float(circuit_gbps), 0.0001)
    baseline = max(float(baseline_gbps), 0.0)
    attack = max(float(incoming_attack_gbps), 0.0)

    offered = baseline + attack
    before = offered / capacity

    if not is_blackholed:
        delivered = offered
        state = DROP_NONE
        note = ("The attack is on the circuit and competing with real traffic "
                "for the same capacity. Nothing is being dropped before it "
                "arrives.")
    elif not upstream_honours:
        # The route is announced and tagged, but the upstream is not acting on
        # it. Your router discards to Null0 after the packets have already
        # crossed the access port, so the circuit is exactly as full.
        delivered = offered
        state = DROP_LOCAL
        note = ("The announcement is out and your router is discarding to "
                "Null0, but the packets crossed the circuit to reach it. "
                "Utilisation is unchanged, which is the state most easily "
                "mistaken for a working blackhole: the drop counters climb "
                "while the link stays full.")
    else:
        # The upstream drops the attack at their edge. Legitimate traffic to
        # the victim goes with it, which is the cost of the method.
        delivered = baseline
        state = DROP_UPSTREAM
        note = ("The upstream is acting on the community and discarding at "
                "their edge, so the attack never reaches the access port. The "
                "circuit is back for every other service, and the host under "
                "attack remains unreachable, which is the trade being made.")

    after = delivered / capacity
    return EdgeDrop(
        circuit_gbps=round(capacity, 4), baseline_gbps=round(baseline, 4),
        attack_gbps=round(attack, 4), is_blackholed=is_blackholed,
        upstream_honours=upstream_honours, offered_gbps=round(offered, 4),
        delivered_gbps=round(delivered, 4),
        utilisation_before=round(before, 6), utilisation_after=round(after, 6),
        state=state, note=note)


def drop_comparison(attack_gbps: float = 18.0, circuit_gbps: float = 10.0,
                    baseline_gbps: float = 2.4) -> list:
    """The three states side by side, which is the argument in one table."""
    scenarios = (
        ("No blackhole", False, True),
        ("Announced, upstream not acting", True, False),
        ("Announced, upstream acting", True, True),
    )
    rows = []
    for label, blackholed, honours in scenarios:
        run = simulate_megaport_edge_drop(blackholed, attack_gbps,
                                          circuit_gbps, baseline_gbps,
                                          honours)
        rows.append({
            "Scenario": label,
            "Offered": f"{run.offered_gbps:,.2f} Gbps",
            "On the circuit": f"{run.delivered_gbps:,.2f} Gbps",
            "Utilisation": f"{run.utilisation_after:.1%}",
            "Saturated": "yes" if run.saturated_after else "no",
        })
    return rows


def console_summary() -> dict:
    config = generate_cisco_rtbh_config()
    drop = simulate_megaport_edge_drop()
    return {
        "communities": 2,
        "provider_community": config.provider_community,
        "well_known_community": config.well_known_community,
        "config_lines": len(config.lines),
        "warnings": len(config.warnings),
        "utilisation_before": drop.utilisation_before,
        "utilisation_after": drop.utilisation_after,
        "recovered": drop.recovered,
    }
