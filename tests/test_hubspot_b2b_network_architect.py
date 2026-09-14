"""Engine tests for the HubSpot B2B Network Architect.

Written for pytest. Deterministic throughout: nothing reads the clock or a
random source, and every timestamp arrives as an argument, so a test and a run
agree.

Run: pytest tests/test_hubspot_b2b_network_architect.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.hubspot_b2b_network_architect.core import (  # noqa: E402
    CATEGORY_HUBSPOT,
    CATEGORY_USER,
    CODE_AMBIGUOUS_TYPE,
    CODE_BACKFILL,
    CODE_CLEAN,
    CODE_DUPLICATE,
    CODE_LEDGER_CLEAN,
    CODE_MAP_CLEAN,
    CODE_REDELIVERY,
    CODE_REPLACED,
    CONNECTED_AT,
    DAY,
    DELIMITER,
    ENGINE_VERSION,
    HOUR,
    LABEL_INTRODUCED_BY,
    LABEL_TARGET_FOR,
    LOGGED,
    MODE_BLIND_CREATE,
    MODE_SEARCH_APPEND,
    MODE_SEARCH_REPLACE,
    MODES,
    NETWORK_CLIENT,
    NETWORK_MEMBER,
    NETWORK_RELATIONSHIP,
    NETWORK_VALUES,
    NETWORKS,
    OUTCOME_CREATED,
    OUTCOME_DUPLICATE,
    OUTCOME_REPLACED,
    OUTCOME_UNCHANGED,
    OUTCOME_UPDATED,
    PORTAL_LABELS,
    PROPERTY_FIELD_TYPE,
    PROPERTY_NAME,
    PROPERTY_TYPE,
    SAMPLE_COMPANIES,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SKIPPED_HISTORICAL,
    SKIPPED_SEEN,
    SOURCE_HUBLEAD,
    SOURCE_SURFE,
    TYPE_CONTACT_TO_COMPANY,
    TYPE_CONTACT_TO_COMPANY_PRIMARY,
    CompanySeed,
    EMPLOYER,
    GRIDWISE_CLIENT,
    Ledger,
    LinkedInMessage,
    PROSPECT,
    Portal,
    assign_network,
    audit_ledger,
    audit_portal,
    canonical,
    categorise,
    domain_variants,
    fingerprint,
    ledger_comparison,
    map_prospect,
    normalise_domain,
    payload_json,
    process_payload,
    resolve_label,
    sample_messages,
    stamp,
)

ALL_THREE = (NETWORK_CLIENT, NETWORK_MEMBER, NETWORK_RELATIONSHIP)
NORTHWIND = SAMPLE_COMPANIES[0]


# ---------------------------------------------------------------------------
# Domain normalisation, which is what the whole dedup story rests on
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pasted", [
    "gridwise.io",
    "GRIDWISE.IO",
    "www.gridwise.io",
    "WWW.Gridwise.IO",
    "http://gridwise.io",
    "https://www.gridwise.io",
    "https://www.gridwise.io/",
    "https://www.gridwise.io/pricing",
    "https://gridwise.io/pricing?utm_source=linkedin",
    "https://gridwise.io#team",
    "http://gridwise.io:443/",
    "hello@gridwise.io",
    "  https://WWW.gridwise.io/about  ",
    "gridwise.io.",
])
def test_every_way_a_person_pastes_a_site_reduces_to_one_domain(pasted):
    assert normalise_domain(pasted) == "gridwise.io"


def test_an_empty_website_normalises_to_an_empty_string():
    assert normalise_domain("") == ""
    assert normalise_domain(None) == ""


def test_two_different_companies_do_not_normalise_together():
    assert normalise_domain("https://gridwise.io") != \
        normalise_domain("https://gridwise.com")


def test_the_variant_table_says_every_paste_matches():
    rows = domain_variants(NORTHWIND)
    assert rows, "the variant table must not be empty"
    assert all(row["Matches"] == "yes" for row in rows), rows


# ---------------------------------------------------------------------------
# Categorization: one company, several networks, one record
# ---------------------------------------------------------------------------

def test_the_property_is_multiple_checkboxes_not_a_pipeline():
    assert PROPERTY_NAME == "gridwise_network"
    assert PROPERTY_TYPE == "enumeration"
    assert PROPERTY_FIELD_TYPE == "checkbox"
    assert DELIMITER == ";"


def test_the_three_networks_are_the_three_gridwise_asked_for():
    labels = [option.label for option in NETWORKS]
    assert labels == ["Client Network", "Member Network",
                      "Relationship Network"]
    assert len(NETWORK_VALUES) == len(set(NETWORK_VALUES))


def test_all_three_networks_land_on_one_record():
    portal = categorise(NORTHWIND, ALL_THREE)
    assert len(portal.records) == 1
    assert portal.duplicate_count == 0
    assert portal.records[0].networks == ALL_THREE


def test_the_property_value_is_one_semicolon_delimited_string():
    portal = categorise(NORTHWIND, ALL_THREE)
    assert portal.records[0].property_value == \
        "client_network;member_network;relationship_network"


def test_the_first_assignment_creates_and_the_rest_patch():
    portal = categorise(NORTHWIND, ALL_THREE)
    verbs = [write.verb for write in portal.writes]
    assert verbs == ["POST", "PATCH", "PATCH"]
    assert portal.writes[0].outcome == OUTCOME_CREATED


def test_every_append_write_carries_the_leading_semicolon():
    """The leading delimiter is the entire difference between add and replace."""
    portal = categorise(NORTHWIND, ALL_THREE)
    for write in portal.writes[1:]:
        assert write.body["properties"][PROPERTY_NAME].startswith(DELIMITER)


def test_the_create_body_carries_the_domain_and_the_network():
    portal = categorise(NORTHWIND, (NETWORK_CLIENT,))
    properties = portal.writes[0].body["properties"]
    assert properties["domain"] == normalise_domain(NORTHWIND.website)
    assert properties[PROPERTY_NAME] == NETWORK_CLIENT


def test_assigning_the_same_network_twice_writes_nothing():
    portal = categorise(NORTHWIND, (NETWORK_CLIENT, NETWORK_CLIENT))
    assert len(portal.records) == 1
    assert portal.records[0].networks == (NETWORK_CLIENT,)
    assert portal.writes[-1].outcome == OUTCOME_UNCHANGED


def test_the_record_count_is_one_however_many_assignments_arrive():
    portal = categorise(NORTHWIND, ALL_THREE * 4)
    assert len(portal.records) == 1
    assert portal.records[0].networks == ALL_THREE


@pytest.mark.parametrize("seed", SAMPLE_COMPANIES)
def test_every_sample_company_categorises_to_a_single_record(seed):
    portal = categorise(seed, ALL_THREE)
    assert len(portal.records) == 1
    assert portal.records[0].domain == normalise_domain(seed.website)


def test_several_companies_share_a_portal_without_colliding():
    portal = Portal()
    for seed in SAMPLE_COMPANIES:
        categorise(seed, ALL_THREE, portal=portal)
    assert len(portal.records) == len(SAMPLE_COMPANIES)
    assert portal.duplicate_count == 0
    assert len(portal.all_domains()) == len(set(portal.all_domains()))


def test_record_ids_are_unique():
    portal = Portal()
    for seed in SAMPLE_COMPANIES:
        categorise(seed, (NETWORK_CLIENT,), portal=portal)
    ids = [record.record_id for record in portal.records]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Categorization: the two ways it goes wrong
# ---------------------------------------------------------------------------

def test_creating_a_company_per_network_duplicates_the_company():
    portal = categorise(NORTHWIND, ALL_THREE, mode=MODE_BLIND_CREATE)
    assert len(portal.records) == 3
    assert portal.duplicate_count == 2
    assert portal.duplicate_domains == [normalise_domain(NORTHWIND.website)]


def test_the_duplicate_writes_are_named_as_duplicates():
    portal = categorise(NORTHWIND, ALL_THREE, mode=MODE_BLIND_CREATE)
    outcomes = [write.outcome for write in portal.writes]
    assert outcomes == [OUTCOME_CREATED, OUTCOME_DUPLICATE, OUTCOME_DUPLICATE]


def test_duplication_raises_a_critical_finding():
    portal = categorise(NORTHWIND, ALL_THREE, mode=MODE_BLIND_CREATE)
    codes = {f.code: f for f in audit_portal(portal, requested=3)}
    assert CODE_DUPLICATE in codes
    assert codes[CODE_DUPLICATE].severity == SEVERITY_CRITICAL


def test_replacing_keeps_one_record_and_loses_the_earlier_network():
    """The quiet one. The record count stays right, so review lets it past."""
    portal = categorise(NORTHWIND, ALL_THREE, mode=MODE_SEARCH_REPLACE)
    assert len(portal.records) == 1
    assert portal.duplicate_count == 0
    assert portal.records[0].networks == (NETWORK_RELATIONSHIP,)


def test_a_replacing_write_omits_the_leading_semicolon():
    portal = categorise(NORTHWIND, (NETWORK_CLIENT, NETWORK_MEMBER),
                        mode=MODE_SEARCH_REPLACE)
    value = portal.writes[-1].body["properties"][PROPERTY_NAME]
    assert not value.startswith(DELIMITER)
    assert value == NETWORK_MEMBER


def test_replacement_is_reported_even_though_nothing_duplicated():
    portal = categorise(NORTHWIND, ALL_THREE, mode=MODE_SEARCH_REPLACE)
    findings = audit_portal(portal, requested=3)
    codes = {f.code for f in findings}
    assert CODE_REPLACED in codes
    assert CODE_DUPLICATE not in codes


def test_the_first_replace_has_nothing_to_overwrite():
    portal = categorise(NORTHWIND, (NETWORK_CLIENT,),
                        mode=MODE_SEARCH_REPLACE)
    assert portal.writes[-1].outcome == OUTCOME_CREATED
    assert CODE_REPLACED not in {f.code for f in audit_portal(portal, 1)}


def test_skipping_normalisation_duplicates_on_a_pasted_url():
    """Northwind's website carries a scheme and a www, which is the whole bug."""
    portal = categorise(NORTHWIND, (NETWORK_CLIENT,), normalise=False)
    clean = CompanySeed(NORTHWIND.name, normalise_domain(NORTHWIND.website),
                        NORTHWIND.industry, NORTHWIND.headcount)
    assign_network(portal, clean, NETWORK_MEMBER, normalise=False)
    assert len(portal.records) == 2
    assert portal.duplicate_count == 0   # two different strings, two records
    assert portal.records[0].domain != portal.records[1].domain


