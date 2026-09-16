"""Tests for the TOH Systems and Commerce Launch Console.

Two things are actually worth proving here. The freshness rules must separate
a tile that is late from a tile that is wrong, because those need different
fixes and only one of them blocks a launch. And the agent guardrails must hold
on every path: exactly one path may send without a person, and the kill switch
must close that path too, or it is not a kill switch.

Pass marker: pytest reports all tests passed, exit 0.
Fail marker: any failure or error line, exit non zero.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.toh_systems_launch_console.core import (  # noqa: E402
    COLOR_CRIT,
    COLOR_OK,
    COLOR_WARN,
    CONDITION_DAMAGED,
    CONDITION_OPENED,
    CONDITION_UNOPENED,
    CONDITION_USED,
    DAMAGE_CLAIM_WINDOW_DAYS,
    ENGINE_VERSION,
    FRESH_WITHIN_MINUTES,
    ITEM_CONDITIONS,
    RETURN_WINDOW_DAYS,
    SAMPLE_CASES,
    SAMPLE_TILES,
    STALE_AFTER_MINUTES,
    STATUS_DIVERGENT,
    STATUS_FRESH,
    STATUS_LAGGING,
    STATUS_STALE,
    dashboard_board,
    evaluate_dashboard_freshness,
    get_system_of_record_map,
    owner_of,
    pilot_supervision_rate,
    simulate_supervised_agent_pilot,
    unowned_entities,
)

# ---------------------------------------------------------------------------
# System of record
# ---------------------------------------------------------------------------


def test_every_named_entity_has_exactly_one_owner():
    rows = get_system_of_record_map()
    names = [row.entity for row in rows]
    assert len(names) == len(set(names)), "an entity is listed twice, so it has two owners"
    for wanted in ("Customer", "Order", "Inventory", "Booking", "Learning", "Support"):
        assert owner_of(wanted) is not None, wanted


def test_no_system_reads_an_entity_it_also_owns():
    """A system in its own reader list is the two writer bug written down."""
    for row in get_system_of_record_map():
        assert row.master_system not in row.readers, row.entity


def test_an_entity_with_no_owner_is_reported_rather_than_guessed():
    assert unowned_entities(("Customer", "Loyalty", "Order")) == ("Loyalty",)
    assert unowned_entities(("Customer",)) == ()


def test_the_lookup_does_not_care_about_case():
    assert owner_of("customer") == owner_of("Customer")
    assert owner_of("  ORDER  ").master_system == "Commerce platform"
    assert owner_of("nothing at all") is None


# ---------------------------------------------------------------------------
# Dashboard freshness
# ---------------------------------------------------------------------------


def test_a_recent_tile_that_agrees_with_its_source_is_fresh():
    reading = evaluate_dashboard_freshness("Orders today", 4, 1_284, 1_284)
    assert reading.status == STATUS_FRESH
    assert reading.alert_color == COLOR_OK
    assert reading.variance == 0
    assert not reading.blocks_launch


def test_the_boundaries_land_on_the_documented_side():
    """The limits are published numbers, so the edges are pinned rather than
    left to whichever comparison operator got typed."""
    assert evaluate_dashboard_freshness("t", FRESH_WITHIN_MINUTES, 10, 10).status == STATUS_FRESH
    assert evaluate_dashboard_freshness("t", FRESH_WITHIN_MINUTES + 1, 10, 10).status == STATUS_LAGGING
    assert evaluate_dashboard_freshness("t", STALE_AFTER_MINUTES, 10, 10).status == STATUS_LAGGING
    assert evaluate_dashboard_freshness("t", STALE_AFTER_MINUTES + 1, 10, 10).status == STATUS_STALE


def test_only_a_stale_or_divergent_tile_blocks_a_launch():
    assert not evaluate_dashboard_freshness("t", 30, 10, 10).blocks_launch
    assert evaluate_dashboard_freshness("t", 300, 10, 10).blocks_launch
    assert evaluate_dashboard_freshness("t", 1, 10, 11).blocks_launch
    assert evaluate_dashboard_freshness("t", 30, 10, 10).alert_color == COLOR_WARN


def test_a_fresh_tile_that_disagrees_with_its_source_is_still_red():
    """The whole point. A number that synced a minute ago and is wrong is the
    one a person acts on, so age never rescues a divergence."""
    reading = evaluate_dashboard_freshness("Open tickets", 0, 148, 151)
    assert reading.status == STATUS_DIVERGENT
    assert reading.alert_color == COLOR_CRIT
    assert reading.variance == 3
    assert reading.blocks_launch


def test_the_variance_is_signed_and_the_percentage_is_not():
    short = evaluate_dashboard_freshness("t", 2, 200, 190)
    assert short.variance == -10
    assert short.variance_pct == 5.0
    long = evaluate_dashboard_freshness("t", 2, 200, 210)
    assert long.variance == 10
    assert long.variance_pct == 5.0


def test_an_empty_source_with_records_on_the_dashboard_is_divergent():
    reading = evaluate_dashboard_freshness("t", 1, 0, 4)
    assert reading.status == STATUS_DIVERGENT
    assert reading.variance_pct == 100.0
    assert evaluate_dashboard_freshness("t", 1, 0, 0).status == STATUS_FRESH


def test_impossible_inputs_raise_rather_than_return_a_soothing_status():
    with pytest.raises(ValueError):
        evaluate_dashboard_freshness("t", -1, 10, 10)
    with pytest.raises(ValueError):
        evaluate_dashboard_freshness("t", 5, -1, 10)
    with pytest.raises(ValueError):
        evaluate_dashboard_freshness("t", 5, 10, -1)


def test_the_board_is_sorted_worst_first_and_loses_no_tile():
    board = dashboard_board(SAMPLE_TILES)
    assert len(board) == len(SAMPLE_TILES)
    rank = {COLOR_CRIT: 0, COLOR_WARN: 1, COLOR_OK: 2}
    ranks = [rank[r.alert_color] for r in board]
    assert ranks == sorted(ranks), "a clear tile is sitting above a blocking one"
    counted = (sum(1 for r in board if r.alert_color == COLOR_CRIT)
               + sum(1 for r in board if r.alert_color == COLOR_WARN)
               + sum(1 for r in board if r.alert_color == COLOR_OK))
    assert counted == len(board), "the three buckets do not add up to the board"


def test_every_reading_carries_a_fix_and_a_headline():
    for reading in dashboard_board(SAMPLE_TILES):
        assert reading.headline.strip()
        assert reading.fix.strip()
        assert reading.source.strip()


# ---------------------------------------------------------------------------
# Supervised agent guardrails
# ---------------------------------------------------------------------------


def test_the_one_automatic_path_is_unopened_inside_the_window():
    decision = simulate_supervised_agent_pilot("TOH-1", 4, CONDITION_UNOPENED)
    assert decision.eligible
    assert decision.auto_send_allowed
    assert not decision.requires_human_approval
    assert decision.sends_without_a_person
    assert decision.policy_code == "CARE-AUTO"


def test_no_other_path_sends_without_a_person():
    """Every combination in the sample space, not three hand picked ones."""
    for days in range(0, 121, 1):
        for condition in ITEM_CONDITIONS:
            decision = simulate_supervised_agent_pilot("TOH-1", days, condition)
            automatic = (condition == CONDITION_UNOPENED
                         and days <= RETURN_WINDOW_DAYS)
            assert decision.sends_without_a_person is automatic, (condition, days)


def test_a_refusal_is_never_sent_by_the_agent():
    for days, condition in ((6, CONDITION_USED), (41, CONDITION_UNOPENED),
                            (200, CONDITION_DAMAGED)):
        decision = simulate_supervised_agent_pilot("TOH-1", days, condition)
        assert not decision.eligible
        assert decision.requires_human_approval
        assert not decision.auto_send_allowed


def test_an_opened_item_inside_the_window_is_eligible_and_still_supervised():
    decision = simulate_supervised_agent_pilot("TOH-1", 9, CONDITION_OPENED)
    assert decision.eligible
    assert decision.requires_human_approval
    assert not decision.auto_send_allowed


def test_a_damage_claim_always_goes_to_a_person_even_when_eligible():
    inside = simulate_supervised_agent_pilot("TOH-1", 2, CONDITION_DAMAGED)
    assert inside.eligible
    assert inside.requires_human_approval
    edge = simulate_supervised_agent_pilot("TOH-1", DAMAGE_CLAIM_WINDOW_DAYS,
                                           CONDITION_DAMAGED)
    assert edge.eligible
    late = simulate_supervised_agent_pilot("TOH-1", DAMAGE_CLAIM_WINDOW_DAYS + 1,
                                           CONDITION_DAMAGED)
    assert not late.eligible


def test_the_return_window_edge_is_the_published_day():
    on_time = simulate_supervised_agent_pilot("TOH-1", RETURN_WINDOW_DAYS,
                                              CONDITION_UNOPENED)
    assert on_time.sends_without_a_person
    late = simulate_supervised_agent_pilot("TOH-1", RETURN_WINDOW_DAYS + 1,
                                           CONDITION_UNOPENED)
    assert not late.eligible
    assert late.policy_code == "CARE-WINDOW"


def test_the_kill_switch_closes_every_path_including_the_automatic_one():
    for days in (0, 4, RETURN_WINDOW_DAYS, 60):
        for condition in ITEM_CONDITIONS:
            decision = simulate_supervised_agent_pilot(
                "TOH-1", days, condition, kill_switch_engaged=True)
            assert decision.kill_switch == "ENGAGED"
            assert decision.requires_human_approval
            assert not decision.auto_send_allowed
            assert not decision.sends_without_a_person
            assert decision.drafted_response.startswith("HELD BY KILL SWITCH")


def test_a_draft_always_exists_so_a_person_has_something_to_approve():
    for days in (0, 15, 45, 120):
        for condition in ITEM_CONDITIONS:
            decision = simulate_supervised_agent_pilot("TOH-9", days, condition)
            assert len(decision.drafted_response) > 60
            assert "TOH-9" in decision.drafted_response
            assert decision.guardrails


def test_an_unknown_condition_or_a_negative_age_raises():
    with pytest.raises(ValueError):
        simulate_supervised_agent_pilot("TOH-1", 4, "slightly damp")
    with pytest.raises(ValueError):
        simulate_supervised_agent_pilot("TOH-1", -1, CONDITION_UNOPENED)


def test_the_supervision_totals_equal_the_rows():
    stats = pilot_supervision_rate(SAMPLE_CASES)
    assert stats["supervised"] + stats["automatic"] == stats["total"]
    assert stats["total"] == len(SAMPLE_CASES)
    assert stats["supervised_pct"] == round(
        stats["supervised"] / stats["total"] * 100, 1)


def test_engaging_the_switch_takes_the_sample_to_full_supervision():
    stats = pilot_supervision_rate(SAMPLE_CASES, kill_switch_engaged=True)
    assert stats["automatic"] == 0
    assert stats["supervised"] == stats["total"]
    assert stats["supervised_pct"] == 100.0


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "toh_systems_launch_console" / "core.py").read_text()
    assert "import streamlit" not in source
    probe = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.toh_systems_launch_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT)
    )
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [r.conflict_rule + r.sync_direction + r.entity
         for r in get_system_of_record_map()]
        + [r.headline + r.fix for r in dashboard_board(SAMPLE_TILES)]
        + [d.drafted_response + d.outcome + " ".join(d.guardrails)
           for days in (0, 9, 41, 200)
           for condition in ITEM_CONDITIONS
           for d in (simulate_supervised_agent_pilot("TOH-1", days, condition),)]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools
    tools = all_tools()
    entry = next(t for t in tools if t.key == "toh-systems-launch-console")
    assert entry.title == "TOH Systems & Commerce Launch Console"
    icons = [t.icon for t in tools]
    assert icons.count(entry.icon) == 1, "this icon is already used by another tool"
    assert len(entry.tagline) > 30
