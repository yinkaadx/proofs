"""Tests for the Fulfillment Process Engineering Console.

Three properties carry this file, and each is the thing the section exists
to assert rather than an example of it working.

Improving a step that is not the constraint must not change throughput.
Checked by taking every non constraint station in turn, halving it, and
requiring the throughput figure to be identical. A line where that fails is
a line whose advice sends effort to the wrong place.

Erlang C is computed two independent ways, by the numerically stable
recursion and by the textbook summation, and the two must agree. A queueing
formula typed from memory is exactly the kind of thing that is subtly wrong
and confidently quoted.

An unstable queue must report no average wait rather than a large one,
because at or above full utilisation there is no steady state and any figure
given for it is fiction.

Pass marker: pytest reports all tests passed, exit 0.
Fail marker: any failure or error line, exit non zero.
"""

from __future__ import annotations

import math
import subprocess
import sys
from itertools import product as combinations
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.fulfillment_process_engineering_console.core import (  # noqa: E402
    ENGINE_VERSION,
    PATTERNS,
    PATTERN_BY_KEY,
    PRIORITY_CONSTRAINT,
    PRIORITY_NOT_CONSTRAINT,
    SAMPLE_STEPS,
    SEVERITY_CRITICAL,
    UNIVERSAL_EDGE_CASES,
    UTILISATION_COMFORTABLE,
    ProcessStep,
    analyze_cycle_time_bottleneck,
    erlang_b,
    erlang_c,
    erlang_c_direct,
    generate_software_requirement,
    simulate_dispatch_capacity,
)


def steps_from(sample=SAMPLE_STEPS):
    return [ProcessStep(s["name"], float(s["minutes"]),
                        int(s["parallel_resources"]), bool(s["manual"]))
            for s in sample]


# ---------------------------------------------------------------------------
# The constraint
# ---------------------------------------------------------------------------


def test_throughput_is_set_by_the_slowest_station_and_nothing_else():
    report = analyze_cycle_time_bottleneck(steps_from())
    slowest = max(s.effective_minutes for s in steps_from())
    assert report.constraint_minutes == pytest.approx(slowest)
    assert report.throughput_per_hour == pytest.approx(60 / slowest, abs=1e-4)


def test_improving_any_non_constraint_step_changes_no_throughput():
    """The property the whole section exists to assert. Every non constraint
    station in turn, halved, and the number must not move."""
    base = analyze_cycle_time_bottleneck(steps_from())
    for index, step in enumerate(steps_from()):
        if step.name == base.constraint_name:
            continue
        faster = steps_from()
        faster[index] = ProcessStep(step.name, step.minutes / 2,
                                    step.parallel_resources, step.manual)
        improved = analyze_cycle_time_bottleneck(faster)
        assert improved.throughput_per_hour == base.throughput_per_hour, step.name
        assert improved.constraint_name == base.constraint_name, step.name


def test_improving_a_non_constraint_step_does_shorten_the_lead_time():
    """The counterpart. It changes how long one order takes and not how
    many an hour, and conflating those is the usual confusion."""
    base = analyze_cycle_time_bottleneck(steps_from())
    steps = steps_from()
    target = next(i for i, s in enumerate(steps)
                  if s.name != base.constraint_name)
    steps[target] = ProcessStep(steps[target].name, steps[target].minutes / 2,
                                steps[target].parallel_resources, True)
    improved = analyze_cycle_time_bottleneck(steps)
    assert improved.lead_time_minutes < base.lead_time_minutes
    assert improved.throughput_per_hour == base.throughput_per_hour


def test_improving_the_constraint_raises_throughput_until_it_moves():
    base = analyze_cycle_time_bottleneck(steps_from())
    others = [r.effective_minutes for r in base.readings if not r.is_constraint]
    runner_up = max(others)

    steps = steps_from()
    index = next(i for i, s in enumerate(steps)
                 if s.name == base.constraint_name)

    # Just better than before but still the constraint: throughput rises.
    midway = (base.constraint_minutes + runner_up) / 2
    steps[index] = ProcessStep(base.constraint_name, midway, 1, True)
    better = analyze_cycle_time_bottleneck(steps)
    assert better.throughput_per_hour > base.throughput_per_hour
    assert better.constraint_name == base.constraint_name

    # Far better: the constraint moves and the gain stops.
    steps[index] = ProcessStep(base.constraint_name, 0.5, 1, True)
    much_better = analyze_cycle_time_bottleneck(steps)
    assert much_better.constraint_name != base.constraint_name
    assert much_better.constraint_minutes == pytest.approx(runner_up)
    assert much_better.throughput_per_hour == pytest.approx(
        60 / runner_up, abs=1e-4)