def test_normalising_collapses_that_same_pair_onto_one_record():
    portal = categorise(NORTHWIND, (NETWORK_CLIENT,), normalise=True)
    clean = CompanySeed(NORTHWIND.name, normalise_domain(NORTHWIND.website),
                        NORTHWIND.industry, NORTHWIND.headcount)
    assign_network(portal, clean, NETWORK_MEMBER, normalise=True)
    assert len(portal.records) == 1
    assert portal.records[0].networks == (NETWORK_CLIENT, NETWORK_MEMBER)


def test_a_clean_run_is_reported_as_clean():
    portal = categorise(NORTHWIND, ALL_THREE)
    findings = audit_portal(portal, requested=3)
    assert [f.code for f in findings] == [CODE_CLEAN]
    assert findings[0].severity == SEVERITY_OK


def test_an_unknown_network_is_refused():
    with pytest.raises(ValueError):
        assign_network(Portal(), NORTHWIND, "partner_network")


def test_an_unknown_write_mode_is_refused():
    with pytest.raises(ValueError):
        assign_network(Portal(), NORTHWIND, NETWORK_CLIENT, mode="upsert")


def test_the_modes_are_the_three_the_page_offers():
    assert MODES == (MODE_SEARCH_APPEND, MODE_SEARCH_REPLACE,
                     MODE_BLIND_CREATE)


