"""Tests for the GHL Real Estate Architecture Console.

Three properties carry this file.

Deduplication has to work in both directions. Two spellings of one address
must produce one key, and a different unit at the same street address must
produce a different one. Only testing the merge direction passes a
normaliser that strips the unit, which is the bug that merges two households.

An envelope is complete only when every signer has completed, and one
decline ends it whatever anybody else did. Both are checked across the whole
space of signer combinations rather than at a chosen point, because marking
complete on the first signature is the bug that moves a deal to under
contract before it is.

The reporting gap is arithmetic. Both totals equal the sum of their own rows
and the property total plus the overstatement equals the contact total, so
the double counting claim is checked rather than asserted.

Pass marker: pytest reports all tests passed, exit 0.
Fail marker: any failure or error line, exit non zero.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from itertools import product as combinations
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.ghl_real_estate_architecture_console.core import (  # noqa: E402
    DEDUPE_DUPLICATE,
    DEDUPE_NEW,
    DEDUPE_REJECTED,
    ENGINE_VERSION,
    ENVELOPE_COMPLETED,
    ENVELOPE_DECLINED,
    ENVELOPE_IN_PROGRESS,
    ENVELOPE_NOT_SENT,
    PIPELINE_STAGE,
    SAMPLE_ADDRESSES,
    SAMPLE_CONTACTS,
    SAMPLE_PROPERTIES,
    SAMPLE_SIGNERS,
    SEVERITY_CRITICAL,
    SIGNER_COMPLETED,
    SIGNER_DECLINED,
    SIGNER_NOT_SENT,
    SIGNER_SENT,
    SIGNER_STATES,
    SIGNER_VIEWED,
    generate_reporting_ledger,
    money,
    normalize_and_dedupe_lead,
    simulate_multi_signer_contract,
)

APT_3B_MESSY = "1428 north maple street apartment 3b, Austin, TX 78704"
APT_3B_CLEAN = "1428 N Maple St Apt 3B, Austin, TX 78704"
APT_4 = "1428 N Maple St Apt 4, Austin, TX 78704"
NO_UNIT = "1428 N Maple St, Austin, TX 78704"


def key_of(address: str) -> str:
    return normalize_and_dedupe_lead(address).dedupe_key


# ---------------------------------------------------------------------------
# Address normalisation
# ---------------------------------------------------------------------------


def test_two_spellings_of_one_address_produce_one_key():
    assert key_of(APT_3B_MESSY) == key_of(APT_3B_CLEAN)
    assert (normalize_and_dedupe_lead(APT_3B_MESSY).standardised
            == normalize_and_dedupe_lead(APT_3B_CLEAN).standardised)


def test_a_different_unit_is_a_different_property():
    """Only testing the merge direction passes a normaliser that strips the
    unit, which merges two households into one record."""
    assert key_of(APT_3B_CLEAN) != key_of(APT_4)
    assert key_of(APT_3B_CLEAN) != key_of(NO_UNIT)
    assert key_of(APT_4) != key_of(NO_UNIT)
    assert len({key_of(APT_3B_CLEAN), key_of(APT_4), key_of(NO_UNIT)}) == 3


def test_the_unit_is_carried_into_the_key_itself():
    keyed = normalize_and_dedupe_lead(APT_3B_CLEAN)
    assert keyed.has_unit
    assert keyed.unit_designator == "APT"
    assert keyed.unit_number == "3B"
    assert "APT" in keyed.dedupe_key
    assert "3B" in keyed.dedupe_key


def test_every_unit_designator_spelling_normalises_to_one_form():
    forms = ("1 Main St Apartment 5, Austin, TX 78704",
             "1 Main St APT 5, Austin, TX 78704",
             "1 Main St Apt. 5, Austin, TX 78704",
             "1 Main St #5, Austin, TX 78704",
             "1 Main St # 5, Austin, TX 78704")
    keys = {key_of(form) for form in forms}
    assert len(keys) == 1, keys


def test_suite_and_apartment_are_not_the_same_designator():
    """They are different in the standard and conflating them would merge a
    commercial suite with a flat."""
    suite = normalize_and_dedupe_lead("1 Main St Suite 5, Austin, TX 78704")
    apartment = normalize_and_dedupe_lead("1 Main St Apt 5, Austin, TX 78704")
    assert suite.unit_designator == "STE"
    assert apartment.unit_designator == "APT"
    assert suite.dedupe_key != apartment.dedupe_key


def test_street_suffixes_and_directionals_are_abbreviated():
    pairs = (("100 Oak Street", "ST"), ("100 Oak Avenue", "AVE"),
             ("100 Oak Boulevard", "BLVD"), ("100 Oak Road", "RD"),
             ("100 Oak Drive", "DR"), ("100 Oak Lane", "LN"),
             ("100 Oak Parkway", "PKWY"))
    for line, expected in pairs:
        row = normalize_and_dedupe_lead(f"{line}, Austin, TX 78704")
        assert row.street.endswith(expected), (line, row.street)
    directional = normalize_and_dedupe_lead("100 North Oak St, Austin, TX 78704")
    assert directional.street.startswith("N ")


def test_an_unknown_suffix_is_kept_rather_than_guessed_at():
    """Inventing an abbreviation is how two different streets become one."""
    row = normalize_and_dedupe_lead("100 Kestrel Quillway, Austin, TX 78704")
    assert "QUILLWAY" in row.street
    assert row.parsed


def test_the_last_line_carries_state_and_zip_without_a_comma():
    row = normalize_and_dedupe_lead(APT_3B_CLEAN)
    assert row.standardised.endswith("TX 78704")
    assert "TX, 78704" not in row.standardised


def test_the_standardised_form_is_upper_case_and_unpunctuated():
    row = normalize_and_dedupe_lead(APT_3B_MESSY)
    line_one = row.standardised.split(",")[0]
    assert line_one == line_one.upper()
    assert "." not in line_one


def test_a_missing_state_or_zip_is_flagged_without_being_invented():
    row = normalize_and_dedupe_lead("1428 N Maple St Apt 3B")
    assert row.state == ""
    assert row.zip5 == ""
    codes = {f.code for f in row.findings}
    assert "ADR-NO-STATE" in codes
    assert "ADR-NO-ZIP" in codes


def test_the_zip_plus_four_is_never_manufactured():
    """A licensed USPS data set assigns it. This does not have one."""
    without = normalize_and_dedupe_lead(APT_3B_CLEAN)
    assert without.zip4 == ""
    assert any(f.code == "ADR-NOT-CASS" for f in without.findings)
    supplied = normalize_and_dedupe_lead(
        "1428 N Maple St, Austin, TX 78704-1234")
    assert supplied.zip4 == "1234"


def test_an_empty_or_unparseable_address_creates_no_record():
    for bad in ("", "   ", "no idea", "Austin"):
        row = normalize_and_dedupe_lead(bad)
        assert row.dedupe_status == DEDUPE_REJECTED, bad
        assert not row.parsed, bad
        assert row.severity == SEVERITY_CRITICAL, bad


def test_a_known_key_is_reported_as_a_duplicate():
    first = normalize_and_dedupe_lead(APT_3B_MESSY)
    assert first.dedupe_status == DEDUPE_NEW
    second = normalize_and_dedupe_lead(APT_3B_CLEAN, (first.dedupe_key,))
    assert second.dedupe_status == DEDUPE_DUPLICATE
    assert second.matched_existing == first.dedupe_key
    other = normalize_and_dedupe_lead(APT_4, (first.dedupe_key,))
    assert other.dedupe_status == DEDUPE_NEW


def test_the_sample_list_produces_four_distinct_properties():
    keys = []
    for address in SAMPLE_ADDRESSES:
        row = normalize_and_dedupe_lead(address, tuple(keys))
        if row.parsed and row.dedupe_key not in keys:
            keys.append(row.dedupe_key)
    assert len(SAMPLE_ADDRESSES) == 5
    assert len(keys) == 4, "exactly one of the five is a duplicate"


# ---------------------------------------------------------------------------
# Multi signer contracts
# ---------------------------------------------------------------------------


def _signers(*statuses):
    return [{"name": f"Signer {i}", "role": "Party", "status": s}
            for i, s in enumerate(statuses, start=1)]


def test_an_envelope_is_complete_only_when_every_signer_is():
    """Checked across the whole space of three signer combinations."""
    for combo in combinations(SIGNER_STATES, repeat=3):
        envelope = simulate_multi_signer_contract("P-1", _signers(*combo))
        all_done = all(s == SIGNER_COMPLETED for s in combo)
        any_declined = any(s == SIGNER_DECLINED for s in combo)
        assert envelope.is_complete == (all_done and not any_declined), combo
        if all_done:
            assert envelope.envelope_status == ENVELOPE_COMPLETED, combo


def test_one_decline_ends_the_envelope_whatever_anybody_else_did():
    for combo in combinations(SIGNER_STATES, repeat=3):
        if SIGNER_DECLINED not in combo:
            continue
        envelope = simulate_multi_signer_contract("P-1", _signers(*combo))
        assert envelope.envelope_status == ENVELOPE_DECLINED, combo
        assert not envelope.is_complete, combo
        assert envelope.is_terminal, combo


def test_one_signature_does_not_move_the_pipeline_to_under_contract():
    """The bug this section exists to prevent."""
    partial = simulate_multi_signer_contract(
        "P-1", _signers(SIGNER_COMPLETED, SIGNER_SENT, SIGNER_VIEWED))
    assert partial.envelope_status == ENVELOPE_IN_PROGRESS
    assert partial.pipeline_stage != PIPELINE_STAGE[ENVELOPE_COMPLETED]
    assert partial.severity == SEVERITY_CRITICAL
    assert any(f.code == "ENV-PARTIAL" for f in partial.findings)


def test_the_signer_counts_always_reconcile():
    for combo in combinations(SIGNER_STATES, repeat=3):
        envelope = simulate_multi_signer_contract("P-1", _signers(*combo))
        assert envelope.reconciles, combo
        assert (envelope.completed_count + envelope.outstanding_count
                + envelope.declined_count == 3), combo


def test_the_derived_status_does_not_depend_on_signing_order():
    base = (SIGNER_COMPLETED, SIGNER_SENT, SIGNER_DECLINED)
    statuses = {simulate_multi_signer_contract(
        "P-1", _signers(*order)).envelope_status
        for order in (base, tuple(reversed(base)),
                      (base[1], base[2], base[0]))}
    assert len(statuses) == 1


def test_an_untouched_envelope_reads_as_not_sent():
    envelope = simulate_multi_signer_contract(
        "P-1", _signers(SIGNER_NOT_SENT, SIGNER_NOT_SENT))
    assert envelope.envelope_status == ENVELOPE_NOT_SENT
    assert envelope.pipeline_stage == PIPELINE_STAGE[ENVELOPE_NOT_SENT]
    assert not envelope.is_terminal


def test_every_envelope_state_maps_to_exactly_one_stage():
    stages = list(PIPELINE_STAGE.values())
    assert len(stages) == len(set(stages)), "two states share a stage"
    assert set(PIPELINE_STAGE) == {ENVELOPE_NOT_SENT, ENVELOPE_IN_PROGRESS,
                                   ENVELOPE_COMPLETED, ENVELOPE_DECLINED}


def test_only_completed_and_declined_are_terminal():
    for combo in combinations(SIGNER_STATES, repeat=2):
        envelope = simulate_multi_signer_contract("P-1", _signers(*combo))
        expected = envelope.envelope_status in (ENVELOPE_COMPLETED,
                                                ENVELOPE_DECLINED)
        assert envelope.is_terminal == expected, combo


def test_every_envelope_says_it_belongs_to_the_property():
    envelope = simulate_multi_signer_contract("P-1001", SAMPLE_SIGNERS)
    assert any(f.code == "ENV-PROPERTY" for f in envelope.findings)


def test_an_envelope_accepts_tuples_as_well_as_dicts():
    as_tuples = simulate_multi_signer_contract(
        "P-1", [("Dana", "Seller", SIGNER_COMPLETED),
                ("Marcus", "Buyer", SIGNER_SENT)])
    assert as_tuples.completed_count == 1
    assert as_tuples.signers[0].name == "Dana"


def test_impossible_envelopes_raise():
    with pytest.raises(ValueError):
        simulate_multi_signer_contract("", SAMPLE_SIGNERS)
    with pytest.raises(ValueError):
        simulate_multi_signer_contract("P-1", [])
    with pytest.raises(ValueError):
        simulate_multi_signer_contract("P-1", _signers("Maybe"))
    with pytest.raises(ValueError):
        simulate_multi_signer_contract(
            "P-1", [{"name": "", "status": SIGNER_SENT}])


# ---------------------------------------------------------------------------
# The reporting ledger
# ---------------------------------------------------------------------------


def test_both_totals_equal_the_sum_of_their_own_rows():
    ledger = generate_reporting_ledger(SAMPLE_CONTACTS, SAMPLE_PROPERTIES)
    assert ledger.reconciles
    assert ledger.property_model_total == money(
        sum(r.property_model_value for r in ledger.rows))
    assert ledger.contact_model_total == money(
        sum(r.contact_model_value for r in ledger.rows))


def test_the_property_model_counts_each_deal_exactly_once():
    ledger = generate_reporting_ledger(SAMPLE_CONTACTS, SAMPLE_PROPERTIES)
    assert ledger.property_model_total == ledger.true_pipeline_value
    assert ledger.property_model_total == money(
        sum(Decimal(str(p["deal_value"])) for p in SAMPLE_PROPERTIES))


def test_the_contact_model_counts_each_deal_once_per_related_contact():
    ledger = generate_reporting_ledger(SAMPLE_CONTACTS, SAMPLE_PROPERTIES)
    for row in ledger.rows:
        assert row.contact_model_value == money(
            row.deal_value * row.contact_count), row.property_id
        assert row.overstatement == money(
            row.deal_value * (row.contact_count - 1)), row.property_id


def test_the_gap_is_exactly_the_property_total_subtracted_from_the_contact_total():
    ledger = generate_reporting_ledger(SAMPLE_CONTACTS, SAMPLE_PROPERTIES)
    assert ledger.overstatement == money(
        ledger.contact_model_total - ledger.property_model_total)
    assert money(ledger.property_model_total + ledger.overstatement) \
        == ledger.contact_model_total
    assert ledger.overstatement > 0
    assert any(f.code == "LED-DOUBLE-COUNT" and f.severity == SEVERITY_CRITICAL
               for f in ledger.findings)


def test_one_contact_per_property_is_the_only_case_where_the_models_agree():
    single = [{"id": "P-1", "deal_value": "100000", "contacts": ("C-1",)},
              {"id": "P-2", "deal_value": "200000", "contacts": ("C-2",)}]
    ledger = generate_reporting_ledger(SAMPLE_CONTACTS, single)
    assert ledger.overstatement == money(0)
    assert ledger.contact_model_total == ledger.property_model_total
    assert any(f.code == "LED-NO-GAP" for f in ledger.findings)


def test_adding_one_contact_to_a_property_adds_its_whole_value_to_the_gap():
    before = generate_reporting_ledger(
        SAMPLE_CONTACTS,
        [{"id": "P-1", "deal_value": "400000", "contacts": ("C-1",)}])
    after = generate_reporting_ledger(
        SAMPLE_CONTACTS,
        [{"id": "P-1", "deal_value": "400000", "contacts": ("C-1", "C-2")}])
    assert after.overstatement - before.overstatement == money("400000")
    assert after.property_model_total == before.property_model_total


def test_a_property_with_no_contacts_reports_nothing_on_the_contact_model():
    """The mirror failure: a deal nobody is linked to disappears entirely."""
    ledger = generate_reporting_ledger(
        SAMPLE_CONTACTS,
        [{"id": "P-1", "deal_value": "500000", "contacts": ()}])
    assert ledger.contact_model_total == money(0)
    assert ledger.property_model_total == money("500000")


def test_a_property_naming_an_unknown_contact_raises():
    with pytest.raises(ValueError):
        generate_reporting_ledger(
            SAMPLE_CONTACTS,
            [{"id": "P-1", "deal_value": "100", "contacts": ("C-999",)}])


def test_impossible_ledgers_raise():
    with pytest.raises(ValueError):
        generate_reporting_ledger(SAMPLE_CONTACTS, [])
    with pytest.raises(ValueError):
        generate_reporting_ledger(SAMPLE_CONTACTS,
                                  [{"id": "", "deal_value": "1"}])
    with pytest.raises(ValueError):
        generate_reporting_ledger(SAMPLE_CONTACTS,
                                  [{"id": "P-1", "deal_value": "-5"}])


def test_every_ledger_warns_that_changing_this_later_is_a_migration():
    ledger = generate_reporting_ledger(SAMPLE_CONTACTS, SAMPLE_PROPERTIES)
    assert any(f.code == "LED-MIGRATION" for f in ledger.findings)


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "ghl_real_estate_architecture_console"
              / "core.py").read_text()
    assert "import streamlit" not in source
    probe = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.ghl_real_estate_architecture_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT)
    )
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [f.title + f.detail + f.fix
         for address in list(SAMPLE_ADDRESSES) + ["", "nonsense"]
         for f in normalize_and_dedupe_lead(address).findings]
        + [envelope.headline + " ".join(f.title + f.detail + f.fix
                                        for f in envelope.findings)
           for combo in (("Completed", "Sent"), ("Declined", "Completed"),
                         ("Completed", "Completed"), ("Not Sent", "Not Sent"))
           for envelope in (simulate_multi_signer_contract("P-1",
                                                           _signers(*combo)),)]
        + [ledger.headline + " ".join(f.title + f.detail + f.fix
                                      for f in ledger.findings)
           for ledger in (generate_reporting_ledger(SAMPLE_CONTACTS,
                                                    SAMPLE_PROPERTIES),)]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools
    tools = all_tools()
    entry = next(t for t in tools
                 if t.key == "ghl-real-estate-architecture-console")
    assert entry.title == "GHL Real Estate Architecture Console"
    icons = [t.icon for t in tools]
    assert icons.count(entry.icon) == 1, "this icon is already used by another tool"
    assert len(entry.tagline) > 30