def test_parallel_resources_divide_the_station_cycle_not_the_work():
    single = ProcessStep("Pick", 8.0, 1)
    doubled = ProcessStep("Pick", 8.0, 2)
    assert single.minutes == doubled.minutes
    assert doubled.effective_minutes == pytest.approx(4.0)
    assert doubled.units_per_hour == pytest.approx(2 * single.units_per_hour)


def test_the_counts_reconcile_for_any_line():
    lines = (
        steps_from(),
        [ProcessStep("A", 5), ProcessStep("B", 5), ProcessStep("C", 5)],
        [ProcessStep("Solo", 12)],
        [ProcessStep("A", 1), ProcessStep("B", 30), ProcessStep("C", 2)],
    )
    for line in lines:
        report = analyze_cycle_time_bottleneck(line)
        assert report.reconciles, [s.name for s in line]
        assert report.lead_time_minutes == pytest.approx(
            sum(s.effective_minutes for s in line))


def test_a_perfectly_balanced_line_has_no_idle_time():
    report = analyze_cycle_time_bottleneck(
        [ProcessStep("A", 5), ProcessStep("B", 5), ProcessStep("C", 5)])
    assert report.idle_minutes_per_unit == pytest.approx(0.0)
    assert report.line_balance_pct == pytest.approx(100.0)


def test_a_single_station_line_is_its_own_constraint():
    report = analyze_cycle_time_bottleneck([ProcessStep("Solo", 12)])
    assert report.constraint_name == "Solo"
    assert report.throughput_per_hour == pytest.approx(5.0)
    assert report.idle_minutes_per_unit == pytest.approx(0.0)


def test_a_manual_constraint_is_raised_as_the_one_worth_automating():
    report = analyze_cycle_time_bottleneck(steps_from())
    assert any(f.code == "BTL-MANUAL" for f in report.findings)
    automated = steps_from()
    index = next(i for i, s in enumerate(automated)
                 if s.name == report.constraint_name)
    automated[index] = ProcessStep(report.constraint_name,
                                   automated[index].minutes, 1, False)
    other = analyze_cycle_time_bottleneck(automated)
    assert any(f.code == "BTL-AUTOMATED" for f in other.findings)


def test_impossible_lines_raise():
    with pytest.raises(ValueError):
        analyze_cycle_time_bottleneck([])
    with pytest.raises(ValueError):
        analyze_cycle_time_bottleneck([ProcessStep("A", 0)])
    with pytest.raises(ValueError):
        analyze_cycle_time_bottleneck([ProcessStep("", 5)])
    with pytest.raises(ValueError):
        analyze_cycle_time_bottleneck([ProcessStep("A", 5, 0)])


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


def test_erlang_c_agrees_with_its_own_textbook_form():
    """A queueing formula typed from memory is exactly the kind of thing
    that is subtly wrong and confidently quoted, so it is computed two
    independent ways and the two must agree."""
    for servers, load in combinations((1, 2, 3, 6, 10, 15),
                                      (0.3, 0.9, 2.0, 4.5, 7.2, 9.9)):
        if load >= servers:
            continue
        assert erlang_c(servers, load) == pytest.approx(
            erlang_c_direct(servers, load), rel=1e-9), (servers, load)


def test_erlang_b_is_a_probability_and_falls_as_servers_rise():
    previous = 1.1
    for servers in range(1, 20):
        value = erlang_b(servers, 5.0)
        assert 0.0 <= value <= 1.0, servers
        assert value < previous, servers
        previous = value


def test_the_recursion_still_works_where_factorials_overflow():
    """The factorial form is the usual reason a capacity spreadsheet stops
    working at about twenty drivers."""
    value = erlang_c(200, 150.0)
    assert 0.0 <= value <= 1.0
    assert math.isfinite(value)


def test_utilisation_is_the_arrival_rate_over_the_fleet_capacity():
    for volume, drivers, minutes in combinations((6.0, 18.0, 40.0),
                                                 (2, 6, 12), (10.0, 18.0)):
        row = simulate_dispatch_capacity(volume, drivers, minutes)
        expected = volume / (drivers * (60 / minutes)) * 100
        assert row.utilisation_pct == pytest.approx(expected, abs=0.01)
        assert row.stable == (expected < 100)


