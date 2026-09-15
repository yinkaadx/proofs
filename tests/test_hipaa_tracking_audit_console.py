"""Engine tests for the HIPAA Web and Tracking Audit Console.

Written for pytest. Deterministic: nothing in the engine reads the clock or a
random source.

Several of these tests exist to stop the tool giving advice that was correct in
2023 and is wrong now. A federal court vacated part of OCR's bulletin in June
2024, and an auditor that still flags every analytics tag on every page would
be confidently out of date.

Run: pytest tests/test_hipaa_tracking_audit_console.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.hipaa_tracking_audit_console.core import (  # noqa: E402
    BAA_AVAILABLE,
    BAA_CONDITIONAL,
    BAA_NOT_OFFERED,
    DISCLAIMER,
    ENGINE_VERSION,
    HANDOFF_API_POST,
    HANDOFF_METHODS,
    HANDOFF_QUERY_PARAMS,
    PAGE_AUTHENTICATED,
    PAGE_BOOKING,
    PAGE_CONDITION,
    PAGE_MARKETING,
    PAGE_TYPES,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MODERATE,
    STATUS_INSECURE,
    STATUS_SECURE,
    audit_everything,
    baa_rows,
    evaluate_ehr_handoff,
    evaluate_gtm_exposure,
    generate_clinician_summary,
    get_baa_matrix,
    unsigned_vendors,
)


def vendor(name_fragment: str):
    return next(e for e in get_baa_matrix() if name_fragment in e.vendor)


# ---------------------------------------------------------------------------
# Tag exposure: where PHI is in play
# ---------------------------------------------------------------------------

def test_a_google_ads_tag_on_a_booking_form_is_critical_and_a_violation():
    finding = evaluate_gtm_exposure(PAGE_BOOKING, True, True)
    assert finding.risk == RISK_CRITICAL
    assert finding.ocr_violation is True


def test_a_google_ads_tag_behind_the_login_is_critical_and_a_violation():
    finding = evaluate_gtm_exposure(PAGE_AUTHENTICATED, False, True)
    assert finding.risk == RISK_CRITICAL
    assert finding.ocr_violation is True


def test_the_authenticated_finding_says_the_vacatur_did_not_reach_it():
    """The most dangerous misreading: that the 2024 ruling made all of this
    go away. It reached unauthenticated pages only."""
    finding = evaluate_gtm_exposure(PAGE_AUTHENTICATED, False, True)
    assert "Unaffected by" in finding.authority
    assert "vacatur" in finding.explanation


def test_removing_the_ads_tag_lowers_the_risk_but_phi_is_still_in_scope():
    finding = evaluate_gtm_exposure(PAGE_BOOKING, True, False)
    assert finding.risk == RISK_HIGH
    assert finding.ocr_violation is True
    assert "attaches to the data" in finding.explanation


def test_a_marketing_page_that_starts_collecting_bookings_changes_verdict():
    """The one transition a practice actually makes, by adding a form."""
    before = evaluate_gtm_exposure(PAGE_MARKETING, False, True)
    after = evaluate_gtm_exposure(PAGE_MARKETING, True, True)
    assert before.ocr_violation is False
    assert after.ocr_violation is True
    assert after.risk == RISK_CRITICAL


# ---------------------------------------------------------------------------
# Tag exposure: what the June 2024 vacatur changed
# ---------------------------------------------------------------------------

def test_a_condition_page_is_no_longer_an_ocr_violation_on_its_own():
    """This is the finding that changed. Before June 2024 an IP address plus a
    visit to a page about a condition was treated as PHI; that was vacated."""
    finding = evaluate_gtm_exposure(PAGE_CONDITION, False, True)
    assert finding.ocr_violation is False
    assert finding.risk == RISK_MODERATE


def test_the_condition_page_still_reports_real_non_hipaa_exposure():
    """Not a violation is not the same as not a problem."""
    finding = evaluate_gtm_exposure(PAGE_CONDITION, False, True)
    assert "FTC Health Breach Notification Rule" in finding.explanation
    assert finding.remediation


def test_the_vacatur_is_cited_by_name_and_date_wherever_it_is_relied_on():
    for page_type in (PAGE_CONDITION, PAGE_MARKETING):
        authority = evaluate_gtm_exposure(page_type, False, True).authority
        assert "American Hospital Association v. Becerra" in authority
        assert "20 June 2024" in authority


def test_a_plain_marketing_page_with_no_form_is_low_risk():
    finding = evaluate_gtm_exposure(PAGE_MARKETING, False, True)
    assert finding.risk == RISK_LOW
    assert finding.ocr_violation is False


def test_only_pages_where_phi_is_in_play_are_called_violations():
    for page_type in PAGE_TYPES:
        for collects in (True, False):
            for ads in (True, False):
                finding = evaluate_gtm_exposure(page_type, collects, ads)
                phi = page_type in (PAGE_AUTHENTICATED, PAGE_BOOKING) or collects
                assert finding.ocr_violation == phi, (page_type, collects, ads)


@pytest.mark.parametrize("page_type", PAGE_TYPES)
def test_every_page_type_gives_a_complete_finding(page_type):
    finding = evaluate_gtm_exposure(page_type, False, True)
    assert finding.headline and finding.explanation and finding.authority
    assert finding.risk in (RISK_CRITICAL, RISK_HIGH, RISK_MODERATE, RISK_LOW)
    assert all(isinstance(v, str) for row in finding.rows()
               for v in row.values())


def test_an_unknown_page_type_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        evaluate_gtm_exposure("Blog archive", False, True)


def test_a_violation_always_names_what_to_do_about_it():
    for page_type in PAGE_TYPES:
        finding = evaluate_gtm_exposure(page_type, True, True)
        if finding.ocr_violation:
            assert finding.remediation, page_type


def test_a_live_tag_on_phi_is_treated_as_a_possible_breach_not_just_removed():
    steps = " ".join(evaluate_gtm_exposure(PAGE_BOOKING, True, True).remediation)
    assert "164.402" in steps
    assert "breach" in steps


# ---------------------------------------------------------------------------
# The EHR handoff
# ---------------------------------------------------------------------------

def test_query_parameters_are_insecure_and_a_post_is_not():
    assert evaluate_ehr_handoff(HANDOFF_QUERY_PARAMS).status == STATUS_INSECURE
    assert evaluate_ehr_handoff(HANDOFF_QUERY_PARAMS).safe is False
    assert evaluate_ehr_handoff(HANDOFF_API_POST).status == STATUS_SECURE
    assert evaluate_ehr_handoff(HANDOFF_API_POST).safe is True


def test_the_query_param_finding_names_where_the_data_actually_lands():
    leaks = " ".join(evaluate_ehr_handoff(HANDOFF_QUERY_PARAMS).leaks).lower()
    for place in ("access log", "browser history", "referer", "analytics"):
        assert place in leaks, place


def test_it_says_plainly_that_tls_does_not_save_a_query_string():
    """The single most common misunderstanding: the site is https, so it is
    fine. The address is not the payload."""
    summary = evaluate_ehr_handoff(HANDOFF_QUERY_PARAMS).summary
    assert "TLS does not help" in summary


def test_the_insecure_example_shows_phi_in_the_address():
    example = evaluate_ehr_handoff(HANDOFF_QUERY_PARAMS).example
    assert example.startswith("https://")
    assert "?" in example and "name=" in example


def test_the_secure_example_puts_phi_in_the_body_and_a_token_in_the_link():
    example = evaluate_ehr_handoff(HANDOFF_API_POST).example
    assert example.startswith("POST ")
    assert '"name"' in example
    assert "token" in example
    # The link the client clicks must not carry the details.
    client_link = example.strip().splitlines()[-1]
    assert "name=" not in client_link and "@" not in client_link


def test_the_secure_option_depends_on_the_ehr_having_signed_a_baa():
    controls = " ".join(evaluate_ehr_handoff(HANDOFF_API_POST).controls)
    assert "BAA" in controls


def test_an_unknown_handoff_method_is_refused():
    with pytest.raises(ValueError):
        evaluate_ehr_handoff("Hidden form field")


@pytest.mark.parametrize("method", HANDOFF_METHODS)
def test_both_handoff_methods_produce_arrow_safe_rows(method):
    for row in evaluate_ehr_handoff(method).rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# The BAA matrix
# ---------------------------------------------------------------------------

def test_the_matrix_covers_the_four_named_vendors():
    vendors = " ".join(e.vendor for e in get_baa_matrix())
    for name in ("Google", "SimplePractice", "GoHighLevel", "Replit"):
        assert name in vendors, name


def test_google_will_not_sign_for_analytics_or_ads():
    entry = vendor("Google")
    assert entry.status == BAA_NOT_OFFERED
    assert entry.confirm_with_vendor is False, (
        "this one is settled vendor policy, not something to go and ask about")


def test_the_google_entry_does_not_confuse_cloud_with_analytics():
    """Google does sign BAAs for certain Cloud and Workspace services, so an
    entry that just said 'Google will not sign' would be wrong."""
    detail = vendor("Google").detail
    assert "Cloud" in detail and "Workspace" in detail
    assert "not among them" in detail


def test_simplepractice_offers_a_baa_but_still_has_to_be_filed():
    entry = vendor("SimplePractice")
    assert entry.status == BAA_AVAILABLE
    assert entry.confirm_with_vendor is True
    assert "not the same as having one" in entry.detail


def test_the_uncertain_vendors_are_marked_conditional_rather_than_asserted():
    """Stating a confident yes or no about a vendor's contract terms without
    checking would be the worst thing this matrix could do."""
    for name in ("GoHighLevel", "Replit"):
        assert vendor(name).status == BAA_CONDITIONAL
        assert vendor(name).confirm_with_vendor is True


def test_every_entry_carries_an_action_rather_than_only_a_status():
    for entry in get_baa_matrix():
        assert entry.action and entry.detail and entry.role


def test_the_working_list_is_everything_that_is_not_a_settled_yes():
    outstanding = unsigned_vendors()
    assert "Google (Analytics and Ads)" in outstanding
    assert len(outstanding) == len(get_baa_matrix())


def test_the_matrix_rows_are_arrow_safe():
    for row in baa_rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# The plain language summary
# ---------------------------------------------------------------------------

def test_the_summary_is_written_for_a_practice_owner():
    points = generate_clinician_summary()
    assert len(points) >= 5
    for point in points:
        assert len(point) > 40
        # Jargon a practice owner would have to look up.
        for term in ("IIHI", "proscribed combination", "45 CFR"):
            assert term not in point, point


def test_the_summary_tells_them_the_guidance_changed():
    body = " ".join(generate_clinician_summary())
    assert "June 2024" in body
    assert "anyone telling you all tracking is banned" in body


def test_the_summary_covers_each_of_the_four_findings():
    body = " ".join(generate_clinician_summary()).lower()
    for topic in ("google", "link", "business associate agreement", "lawyer"):
        assert topic in body, topic


def test_the_summary_never_tells_them_to_just_remove_the_tag_and_move_on():
    body = " ".join(generate_clinician_summary())
    assert "do not simply" in body


# ---------------------------------------------------------------------------
# The whole audit, and house rules
# ---------------------------------------------------------------------------

def test_the_combined_audit_returns_every_part():
    audit = audit_everything()
    assert set(audit) == {"exposure", "handoff", "baa", "summary", "open_items"}
    assert audit["open_items"] > 0


def test_the_best_case_configuration_still_leaves_the_vendor_work_outstanding():
    audit = audit_everything(PAGE_MARKETING, False, False, HANDOFF_API_POST)
    assert audit["exposure"].ocr_violation is False
    assert audit["handoff"].safe is True
    assert audit["open_items"] == len(unsigned_vendors())


def test_the_worst_case_counts_more_open_items_than_the_best():
    worst = audit_everything(PAGE_BOOKING, True, True, HANDOFF_QUERY_PARAMS)
    best = audit_everything(PAGE_MARKETING, False, False, HANDOFF_API_POST)
    assert worst["open_items"] > best["open_items"]


def test_it_never_presents_itself_as_legal_advice():
    assert "not legal advice" in DISCLAIMER


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (Path(__file__).resolve().parents[1] / "tools"
              / "hipaa_tracking_audit_console" / "core.py").read_text()
    assert "streamlit" not in source


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [DISCLAIMER] + generate_clinician_summary()
        + [e.detail + e.action for e in get_baa_matrix()]
        + [evaluate_gtm_exposure(p, True, True).explanation for p in PAGE_TYPES]
        + [evaluate_ehr_handoff(m).summary for m in HANDOFF_METHODS])
    assert "—" not in text
    assert "–" not in text
