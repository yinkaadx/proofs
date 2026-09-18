"""Tests for the Transfer Booking and Routing Console.

Two things are worth proving. The fare curve must have no cliff in it: banded
pricing applied to the whole journey rather than marginally makes a 50.1 km
run cheaper than a 49.9 km one, and customers find that before finance does.
And the state machine must have no move that bills for a journey nobody took,
which means terminal states offer nothing and no state is unreachable.

Pass marker: pytest reports all tests passed, exit 0.
Fail marker: any failure or error line, exit non zero.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.transfer_booking_routing_console.core import (  # noqa: E402
    BASE_FARE,
    BILLABLE_FROM,
    DISTANCE_BANDS,
    ENGINE_VERSION,
    FIXED_ROUTES,
    HAPPY_PATH_NEXT,
    MINIMUM_FARE,
    OS_ANDROID,
    OS_IOS,
    OS_TYPES,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    STATUS_ARRIVED,
    STATUS_ASSIGNED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_EN_ROUTE,
    STATUS_NO_SHOW,
    STATUS_ON_BOARD,
    STATUS_UNASSIGNED,
    TERMINAL_STATUSES,
    TRANSITIONS,
    VEHICLES,
    VEHICLE_BY_KEY,
    VERDICT_NOT_POSSIBLE,
    band_breakdown,
    calculate_transfer_fare,
    evaluate_web_gps_limits,
    fixed_route_break_even,
    money,
    reachable_statuses,
    simulate_driver_status,
)

SALOON = "saloon"


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def test_the_bands_cover_the_distance_exactly_and_lose_nothing():
    for distance in (0.0, 0.4, 9.9, 10.0, 10.1, 49.9, 50.0, 87.3, 500.0):
        rows = band_breakdown(distance)
        assert sum(km for _band, km in rows) == pytest.approx(distance), distance


def test_the_fare_curve_has_no_cliff_at_any_band_boundary():
    """The bug this whole design exists to prevent. Charging the journey at
    the band it ends in makes a longer trip cheaper than a shorter one."""
    for band in DISTANCE_BANDS:
        if band.upper_km == float("inf"):
            continue
        edge = band.upper_km
        before = calculate_transfer_fare(edge - 0.1, SALOON).metered_fare
        at = calculate_transfer_fare(edge, SALOON).metered_fare
        after = calculate_transfer_fare(edge + 0.1, SALOON).metered_fare
        assert before <= at <= after, edge
        # And the step across the boundary stays small, which is what
        # "no cliff" actually means rather than merely "increasing".
        assert after - before < Decimal("1.00"), edge


def test_the_fare_never_decreases_as_the_journey_gets_longer():
    previous = Decimal("-1")
    distance = 0.0
    while distance <= 160.0:
        fare = calculate_transfer_fare(distance, SALOON).metered_fare
        assert fare >= previous, distance
        previous = fare
        distance = round(distance + 0.5, 1)


def test_the_printed_rows_always_add_up_to_the_subtotal():
    for distance in (0.5, 8.0, 28.0, 41.5, 96.0, 150.0):
        for vehicle in VEHICLES:
            quote = calculate_transfer_fare(distance, vehicle.key)
            assert quote.rows_total == quote.subtotal, (distance, vehicle.key)


def test_the_base_fare_is_charged_once_and_only_once():
    for distance in (1.0, 30.0, 120.0):
        rows = calculate_transfer_fare(distance, SALOON).rows
        base_rows = [r for r in rows if r.label == "Base fare"]
        assert len(base_rows) == 1, distance
        assert base_rows[0].amount == money(BASE_FARE)


def test_the_minimum_fare_carries_a_short_job_and_is_flagged():
    short = calculate_transfer_fare(0.5, SALOON)
    assert short.minimum_applied
    assert short.metered_fare == money(MINIMUM_FARE)
    assert short.subtotal < money(MINIMUM_FARE)
    assert any(f.code == "FARE-MINIMUM" for f in short.findings)
    long = calculate_transfer_fare(30.0, SALOON)
    assert not long.minimum_applied
    assert long.metered_fare == long.subtotal


def test_the_metered_fare_is_never_below_the_minimum():
    for distance in (0.0, 0.1, 1.0, 3.0, 5.0, 12.0):
        for vehicle in VEHICLES:
            fare = calculate_transfer_fare(distance, vehicle.key).metered_fare
            assert fare >= money(MINIMUM_FARE), (distance, vehicle.key)


def test_a_bigger_vehicle_never_costs_less_than_a_smaller_one():
    ordered = sorted(VEHICLES, key=lambda v: v.multiplier)
    for distance in (15.0, 40.0, 100.0):
        fares = [calculate_transfer_fare(distance, v.key).metered_fare
                 for v in ordered]
        assert fares == sorted(fares), distance


def test_the_saloon_multiplier_adds_no_uplift_row():
    rows = calculate_transfer_fare(30.0, SALOON).rows
    assert not any("uplift" in row.label for row in rows)
    assert VEHICLE_BY_KEY[SALOON].multiplier == Decimal("1.00")
    mpv_rows = calculate_transfer_fare(30.0, "mpv").rows
    assert any("uplift" in row.label for row in mpv_rows)


def test_a_flat_price_below_the_meter_is_reported_as_a_loss():
    route = FIXED_ROUTES[0]
    quote = calculate_transfer_fare(route.distance_km, "minibus", route.name)
    assert quote.difference > 0
    assert quote.fixed_route_loses_money
    assert quote.severity == SEVERITY_CRITICAL
    assert any(f.code == "FARE-FIXED-LOSS" for f in quote.findings)


def test_the_difference_is_always_the_meter_less_the_flat_price():
    for route in FIXED_ROUTES:
        for vehicle in VEHICLES:
            quote = calculate_transfer_fare(route.distance_km, vehicle.key,
                                            route.name)
            assert quote.difference == money(
                quote.metered_fare - route.price), (route.name, vehicle.key)
            assert quote.fixed_price == route.price


def test_no_fixed_route_means_no_comparison_rather_than_a_zero():
    quote = calculate_transfer_fare(28.0, SALOON)
    assert quote.fixed_route is None
    assert quote.fixed_price is None
    assert quote.difference is None
    assert not quote.fixed_route_loses_money


def test_the_break_even_helper_agrees_with_the_quote_it_summarises():
    for route in FIXED_ROUTES:
        for vehicle in VEHICLES:
            assert fixed_route_break_even(route, vehicle.key) == (
                calculate_transfer_fare(route.distance_km,
                                        vehicle.key).metered_fare)


def test_a_vehicle_can_be_named_by_object_key_or_title():
    by_key = calculate_transfer_fare(30.0, "mpv").metered_fare
    by_title = calculate_transfer_fare(30.0, "MPV").metered_fare
    by_object = calculate_transfer_fare(30.0, VEHICLE_BY_KEY["mpv"]).metered_fare
    assert by_key == by_title == by_object


def test_impossible_journeys_raise():
    with pytest.raises(ValueError):
        calculate_transfer_fare(-1.0, SALOON)
    with pytest.raises(ValueError):
        calculate_transfer_fare(5000.0, SALOON)
    with pytest.raises(ValueError):
        calculate_transfer_fare(30.0, "hovercraft")
    with pytest.raises(ValueError):
        band_breakdown(-0.1)


# ---------------------------------------------------------------------------
# Web GPS limits
# ---------------------------------------------------------------------------


def test_background_tracking_is_impossible_on_both_platforms():
    """The answer is the same on both, and the tool must not soften it."""
    for os_type in OS_TYPES:
        assessment = evaluate_web_gps_limits(os_type)
        assert assessment.verdict == VERDICT_NOT_POSSIBLE, os_type
        assert assessment.background_tracking_possible is False, os_type
        assert assessment.blocking_constraints, os_type


def test_the_service_worker_constraint_applies_to_every_platform():
    """It is the one that decides the answer, so it cannot be platform
    specific."""
    for os_type in OS_TYPES:
        codes = {c.code for c in evaluate_web_gps_limits(os_type).constraints}
        assert "GPS-SW" in codes, os_type
    shared = evaluate_web_gps_limits(OS_IOS)
    worker = next(c for c in shared.constraints if c.code == "GPS-SW")
    assert worker.blocking
    assert "Nothing works around this" in worker.workaround


def test_each_platform_carries_its_own_reason_as_well_as_the_shared_one():
    ios = {c.code for c in evaluate_web_gps_limits(OS_IOS).constraints}
    android = {c.code for c in evaluate_web_gps_limits(OS_ANDROID).constraints}
    assert "GPS-IOS-SUSPEND" in ios and "GPS-IOS-SUSPEND" not in android
    assert "GPS-AND-FREEZE" in android and "GPS-AND-FREEZE" not in ios
    assert ios & android, "the shared constraints must appear on both"


def test_every_constraint_names_a_workaround_or_says_there_is_none():
    for os_type in OS_TYPES:
        for constraint in evaluate_web_gps_limits(os_type).constraints:
            assert constraint.workaround.strip(), constraint.code
            assert len(constraint.detail) > 40, constraint.code


def test_three_working_alternatives_are_always_offered():
    for os_type in OS_TYPES:
        alternatives = evaluate_web_gps_limits(os_type).alternatives
        assert len(alternatives) == 3, os_type
        joined = " ".join(alternatives).lower()
        assert "native" in joined
        assert "driver initiated" in joined or "taps" in joined
        assert "wake lock" in joined


def test_an_unknown_platform_raises_rather_than_guessing():
    for bad in ("Windows Phone", "", "firefox"):
        with pytest.raises(ValueError):
            evaluate_web_gps_limits(bad)


# ---------------------------------------------------------------------------
# Driver status
# ---------------------------------------------------------------------------


def test_the_happy_path_walks_from_unassigned_to_completed():
    status = STATUS_UNASSIGNED
    walked = [status]
    for _step in range(10):
        transition = simulate_driver_status(status)
        if transition.is_terminal:
            break
        status = transition.next_status
        walked.append(status)
    assert walked == [STATUS_UNASSIGNED, STATUS_ASSIGNED, STATUS_EN_ROUTE,
                      STATUS_ARRIVED, STATUS_ON_BOARD, STATUS_COMPLETED]


def test_a_terminal_state_offers_nothing_at_all():
    """A finished job with a next step is a job billed twice."""
    for status in TERMINAL_STATUSES:
        transition = simulate_driver_status(status)
        assert transition.is_terminal, status
        assert transition.allowed_next == (), status
        assert transition.next_status is None, status
        assert transition.exceptions == (), status
        assert not transition.permits(STATUS_ASSIGNED), status


def test_no_state_can_skip_straight_to_completed():
    """Assigned to Completed is a board that bills for journeys nobody took."""
    for status, allowed in TRANSITIONS.items():
        if status == STATUS_ON_BOARD:
            assert STATUS_COMPLETED in allowed
        else:
            assert STATUS_COMPLETED not in allowed, status


def test_a_no_show_can_only_be_claimed_after_arriving():
    for status, allowed in TRANSITIONS.items():
        if status == STATUS_ARRIVED:
            assert STATUS_NO_SHOW in allowed
        else:
            assert STATUS_NO_SHOW not in allowed, status


def test_a_job_cannot_be_cancelled_once_the_passenger_is_aboard():
    aboard = simulate_driver_status(STATUS_ON_BOARD)
    assert STATUS_CANCELLED not in aboard.allowed_next
    assert aboard.allowed_next == (STATUS_COMPLETED,)
    assert aboard.billable
    assert BILLABLE_FROM == STATUS_ON_BOARD


def test_every_state_is_reachable_from_the_start():
    """An unreachable state is dead code in the board and a bug in the map."""
    assert reachable_statuses(STATUS_UNASSIGNED) == set(TRANSITIONS)


def test_every_named_destination_is_itself_a_known_state():
    for status, allowed in TRANSITIONS.items():
        for destination in allowed:
            assert destination in TRANSITIONS, (status, destination)
        assert len(set(allowed)) == len(allowed), status
        assert status not in allowed, f"{status} transitions to itself"


def test_the_happy_path_move_is_always_one_of_the_allowed_moves():
    for status, expected in HAPPY_PATH_NEXT.items():
        transition = simulate_driver_status(status)
        assert transition.next_status == expected, status
        assert transition.permits(expected), status
        assert set(transition.exceptions) == set(transition.allowed_next) - {expected}
        assert len(transition.exceptions) == len(transition.allowed_next) - 1


def test_every_non_terminal_state_has_a_happy_path_and_terminals_do_not():
    for status in TRANSITIONS:
        if status in TERMINAL_STATUSES:
            assert status not in HAPPY_PATH_NEXT, status
        else:
            assert status in HAPPY_PATH_NEXT, status


def test_an_unknown_status_raises_rather_than_falling_through():
    for bad in ("Driving", "", "COMPLETED", "  "):
        with pytest.raises(ValueError):
            simulate_driver_status(bad)


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "transfer_booking_routing_console"
              / "core.py").read_text()
    assert "import streamlit" not in source
    probe = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.transfer_booking_routing_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT)
    )
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [q.headline + " ".join(f.title + f.detail + f.fix for f in q.findings)
         + " ".join(row.label for row in q.rows)
         for route in FIXED_ROUTES
         for vehicle in VEHICLES
         for q in (calculate_transfer_fare(route.distance_km, vehicle.key,
                                           route.name),)]
        + [v.note + v.title for v in VEHICLES]
        + [b.label for b in DISTANCE_BANDS]
        + [a.headline + a.fix + " ".join(a.alternatives)
           + " ".join(c.title + c.detail + c.workaround for c in a.constraints)
           for a in (evaluate_web_gps_limits(os) for os in OS_TYPES)]
        + [t.headline + t.fix
           + " ".join(f.title + f.detail + f.fix for f in t.findings)
           for t in (simulate_driver_status(s) for s in TRANSITIONS)]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools
    tools = all_tools()
    entry = next(t for t in tools if t.key == "transfer-booking-routing-console")
    assert entry.title == "Transfer Booking & Routing Console"
    icons = [t.icon for t in tools]
    assert icons.count(entry.icon) == 1, "this icon is already used by another tool"
    assert len(entry.tagline) > 30
