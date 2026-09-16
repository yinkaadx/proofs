"""Engine tests for the Megaport BGP RTBH and Blackhole Console.

Written for pytest. Deterministic: nothing in the engine reads the clock or a
random source.

This output gets pasted into a production router, so the tests care less about
the happy path than about every way a bad prefix could get through. A blackhole
generated for a /24 discards traffic for two hundred and fifty six hosts.

Run: pytest tests/test_megaport_bgp_rtbh_console.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.megaport_bgp_rtbh_console.core import (  # noqa: E402
    COMMUNITY_NOTE,
    DEFAULT_NEIGHBOR,
    DEFAULT_PARENT,
    DEFAULT_VICTIM,
    DROP_LOCAL,
    DROP_NONE,
    DROP_UPSTREAM,
    ENGINE_VERSION,
    HOST_LEN,
    MEGAPORT_BLACKHOLE,
    OK,
    REJECTED,
    WELL_KNOWN_BLACKHOLE,
    console_summary,
    drop_comparison,
    generate_cisco_rtbh_config,
    normalise_host,
    simulate_megaport_edge_drop,
    validate_blackhole_prefix,
)

GOOD = "203.0.113.45/32"


def failed_names(validation):
    return {c.name for c in validation.failures}


# ---------------------------------------------------------------------------
# Prefix validation: the refusals
# ---------------------------------------------------------------------------

def test_a_host_route_inside_the_parent_is_accepted():
    validation = validate_blackhole_prefix(GOOD, DEFAULT_PARENT)
    assert validation.valid
    assert validation.status == OK
    assert validation.failures == []


def test_a_slash_24_is_refused_because_it_would_drop_the_whole_subnet():
    validation = validate_blackhole_prefix("203.0.113.0/24", DEFAULT_PARENT)
    assert not validation.valid
    assert "Target is a single host, /32" in failed_names(validation)
    detail = " ".join(c.detail for c in validation.failures)
    assert "256" in detail, "the refusal should say how many hosts it covers"


# Each entry is a genuinely valid network at that length. The first attempt at
# this test used 203.0.113.0 for every one of them, which has host bits set at
# /8, /16 and /23, so those cases were refused at the parse check instead. The
# engine was right and the test was asking the wrong question, so the addresses
# are now correct for their masks and the host length check is what fires.
@pytest.mark.parametrize("network", [
    "203.0.0.0/8", "203.0.0.0/16", "203.0.112.0/23", "203.0.113.0/24",
    "203.0.113.0/25", "203.0.113.44/30", "203.0.113.44/31",
])
def test_every_prefix_length_short_of_a_host_route_is_refused(network):
    validation = validate_blackhole_prefix(network, "203.0.0.0/8")
    assert not validation.valid
    assert "Target is a single host, /32" in failed_names(validation)


def test_an_address_with_host_bits_set_is_refused_before_the_length_check():
    """A separate refusal, and a more precise one: the string is not a network
    at all, so saying it is the wrong length would be answering a question the
    input never posed."""
    validation = validate_blackhole_prefix("203.0.113.0/23", "203.0.0.0/8")
    assert not validation.valid
    assert failed_names(validation) == {"Target parses as a prefix"}


def test_a_host_outside_the_parent_prefix_is_refused():
    validation = validate_blackhole_prefix("198.51.100.7/32", DEFAULT_PARENT)
    assert not validation.valid
    assert "Target sits inside the parent prefix" in failed_names(validation)


def test_the_containment_refusal_explains_the_consequence():
    validation = validate_blackhole_prefix("198.51.100.7/32", DEFAULT_PARENT)
    detail = " ".join(c.detail for c in validation.failures)
    assert "somebody else" in detail or "authorised" in detail


def test_an_address_with_bits_below_the_mask_is_refused_not_rounded():
    """Silently rounding 203.0.113.45/24 down to 203.0.113.0/24 would
    blackhole the subnet while the operator believed they typed a host."""
    validation = validate_blackhole_prefix("203.0.113.45/24", DEFAULT_PARENT)
    assert not validation.valid
    assert "Target parses as a prefix" in failed_names(validation)


@pytest.mark.parametrize("bad", ["", "not-an-ip", "203.0.113.999/32",
                                 "203.0.113.45/33", "203.0.113.45/-1",
                                 "203.0.113/32"])
def test_garbage_input_is_refused_rather_than_guessed(bad):
    validation = validate_blackhole_prefix(bad, DEFAULT_PARENT)
    assert not validation.valid
    assert validation.status == REJECTED


def test_a_bad_parent_prefix_is_refused_too():
    validation = validate_blackhole_prefix(GOOD, "not-a-prefix")
    assert not validation.valid
    assert "Parent parses as a prefix" in failed_names(validation)


def test_mixing_address_families_is_refused():
    validation = validate_blackhole_prefix("2001:db8::1/128", DEFAULT_PARENT)
    assert not validation.valid
    assert "Address families match" in failed_names(validation)


def test_ipv6_requires_a_slash_128_and_accepts_one():
    good = validate_blackhole_prefix("2001:db8::1/128", "2001:db8::/32")
    assert good.valid
    assert good.version == 6
    assert good.host_length == 128

    bad = validate_blackhole_prefix("2001:db8::/64", "2001:db8::/32")
    assert not bad.valid
    assert "Target is a single host, /128" in failed_names(bad)


def test_a_host_equal_to_its_parent_is_refused_as_not_more_specific():
    """A /32 parent and a /32 target cannot win best path over the cover."""
    validation = validate_blackhole_prefix(GOOD, GOOD)
    assert not validation.valid
    assert "Target is more specific than the parent" in failed_names(validation)


def test_a_multicast_or_loopback_target_is_refused():
    for address in ("224.0.0.1/32", "127.0.0.1/32"):
        validation = validate_blackhole_prefix(address, "0.0.0.0/0")
        assert not validation.valid, address
        assert "Target is a routable unicast address" in failed_names(validation)


def test_the_validation_rows_are_arrow_safe():
    for row in validate_blackhole_prefix("203.0.113.0/24",
                                         DEFAULT_PARENT).rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# Host normalisation
# ---------------------------------------------------------------------------

def test_a_bare_address_becomes_a_host_prefix():
    assert normalise_host("203.0.113.45") == GOOD
    assert normalise_host("2001:db8::1") == "2001:db8::1/128"


def test_an_already_formed_host_prefix_is_unchanged():
    assert normalise_host(GOOD) == GOOD


def test_whitespace_around_the_address_is_tolerated():
    assert normalise_host("  203.0.113.45  ") == GOOD


def test_normalising_a_non_host_prefix_is_refused():
    with pytest.raises(ValueError) as caught:
        normalise_host("203.0.113.0/24")
    assert "/32" in str(caught.value)


# ---------------------------------------------------------------------------
# The Cisco configuration
# ---------------------------------------------------------------------------

def test_the_config_sets_both_communities_and_no_export():
    config = generate_cisco_rtbh_config()
    body = config.config
    assert MEGAPORT_BLACKHOLE in body
    assert WELL_KNOWN_BLACKHOLE in body
    assert "no-export" in body


def test_the_config_sends_communities_or_the_whole_thing_is_a_no_op():
    """Without this line BGP strips the communities and nothing is dropped,
    while every show command on this side still looks correct."""
    config = generate_cisco_rtbh_config()
    assert f"neighbor {DEFAULT_NEIGHBOR} send-community" in config.config


def test_the_config_contains_the_prefix_list_the_route_map_and_the_neighbor():
    body = generate_cisco_rtbh_config().config
    assert "ip prefix-list" in body
    assert "route-map" in body
    assert f"neighbor {DEFAULT_NEIGHBOR}" in body
    assert "Null0" in body


def test_the_prefix_list_permits_only_the_victim_host():
    config = generate_cisco_rtbh_config()
    permit = [line for line in config.lines if "prefix-list" in line
              and "permit" in line]
    assert len(permit) == 1
    assert config.victim in permit[0]


def test_the_route_map_matches_the_prefix_list_it_defines():
    config = generate_cisco_rtbh_config()
    body = config.config
    assert f"match ip address prefix-list {config.prefix_list_name}" in body
    assert f"route-map {config.route_map_name} permit 10" in body


def test_the_route_map_has_a_second_permit_so_other_routes_survive():
    """A route map applied outbound with only one clause denies everything
    else, which takes the whole session's advertisements down with it."""
    config = generate_cisco_rtbh_config()
    assert f"route-map {config.route_map_name} permit 20" in config.config