def test_an_unstable_queue_reports_no_average_wait_rather_than_a_large_one():
    """At or above full utilisation there is no steady state, so any figure
    quoted for the average wait is fiction."""
    row = simulate_dispatch_capacity(40.0, 6, 18.0)
    assert not row.stable
    assert row.utilisation_pct >= 100
    assert math.isinf(row.average_wait_minutes)
    assert math.isinf(row.orders_waiting)
    assert row.severity == SEVERITY_CRITICAL
    assert any(f.code == "CAP-UNSTABLE" for f in row.findings)


def test_the_threshold_sits_exactly_at_full_utilisation():
    capacity_rate = 6 * (60 / 18)
    just_under = simulate_dispatch_capacity(capacity_rate * 0.999, 6, 18.0)
    just_over = simulate_dispatch_capacity(capacity_rate * 1.001, 6, 18.0)
    assert just_under.stable
    assert math.isfinite(just_under.average_wait_minutes)
    assert not just_over.stable


def test_the_wait_rises_faster_than_the_load_does():
    """The curve bends. Equal steps in utilisation produce unequal steps in
    wait, and each step is larger than the one before it."""
    capacity_rate = 8 * (60 / 20)
    waits = [simulate_dispatch_capacity(capacity_rate * share, 8,
                                        20.0).average_wait_minutes
             for share in (0.5, 0.6, 0.7, 0.8, 0.9)]
    assert waits == sorted(waits)
    jumps = [b - a for a, b in zip(waits, waits[1:])]
    assert jumps == sorted(jumps), jumps


def test_more_drivers_never_make_the_wait_longer():
    previous = float("inf")
    for drivers in range(3, 16):
        row = simulate_dispatch_capacity(18.0, drivers, 18.0)
        assert row.average_wait_minutes <= previous, drivers
        previous = row.average_wait_minutes


def test_the_recommended_fleet_sizes_actually_achieve_what_they_claim():
    for volume, minutes in combinations((12.0, 25.0, 45.0), (15.0, 30.0)):
        row = simulate_dispatch_capacity(volume, 1, minutes)
        stable = simulate_dispatch_capacity(volume,
                                            row.drivers_needed_for_stability,
                                            minutes)
        assert stable.stable, (volume, minutes)
        comfort = simulate_dispatch_capacity(volume,
                                             row.drivers_needed_for_comfort,
                                             minutes)
        assert comfort.utilisation_pct <= UTILISATION_COMFORTABLE + 0.01, (
            volume, minutes)


def test_no_orders_means_no_wait_and_no_instability():
    row = simulate_dispatch_capacity(0.0, 4, 20.0)
    assert row.stable
    assert row.utilisation_pct == pytest.approx(0.0)
    assert row.average_wait_minutes == pytest.approx(0.0)


def test_every_capacity_reading_ships_its_assumptions():
    row = simulate_dispatch_capacity(18.0, 6, 18.0)
    assert len(row.assumptions) >= 3
    assert any("Measure your own" in line for line in row.assumptions)
    assert any(f.code == "CAP-MODEL" for f in row.findings)


def test_impossible_fleets_raise():
    with pytest.raises(ValueError):
        simulate_dispatch_capacity(-1.0, 4, 20.0)
    with pytest.raises(ValueError):
        simulate_dispatch_capacity(10.0, 0, 20.0)
    with pytest.raises(ValueError):
        simulate_dispatch_capacity(10.0, 4, 0.0)
    with pytest.raises(ValueError):
        erlang_c(0, 1.0)


# ---------------------------------------------------------------------------
# The ticket
# ---------------------------------------------------------------------------


def test_every_ticket_carries_the_four_universal_edge_cases():
    for pattern in PATTERNS:
        ticket = generate_software_requirement(pattern.label)
        for case in UNIVERSAL_EDGE_CASES:
            assert case in ticket.edge_cases, pattern.key
        assert ticket.edge_case_count >= 4 + len(pattern.specific_edges)


def test_a_step_that_is_not_the_constraint_is_deprioritised_and_said_so():
    """Automating a non constraint spends engineering and changes no number."""
    ticket = generate_software_requirement("Manual label and carrier selection",
                                           is_the_constraint=False)
    assert ticket.priority == PRIORITY_NOT_CONSTRAINT
    assert ticket.severity == SEVERITY_CRITICAL
    assert any(f.code == "REQ-NOT-CONSTRAINT" for f in ticket.findings)
    assert PRIORITY_NOT_CONSTRAINT in ticket.ticket


