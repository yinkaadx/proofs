"""Engine tests for the Cloudflare R2 Transfer Optimization Console.

Written for pytest. Deterministic: nothing in the engine reads the clock or a
random source.

The tests that matter most assert that the model refuses to invent bandwidth.
A multipart simulator that returns chunk_count times faster is a toy, and it
would tell somebody on a short fast link to split a file for no reason while
charging them the per part overhead.

Run: pytest tests/test_cloudflare_r2_transfer_optimizer.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.cloudflare_r2_transfer_optimizer.core import (  # noqa: E402
    ENGINE_VERSION,
    FEASIBLE,
    INFEASIBLE,
    MAX_OBJECT_TIB,
    MAX_PARTS,
    MAX_PART_GIB,
    MAX_PRESIGN_SECONDS,
    MIN_PART_MIB,
    MIN_PRESIGN_SECONDS,
    NETWORK_TIERS,
    PROTOCOL_EFFICIENCY,
    PROTOCOL_HTTP11,
    PROTOCOL_HTTP2,
    PROTOCOLS,
    TIER_BY_NAME,
    TIGHT,
    calculate_transfer_math,
    get_latency_breakdown,
    latency_rows,
    multipart_sweep,
    presign_validate,
    r2_limits,
    simulate_multipart_upload,
    single_stream_mbps,
    tier_table,
    validate_parts,
)

NEAR = "Business fibre"
FAR = "Long haul 1 Gb"


# ---------------------------------------------------------------------------
# Transfer math
# ---------------------------------------------------------------------------

def test_a_megabyte_is_eight_megabits():
    """The error that makes a plan look eight times easier than it is."""
    math = calculate_transfer_math(100.0, 8.0)
    assert math.required_mbps == pytest.approx(100.0)


def test_the_required_bitrate_is_size_times_eight_over_seconds():
    # The engine rounds its reported figures to three decimals on purpose, so
    # the tolerance is that rounding rather than float precision. A tighter
    # bound would be testing the rounding, not the arithmetic.
    for size, seconds in ((2048.0, 120.0), (500.0, 10.0), (1.0, 1.0)):
        math = calculate_transfer_math(size, seconds)
        assert math.required_mbps == pytest.approx(size * 8.0 / seconds,
                                                   abs=0.001)


def test_overhead_always_demands_more_than_the_payload_alone():
    math = calculate_transfer_math(2048.0, 120.0)
    assert math.required_with_overhead_mbps > math.required_mbps
    assert math.required_with_overhead_mbps == pytest.approx(
        math.required_mbps / PROTOCOL_EFFICIENCY, abs=0.001)


def test_it_picks_the_smallest_tier_that_clears_the_requirement():
    math = calculate_transfer_math(2048.0, 120.0)
    tier = TIER_BY_NAME[math.minimum_tier]
    assert tier.megabits >= math.required_with_overhead_mbps
    faster = [t for t in NETWORK_TIERS
              if t.megabits >= math.required_with_overhead_mbps]
    assert tier.megabits == min(t.megabits for t in faster)


def test_headroom_is_the_gap_to_the_chosen_tier():
    math = calculate_transfer_math(2048.0, 120.0)
    tier = TIER_BY_NAME[math.minimum_tier]
    assert math.headroom_mbps == pytest.approx(
        tier.megabits - math.required_with_overhead_mbps, abs=0.001)


def test_a_thin_margin_is_reported_as_tight_rather_than_feasible():
    """Under a fifth of headroom is a hope, not a plan."""
    tier = TIER_BY_NAME[NEAR]
    # Ask for 90 percent of what the tier can deliver.
    seconds = 100.0
    size = (tier.megabits * PROTOCOL_EFFICIENCY * 0.9 * seconds) / 8.0
    assert calculate_transfer_math(size, seconds).status == TIGHT


def test_a_comfortable_margin_is_reported_as_feasible():
    tier = TIER_BY_NAME[NEAR]
    seconds = 100.0
    size = (tier.megabits * PROTOCOL_EFFICIENCY * 0.3 * seconds) / 8.0
    assert calculate_transfer_math(size, seconds).status == FEASIBLE


def test_a_demand_beyond_every_tier_is_refused_not_rounded_down():
    math = calculate_transfer_math(5_000_000.0, 1.0)
    assert math.status == INFEASIBLE
    assert math.minimum_tier == "none"
    assert not math.feasible


def test_a_deadline_of_zero_says_the_deadline_is_wrong():
    """Rather than dividing by zero or quietly returning something."""
    math = calculate_transfer_math(100.0, 0.0)
    assert math.status == INFEASIBLE
    assert math.required_mbps == float("inf")
    assert "deadline is wrong" in math.note


def test_a_negative_deadline_is_refused_too():
    assert calculate_transfer_math(100.0, -5.0).status == INFEASIBLE


def test_a_larger_file_never_needs_less_bandwidth():
    previous = 0.0
    for size in (1.0, 10.0, 100.0, 1000.0, 10000.0):
        current = calculate_transfer_math(size, 60.0).required_mbps
        assert current > previous
        previous = current


def test_a_longer_deadline_never_needs_more_bandwidth():
    rates = [calculate_transfer_math(2048.0, s).required_mbps
             for s in (10.0, 60.0, 120.0, 600.0)]
    assert rates == sorted(rates, reverse=True)


def test_the_math_rows_and_tier_table_are_arrow_safe():
    rows = calculate_transfer_math(2048.0, 120.0).rows()
    rows += tier_table(2048.0, 120.0)
    for row in rows:
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# The single stream ceiling, which is why multipart exists
# ---------------------------------------------------------------------------

def test_one_stream_is_capped_by_the_link_on_a_short_hop():
    tier = TIER_BY_NAME[NEAR]
    assert single_stream_mbps(tier) == pytest.approx(
        tier.megabits * PROTOCOL_EFFICIENCY, abs=0.001)


def test_one_stream_is_capped_by_the_round_trip_on_a_long_hop():
    """A single congestion window per round trip, however fat the pipe."""
    tier = TIER_BY_NAME[FAR]
    assert single_stream_mbps(tier) < tier.megabits * PROTOCOL_EFFICIENCY


def test_a_higher_round_trip_lowers_what_one_stream_can_reach():
    near = single_stream_mbps(TIER_BY_NAME[FAR])
    further = single_stream_mbps(TIER_BY_NAME["Long haul 10 Gb"])
    assert further < near, "240 ms must be worse than 180 ms for one stream"


# ---------------------------------------------------------------------------
# Multipart
# ---------------------------------------------------------------------------

def test_parallel_parts_never_exceed_the_link():
    """The whole honesty of the model. Streams do not create bandwidth."""
    tier = TIER_BY_NAME[FAR]
    plan = simulate_multipart_upload(2048.0, 64, PROTOCOL_HTTP2, FAR)
    achieved = (2048.0 * 8.0) / (plan.parallel_seconds - plan.overhead_seconds)
    assert achieved <= tier.megabits * PROTOCOL_EFFICIENCY + 1e-6


def test_the_speedup_is_not_the_chunk_count():
    plan = simulate_multipart_upload(2048.0, 32, PROTOCOL_HTTP2, FAR)
    assert plan.speedup < 32
    assert plan.speedup > 1


def test_splitting_is_worth_it_at_distance():
    plan = simulate_multipart_upload(2048.0, 8, PROTOCOL_HTTP2, FAR)
    assert plan.worth_it
    assert plan.speedup > 3


def test_splitting_buys_nothing_on_a_short_fast_link():
    """And the tool says so rather than reporting a flattering number."""
    plan = simulate_multipart_upload(2048.0, 8, PROTOCOL_HTTP2, NEAR)
    assert not plan.worth_it
    assert plan.speedup <= 1.0


def test_too_many_parts_becomes_slower_not_faster():
    """Per part overhead eventually outgrows the benefit."""
    best = simulate_multipart_upload(2048.0, 8, PROTOCOL_HTTP2, FAR)
    excessive = simulate_multipart_upload(2048.0, 64, PROTOCOL_HTTP2, FAR)
    assert excessive.parallel_seconds > best.parallel_seconds


def test_one_part_is_the_single_stream_baseline():
    plan = simulate_multipart_upload(2048.0, 1, PROTOCOL_HTTP2, FAR)
    assert plan.speedup == pytest.approx(1.0, abs=0.02)


def test_http2_costs_less_per_part_than_http11():
    fast = simulate_multipart_upload(2048.0, 32, PROTOCOL_HTTP2, FAR)
    slow = simulate_multipart_upload(2048.0, 32, PROTOCOL_HTTP11, FAR)
    assert fast.overhead_seconds < slow.overhead_seconds
    assert fast.parallel_seconds < slow.parallel_seconds


def test_an_unknown_protocol_falls_back_rather_than_crashing():
    plan = simulate_multipart_upload(2048.0, 8, "QUIC", FAR)
    assert plan.protocol == PROTOCOL_HTTP2


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("count", [1, 2, 8, 32])
def test_every_valid_plan_reports_a_positive_duration(protocol, count):
    plan = simulate_multipart_upload(2048.0, count, protocol, FAR)
    assert plan.valid
    assert plan.parallel_seconds > 0
    assert plan.single_stream_seconds > 0
    for row in plan.rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# R2 constraints, verified against Cloudflare's documentation
# ---------------------------------------------------------------------------

def test_a_part_under_five_mebibytes_is_rejected():
    """Only the final part may be smaller, so this fails on part two."""
    problems = validate_parts(20.0, 8)
    assert problems
    assert f"{MIN_PART_MIB} MiB minimum" in problems[0]


def test_a_single_part_upload_is_allowed_to_be_small():
    assert validate_parts(1.0, 1) == []


def test_more_than_ten_thousand_parts_is_rejected():
    problems = validate_parts(5_000_000.0, MAX_PARTS + 1)
    assert any(f"{MAX_PARTS:,}" in p for p in problems)


def test_exactly_ten_thousand_parts_is_allowed():
    assert not any(f"{MAX_PARTS:,}" in p
                   for p in validate_parts(5_000_000.0, MAX_PARTS))


def test_a_part_over_five_gibibytes_is_rejected():
    problems = validate_parts(20_000.0, 1)
    assert any(f"{MAX_PART_GIB} GiB" in p for p in problems)


def test_zero_or_negative_parts_is_rejected():
    assert validate_parts(100.0, 0)
    assert validate_parts(100.0, -1)


def test_an_invalid_plan_reports_no_speedup_and_is_marked_not_worth_it():
    plan = simulate_multipart_upload(20.0, 8, PROTOCOL_HTTP2, FAR)
    assert not plan.valid
    assert not plan.worth_it
    assert plan.tone == "crit"


def test_the_sweep_marks_which_part_counts_r2_would_accept():
    rows = multipart_sweep(20.0, PROTOCOL_HTTP2, FAR)
    assert {row["Valid on R2"] for row in rows} == {"yes", "no"}
    for row in rows:
        assert all(isinstance(v, str) for v in row.values()), row


def test_the_documented_limits_are_stated_once_and_completely():
    text = " ".join(row["Value"] for row in r2_limits())
    assert f"{MIN_PART_MIB} MiB" in text
    assert f"{MAX_PART_GIB} GiB" in text
    assert f"{MAX_PARTS:,}" in text
    assert f"{MAX_OBJECT_TIB} TiB" in text
    assert "Required" in text
    for row in r2_limits():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# Presigned URLs
# ---------------------------------------------------------------------------

def test_seven_days_is_accepted_and_one_second_more_is_not():
    assert presign_validate(MAX_PRESIGN_SECONDS)[0] is True
    assert presign_validate(MAX_PRESIGN_SECONDS + 1)[0] is False


def test_the_minimum_lifetime_is_one_second():
    assert presign_validate(MIN_PRESIGN_SECONDS)[0] is True
    assert presign_validate(0)[0] is False
    assert presign_validate(-10)[0] is False


def test_a_refused_lifetime_says_why_in_the_message():
    ok, message = presign_validate(MAX_PRESIGN_SECONDS + 1)
    assert not ok
    assert "seven days" in message


# ---------------------------------------------------------------------------
# Edge latency
# ---------------------------------------------------------------------------

def test_every_edge_reports_all_four_phases():
    for edge in get_latency_breakdown():
        assert edge.dns_ms > 0 and edge.tls_ms > 0
        assert edge.ttfb_ms > 0 and edge.payload_ms > 0
        assert edge.location


def test_the_total_is_the_sum_of_the_phases():
    for edge in get_latency_breakdown():
        assert edge.total_ms == pytest.approx(
            edge.dns_ms + edge.tls_ms + edge.ttfb_ms + edge.payload_ms,
            abs=0.01)


def test_most_of_a_small_request_happens_before_the_payload():
    """Which is why the win is a warm connection, not a faster pipe."""
    for edge in get_latency_breakdown():
        assert edge.overhead_share > 0.5, edge.location
        assert edge.before_payload_ms > edge.payload_ms


def test_edges_are_ordered_nearest_first():
    totals = [edge.total_ms for edge in get_latency_breakdown()]
    assert totals == sorted(totals)


def test_a_more_distant_edge_costs_more_in_every_phase():
    edges = get_latency_breakdown()
    near, far = edges[0], edges[-1]
    assert far.dns_ms > near.dns_ms
    assert far.tls_ms > near.tls_ms
    assert far.ttfb_ms > near.ttfb_ms


def test_the_latency_rows_are_arrow_safe():
    for row in latency_rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------

def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (Path(__file__).resolve().parents[1] / "tools"
              / "cloudflare_r2_transfer_optimizer" / "core.py").read_text()
    assert "streamlit" not in source


def test_results_are_deterministic():
    assert calculate_transfer_math(2048.0, 120.0) == \
        calculate_transfer_math(2048.0, 120.0)
    first = simulate_multipart_upload(2048.0, 8, PROTOCOL_HTTP2, FAR)
    second = simulate_multipart_upload(2048.0, 8, PROTOCOL_HTTP2, FAR)
    assert first.parallel_seconds == second.parallel_seconds


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [calculate_transfer_math(2048.0, 120.0).note,
         calculate_transfer_math(5_000_000.0, 1.0).note,
         calculate_transfer_math(100.0, 0.0).note]
        + [t.note for t in NETWORK_TIERS]
        + validate_parts(20.0, 8)
        + [row["Value"] for row in r2_limits()]
        + [presign_validate(MAX_PRESIGN_SECONDS + 1)[1]])
    assert "—" not in text
    assert "–" not in text
