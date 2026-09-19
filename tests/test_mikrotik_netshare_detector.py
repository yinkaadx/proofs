"""Tests for the MikroTik NetShare Detection Console.

Three properties carry this file, and all three exist because the cost of a
false positive is a paying customer cut off at a counter.

The MAC address must never move the score. Everything behind a tethering
phone shares that one address, so any weight on it is weight on noise.

One weak signal must never reach a detection, and no combination of weak
signals alone may reach the bar for acting without a person. Those two are
checked across the whole input grid rather than at a chosen point.

Both voucher actions must be idempotent, because a poller that retries after
a timeout must not be able to flip a subscriber's service back and forth.

Pass marker: pytest reports all tests passed, exit 0.
Fail marker: any failure or error line, exit non zero.
"""

from __future__ import annotations

import subprocess
import sys
from itertools import product as combinations
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.mikrotik_netshare_detector.core import (  # noqa: E402
    ACTIONS,
    ACTION_BLOCK,
    ACTION_UNBLOCK,
    API_PORT_PLAIN,
    API_PORT_TLS,
    AUTO_ACTION_THRESHOLD,
    DETECTION_THRESHOLD,
    ENGINE_VERSION,
    NATIVE_TTLS,
    ONE_HOP_TTLS,
    OUTCOME_ALREADY,
    OUTCOME_CHANGED,
    OUTCOME_REJECTED,
    PORT_BREADTH_THRESHOLD,
    POLL_INTERVAL_SECONDS,
    RULE_MULTI_OS,
    RULE_NONE,
    RULE_PORT_BREADTH,
    RULE_TTL,
    SAMPLE_SUBSCRIBERS,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    STATUSES,
    STATUS_ACTIVE,
    STATUS_BLOCKED,
    STRONG_SIGNAL_WEIGHT,
    WEAK_SIGNAL_WEIGHT,
    analyze_tethering_heuristics,
    mac_is_randomised,
    manage_voucher_status,
    simulate_api_polling_cycle,
)

FACTORY_MAC = "A4:83:E7:11:22:33"
RANDOM_MAC = "DA:11:9C:55:66:77"
CLEAN_TTL = 64


# ---------------------------------------------------------------------------
# Detection heuristics
# ---------------------------------------------------------------------------


def test_the_mac_address_never_moves_the_score():
    """Everything behind a tethering phone shares it, so any weight on it is
    weight on noise."""
    macs = (FACTORY_MAC, RANDOM_MAC, "00:11:22:33:44:55", "ff:ff:ff:ff:ff:ff",
            "3C-22-FB-01-02-03")
    for ttl, agents, ports in combinations((64, 63, 127), (0, 2), (10, 300)):
        scores = {analyze_tethering_heuristics(m, ttl, agents, ports).score
                  for m in macs}
        assert len(scores) == 1, (ttl, agents, ports, scores)


def test_a_native_ttl_triggers_no_ttl_signal():
    for ttl in NATIVE_TTLS:
        verdict = analyze_tethering_heuristics(FACTORY_MAC, ttl, 1, 10)
        ttl_signal = next(s for s in verdict.signals if s.rule == RULE_TTL)
        assert not ttl_signal.triggered, ttl
        assert ttl_signal.weight == 0, ttl


def test_one_hop_below_a_native_ttl_is_the_strong_signal():
    for ttl in ONE_HOP_TTLS:
        verdict = analyze_tethering_heuristics(FACTORY_MAC, ttl, 1, 10)
        ttl_signal = next(s for s in verdict.signals if s.rule == RULE_TTL)
        assert ttl_signal.triggered, ttl
        assert ttl_signal.strength == "strong", ttl
        assert ttl_signal.weight == STRONG_SIGNAL_WEIGHT, ttl
        assert verdict.detected, ttl
        assert verdict.rule_triggered == RULE_TTL, ttl


def test_one_weak_signal_alone_never_reaches_a_detection():
    """The rule that keeps the counter argument from happening."""
    only_agents = analyze_tethering_heuristics(FACTORY_MAC, CLEAN_TTL, 3, 10)
    assert only_agents.score == WEAK_SIGNAL_WEIGHT
    assert not only_agents.detected
    only_ports = analyze_tethering_heuristics(FACTORY_MAC, CLEAN_TTL, 1, 400)
    assert only_ports.score == WEAK_SIGNAL_WEIGHT
    assert not only_ports.detected
    assert WEAK_SIGNAL_WEIGHT < DETECTION_THRESHOLD


