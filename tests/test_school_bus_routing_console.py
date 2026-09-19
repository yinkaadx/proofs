"""Tests for the School Bus Routing and Network Analyst Console.

Three invariants carry this file, and all three are properties rather than
examples.

A street is never shorter than the straight line it follows, so the walk zone
can only shrink when it is measured on the network. That direction is checked
across every network type and every radius rather than at one convenient
point, because a routing analysis that returned more walkers than the straight
line count would be wrong in a way nobody would question.

Every child is in exactly one group, and every stop is either kept or
removed. The counts reconcile or the model is not usable.

And the gross cost figure does not move when the disposition changes, while
the cash figure does. Reporting those as one number is how a saving gets
promised and then not found.

Pass marker: pytest reports all tests passed, exit 0.
Fail marker: any failure or error line, exit non zero.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from itertools import product as combinations
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.school_bus_routing_console.core import (  # noqa: E402
    BASELINE_WALK_MILES,
    COMFORTABLE_WALK_MILES,
    COST_SPLIT,
    DEFAULT_SCHOOL_DAYS,
    DISPOSITIONS,
    DISPOSITION_ELIMINATED,
    DISPOSITION_REDEPLOYED,
    DISPOSITION_RETAINED,
    ENGINE_VERSION,
    NETWORK_BY_KEY,
    NETWORK_TYPES,
    REALISED_BY_DISPOSITION,
    RUNS_PER_DAY,
    SECONDS_PER_STOP,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    calculate_network_vs_euclidean,
    generate_cost_impact_report,
    money,
    simulate_stop_consolidation,
)

RADII = (0.25, 0.5, 1.0, 1.5, 2.0, 3.0)
COUNTS = (50, 300, 1200, 5000)


# ---------------------------------------------------------------------------
# Street network against straight line
# ---------------------------------------------------------------------------


def test_circuity_is_never_below_one_for_any_network():
    """The fact the whole section rests on. A street cannot be shorter than
    the line it follows."""
    for network in NETWORK_TYPES:
        assert network.circuity >= 1.0, network.key
        assert 0.0 <= network.barrier_share < 1.0, network.key


def test_the_walk_zone_can_only_shrink_never_grow():
    """Checked across the grid, because an analysis returning more walkers
    than the straight line count would be wrong in a way nobody questions."""
    for count, radius, network in combinations(COUNTS, RADII, NETWORK_TYPES):
        split = calculate_network_vs_euclidean(count, radius, network)
        assert split.walkers_network <= split.walkers_straight_line, (
            count, radius, network.key)
        assert split.reclassified_to_bus >= 0, (count, radius, network.key)


def test_every_child_lands_in_exactly_one_group():
    for count, radius, network in combinations(COUNTS, RADII, NETWORK_TYPES):
        split = calculate_network_vs_euclidean(count, radius, network)
        assert split.reconciles, (count, radius, network.key)
        assert (split.walkers_network + split.bus_eligible_network
                == split.student_count)
        assert split.walkers_network >= 0
        assert split.bus_eligible_network >= 0


def test_a_more_tangled_network_never_leaves_more_children_walking():
    """Ordering the networks by circuity must order the walker counts the
    other way, for the same school."""
    ordered = sorted(NETWORK_TYPES, key=lambda n: n.circuity)
    for count, radius in combinations(COUNTS, RADII):
        walkers = [calculate_network_vs_euclidean(count, radius, n).walkers_network
                   for n in ordered]
        assert walkers == sorted(walkers, reverse=True), (count, radius)


def test_the_reachable_radius_is_the_policy_radius_divided_by_circuity():
    for radius, network in combinations(RADII, NETWORK_TYPES):
        split = calculate_network_vs_euclidean(1000, radius, network)
        assert split.effective_radius_miles == pytest.approx(
            radius / network.circuity, abs=0.001)
        assert split.effective_radius_miles <= radius


def test_barrier_isolated_students_are_never_counted_as_walkers():
    """Distance was never their problem, so a distance rule cannot find them."""
    for network in NETWORK_TYPES:
        split = calculate_network_vs_euclidean(2000, 1.5, network)
        assert split.barrier_isolated >= 0
        assert split.walkers_network + split.barrier_isolated <= split.student_count
        if split.barrier_isolated:
            assert any(f.code == "NET-BARRIER" and f.severity == SEVERITY_CRITICAL
                       for f in split.findings), network.key


def test_a_school_with_no_students_produces_no_walkers_and_no_riders():
    split = calculate_network_vs_euclidean(0, 1.0)
    assert split.walkers_network == 0
    assert split.bus_eligible_network == 0
    assert split.reconciles
    assert split.walk_zone_shrink_pct == 0.0


def test_the_shrink_percentage_matches_the_counts_it_summarises():
    for count, radius, network in combinations(COUNTS, RADII, NETWORK_TYPES):
        split = calculate_network_vs_euclidean(count, radius, network)
        assert split.walk_zone_shrink_pct == pytest.approx(
            round(split.reclassified_to_bus / count * 100, 1), abs=0.05)


def test_every_split_ships_its_assumptions():
    split = calculate_network_vs_euclidean(1200, 1.5)
    assert len(split.assumptions) >= 3
    assert any("not measured constants" in line for line in split.assumptions)


def test_a_network_can_be_named_by_object_key_or_label():
    by_key = calculate_network_vs_euclidean(1200, 1.5, "suburban")
    by_label = calculate_network_vs_euclidean(1200, 1.5, "Suburban cul de sac")
    by_object = calculate_network_vs_euclidean(1200, 1.5,
                                               NETWORK_BY_KEY["suburban"])
    assert by_key.walkers_network == by_label.walkers_network == by_object.walkers_network


def test_impossible_schools_raise():
    with pytest.raises(ValueError):
        calculate_network_vs_euclidean(-1, 1.0)
    with pytest.raises(ValueError):
        calculate_network_vs_euclidean(100, 0)
    with pytest.raises(ValueError):
        calculate_network_vs_euclidean(100, 50)
    with pytest.raises(ValueError):
        calculate_network_vs_euclidean(100, 1.0, "teleportation")


# ---------------------------------------------------------------------------
# Stop consolidation
# ---------------------------------------------------------------------------


def test_every_stop_is_either_kept_or_removed():
    for stops in (5, 40, 80, 200):
        for walk in (0.05, 0.10, 0.25, 0.40, 0.60):
            plan = simulate_stop_consolidation(stops, walk)
            assert plan.reconciles, (stops, walk)
            assert plan.optimized_stops >= 1, (stops, walk)
            assert plan.optimized_stops <= stops, (stops, walk)
            assert plan.stops_removed >= 0, (stops, walk)


def test_a_longer_walk_never_needs_more_stops():
    for stops in (40, 80, 200):
        counts = [simulate_stop_consolidation(stops, w).optimized_stops
                  for w in (0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.60)]
        assert counts == sorted(counts, reverse=True), stops


def test_doubling_the_walk_roughly_halves_the_stops():
    """Stops sit along a route, so the count falls in proportion to the walk."""
    base = simulate_stop_consolidation(200, 0.10).optimized_stops
    double = simulate_stop_consolidation(200, 0.20).optimized_stops
    assert double == pytest.approx(base / 2, abs=1)


def test_a_walk_at_or_below_the_baseline_removes_nothing():
    for walk in (0.05, BASELINE_WALK_MILES):
        plan = simulate_stop_consolidation(80, walk)
        assert plan.stops_removed == 0, walk
        assert plan.optimized_stops == 80, walk
        assert plan.daily_minutes_saved == 0.0, walk
        assert any(f.code == "STOP-NONE" for f in plan.findings), walk


def test_the_time_saved_follows_the_stops_removed_and_the_dwell():
    for stops in (40, 80, 200):
        for walk in (0.15, 0.25, 0.40):
            plan = simulate_stop_consolidation(stops, walk)
            assert plan.seconds_saved_per_run == plan.stops_removed * SECONDS_PER_STOP
            assert plan.daily_minutes_saved == pytest.approx(
                plan.seconds_saved_per_run * RUNS_PER_DAY / 60, abs=0.05)


def test_a_long_walk_to_the_stop_is_raised_as_critical():
    """The saving is paid for by families, and past a point it is not payable."""
    far = simulate_stop_consolidation(80, COMFORTABLE_WALK_MILES + 0.05)
    assert far.severity == SEVERITY_CRITICAL
    assert any(f.code == "STOP-WALK-LONG" for f in far.findings)
    near = simulate_stop_consolidation(80, COMFORTABLE_WALK_MILES)
    assert not any(f.code == "STOP-WALK-LONG" for f in near.findings)


def test_any_removal_raises_the_crossing_that_has_to_be_checked():
    plan = simulate_stop_consolidation(80, 0.25)
    assert plan.stops_removed > 0
    assert any(f.code == "STOP-HAZARD" and f.severity == SEVERITY_CRITICAL
               for f in plan.findings)


def test_the_annual_hours_follow_the_daily_minutes_and_the_year():
    for days in (150, 180, 200):
        plan = simulate_stop_consolidation(80, 0.25, school_days=days)
        assert plan.annual_hours_saved == pytest.approx(
            plan.daily_minutes_saved * days / 60, abs=0.1)


def test_impossible_routes_raise():
    with pytest.raises(ValueError):
        simulate_stop_consolidation(0, 0.25)
    with pytest.raises(ValueError):
        simulate_stop_consolidation(80, 0)
    with pytest.raises(ValueError):
        simulate_stop_consolidation(80, 5)
    with pytest.raises(ValueError):
        simulate_stop_consolidation(80, 0.25, school_days=0)


# ---------------------------------------------------------------------------
# Cost impact
# ---------------------------------------------------------------------------


def test_the_cost_split_covers_the_whole_cost():
    assert sum(share for _c, share, _v in COST_SPLIT) == pytest.approx(1.0)


def test_the_gross_does_not_move_when_the_disposition_does():
    """The gross is arithmetic. Only the cash depends on what actually
    happens to the bus and the driver."""
    grosses = {generate_cost_impact_report(3, "515.00", d).gross_annual_saving
               for d in DISPOSITIONS}
    assert len(grosses) == 1


def test_the_cash_does_move_and_in_the_right_order():
    """Eliminated saves the most. Redeployed saves no cash at all, because
    the bus is still driving a route and the driver is still paid."""
    eliminated = generate_cost_impact_report(3, "515.00", DISPOSITION_ELIMINATED)
    retained = generate_cost_impact_report(3, "515.00", DISPOSITION_RETAINED)
    redeployed = generate_cost_impact_report(3, "515.00", DISPOSITION_REDEPLOYED)
    assert eliminated.cash_annual_saving > retained.cash_annual_saving
    assert retained.cash_annual_saving > redeployed.cash_annual_saving
    assert redeployed.cash_annual_saving == money(0)
    assert eliminated.capacity_annual_value == money(0)
    assert redeployed.capacity_annual_value == redeployed.gross_annual_saving


def test_cash_plus_capacity_always_equals_the_gross():
    for buses, disposition, days in combinations((0, 1, 3, 12), DISPOSITIONS,
                                                 (150, 180, 200)):
        impact = generate_cost_impact_report(buses, "515.00", disposition, days)
        assert impact.reconciles, (buses, disposition, days)
        assert money(impact.cash_annual_saving + impact.capacity_annual_value) \
            == impact.gross_annual_saving


def test_the_component_rows_always_add_to_the_gross():
    for buses, disposition in combinations((1, 3, 25), DISPOSITIONS):
        impact = generate_cost_impact_report(buses, "742.50", disposition)
        assert impact.rows_annual_total == impact.gross_annual_saving
        assert len(impact.rows) == len(COST_SPLIT)


def test_each_disposition_realises_exactly_the_components_it_should():
    for disposition in DISPOSITIONS:
        impact = generate_cost_impact_report(3, "515.00", disposition)
        realised = {row.component for row in impact.rows if row.realised}
        assert realised == set(REALISED_BY_DISPOSITION[disposition]), disposition


def test_a_redeployed_bus_is_still_burning_fuel():
    """The defect the first draft had: redeployed and retained returning the
    same cash. A redeployed bus drives a new route, a parked spare does not."""
    redeployed = generate_cost_impact_report(3, "515.00", DISPOSITION_REDEPLOYED)
    retained = generate_cost_impact_report(3, "515.00", DISPOSITION_RETAINED)
    assert redeployed.cash_annual_saving != retained.cash_annual_saving
    fuel_redeployed = next(r for r in redeployed.rows if r.component == "Fuel")
    fuel_retained = next(r for r in retained.rows if r.component == "Fuel")
    assert not fuel_redeployed.realised
    assert fuel_retained.realised


def test_the_driver_is_the_largest_component():
    shares = {component: share for component, share, _v in COST_SPLIT}
    assert shares["Driver wages and benefits"] == max(shares.values())
    impact = generate_cost_impact_report(3, "515.00")
    assert any(f.code == "COST-DRIVER-SHARE" for f in impact.findings)


def test_saving_no_buses_saves_no_money():
    for disposition in DISPOSITIONS:
        impact = generate_cost_impact_report(0, "515.00", disposition)
        assert impact.gross_annual_saving == money(0), disposition
        assert impact.cash_annual_saving == money(0), disposition
        assert impact.reconciles, disposition


def test_the_gross_scales_with_the_buses_and_the_days():
    one = generate_cost_impact_report(1, "500.00", DISPOSITION_ELIMINATED, 180)
    four = generate_cost_impact_report(4, "500.00", DISPOSITION_ELIMINATED, 180)
    assert four.gross_annual_saving == money(one.gross_annual_saving * 4)
    short = generate_cost_impact_report(1, "500.00", DISPOSITION_ELIMINATED, 90)
    assert short.gross_annual_saving == money(one.gross_annual_saving / 2)


def test_the_leadership_summary_names_the_question_to_settle_first():
    impact = generate_cost_impact_report(3, "515.00", DISPOSITION_REDEPLOYED)
    labels = [label for label, _value in impact.leadership_summary]
    assert "The question to settle first" in labels
    joined = " ".join(value for _label, value in impact.leadership_summary)
    assert "driver positions actually go" in joined


def test_impossible_costs_raise():
    with pytest.raises(ValueError):
        generate_cost_impact_report(-1, "515.00")
    with pytest.raises(ValueError):
        generate_cost_impact_report(3, "0.00")
    with pytest.raises(ValueError):
        generate_cost_impact_report(3, "-5.00")
    with pytest.raises(ValueError):
        generate_cost_impact_report(3, "515.00", "sold for scrap")
    with pytest.raises(ValueError):
        generate_cost_impact_report(3, "515.00", DISPOSITION_ELIMINATED, 0)


def test_money_rounds_once_at_the_point_it_becomes_currency():
    assert money("0.005") == Decimal("0.01")
    assert money("2.344") == Decimal("2.34")
    assert money("2.345") == Decimal("2.35")


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"
    assert DEFAULT_SCHOOL_DAYS == 180


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "school_bus_routing_console" / "core.py").read_text()
    assert "import streamlit" not in source
    probe = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.school_bus_routing_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT)
    )
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [n.note + n.label for n in NETWORK_TYPES]
        + [split.headline + " ".join(split.assumptions)
           + " ".join(f.title + f.detail + f.fix for f in split.findings)
           for network in NETWORK_TYPES
           for split in (calculate_network_vs_euclidean(1200, 1.5, network),)]
        + [plan.headline + " ".join(plan.assumptions)
           + " ".join(f.title + f.detail + f.fix for f in plan.findings)
           for walk in (0.05, 0.25, 0.45)
           for plan in (simulate_stop_consolidation(80, walk),)]
        + [impact.headline + " ".join(impact.assumptions)
           + " ".join(label + value for label, value in impact.leadership_summary)
           + " ".join(f.title + f.detail + f.fix for f in impact.findings)
           for disposition in DISPOSITIONS
           for impact in (generate_cost_impact_report(3, "515.00", disposition),)]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools
    tools = all_tools()
    entry = next(t for t in tools if t.key == "school-bus-routing-console")
    assert entry.title == "School Bus Routing & Network Analyst Console"
    icons = [t.icon for t in tools]
    assert icons.count(entry.icon) == 1, "this icon is already used by another tool"
    assert len(entry.tagline) > 30