@pytest.mark.parametrize("mode", MODES)
def test_every_mode_produces_arrow_safe_rows(mode):
    """Arrow refuses a column that mixes types, so every cell is a string."""
    portal = categorise(NORTHWIND, ALL_THREE, mode=mode)
    for row in portal.rows() + portal.write_rows():
        assert all(isinstance(value, str) for value in row.values()), row


def test_the_write_log_has_one_entry_per_assignment():
    portal = categorise(NORTHWIND, ALL_THREE, mode=MODE_BLIND_CREATE)
    assert len(portal.writes) == 3
    assert [write.step for write in portal.writes] == [1, 2, 3]


def test_a_write_body_serialises_as_json():
    portal = categorise(NORTHWIND, ALL_THREE)
    for write in portal.writes:
        assert json.loads(write.pretty_body) == write.body


# ---------------------------------------------------------------------------
# Associations: the prospect, the employer and the Gridwise client
# ---------------------------------------------------------------------------

def test_a_primary_company_writes_two_rows_not_one():
    """Verified HubSpot behaviour: 279 and 1 are both written."""
    mapped = map_prospect()
    hubspot_rows = [row for row in mapped.rows
                    if row.association.category == CATEGORY_HUBSPOT]
    assert len(hubspot_rows) == 2
    assert {row.association.type_id for row in hubspot_rows} == \
        {TYPE_CONTACT_TO_COMPANY, TYPE_CONTACT_TO_COMPANY_PRIMARY}


