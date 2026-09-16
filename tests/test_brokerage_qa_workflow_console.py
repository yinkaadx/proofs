"""Engine tests for the Real Estate Brokerage QA and Workflow Console.

Written for pytest. Deterministic: nothing in the engine reads the clock or a
random source, and money is held as integer cents throughout.

The central tests are the ones a role matrix cannot pass. A Team Lead may
approve transactions and may not approve their own, and any suite that only
asks the first question passes while separation of duties is absent.

Run: pytest tests/test_brokerage_qa_workflow_console.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.brokerage_qa_workflow_console.core import (  # noqa: E402
    ACTIONS,
    DISCLAIMER,
    ENGINE_VERSION,
    MOD_BROKER_REVIEW,
    MOD_COMMISSIONS,
    MOD_LEADS,
    MOD_MLS,
    MOD_TRANSACTIONS,
    MODULES,
    ROLE_AGENT,
    ROLE_BROKER,
    ROLE_TEAM_LEAD,
    ROLES,
    SEV_BLOCKER,
    STATUS_BLOCKED,
    STATUS_FAIL,
    STATUS_PASS,
    SUPERVISORY_ACTIONS,
    console_summary,
    evaluate_role_permissions,
    generate_marker_io_bug_report,
    get_brokerage_workflows,
    money,
    permission_matrix,
    separation_of_duties_report,
    severity_rank,
    split_commission,
    split_with_cap,
    to_cents,
    workflow_rows,
    workflow_summary,
)

SELF = "AG-4471"
OTHER = "AG-9002"


def check(check_id):
    return next(c for c in get_brokerage_workflows() if c.check_id == check_id)


# ---------------------------------------------------------------------------
# Permissions, pass one: the role matrix
# ---------------------------------------------------------------------------

def test_a_team_lead_may_approve_in_broker_review():
    """The matrix answer, which is true and not sufficient."""
    result = evaluate_role_permissions(ROLE_TEAM_LEAD, MOD_BROKER_REVIEW)
    assert "approve" in result.allowed
    assert result.permits("approve")


def test_an_agent_may_not_approve_anything():
    for module in MODULES:
        result = evaluate_role_permissions(ROLE_AGENT, module)
        assert "approve" not in result.allowed, module
        assert "override" not in result.allowed, module


def test_only_the_broker_may_override():
    for module in MODULES:
        broker = evaluate_role_permissions(ROLE_BROKER, module)
        lead = evaluate_role_permissions(ROLE_TEAM_LEAD, module)
        assert "override" not in lead.allowed, module
        if "override" in broker.allowed:
            assert broker.permits("override")


def test_allowed_and_restricted_partition_every_action():
    for role in ROLES:
        for module in MODULES:
            result = evaluate_role_permissions(role, module)
            assert set(result.allowed) | set(result.restricted) == set(ACTIONS)
            assert not set(result.allowed) & set(result.restricted)


def test_an_unknown_role_or_module_is_refused():
    with pytest.raises(ValueError):
        evaluate_role_permissions("Office Manager", MOD_LEADS)
    with pytest.raises(ValueError):
        evaluate_role_permissions(ROLE_AGENT, "Marketing")


# ---------------------------------------------------------------------------
# Permissions, pass two: separation of duties
# ---------------------------------------------------------------------------

def test_a_team_lead_may_not_approve_their_own_file():
    """The check the matrix cannot make. The role is correct and the actor is
    wrong, so a suite written against the matrix alone passes here."""
    result = evaluate_role_permissions(ROLE_TEAM_LEAD, MOD_BROKER_REVIEW,
                                       SELF, SELF)
    assert result.self_review
    assert "approve" in result.allowed
    assert "approve" in result.self_action_blocked
    assert not result.permits("approve")


def test_the_same_team_lead_may_approve_somebody_else_s_file():
    result = evaluate_role_permissions(ROLE_TEAM_LEAD, MOD_BROKER_REVIEW,
                                       SELF, OTHER)
    assert not result.self_review
    assert result.permits("approve")
    assert result.self_action_blocked == ()


def test_a_managing_broker_is_held_to_the_same_rule():
    """Seniority is not a second person either."""
    result = evaluate_role_permissions(ROLE_BROKER, MOD_BROKER_REVIEW,
                                       SELF, SELF)
    assert not result.permits("approve")
    assert not result.permits("override")


def test_only_supervisory_actions_are_withheld_on_your_own_file():
    """Editing your own lead is fine. Approving your own deal is not."""
    result = evaluate_role_permissions(ROLE_AGENT, MOD_LEADS, SELF, SELF)
    assert result.permits("view")
    assert result.permits("edit")
    assert result.self_action_blocked == ()


def test_every_withheld_action_is_a_supervisory_one():
    for role in ROLES:
        for module in MODULES:
            result = evaluate_role_permissions(role, module, SELF, SELF)
            for action in result.self_action_blocked:
                assert action in SUPERVISORY_ACTIONS, (role, module, action)


def test_the_two_passes_differ_exactly_where_it_matters():
    matrix_only = evaluate_role_permissions(ROLE_TEAM_LEAD, MOD_BROKER_REVIEW)
    own_file = evaluate_role_permissions(ROLE_TEAM_LEAD, MOD_BROKER_REVIEW,
                                         SELF, SELF)
    assert matrix_only.allowed == own_file.allowed
    assert matrix_only.effective != own_file.effective


def test_passing_no_actor_gives_the_matrix_answer_unchanged():
    """So a caller who forgets the second pass gets the permissive result,
    which is why the checkpoint for it is a blocker."""
    result = evaluate_role_permissions(ROLE_TEAM_LEAD, MOD_BROKER_REVIEW)
    assert result.effective == result.allowed
    assert not result.self_review


def test_the_explanation_names_the_actor_and_the_reason():
    result = evaluate_role_permissions(ROLE_TEAM_LEAD, MOD_BROKER_REVIEW,
                                       SELF, SELF)
    assert SELF in result.note
    assert "second role does not make a person a second person" in result.note


def test_the_permission_rows_and_matrix_are_arrow_safe():
    rows = evaluate_role_permissions(ROLE_TEAM_LEAD, MOD_BROKER_REVIEW,
                                     SELF, SELF).rows()
    rows += permission_matrix(MOD_BROKER_REVIEW)
    rows += separation_of_duties_report(SELF)
    for row in rows:
        assert all(isinstance(v, str) for v in row.values()), row


def test_the_separation_report_covers_every_role_and_module():
    rows = separation_of_duties_report(SELF)
    assert len(rows) == len(ROLES) * len(MODULES)
    assert any(row["Withheld"] != "none" for row in rows)


# ---------------------------------------------------------------------------
# Workflow checkpoints
# ---------------------------------------------------------------------------

def test_the_matrix_covers_every_module():
    assert {c.module for c in get_brokerage_workflows()} == set(MODULES)


def test_checkpoints_are_ranked_worst_first():
    ranks = [c.rank for c in get_brokerage_workflows()]
    assert ranks == sorted(ranks)


def test_check_ids_are_unique_and_every_check_states_a_rule():
    checks = get_brokerage_workflows()
    ids = [c.check_id for c in checks]
    assert len(ids) == len(set(ids))
    for c in checks:
        assert len(c.expected) > 30, c.check_id
        assert c.why_it_matters
        assert c.status in (STATUS_PASS, STATUS_FAIL, STATUS_BLOCKED)


def test_both_self_approval_checks_are_blockers_and_both_are_failing():
    for check_id in ("BR-101", "BR-102"):
        entry = check(check_id)
        assert entry.severity == SEV_BLOCKER
        assert entry.status == STATUS_FAIL


def test_a_blocked_check_is_counted_as_an_unknown_not_a_pass():
    """Counting it out of the denominator is how a release is signed off on
    evidence nobody gathered."""
    summary = workflow_summary()
    assert summary["blocked"] >= 1
    assert summary["executed"] < summary["total"]
    assert summary["verified_rate"] == pytest.approx(
        summary["passed"] / summary["total"], abs=1e-6)
    quoted = summary["passed"] / summary["executed"]
    assert summary["verified_rate"] < quoted


def test_open_blockers_are_counted_separately_from_failures():
    summary = workflow_summary()
    assert summary["blockers_open"] == 2
    assert summary["blockers_open"] <= summary["failed"]


def test_severity_rank_puts_an_unknown_value_last():
    assert severity_rank(SEV_BLOCKER) < severity_rank("S4 Minor")
    assert severity_rank("S9 Cosmetic") > severity_rank("S4 Minor")


def test_the_workflow_rows_are_arrow_safe():
    for row in workflow_rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# Commission splits
# ---------------------------------------------------------------------------

def test_money_is_held_as_integer_cents():
    assert to_cents("1000.01") == 100_001
    assert to_cents(0.005) == 1, "half a cent rounds half up, once"
    assert money(100_001) == "$1,000.01"


def test_a_simple_split_reconciles_exactly():
    split = split_commission(9000, (("Agent", 7000), ("Brokerage", 3000)))
    assert split.reconciles
    assert split.total_cents == 900_000
    assert split.legs[0].cents == 630_000


def test_a_split_that_does_not_divide_evenly_still_reconciles():
    """Three equal legs on an odd amount is where independent rounding loses
    a penny."""
    split = split_commission(1000.01, (("A", 3333), ("B", 3333), ("C", 3334)))
    assert split.reconciles
    assert split.total_cents == 100_001
    assert sum(leg.cents for leg in split.legs) == 100_001


@pytest.mark.parametrize("gross", [0.01, 1.03, 999.99, 1000.01, 33_333.33,
                                   987_654.21])
def test_every_awkward_amount_reconciles(gross):
    split = split_commission(gross, (("A", 3333), ("B", 3333), ("C", 3334)))
    assert split.reconciles, gross


def test_the_leftover_cents_go_to_one_named_leg():
    split = split_commission(1000.01, (("A", 3333), ("B", 3333), ("C", 3334)))
    assert split.remainder_assigned_to
    assert split.remainder_assigned_to in {leg.name for leg in split.legs}


def test_the_remainder_can_be_directed_to_a_chosen_leg():
    split = split_commission(1000.01, (("A", 3333), ("B", 3333), ("C", 3334)),
                             remainder_to="A")
    assert split.reconciles
    assert split.remainder_assigned_to == "A"


def test_legs_that_do_not_sum_to_a_hundred_percent_are_refused():
    """Rather than being balanced silently by the rounding."""
    with pytest.raises(ValueError) as caught:
        split_commission(9000, (("Agent", 7000), ("Brokerage", 2000)))
    assert "10000" in str(caught.value)


def test_an_empty_split_is_refused():
    with pytest.raises(ValueError):
        split_commission(9000, ())


def test_a_cap_boundary_inside_the_file_uses_two_rates():
    """The bug this tool exists to show. One rate on the whole amount is
    wrong in somebody's favour and surfaces at year end."""
    capped = split_with_cap(9000, 7000, 10_000, 1200)
    assert capped.reconciles
    assert capped.legs[0].cents == 864_000
    assert capped.legs[1].cents == 36_000


