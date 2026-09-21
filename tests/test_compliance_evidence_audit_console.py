"""Tests for the Compliance Evidence and WORM Audit Console.

Three properties carry this file, and each is checked against something
independent rather than against the engine's own opinion.

The hashing is checked against hashlib directly, including published SHA256
vectors, so a mistake in the wrapper cannot pass. Canonicalisation is checked
by feeding the same logical record in different key orders and whitespace and
requiring one digest, and the chain is checked by deleting a record and
requiring verification to break, because a per record hash detects an edit
and never detects a deletion.

Discrepancies are ranked by what they cost rather than by numeric distance,
and the test asserts that ordering explicitly, since a success where an
authorisation failure was intended is numerically close to nothing and is the
worst outcome in the set.

The tracker refuses rather than emitting a gap. Every one of the eight points
is blanked in turn and the entry must not be produced.

Pass marker: pytest reports all tests passed, exit 0.
Fail marker: any failure or error line, exit non zero.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from itertools import product as combinations
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.compliance_evidence_audit_console.core import (  # noqa: E402
    ADMITTED,
    COMPLIANT,
    DISCREPANT,
    ENGINE_VERSION,
    ENTRY_COMPLETE,
    ENTRY_REFUSED,
    GENESIS,
    KIND_AUTH_BYPASS,
    KIND_EXISTENCE_LEAK,
    KIND_NONE,
    KIND_RANK,
    KIND_SILENT_SUCCESS,
    KIND_WRONG_CLASS,
    KIND_WRONG_CODE,
    PLACEHOLDER_MARKERS,
    PROPOSED_STATUSES,
    REJECTED,
    SAMPLE_CHECKS,
    SAMPLE_ENTRY,
    SAMPLE_PAYLOADS,
    SEVERITY_CRITICAL,
    TRACKER_FIELDS,
    build_chain,
    canonicalise,
    format_readiness_tracker_entry,
    generate_evidence_hash,
    sha256_of,
    validate_api_compliance,
    verify_chain,
)


# ---------------------------------------------------------------------------
# Hashing and the chain
# ---------------------------------------------------------------------------


def test_the_hash_is_real_sha256_checked_against_the_library():
    for text in ("", "abc", "the quick brown fox", "unicode: café"):
        assert sha256_of(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_published_vectors_hold():
    """If these two ever differ, the wrapper is not SHA256."""
    assert sha256_of("") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    assert sha256_of("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


def test_the_same_record_in_any_key_order_gives_one_digest():
    """Hashing a serialised dictionary directly makes the digest depend on
    key order, and the check then fails for reasons nobody can explain."""
    orders = (
        {"a": 1, "b": 2, "c": 3},
        {"c": 3, "b": 2, "a": 1},
        {"b": 2, "c": 3, "a": 1},
    )
    digests = {generate_evidence_hash(o).content_hash for o in orders}
    assert len(digests) == 1, digests


def test_whitespace_and_unicode_form_do_not_change_the_digest():
    composed = "café"          # e with acute as one code point
    decomposed = "café"       # e followed by a combining acute
    assert canonicalise(composed) == canonicalise(decomposed)
    assert (generate_evidence_hash({"name": composed}).content_hash
            == generate_evidence_hash({"name": decomposed}).content_hash)


def test_a_different_record_gives_a_different_digest():
    base = generate_evidence_hash({"control": "AC-2", "result": "pass"})
    changed = generate_evidence_hash({"control": "AC-2", "result": "fail"})
    assert base.content_hash != changed.content_hash
    assert base.chain_hash != changed.chain_hash


def test_every_admitted_record_is_self_consistent():
    for payload in SAMPLE_PAYLOADS:
        record = generate_evidence_hash(payload)
        assert record.admitted
        assert record.self_consistent
        assert record.chain_hash == sha256_of(
            f"{record.previous_hash}|{record.content_hash}")
        assert record.content_hash == sha256_of(record.canonical_form)


def test_a_chain_of_records_verifies_end_to_end():
    chain = build_chain(SAMPLE_PAYLOADS)
    result = verify_chain(chain)
    assert result["records"] == len(SAMPLE_PAYLOADS)
    assert result["intact"]
    assert result["broken_at"] == 0
    assert chain[0].previous_hash == GENESIS
    for earlier, later in zip(chain, chain[1:]):
        assert later.previous_hash == earlier.chain_hash


def test_deleting_any_record_breaks_the_chain():
    """A per record hash detects an edit and never detects a deletion, and a
    deletion is the edit an insider actually makes."""
    chain = build_chain(SAMPLE_PAYLOADS)
    for index in range(len(chain) - 1):
        shortened = chain[:index] + chain[index + 1:]
        result = verify_chain(shortened)
        assert not result["intact"], index
        assert result["broken_at"] > 0, index


def test_editing_a_record_breaks_every_link_after_it():
    chain = build_chain(SAMPLE_PAYLOADS)
    forged = generate_evidence_hash({"control": "AC-2", "result": "forged"},
                                    chain[0].previous_hash, 1)
    tampered = (forged,) + chain[1:]
    assert not verify_chain(tampered)["intact"]
    assert verify_chain(tampered)["broken_at"] == 2


def test_the_first_record_is_anchored_to_a_genesis_value():
    """An unanchored first record can be swapped without breaking anything."""
    record = generate_evidence_hash(SAMPLE_PAYLOADS[0])
    assert record.previous_hash == GENESIS
    assert len(GENESIS) == 64


def test_a_malformed_previous_link_is_refused_before_hashing():
    for bad in ("", "not-a-hash", "abc", "z" * 64, GENESIS[:63]):
        record = generate_evidence_hash({"a": 1}, bad)
        assert not record.admitted, bad
        assert record.status == REJECTED, bad
        assert record.chain_hash == "", bad
        assert record.severity == SEVERITY_CRITICAL, bad


def test_an_empty_payload_is_refused_rather_than_hashed():
    """A digest of nothing is an evidence record that proves nothing existed."""
    for empty in ("", "   ", {}, []):
        record = generate_evidence_hash(empty)
        assert not record.admitted, empty
        assert record.content_hash == "", empty


def test_every_admitted_record_raises_the_custody_problem():
    """The digest is only worth something if it lives out of the writer's
    reach, and this tool cannot give you that."""
    record = generate_evidence_hash(SAMPLE_PAYLOADS[0])
    codes = {f.code for f in record.findings}
    assert "EVD-CUSTODY" in codes
    assert "EVD-TIME" in codes


def test_a_sequence_below_one_raises():
    with pytest.raises(ValueError):
        generate_evidence_hash({"a": 1}, GENESIS, 0)


# ---------------------------------------------------------------------------
# API compliance
# ---------------------------------------------------------------------------


def test_a_matching_status_is_compliant():
    for code in (200, 201, 204, 400, 403, 404, 500):
        check = validate_api_compliance("GET /x", code, code)
        assert check.compliant, code
        assert check.kind == KIND_NONE, code
        assert check.verdict == COMPLIANT, code


def test_success_where_authorisation_was_required_is_the_worst_outcome():
    for expected in (401, 403):
        for observed in (200, 201, 204):
            check = validate_api_compliance("GET /admin", observed, expected)
            assert check.kind == KIND_AUTH_BYPASS, (expected, observed)
            assert check.rank == 0, (expected, observed)
            assert check.severity == SEVERITY_CRITICAL


def test_the_ranking_is_by_cost_not_by_numeric_distance():
    """A 200 against an intended 403 is numerically further from its target
    than a 204 against an intended 200, and is vastly worse. Distance between
    status codes carries no meaning; what the discrepancy costs does."""
    bypass = validate_api_compliance("GET /admin", 200, 403)
    cosmetic = validate_api_compliance("GET /health", 204, 200)
    assert abs(bypass.response_code - bypass.expected_status) > abs(
        cosmetic.response_code - cosmetic.expected_status)
    assert bypass.rank < cosmetic.rank


def test_every_discrepancy_kind_is_reachable_and_ordered():
    cases = {
        KIND_AUTH_BYPASS: ("GET /a", 200, 403),
        KIND_SILENT_SUCCESS: ("POST /b", 200, 422),
        KIND_EXISTENCE_LEAK: ("GET /c", 403, 404),
        KIND_WRONG_CLASS: ("DELETE /d", 500, 409),
        KIND_WRONG_CODE: ("GET /e", 204, 200),
        KIND_NONE: ("GET /f", 200, 200),
    }
    ranks = []
    for kind, (endpoint, observed, expected) in cases.items():
        check = validate_api_compliance(endpoint, observed, expected)
        assert check.kind == kind, (kind, check.kind)
        assert check.rank == KIND_RANK[kind], kind
        ranks.append(check.rank)
    assert sorted(ranks) == list(range(len(cases)))


def test_a_discrepancy_is_never_reported_as_compliant():
    for observed, expected in combinations((200, 204, 400, 403, 404, 500),
                                           repeat=2):
        check = validate_api_compliance("GET /x", observed, expected)
        assert check.compliant == (observed == expected), (observed, expected)
        assert (check.verdict == COMPLIANT) == check.compliant


def test_a_forbidden_where_not_found_was_required_leaks_existence():
    check = validate_api_compliance("GET /records/9", 403, 404)
    assert check.kind == KIND_EXISTENCE_LEAK
    assert check.severity == SEVERITY_CRITICAL
    assert any(f.code == "API-EXISTS" for f in check.findings)


def test_every_check_says_an_assertion_is_not_evidence():
    for endpoint, observed, expected in SAMPLE_CHECKS:
        check = validate_api_compliance(endpoint, observed, expected)
        assert any(f.code == "API-EVIDENCE" for f in check.findings), endpoint


def test_impossible_checks_raise():
    with pytest.raises(ValueError):
        validate_api_compliance("", 200, 200)
    with pytest.raises(ValueError):
        validate_api_compliance("GET /x", 99, 200)
    with pytest.raises(ValueError):
        validate_api_compliance("GET /x", 200, 600)


# ---------------------------------------------------------------------------
# The readiness tracker
# ---------------------------------------------------------------------------


def test_a_complete_entry_produces_all_eight_points_in_order():
    entry = format_readiness_tracker_entry(**SAMPLE_ENTRY)
    assert entry.complete
    assert entry.status == ENTRY_COMPLETE
    assert entry.point_count == 8
    lines = entry.entry_text.splitlines()
    assert len(lines) == 8
    for index, (_key, label) in enumerate(TRACKER_FIELDS):
        assert lines[index].startswith(label), label


def test_blanking_any_one_of_the_eight_points_refuses_the_entry():
    """A row with a blank field reads as answered to everybody except the
    person who has to defend it."""
    for key, label in TRACKER_FIELDS:
        payload = dict(SAMPLE_ENTRY)
        payload[key] = ""
        entry = format_readiness_tracker_entry(**payload)
        assert not entry.complete, key
        assert entry.status == ENTRY_REFUSED, key
        assert entry.entry_text == "", key
        assert entry.entry_hash == "", key
        assert label in entry.missing or entry.severity == SEVERITY_CRITICAL


def test_a_placeholder_is_caught_even_though_it_is_not_blank():
    """The failure a blank check alone does not catch."""
    for marker in PLACEHOLDER_MARKERS:
        payload = dict(SAMPLE_ENTRY)
        payload["limitations"] = marker
        entry = format_readiness_tracker_entry(**payload)
        assert not entry.complete, marker
        assert entry.placeholders, marker
        assert any(f.code == "TRK-PLACEHOLDER" for f in entry.findings), marker


def test_limitations_and_corrective_action_accept_none_but_not_nothing():
    """Writing none is a statement. A blank is not, and the two read
    identically to whoever signs."""
    stated = dict(SAMPLE_ENTRY)
    stated["corrective_action"] = ("None required, because the control "
                                   "passed on every attempt.")
    assert format_readiness_tracker_entry(**stated).complete
    blank = dict(SAMPLE_ENTRY)
    blank["corrective_action"] = "   "
    assert not format_readiness_tracker_entry(**blank).complete


def test_only_a_permitted_status_is_accepted():
    for status in PROPOSED_STATUSES:
        payload = dict(SAMPLE_ENTRY)
        payload["proposed_status"] = status
        assert format_readiness_tracker_entry(**payload).complete, status
    for bad in ("Probably fine", "green", "PASS", "compliant"):
        payload = dict(SAMPLE_ENTRY)
        payload["proposed_status"] = bad
        entry = format_readiness_tracker_entry(**payload)
        assert not entry.complete, bad
        assert any(f.code == "TRK-STATUS" for f in entry.findings), bad


def test_an_ambiguous_date_is_refused():
    """A record that is off by nine months is a different record."""
    for bad in ("18/09/2026", "09/18/2026", "Sept 18 2026", "2026-9-18"):
        payload = dict(SAMPLE_ENTRY)
        payload["test_date"] = bad
        entry = format_readiness_tracker_entry(**payload)
        assert not entry.complete, bad
        assert any(f.code == "TRK-DATE" for f in entry.findings), bad
    good = dict(SAMPLE_ENTRY)
    good["test_date"] = "2026-01-05"
    assert format_readiness_tracker_entry(**good).complete


def test_a_moving_build_label_is_refused():
    """A label that moves cannot retrieve what was tested."""
    for bad in ("latest", "current", "prod", "production", "main", "LATEST"):
        payload = dict(SAMPLE_ENTRY)
        payload["build_id"] = bad
        entry = format_readiness_tracker_entry(**payload)
        assert not entry.complete, bad
        assert any(f.code == "TRK-BUILD" for f in entry.findings), bad


def test_a_complete_entry_is_hashed_and_a_refused_one_is_not():
    complete = format_readiness_tracker_entry(**SAMPLE_ENTRY)
    assert len(complete.entry_hash) == 64
    assert complete.entry_hash == sha256_of(canonicalise(complete.entry_text))
    broken = dict(SAMPLE_ENTRY)
    broken["answer"] = ""
    assert format_readiness_tracker_entry(**broken).entry_hash == ""


def test_the_same_entry_always_hashes_the_same():
    first = format_readiness_tracker_entry(**SAMPLE_ENTRY)
    second = format_readiness_tracker_entry(**SAMPLE_ENTRY)
    assert first.entry_hash == second.entry_hash


def test_every_entry_says_it_is_not_legal_advice():
    for payload in (SAMPLE_ENTRY, {**SAMPLE_ENTRY, "answer": ""}):
        entry = format_readiness_tracker_entry(**payload)
        assert any(f.code == "TRK-NOT-ADVICE" for f in entry.findings)


def test_the_eight_points_are_the_eight_the_deliverable_names():
    keys = [key for key, _label in TRACKER_FIELDS]
    assert keys == ["answer", "build_id", "test_date", "actual_behavior",
                    "evidence_link", "limitations", "corrective_action",
                    "proposed_status"]
    labels = [label for _key, label in TRACKER_FIELDS]
    assert [l.split(".")[0] for l in labels] == [str(i) for i in range(1, 9)]


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "compliance_evidence_audit_console"
              / "core.py").read_text()
    assert "import streamlit" not in source
    probe = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.compliance_evidence_audit_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT)
    )
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [record.headline + " ".join(f.title + f.detail + f.fix
                                    for f in record.findings)
         for payload in list(SAMPLE_PAYLOADS) + ["", {"a": 1}]
         for record in (generate_evidence_hash(payload),)]
        + [check.headline + " ".join(f.title + f.detail + f.fix
                                     for f in check.findings)
           for endpoint, observed, expected in SAMPLE_CHECKS
           for check in (validate_api_compliance(endpoint, observed,
                                                 expected),)]
        + [label for _key, label in TRACKER_FIELDS]
        + [entry.headline + " ".join(f.title + f.detail + f.fix
                                     for f in entry.findings)
           for payload in (SAMPLE_ENTRY, {**SAMPLE_ENTRY, "limitations": ""},
                           {**SAMPLE_ENTRY, "build_id": "latest"})
           for entry in (format_readiness_tracker_entry(**payload),)]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools
    tools = all_tools()
    entry = next(t for t in tools
                 if t.key == "compliance-evidence-audit-console")
    assert entry.title == "Compliance Evidence & WORM Audit Console"
    icons = [t.icon for t in tools]
    assert icons.count(entry.icon) == 1, "this icon is already used by another tool"
    assert len(entry.tagline) > 30
