"""Tests for the AppSec Threat Modeling & Research Console.

The CVSS assertions are not self referential. The weights, the equations and
the qualitative bands come from the published CVSS v3.1 specification, and the
expected scores below were worked from those equations rather than read back
from the engine, so a wrong engine fails rather than agreeing with itself.
Three of them were wrong on the first pass and the engine was right. Every
value in KNOWN_SCORES, and in fact all 2592 base vectors, has since been
checked against an independent implementation of the same specification.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from tools.appsec_threat_modeling_console.core import (
    AC_WEIGHTS,
    AV_WEIGHTS,
    BAND_ORDER,
    CIA_WEIGHTS,
    ENGINE_VERSION,
    METRIC_ORDER,
    PR_WEIGHTS_CHANGED,
    PR_WEIGHTS_UNCHANGED,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    SEVERITY_NONE,
    TARGET_LAYERS,
    UI_WEIGHTS,
    VULN_CLASSES,
    all_metric_combinations,
    analyze_threat_vector,
    base_score,
    capability_rows,
    coverage_summary,
    drifted_categories,
    exploit_likelihood,
    get_security_capability_matrix,
    impact_saturation_rows,
    metric_sensitivity,
    naive_roundup,
    ranked_portfolio,
    rounding_audit,
    roundup,
    scope_is_monotonic,
    sensitivity_rows,
    severity_band,
    translate_to_product_feature,
)

ROOT = Path(__file__).resolve().parents[1]


def vector(text: str) -> dict[str, str]:
    """Parse a CVSS vector string into the mapping the engine takes."""
    parts = text.replace("CVSS:3.1/", "").split("/")
    return dict(part.split(":", 1) for part in parts)


# ---------------------------------------------------------------------------
# Weights, straight from the specification
# ---------------------------------------------------------------------------


def test_attack_vector_weights_match_the_specification():
    assert AV_WEIGHTS == {"N": Decimal("0.85"), "A": Decimal("0.62"),
                          "L": Decimal("0.55"), "P": Decimal("0.2")}


def test_attack_complexity_weights_match_the_specification():
    assert AC_WEIGHTS == {"L": Decimal("0.77"), "H": Decimal("0.44")}


def test_user_interaction_weights_match_the_specification():
    assert UI_WEIGHTS == {"N": Decimal("0.85"), "R": Decimal("0.62")}


def test_impact_weights_match_the_specification():
    assert CIA_WEIGHTS == {"H": Decimal("0.56"), "L": Decimal("0.22"),
                           "N": Decimal("0")}


def test_privileges_required_has_two_tables_selected_by_scope():
    assert PR_WEIGHTS_UNCHANGED == {"N": Decimal("0.85"), "L": Decimal("0.62"),
                                    "H": Decimal("0.27")}
    assert PR_WEIGHTS_CHANGED == {"N": Decimal("0.85"), "L": Decimal("0.68"),
                                  "H": Decimal("0.50")}


def test_scope_changes_only_the_low_and_high_privilege_weights():
    assert PR_WEIGHTS_CHANGED["N"] == PR_WEIGHTS_UNCHANGED["N"]
    assert PR_WEIGHTS_CHANGED["L"] > PR_WEIGHTS_UNCHANGED["L"]
    assert PR_WEIGHTS_CHANGED["H"] > PR_WEIGHTS_UNCHANGED["H"]


def test_every_weight_is_a_decimal_not_a_float():
    for table in (AV_WEIGHTS, AC_WEIGHTS, UI_WEIGHTS, CIA_WEIGHTS,
                  PR_WEIGHTS_UNCHANGED, PR_WEIGHTS_CHANGED):
        for weight in table.values():
            assert isinstance(weight, Decimal)


# ---------------------------------------------------------------------------
# Base scores worked by hand from the equations
# ---------------------------------------------------------------------------

# Each expectation below was computed from the specification equations and not
# read back from the engine.
KNOWN_SCORES = [
    # Worst case: everything open, everything destroyed.
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", "10.0"),
    # Network, no privileges, confidentiality only.
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N", "7.5"),
    # The same, with all three impacts High.
    ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", "9.8"),
    # Physical access, high complexity, high privilege, low impact.
    ("CVSS:3.1/AV:P/AC:H/PR:H/UI:R/S:U/C:L/I:N/A:N", "1.6"),
    # Adjacent, low privilege, unchanged scope, integrity only.
    ("CVSS:3.1/AV:A/AC:L/PR:L/UI:N/S:U/C:N/I:H/A:N", "5.7"),
    # Local, user interaction required, changed scope.
    ("CVSS:3.1/AV:L/AC:L/PR:L/UI:R/S:C/C:H/I:H/A:H", "8.2"),
]


@pytest.mark.parametrize("text,expected", KNOWN_SCORES)
def test_base_scores_match_hand_worked_values(text, expected):
    assert base_score(vector(text)).score == Decimal(expected)


def test_no_impact_at_all_scores_zero():
    breakdown = base_score(vector(
        "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N"))
    assert breakdown.score == Decimal("0.0")
    assert breakdown.band == SEVERITY_NONE


def test_the_score_never_exceeds_ten():
    for metrics in all_metric_combinations():
        assert base_score(metrics).score <= Decimal("10.0")


def test_the_score_is_never_negative():
    for metrics in all_metric_combinations():
        assert base_score(metrics).score >= Decimal("0.0")


def test_the_vector_string_round_trips():
    text = "CVSS:3.1/AV:A/AC:H/PR:L/UI:R/S:C/C:L/I:H/A:N"
    assert base_score(vector(text)).vector == text


def test_a_missing_metric_is_rejected():
    metrics = vector("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")
    del metrics["A"]
    with pytest.raises(ValueError, match="missing CVSS metric A"):
        base_score(metrics)


def test_an_invented_metric_value_is_rejected():
    metrics = vector("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")
    metrics["AV"] = "X"
    with pytest.raises(ValueError, match="not a CVSS v3.1 value"):
        base_score(metrics)


# ---------------------------------------------------------------------------
# Rounding and the qualitative scale
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("4.02", "4.1"), ("4.00", "4.0"), ("0.01", "0.1"),
    ("6.90", "6.9"), ("8.91", "9.0"), ("9.999", "10.0"),
])
def test_roundup_follows_the_specification_examples(raw, expected):
    assert roundup(Decimal(raw)) == Decimal(expected)


def test_roundup_never_lowers_a_value():
    for step in range(0, 1001):
        value = Decimal(step) / Decimal(100)
        assert roundup(value) >= value


@pytest.mark.parametrize("score,band", [
    ("0.0", SEVERITY_NONE), ("0.1", SEVERITY_LOW), ("3.9", SEVERITY_LOW),
    ("4.0", SEVERITY_MEDIUM), ("6.9", SEVERITY_MEDIUM),
    ("7.0", SEVERITY_HIGH), ("8.9", SEVERITY_HIGH),
    ("9.0", SEVERITY_CRITICAL), ("10.0", SEVERITY_CRITICAL),
])
def test_the_qualitative_bands_match_the_published_scale(score, band):
    assert severity_band(Decimal(score)) == band


def test_the_rounding_audit_reports_what_it_measured():
    audit = rounding_audit()
    assert audit.checked == 2592
    assert audit.divergent == len(audit.examples) or audit.divergent > 12
    # This is the measured result, not an aspiration. If a future change to the
    # equations makes the float shortcut unsafe, this test is where it shows.
    assert audit.divergent == 0
    assert audit.clean is True
    assert "measurement" in audit.verdict


def test_the_naive_roundup_is_kept_only_for_comparison():
    assert naive_roundup(4.02) == pytest.approx(4.1)
    assert naive_roundup(4.0) == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# The measured properties of the equations
# ---------------------------------------------------------------------------


def test_the_metric_space_is_the_size_the_specification_implies():
    # 4 x 2 x 3 x 2 x 2 x 3 x 3 x 3
    assert len(list(all_metric_combinations())) == 2592


def test_changing_scope_never_lowers_a_base_score():
    checked, lowered = scope_is_monotonic()
    assert checked == 1296
    assert lowered == 0


def test_scope_changed_is_not_a_fixed_increment():
    """If it were an increment the delta would be the same everywhere."""
    deltas = set()
    for metrics in all_metric_combinations():
        if metrics["S"] != "U" or metrics["PR"] == "N":
            continue
        unchanged = base_score(metrics).score
        if unchanged == 0:
            continue
        deltas.add(base_score({**metrics, "S": "C"}).score - unchanged)
    assert len(deltas) > 1


def test_impact_saturates_rather_than_adding_up():
    rows = impact_saturation_rows()
    assert rows[0]["Times the first row"] == "1.00x"
    assert rows[-1]["Damage"] == "All three"
    multiple = float(rows[-1]["Times the first row"].rstrip("x"))
    assert 1.5 < multiple < 2.0


def test_three_high_impacts_score_less_than_three_times_one():
    one = base_score(vector("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N"))
    three = base_score(vector("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"))
    assert three.impact < 3 * one.impact
    assert three.score < 3 * one.score


def test_every_metric_is_measured_for_sensitivity():
    measured = metric_sensitivity()
    assert {s.metric for s in measured} == set(METRIC_ORDER)
    assert len(measured) == len(METRIC_ORDER)


def test_user_interaction_is_the_least_able_to_move_the_band():
    measured = {s.metric: s for s in metric_sensitivity()}
    assert measured["UI"].max_band_jump == 1
    for metric in ("C", "I", "A"):
        assert measured[metric].max_band_jump == 3
        assert measured[metric].flip_rate > measured["UI"].flip_rate


def test_the_sensitivity_table_is_ordered_by_how_much_it_can_move():
    jumps = [s.max_band_jump for s in metric_sensitivity()]
    assert jumps == sorted(jumps, reverse=True)


def test_sensitivity_counts_every_substitution_once():
    for measured in metric_sensitivity():
        alternatives = {"AV": 3, "AC": 1, "PR": 2, "UI": 1,
                        "S": 1, "C": 2, "I": 2, "A": 2}[measured.metric]
        assert measured.trials == 2592 * alternatives
        assert measured.band_flips <= measured.trials


def test_the_band_order_runs_from_none_to_critical():
    assert BAND_ORDER[0] == SEVERITY_NONE
    assert BAND_ORDER[-1] == SEVERITY_CRITICAL
    assert len(BAND_ORDER) == 5


# ---------------------------------------------------------------------------
# Threat analysis
# ---------------------------------------------------------------------------


def test_every_class_scores_at_every_layer():
    for vuln in VULN_CLASSES:
        for layer in TARGET_LAYERS:
            analysis = analyze_threat_vector(vuln.key, layer.key)
            assert Decimal("0") <= analysis.score <= Decimal("10")
            assert 0 <= analysis.exploit_likelihood <= 100


def test_a_class_can_be_named_by_key_or_by_display_name():
    by_key = analyze_threat_vector("bola", "api")
    by_name = analyze_threat_vector(by_key.vuln.name, by_key.layer.name)
    assert by_key.vector == by_name.vector
    assert by_key.score == by_name.score


def test_an_unknown_class_is_rejected():
    with pytest.raises(ValueError, match="unknown vulnerability type"):
        analyze_threat_vector("heartbleed", "api")


def test_an_unknown_layer_is_rejected():
    with pytest.raises(ValueError, match="unknown target layer"):
        analyze_threat_vector("bola", "mainframe")


def test_the_public_edge_removes_the_privilege_requirement():
    analysis = analyze_threat_vector("bola", "edge")
    assert analysis.breakdown.metrics["PR"] == "N"
    api = analyze_threat_vector("bola", "api")
    assert analysis.score > api.score


def test_the_data_store_lowers_the_score_and_the_likelihood():
    api = analyze_threat_vector("sqli", "api")
    data = analyze_threat_vector("sqli", "data")
    assert data.score < api.score
    assert data.exploit_likelihood < api.exploit_likelihood


def test_a_boundary_crossing_class_is_always_scope_changed():
    for vuln in VULN_CLASSES:
        if not vuln.crosses_boundary:
            continue
        for layer in TARGET_LAYERS:
            analysis = analyze_threat_vector(vuln.key, layer.key)
            assert analysis.breakdown.metrics["S"] == "C"


def test_the_build_pipeline_forces_scope_changed_even_for_local_classes():
    analysis = analyze_threat_vector("sqli", "pipeline")
    assert analysis.vuln.crosses_boundary is False
    assert analysis.breakdown.metrics["S"] == "C"


def test_the_layer_is_what_moves_the_score():
    scores = {layer.key: analyze_threat_vector("bola", layer.key).score
              for layer in TARGET_LAYERS}
    assert len(set(scores.values())) > 1


def test_analysis_is_deterministic():
    first = analyze_threat_vector("ssrf", "api")
    second = analyze_threat_vector("ssrf", "api")
    assert first.vector == second.vector
    assert first.score == second.score
    assert first.exploit_likelihood == second.exploit_likelihood
    assert [f.code for f in first.findings] == [f.code for f in second.findings]


def test_likelihood_is_not_the_score():
    """The two answer different questions and must be free to disagree."""
    pairs = {(analyze_threat_vector(v.key, l.key).score,
              analyze_threat_vector(v.key, l.key).exploit_likelihood)
             for v in VULN_CLASSES for l in TARGET_LAYERS}
    ordered_by_score = sorted(pairs, key=lambda p: p[0])
    ordered_by_likelihood = sorted(pairs, key=lambda p: p[1])
    assert ordered_by_score != ordered_by_likelihood


def test_high_complexity_lowers_the_likelihood():
    vuln = next(v for v in VULN_CLASSES if v.key == "supply_chain")
    layer = next(l for l in TARGET_LAYERS if l.key == "api")
    easy = dict(vuln.metrics)
    easy["AC"] = "L"
    hard = dict(vuln.metrics)
    hard["AC"] = "H"
    assert (exploit_likelihood(vuln, hard, layer)
            < exploit_likelihood(vuln, easy, layer))


def test_likelihood_is_clamped_to_the_stated_range():
    for vuln in VULN_CLASSES:
        for layer in TARGET_LAYERS:
            for av in AV_WEIGHTS:
                metrics = {**vuln.metrics, **layer.overrides, "AV": av}
                value = exploit_likelihood(vuln, metrics, layer)
                assert 0 <= value <= 100


def test_every_analysis_carries_at_least_one_finding():
    for vuln in VULN_CLASSES:
        for layer in TARGET_LAYERS:
            assert analyze_threat_vector(vuln.key, layer.key).findings


def test_the_scope_finding_fires_when_scope_is_changed():
    analysis = analyze_threat_vector("ssrf", "api")
    codes = [f.code for f in analysis.findings]
    assert "APPSEC-SCOPE" in codes


def test_the_saturation_finding_fires_on_three_high_impacts():
    analysis = analyze_threat_vector("sqli", "edge")
    assert [analysis.breakdown.metrics[k] for k in "CIA"] == ["H", "H", "H"]
    assert "APPSEC-SATURATION" in [f.code for f in analysis.findings]


def test_the_inversion_finding_fires_when_severity_and_likelihood_disagree():
    inverted = [analyze_threat_vector(v.key, l.key)
                for v in VULN_CLASSES for l in TARGET_LAYERS
                if analyze_threat_vector(v.key, l.key)
                .severity_likelihood_inversion]
    assert inverted, "no vector exercises the inversion path"
    for analysis in inverted:
        assert "APPSEC-INVERSION" in [f.code for f in analysis.findings]


def test_findings_always_carry_a_fix():
    for vuln in VULN_CLASSES:
        for layer in TARGET_LAYERS:
            for finding in analyze_threat_vector(vuln.key, layer.key).findings:
                assert finding.fix.strip()
                assert finding.detail.strip()
                assert finding.code.startswith("APPSEC-")


# ---------------------------------------------------------------------------
# Product translation
# ---------------------------------------------------------------------------


def test_translation_produces_stories_criteria_and_controls():
    translation = translate_to_product_feature(
        analyze_threat_vector("bola", "api"))
    assert len(translation.stories) == 3
    assert len(translation.criteria) == 3
    assert len(translation.controls) >= 4


def test_translation_rejects_anything_but_an_analysis():
    with pytest.raises(TypeError, match="analyze_threat_vector"):
        translate_to_product_feature({"score": 9.8})


def test_every_class_maps_to_both_frameworks():
    for vuln in VULN_CLASSES:
        translation = translate_to_product_feature(
            analyze_threat_vector(vuln.key, "api"))
        frameworks = {m.framework for m in translation.controls}
        assert any("SOC 2" in f for f in frameworks)
        assert any("ISO" in f for f in frameworks)


def test_control_identifiers_are_never_blank():
    for vuln in VULN_CLASSES:
        translation = translate_to_product_feature(
            analyze_threat_vector(vuln.key, "edge"))
        for mapping in translation.controls:
            assert mapping.control_id.strip()
            assert mapping.control_name.strip()
            assert mapping.why.strip()


def test_story_and_criterion_identifiers_are_unique():
    translation = translate_to_product_feature(
        analyze_threat_vector("stored_xss", "api"))
    idents = [s.ident for s in translation.stories]
    assert len(set(idents)) == len(idents)
    criteria = [c.ident for c in translation.criteria]
    assert len(set(criteria)) == len(criteria)


def test_priority_needs_both_severity_and_likelihood():
    priorities = {
        translate_to_product_feature(
            analyze_threat_vector(v.key, l.key)).priority
        for v in VULN_CLASSES for l in TARGET_LAYERS}
    assert len(priorities) > 1


def test_a_severe_but_unlikely_item_is_not_this_sprint():
    for vuln in VULN_CLASSES:
        for layer in TARGET_LAYERS:
            analysis = analyze_threat_vector(vuln.key, layer.key)
            translation = translate_to_product_feature(analysis)
            if translation.priority == "This sprint":
                assert analysis.exploit_likelihood >= 70
                assert analysis.band in (SEVERITY_HIGH, SEVERITY_CRITICAL)


def test_effort_tracks_the_fix_not_the_score():
    """A cross boundary fix is more work even when it scores lower."""
    local = translate_to_product_feature(analyze_threat_vector("bola", "api"))
    boundary = translate_to_product_feature(
        analyze_threat_vector("ssrf", "api"))
    assert boundary.stories[0].points > local.stories[0].points


def test_the_guardrail_refuses_to_call_a_mapping_a_mitigation():
    translation = translate_to_product_feature(
        analyze_threat_vector("sqli", "api"))
    flat = " ".join(translation.guardrail.split())
    assert "not evidence that the vulnerability is gone" in flat
    assert "acceptance criteria" in flat


# ---------------------------------------------------------------------------
# Capability matrix
# ---------------------------------------------------------------------------


def test_the_matrix_has_exactly_ten_rows():
    assert len(get_security_capability_matrix()) == 10


def test_the_matrix_is_the_2025_edition_in_order():
    rows = get_security_capability_matrix()
    for index, row in enumerate(rows, start=1):
        assert row.category_2025.startswith(f"A{index:02d}:2025")


def test_ssrf_has_no_2025_category_of_its_own():
    rows = get_security_capability_matrix()
    assert not any("Server-Side Request Forgery" in r.category_2025
                   for r in rows)
    absorbed = next(r for r in rows if "Server-Side Request Forgery"
                    in r.category_2021)
    assert absorbed.category_2025.startswith("A01:2025")


def test_the_new_2025_row_has_no_2021_ancestor():
    new = [r for r in get_security_capability_matrix()
           if r.category_2021 == "No 2021 equivalent"]
    assert len(new) == 1
    assert new[0].category_2025.startswith("A10:2025")
    assert new[0].drift == "New in 2025"


def test_the_drifted_rows_are_the_ones_a_migration_would_miss():
    drifted = drifted_categories()
    assert len(drifted) == 3
    assert {r.drift for r in drifted} == {
        "Scope widened", "Absorbed from another category", "New in 2025"}


def test_the_coverage_summary_adds_up_to_the_matrix():
    summary = coverage_summary()
    assert sum(summary.values()) == len(get_security_capability_matrix())


def test_every_matrix_row_names_a_defensive_feature():
    for row in get_security_capability_matrix():
        assert row.feature.strip()
        assert row.note.strip()
        assert row.coverage in ("Covered", "Partial", "Not covered")


def test_every_vulnerability_class_maps_into_the_matrix():
    categories = {r.category_2025 for r in get_security_capability_matrix()}
    for vuln in VULN_CLASSES:
        assert vuln.owasp_2025 in categories


# ---------------------------------------------------------------------------
# Rows reaching the screen must be Arrow safe
# ---------------------------------------------------------------------------


def _assert_all_strings(rows):
    assert rows
    keys = set(rows[0])
    for row in rows:
        assert set(row) == keys, "ragged rows break the Arrow conversion"
        for value in row.values():
            assert isinstance(value, str)


def test_metric_rows_are_arrow_safe():
    _assert_all_strings(analyze_threat_vector("bola", "api").rows())


def test_arithmetic_rows_are_arrow_safe():
    _assert_all_strings(
        analyze_threat_vector("bola", "api").breakdown.arithmetic_rows())


def test_capability_rows_are_arrow_safe():
    _assert_all_strings(capability_rows())


def test_sensitivity_rows_are_arrow_safe():
    _assert_all_strings(sensitivity_rows())


def test_saturation_rows_are_arrow_safe():
    _assert_all_strings(impact_saturation_rows())


def test_ranked_portfolio_rows_are_arrow_safe():
    for layer in TARGET_LAYERS:
        _assert_all_strings(ranked_portfolio(layer.key))


def test_translation_rows_are_arrow_safe():
    translation = translate_to_product_feature(
        analyze_threat_vector("ssrf", "edge"))
    _assert_all_strings(translation.story_rows())
    _assert_all_strings(translation.criteria_rows())
    _assert_all_strings(translation.control_rows())


def test_the_ranked_portfolio_covers_every_class():
    rows = ranked_portfolio("api")
    assert len(rows) == len(VULN_CLASSES)
    assert {r["Vulnerability"] for r in rows} == {v.name for v in VULN_CLASSES}


def test_the_two_rankings_disagree_somewhere():
    moves = [int(r["Moves"]) for r in ranked_portfolio("api")]
    assert any(m != 0 for m in moves), (
        "if the two orders always agreed the second column would be pointless")


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "appsec_threat_modeling_console"
              / "core.py").read_text()
    assert "import streamlit" not in source
    assert "from streamlit" not in source


def test_the_core_is_importable_without_streamlit_installed():
    """A dependency added by accident would only show up out of process."""
    code = (
        "import sys;"
        "sys.modules['streamlit'] = None;"
        "from tools.appsec_threat_modeling_console import core;"
        "print(core.analyze_threat_vector('bola', 'api').score)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "7.1"


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [v.mechanics for v in VULN_CLASSES]
        + [l.note for l in TARGET_LAYERS]
        + [r.note for r in get_security_capability_matrix()]
        + [f.detail + f.fix
           for v in VULN_CLASSES
           for f in analyze_threat_vector(v.key, "api").findings]
        + [translate_to_product_feature(
            analyze_threat_vector(v.key, "api")).guardrail
           for v in VULN_CLASSES]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools

    tools = all_tools()
    entry = next(t for t in tools
                 if t.key == "appsec-threat-modeling-console")
    assert entry.title == "AppSec Threat Modeling & Research Console"
    assert len(entry.tagline) > 30
    icons = [t.icon for t in tools if not t.key.startswith("synthetic-")]
    assert icons.count(entry.icon) == 1