def test_applying_one_rate_throughout_differs_by_a_real_amount():
    capped = split_with_cap(9000, 7000, 10_000, 1200)
    flat = split_commission(9000, (("Agent", 7000), ("Brokerage", 3000)))
    assert capped.legs[0].cents - flat.legs[0].cents == 234_000
    assert money(234_000) == "$2,340.00"


def test_a_file_entirely_below_the_cap_matches_the_flat_split():
    capped = split_with_cap(1000, 7000, 10_000, 5000)
    flat = split_commission(1000, (("Agent", 7000), ("Brokerage", 3000)))
    assert capped.legs[0].cents == flat.legs[0].cents


def test_a_file_entirely_above_the_cap_pays_the_post_cap_rate():
    capped = split_with_cap(9000, 7000, 10_000, 0)
    assert capped.legs[0].cents == 900_000
    assert capped.legs[1].cents == 0
    assert capped.reconciles


def test_a_negative_cap_is_treated_as_exhausted_rather_than_crashing():
    capped = split_with_cap(9000, 7000, 10_000, -500)
    assert capped.reconciles
    assert capped.legs[0].cents == 900_000


@pytest.mark.parametrize("cap", [0, 1, 1200, 4999.99, 9000, 20_000])
def test_the_cap_split_reconciles_at_every_boundary(cap):
    assert split_with_cap(9000, 7000, 10_000, cap).reconciles, cap


