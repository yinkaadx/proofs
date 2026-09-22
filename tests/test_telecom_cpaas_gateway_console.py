"""Tests for the Telecom CPaaS and SMPP Gateway Console.

The USSD tests are about what is not delivered, because a late response
is discarded by the network while the application's own write succeeds.
The SMPP tests are about the receipts a failover strands, since those
messages sit as sent with no receipt and nothing raises.
"""

from __future__ import annotations

import itertools
import pathlib
import subprocess
import sys

import pytest

from tools.telecom_cpaas_gateway_console.core import (
    APPLICATION_TIMEOUT_SECONDS,
    ENGINE_VERSION,
    LICENSED_TPS,
    MENU_TREE,
    NETWORK_SESSION_BUDGET_SECONDS,
    PREFIX_CONTINUE,
    PREFIX_END,
    PRIMARY_BOUND,
    PRIMARY_LINK_DEAD,
    PRIMARY_QUEUE_FULL,
    PRIMARY_STATES,
    PRIMARY_THROTTLED,
    PRIMARY_UNBOUND,
    ROUTE_BACKPRESSURE,
    ROUTE_HELD,
    ROUTE_PRIMARY,
    ROUTE_SECONDARY,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SMPP_WINDOW_SIZE,
    STACK_CUSTOM,
    STACK_KANNEL,
    STATE_ACTIVE,
    STATE_COMPLETED,
    STATE_TIMED_OUT,
    USSD_MAX_CHARACTERS,
    compare_stacks,
    get_stack,
    get_telecom_architecture_matrix,
    reconnect_ramp,
    simulate_smpp_carrier_failover,
    simulate_ussd_session,
    validate_menu_payload,
)
from tools.registry import all_tools

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL_DIR = ROOT / "tools" / "telecom_cpaas_gateway_console"

SESSION = "ATUid_7f31a8"


# ---------------------------------------------------------------------------
# USSD session lifecycle
# ---------------------------------------------------------------------------

def test_a_prompt_reply_keeps_the_session_active():
    session = simulate_ussd_session(SESSION, 1, 2.0)
    assert session.state == STATE_ACTIVE
    assert session.active
    assert session.payload_delivered
    assert session.menu_payload.startswith(PREFIX_CONTINUE)


def test_the_terminal_step_closes_the_session_with_end():
    """Leaving it as CON holds a session slot open until the operator's cap
    expires, and the subscriber cannot use it for anything else."""
    session = simulate_ussd_session(SESSION, len(MENU_TREE) - 1, 1.0)
    assert session.state == STATE_COMPLETED
    assert session.menu_payload.startswith(PREFIX_END)
    assert "USD-END" in {f.code for f in session.findings}


def test_a_reply_past_the_budget_times_the_session_out():
    session = simulate_ussd_session(SESSION, 2, 12.5)
    assert session.state == STATE_TIMED_OUT
    assert session.timed_out
    assert "USD-TIMEOUT" in {f.code for f in session.findings}
    assert session.severity == SEVERITY_CRITICAL


def test_a_timed_out_step_delivers_no_payload_at_all():
    """The network released the session, so the response goes nowhere. The
    application's own write still succeeds, which is why this reads as a
    success in every log the application keeps."""
    session = simulate_ussd_session(SESSION, 2, 15.0)
    assert not session.payload_delivered
    assert session.menu_payload == ""
    assert session.intercept_message
    assert "never delivered" in session.intercept_message


def test_the_intercept_message_is_for_the_log_and_not_the_subscriber():
    session = simulate_ussd_session(SESSION, 2, 15.0)
    assert SESSION in session.intercept_message
    assert not session.intercept_message.startswith(PREFIX_CONTINUE)
    assert not session.intercept_message.startswith(PREFIX_END)


def test_the_boundary_is_exclusive_at_exactly_the_budget():
    assert not simulate_ussd_session(
        SESSION, 1, float(APPLICATION_TIMEOUT_SECONDS)).timed_out
    assert simulate_ussd_session(
        SESSION, 1, APPLICATION_TIMEOUT_SECONDS + 0.1).timed_out


def test_the_operator_cap_times_out_a_session_that_each_step_passed():
    """The cap covers the whole session and not each step, so a flow that
    fits comfortably in testing runs out on a slow reader."""
    session = simulate_ussd_session(
        SESSION, 2, 5.0, elapsed_seconds=NETWORK_SESSION_BUDGET_SECONDS)
    assert session.timed_out
    assert not session.payload_delivered
    assert "operator session cap" in session.intercept_message


