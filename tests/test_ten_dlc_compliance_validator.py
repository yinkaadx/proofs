"""Engine tests for the 10DLC Campaign Registry Compliance Validator.

Written for pytest. Deterministic throughout: the engine reads no clock and no
random source.

Run: pytest tests/test_ten_dlc_compliance_validator.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.ten_dlc_compliance_validator.core import (  # noqa: E402
    CONSENT_CLAUSES,
    DISQUALIFIED,
    DISQUALIFIER,
    EIN_REGISTRY,
    ENCODING_GSM,
    ENCODING_UCS2,
    GSM_MULTI,
    GSM_SINGLE,
    MANDATORY,
    MESSAGE_BRAND,
    MESSAGE_LENGTH,
    MESSAGE_OPT_OUT,
    MESSAGE_SHAFT,
    MESSAGE_SHORTENER,
    NOT_READY,
    PUBLIC_SHORTENERS,
    READY,
    RECOMMENDED,
    SAMPLE_CONSENT_DISQUALIFIED,
    SAMPLE_CONSENT_GOOD,
    SAMPLE_CONSENT_THIN,
    SAMPLE_MESSAGE_EMOJI,
    SAMPLE_MESSAGE_GOOD,
    SAMPLE_MESSAGE_THIN,
    SHAFT_TERMS,
    UCS2_MULTI,
    UCS2_SINGLE,
    VERDICT_BAD_EIN,
    VERDICT_CASE_ONLY,
    VERDICT_EXACT,
    VERDICT_MISMATCH,
    VERDICT_UNKNOWN_EIN,
    brand_rows,
    check_message,
    consent_fix,
    consent_rows,
    consent_verdict,
    encoding_of,
    match_brand,
    message_units,
    normalise_ein,
    record_by_ein,
    scan_consent,
    segment_count,
)

NORTHWIND = "Northwind Logistics, Inc."
NORTHWIND_EIN = "47-1829304"
BRIGHT = "Bright & Early Coaching LLC"
BRIGHT_EIN = "82-4471903"
HARBOR = "The Harbor Clinic"
HARBOR_EIN = "13-5620991"


def verdict_of(text: str, brand: str = ""):
    return consent_verdict(scan_consent(text, brand))


def clause_finding(text: str, key: str, brand: str = ""):
    for finding in scan_consent(text, brand):
        if finding.clause.key == key:
            return finding
    raise AssertionError(f"no clause keyed {key!r}")


def check_of(report, key: str):
    for check in report.checks:
        if check.key == key:
            return check
    raise AssertionError(f"no check keyed {key!r}")


# ---------------------------------------------------------------------------
# EIN reading
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("typed", ["47-1829304", "471829304", " 47 1829304 ",
                                   "47.1829304"])
def test_an_ein_is_read_however_it_was_typed(typed):
    ok, formatted, _ = normalise_ein(typed)
    assert ok is True
    assert formatted == NORTHWIND_EIN


def test_a_short_ein_is_refused_with_the_digit_count():
    ok, _, message = normalise_ein("4718293")
    assert ok is False
    assert "7" in message


def test_an_empty_ein_is_refused_without_guessing():
    assert normalise_ein("")[0] is False
    assert normalise_ein("no digits here")[0] is False


def test_a_known_ein_finds_its_record_and_an_unknown_one_does_not():
    assert record_by_ein("471829304").legal_name == NORTHWIND
    assert record_by_ein("99-9999999") is None
    assert record_by_ein("nonsense") is None


def test_every_seeded_record_is_internally_consistent():
    for record in EIN_REGISTRY:
        ok, formatted, _ = normalise_ein(record.ein)
        assert ok and formatted == record.ein
        assert record.legal_name.strip() == record.legal_name
        assert len(record.state) == 2


# ---------------------------------------------------------------------------
# Brand identity matching
# ---------------------------------------------------------------------------

def test_a_name_filed_exactly_verifies_on_the_first_attempt():
    match = match_brand(NORTHWIND, NORTHWIND_EIN)
    assert match.verdict == VERDICT_EXACT
    assert match.accepted is True
    assert match.exact is True
    assert match.differences == []


def test_surrounding_whitespace_is_not_a_mismatch():
    assert match_brand(f"   {NORTHWIND}  ", NORTHWIND_EIN).exact is True


def test_capitalisation_alone_is_accepted_but_still_reported():
    match = match_brand(NORTHWIND.upper(), NORTHWIND_EIN)
    assert match.verdict == VERDICT_CASE_ONLY
    assert match.accepted is True
    assert any(d.kind == "Capitalisation" for d in match.differences)


def test_one_missing_full_stop_is_an_instant_rejection():
    """The headline case. It reads the same to a person and the registry
    compares strings."""
    match = match_brand("Northwind Logistics, Inc", NORTHWIND_EIN)
    assert match.verdict == VERDICT_MISMATCH
    assert match.accepted is False
    assert any(d.kind == "Missing punctuation" for d in match.differences)
    assert match.correction == NORTHWIND


def test_extra_punctuation_is_named_as_extra():
    match = match_brand("The Harbor Clinic, Inc.", HARBOR_EIN)
    assert match.accepted is False
    assert any("suffix" in d.kind.lower() or "punctuation" in d.kind.lower()
               for d in match.differences)


def test_a_trade_name_without_the_entity_suffix_is_caught():
    match = match_brand("Northwind Logistics", NORTHWIND_EIN)
    assert match.accepted is False
    assert any(d.kind == "Missing entity suffix" for d in match.differences)


def test_the_wrong_entity_suffix_is_caught():
    match = match_brand("Northwind Logistics, LLC", NORTHWIND_EIN)
    assert match.accepted is False
    assert any(d.kind == "Wrong entity suffix" for d in match.differences)


def test_spelling_out_an_ampersand_is_caught():
    match = match_brand("Bright and Early Coaching LLC", BRIGHT_EIN)
    assert match.accepted is False
    assert any("mpersand" in d.kind for d in match.differences)


def test_using_an_ampersand_where_the_record_spells_it_out_is_caught():
    match = match_brand("Northwind & Logistics, Inc.", NORTHWIND_EIN)
    assert match.accepted is False


def test_dropping_a_leading_the_is_caught():
    match = match_brand("Harbor Clinic", HARBOR_EIN)
    assert match.accepted is False
    assert any(d.kind == "Missing leading The" for d in match.differences)


def test_an_entirely_different_name_says_so_plainly():
    match = match_brand("Some Other Company LLC", NORTHWIND_EIN)
    assert match.accepted is False
    assert any(d.kind == "Different name" for d in match.differences)


def test_every_difference_carries_a_fix_that_names_the_filed_string():
    match = match_brand("Northwind Logistics", NORTHWIND_EIN)
    assert match.differences
    for difference in match.differences:
        assert difference.fix
        assert NORTHWIND in difference.fix


def test_an_unknown_ein_is_refused_before_the_name_is_even_compared():
    match = match_brand(NORTHWIND, "99-9999999")
    assert match.verdict == VERDICT_UNKNOWN_EIN
    assert match.accepted is False
    assert match.differences == []


def test_a_malformed_ein_is_refused_before_submission():
    match = match_brand(NORTHWIND, "123")
    assert match.verdict == VERDICT_BAD_EIN
    assert match.accepted is False
    assert match.correction == ""


def test_the_submission_table_shows_both_names_side_by_side():
    rows = {row["Field"]: row["Value"]
            for row in brand_rows(match_brand("Northwind Logistics",
                                              NORTHWIND_EIN))}
    assert rows["Legal name submitted"] == "Northwind Logistics"
    assert rows["Legal name on the IRS record"] == NORTHWIND
    assert rows["Verdict"] == VERDICT_MISMATCH


def test_the_brand_tables_are_arrow_safe():
    for name, ein in ((NORTHWIND, NORTHWIND_EIN), ("Wrong", "123")):
        match = match_brand(name, ein)
        for rows in (brand_rows(match), match.difference_rows()):
            for column in {key for row in rows for key in row}:
                kinds = {type(row[column]).__name__ for row in rows}
                assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Consent scanning
# ---------------------------------------------------------------------------

def test_a_compliant_page_passes_every_mandatory_clause():
    verdict = verdict_of(SAMPLE_CONSENT_GOOD, NORTHWIND)
    assert verdict.headline == READY
    assert verdict.ready is True
    assert verdict.missing == []
    assert verdict.percent == 100


def test_a_thin_page_names_every_clause_it_is_missing():
    verdict = verdict_of(SAMPLE_CONSENT_THIN, NORTHWIND)
    assert verdict.headline == NOT_READY
    missing = {clause.key for clause in verdict.missing}
    assert {"rates", "opt_out", "help", "frequency"} <= missing


def test_an_empty_page_fails_everything_rather_than_passing_by_default():
    verdict = verdict_of("", NORTHWIND)
    assert verdict.passing_mandatory == 0
    assert verdict.ready is False


@pytest.mark.parametrize("wording", [
    "Message and data rates may apply.",
    "Msg & data rates may apply",
    "msg and data rates may apply",
])
def test_the_rates_clause_is_found_in_the_forms_people_actually_write(wording):
    assert clause_finding(wording, "rates").present is True


def test_a_paraphrased_rates_clause_is_not_accepted():
    """The disclosure is checked for by name, so a paraphrase is a rejection."""
    assert clause_finding("Standard carrier charges might be incurred.",
                          "rates").present is False


@pytest.mark.parametrize("wording", ["Reply STOP to cancel", "Text STOP to opt out",
                                     "reply stop at any time"])
def test_the_opt_out_clause_is_found_in_its_usual_forms(wording):
    assert clause_finding(wording, "opt_out").present is True


def test_unsubscribe_prose_alone_is_not_an_opt_out_instruction():
    """The person has to be told the keyword, not merely that stopping is
    possible."""
    assert clause_finding("You can unsubscribe whenever you like.",
                          "opt_out").present is False


@pytest.mark.parametrize("wording", ["Message frequency varies",
                                     "Up to 4 msgs per month",
                                     "recurring messages"])
def test_frequency_is_found_however_it_is_expressed(wording):
    assert clause_finding(wording, "frequency").present is True


def test_naming_a_privacy_policy_without_linking_to_one_does_not_count():
    """The reviewer follows the link, so a page that only says the words fails
    the same clause."""
    assert clause_finding("See our Privacy Policy for details.",
                          "privacy_policy").present is False
    assert clause_finding("See our Privacy Policy at example.com/privacy.",
                          "privacy_policy").present is True


def test_the_page_has_to_name_the_brand_being_registered():
    assert clause_finding(SAMPLE_CONSENT_GOOD, "brand_name",
                          NORTHWIND).present is True


def test_a_page_naming_a_different_company_fails_the_brand_clause():
    """A consent page for one company does not register a campaign for
    another."""
    finding = clause_finding(SAMPLE_CONSENT_GOOD, "brand_name",
                             "Halcyon Data Systems Corporation")
    assert finding.present is False
    assert "brand_name" in {c.key for c in
                            verdict_of(SAMPLE_CONSENT_GOOD,
                                       "Halcyon Data Systems Corporation").missing}


def test_a_brand_is_matched_on_its_distinctive_word_not_its_suffix():
    """Matching on LLC would find a brand that is not on the page at all."""
    assert clause_finding("You agree to receive texts from Acme LLC.",
                          "brand_name", "Northwind Logistics, Inc.").present is False


def test_sharing_language_disqualifies_an_otherwise_perfect_page():
    verdict = verdict_of(SAMPLE_CONSENT_DISQUALIFIED, NORTHWIND)
    assert verdict.headline == DISQUALIFIED
    assert verdict.ready is False
    assert {clause.key for clause in verdict.disqualifiers} == {
        "third_party_sharing"}


def test_a_pre_checked_box_disqualifies_a_page():
    text = SAMPLE_CONSENT_GOOD + " The box is pre-checked for your convenience."
    verdict = verdict_of(text, NORTHWIND)
    assert verdict.headline == DISQUALIFIED
    assert "pre_checked" in {clause.key for clause in verdict.disqualifiers}


def test_a_disqualifier_passes_by_being_absent():
    findings = {f.clause.key: f for f in scan_consent(SAMPLE_CONSENT_GOOD,
                                                      NORTHWIND)}
    sharing = findings["third_party_sharing"]
    assert sharing.present is False
    assert sharing.passing is True


def test_the_fix_block_lists_only_what_is_missing():
    fix = consent_fix(scan_consent(SAMPLE_CONSENT_THIN, NORTHWIND))
    assert "Message and data rates may apply." in fix
    assert "Reply STOP to cancel at any time." in fix


def test_the_fix_block_is_empty_when_the_page_is_complete():
    assert "Nothing to add" in consent_fix(scan_consent(SAMPLE_CONSENT_GOOD,
                                                        NORTHWIND))


def test_the_fix_block_calls_out_what_has_to_be_removed():
    fix = consent_fix(scan_consent(SAMPLE_CONSENT_DISQUALIFIED, NORTHWIND))
    assert "Remove" in fix


def test_the_wording_a_clause_suggests_actually_satisfies_that_clause():
    """Otherwise the tool would hand back advice that does not work."""
    for clause in CONSENT_CLAUSES:
        if clause.level == DISQUALIFIER:
            continue
        assert clause_finding(clause.wording, clause.key,
                              NORTHWIND).present is True, clause.key


def test_pasting_every_suggested_sentence_produces_a_page_that_passes():
    page = " ".join(clause.wording for clause in CONSENT_CLAUSES
                    if clause.level != DISQUALIFIER)
    assert verdict_of(page, NORTHWIND).ready is True


def test_every_clause_carries_a_level_a_reason_and_wording():
    for clause in CONSENT_CLAUSES:
        assert clause.level in (MANDATORY, RECOMMENDED, DISQUALIFIER)
        assert len(clause.why) > 30
        assert clause.wording


def test_the_consent_table_is_arrow_safe():
    rows = consent_rows(scan_consent(SAMPLE_CONSENT_THIN, NORTHWIND))
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Message encoding and segments
# ---------------------------------------------------------------------------

def test_plain_text_is_gsm_and_one_emoji_moves_the_whole_message():
    assert encoding_of("Reply STOP to cancel.") == ENCODING_GSM
    assert encoding_of("Your order is out \U0001F69A") == ENCODING_UCS2


def test_a_curly_quote_pasted_from_a_document_also_forces_ucs2():
    """The one nobody expects: the text looks identical on screen."""
    assert encoding_of("Your order’s on the way") == ENCODING_UCS2


def test_gsm_extension_characters_cost_two_septets():
    assert message_units("abc") == 3
    assert message_units("a{b}") == 6       # two braces at two each


def test_a_surrogate_pair_counts_as_two_units():
    assert message_units("\U0001F69A") == 2


@pytest.mark.parametrize("length,expected", [
    (1, 1), (GSM_SINGLE, 1), (GSM_SINGLE + 1, 2), (GSM_MULTI * 2, 2),
    (GSM_MULTI * 2 + 1, 3),
])
def test_gsm_segments_turn_where_the_specification_says(length, expected):
    assert segment_count("a" * length) == expected


@pytest.mark.parametrize("length,expected", [
    (1, 1), (UCS2_SINGLE, 1), (UCS2_SINGLE + 1, 2), (UCS2_MULTI * 2, 2),
])
def test_ucs2_segments_turn_where_the_specification_says(length, expected):
    # A character outside the GSM alphabet entirely. An accented Latin letter
    # would not do: several of those are in the GSM set and stay 7 bit, which
    # is what this fixture got wrong the first time.
    assert segment_count("Ж" * length) == expected


def test_an_empty_message_is_zero_segments_rather_than_one():
    assert segment_count("") == 0
    assert message_units("") == 0


# ---------------------------------------------------------------------------
# Sample message validation
# ---------------------------------------------------------------------------

def test_a_compliant_sample_passes_every_mandatory_check():
    report = check_message(SAMPLE_MESSAGE_GOOD, "Northwind Logistics, Inc.")
    assert report.ready is True
    assert report.segments == 1
    assert report.corrected == report.text


def test_a_message_with_no_opt_out_fails_on_the_first_message():
    report = check_message("Northwind Logistics: your order shipped.",
                           "Northwind Logistics", first_message=True)
    assert report.ready is False
    assert check_of(report, MESSAGE_OPT_OUT).passing is False
    assert check_of(report, MESSAGE_OPT_OUT).level == MANDATORY


def test_the_opt_out_is_only_recommended_on_a_later_message():
    report = check_message("Northwind Logistics: your order shipped.",
                           "Northwind Logistics", first_message=False)
    assert check_of(report, MESSAGE_OPT_OUT).level == RECOMMENDED
    assert report.ready is True


def test_a_message_that_never_names_the_sender_fails():
    report = check_message("Your order shipped. Reply STOP to cancel.",
                           "Northwind Logistics")
    assert check_of(report, MESSAGE_BRAND).passing is False
    assert report.ready is False


@pytest.mark.parametrize("shortener", PUBLIC_SHORTENERS)
def test_every_public_shortener_is_caught(shortener):
    report = check_message(
        f"Northwind Logistics: track it at {shortener}/abc. Reply STOP to "
        f"cancel.", "Northwind Logistics")
    assert check_of(report, MESSAGE_SHORTENER).passing is False
    assert report.ready is False


def test_a_branded_link_is_not_flagged():
    report = check_message(
        "Northwind Logistics: track at nwlogistics.com/t/1. Reply STOP to "
        "cancel.", "Northwind Logistics")
    assert check_of(report, MESSAGE_SHORTENER).passing is True


@pytest.mark.parametrize("term", SHAFT_TERMS)
def test_restricted_content_is_caught_as_a_different_use_case(term):
    report = check_message(
        f"Northwind Logistics: your {term} order shipped. Reply STOP to "
        f"cancel.", "Northwind Logistics")
    assert check_of(report, MESSAGE_SHAFT).passing is False


def test_a_restricted_term_inside_another_word_does_not_fire():
    """Whole words only, or Hemphill Road becomes a restricted campaign."""
    report = check_message(
        "Northwind Logistics: delivery to Hemphill Road. Reply STOP to cancel.",
        "Northwind Logistics")
    assert check_of(report, MESSAGE_SHAFT).passing is True


def test_the_corrected_message_adds_exactly_what_was_missing():
    report = check_message("Your order shipped.", "Northwind Logistics")
    assert "Reply STOP to cancel." in report.corrected
    assert report.corrected.startswith("Northwind Logistics:")


def test_a_correction_that_adds_a_segment_is_reported_as_such():
    """Adding the opt out is not free: it can double what every send costs."""
    body = "Northwind Logistics: " + "a" * 129      # 150 of the 160
    report = check_message(body, "Northwind Logistics")
    assert report.segments == 1
    assert report.corrected_segments == 2


def test_a_correction_that_fits_does_not_add_a_segment():
    report = check_message("Northwind Logistics: your order shipped.",
                           "Northwind Logistics")
    assert report.corrected_segments == report.segments == 1


def test_the_emoji_sample_is_billed_as_two_segments():
    report = check_message(SAMPLE_MESSAGE_EMOJI, "Northwind Logistics")
    assert report.encoding == ENCODING_UCS2
    assert report.segments == 2
    assert check_of(report, MESSAGE_LENGTH).passing is False


def test_the_thin_sample_fails_on_several_counts_at_once():
    report = check_message(SAMPLE_MESSAGE_THIN, "Northwind Logistics")
    failing = {check.key for check in report.failing}
    assert {MESSAGE_OPT_OUT, MESSAGE_BRAND, MESSAGE_SHORTENER} <= failing


def test_every_failing_check_carries_a_fix():
    report = check_message(SAMPLE_MESSAGE_THIN, "Northwind Logistics")
    assert report.failing
    for check in report.failing:
        assert check.fix
        assert check.detail


def test_an_empty_message_is_not_reported_as_ready():
    assert check_message("", "Northwind Logistics").ready is False


def test_the_message_table_is_arrow_safe():
    rows = check_message(SAMPLE_MESSAGE_THIN, "Northwind Logistics").rows()
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# The three checks agree with each other
# ---------------------------------------------------------------------------

def test_one_brand_carries_through_all_three_checks():
    """All three name the same company, or the registration fails on the one
    that does not."""
    match = match_brand(NORTHWIND, NORTHWIND_EIN)
    brand = match.record.legal_name
    assert match.accepted is True
    assert verdict_of(SAMPLE_CONSENT_GOOD, brand).ready is True
    assert check_message(SAMPLE_MESSAGE_GOOD, brand).ready is True


def test_a_page_and_a_message_for_a_different_brand_both_fail():
    other = "Halcyon Data Systems Corporation"
    assert verdict_of(SAMPLE_CONSENT_GOOD, other).ready is False
    assert check_message(SAMPLE_MESSAGE_GOOD, other).ready is False


# ---------------------------------------------------------------------------
# Prose discipline
# ---------------------------------------------------------------------------

def test_no_dash_characters_in_any_user_facing_prose():
    parts: list[str] = []
    for clause in CONSENT_CLAUSES:
        parts += [clause.label, clause.why, clause.wording]
    for name, ein in ((NORTHWIND, NORTHWIND_EIN), ("Northwind Logistics", NORTHWIND_EIN),
                      ("Bright and Early Coaching LLC", BRIGHT_EIN),
                      (NORTHWIND, "99-9999999"), (NORTHWIND, "12")):
        match = match_brand(name, ein)
        parts += [match.verdict, match.explanation]
        parts += [d.kind + d.detail + d.fix for d in match.differences]
    for text in (SAMPLE_CONSENT_GOOD, SAMPLE_CONSENT_THIN,
                 SAMPLE_CONSENT_DISQUALIFIED):
        parts.append(verdict_of(text, NORTHWIND).summary)
        parts.append(consent_fix(scan_consent(text, NORTHWIND)))
    for text in (SAMPLE_MESSAGE_GOOD, SAMPLE_MESSAGE_THIN, SAMPLE_MESSAGE_EMOJI):
        report = check_message(text, NORTHWIND)
        parts += [check.detail + check.fix for check in report.checks]
    prose = "\n".join(parts)
    assert "—" not in prose
    assert "–" not in prose