def test_the_constraint_case_is_prioritised():
    ticket = generate_software_requirement("Manual label and carrier selection",
                                           is_the_constraint=True)
    assert ticket.priority == PRIORITY_CONSTRAINT
    assert any(f.code == "REQ-CONSTRAINT" for f in ticket.findings)


def test_every_pattern_is_reachable_from_plain_language():
    phrases = {
        "label": "we print the shipping label by hand",
        "pick": "someone walks the shelf with a paper pick list",
        "dispatch": "the supervisor assigns each driver manually",
        "status": "we email the customer a status update ourselves",
    }
    assert set(phrases) == set(PATTERN_BY_KEY)
    for key, phrase in phrases.items():
        assert generate_software_requirement(phrase).pattern_key == key, key


def test_an_unmatched_step_writes_no_trigger_rather_than_a_generic_one():
    """A generic ticket looks complete and specifies nothing."""
    ticket = generate_software_requirement("we do the thing with the widgets")
    assert not ticket.matched
    assert ticket.trigger_event == ""
    assert ticket.automated_action == ""
    assert ticket.edge_cases == UNIVERSAL_EDGE_CASES
    assert any(f.code == "REQ-UNMATCHED" for f in ticket.findings)
    assert "not specified" in ticket.ticket


def test_every_matched_ticket_has_a_trigger_an_action_and_acceptance():
    for pattern in PATTERNS:
        ticket = generate_software_requirement(pattern.label)
        assert ticket.matched, pattern.key
        assert len(ticket.trigger_event) > 20, pattern.key
        assert len(ticket.automated_action) > 40, pattern.key
        assert len(ticket.acceptance_criteria) >= 3, pattern.key
        assert "TRIGGER" in ticket.ticket
        assert "AUTOMATED ACTION" in ticket.ticket
        assert "ACCEPTANCE CRITERIA" in ticket.ticket


def test_every_ticket_insists_the_manual_path_survives():
    for pattern in PATTERNS:
        ticket = generate_software_requirement(pattern.label)
        assert any(f.code == "REQ-MANUAL-PATH" for f in ticket.findings)


def test_the_pattern_library_is_internally_consistent():
    keys = [p.key for p in PATTERNS]
    assert len(keys) == len(set(keys))
    seen: dict = {}
    for pattern in PATTERNS:
        assert len(pattern.specific_edges) >= 4, pattern.key
        assert pattern.minutes_saved_per_unit > 0, pattern.key
        for trigger in pattern.triggers:
            assert trigger == trigger.lower(), trigger
            assert trigger not in seen, (trigger, seen.get(trigger))
            seen[trigger] = pattern.key


def test_an_empty_bottleneck_raises():
    with pytest.raises(ValueError):
        generate_software_requirement("")
    with pytest.raises(ValueError):
        generate_software_requirement("   ")


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "fulfillment_process_engineering_console"
              / "core.py").read_text()
    assert "import streamlit" not in source
    probe = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.fulfillment_process_engineering_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT)
    )
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [report.headline + " ".join(f.title + f.detail + f.fix
                                    for f in report.findings)
         for report in (analyze_cycle_time_bottleneck(steps_from()),
                        analyze_cycle_time_bottleneck([ProcessStep("A", 5),
                                                       ProcessStep("B", 5)]))]
        + [row.headline + " ".join(row.assumptions)
           + " ".join(f.title + f.detail + f.fix for f in row.findings)
           for volume in (6.0, 18.0, 40.0)
           for row in (simulate_dispatch_capacity(volume, 6, 18.0),)]
        + list(UNIVERSAL_EDGE_CASES)
        + [ticket.ticket + " ".join(f.title + f.detail + f.fix
                                    for f in ticket.findings)
           for label in [p.label for p in PATTERNS] + ["unmatched thing"]
           for flag in (True, False)
           for ticket in (generate_software_requirement(label, flag),)]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools
    tools = all_tools()
    entry = next(t for t in tools
                 if t.key == "fulfillment-process-engineering-console")
    assert entry.title == "Fulfillment Process Engineering Console"
    icons = [t.icon for t in tools]
    assert icons.count(entry.icon) == 1, "this icon is already used by another tool"
    assert len(entry.tagline) > 30