def test_the_map_holds_three_rows_across_two_companies():
    mapped = map_prospect()
    assert len(mapped.rows) == 3
    assert len({row.to_party.record_id for row in mapped.rows}) == 2


def test_exactly_one_company_is_primary_and_it_is_the_employer():
    mapped = map_prospect()
    assert mapped.primary_company_ids == [EMPLOYER.record_id]


def test_the_client_association_carries_the_custom_label():
    mapped = map_prospect()
    labelled = mapped.labelled_rows
    assert len(labelled) == 1
    assert labelled[0].association.label == LABEL_TARGET_FOR
    assert labelled[0].association.category == CATEGORY_USER
    assert labelled[0].to_party.record_id == GRIDWISE_CLIENT.record_id


def test_the_prospect_is_not_employed_by_the_gridwise_client():
    """The point of the labelled association: two companies, two meanings."""
    mapped = map_prospect()
    assert GRIDWISE_CLIENT.record_id not in mapped.primary_company_ids
    assert EMPLOYER.record_id != GRIDWISE_CLIENT.record_id


def test_the_second_label_can_be_used_instead():
    mapped = map_prospect(label_name=LABEL_INTRODUCED_BY)
    assert mapped.labelled_rows[0].association.label == LABEL_INTRODUCED_BY


def test_resolve_label_returns_the_user_defined_entry_not_the_hubspot_one():
    resolved = resolve_label(LABEL_TARGET_FOR)
    assert resolved.category == CATEGORY_USER
    assert resolved.label == LABEL_TARGET_FOR


def test_a_type_id_is_only_unique_inside_its_category():
    """This is why the label is resolved by name and never hardcoded."""
    ones = [entry for entry in PORTAL_LABELS if entry.type_id == 1]
    assert len(ones) == 2
    assert {entry.category for entry in ones} == {CATEGORY_HUBSPOT,
                                                  CATEGORY_USER}


def test_an_unknown_label_is_refused_rather_than_guessed():
    with pytest.raises(KeyError):
        resolve_label("Sponsored By")


def test_hardcoding_the_type_id_marks_a_second_company_primary():
    mapped = map_prospect(hardcode_type_id=True)
    assert len(mapped.primary_company_ids) == 2
    assert mapped.labelled_rows == []


def test_hardcoding_raises_the_ambiguity_finding():
    mapped = map_prospect(hardcode_type_id=True)
    codes = {f.code for f in mapped.findings}
    assert CODE_AMBIGUOUS_TYPE in codes
    assert all(f.severity == SEVERITY_CRITICAL for f in mapped.findings)


def test_a_resolved_map_is_reported_clean():
    mapped = map_prospect()
    assert [f.code for f in mapped.findings] == [CODE_MAP_CLEAN]


def test_the_payload_names_the_category_beside_every_type_id():
    payload = map_prospect().payload()
    assert len(payload["inputs"]) == 3
    for entry in payload["inputs"]:
        for spec in entry["types"]:
            assert spec["associationCategory"] in (CATEGORY_HUBSPOT,
                                                   CATEGORY_USER)
            assert isinstance(spec["associationTypeId"], int)


def test_the_payload_starts_from_the_prospect_every_time():
    payload = map_prospect().payload()
    assert {entry["from"]["id"] for entry in payload["inputs"]} == \
        {PROSPECT.record_id}


def test_the_payload_serialises_as_json():
    mapped = map_prospect()
    assert json.loads(mapped.pretty_payload()) == mapped.payload()


def test_the_association_table_is_arrow_safe():
    for row in map_prospect().table_rows():
        assert all(isinstance(value, str) for value in row.values()), row


# ---------------------------------------------------------------------------
# The LinkedIn deduplication ledger
# ---------------------------------------------------------------------------