def test_the_static_route_uses_a_dotted_mask_for_ipv4():
    body = generate_cisco_rtbh_config().config
    assert "ip route 203.0.113.45 255.255.255.255 Null0" in body


def test_an_ipv6_victim_produces_ipv6_syntax_throughout():
    config = generate_cisco_rtbh_config("2001:db8::1", "2001:db8::/32",
                                        "2001:db8:ffff::1")
    body = config.config
    assert "ipv6 prefix-list" in body
    assert "ipv6 route 2001:db8::1/128 Null0" in body
    assert "address-family ipv6 unicast" in body
    assert "ip prefix-list" not in body


def test_the_local_asn_reaches_the_router_bgp_line():
    assert "router bgp 64512" in generate_cisco_rtbh_config(
        local_asn=64512).config


def test_a_custom_provider_community_is_used_instead_of_the_default():
    config = generate_cisco_rtbh_config(provider_community="64512:666")
    assert "64512:666" in config.config
    assert MEGAPORT_BLACKHOLE not in config.config
    # The well known one is always sent alongside it.
    assert WELL_KNOWN_BLACKHOLE in config.config


def test_generation_is_refused_for_anything_that_fails_validation():
    """The generator does not hand back a config and leave the checking to
    the operator."""
    for bad in ("203.0.113.0/24", "198.51.100.7", "not-an-ip"):
        with pytest.raises(ValueError):
            generate_cisco_rtbh_config(bad, DEFAULT_PARENT)


