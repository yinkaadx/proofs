"""Compliance Evidence and WORM Audit Console: the engine.

Three parts of an evidence pipeline, each holding one property that decides
whether the evidence is worth anything when somebody hostile reads it.

1. A hash proves integrity only if the same logical record always produces
   the same digest. Hash a dictionary through an ordinary serialiser and the
   same record hashes differently depending on key order, whitespace and
   unicode form, so the evidence fails its own check for a reason nobody can
   explain in a deposition. Canonicalise first, then hash.
   And a bare per record hash detects modification but not deletion. Chain
   each record to the one before it and a removed record breaks every link
   after it, which is the difference between tamper evident and tamper
   evident against somebody who can delete.
2. An intent and a deployed behaviour that disagree are a finding whichever
   way round they are. A 200 where an authorisation failure was intended is
   not a minor discrepancy, and a tool that ranks discrepancies by how far
   apart the numbers are will rank it as one.
3. A tracker entry with a blank field is worse than no entry, because it
   looks answered. The formatter refuses to emit one rather than producing
   a document with a gap in it that somebody signs.

Nothing here writes to real storage, calls a real endpoint or constitutes
legal advice. The hashing is real SHA256 from the standard library and the
tests check it against the library directly.

No Streamlit import lives here on purpose, so the same engine could sit behind
a real evidence worker without a line changing.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

SEVERITY_OK = "OK"
SEVERITY_WARN = "WARN"
SEVERITY_CRITICAL = "CRITICAL"

# The digest of an empty chain, so the first record still has a predecessor
# to commit to. Without it the first record is unanchored and can be swapped.
GENESIS = "0" * 64


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


def _worst(findings) -> str:
    for level in (SEVERITY_CRITICAL, SEVERITY_WARN):
        if any(f.severity == level for f in findings):
            return level
    return SEVERITY_OK


# ---------------------------------------------------------------------------
# 1. Evidence hashing
# ---------------------------------------------------------------------------

def canonicalise(payload_data) -> str:
    """One logical record, one byte string, every time.

    Sorted keys, no incidental whitespace, unicode normalised to composed
    form, and non ASCII left as itself rather than escaped. Skip any of those
    and the same record hashes differently on a different machine, which
    turns the integrity check into a source of false alarms and then into a
    check nobody trusts.
    """
    if isinstance(payload_data, (str, bytes)):
        text = (payload_data.decode("utf-8") if isinstance(payload_data, bytes)
                else payload_data)
        return unicodedata.normalize("NFC", text)
    return unicodedata.normalize(
        "NFC",
        json.dumps(payload_data, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False, default=str))


def holds_nothing(payload_data) -> bool:
    """True when the payload carries no content to attest to.

    ``canonicalise({})`` returns the two characters ``{}``, which is a
    perfectly hashable string, so a whitespace test alone admits an empty
    container and mints a digest that proves an empty record existed.
    """
    if payload_data is None:
        return True
    if isinstance(payload_data, (str, bytes)):
        text = (payload_data.decode("utf-8", "replace")
                if isinstance(payload_data, bytes) else payload_data)
        return not text.strip()
    if isinstance(payload_data, (dict, list, tuple, set, frozenset)):
        return len(payload_data) == 0
    return False


def sha256_of(text: str) -> str:
    """Plain SHA256 of the UTF-8 bytes. The tests check it against hashlib."""
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


ADMITTED = "ADMITTED TO WORM STORAGE"
REJECTED = "REJECTED"


@dataclass(frozen=True)
class EvidenceRecord:
    sequence: int
    payload_data: object
    canonical_form: str
    canonical_bytes: int
    content_hash: str
    previous_hash: str
    chain_hash: str
    admitted: bool
    status: str
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    def recompute(self) -> str:
        """The chain hash as it should be, from the parts it claims."""
        return sha256_of(f"{self.previous_hash}|{self.content_hash}")

    @property
    def self_consistent(self) -> bool:
        return (self.chain_hash == self.recompute()
                and self.content_hash == sha256_of(self.canonical_form))


def generate_evidence_hash(payload_data, previous_hash: str = GENESIS,
                           sequence: int = 1) -> EvidenceRecord:
    """Canonicalise, hash, and commit to the record before this one.

    The chain hash covers the previous hash as well as this record's content,
    so removing any earlier record breaks every link after it. A per record
    hash on its own detects an edit and never detects a deletion, and a
    deletion is the edit an insider actually makes.
    """
    previous = str(previous_hash or "").strip().lower()
    index = int(sequence)
    findings: list[Finding] = []

    if not re.fullmatch(r"[0-9a-f]{64}", previous):
        findings.append(Finding(
            code="EVD-PREV", severity=SEVERITY_CRITICAL,
            title=f"{previous_hash!r} is not a SHA256 digest",
            detail=("The record cannot be chained to something that is not a "
                    "digest, so nothing was admitted. A chain with a "
                    "malformed link is a chain that verifies against "
                    "nothing."),
            fix=(f"Pass the previous record's chain hash, or {GENESIS[:8]}"
                 f"... for the first record in a new chain.")))
        return EvidenceRecord(
            sequence=index, payload_data=payload_data, canonical_form="",
            canonical_bytes=0, content_hash="", previous_hash=previous,
            chain_hash="", admitted=False, status=REJECTED,
            headline="Rejected before hashing: the previous link is malformed",
            findings=tuple(findings))

    if index < 1:
        raise ValueError("a chain sequence starts at one")

    canonical = canonicalise(payload_data)
    if holds_nothing(payload_data) or not canonical.strip():
        findings.append(Finding(
            code="EVD-EMPTY", severity=SEVERITY_CRITICAL,
            title="There is nothing to hash",
            detail=("An empty payload produces a valid digest of nothing, "
                    "which is an evidence record that proves an empty "
                    "record existed."),
            fix="Admit the artifact, not a placeholder for it."))
        return EvidenceRecord(
            sequence=index, payload_data=payload_data,
            canonical_form=canonical, canonical_bytes=0, content_hash="",
            previous_hash=previous, chain_hash="", admitted=False,
            status=REJECTED, headline="Rejected: nothing to hash",
            findings=tuple(findings))

    content = sha256_of(canonical)
    chain = sha256_of(f"{previous}|{content}")

    findings.append(Finding(
        code="EVD-CANONICAL", severity=SEVERITY_OK,
        title="The payload was canonicalised before it was hashed",
        detail=("Sorted keys, no incidental whitespace and composed unicode, "
                "so the same logical record produces the same digest on any "
                "machine. Hashing a serialised dictionary directly makes the "
                "digest depend on key order, and the check then fails for "
                "reasons nobody can explain."),
        fix="Canonicalise at the point of admission, never at the point of verification."))

    findings.append(Finding(
        code="EVD-CHAIN", severity=SEVERITY_OK,
        title=f"Record {index} commits to the hash of record {index - 1}",
        detail=("A per record hash detects an edit and never detects a "
                "deletion. Chaining means a removed record breaks every "
                "link after it, and deletion is the edit an insider "
                "actually makes."),
        fix="Verify the whole chain rather than spot checking records."))

    findings.append(Finding(
        code="EVD-CUSTODY", severity=SEVERITY_CRITICAL,
        title="A hash stored beside the data it protects proves nothing",
        detail=("Whoever can change the record can change the digest next to "
                "it. The integrity claim rests entirely on the digest living "
                "somewhere the writer cannot reach."),
        fix=("Write the chain hash to storage under different control, or "
             "publish it somewhere append only. This tool computes the "
             "digest and cannot give you the custody.")))

    findings.append(Finding(
        code="EVD-TIME", severity=SEVERITY_WARN,
        title="A timestamp the writer controls is not evidence of when",
        detail=("A hash proves what, not when. If the clock belongs to the "
                "same party as the record, the record can be backdated and "
                "the digest will verify perfectly."),
        fix=("Take the time from a source the writer does not control, and "
             "record which source it was.")))

    headline = (f"Record {index} admitted, {len(canonical.encode('utf-8'))} "
                f"canonical byte(s), chain hash {chain[:16]}")

    return EvidenceRecord(
        sequence=index, payload_data=payload_data, canonical_form=canonical,
        canonical_bytes=len(canonical.encode("utf-8")), content_hash=content,
        previous_hash=previous, chain_hash=chain, admitted=True,
        status=ADMITTED, headline=headline, findings=tuple(findings),
    )


def build_chain(payloads) -> tuple[EvidenceRecord, ...]:
    """Admit a sequence of payloads, each committing to the one before."""
    records: list[EvidenceRecord] = []
    previous = GENESIS
    for index, payload in enumerate(payloads or (), start=1):
        record = generate_evidence_hash(payload, previous, index)
        records.append(record)
        if record.admitted:
            previous = record.chain_hash
    return tuple(records)


def verify_chain(records) -> dict:
    """Walk the chain and name the first link that does not hold."""
    rows = tuple(records or ())
    previous = GENESIS
    broken_at = 0
    for record in rows:
        if record.previous_hash != previous or not record.self_consistent:
            broken_at = record.sequence
            break
        previous = record.chain_hash
    return {
        "records": len(rows),
        "intact": broken_at == 0 and bool(rows),
        "broken_at": broken_at,
        "head": rows[-1].chain_hash if rows else GENESIS,
    }


SAMPLE_PAYLOADS: tuple[dict, ...] = (
    {"control": "AC-2", "artifact": "access-review-2026-Q1.pdf",
     "reviewer": "K. Osei", "result": "pass"},
    {"control": "AC-6", "artifact": "least-privilege-matrix.xlsx",
     "reviewer": "K. Osei", "result": "pass with exception"},
    {"control": "AU-11", "artifact": "retention-policy-v4.pdf",
     "reviewer": "D. Marsh", "result": "pass"},
)


# ---------------------------------------------------------------------------
# 2. API compliance validation
# ---------------------------------------------------------------------------

CLASS_BY_PREFIX = {
    "2": "success", "3": "redirect", "4": "client error", "5": "server error"}

COMPLIANT = "COMPLIANT"
DISCREPANT = "DISCREPANT"

# Discrepancy kinds, ordered by what they cost rather than by how far apart
# the two numbers are. A tool that ranks by numeric distance ranks an
# authorisation bypass below a redirect, which is exactly backwards.
KIND_AUTH_BYPASS = "AUTHORISATION BYPASS"
KIND_SILENT_SUCCESS = "SILENT SUCCESS"
KIND_EXISTENCE_LEAK = "EXISTENCE DISCLOSURE"
KIND_WRONG_CLASS = "WRONG RESPONSE CLASS"
KIND_WRONG_CODE = "WRONG CODE, SAME CLASS"
KIND_NONE = "NONE"

KIND_SEVERITY = {
    KIND_AUTH_BYPASS: SEVERITY_CRITICAL,
    KIND_SILENT_SUCCESS: SEVERITY_CRITICAL,
    KIND_EXISTENCE_LEAK: SEVERITY_CRITICAL,
    KIND_WRONG_CLASS: SEVERITY_WARN,
    KIND_WRONG_CODE: SEVERITY_WARN,
    KIND_NONE: SEVERITY_OK,
}

# Rank for reporting. Lower is worse, so a sort puts the dangerous ones first.
KIND_RANK = {
    KIND_AUTH_BYPASS: 0, KIND_SILENT_SUCCESS: 1, KIND_EXISTENCE_LEAK: 2,
    KIND_WRONG_CLASS: 3, KIND_WRONG_CODE: 4, KIND_NONE: 5,
}

AUTH_CODES: tuple[int, ...] = (401, 403)


@dataclass(frozen=True)
class ComplianceCheck:
    endpoint: str
    response_code: int
    expected_status: int
    observed_class: str
    expected_class: str
    compliant: bool
    kind: str
    rank: int
    verdict: str
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def validate_api_compliance(endpoint: str, response_code: int,
                            expected_status: int) -> ComplianceCheck:
    """Compare what was intended against what is deployed, and rank the gap.

    Ranked by what the discrepancy costs rather than by how far apart the two
    numbers are. Two hundred where four oh one was intended is eleven codes
    away from six hundred and is the worst thing on this page.
    """
    name = str(endpoint or "").strip()
    if not name:
        raise ValueError("a check needs an endpoint")
    observed = int(response_code)
    expected = int(expected_status)
    for code in (observed, expected):
        if not 100 <= code <= 599:
            raise ValueError(f"{code} is not an HTTP status code")

    observed_class = CLASS_BY_PREFIX.get(str(observed)[0], "informational")
    expected_class = CLASS_BY_PREFIX.get(str(expected)[0], "informational")
    findings: list[Finding] = []

    if observed == expected:
        kind = KIND_NONE
        findings.append(Finding(
            code="API-MATCH", severity=SEVERITY_OK,
            title=f"{name} returns {observed} as intended",
            detail="The deployed behaviour matches the documented requirement.",
            fix=("Record the check with its date and build. A control that "
                 "passed once and is never rechecked is a control nobody is "
                 "testing.")))
    elif expected in AUTH_CODES and observed_class == "success":
        kind = KIND_AUTH_BYPASS
        findings.append(Finding(
            code="API-BYPASS", severity=SEVERITY_CRITICAL,
            title=f"{name} returns {observed} where {expected} was required",
            detail=("The requirement says this request should be refused and "
                    "the deployment serves it. Whatever the endpoint "
                    "returns, it returned it to a caller who should have "
                    "been stopped."),
            fix=("Treat this as an incident rather than a finding. Establish "
                 "how long it has been deployed before deciding what has to "
                 "be disclosed.")))
    elif observed_class == "success" and expected_class in ("client error",
                                                            "server error"):
        kind = KIND_SILENT_SUCCESS
        findings.append(Finding(
            code="API-SILENT", severity=SEVERITY_CRITICAL,
            title=f"{name} returns {observed} where {expected} was required",
            detail=("A success code on a request that should have failed is "
                    "worse than a server error, because every client treats "
                    "it as done. The failure is invisible to monitoring that "
                    "counts status codes."),
            fix=("Return the error class the requirement names. A body that "
                 "says error under a 200 is read by nothing.")))
    elif expected == 404 and observed == 403:
        kind = KIND_EXISTENCE_LEAK
        findings.append(Finding(
            code="API-EXISTS", severity=SEVERITY_CRITICAL,
            title=f"{name} returns 403 where 404 was required",
            detail=("Forbidden confirms the resource exists. Not found does "
                    "not. Where the requirement asks for 404 it is usually "
                    "asking precisely so that existence is not disclosed to "
                    "a caller who may not know."),
            fix="Return 404 for both absent and forbidden on this endpoint."))
    elif observed_class != expected_class:
        kind = KIND_WRONG_CLASS
        findings.append(Finding(
            code="API-CLASS", severity=SEVERITY_WARN,
            title=f"{name} returns a {observed_class} where a {expected_class} was required",
            detail=(f"{observed} against an intended {expected}. Different "
                    f"class means client code branches differently, so this "
                    f"changes behaviour downstream rather than only "
                    f"changing a log line."),
            fix="Align the deployment with the requirement, or amend the requirement deliberately."))
    else:
        kind = KIND_WRONG_CODE
        findings.append(Finding(
            code="API-CODE", severity=SEVERITY_WARN,
            title=f"{name} returns {observed} where {expected} was required",
            detail=(f"Same {observed_class} class, different code. Clients "
                    f"branching on the class behave the same, clients "
                    f"branching on the code do not."),
            fix="Decide which is correct and change one of them on purpose."))

    findings.append(Finding(
        code="API-EVIDENCE", severity=SEVERITY_WARN,
        title="A check with no artifact is an assertion",
        detail=("The result above is only evidence if the request and the "
                "response were captured. A tracker row saying the check "
                "passed, with nothing attached, is a claim rather than a "
                "record."),
        fix="Hash the captured exchange and attach the digest to the tracker row."))

    compliant = kind == KIND_NONE
    verdict = COMPLIANT if compliant else DISCREPANT
    headline = (f"{name}: intended {expected}, deployed {observed}, "
                f"{'no discrepancy' if compliant else kind.lower()}")

    return ComplianceCheck(
        endpoint=name, response_code=observed, expected_status=expected,
        observed_class=observed_class, expected_class=expected_class,
        compliant=compliant, kind=kind, rank=KIND_RANK[kind], verdict=verdict,
        headline=headline, findings=tuple(findings),
    )


SAMPLE_CHECKS: tuple[tuple[str, int, int], ...] = (
    ("GET /v1/records/{id}", 200, 200),
    ("GET /v1/admin/export", 200, 403),
    ("POST /v1/records", 200, 422),
    ("GET /v1/records/other-tenant", 403, 404),
    ("DELETE /v1/records/{id}", 500, 409),
    ("GET /v1/health", 204, 200),
)


# ---------------------------------------------------------------------------
# 3. The readiness tracker entry
# ---------------------------------------------------------------------------

# The eight points, in order, with the label each one carries in the
# deliverable. The order is part of the format rather than a preference.
TRACKER_FIELDS: tuple[tuple[str, str], ...] = (
    ("answer", "1. Answer"),
    ("build_id", "2. Build identifier"),
    ("test_date", "3. Test date"),
    ("actual_behavior", "4. Actual observed behaviour"),
    ("evidence_link", "5. Evidence reference"),
    ("limitations", "6. Limitations"),
    ("corrective_action", "7. Corrective action"),
    ("proposed_status", "8. Proposed status"),
)

PROPOSED_STATUSES: tuple[str, ...] = (
    "Compliant", "Compliant with exception", "Partially compliant",
    "Not compliant", "Not applicable")

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Words that look like an answer and are not one. A tracker row carrying one
# of these reads as completed to everybody except the person who has to
# defend it.
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "tbd", "tbc", "todo", "n/a", "na", "pending", "xxx", "placeholder",
    "fill in", "see above", "as discussed",
)

ENTRY_COMPLETE = "COMPLETE"
ENTRY_REFUSED = "REFUSED, THE ENTRY HAS A GAP"


@dataclass(frozen=True)
class FieldCheck:
    key: str
    label: str
    value: str
    present: bool
    problem: str


@dataclass(frozen=True)
class TrackerEntry:
    fields: tuple[FieldCheck, ...]
    status: str
    complete: bool
    missing: tuple[str, ...]
    placeholders: tuple[str, ...]
    entry_text: str
    entry_hash: str
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def point_count(self) -> int:
        return len(self.fields)


def format_readiness_tracker_entry(answer: str, build_id: str, test_date: str,
                                   actual_behavior: str, evidence_link: str,
                                   limitations: str, corrective_action: str,
                                   proposed_status: str) -> TrackerEntry:
    """Produce the eight point entry, or refuse and say which point is missing.

    A tracker row with a blank field is worse than no row, because it looks
    answered. So the formatter refuses rather than emitting a document with a
    gap in it that somebody then signs.

    Limitations and corrective action are required even when the answer is
    none. Writing none is a statement that the question was considered; a
    blank is a statement that it was not, and the two read identically to
    whoever signs.
    """
    supplied = {
        "answer": answer, "build_id": build_id, "test_date": test_date,
        "actual_behavior": actual_behavior, "evidence_link": evidence_link,
        "limitations": limitations, "corrective_action": corrective_action,
        "proposed_status": proposed_status,
    }

    checks: list[FieldCheck] = []
    missing: list[str] = []
    placeholders: list[str] = []
    findings: list[Finding] = []

    for key, label in TRACKER_FIELDS:
        value = str(supplied.get(key, "") or "").strip()
        problem = ""
        if not value:
            problem = "blank"
            missing.append(label)
        elif value.lower().strip(" .") in PLACEHOLDER_MARKERS:
            problem = "placeholder"
            placeholders.append(label)
        checks.append(FieldCheck(key=key, label=label, value=value,
                                 present=bool(value), problem=problem))

    for label in missing:
        findings.append(Finding(
            code="TRK-BLANK", severity=SEVERITY_CRITICAL,
            title=f"{label} is blank",
            detail=("A blank field in a compliance deliverable reads as "
                    "answered to everybody except the person who has to "
                    "defend it."),
            fix=("Write the answer, or write none and say why none. Both are "
                 "statements. A blank is not.")))

    for label in placeholders:
        findings.append(Finding(
            code="TRK-PLACEHOLDER", severity=SEVERITY_CRITICAL,
            title=f"{label} carries a placeholder rather than an answer",
            detail=("It survives review because it is not empty, and it is "
                    "not an answer. This is the failure mode a blank check "
                    "alone does not catch."),
            fix="Replace it before the entry is circulated, not after."))

    status_value = str(proposed_status or "").strip()
    if status_value and status_value not in PROPOSED_STATUSES:
        findings.append(Finding(
            code="TRK-STATUS", severity=SEVERITY_CRITICAL,
            title=f"{status_value} is not one of the permitted statuses",
            detail=("A free text status cannot be counted, filtered or "
                    "rolled up, so a tracker built from them cannot answer "
                    "how many controls are compliant."),
            fix=f"Use one of: {', '.join(PROPOSED_STATUSES)}."))

    date_value = str(test_date or "").strip()
    if date_value and not DATE_PATTERN.match(date_value):
        findings.append(Finding(
            code="TRK-DATE", severity=SEVERITY_CRITICAL,
            title=f"{date_value} is not an unambiguous date",
            detail=("A date written any other way is read differently on "
                    "either side of the Atlantic, and a compliance record "
                    "that is off by nine months is a different record."),
            fix="Write it as four digit year, month, day."))

    build_value = str(build_id or "").strip()
    if build_value and build_value.lower() in ("latest", "current", "prod",
                                               "production", "main"):
        findings.append(Finding(
            code="TRK-BUILD", severity=SEVERITY_CRITICAL,
            title=f"{build_value} names a moving target rather than a build",
            detail=("The evidence describes whatever was deployed on the "
                    "test date, and a label that moves cannot be used to "
                    "retrieve it later."),
            fix="Record the immutable build identifier or the commit."))

    status_ok = status_value in PROPOSED_STATUSES
    date_ok = bool(DATE_PATTERN.match(date_value)) if date_value else False
    build_ok = bool(build_value) and build_value.lower() not in (
        "latest", "current", "prod", "production", "main")
    complete = (not missing and not placeholders and status_ok and date_ok
                and build_ok)

    if complete:
        findings.append(Finding(
            code="TRK-COMPLETE", severity=SEVERITY_OK,
            title="All eight points carry an answer",
            detail=("Nothing is blank, nothing is a placeholder, the status "
                    "is from the permitted set and the build is a fixed "
                    "identifier."),
            fix=("Hash the entry and file the digest with the evidence, so "
                 "the row and the artifact are tied together.")))

    findings.append(Finding(
        code="TRK-NOT-ADVICE", severity=SEVERITY_WARN,
        title="This is a formatter and not legal advice",
        detail=("It checks that the eight points are answered. Whether the "
                "answers are adequate for your obligation is a judgement "
                "this tool does not make and cannot."),
        fix="Have counsel read the entries, not the tool."))

    if complete:
        lines = [f"{label}: {supplied[key]}".strip()
                 for key, label in TRACKER_FIELDS]
        entry_text = "\n".join(lines)
        entry_hash = sha256_of(canonicalise(entry_text))
        status = ENTRY_COMPLETE
        headline = (f"Eight of eight points answered, entry digest "
                    f"{entry_hash[:16]}")
    else:
        entry_text = ""
        entry_hash = ""
        status = ENTRY_REFUSED
        gaps = len(missing) + len(placeholders)
        extra = sum(1 for flag in (status_ok, date_ok, build_ok) if not flag)
        headline = (f"Refused: {gaps} point(s) with a gap and {extra} field "
                    f"level problem(s). No entry was produced")

    return TrackerEntry(
        fields=tuple(checks), status=status, complete=complete,
        missing=tuple(missing), placeholders=tuple(placeholders),
        entry_text=entry_text, entry_hash=entry_hash, headline=headline,
        findings=tuple(findings),
    )


SAMPLE_ENTRY: dict = {
    "answer": ("Yes. Access to the export endpoint is restricted to the "
               "compliance role and refused for every other role."),
    "build_id": "rel-2026.09.14-a41f2e9",
    "test_date": "2026-09-18",
    "actual_behavior": ("Requests from the analyst role received 403 on all "
                        "twelve attempts. Requests from the compliance role "
                        "received 200 and the expected payload."),
    "evidence_link": "worm://evidence/2026-09-18/export-authz-run-04.har",
    "limitations": ("Tested against the staging dataset only. Production "
                    "volumes were not exercised and no load condition was "
                    "applied."),
    "corrective_action": ("None required. A production reverification is "
                          "scheduled for the next release."),
    "proposed_status": "Compliant with exception",
}
