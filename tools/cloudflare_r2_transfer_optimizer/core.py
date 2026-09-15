"""Cloudflare R2 transfer optimisation engine.

Three questions decide whether a large upload to R2 works or times out: how
much bandwidth the deadline actually demands, whether splitting the file into
parts will help or merely add overhead, and where the time goes on a request
that has not sent a byte of payload yet.

Pure logic, no Streamlit import, so this is unit testable on its own.
Deterministic: nothing reads the clock or a random source.

THE HONEST PART OF THE MULTIPART MODEL

A parallel upload does not create bandwidth. The reason chunking helps is
narrower and more interesting: a single TCP stream cannot fill a fast, distant
link, because it can only have one congestion window in flight per round trip.
That ceiling is the bandwidth delay product, and it does not care how fat the
pipe is. Several streams fill the pipe that one stream leaves mostly empty.

So the speedup here is capped by the link, not multiplied by the chunk count. A
model that returned chunk_count times faster would be a toy, and it would tell
somebody on a short fast link to split a file for no reason while charging them
the per part overhead.

R2 CONSTRAINTS ENCODED HERE, VERIFIED AGAINST CLOUDFLARE'S DOCUMENTATION

  Minimum part size is 5 MiB, for every part except the last.
  Maximum part size is 5 GiB, and the maximum object is 5 TiB.
  A multipart upload may have at most 10,000 parts.
  Every part except the last must be the SAME size. This is stricter than S3
  and is the constraint most often met as a mysterious 400 on part two.
  The last part may be smaller, but never larger than the others.
  A presigned URL may live from one second to seven days, 604800 seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

MIB = 1024 * 1024

# Verified R2 limits.
MIN_PART_MIB = 5
MAX_PART_GIB = 5
MAX_PARTS = 10_000
MAX_OBJECT_TIB = 5
MAX_PRESIGN_SECONDS = 604_800          # seven days
MIN_PRESIGN_SECONDS = 1


# ---------------------------------------------------------------------------
# Network tiers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NetworkTier:
    name: str
    megabits: float
    rtt_ms: int
    note: str


# Ordered slowest to fastest, so a feasibility check walks up and stops at the
# first tier that clears the requirement.
NETWORK_TIERS: tuple = (
    NetworkTier("Mobile 4G", 25.0, 55, "A phone on a good day."),
    NetworkTier("Home broadband", 100.0, 20, "A typical domestic upload link."),
    NetworkTier("Business fibre", 500.0, 12, "A small office symmetric line."),
    NetworkTier("Datacentre 1 Gb", 1000.0, 8, "A single server uplink."),
    NetworkTier("Datacentre 10 Gb", 10000.0, 4, "A rack uplink."),
    # Long haul links are the whole reason multipart exists. The bandwidth is
    # there and one stream cannot reach it, because a single congestion window
    # per round trip is a hard ceiling that does not care how fat the pipe is.
    NetworkTier("Long haul 1 Gb", 1000.0, 180,
                "A gigabit uplink to an edge on another continent."),
    NetworkTier("Long haul 10 Gb", 10000.0, 240,
                "A fast uplink at intercontinental distance."),
)

TIER_BY_NAME = {tier.name: tier for tier in NETWORK_TIERS}

# Real links never deliver their rated speed. TCP, TLS and HTTP framing, plus
# the retransmits any real path produces, take a slice off the top. Modelling
# at line rate is how a plan that looked fine on paper misses its window.
PROTOCOL_EFFICIENCY = 0.92

FEASIBLE = "Feasible"
TIGHT = "Tight"
INFEASIBLE = "Not achievable"


@dataclass(frozen=True)
class TransferMath:
    file_size_mb: float
    target_seconds: float
    required_mbps: float
    required_with_overhead_mbps: float
    status: str
    minimum_tier: str
    headroom_mbps: float
    note: str

    @property
    def feasible(self) -> bool:
        return self.status != INFEASIBLE

    @property
    def tone(self) -> str:
        return {FEASIBLE: "ok", TIGHT: "warn"}.get(self.status, "crit")

    def rows(self) -> list:
        return [
            {"Measure": "File size", "Value": f"{self.file_size_mb:,.0f} MB"},
            {"Measure": "Deadline", "Value": f"{self.target_seconds:,.0f} s"},
            {"Measure": "Payload bitrate",
             "Value": f"{self.required_mbps:,.2f} Mbps"},
            {"Measure": "With protocol overhead",
             "Value": f"{self.required_with_overhead_mbps:,.2f} Mbps"},
            {"Measure": "Smallest tier that clears it",
             "Value": self.minimum_tier},
            {"Measure": "Headroom on that tier",
             "Value": f"{self.headroom_mbps:,.2f} Mbps"},
        ]


def calculate_transfer_math(file_size_mb: float = 2048.0,
                            target_seconds: float = 120.0) -> TransferMath:
    """How much bandwidth a deadline demands, and which tier can supply it.

    A megabyte is eight megabits, and getting that factor of eight wrong by
    reading MB as Mb is the single most common error in a transfer estimate. It
    is off by eight hundred percent and it always looks achievable.
    """
    size = max(float(file_size_mb), 0.0)
    seconds = float(target_seconds)
    if seconds <= 0:
        return TransferMath(
            size, 0.0, float("inf"), float("inf"), INFEASIBLE, "none", 0.0,
            "A deadline of zero seconds demands infinite bandwidth. No tier "
            "clears it, and the honest answer is that the deadline is wrong "
            "rather than the network.")

    required = (size * 8.0) / seconds
    with_overhead = required / PROTOCOL_EFFICIENCY

    clearing = [t for t in NETWORK_TIERS if t.megabits >= with_overhead]
    if not clearing:
        fastest = NETWORK_TIERS[-1]
        return TransferMath(
            size, seconds, round(required, 3), round(with_overhead, 3),
            INFEASIBLE, "none",
            round(fastest.megabits - with_overhead, 3),
            f"{with_overhead:,.0f} Mbps is beyond even a "
            f"{fastest.name} link at {fastest.megabits:,.0f} Mbps. Either the "
            f"deadline moves or the payload shrinks.")

    tier = clearing[0]
    headroom = tier.megabits - with_overhead
    # Under a fifth of headroom is not a plan, it is a hope. Any retransmit,
    # any other traffic on the link, and the window is missed.
    tight = headroom < tier.megabits * 0.2
    return TransferMath(
        size, seconds, round(required, 3), round(with_overhead, 3),
        TIGHT if tight else FEASIBLE, tier.name, round(headroom, 3),
        (f"{tier.name} clears it with only {headroom:,.0f} Mbps to spare, "
         f"which one retransmit or one noisy neighbour will take."
         if tight else
         f"{tier.name} carries it with {headroom:,.0f} Mbps to spare."))


def tier_table(file_size_mb: float, target_seconds: float) -> list:
    """Every tier against this deadline, so the gap is a number."""
    math = calculate_transfer_math(file_size_mb, target_seconds)
    rows = []
    for tier in NETWORK_TIERS:
        effective = tier.megabits * PROTOCOL_EFFICIENCY
        seconds = (file_size_mb * 8.0) / effective if effective else 0.0
        rows.append({
            "Tier": tier.name,
            "Rated": f"{tier.megabits:,.0f} Mbps",
            "Round trip": f"{tier.rtt_ms} ms",
            "Time for this file": f"{seconds:,.1f} s",
            "Meets deadline": "yes" if seconds <= math.target_seconds else "no",
        })
    return rows


# ---------------------------------------------------------------------------
# Multipart upload
# ---------------------------------------------------------------------------

PROTOCOL_HTTP2 = "HTTP/2"
PROTOCOL_HTTP11 = "HTTP/1.1"
PROTOCOLS: tuple = (PROTOCOL_HTTP2, PROTOCOL_HTTP11)

# Cost of opening and finishing one part: the request, the response, and the
# share of the completion call. HTTP/2 multiplexes over one connection, so it
# pays this once per part rather than once per connection.
PART_OVERHEAD_MS = {PROTOCOL_HTTP2: 35, PROTOCOL_HTTP11: 110}

# A single TCP stream can have one congestion window in flight per round trip.
# This is the practical ceiling that makes parallelism worth anything, and it
# is why a fast distant link feels slow with one stream.
TCP_WINDOW_BYTES = 4 * MIB


@dataclass
class MultipartPlan:
    file_size_mb: float
    chunk_count: int
    protocol: str
    tier: str
    part_size_mib: float = 0.0
    single_stream_seconds: float = 0.0
    parallel_seconds: float = 0.0
    overhead_seconds: float = 0.0
    violations: list = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.violations

    @property
    def speedup(self) -> float:
        if self.parallel_seconds <= 0:
            return 0.0
        return round(self.single_stream_seconds / self.parallel_seconds, 3)

    @property
    def worth_it(self) -> bool:
        """Under a tenth faster is not worth the parts and the complexity."""
        return self.valid and self.speedup >= 1.1

    @property
    def tone(self) -> str:
        if not self.valid:
            return "crit"
        return "ok" if self.worth_it else "warn"

    def rows(self) -> list:
        return [
            {"Measure": "Parts", "Value": str(self.chunk_count)},
            {"Measure": "Part size", "Value": f"{self.part_size_mib:,.2f} MiB"},
            {"Measure": "Single stream",
             "Value": f"{self.single_stream_seconds:,.2f} s"},
            {"Measure": "Parallel parts",
             "Value": f"{self.parallel_seconds:,.2f} s"},
            {"Measure": "Per part overhead",
             "Value": f"{self.overhead_seconds:,.2f} s"},
            {"Measure": "Speedup", "Value": f"{self.speedup:,.2f}x"},
        ]


def single_stream_mbps(tier: NetworkTier) -> float:
    """What one TCP stream can actually pull on this link.

    Bounded by the bandwidth delay product: one congestion window per round
    trip. On a short link this is above the line rate and the link wins; on a
    long one it is far below, which is the whole reason to open more streams.
    """
    per_second = (TCP_WINDOW_BYTES * 8.0) / (tier.rtt_ms / 1000.0)
    return min(tier.megabits * PROTOCOL_EFFICIENCY, per_second / 1_000_000.0)


def validate_parts(file_size_mb: float, chunk_count: int) -> list:
    """Every R2 rule this plan breaks, named the way the API reports it."""
    problems: list = []
    size_mib = (file_size_mb * 1_000_000) / MIB
    if chunk_count < 1:
        problems.append("A multipart upload needs at least one part.")
        return problems
    if chunk_count > MAX_PARTS:
        problems.append(f"{chunk_count:,} parts exceeds R2's limit of "
                        f"{MAX_PARTS:,}.")
    part_mib = size_mib / chunk_count
    if chunk_count > 1 and part_mib < MIN_PART_MIB:
        problems.append(
            f"A part of {part_mib:,.2f} MiB is under R2's {MIN_PART_MIB} MiB "
            f"minimum. Only the final part may be smaller, so this fails on "
            f"part two rather than at the start.")
    if part_mib > MAX_PART_GIB * 1024:
        problems.append(f"A part of {part_mib / 1024:,.2f} GiB exceeds R2's "
                        f"{MAX_PART_GIB} GiB maximum part size.")
    if size_mib / (1024 * 1024) > MAX_OBJECT_TIB * 1024:
        problems.append(f"The object exceeds R2's {MAX_OBJECT_TIB} TiB limit.")
    return problems


def simulate_multipart_upload(file_size_mb: float = 2048.0,
                              chunk_count: int = 8,
                              protocol: str = PROTOCOL_HTTP2,
                              tier_name: str = "Business fibre") -> MultipartPlan:
    """Compare one stream against parallel parts, without inventing bandwidth.

    The parallel time is bounded by the link, so the speedup tops out at the
    ratio between the link and what one stream could manage. Beyond that,
    adding parts only adds overhead, and the plan says so rather than
    reporting a larger number.
    """
    protocol = protocol if protocol in PROTOCOLS else PROTOCOL_HTTP2
    tier = TIER_BY_NAME.get(tier_name, NETWORK_TIERS[2])
    size = max(float(file_size_mb), 0.0)
    count = int(chunk_count)

    plan = MultipartPlan(size, count, protocol, tier.name)
    plan.violations = validate_parts(size, count)
    if plan.violations or size <= 0:
        return plan

    plan.part_size_mib = round((size * 1_000_000) / MIB / count, 4)
    megabits = size * 8.0

    one_stream = single_stream_mbps(tier)
    plan.single_stream_seconds = round(megabits / one_stream, 4)

    # Many streams still cannot exceed the link. This min is the honest bit.
    parallel_mbps = min(one_stream * count, tier.megabits * PROTOCOL_EFFICIENCY)
    overhead = (count * PART_OVERHEAD_MS[protocol]) / 1000.0
    plan.overhead_seconds = round(overhead, 4)
    plan.parallel_seconds = round(megabits / parallel_mbps + overhead, 4)
    return plan


def multipart_sweep(file_size_mb: float, protocol: str,
                    tier_name: str) -> list:
    """Where more parts stop helping, which is the number worth knowing."""
    rows = []
    for count in (1, 2, 4, 8, 16, 32, 64):
        plan = simulate_multipart_upload(file_size_mb, count, protocol,
                                         tier_name)
        rows.append({
            "Parts": str(count),
            "Part size": (f"{plan.part_size_mib:,.1f} MiB" if plan.valid
                          else "invalid"),
            "Duration": (f"{plan.parallel_seconds:,.2f} s" if plan.valid
                         else "n/a"),
            "Speedup": f"{plan.speedup:,.2f}x" if plan.valid else "n/a",
            "Valid on R2": "yes" if plan.valid else "no",
        })
    return rows


# ---------------------------------------------------------------------------
# Edge latency
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EdgeLatency:
    location: str
    dns_ms: float
    tls_ms: float
    ttfb_ms: float
    payload_ms: float

    @property
    def total_ms(self) -> float:
        return round(self.dns_ms + self.tls_ms + self.ttfb_ms
                     + self.payload_ms, 2)

    @property
    def before_payload_ms(self) -> float:
        """Everything spent before one byte of the object has moved."""
        return round(self.dns_ms + self.tls_ms + self.ttfb_ms, 2)

    @property
    def overhead_share(self) -> float:
        return round(self.before_payload_ms / self.total_ms, 4) if self.total_ms else 0.0


def get_latency_breakdown() -> list:
    """Where the time goes on a small object request, by edge location.

    Split out on purpose. On a small object most of the wall clock is spent
    before any payload moves, so tuning the transfer is tuning the wrong
    thing: the win is a warm connection, not a faster pipe.
    """
    return [
        EdgeLatency("London", 8.4, 21.6, 32.1, 41.0),
        EdgeLatency("Frankfurt", 9.1, 24.8, 36.4, 43.2),
        EdgeLatency("Ashburn", 11.7, 42.3, 58.9, 52.6),
        EdgeLatency("Singapore", 14.2, 68.5, 94.7, 71.3),
        EdgeLatency("Sao Paulo", 16.9, 81.2, 112.4, 88.5),
        EdgeLatency("Johannesburg", 19.4, 96.7, 131.8, 102.9),
    ]


def latency_rows() -> list:
    return [
        {
            "Edge": edge.location,
            "DNS": f"{edge.dns_ms:,.1f} ms",
            "TLS handshake": f"{edge.tls_ms:,.1f} ms",
            "TTFB": f"{edge.ttfb_ms:,.1f} ms",
            "Payload": f"{edge.payload_ms:,.1f} ms",
            "Total": f"{edge.total_ms:,.1f} ms",
            "Before payload": f"{edge.overhead_share:.0%}",
        }
        for edge in get_latency_breakdown()
    ]


def presign_validate(seconds: int) -> tuple:
    """Whether a presigned URL lifetime is one R2 will actually issue."""
    if seconds < MIN_PRESIGN_SECONDS:
        return False, (f"A presigned URL must live at least "
                       f"{MIN_PRESIGN_SECONDS} second.")
    if seconds > MAX_PRESIGN_SECONDS:
        return False, (f"{seconds:,} seconds exceeds R2's maximum of "
                       f"{MAX_PRESIGN_SECONDS:,}, which is seven days.")
    return True, f"Valid. The URL expires after {seconds:,} seconds."


def r2_limits() -> list:
    """The constraints, stated once so the page and the tests share them."""
    return [
        {"Limit": "Minimum part size",
         "Value": f"{MIN_PART_MIB} MiB, except the final part"},
        {"Limit": "Maximum part size", "Value": f"{MAX_PART_GIB} GiB"},
        {"Limit": "Maximum parts", "Value": f"{MAX_PARTS:,}"},
        {"Limit": "Maximum object", "Value": f"{MAX_OBJECT_TIB} TiB"},
        {"Limit": "Equal part sizes",
         "Value": "Required. Every part but the last must match"},
        {"Limit": "Presigned URL lifetime",
         "Value": f"1 second to {MAX_PRESIGN_SECONDS:,} seconds, seven days"},
    ]