def test_two_weak_signals_flag_but_can_never_act_alone():
    both = analyze_tethering_heuristics(FACTORY_MAC, CLEAN_TTL, 3, 400)
    assert both.detected
    assert both.score == 2 * WEAK_SIGNAL_WEIGHT
    assert not both.safe_to_auto_block
    assert both.confidence == "MODERATE"
    assert any(f.code == "DET-REVIEW" and f.severity == SEVERITY_CRITICAL
               for f in both.findings)


def test_no_combination_of_weak_signals_can_reach_the_auto_action_bar():
    """Checked by arithmetic over the whole weak space, not by example."""
    weak_rules = 2
    assert weak_rules * WEAK_SIGNAL_WEIGHT < AUTO_ACTION_THRESHOLD
    for agents, ports in combinations((0, 1, 2, 5), (0, 50, 200, 500)):
        verdict = analyze_tethering_heuristics(FACTORY_MAC, CLEAN_TTL,
                                               agents, ports)
        assert not verdict.safe_to_auto_block, (agents, ports)


def test_auto_action_needs_the_strong_signal_plus_something_else():
    alone = analyze_tethering_heuristics(FACTORY_MAC, 63, 1, 10)
    assert alone.detected
    assert not alone.safe_to_auto_block, (
        "the strong signal by itself is still one reading of one packet")
    supported = analyze_tethering_heuristics(FACTORY_MAC, 63, 2, 400)
    assert supported.safe_to_auto_block
    assert supported.confidence == "HIGH"


def test_the_score_always_equals_the_sum_of_its_triggered_rows():
    for ttl, agents, ports in combinations((64, 63, 128, 127, 200),
                                           (0, 1, 2, 4), (0, 100, 200, 450)):
        verdict = analyze_tethering_heuristics(FACTORY_MAC, ttl, agents, ports)
        assert verdict.score == verdict.score_from_signals, (ttl, agents, ports)
        assert len(verdict.triggered_rules) == sum(
            1 for s in verdict.signals if s.triggered)


def test_detection_always_follows_the_threshold():
    for ttl, agents, ports in combinations((64, 63, 127, 200),
                                           (0, 2), (10, 400)):
        verdict = analyze_tethering_heuristics(FACTORY_MAC, ttl, agents, ports)
        assert verdict.detected == (verdict.score >= DETECTION_THRESHOLD)
        assert verdict.safe_to_auto_block == (
            verdict.score >= AUTO_ACTION_THRESHOLD)
        if verdict.safe_to_auto_block:
            assert verdict.detected, "acting without flagging is incoherent"


def test_the_port_breadth_bar_is_where_it_says_it_is():
    under = analyze_tethering_heuristics(FACTORY_MAC, CLEAN_TTL, 1,
                                         PORT_BREADTH_THRESHOLD - 1)
    at = analyze_tethering_heuristics(FACTORY_MAC, CLEAN_TTL, 1,
                                      PORT_BREADTH_THRESHOLD)
    assert under.score == 0
    assert at.score == WEAK_SIGNAL_WEIGHT
    assert at.rule_triggered == RULE_PORT_BREADTH


def test_a_clean_sample_triggers_nothing_and_says_so_honestly():
    clean = analyze_tethering_heuristics(FACTORY_MAC, CLEAN_TTL, 1, 10)
    assert clean.score == 0
    assert not clean.detected
    assert clean.rule_triggered == RULE_NONE
    assert clean.confidence == "NONE"
    assert any(f.code == "DET-CLEAN" for f in clean.findings)
    joined = " ".join(f.fix for f in clean.findings)
    assert "not absence of sharing" in joined


def test_every_verdict_says_the_rules_are_heuristics():
    for ttl in (64, 63, 127):
        verdict = analyze_tethering_heuristics(FACTORY_MAC, ttl, 2, 300)
        assert any(f.code == "DET-HEURISTIC" for f in verdict.findings), ttl
        assert any(f.code == "MAC-NOT-A-SIGNAL" for f in verdict.findings), ttl


def test_every_signal_carries_its_own_caveat():
    verdict = analyze_tethering_heuristics(FACTORY_MAC, 63, 3, 400)
    assert len(verdict.signals) == 3
    for signal in verdict.signals:
        assert len(signal.caveat) > 40, signal.rule
        assert signal.evidence.strip(), signal.rule


def test_a_randomised_mac_is_identified_and_still_scores_nothing():
    assert mac_is_randomised(RANDOM_MAC)
    assert not mac_is_randomised(FACTORY_MAC)
    random_verdict = analyze_tethering_heuristics(RANDOM_MAC, CLEAN_TTL, 1, 10)
    assert random_verdict.mac_is_randomised
    assert random_verdict.score == 0
    assert any(f.code == "MAC-RANDOM" for f in random_verdict.findings)