def test_the_split_rows_are_arrow_safe():
    rows = split_commission(9000, (("Agent", 7000), ("Brokerage", 3000))).rows()
    rows += split_with_cap(9000, 7000, 10_000, 1200).rows()
    for row in rows:
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# The defect report
# ---------------------------------------------------------------------------

def test_the_report_names_the_role_marker_io_cannot_capture():
    report = generate_marker_io_bug_report("BR-102")
    assert report.role == ROLE_TEAM_LEAD
    assert report.screen
    assert "Marker.io" in report.environment["Captured with"]


def test_the_report_states_expected_and_actual_separately():
    report = generate_marker_io_bug_report("BR-102")
    assert report.expected and report.actual
    assert report.expected != report.actual
    assert len(report.expected) > 40


def test_the_report_states_the_brokerage_consequence_not_just_the_bug():
    report = generate_marker_io_bug_report("BR-102")
    assert "supervision" in report.workflow_impact
    assert "role matrix" in report.workflow_impact


def test_the_commission_report_carries_the_arithmetic():
    report = generate_marker_io_bug_report("BR-202")
    assert "$8,640.00" in report.expected
    assert "$6,300.00" in report.actual
    assert "$2,340.00" in report.workflow_impact


def test_the_markdown_has_every_section_a_developer_needs():
    markdown = generate_marker_io_bug_report("BR-102").as_markdown()
    for heading in ("## Preconditions", "## Steps to reproduce", "## Expected",
                    "## Actual", "## Broker workflow impact",
                    "## Environment"):
        assert heading in markdown, heading
    assert "1. Sign in as" in markdown