def test_the_rollback_is_generated_with_the_change():
    """A change that drops traffic needs its removal written before it is
    applied rather than recalled afterwards."""
    config = generate_cisco_rtbh_config()
    removal = config.removal
    assert "no ip prefix-list" in removal
    assert config.victim in removal
    assert "clear ip bgp" in removal


def test_the_warnings_say_rtbh_does_not_save_the_service():
    warnings = " ".join(generate_cisco_rtbh_config().warnings)
    assert "legitimate traffic" in warnings
    assert "does not save the service" in warnings


def test_the_community_caveat_is_carried_with_the_config():
    assert COMMUNITY_NOTE in generate_cisco_rtbh_config().warnings


def test_the_config_tells_the_operator_how_to_verify_it():
    body = generate_cisco_rtbh_config().config
    assert "advertised-routes" in body
    assert "Community" in body


def test_generation_is_deterministic():
    assert generate_cisco_rtbh_config().config == \
        generate_cisco_rtbh_config().config


# ---------------------------------------------------------------------------
# Where the traffic is dropped
# ---------------------------------------------------------------------------

def test_with_no_blackhole_the_attack_is_on_the_circuit():
    drop = simulate_megaport_edge_drop(False, 18.0, 10.0, 2.4)
    assert drop.state == DROP_NONE
    assert drop.utilisation_after == drop.utilisation_before
    assert drop.saturated_after