def test_malformed_inputs_raise_rather_than_scoring_a_guess():
    for bad_mac in ("", "not a mac", "A4:83:E7:11:22", "GG:83:E7:11:22:33"):
        with pytest.raises(ValueError):
            analyze_tethering_heuristics(bad_mac, 64, 1, 10)
    for bad_ttl in (0, -1, 256):
        with pytest.raises(ValueError):
            analyze_tethering_heuristics(FACTORY_MAC, bad_ttl, 1, 10)
    with pytest.raises(ValueError):
        analyze_tethering_heuristics(FACTORY_MAC, 64, -1, 10)
    with pytest.raises(ValueError):
        analyze_tethering_heuristics(FACTORY_MAC, 64, 1, -5)


def test_the_sample_subscribers_cover_every_outcome():
    outcomes = set()
    for _vid, mac, ttl, agents, ports in SAMPLE_SUBSCRIBERS:
        verdict = analyze_tethering_heuristics(mac, ttl, agents, ports)
        outcomes.add((verdict.detected, verdict.safe_to_auto_block))
    assert (False, False) in outcomes
    assert (True, False) in outcomes
    assert (True, True) in outcomes


# ---------------------------------------------------------------------------
# The polling cycle
# ---------------------------------------------------------------------------


def test_the_cycle_closes_everything_it_opens():
    for tls in (True, False):
        cycle = simulate_api_polling_cycle(tls)
        assert cycle.closes_what_it_opens, tls
        assert cycle.sockets_left_open == 0, tls
        assert cycle.stateless, tls


def test_the_cycle_ends_holding_nothing():
    cycle = simulate_api_polling_cycle()
    assert not cycle.steps[-1].holds_socket
    close_index = next(i for i, s in enumerate(cycle.steps) if s.phase == "close")
    for step in cycle.steps[close_index:]:
        assert not step.holds_socket, step.phase


def test_the_phases_happen_in_the_only_order_that_works():
    cycle = simulate_api_polling_cycle()
    order = [step.phase for step in cycle.steps]
    for earlier, later in (("connect", "authenticate"),
                           ("authenticate", "request"),
                           ("request", "read"),
                           ("read", "process"),
                           ("process", "close"),
                           ("close", "sleep")):
        assert order.index(earlier) < order.index(later), (earlier, later)
    assert cycle.steps[0].ordinal == 1
    assert [s.ordinal for s in cycle.steps] == list(range(1, len(cycle.steps) + 1))


def test_the_tls_port_is_used_by_default_and_plaintext_is_flagged():
    secure = simulate_api_polling_cycle(use_tls=True)
    assert secure.port == API_PORT_TLS
    assert any(f.code == "API-TLS" for f in secure.findings)
    plain = simulate_api_polling_cycle(use_tls=False)
    assert plain.port == API_PORT_PLAIN
    assert plain.severity == SEVERITY_CRITICAL
    assert any(f.code == "API-PLAINTEXT" for f in plain.findings)


def test_the_log_has_one_line_per_step():
    for interval in (15, POLL_INTERVAL_SECONDS, 300):
        cycle = simulate_api_polling_cycle(interval_seconds=interval)
        assert len(cycle.log) == len(cycle.steps), interval
        assert str(interval) in " ".join(cycle.log), interval


def test_every_cycle_raises_the_detection_latency_and_the_read_only_account():
    cycle = simulate_api_polling_cycle()
    codes = {f.code for f in cycle.findings}
    assert "API-INTERVAL" in codes
    assert "API-READONLY" in codes
    assert "API-STATELESS" in codes


def test_an_impossible_interval_raises():
    with pytest.raises(ValueError):
        simulate_api_polling_cycle(interval_seconds=0)
    with pytest.raises(ValueError):
        simulate_api_polling_cycle(interval_seconds=-60)
    with pytest.raises(ValueError):
        simulate_api_polling_cycle(interval_seconds=7200)


# ---------------------------------------------------------------------------
# Voucher state
# ---------------------------------------------------------------------------


def test_each_action_reaches_its_own_state_from_either_starting_point():
    for start in STATUSES:
        assert manage_voucher_status("V-1", ACTION_BLOCK, start).status == STATUS_BLOCKED
        assert manage_voucher_status("V-1", ACTION_UNBLOCK, start).status == STATUS_ACTIVE


def test_both_actions_are_idempotent():
    """A poller that retries after a timeout must not flip a subscriber's
    service back and forth."""
    for action in ACTIONS:
        for start in STATUSES:
            first = manage_voucher_status("V-1", action, start)
            second = manage_voucher_status("V-1", action, first.status)
            third = manage_voucher_status("V-1", action, second.status)
            assert second.status == first.status, (action, start)
            assert third.status == first.status, (action, start)
            assert not second.changed, (action, start)
            assert second.outcome == OUTCOME_ALREADY, (action, start)


