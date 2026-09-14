"""Page tests for the 10DLC Campaign Registry Compliance Validator, via AppTest.

Written for pytest.

Run: pytest tests/test_ten_dlc_compliance_validator_page.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.ten_dlc_compliance_validator.core import (  # noqa: E402
    DISQUALIFIED,
    EIN_REGISTRY,
    NOT_READY,
    READY,
    SAMPLE_CONSENT_GOOD,
    SAMPLE_CONSENT_THIN,
    VERDICT_CASE_ONLY,
    VERDICT_EXACT,
    VERDICT_MISMATCH,
    VERDICT_UNKNOWN_EIN,
)
from tools.ten_dlc_compliance_validator.page import (  # noqa: E402
    BRAND_PRESETS,
    CONSENT_PRESETS,
    MESSAGE_PRESETS,
)

HARNESS = (Path(__file__).resolve().parent
           / "_page_harness_ten_dlc_compliance_validator.py")

NORTHWIND = "Northwind Logistics, Inc."
NORTHWIND_EIN = "47-1829304"


def text_of(at: AppTest) -> str:
    parts: list[str] = []
    for attr in ("markdown", "text", "info", "success", "warning", "error",
                 "caption", "code", "header", "subheader", "title"):
        try:
            parts += [str(getattr(el, "value", "")) for el in getattr(at, attr)]
        except Exception:
            pass
    try:
        for frame in at.get("dataframe"):
            value = getattr(frame, "value", None)
            if value is not None and hasattr(value, "to_numpy"):
                parts += [str(cell) for cell in value.to_numpy().ravel()]
                parts += [str(col) for col in value.columns]
    except Exception:
        pass
    return "\n".join(parts)


def run_app(timeout: int = 180) -> AppTest:
    at = AppTest.from_file(str(HARNESS), default_timeout=timeout)
    at.run()
    return at


def widget(at: AppTest, kind: str, label: str):
    for el in getattr(at, kind):
        if el.label == label:
            return el
    raise AssertionError(f"no {kind} labelled {label!r}; "
                         f"have {[el.label for el in getattr(at, kind)]}")


def brand(name: str, ein: str = NORTHWIND_EIN,
          at: AppTest | None = None) -> AppTest:
    at = at or run_app()
    widget(at, "text_input", "Legal business name").set_value(name).run()
    widget(at, "text_input", "EIN").set_value(ein).run()
    return at


def consent(text: str, at: AppTest | None = None) -> AppTest:
    at = at or run_app()
    widget(at, "text_area", "Consent page wording").set_value(text).run()
    return at


def message(text: str, at: AppTest | None = None) -> AppTest:
    at = at or run_app()
    widget(at, "text_area", "Sample message").set_value(text).run()
    return at


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------

def test_the_page_renders_with_no_exception():
    at = run_app()
    assert not at.exception, [str(e.value) for e in at.exception]


def test_the_hero_and_the_three_tabs_are_present():
    at = run_app()
    assert "10DLC Campaign Registry Compliance Validator" in text_of(at)
    labels = [tab.label for tab in at.tabs]
    for expected in ("Brand Identity", "Consent Scanner", "Sample Message"):
        assert expected in labels


def test_no_two_widgets_of_a_kind_share_a_label():
    """Three selectors on one page all reading Load an example is ambiguous to
    a screen reader and to anyone reading the transcript."""
    at = run_app()
    for kind in ("selectbox", "text_input", "text_area", "checkbox"):
        labels = [el.label for el in getattr(at, kind)]
        assert len(labels) == len(set(labels)), f"{kind}: {labels}"


def test_it_says_plainly_that_no_submission_is_made():
    body = text_of(run_app())
    assert "no submission is made" in body
    assert "simulated" in body


# ---------------------------------------------------------------------------
# Brand identity matcher
# ---------------------------------------------------------------------------

def test_the_page_opens_on_the_near_miss_that_gets_registrations_rejected():
    body = text_of(run_app())
    assert VERDICT_MISMATCH in body
    assert "Missing punctuation" in body


def test_the_exact_name_verifies():
    body = text_of(brand(NORTHWIND))
    assert VERDICT_EXACT in body
    assert "Verified" in body


def test_capitalisation_alone_is_accepted_on_screen():
    body = text_of(brand(NORTHWIND.upper()))
    assert VERDICT_CASE_ONLY in body
    assert "Verified" in body


def test_a_rejected_name_offers_the_exact_string_to_paste():
    at = brand("Northwind Logistics")
    body = text_of(at)
    assert "Paste this into the brand form" in body
    assert NORTHWIND in body
    assert "Missing entity suffix" in body


def test_an_exact_match_offers_nothing_to_paste():
    assert "Paste this into the brand form" not in text_of(brand(NORTHWIND))


def test_an_unknown_ein_is_rejected_before_the_name_is_compared():
    body = text_of(brand(NORTHWIND, "99-9999999"))
    assert VERDICT_UNKNOWN_EIN in body
    assert "vetting fee is spent" in body


def test_a_malformed_ein_is_refused_with_the_digit_count():
    body = text_of(brand(NORTHWIND, "4718"))
    assert "Refused before submission" in body
    assert "commonest reason" in body


@pytest.mark.parametrize("preset", list(BRAND_PRESETS))
def test_every_brand_preset_loads_into_both_fields(preset):
    """Including the one the page opens on: the fields it was seeded with have
    to be the ones the selector claims, or the page opens on a small lie."""
    at = run_app()
    widget(at, "selectbox", "Load a brand example").set_value(preset).run()
    name, ein = BRAND_PRESETS[preset]
    assert widget(at, "text_input", "Legal business name").value == name
    assert widget(at, "text_input", "EIN").value == ein
    assert not at.exception, [str(e.value) for e in at.exception]


def test_the_simulated_registry_is_shown_so_the_examples_can_be_checked():
    body = text_of(run_app())
    for record in EIN_REGISTRY:
        assert record.ein in body
        assert record.legal_name in body


# ---------------------------------------------------------------------------
# Consent scanner
# ---------------------------------------------------------------------------

def test_a_compliant_page_reads_as_ready():
    at = brand(NORTHWIND)
    body = text_of(consent(SAMPLE_CONSENT_GOOD, at))
    assert READY in body
    assert "Nothing to add" in body


def test_a_thin_page_names_the_clauses_it_is_missing():
    at = brand(NORTHWIND)
    body = text_of(consent(SAMPLE_CONSENT_THIN, at))
    assert NOT_READY in body
    assert "Message and data rates may apply." in body
    assert "Reply STOP to cancel at any time." in body


def test_sharing_language_disqualifies_the_page_on_screen():
    at = brand(NORTHWIND)
    at = consent(SAMPLE_CONSENT_GOOD + " We may share your information with "
                 "our marketing partners.", at)
    body = text_of(at)
    assert DISQUALIFIED in body
    assert "Remove the sharing language" in body


def test_the_scanner_checks_against_the_brand_from_the_first_tab():
    """A consent page for one company does not register a campaign for
    another, so the two tabs have to agree."""
    at = brand("Halcyon Data Systems Corporation", "36-7712045")
    body = text_of(consent(SAMPLE_CONSENT_GOOD, at))
    assert "Halcyon Data Systems Corporation" in body
    assert NOT_READY in body
    assert "Who is sending" in body


@pytest.mark.parametrize("preset", list(CONSENT_PRESETS))
def test_every_consent_preset_renders_without_crashing(preset):
    at = run_app()
    widget(at, "selectbox", "Load a consent page example").set_value(preset).run()
    assert not at.exception, [str(e.value) for e in at.exception]


def test_an_empty_consent_page_fails_rather_than_passing_quietly():
    at = brand(NORTHWIND)
    body = text_of(consent("", at))
    assert NOT_READY in body
    assert "0 of 7 mandatory clauses" in body


# ---------------------------------------------------------------------------
# Sample message
# ---------------------------------------------------------------------------

def test_a_compliant_message_passes_with_one_segment():
    at = brand(NORTHWIND)
    body = text_of(message(
        "Northwind Logistics: your delivery [OrderNumber] arrives today. "
        "Reply STOP to cancel. Reply HELP for help.", at))
    assert "Ready to submit" in body
    assert "Segments billed" in body


def test_a_message_with_no_opt_out_is_rejected_and_corrected():
    at = brand(NORTHWIND)
    at = message("Northwind Logistics: your order shipped.", at)
    body = text_of(at)
    assert "Will be rejected" in body
    assert "Opt out instruction" in body
    assert "Reply STOP to cancel." in body


def test_a_public_shortener_is_flagged_as_filtered_by_carriers():
    at = brand(NORTHWIND)
    body = text_of(message(
        "Northwind Logistics: track at bit.ly/3xKp2. Reply STOP to cancel.",
        at))
    assert "shared shortener" in body
    assert "Will be rejected" in body


def test_one_emoji_moves_the_message_to_ucs2_and_says_why():
    at = brand(NORTHWIND)
    body = text_of(message(
        "Northwind Logistics: your delivery is out \U0001F69A. Reply STOP to "
        "cancel.", at))
    assert "UCS 2" in body
    assert "cuts the single segment length from 160 to 70" in body


def test_adding_the_opt_out_can_add_a_segment_and_the_page_warns():
    """The cost nobody sees until the invoice."""
    at = brand(NORTHWIND)
    at = message("Northwind Logistics: " + "a" * 129, at)
    body = text_of(at)
    assert "takes this from 1 to 2 segments" in body


def test_turning_off_the_first_message_flag_relaxes_the_opt_out_rule():
    at = brand(NORTHWIND)
    at = message("Northwind Logistics: your order shipped.", at)
    assert "Will be rejected" in text_of(at)

    widget(at, "checkbox",
           "This is the first message of the campaign").set_value(False).run()
    assert "Ready to submit" in text_of(at)


@pytest.mark.parametrize("preset", list(MESSAGE_PRESETS))
def test_every_message_preset_renders_without_crashing(preset):
    at = run_app()
    widget(at, "selectbox", "Load a message example").set_value(preset).run()
    assert not at.exception, [str(e.value) for e in at.exception]


# ---------------------------------------------------------------------------
# The three checks together
# ---------------------------------------------------------------------------

def test_all_three_passing_reports_the_submission_as_ready():
    at = brand(NORTHWIND)
    at = consent(SAMPLE_CONSENT_GOOD, at)
    at = message("Northwind Logistics: your delivery [OrderNumber] arrives "
                 "today. Reply STOP to cancel.", at)
    assert "All three pass" in text_of(at)


def test_one_failing_check_holds_the_whole_submission():
    at = brand("Northwind Logistics")      # missing the entity suffix
    at = consent(SAMPLE_CONSENT_GOOD, at)
    body = text_of(at)
    assert "Not ready" in body
    assert "Any one of them failing stops the campaign" in body


def test_no_dash_characters_reach_the_screen():
    at = brand("Northwind Logistics")
    body = text_of(consent(SAMPLE_CONSENT_THIN, at))
    assert "—" not in body
    assert "–" not in body