def test_a_timed_out_session_can_never_be_resumed():
    """All state is server side against an identifier that changes when
    the subscriber dials again."""
    session = simulate_ussd_session(SESSION, 2, 20.0)
    assert "USD-NORESUME" in {f.code for f in session.findings}


def test_every_menu_payload_fits_the_air_interface_limit():
    for node in MENU_TREE:
        valid, note = validate_menu_payload(node.payload)
        assert valid, (node.step, note)
        assert len(node.payload) <= USSD_MAX_CHARACTERS, node.step


def test_an_over_length_payload_is_refused_by_the_validator():
    valid, note = validate_menu_payload(PREFIX_CONTINUE + "x" * 200)
    assert not valid
    assert str(USSD_MAX_CHARACTERS) in note


def test_a_payload_with_no_prefix_is_refused():
    """Without a prefix the aggregator has no way to know whether to hold
    the session, and most of them close it."""
    valid, note = validate_menu_payload("Welcome, press 1 to continue")
    assert not valid
    assert PREFIX_CONTINUE.strip() in note


def test_every_session_states_that_ussd_has_no_delivery_receipt():
    for step in range(len(MENU_TREE)):
        codes = {f.code for f in
                 simulate_ussd_session(SESSION, step, 1.0).findings}
        assert "USD-NORECEIPT" in codes, step


def test_invalid_session_inputs_raise():
    with pytest.raises(ValueError):
        simulate_ussd_session("", 0, 1.0)
    with pytest.raises(ValueError):
        simulate_ussd_session(SESSION, 0, -1.0)
    with pytest.raises(ValueError):
        simulate_ussd_session(SESSION, len(MENU_TREE), 1.0)
    with pytest.raises(ValueError):
        simulate_ussd_session(SESSION, 0, 1.0, elapsed_seconds=-5)


# ---------------------------------------------------------------------------
# SMPP failover
# ---------------------------------------------------------------------------

def test_a_healthy_bind_submits_on_the_primary_at_the_licensed_rate():
    result = simulate_smpp_carrier_failover(4500, PRIMARY_BOUND, True)
    assert result.route == ROUTE_PRIMARY
    assert result.submitting_tps == LICENSED_TPS
    assert result.dlr_at_risk == 0


def test_a_throttle_is_backpressure_and_never_a_failover():
    """Retry logic that treats this as a failed send resubmits the same
    message, and the subscriber gets two one time codes."""
    for state in (PRIMARY_THROTTLED, PRIMARY_QUEUE_FULL):
        result = simulate_smpp_carrier_failover(4500, state, True)
        assert result.route == ROUTE_BACKPRESSURE, state
        assert result.submitting_tps < LICENSED_TPS, state
        assert "SMP-BACKPRESSURE" in {f.code for f in result.findings}, state


def test_a_dead_link_fails_over_only_when_a_secondary_exists():
    for state in (PRIMARY_UNBOUND, PRIMARY_LINK_DEAD):
        with_secondary = simulate_smpp_carrier_failover(4500, state, True)
        without = simulate_smpp_carrier_failover(4500, state, False)
        assert with_secondary.route == ROUTE_SECONDARY, state
        assert without.route == ROUTE_HELD, state
        assert without.submitting_tps == 0, state


def test_the_failover_strands_every_outstanding_delivery_receipt():
    """Identifiers are issued by the carrier that accepted the submit, so
    a receipt for the primary arrives under the primary's identifier."""
    result = simulate_smpp_carrier_failover(4500, PRIMARY_LINK_DEAD, True)
    assert result.dlr_at_risk == 4500
    assert "SMP-DLR" in {f.code for f in result.findings}
    assert result.severity == SEVERITY_CRITICAL


def test_receipts_are_only_at_risk_when_the_route_actually_moved():
    for state, secondary in itertools.product(PRIMARY_STATES, (True, False)):
        result = simulate_smpp_carrier_failover(1000, state, secondary)
        if result.dlr_at_risk:
            assert result.route == ROUTE_SECONDARY, (state, secondary)


def test_a_held_queue_routes_nothing_and_drains_never():
    result = simulate_smpp_carrier_failover(4500, PRIMARY_UNBOUND, False)
    assert not result.routed
    assert result.queued == 4500
    assert result.in_flight == 0
    assert result.drain_seconds == float("inf")
    assert "SMP-HELD" in {f.code for f in result.findings}


