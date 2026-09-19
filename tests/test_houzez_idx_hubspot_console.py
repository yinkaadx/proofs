"""Tests for the Houzez IDX and HubSpot Console.

Two things are worth proving. A listing that forbids display must produce no
mapped data at all, not data with a flag beside it, because a dictionary that
exists is a dictionary something eventually renders and the penalty for
rendering it is the feed being switched off. And the router must be
idempotent under replay: the same lead arriving twice writes once, and the
same person asking about a second property is one contact and two enquiries
rather than two of each.

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

from tools.houzez_idx_hubspot_console.core import (  # noqa: E402
    ENGINE_VERSION,
    FIELD_MAP,
    PROPERTY_TYPES,
    ROUTE_CREATED,
    ROUTE_DUPLICATE,
    ROUTE_NEW_ENQUIRY,
    ROUTE_REJECTED,
    SAMPLE_EVENTS,
    SAMPLE_LISTINGS,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    STATUS_MAP,
    SYNC_PUBLISH,
    SYNC_PUBLISH_REDACTED,
    SYNC_SUPPRESS,
    TRACKING_COOKIE_NAME,
    TYPE_MAP,
    VALID_ACCEPTED,
    VALID_ACCEPTED_UNATTRIBUTED,
    VALID_REJECTED,
    contact_key,
    idempotency_key,
    normalise_email,
    replay_ledger,
    simulate_idx_feed_sync,
    simulate_make_lead_routing,
    validate_hubspot_embed,
)

OPEN_LISTING = "MLS-4471902"        # displays everything
NO_ADDRESS_LISTING = "MLS-5580311"  # displays, but withholds the address
NO_DISPLAY_LISTING = "MLS-6012887"  # forbids display entirely
GOOD_COOKIE = "9f2c41ab77de40518c3b6a0e5d17b842"


# ---------------------------------------------------------------------------
# IDX mapping
# ---------------------------------------------------------------------------


def test_a_listing_that_forbids_display_produces_no_meta_at_all():
    """Not data with a flag beside it. No data."""
    record = simulate_idx_feed_sync(NO_DISPLAY_LISTING, "ResidentialLease")
    assert record.action == SYNC_SUPPRESS
    assert record.meta == {}
    assert record.taxonomies == {}
    assert not record.publishable
    assert record.mapped_field_count == 0
    assert any(f.code == "IDX-NO-DISPLAY" and f.severity == SEVERITY_CRITICAL
               for f in record.findings)


def test_a_withheld_address_is_absent_rather_than_blank():
    """A theme that checks for the key rather than the value prints a blank
    line otherwise."""
    record = simulate_idx_feed_sync(NO_ADDRESS_LISTING, "Residential")
    assert record.action == SYNC_PUBLISH_REDACTED
    assert record.publishable
    assert "fave_property_address" not in record.meta
    assert "fave_property_zip" not in record.meta
    assert set(record.withheld_fields) == {"fave_property_address",
                                           "fave_property_zip"}
    assert any(f.code == "IDX-NO-ADDRESS" for f in record.findings)


def test_a_withheld_address_still_keeps_the_map_pin():
    """The coordinates are permitted, which is why the rule is about the
    address fields specifically rather than about location."""
    record = simulate_idx_feed_sync(NO_ADDRESS_LISTING, "Residential")
    assert record.meta["fave_property_location_lat"]
    assert record.meta["fave_property_location_long"]


def test_a_fully_open_listing_maps_every_field_in_the_map():
    record = simulate_idx_feed_sync(OPEN_LISTING, "Residential")
    assert record.action == SYNC_PUBLISH
    assert record.withheld_fields == ()
    assert record.mapped_field_count == len(FIELD_MAP)
    for _feed_field, houzez_key, _note in FIELD_MAP:
        assert houzez_key in record.meta, houzez_key


def test_every_mapped_key_carries_the_houzez_prefix():
    """Getting the prefix wrong renders every field blank with no error."""
    for _feed_field, houzez_key, _note in FIELD_MAP:
        assert houzez_key.startswith("fave_"), houzez_key
    record = simulate_idx_feed_sync(OPEN_LISTING, "Residential")
    for key in record.meta:
        assert key.startswith("fave_"), key


def test_every_meta_value_is_a_string_for_the_template():
    record = simulate_idx_feed_sync(OPEN_LISTING, "Residential")
    for key, value in record.meta.items():
        assert isinstance(value, str), key


def test_an_unmapped_property_type_suppresses_rather_than_importing_blind():
    """An unmapped type imports with no term and drops out of every filtered
    search while still rendering on its own page."""
    record = simulate_idx_feed_sync(OPEN_LISTING, "Houseboat")
    assert record.action == SYNC_SUPPRESS
    assert record.meta == {}
    assert any(f.code == "IDX-TYPE" and f.severity == SEVERITY_CRITICAL
               for f in record.findings)


def test_every_feed_type_has_a_houzez_term_and_none_is_empty():
    assert set(PROPERTY_TYPES) == set(TYPE_MAP)
    for feed_type, term in TYPE_MAP.items():
        assert term.strip(), feed_type
        record = simulate_idx_feed_sync(OPEN_LISTING, feed_type)
        assert record.houzez_type_term == term, feed_type


def test_the_status_term_comes_from_the_feed_status():
    for mls_id, listing in SAMPLE_LISTINGS.items():
        if not listing["InternetEntireListingDisplayYN"]:
            continue
        record = simulate_idx_feed_sync(mls_id, "Residential")
        assert record.houzez_status_term == STATUS_MAP[
            listing["StandardStatus"]], mls_id


def test_a_listing_absent_from_the_pull_is_suppressed_and_flagged():
    """A feed is a replica, so a record that stops appearing was withdrawn."""
    record = simulate_idx_feed_sync("MLS-0000000", "Residential")
    assert record.action == SYNC_SUPPRESS
    assert record.meta == {}
    assert any(f.code == "IDX-ABSENT" for f in record.findings)


def test_a_malformed_listing_id_produces_nothing():
    for bad in ("", "  ", "ab", "id with spaces", "x" * 40, "!@#"):
        record = simulate_idx_feed_sync(bad, "Residential")
        assert record.action == SYNC_SUPPRESS, bad
        assert record.meta == {}, bad
        assert any(f.code == "IDX-ID" for f in record.findings), bad


def test_every_publishable_record_names_a_brokerage_to_credit():
    """The most commonly missed IDX rule."""
    for mls_id in SAMPLE_LISTINGS:
        record = simulate_idx_feed_sync(mls_id, "Residential")
        if not record.publishable:
            continue
        assert record.attribution.strip(), mls_id
        assert any(f.code == "IDX-ATTRIBUTION" for f in record.findings), mls_id


def test_the_listing_key_is_carried_for_the_upsert():
    record = simulate_idx_feed_sync(OPEN_LISTING, "Residential")
    assert record.listing_key == SAMPLE_LISTINGS[OPEN_LISTING]["ListingKey"]
    assert record.listing_key != record.mls_id
    assert any(f.code == "IDX-KEY" for f in record.findings)


# ---------------------------------------------------------------------------
# HubSpot embed
# ---------------------------------------------------------------------------


def test_a_submission_with_no_cookie_is_accepted_and_unattributed():
    """The whole point. It does not fail, it succeeds and loses the join."""
    validation = validate_hubspot_embed({"email": "a@example.com"}, "")
    assert validation.status == VALID_ACCEPTED_UNATTRIBUTED
    assert validation.accepted
    assert not validation.attribution_works
    assert not validation.tracking_cookie_present
    assert any(f.code == "HS-NO-HUTK" for f in validation.findings)


def test_a_good_cookie_gives_a_fully_attributed_submission():
    validation = validate_hubspot_embed({"email": "a@example.com"}, GOOD_COOKIE)
    assert validation.status == VALID_ACCEPTED
    assert validation.tracking_cookie_present
    assert validation.tracking_cookie_valid
    assert validation.attribution_works
    assert validation.payload["context"]["hutk"] == GOOD_COOKIE


def test_the_context_is_empty_rather_than_carrying_a_bad_token():
    for bad in ("", "not-a-token", GOOD_COOKIE[:20], GOOD_COOKIE.upper(),
                GOOD_COOKIE + "ff"):
        validation = validate_hubspot_embed({"email": "a@example.com"}, bad)
        assert validation.payload["context"] == {}, bad
        assert not validation.attribution_works, bad


def test_a_missing_or_malformed_email_is_rejected():
    for data in ({}, {"email": ""}, {"email": "   "}, {"email": "nope"},
                 {"email": "a@b"}, {"email": "a b@example.com"}):
        validation = validate_hubspot_embed(data, GOOD_COOKIE)
        assert validation.status == VALID_REJECTED, data
        assert not validation.accepted, data
        assert not validation.attribution_works, data
        assert validation.severity == SEVERITY_CRITICAL, data


def test_a_rejected_submission_is_never_reported_as_attributed():
    """Attribution on a submission that never landed is a contradiction."""
    validation = validate_hubspot_embed({"firstname": "A"}, GOOD_COOKIE)
    assert validation.status == VALID_REJECTED
    assert not validation.attribution_works


def test_the_payload_carries_every_field_it_was_given():
    data = {"email": "a@example.com", "firstname": "A", "phone": "0123"}
    validation = validate_hubspot_embed(data, GOOD_COOKIE)
    names = {entry["name"] for entry in validation.payload["fields"]}
    assert names == set(data)
    assert len(validation.payload["fields"]) == len(data)
    for entry in validation.payload["fields"]:
        assert isinstance(entry["value"], str), entry["name"]


def test_thin_payloads_are_flagged_without_being_rejected():
    validation = validate_hubspot_embed({"email": "a@example.com"}, GOOD_COOKIE)
    assert validation.accepted
    assert set(validation.missing_recommended) == {"firstname", "lastname",
                                                   "phone"}
    assert any(f.code == "HS-THIN" for f in validation.findings)
    full = validate_hubspot_embed(
        {"email": "a@example.com", "firstname": "A", "lastname": "B",
         "phone": "1"}, GOOD_COOKIE)
    assert full.missing_recommended == ()
    assert full.severity == SEVERITY_OK


def test_the_cookie_name_is_the_one_hubspot_actually_sets():
    assert TRACKING_COOKIE_NAME == "hubspotutk"
    validation = validate_hubspot_embed({"email": "a@example.com"}, "")
    joined = " ".join(f.fix + f.title for f in validation.findings)
    assert TRACKING_COOKIE_NAME in joined


# ---------------------------------------------------------------------------
# Lead routing idempotency
# ---------------------------------------------------------------------------


def test_the_same_lead_twice_writes_once():
    first = simulate_make_lead_routing("a@example.com", "MLS-1")
    assert first.outcome == ROUTE_CREATED
    assert first.contact_created and first.enquiry_created
    retry = simulate_make_lead_routing(
        "a@example.com", "MLS-1",
        seen_enquiry_keys=(first.idempotency_key,),
        seen_contact_keys=(first.contact_key,))
    assert retry.outcome == ROUTE_DUPLICATE
    assert not retry.contact_created
    assert not retry.enquiry_created
    assert not retry.wrote_anything


def test_a_second_property_is_one_contact_and_two_enquiries():
    """The most common duplicate in a property CRM is creating both again."""
    first = simulate_make_lead_routing("a@example.com", "MLS-1")
    second = simulate_make_lead_routing(
        "a@example.com", "MLS-2",
        seen_enquiry_keys=(first.idempotency_key,),
        seen_contact_keys=(first.contact_key,))
    assert second.outcome == ROUTE_NEW_ENQUIRY
    assert not second.contact_created
    assert second.enquiry_created
    assert second.contact_key == first.contact_key
    assert second.idempotency_key != first.idempotency_key


def test_the_keys_are_deterministic_and_case_insensitive():
    assert idempotency_key("A@Example.com ", "MLS-1") == idempotency_key(
        "a@example.com", "mls-1")
    assert contact_key(" A@EXAMPLE.COM") == contact_key("a@example.com")
    assert idempotency_key("a@example.com", "MLS-1") == idempotency_key(
        "a@example.com", "MLS-1")


def test_different_leads_get_different_keys():
    assert idempotency_key("a@example.com", "MLS-1") != idempotency_key(
        "b@example.com", "MLS-1")
    assert idempotency_key("a@example.com", "MLS-1") != idempotency_key(
        "a@example.com", "MLS-2")
    assert contact_key("a@example.com") != contact_key("b@example.com")


def test_normalising_an_email_does_not_fold_plus_addressing():
    """Folding is right for some providers and wrong for others, and merging
    two real people is worse than letting a duplicate through."""
    assert normalise_email(" A.B+Tag@Example.COM ") == "a.b+tag@example.com"
    assert contact_key("a+one@example.com") != contact_key("a@example.com")
    result = simulate_make_lead_routing("a+one@example.com", "MLS-1")
    assert any(f.code == "MAKE-PLUS" for f in result.findings)


def test_a_lead_with_no_usable_email_is_rejected_and_writes_nothing():
    for bad in ("", "   ", "not an email", "a@b", "a b@example.com"):
        result = simulate_make_lead_routing(bad, "MLS-1")
        assert result.outcome == ROUTE_REJECTED, bad
        assert not result.wrote_anything, bad
        assert result.crm_record == {}, bad
        assert result.severity == SEVERITY_CRITICAL, bad


def test_a_lead_with_no_property_interest_is_rejected():
    result = simulate_make_lead_routing("a@example.com", "")
    assert result.outcome == ROUTE_REJECTED
    assert not result.wrote_anything
    assert result.idempotency_key == ""
    assert any(f.code == "MAKE-NO-INTEREST" for f in result.findings)


def test_replaying_the_whole_ledger_gives_the_counts_it_should():
    ledger = replay_ledger(SAMPLE_EVENTS)
    assert ledger["events"] == len(SAMPLE_EVENTS)
    assert ledger["contacts_created"] == 2
    assert ledger["enquiries_created"] == 3
    assert ledger["duplicates_suppressed"] == 2
    assert ledger["rejected"] == 1
    outcomes = [row.outcome for row in ledger["rows"]]
    assert outcomes.count(ROUTE_CREATED) == 2
    assert len(outcomes) == ledger["events"]


def test_every_arrival_lands_in_exactly_one_bucket():
    ledger = replay_ledger(SAMPLE_EVENTS)
    created = sum(1 for r in ledger["rows"] if r.outcome == ROUTE_CREATED)
    reused = sum(1 for r in ledger["rows"] if r.outcome == ROUTE_NEW_ENQUIRY)
    total = (created + reused + ledger["duplicates_suppressed"]
             + ledger["rejected"])
    assert total == ledger["events"]
    assert ledger["contacts_created"] == created
    assert ledger["enquiries_created"] == created + reused


def test_replaying_the_same_ledger_twice_writes_nothing_new():
    """The property that matters: a scenario rerun must be inert."""
    once = replay_ledger(SAMPLE_EVENTS)
    twice = replay_ledger(SAMPLE_EVENTS + SAMPLE_EVENTS)
    assert twice["contacts_created"] == once["contacts_created"]
    assert twice["enquiries_created"] == once["enquiries_created"]
    assert twice["events"] == 2 * once["events"]
    assert twice["duplicates_suppressed"] > once["duplicates_suppressed"]


def test_the_crm_record_names_what_it_did_to_each_object():
    created = simulate_make_lead_routing("a@example.com", "MLS-1")
    assert created.crm_record["contact"]["action"] == "created"
    assert created.crm_record["enquiry"]["action"] == "created"
    duplicate = simulate_make_lead_routing(
        "a@example.com", "MLS-1",
        seen_enquiry_keys=(created.idempotency_key,),
        seen_contact_keys=(created.contact_key,))
    assert duplicate.crm_record["contact"]["action"] == "matched on email"
    assert duplicate.crm_record["enquiry"]["action"] == "suppressed as duplicate"


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "houzez_idx_hubspot_console" / "core.py").read_text()
    assert "import streamlit" not in source
    probe = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.houzez_idx_hubspot_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT)
    )
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [f.title + f.detail + f.fix
         for mls_id in list(SAMPLE_LISTINGS) + ["MLS-0000000", ""]
         for kind in ("Residential", "Houseboat")
         for f in simulate_idx_feed_sync(mls_id, kind).findings]
        + [note for _a, _b, note in FIELD_MAP]
        + [f.title + f.detail + f.fix
           for data, cookie in (({"email": "a@example.com"}, ""),
                                ({"email": "a@example.com"}, GOOD_COOKIE),
                                ({}, GOOD_COOKIE))
           for f in validate_hubspot_embed(data, cookie).findings]
        + [f.title + f.detail + f.fix
           for row in replay_ledger(SAMPLE_EVENTS)["rows"]
           for f in row.findings]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools
    tools = all_tools()
    entry = next(t for t in tools if t.key == "houzez-idx-hubspot-console")
    assert entry.title == "Houzez IDX & HubSpot Console"
    icons = [t.icon for t in tools]
    assert icons.count(entry.icon) == 1, "this icon is already used by another tool"
    assert len(entry.tagline) > 30