def test_discarding_at_your_own_border_does_not_free_the_circuit():
    """The packets crossed the link to reach the router that discards them.
    This is the state most easily mistaken for a working blackhole."""
    drop = simulate_megaport_edge_drop(True, 18.0, 10.0, 2.4,
                                       upstream_honours=False)
    assert drop.state == DROP_LOCAL
    assert drop.utilisation_after == drop.utilisation_before
    assert not drop.recovered
    assert "counters climb" in drop.note


def test_the_upstream_acting_on_the_community_frees_the_circuit():
    drop = simulate_megaport_edge_drop(True, 18.0, 10.0, 2.4,
                                       upstream_honours=True)
    assert drop.state == DROP_UPSTREAM
    assert drop.recovered
    assert drop.delivered_gbps == pytest.approx(2.4, abs=0.001)
    assert not drop.saturated_after


def test_the_recovered_circuit_still_leaves_the_victim_unreachable():
    """Which is the trade RTBH makes, not a side effect of it."""
    drop = simulate_megaport_edge_drop(True, 18.0, 10.0, 2.4)
    assert "remains unreachable" in drop.note


def test_utilisation_is_offered_over_capacity():
    drop = simulate_megaport_edge_drop(False, 18.0, 10.0, 2.4)
    assert drop.utilisation_before == pytest.approx((18.0 + 2.4) / 10.0,
                                                    abs=1e-6)


def test_an_attack_below_capacity_never_saturates():
    drop = simulate_megaport_edge_drop(False, 3.0, 10.0, 2.4)
    assert not drop.saturated_before
    assert drop.utilisation_before < 1.0


def test_a_bigger_attack_raises_utilisation_monotonically():
    levels = [simulate_megaport_edge_drop(False, gbps, 10.0, 2.4)
              .utilisation_before for gbps in (0.0, 5.0, 18.0, 90.0)]
    assert levels == sorted(levels)


def test_negative_inputs_are_floored_rather_than_producing_nonsense():
    drop = simulate_megaport_edge_drop(True, -5.0, 10.0, -2.0)
    assert drop.attack_gbps == 0.0
    assert drop.baseline_gbps == 0.0
    assert drop.utilisation_after >= 0.0


def test_a_zero_capacity_circuit_does_not_divide_by_zero():
    drop = simulate_megaport_edge_drop(True, 18.0, 0.0, 2.4)
    assert drop.circuit_gbps > 0
    assert drop.utilisation_before > 0


def test_the_comparison_shows_all_three_states():
    rows = drop_comparison(18.0, 10.0, 2.4)
    assert len(rows) == 3
    assert [row["Saturated"] for row in rows] == ["yes", "yes", "no"]
    for row in rows:
        assert all(isinstance(v, str) for v in row.values()), row


def test_the_drop_rows_are_arrow_safe():
    for row in simulate_megaport_edge_drop().rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# Summary and house rules
# ---------------------------------------------------------------------------

def test_the_summary_agrees_with_the_parts_it_counts():
    summary = console_summary()
    assert summary["provider_community"] == MEGAPORT_BLACKHOLE
    assert summary["well_known_community"] == WELL_KNOWN_BLACKHOLE
    assert summary["config_lines"] == len(generate_cisco_rtbh_config().lines)
    assert summary["recovered"] is True


def test_the_well_known_community_is_the_rfc_7999_value():
    assert WELL_KNOWN_BLACKHOLE == "65535:666"


def test_the_host_lengths_are_the_right_ones_for_each_family():
    assert HOST_LEN[4] == 32
    assert HOST_LEN[6] == 128


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "megaport_bgp_rtbh_console"
              / "core.py").read_text()
    assert "streamlit" not in source


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [COMMUNITY_NOTE]
        + generate_cisco_rtbh_config().warnings
        + [c.detail for c in validate_blackhole_prefix("203.0.113.0/24",
                                                       DEFAULT_PARENT).checks]
        + [simulate_megaport_edge_drop(b, 18.0, 10.0, 2.4, u).note
           for b, u in ((False, True), (True, False), (True, True))])
    assert "—" not in text
    assert "–" not in text