def message(**overrides) -> LinkedInMessage:
    base = dict(message_urn="urn:li:message:1", conversation_urn="urn:li:c:1",
                sender="Dara Mensah", recipient="Gridwise",
                sent_at=CONNECTED_AT + HOUR, body="Hello there")
    base.update(overrides)
    return LinkedInMessage(**base)


def test_a_fingerprint_is_sixteen_hex_characters():
    digest = fingerprint(message())
    assert len(digest) == 16
    assert all(char in "0123456789abcdef" for char in digest)


def test_the_same_message_hashes_the_same_every_time():
    assert fingerprint(message()) == fingerprint(message())


def test_whitespace_does_not_change_the_fingerprint():
    """A second sync rewraps the body. A hash that moves lets the copy in."""
    assert fingerprint(message(body="Hello  there")) == fingerprint(message())
    assert fingerprint(message(body="Hello\r\n there")) == fingerprint(message())
    assert fingerprint(message(body="  Hello there  ")) == fingerprint(message())


@pytest.mark.parametrize("field,value", [
    ("message_urn", "urn:li:message:2"),
    ("conversation_urn", "urn:li:c:2"),
    ("sent_at", CONNECTED_AT + 2 * HOUR),
    ("body", "Hello there!"),
])
def test_anything_that_actually_differs_hashes_apart(field, value):
    assert fingerprint(message(**{field: value})) != fingerprint(message())


def test_two_identical_texts_in_different_messages_still_hash_apart():
    first = message(message_urn="urn:li:message:10")
    second = message(message_urn="urn:li:message:11")
    assert first.body == second.body
    assert fingerprint(first) != fingerprint(second)


def test_the_canonical_string_carries_all_four_parts():
    text = canonical(message())
    assert text.split("|") == ["urn:li:c:1", "urn:li:message:1",
                               str(CONNECTED_AT + HOUR), "Hello there"]


def test_the_sample_payload_has_two_historical_and_four_later_deliveries():
    messages = sample_messages()
    assert len(messages) == 6
    assert sum(1 for m in messages if m.sent_at < CONNECTED_AT) == 2


def test_the_ledger_logs_only_what_is_new_and_unseen():
    ledger = process_payload(sample_messages())
    assert ledger.logged == 2
    assert ledger.count(SKIPPED_HISTORICAL) == 2
    assert ledger.count(SKIPPED_SEEN) == 2


def test_a_redelivered_message_is_refused_byte_for_byte_and_rewrapped():
    ledger = process_payload(sample_messages())
    refused = [r for r in ledger.results if r.outcome == SKIPPED_SEEN]
    assert len(refused) == 2
    assert refused[0].digest != refused[1].digest   # two different messages
    logged = {r.digest for r in ledger.results if r.logged}
    assert {r.digest for r in refused} == logged


def test_connecting_a_second_time_writes_nothing_at_all():
    """The real test of a ledger: run the whole payload through it twice."""
    ledger = process_payload(sample_messages())
    first_pass = ledger.logged
    process_payload(sample_messages(), ledger=ledger)
    assert ledger.logged == first_pass
    second_pass = ledger.results[len(sample_messages()):]
    assert all(r.outcome == SKIPPED_SEEN for r in second_pass)


def test_a_skipped_historical_message_is_still_recorded():
    """Otherwise the second sync would treat it as new and log it."""
    ledger = process_payload(sample_messages())
    historical = [r for r in ledger.results
                  if r.outcome == SKIPPED_HISTORICAL]
    assert all(r.digest in ledger.seen for r in historical)


def test_turning_the_cutoff_off_backfills_the_whole_history():
    ledger = process_payload(sample_messages(), skip_history=False)
    assert ledger.logged == 4
    assert ledger.count(SKIPPED_HISTORICAL) == 0
    assert ledger.count(SKIPPED_SEEN) == 2


def test_the_backfill_is_reported_as_critical():
    ledger = process_payload(sample_messages(), skip_history=False)
    findings = {f.code: f for f in audit_ledger(ledger, skip_history=False)}
    assert CODE_BACKFILL in findings
    assert findings[CODE_BACKFILL].severity == SEVERITY_CRITICAL


