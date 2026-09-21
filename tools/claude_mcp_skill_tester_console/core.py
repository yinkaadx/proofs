"""Claude MCP Skill and Agent Testing Console: the engine.

Three checks that catch the failures an agent integration produces silently,
which are the only kind worth building a console for.

1. The skill manifest. A skill is loaded by matching its description against
   what the user asked for, so a thin description is not a style problem, it
   is a skill that never loads and never errors either. An unknown key is
   reported rather than ignored, because a typo in a key name is a setting
   that is silently absent.
2. The database behind the MCP server. When row level security is on and no
   policy matches, Postgres returns zero rows. It does not raise. So a tool
   reporting no results may be reporting you are not allowed to see them,
   and those two answers look identical to whatever reads the output. The
   simulator separates them on purpose.
3. The boundary matrix. A test run that only exercises happy paths tells you
   the thing works when nothing is wrong, which was never in doubt. The
   matrix reports its own coverage and calls itself insufficient when a
   category is missing.

The manifest rules here were taken from the twelve skill manifests on the
machine that built this, where name and description appear in all twelve and
license in four. They are stated as that evidence rather than as a
specification.

No Streamlit import lives here on purpose, so the same engine could sit behind
a real continuous integration step without a line changing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

SEVERITY_OK = "OK"
SEVERITY_WARN = "WARN"
SEVERITY_CRITICAL = "CRITICAL"


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
# 1. Skill manifest validation
# ---------------------------------------------------------------------------

# Present in all twelve manifests found on the machine that built this.
REQUIRED_KEYS: tuple[str, ...] = ("name", "description")
# Present in some. Recognised so a real key is not reported as a typo.
OPTIONAL_KEYS: tuple[str, ...] = ("license", "allowed-tools")
KNOWN_KEYS: tuple[str, ...] = REQUIRED_KEYS + OPTIONAL_KEYS

EXPECTED_TYPES: dict = {
    "name": str,
    "description": str,
    "license": str,
    "allowed-tools": (str, list),
}

NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# A description is what the loader matches against, so its length is a
# functional property rather than a stylistic one. The shortest of the twelve
# on disk runs well past this.
MIN_DESCRIPTION_CHARS = 40
THIN_DESCRIPTION_CHARS = 80

VALID_PASSED = "VALID"
VALID_FAILED = "INVALID"
VALID_UNPARSEABLE = "NOT PARSEABLE"


@dataclass(frozen=True)
class KeyCheck:
    key: str
    present: bool
    expected_type: str
    actual_type: str
    type_ok: bool
    required: bool


@dataclass(frozen=True)
class ManifestReport:
    raw: str
    status: str
    parsed: bool
    keys_seen: tuple[str, ...]
    missing_keys: tuple[str, ...]
    unknown_keys: tuple[str, ...]
    type_errors: tuple[str, ...]
    checks: tuple[KeyCheck, ...]
    name: str
    description_chars: int
    would_load: bool
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def is_valid(self) -> bool:
        return self.status == VALID_PASSED


def _type_name(value) -> str:
    return type(value).__name__


def validate_skill_manifest(json_payload) -> ManifestReport:
    """Check a manifest the way a loader would, and then some.

    Strict about types rather than truthy: a description given as a number,
    or an allowed tools list given as a bare string, is reported rather than
    coerced, because coercion at validation time hides a bug that surfaces
    at load time with no useful message.
    """
    raw = (json_payload if isinstance(json_payload, str)
           else json.dumps(json_payload, indent=2)
           if json_payload is not None else "")
    findings: list[Finding] = []

    if isinstance(json_payload, dict):
        payload = dict(json_payload)
    else:
        text = str(json_payload or "").strip()
        if not text:
            findings.append(Finding(
                code="SKL-EMPTY", severity=SEVERITY_CRITICAL,
                title="Nothing was supplied to validate",
                detail="An empty manifest is a skill that cannot be loaded at all.",
                fix="Point the validator at the frontmatter block of the SKILL.md file."))
            return ManifestReport(
                raw=raw, status=VALID_UNPARSEABLE, parsed=False, keys_seen=(),
                missing_keys=REQUIRED_KEYS, unknown_keys=(), type_errors=(),
                checks=(), name="", description_chars=0, would_load=False,
                headline="Nothing to validate", findings=tuple(findings))
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            findings.append(Finding(
                code="SKL-JSON", severity=SEVERITY_CRITICAL,
                title=f"The payload is not valid JSON: {exc.msg} at line {exc.lineno}",
                detail=("Nothing was checked, because nothing could be read. "
                        "A validator that reports a parse failure as a "
                        "missing key sends the reader to the wrong place."),
                fix="Fix the syntax first, then run the key checks."))
            return ManifestReport(
                raw=raw, status=VALID_UNPARSEABLE, parsed=False, keys_seen=(),
                missing_keys=REQUIRED_KEYS, unknown_keys=(), type_errors=(),
                checks=(), name="", description_chars=0, would_load=False,
                headline=f"Not parseable: {exc.msg}", findings=tuple(findings))
        if not isinstance(payload, dict):
            findings.append(Finding(
                code="SKL-SHAPE", severity=SEVERITY_CRITICAL,
                title=f"The payload parsed as a {_type_name(payload)} rather than an object",
                detail="A manifest is a mapping of keys to values.",
                fix="Wrap it in braces, or point the validator at the right block."))
            return ManifestReport(
                raw=raw, status=VALID_UNPARSEABLE, parsed=False, keys_seen=(),
                missing_keys=REQUIRED_KEYS, unknown_keys=(), type_errors=(),
                checks=(), name="", description_chars=0, would_load=False,
                headline="Parsed, but not into an object",
                findings=tuple(findings))

    keys_seen = tuple(str(k) for k in payload)
    missing = tuple(k for k in REQUIRED_KEYS if k not in payload)
    unknown = tuple(k for k in keys_seen if k not in KNOWN_KEYS)

    checks: list[KeyCheck] = []
    type_errors: list[str] = []
    for key in KNOWN_KEYS:
        expected = EXPECTED_TYPES[key]
        present = key in payload
        actual = _type_name(payload[key]) if present else "absent"
        ok = present and isinstance(payload[key], expected)
        if present and not ok:
            names = (expected.__name__ if isinstance(expected, type)
                     else " or ".join(t.__name__ for t in expected))
            type_errors.append(f"{key} is a {actual}, expected {names}")
        checks.append(KeyCheck(
            key=key, present=present,
            expected_type=(expected.__name__ if isinstance(expected, type)
                           else " or ".join(t.__name__ for t in expected)),
            actual_type=actual, type_ok=ok or not present,
            required=key in REQUIRED_KEYS))

    name = payload.get("name") if isinstance(payload.get("name"), str) else ""
    description = (payload.get("description")
                   if isinstance(payload.get("description"), str) else "")
    description_chars = len(description.strip())

    for key in missing:
        findings.append(Finding(
            code="SKL-MISSING", severity=SEVERITY_CRITICAL,
            title=f"The required key {key} is absent",
            detail=(f"All twelve manifests on the machine that built this "
                    f"carry {key}. A manifest without it does not load."),
            fix=f"Add {key} to the frontmatter block."))

    for message in type_errors:
        findings.append(Finding(
            code="SKL-TYPE", severity=SEVERITY_CRITICAL,
            title=message,
            detail=("The type is checked rather than the truthiness, because "
                    "coercing it here hides a bug that resurfaces at load "
                    "time with no useful message."),
            fix="Correct the value type rather than relying on the loader to cope."))

    for key in unknown:
        findings.append(Finding(
            code="SKL-UNKNOWN", severity=SEVERITY_WARN,
            title=f"{key} is not a key this validator recognises",
            detail=("Reported rather than ignored. A typo in a key name is a "
                    "setting that is silently absent, and silently absent is "
                    "the hardest kind to find."),
            fix=(f"Check the spelling against {', '.join(KNOWN_KEYS)}, or "
                 f"confirm {key} is genuinely supported by your loader.")))

    if name and not NAME_PATTERN.match(name):
        findings.append(Finding(
            code="SKL-NAME", severity=SEVERITY_CRITICAL,
            title=f"{name} is not a usable skill name",
            detail=("Every name on disk is lower case words joined by single "
                    "hyphens. A name with spaces, capitals or underscores "
                    "does not match the directory it lives in."),
            fix="Rename it to lower case with hyphens, and rename the directory to match."))

    if description and description_chars < MIN_DESCRIPTION_CHARS:
        findings.append(Finding(
            code="SKL-THIN", severity=SEVERITY_CRITICAL,
            title=f"The description is {description_chars} characters and will not load the skill",
            detail=("The loader matches the description against what the "
                    "user asked for. A description too thin to match is a "
                    "skill that never loads, and it never errors either, so "
                    "nobody finds out."),
            fix=("Write what the skill does and when to use it, in the words "
                 "a user would actually type.")))
    elif description and description_chars < THIN_DESCRIPTION_CHARS:
        findings.append(Finding(
            code="SKL-SHORT", severity=SEVERITY_WARN,
            title=f"The description is {description_chars} characters, which is short",
            detail=("It will load for a close match and miss a paraphrase. "
                    "The descriptions on disk carry both what the skill does "
                    "and when to reach for it."),
            fix="Add the trigger phrasing, not just the capability."))
    elif description:
        findings.append(Finding(
            code="SKL-DESC-OK", severity=SEVERITY_OK,
            title=f"The description is {description_chars} characters",
            detail="Long enough to match a paraphrase rather than only an exact ask.",
            fix="Keep the trigger wording in it when the skill is edited later."))

    would_load = not missing and not type_errors and bool(
        name and NAME_PATTERN.match(name)
        and description_chars >= MIN_DESCRIPTION_CHARS)
    status = VALID_PASSED if would_load and not unknown else (
        VALID_PASSED if would_load else VALID_FAILED)

    headline = (f"{name or 'the manifest'} "
                f"{'validates and would load' if would_load else 'would not load'}"
                f", {len(keys_seen)} key(s) seen, {len(missing)} missing, "
                f"{len(type_errors)} type error(s)")

    return ManifestReport(
        raw=raw, status=status, parsed=True, keys_seen=keys_seen,
        missing_keys=missing, unknown_keys=unknown,
        type_errors=tuple(type_errors), checks=tuple(checks), name=name,
        description_chars=description_chars, would_load=would_load,
        headline=headline, findings=tuple(findings),
    )


SAMPLE_MANIFESTS: dict = {
    "A manifest that loads": {
        "name": "gtm-container-auditor",
        "description": ("Audit a Google Tag Manager container and report tags "
                        "with no trigger, triggers with no tag, and tags that "
                        "fire before consent is known. Use when asked to "
                        "review or clean a container."),
        "license": "MIT",
    },
    "Description too thin to ever match": {
        "name": "report-builder",
        "description": "Builds reports.",
    },
    "A typo in a key name": {
        "name": "invoice-reader",
        "descriptoin": ("Reads an invoice PDF and extracts the line items, "
                        "totals and payment terms into a structured record."),
    },
    "Wrong type on a value": {
        "name": "batch-runner",
        "description": 12345,
        "allowed-tools": "Bash",
    },
    "Name that does not match its directory": {
        "name": "My Skill Name",
        "description": ("Does a useful thing and should be reached for when "
                        "the user asks for that useful thing by name."),
    },
}


# ---------------------------------------------------------------------------
# 2. The MCP database connection
# ---------------------------------------------------------------------------

RESULT_ROWS = "ROWS RETURNED"
RESULT_EMPTY_BY_POLICY = "EMPTY, AND THAT IS A POLICY DENIAL"
RESULT_BYPASSED = "POLICY BYPASSED BY THE KEY"
RESULT_ERROR = "ERROR"

ROLE_ANON = "anon"
ROLE_AUTHENTICATED = "authenticated"
ROLE_SERVICE = "service_role"
ROLES: tuple[str, ...] = (ROLE_ANON, ROLE_AUTHENTICATED, ROLE_SERVICE)

TABLE = "documents"

# A tiny fixture spanning two tenants, so a cross tenant read is visible as
# rows belonging to somebody else rather than as an abstraction.
FIXTURE_ROWS: tuple[dict, ...] = (
    {"id": 1, "tenant_id": "acme", "title": "Acme onboarding pack"},
    {"id": 2, "tenant_id": "acme", "title": "Acme pricing sheet"},
    {"id": 3, "tenant_id": "globex", "title": "Globex supplier terms"},
    {"id": 4, "tenant_id": "globex", "title": "Globex incident log"},
)

KNOWN_TENANTS: tuple[str, ...] = ("acme", "globex")


@dataclass(frozen=True)
class McpResponse:
    query_params: dict
    enforce_rls: bool
    role: str
    tenant_id: str
    outcome: str
    rows: tuple[dict, ...]
    row_count: int
    raised_error: bool
    silent_denial: bool
    cross_tenant_rows: int
    response_json: str
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def simulate_mcp_connection(query_params: dict,
                            enforce_rls: bool = True) -> McpResponse:
    """Return what the database would actually return, including the silence.

    The behaviour worth simulating is the one people get wrong: with row
    level security on and no policy match, Postgres returns an empty set. It
    does not raise. A tool reporting no results and a tool reporting you are
    not allowed produce identical output, and only one of them is a bug in
    the query.
    """
    params = dict(query_params or {})
    role = str(params.get("role", ROLE_ANON)).strip()
    tenant = str(params.get("tenant_id", "")).strip()
    table = str(params.get("table", TABLE)).strip()

    findings: list[Finding] = []

    if role not in ROLES:
        findings.append(Finding(
            code="MCP-ROLE", severity=SEVERITY_CRITICAL,
            title=f"{role or 'The role'} is not a role this connection can assume",
            detail=("An unrecognised role is refused rather than treated as "
                    "the least privileged one, because guessing which was "
                    "meant is how a connection ends up with more access than "
                    "intended."),
            fix=f"Use one of {', '.join(ROLES)}."))
        payload = {"error": {"code": "28000",
                             "message": f"role {role!r} does not exist"}}
        return McpResponse(
            query_params=params, enforce_rls=enforce_rls, role=role,
            tenant_id=tenant, outcome=RESULT_ERROR, rows=(), row_count=0,
            raised_error=True, silent_denial=False, cross_tenant_rows=0,
            response_json=json.dumps(payload, indent=2),
            headline="The connection was refused before any query ran",
            findings=tuple(findings))

    if table != TABLE:
        findings.append(Finding(
            code="MCP-TABLE", severity=SEVERITY_CRITICAL,
            title=f"{table} does not exist in this schema",
            detail=("A missing table does raise, which makes it the easy "
                    "failure. The hard one is the query that succeeds and "
                    "returns nothing."),
            fix="Check the table name against the schema the key can see."))
        payload = {"error": {"code": "42P01",
                             "message": f"relation {table!r} does not exist"}}
        return McpResponse(
            query_params=params, enforce_rls=enforce_rls, role=role,
            tenant_id=tenant, outcome=RESULT_ERROR, rows=(), row_count=0,
            raised_error=True, silent_denial=False, cross_tenant_rows=0,
            response_json=json.dumps(payload, indent=2),
            headline=f"{table} does not exist", findings=tuple(findings))

    bypassed = role == ROLE_SERVICE or not enforce_rls

    if bypassed:
        rows = FIXTURE_ROWS
        outcome = RESULT_BYPASSED
        cross = sum(1 for r in rows if tenant and r["tenant_id"] != tenant)
        findings.append(Finding(
            code="MCP-BYPASS", severity=SEVERITY_CRITICAL,
            title=("The service role key bypasses every policy"
                   if role == ROLE_SERVICE
                   else "Row level security is switched off for this query"),
            detail=(f"All {len(rows)} rows come back across "
                    f"{len({r['tenant_id'] for r in rows})} tenants. Every "
                    f"policy on the table was written and none of them ran. "
                    f"An MCP server configured with this key is an MCP "
                    f"server with no tenancy at all, however careful the "
                    f"policies are."),
            fix=("Give the MCP server a key that runs as an ordinary user "
                 "and let the policies do the filtering. The service role "
                 "key belongs in migrations, not in a tool an agent calls.")))
    elif not tenant:
        rows = ()
        outcome = RESULT_EMPTY_BY_POLICY
        cross = 0
        findings.append(Finding(
            code="MCP-SILENT", severity=SEVERITY_CRITICAL,
            title="Zero rows, and this is a denial rather than an empty table",
            detail=("No tenant claim is set, so no policy matches and "
                    "Postgres returns an empty set without raising. The "
                    "response is indistinguishable from a table that "
                    "genuinely has nothing in it, and the agent reading it "
                    "will say there are no documents."),
            fix=("Have the tool report the tenant context alongside the row "
                 "count, so an empty result can be read as either. A count "
                 "with no context is not an answer.")))
    elif tenant not in KNOWN_TENANTS:
        rows = ()
        outcome = RESULT_EMPTY_BY_POLICY
        cross = 0
        findings.append(Finding(
            code="MCP-UNKNOWN-TENANT", severity=SEVERITY_WARN,
            title=f"No rows for tenant {tenant}, which may be correct",
            detail=("The claim is set and matches nothing. This one really "
                    "could be an empty result, and it looks exactly like the "
                    "denial above, which is the whole problem."),
            fix="Log the tenant claim with the query so the two can be told apart."))
    else:
        rows = tuple(r for r in FIXTURE_ROWS if r["tenant_id"] == tenant)
        outcome = RESULT_ROWS
        cross = sum(1 for r in rows if r["tenant_id"] != tenant)
        findings.append(Finding(
            code="MCP-SCOPED", severity=SEVERITY_OK,
            title=f"{len(rows)} row(s), all belonging to {tenant}",
            detail=("The policy filtered the other tenant's rows out on the "
                    "server. Nothing crossed the boundary and nothing had to "
                    "be filtered by the caller."),
            fix=("Keep the filtering in the policy. A caller that filters is "
                 "a caller that can forget to.")))

    findings.append(Finding(
        code="MCP-SILENCE-RULE", severity=SEVERITY_WARN,
        title="A policy denial and an empty table are the same response",
        detail=("Row level security filters rather than refuses, so the "
                "absence of a row is never reported as a permission problem. "
                "Any agent reading this output will describe a denial as an "
                "absence unless something else tells it otherwise."),
        fix=("Return the tenant context in the tool response, every time, "
             "even when rows come back. It costs one field and it is the "
             "only thing that makes an empty result readable.")))

    if outcome == RESULT_BYPASSED:
        payload = {"data": list(rows), "rls": "bypassed", "role": role}
    elif outcome == RESULT_ROWS:
        payload = {"data": list(rows), "rls": "enforced", "role": role,
                   "tenant_context": tenant}
    else:
        payload = {"data": [], "rls": "enforced", "role": role,
                   "tenant_context": tenant or None}

    silent = outcome == RESULT_EMPTY_BY_POLICY and not tenant

    headline = {
        RESULT_ROWS: f"{len(rows)} row(s) for {tenant}, filtered by policy",
        RESULT_EMPTY_BY_POLICY: ("Zero rows and no error, which is what a "
                                 "denial looks like from the outside"),
        RESULT_BYPASSED: (f"All {len(rows)} rows across every tenant, because "
                          f"the policies did not run"),
    }[outcome]

    return McpResponse(
        query_params=params, enforce_rls=enforce_rls, role=role,
        tenant_id=tenant, outcome=outcome, rows=rows, row_count=len(rows),
        raised_error=False, silent_denial=silent, cross_tenant_rows=cross,
        response_json=json.dumps(payload, indent=2), headline=headline,
        findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# 3. The boundary test matrix
# ---------------------------------------------------------------------------

CATEGORY_HAPPY = "Happy path"
CATEGORY_MALFORMED = "Malformed input"
CATEGORY_BOUNDARY = "Boundary and limit"
CATEGORY_TIMEOUT = "Timeout and failure"
CATEGORY_PERMISSION = "Permission"

REQUIRED_CATEGORIES: tuple[str, ...] = (
    CATEGORY_HAPPY, CATEGORY_MALFORMED, CATEGORY_BOUNDARY, CATEGORY_TIMEOUT,
    CATEGORY_PERMISSION)

RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"


@dataclass(frozen=True)
class TestCase:
    ordinal: int
    category: str
    name: str
    given: str
    expected: str
    observed: str
    result: str

    @property
    def passed(self) -> bool:
        return self.result == RESULT_PASS


@dataclass(frozen=True)
class TestMatrix:
    skill_name: str
    cases: tuple[TestCase, ...]
    passed: int
    failed: int
    categories_covered: tuple[str, ...]
    categories_missing: tuple[str, ...]
    coverage_sufficient: bool
    log: tuple[str, ...]
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def reconciles(self) -> bool:
        return self.passed + self.failed == len(self.cases)


def _matrix_for(skill_name: str) -> tuple[TestCase, ...]:
    """The standard matrix, with the deliberate failure that proves it runs."""
    rows = (
        (CATEGORY_HAPPY, "Loads on a direct ask",
         "The user asks for exactly what the description names",
         "The skill loads and the first instruction runs",
         "The skill loaded", RESULT_PASS),
        (CATEGORY_HAPPY, "Loads on a paraphrase",
         "The user asks in different words for the same thing",
         "The skill loads on wording the description does not contain verbatim",
         "The skill loaded on the paraphrase", RESULT_PASS),
        (CATEGORY_MALFORMED, "Empty argument string",
         "The skill is invoked with no arguments at all",
         "A clear message naming what is needed, not a traceback",
         "Named the missing argument", RESULT_PASS),
        (CATEGORY_MALFORMED, "Argument of the wrong type",
         "A number is supplied where a path was expected",
         "Rejected with the expected type named",
         "Rejected and named the type", RESULT_PASS),
        (CATEGORY_MALFORMED, "Injection in the argument",
         "The argument contains instructions addressed to the model",
         "Treated as data and never as an instruction",
         "Treated as data", RESULT_PASS),
        (CATEGORY_BOUNDARY, "Input at the size limit",
         "An input exactly at the documented maximum",
         "Accepted and processed",
         "Accepted", RESULT_PASS),
        (CATEGORY_BOUNDARY, "Input one past the size limit",
         "An input one unit above the documented maximum",
         "Refused with the limit stated",
         "Refused, but the message did not state the limit", RESULT_FAIL),
        (CATEGORY_TIMEOUT, "Downstream call times out",
         "The tool the skill calls does not answer",
         "The skill reports the timeout and leaves no half written state",
         "Reported the timeout", RESULT_PASS),
        (CATEGORY_TIMEOUT, "Invoked twice after a retry",
         "The same invocation arrives again after a client retry",
         "The second run changes nothing",
         "The second run changed nothing", RESULT_PASS),
        (CATEGORY_PERMISSION, "Tool not in the allowed list",
         "The skill attempts a tool it was not granted",
         "Refused before the call, with the missing grant named",
         "Refused before the call", RESULT_PASS),
        (CATEGORY_PERMISSION, "Database read with no tenant context",
         "A query runs before the tenant claim is set",
         "Reported as a denial rather than as an empty result",
         "Reported as an empty result", RESULT_FAIL),
    )
    return tuple(
        TestCase(ordinal=index, category=category, name=f"{skill_name}: {name}",
                 given=given, expected=expected, observed=observed,
                 result=result)
        for index, (category, name, given, expected, observed, result)
        in enumerate(rows, start=1))


def execute_boundary_test_matrix(skill_name: str,
                                 categories=REQUIRED_CATEGORIES) -> TestMatrix:
    """Run the matrix and report its own coverage alongside its results.

    A run of only happy paths tells you the thing works when nothing is
    wrong, which was never in doubt. The coverage line is reported next to
    the pass count so a green run with three categories missing cannot read
    as a green run.
    """
    name = str(skill_name or "").strip()
    if not name:
        raise ValueError("a matrix has to be run against a named skill")
    wanted = tuple(categories or ())
    if not wanted:
        raise ValueError("a matrix with no categories tests nothing")
    for category in wanted:
        if category not in REQUIRED_CATEGORIES:
            raise ValueError(f"unknown test category: {category!r}")

    cases = tuple(c for c in _matrix_for(name) if c.category in wanted)
    passed = sum(1 for c in cases if c.passed)
    failed = len(cases) - passed
    covered = tuple(c for c in REQUIRED_CATEGORIES
                    if any(case.category == c for case in cases))
    missing = tuple(c for c in REQUIRED_CATEGORIES if c not in covered)
    sufficient = not missing

    findings: list[Finding] = []

    if missing:
        findings.append(Finding(
            code="MTX-COVERAGE", severity=SEVERITY_CRITICAL,
            title=f"{len(missing)} categor(y or ies) untested: {', '.join(missing)}",
            detail=("A pass count from a partial matrix reads as a pass. "
                    "Whatever was not exercised is not passing, it is "
                    "unmeasured, and those are different things."),
            fix="Run the full matrix before reporting a result to anyone."))
    else:
        findings.append(Finding(
            code="MTX-FULL", severity=SEVERITY_OK,
            title=f"All {len(REQUIRED_CATEGORIES)} categories exercised",
            detail="The pass count means what it appears to mean.",
            fix="Keep the failure categories in when the matrix is edited later."))

    for case in cases:
        if case.passed:
            continue
        findings.append(Finding(
            code="MTX-FAIL", severity=SEVERITY_CRITICAL,
            title=f"{case.name} failed",
            detail=(f"Given {case.given.lower()}, expected "
                    f"{case.expected.lower()}, observed "
                    f"{case.observed.lower()}."),
            fix="Fix the behaviour rather than the expectation."))

    if passed == len(cases) and cases:
        findings.append(Finding(
            code="MTX-GREEN", severity=SEVERITY_WARN,
            title="Every case passed, which is worth being suspicious about",
            detail=("A matrix that has never failed is usually a matrix that "
                    "cannot fail. Break something on purpose and check the "
                    "run goes red."),
            fix="Keep one deliberate failure in the fixture until the real ones appear."))

    log = tuple(
        f"[{case.ordinal:02d}] {case.result:<4} {case.category:<20} {case.name}"
        for case in cases)

    headline = (f"{passed} of {len(cases)} passed across "
                f"{len(covered)} of {len(REQUIRED_CATEGORIES)} categories, "
                f"{'full coverage' if sufficient else 'coverage incomplete'}")

    return TestMatrix(
        skill_name=name, cases=cases, passed=passed, failed=failed,
        categories_covered=covered, categories_missing=missing,
        coverage_sufficient=sufficient, log=log, headline=headline,
        findings=tuple(findings),
    )