def test_the_window_bounds_the_in_flight_count_whatever_the_backlog():
    for pending in (0, 3, 10, 50000):
        result = simulate_smpp_carrier_failover(pending, PRIMARY_BOUND, True)
        assert result.in_flight <= SMPP_WINDOW_SIZE, pending
        assert result.in_flight + result.queued == pending, pending


def test_the_ramp_starts_low_rises_and_stops_at_the_licensed_rate():
    """Dumping a backlog at the licensed rate into a bind the carrier has
    just accepted gets the sender throttled inside a second."""
    ramp = reconnect_ramp()
    rates = [step.tps for step in ramp]
    seconds = [step.at_second for step in ramp]
    assert rates[0] < LICENSED_TPS
    assert rates[-1] == LICENSED_TPS
    assert rates == sorted(rates)
    assert seconds == sorted(seconds)
    assert seconds[0] == 0
    assert all(rate <= LICENSED_TPS for rate in rates)


def test_a_dead_link_always_raises_the_keepalive_finding():
    """A TCP socket stays open long after the far end stops processing, so
    a dead SMPP link looks connected."""
    for secondary in (True, False):
        result = simulate_smpp_carrier_failover(100, PRIMARY_LINK_DEAD,
                                                secondary)
        assert "SMP-ENQUIRE" in {f.code for f in result.findings}, secondary


def test_every_outcome_states_the_idempotency_requirement():
    for state, secondary in itertools.product(PRIMARY_STATES, (True, False)):
        codes = {f.code for f in
                 simulate_smpp_carrier_failover(50, state, secondary)
                 .findings}
        assert "SMP-IDEMPOTENT" in codes, (state, secondary)


def test_invalid_failover_inputs_raise():
    with pytest.raises(ValueError):
        simulate_smpp_carrier_failover(-1, PRIMARY_BOUND, True)
    with pytest.raises(ValueError):
        simulate_smpp_carrier_failover(100, "Probably fine", True)


# ---------------------------------------------------------------------------
# Architecture matrix
# ---------------------------------------------------------------------------

def test_the_matrix_covers_three_stacks_with_no_blank_cell():
    matrix = get_telecom_architecture_matrix()
    assert len(matrix) == 3
    for option in matrix:
        for value in (option.name, option.language,
                      option.protocol_compliance, option.scalability,
                      option.operational_cost, option.choose_when,
                      option.avoid_when, option.edge_cases_owned):
            assert value.strip(), option.name
        assert option.delivery_weeks_low <= option.delivery_weeks_high


def test_all_three_speak_the_same_protocol_so_that_decides_nothing():
    facts = compare_stacks()
    assert facts["all_speak_smpp_34"] is True
    assert all(s.speaks_smpp_34 for s in get_telecom_architecture_matrix())


def test_the_custom_build_is_the_slowest_by_a_wide_margin():
    facts = compare_stacks()
    assert facts["by_delivery"][-1] == STACK_CUSTOM
    assert facts["fastest"] == STACK_KANNEL
    assert facts["custom_multiple"] >= 5


def test_the_custom_build_owns_every_edge_case():
    custom = get_stack(STACK_CUSTOM)
    for edge in ("window", "unbind", "sequence number", "reconnect"):
        assert edge in custom.edge_cases_owned.lower(), edge


def test_an_unknown_stack_raises():
    with pytest.raises(KeyError):
        get_stack("Custom Kannel fork")


# ---------------------------------------------------------------------------
# House invariants
# ---------------------------------------------------------------------------

def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    """The engine has to run outside the app, or it is not an engine."""
    script = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.telecom_cpaas_gateway_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT))
    done = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    for name in ("core.py", "page.py"):
        text = (TOOL_DIR / name).read_text(encoding="utf-8")
        assert "—" not in text, name
        assert "–" not in text, name


def test_the_tool_is_registered_with_a_unique_icon():
    tools = [t for t in all_tools() if not t.key.startswith("synthetic-")]
    keys = [t.key for t in tools]
    assert "telecom-cpaas-gateway-console" in keys
    icons = [t.icon for t in tools]
    assert len(icons) == len(set(icons))
    assert SEVERITY_OK != SEVERITY_CRITICAL