def test_a_repeat_is_reported_rather_than_raised():
    repeat = manage_voucher_status("V-1", ACTION_BLOCK, STATUS_BLOCKED)
    assert repeat.outcome == OUTCOME_ALREADY
    assert not repeat.changed
    assert any(f.code == "VCH-IDEMPOTENT" for f in repeat.findings)
    # Nothing critical fires, because nobody was cut off by this call. The
    # overall severity is still a warning, because the reminder that the
    # desk must be able to reverse a block is attached to every result on
    # purpose and does not depend on what this particular call did.
    assert not any(f.severity == SEVERITY_CRITICAL for f in repeat.findings)
    real_block = manage_voucher_status("V-1", ACTION_BLOCK, STATUS_ACTIVE)
    assert any(f.severity == SEVERITY_CRITICAL for f in real_block.findings)


def test_a_real_block_is_reported_as_cutting_somebody_off():
    blocked = manage_voucher_status("V-1042", ACTION_BLOCK, STATUS_ACTIVE)
    assert blocked.changed
    assert blocked.status == STATUS_BLOCKED
    assert blocked.outcome == OUTCOME_CHANGED
    assert any(f.code == "VCH-BLOCKED" and f.severity == SEVERITY_CRITICAL
               for f in blocked.findings)


def test_every_result_reminds_the_desk_it_can_reverse_this():
    for action, start in combinations(ACTIONS, STATUSES):
        result = manage_voucher_status("V-1", action, start)
        assert any(f.code == "VCH-DESK" for f in result.findings), (action, start)


def test_the_audit_line_records_both_ends_of_the_change():
    result = manage_voucher_status("V-99", ACTION_BLOCK, STATUS_ACTIVE)
    assert "voucher=V-99" in result.audit_line
    assert "action=block" in result.audit_line
    assert f"from={STATUS_ACTIVE}" in result.audit_line
    assert f"to={STATUS_BLOCKED}" in result.audit_line
    assert "changed=true" in result.audit_line


def test_a_bad_id_action_or_state_is_rejected_and_writes_nothing():
    for bad_id in ("", "  ", "ab", "id with spaces", "x" * 40):
        result = manage_voucher_status(bad_id, ACTION_BLOCK, STATUS_ACTIVE)
        assert result.outcome == OUTCOME_REJECTED, bad_id
        assert not result.changed, bad_id
        assert result.audit_line == "", bad_id
    bad_action = manage_voucher_status("V-1", "delete", STATUS_ACTIVE)
    assert bad_action.outcome == OUTCOME_REJECTED
    assert not bad_action.changed
    bad_state = manage_voucher_status("V-1", ACTION_BLOCK, "Suspended")
    assert bad_state.outcome == OUTCOME_REJECTED
    assert not bad_state.changed


def test_the_action_is_read_without_case_fuss():
    upper = manage_voucher_status("V-1", "BLOCK", STATUS_ACTIVE)
    lower = manage_voucher_status("V-1", "block", STATUS_ACTIVE)
    assert upper.status == lower.status == STATUS_BLOCKED
    assert upper.outcome == lower.outcome == OUTCOME_CHANGED


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"
    assert ONE_HOP_TTLS == tuple(t - 1 for t in NATIVE_TTLS)


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "mikrotik_netshare_detector" / "core.py").read_text()
    assert "import streamlit" not in source
    probe = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.mikrotik_netshare_detector.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT)
    )
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [verdict.headline
         + " ".join(s.evidence + s.caveat + s.rule for s in verdict.signals)
         + " ".join(f.title + f.detail + f.fix for f in verdict.findings)
         for ttl, agents, ports in combinations((64, 63, 200), (1, 3), (10, 400))
         for verdict in (analyze_tethering_heuristics(RANDOM_MAC, ttl, agents,
                                                      ports),)]
        + [cycle.rationale + " ".join(s.detail + s.phase for s in cycle.steps)
           + " ".join(f.title + f.detail + f.fix for f in cycle.findings)
           for tls in (True, False)
           for cycle in (simulate_api_polling_cycle(tls),)]
        + [" ".join(f.title + f.detail + f.fix for f in result.findings)
           for action, start in combinations(ACTIONS, STATUSES)
           for result in (manage_voucher_status("V-1", action, start),)]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools
    tools = all_tools()
    entry = next(t for t in tools if t.key == "mikrotik-netshare-detector")
    assert entry.title == "MikroTik NetShare Detection Console"
    icons = [t.icon for t in tools]
    assert icons.count(entry.icon) == 1, "this icon is already used by another tool"
    assert len(entry.tagline) > 30
