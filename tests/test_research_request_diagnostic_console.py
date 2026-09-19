"""Tests for the Research Request Diagnostic Console.

Two things are worth proving, and both are about the tool refusing to
produce something that sounds right.

The reframer must report an unmatched request as unmatched. A reframe that
would have been returned whatever was typed is not a reframe, it is filler
with a business vocabulary, and it is exactly the failure this tool argues
against elsewhere.

The methodology scorer must be arithmetic rather than assertion: published
weights that sum to one hundred, totals that equal the sum of their rows, a
recommendation that follows the scores, and an honest admission when the two
methods are too close to separate.

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

from tools.research_request_diagnostic_console.core import (  # noqa: E402
    ARCHETYPES,
    ARCHETYPE_BY_KEY,
    CONFIDENCE_QUALIFIED,
    CONFIDENCE_UNKNOWN,
    DECISIVE_MARGIN,
    DEMOGRAPHICS,
    DIMENSIONS,
    ENGINE_VERSION,
    GOALS,
    GOAL_BY_KEY,
    METHOD_BOTH,
    METHOD_EXISTING,
    METHOD_SURVEY,
    NO_DECISION_MARKERS,
    PIVOTAL_QUESTION,
    REFUSALS,
    REGIONS,
    REGION_NATIONAL,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    TOTAL_WEIGHT,
    VERDICT_DO_NOT_COMMISSION,
    VERDICT_REFRAME_FIRST,
    VERDICT_UNCLASSIFIED,
    apply_us_market_context,
    evaluate_methodology,
    reframe_client_request,
)

FINDING = "62 percent said they would switch for faster delivery"


# ---------------------------------------------------------------------------
# Reframing
# ---------------------------------------------------------------------------


def test_an_unmatched_request_is_reported_as_unmatched():
    """A reframe that fits anything is filler with a business vocabulary."""
    reframe = reframe_client_request(
        "Please analyse the widget frobnication rate for Q3")
    assert not reframe.classified
    assert reframe.archetype_key == ""
    assert reframe.verdict == VERDICT_UNCLASSIFIED
    assert reframe.underlying_problem == ""
    assert reframe.real_decision == ""
    assert any(f.code == "REQ-UNMATCHED" for f in reframe.findings)


def test_an_unmatched_request_still_hands_over_the_one_question_that_matters():
    reframe = reframe_client_request("Something entirely unrelated to research")
    assert reframe.missing_context == (PIVOTAL_QUESTION,)
    assert "do differently" in PIVOTAL_QUESTION


def test_every_archetype_is_reachable_from_a_plain_english_request():
    requests = {
        "churn": "why are we losing customers every month",
        "pricing": "what would people pay for the premium tier",
        "market_size": "we need to size the market for the board",
        "concept_test": "can we test this new feature with customers",
        "brand": "how is our brand perceived against the competition",
        "segmentation": "work out who our ideal customer is",
        "campaign": "why did the campaign underperform",
    }
    assert set(requests) == set(ARCHETYPE_BY_KEY)
    for key, text in requests.items():
        reframe = reframe_client_request(text)
        assert reframe.archetype_key == key, (key, reframe.archetype_key)
        assert reframe.classified


def test_a_request_that_admits_nothing_depends_on_it_is_refused():
    """The pivotal question, applied. Research that changes nothing is an
    invoice with a deck attached."""
    for marker in NO_DECISION_MARKERS:
        reframe = reframe_client_request(f"{marker}, how big is the market")
        assert reframe.verdict == VERDICT_DO_NOT_COMMISSION, marker
        assert reframe.severity == SEVERITY_CRITICAL, marker
        assert any(f.code == "REQ-NO-DECISION" for f in reframe.findings), marker


def test_the_refusal_applies_even_when_the_request_is_unclassified():
    reframe = reframe_client_request("just curious about the frobnication rate")
    assert not reframe.classified
    assert reframe.verdict == VERDICT_DO_NOT_COMMISSION


def test_a_classified_request_with_a_real_decision_is_reframed_not_refused():
    reframe = reframe_client_request("why are customers cancelling")
    assert reframe.verdict == VERDICT_REFRAME_FIRST
    assert reframe.classified
    assert reframe.real_decision.strip()
    assert reframe.underlying_problem.strip()


def test_every_archetype_names_a_decision_and_asks_real_questions():
    for archetype in ARCHETYPES:
        assert len(archetype.real_decision) > 40, archetype.key
        assert len(archetype.underlying_problem) > 40, archetype.key
        assert len(archetype.trap) > 40, archetype.key
        assert len(archetype.missing_context) >= 3, archetype.key
        for question in archetype.missing_context:
            # A question mark somewhere, not necessarily at the end: a
            # question followed by an instruction is still a question, and
            # demanding it end in one would have deleted useful guidance.
            assert "?" in question, (archetype.key, question)


def test_a_trigger_does_not_fire_on_ordinary_english_that_means_nothing_like_it():
    """Found by probing the tool rather than by reading it: "pay more
    attention" was matching the pricing archetype. A reframe that fires on
    the wrong request is the same failure as one that fits every request."""
    innocent = reframe_client_request(
        "should we pay more attention to the onboarding emails")
    assert innocent.archetype_key != "pricing"
    # The phrasing it was widened for must still work.
    assert reframe_client_request(
        "would customers pay more for next day delivery").archetype_key == "pricing"
    assert reframe_client_request(
        "what would people pay for the premium tier").archetype_key == "pricing"


def test_the_archetype_library_has_no_duplicate_keys_or_triggers():
    keys = [a.key for a in ARCHETYPES]
    assert len(keys) == len(set(keys))
    seen: dict = {}
    for archetype in ARCHETYPES:
        for trigger in archetype.triggers:
            assert trigger == trigger.lower(), trigger
            assert trigger not in seen, (trigger, seen.get(trigger))
            seen[trigger] = archetype.key


def test_matching_is_case_and_whitespace_insensitive():
    plain = reframe_client_request("why are we losing customers")
    shouted = reframe_client_request("  WHY ARE WE   LOSING CUSTOMERS  ")
    assert plain.archetype_key == shouted.archetype_key
    assert plain.real_decision == shouted.real_decision


def test_an_empty_request_raises_rather_than_reframing_nothing():
    for bad in ("", "   ", None):
        with pytest.raises(ValueError):
            reframe_client_request(bad)


# ---------------------------------------------------------------------------
# Methodology scoring
# ---------------------------------------------------------------------------


def test_the_published_weights_sum_to_one_hundred():
    assert TOTAL_WEIGHT == 100
    assert sum(weight for _n, weight, _note in DIMENSIONS) == TOTAL_WEIGHT


def test_every_total_equals_the_sum_of_its_own_rows():
    for goal in GOALS:
        verdict = evaluate_methodology(goal.key)
        assert verdict.rows_survey_total == verdict.survey_total, goal.key
        assert verdict.rows_existing_total == verdict.existing_total, goal.key
        assert len(verdict.rows) == len(DIMENSIONS), goal.key


def test_no_score_can_exceed_the_weight_available_to_it():
    for goal in GOALS:
        verdict = evaluate_methodology(goal.key)
        for row in verdict.rows:
            assert 0 <= row.survey_weighted <= row.weight, (goal.key, row.dimension)
            assert 0 <= row.existing_weighted <= row.weight, (goal.key, row.dimension)
        assert 0 <= verdict.survey_total <= TOTAL_WEIGHT, goal.key
        assert 0 <= verdict.existing_total <= TOTAL_WEIGHT, goal.key


def test_the_recommendation_always_follows_the_scores():
    for goal in GOALS:
        verdict = evaluate_methodology(goal.key)
        if verdict.margin < DECISIVE_MARGIN:
            assert verdict.recommendation == METHOD_BOTH, goal.key
            assert not verdict.decisive, goal.key
        elif verdict.existing_total > verdict.survey_total:
            assert verdict.recommendation == METHOD_EXISTING, goal.key
        else:
            assert verdict.recommendation == METHOD_SURVEY, goal.key


def test_the_margin_is_the_distance_between_the_two_totals():
    for goal in GOALS:
        verdict = evaluate_methodology(goal.key)
        assert verdict.margin == pytest.approx(
            abs(verdict.survey_total - verdict.existing_total), abs=0.01)


def test_behavioural_questions_favour_the_data_already_collected():
    """What people do is recorded. What they say they did is a
    reconstruction, and a polite one."""
    for key in ("churn", "campaign", "pricing"):
        verdict = evaluate_methodology(key)
        assert verdict.existing_total > verdict.survey_total, key
        assert verdict.recommendation == METHOD_EXISTING, key


def test_attitudinal_questions_favour_asking_people():
    """No log records why somebody thinks what they think."""
    for key in ("brand", "motivation"):
        verdict = evaluate_methodology(key)
        assert verdict.survey_total > verdict.existing_total, key
        assert verdict.recommendation == METHOD_SURVEY, key


def test_a_close_call_is_admitted_rather_than_decided():
    """Declaring a winner inside the noise is a coin toss with a decimal
    place on it."""
    close = [g for g in GOALS if evaluate_methodology(g.key).margin < DECISIVE_MARGIN]
    assert close, "the library should contain at least one genuinely close goal"
    for goal in close:
        verdict = evaluate_methodology(goal.key)
        assert verdict.recommendation == METHOD_BOTH, goal.key
        assert any(f.code == "METH-CLOSE" for f in verdict.findings), goal.key


def test_the_question_type_dimension_carries_the_most_weight():
    """It decides most cases on its own, so it must be the heaviest row."""
    weights = {name: weight for name, weight, _note in DIMENSIONS}
    assert weights["Fit to the question type"] == max(weights.values())


def test_recommending_existing_data_always_raises_the_instrumentation_point():
    for goal in GOALS:
        verdict = evaluate_methodology(goal.key)
        if verdict.recommendation != METHOD_EXISTING:
            continue
        assert any(f.code == "METH-INSTRUMENT" for f in verdict.findings), goal.key


def test_every_goal_scores_every_dimension():
    for goal in GOALS:
        assert len(goal.survey_scores) == len(DIMENSIONS), goal.key
        assert len(goal.existing_scores) == len(DIMENSIONS), goal.key
        for score in goal.survey_scores + goal.existing_scores:
            assert 0 <= score <= 10, goal.key


def test_a_goal_can_be_named_by_object_key_or_label():
    by_key = evaluate_methodology("churn")
    by_label = evaluate_methodology(GOAL_BY_KEY["churn"].label)
    by_object = evaluate_methodology(GOAL_BY_KEY["churn"])
    assert by_key.survey_total == by_label.survey_total == by_object.survey_total
    assert by_key.recommendation == by_label.recommendation


def test_an_unknown_goal_raises_rather_than_scoring_a_guess():
    for bad in ("", "world peace", "   "):
        with pytest.raises(ValueError):
            evaluate_methodology(bad)


# ---------------------------------------------------------------------------
# US market context
# ---------------------------------------------------------------------------


def test_the_layer_refuses_to_characterise_any_group_and_says_so():
    """The point of the section. A region label and an age band do not
    contain what a group of people thinks."""
    layer = apply_us_market_context(FINDING, "West", "Under 30")
    assert len(layer.refusals) == len(REFUSALS)
    assert any("does not tell you what any group of people thinks"
               in refusal for refusal in layer.refusals)
    assert any(f.code == "CTX-SCOPE" for f in layer.findings)


def test_a_subgroup_read_always_raises_the_base_size_first():
    """The single most common way a finding becomes confidently wrong."""
    for demographic in DEMOGRAPHICS:
        if demographic == "All adults":
            continue
        for region in REGIONS:
            layer = apply_us_market_context(FINDING, region, demographic)
            assert layer.confidence == CONFIDENCE_UNKNOWN, (region, demographic)
            assert any(f.code == "CTX-SUBGROUP" and f.severity == SEVERITY_CRITICAL
                       for f in layer.findings), (region, demographic)


def test_only_a_national_all_adult_figure_is_said_to_travel():
    for region, demographic in combinations(REGIONS, DEMOGRAPHICS):
        layer = apply_us_market_context(FINDING, region, demographic)
        expected = (region == REGION_NATIONAL and demographic == "All adults")
        assert layer.travels_nationally is expected, (region, demographic)


def test_a_regional_figure_says_it_is_regional_in_the_interpretation():
    for region in REGIONS:
        if region == REGION_NATIONAL:
            continue
        layer = apply_us_market_context(FINDING, region, "All adults")
        assert region in layer.interpretation, region
        assert layer.confidence == CONFIDENCE_QUALIFIED, region
        assert any(f.code == "CTX-REGION" for f in layer.findings), region


def test_a_national_average_is_flagged_for_hiding_variance():
    layer = apply_us_market_context(FINDING, REGION_NATIONAL, "All adults")
    assert any(f.code == "CTX-NATIONAL" for f in layer.findings)
    assert layer.travels_nationally


def test_every_combination_raises_at_least_three_questions():
    for region, demographic in combinations(REGIONS, DEMOGRAPHICS):
        layer = apply_us_market_context(FINDING, region, demographic)
        assert len(layer.questions_to_ask) >= 3, (region, demographic)
        for question in layer.questions_to_ask:
            assert question.strip().endswith("?"), question


def test_income_and_urbanicity_cuts_get_their_own_structural_note():
    income = apply_us_market_context(FINDING, "South", "Lower income households")
    assert any("cost of living" in note for note in income.structural_notes)
    urban = apply_us_market_context(FINDING, "South", "Rural")
    assert any("available" in note for note in urban.structural_notes)
    age = apply_us_market_context(FINDING, "South", "Under 30")
    assert any("panel" in note or "mode" in note.lower()
               for note in age.structural_notes)


def test_the_finding_is_carried_through_unchanged():
    layer = apply_us_market_context("  a quoted finding  ", "Midwest",
                                    "All adults")
    assert layer.raw_finding == "a quoted finding"


def test_an_unknown_region_or_demographic_raises():
    with pytest.raises(ValueError):
        apply_us_market_context(FINDING, "Antarctica", "All adults")
    with pytest.raises(ValueError):
        apply_us_market_context(FINDING, "West", "People who like blue")
    with pytest.raises(ValueError):
        apply_us_market_context("", "West", "All adults")


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "research_request_diagnostic_console"
              / "core.py").read_text()
    assert "import streamlit" not in source
    probe = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.research_request_diagnostic_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT)
    )
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [a.surface_reading + a.underlying_problem + a.real_decision + a.trap
         + " ".join(a.missing_context) for a in ARCHETYPES]
        + [f.title + f.detail + f.fix
           for request in ("why are we losing customers", "just curious",
                           "frobnication rate")
           for f in reframe_client_request(request).findings]
        + [note for _n, _w, note in DIMENSIONS]
        + [g.note + g.label for g in GOALS]
        + [f.title + f.detail + f.fix
           for goal in GOALS
           for f in evaluate_methodology(goal.key).findings]
        + [layer.interpretation + " ".join(layer.structural_notes)
           + " ".join(layer.questions_to_ask) + " ".join(layer.refusals)
           + " ".join(f.title + f.detail + f.fix for f in layer.findings)
           for region, demographic in combinations(REGIONS, DEMOGRAPHICS)
           for layer in (apply_us_market_context(FINDING, region, demographic),)]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools
    tools = all_tools()
    entry = next(t for t in tools
                 if t.key == "research-request-diagnostic-console")
    assert entry.title == "Research Request Diagnostic Console"
    icons = [t.icon for t in tools]
    assert icons.count(entry.icon) == 1, "this icon is already used by another tool"
    assert len(entry.tagline) > 30