def test_every_failing_checkpoint_can_produce_a_report():
    for entry in get_brokerage_workflows():
        if entry.passed:
            continue
        report = generate_marker_io_bug_report(entry.check_id)
        assert report.title and report.expected and report.workflow_impact
        for row in report.rows():
            assert all(isinstance(v, str) for v in row.values()), row


def test_an_unknown_checkpoint_is_refused():
    with pytest.raises(ValueError) as caught:
        generate_marker_io_bug_report("BR-999")
    assert "BR-101" in str(caught.value)


def test_report_generation_is_deterministic():
    assert generate_marker_io_bug_report("BR-102").as_markdown() == \
        generate_marker_io_bug_report("BR-102").as_markdown()


# ---------------------------------------------------------------------------
# Summary and house rules
# ---------------------------------------------------------------------------

def test_the_summary_agrees_with_the_parts_it_counts():
    summary = console_summary()
    assert summary["checks"] == len(get_brokerage_workflows())
    assert summary["roles"] == len(ROLES)
    assert summary["modules"] == len(MODULES)
    assert summary["cap_agent"] == 864_000
    assert summary["cap_reconciles"] is True


def test_it_never_presents_itself_as_legal_advice():
    assert "educational simulator" in DISCLAIMER
    assert "rather than a rule this tool asserts" in DISCLAIMER
    assert "state licensing authority" in DISCLAIMER
    assert "differ by state" in DISCLAIMER


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "brokerage_qa_workflow_console"
              / "core.py").read_text()
    assert "streamlit" not in source


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [DISCLAIMER]
        + [c.expected + c.why_it_matters for c in get_brokerage_workflows()]
        + [evaluate_role_permissions(r, MOD_BROKER_REVIEW, SELF, SELF).note
           for r in ROLES]
        + [generate_marker_io_bug_report(c).workflow_impact
           for c in ("BR-102", "BR-202")])
    assert "—" not in text
    assert "–" not in text