def test_the_guarded_run_reports_the_refusals_and_the_cutoff():
    ledger = process_payload(sample_messages())
    codes = {f.code for f in audit_ledger(ledger)}
    assert codes == {CODE_REDELIVERY, CODE_LEDGER_CLEAN}
    assert CODE_BACKFILL not in codes


def test_the_fingerprint_is_checked_before_the_date():
    """A message already on the timeline is never written again, ever."""
    old = message(sent_at=CONNECTED_AT - DAY)
    ledger = Ledger()
    process_payload([old], ledger=ledger, skip_history=False)
    assert ledger.results[-1].outcome == LOGGED
    process_payload([old], ledger=ledger, skip_history=False)
    assert ledger.results[-1].outcome == SKIPPED_SEEN


def test_an_enrolment_is_counted_for_every_activity_written():
    for skip in (True, False):
        ledger = process_payload(sample_messages(), skip_history=skip)
        assert ledger.workflow_triggers == ledger.logged
    assert process_payload(sample_messages(), skip_history=False).logged > \
        process_payload(sample_messages()).logged


def test_the_comparison_puts_the_two_runs_side_by_side():
    rows = {row["Measure"]: row for row in ledger_comparison(sample_messages())}
    written = rows["Activities written to the timeline"]
    assert written["Ledger on"] == "2"
    assert written["Ledger off"] == "4"
    assert rows["Messages received"]["Ledger on"] == "6"
    assert rows["Messages received"]["Ledger off"] == "6"


def test_the_comparison_is_arrow_safe():
    for row in ledger_comparison(sample_messages()):
        assert all(isinstance(value, str) for value in row.values()), row


def test_the_ledger_rows_are_arrow_safe():
    for row in process_payload(sample_messages()).rows():
        assert all(isinstance(value, str) for value in row.values()), row


@pytest.mark.parametrize("count", range(1, 7))
def test_every_prefix_of_the_payload_is_processed(count):
    ledger = process_payload(sample_messages()[:count])
    assert len(ledger.results) == count
    assert ledger.logged + ledger.skipped == count


@pytest.mark.parametrize("source", [SOURCE_HUBLEAD, SOURCE_SURFE])
def test_either_integration_produces_the_same_decisions(source):
    ledger = process_payload(sample_messages(), source=source)
    assert ledger.source == source
    assert ledger.logged == 2


def test_the_stamp_is_relative_to_the_connection_not_to_a_wall_clock():
    assert stamp(CONNECTED_AT) == "at connection"
    assert stamp(CONNECTED_AT + 2 * HOUR) == "2h after connection"
    assert stamp(CONNECTED_AT - 14 * DAY) == "14d before connection"
    assert stamp(CONNECTED_AT + 600) == "10m after connection"


def test_the_incoming_payload_is_valid_json_in_the_shape_they_post():
    body = json.loads(payload_json(sample_messages(), source=SOURCE_SURFE))
    assert body["source"] == SOURCE_SURFE
    assert body["connectedAt"] == CONNECTED_AT
    assert len(body["messages"]) == 6
    assert set(body["messages"][0]) == {"id", "conversationId", "from", "to",
                                        "sentAt", "body"}


def test_an_empty_payload_is_handled_rather_than_crashing():
    ledger = process_payload([])
    assert ledger.results == []
    assert ledger.logged == 0
    assert json.loads(payload_json([]))["messages"] == []


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------

def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_no_finding_uses_a_dash_character():
    findings = list(audit_portal(categorise(NORTHWIND, ALL_THREE,
                                            mode=MODE_BLIND_CREATE), 3))
    findings += list(audit_portal(categorise(NORTHWIND, ALL_THREE,
                                             mode=MODE_SEARCH_REPLACE), 3))
    findings += list(map_prospect(hardcode_type_id=True).findings)
    findings += list(map_prospect().findings)
    findings += list(audit_ledger(process_payload(sample_messages(),
                                                  skip_history=False),
                                  skip_history=False))
    findings += list(audit_ledger(process_payload(sample_messages())))
    assert findings
    for finding in findings:
        text = f"{finding.title} {finding.detail} {finding.fix}"
        assert "—" not in text, finding.code
        assert "–" not in text, finding.code


def test_the_core_imports_no_streamlit():
    source = (Path(__file__).resolve().parents[1]
              / "tools" / "hubspot_b2b_network_architect" / "core.py").read_text()
    assert "streamlit" not in source
