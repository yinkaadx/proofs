"""Classic ASP to Linux migration engine.

No Streamlit import lives in this file. The VBScript is really parsed,
the identifiers are really classified, and the Nginx rules are really
shaped by whether the legacy URL carries a query string, because each of
those is the difference between a converter and a find and replace.

Three failures shape the file, and all three are specific to moving a
Windows hosted Classic ASP application onto Linux:

* parameter binding covers values and does not cover identifiers. A
  converter that wraps every interpolation in a placeholder produces
  code that will not run the moment the legacy page sorted by a column
  name taken from the query string, and the developer then removes the
  placeholder to make it work. So an interpolated identifier is refused
  here and an allow list is emitted in its place;
* IIS on Windows matches URLs and table names without regard to case.
  Nginx on Linux does not, and neither does MySQL's table lookup on a
  case sensitive filesystem. Every URL that used to resolve in five
  different casings now resolves in one, and every query against
  Questions now fails against questions;
* an Nginx location block never sees the query string. A redirect
  written as a location on an ASP page with an identifier in the query
  string silently matches nothing, and silently is the important word,
  because the configuration loads and the server starts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence
from urllib.parse import parse_qsl, urlparse

ENGINE_VERSION = "1.0.0"

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_CRITICAL = "critical"

_SEVERITY_ORDER = {SEVERITY_OK: 0, SEVERITY_WARN: 1, SEVERITY_CRITICAL: 2}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


def _worst(findings: Sequence[Finding]) -> str:
    if not findings:
        return SEVERITY_OK
    return max((f.severity for f in findings), key=lambda s: _SEVERITY_ORDER[s])


# ---------------------------------------------------------------------------
# 1. VBScript query conversion
# ---------------------------------------------------------------------------

SLOT_VALUE = "value"
SLOT_IDENTIFIER = "identifier"

SOURCE_QUERYSTRING = "Request.QueryString"
SOURCE_FORM = "Request.Form"
SOURCE_COOKIES = "Request.Cookies"
SOURCE_SERVER = "Request.ServerVariables"
SOURCE_AMBIGUOUS = "Request, collection not stated"
SOURCE_LOCAL = "A local VBScript variable"

CONVERTED = "Converted, every value is bound"
BLOCKED = "Blocked, an identifier is interpolated"
NOTHING_TO_DO = "No concatenation found"

# Classic ASP resolves a bare Request("x") against QueryString, Form,
# Cookies, ClientCertificate, and then ServerVariables, in that order.
REQUEST_SEARCH_ORDER = ("QueryString", "Form", "Cookies",
                        "ClientCertificate", "ServerVariables")

# A concatenation that follows one of these keywords is being used as an
# identifier rather than as a value, and no placeholder can stand there.
_IDENTIFIER_KEYWORDS = ("order by", "group by", "from", "join", "into",
                        "update", "table", "asc", "desc")

_ASSIGNMENT = re.compile(
    r"""(?im)^\s*(?:dim\s+\w+\s*:\s*)?
        (?P<name>\w+)\s*=\s*(?P<body>(?:"[^"]*"|[^\r\n])+)$""",
    re.VERBOSE)

_REQUEST_CALL = re.compile(
    r"""Request(?:\s*\.\s*(?P<collection>QueryString|Form|Cookies
        |ServerVariables))?\s*\(\s*(?P<quote>["'])(?P<key>[^"']+)(?P=quote)
        \s*\)""",
    re.IGNORECASE | re.VERBOSE)


@dataclass(frozen=True)
class Slot:
    position: int
    expression: str
    slot_kind: str
    source: str
    key: str
    preceding_sql: str

    @property
    def bindable(self) -> bool:
        return self.slot_kind == SLOT_VALUE

    @property
    def placeholder(self) -> str:
        return f":p{self.position}"


@dataclass(frozen=True)
class Conversion:
    legacy_asp_code: str
    sql_template: str
    slots: tuple[Slot, ...]
    php_pdo: str
    python_psycopg: str
    status: str
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def converted(self) -> bool:
        return self.status == CONVERTED

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def value_slots(self) -> tuple[Slot, ...]:
        return tuple(s for s in self.slots if s.bindable)

    @property
    def identifier_slots(self) -> tuple[Slot, ...]:
        return tuple(s for s in self.slots if not s.bindable)


def _split_concatenation(body: str) -> list[tuple[str, str]]:
    """Split a VBScript concatenation into literal and expression parts.

    The ampersands that matter are the ones at parenthesis depth zero and
    outside a string. A quote inside Request.QueryString("id") belongs to
    the expression, not to the SQL, and a splitter that does not track
    depth turns that argument into a SQL fragment and the whole parse into
    nonsense.
    """
    chunks: list[str] = []
    buffer: list[str] = []
    depth = 0
    in_string = False
    index = 0
    while index < len(body):
        char = body[index]
        if in_string:
            if char == '"':
                if index + 1 < len(body) and body[index + 1] == '"':
                    buffer.append('""')
                    index += 2
                    continue
                in_string = False
            buffer.append(char)
            index += 1
            continue
        if char == '"':
            in_string = True
            buffer.append(char)
            index += 1
            continue
        if char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        elif char == "&" and depth == 0:
            chunks.append("".join(buffer))
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    chunks.append("".join(buffer))

    parts: list[tuple[str, str]] = []
    for chunk in chunks:
        text = chunk.strip()
        if not text:
            continue
        if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
            parts.append(("literal", text[1:-1].replace('""', '"')))
        else:
            parts.append(("expr", text))
    return parts


def _classify_source(expression: str) -> tuple[str, str]:
    match = _REQUEST_CALL.search(expression)
    if not match:
        return SOURCE_LOCAL, expression.strip()
    collection = match.group("collection")
    key = match.group("key")
    if collection is None:
        return SOURCE_AMBIGUOUS, key
    lookup = {
        "querystring": SOURCE_QUERYSTRING, "form": SOURCE_FORM,
        "cookies": SOURCE_COOKIES, "servervariables": SOURCE_SERVER,
    }
    return lookup[collection.lower()], key


def _classify_slot(preceding: str) -> str:
    trimmed = preceding.rstrip()
    lowered = trimmed.lower()
    if lowered.endswith("'") or lowered.endswith('"'):
        return SLOT_VALUE
    tail = re.split(r"[\s(,]+", lowered)
    tail = [token for token in tail if token]
    if tail and tail[-1] in _IDENTIFIER_KEYWORDS:
        return SLOT_IDENTIFIER
    if len(tail) >= 2 and tail[-2] in ("order", "group") and \
            tail[-1] == "by":
        return SLOT_IDENTIFIER
    return SLOT_VALUE


_PHP_SOURCE = {
    SOURCE_QUERYSTRING: "$_GET",
    SOURCE_FORM: "$_POST",
    SOURCE_COOKIES: "$_COOKIE",
    SOURCE_SERVER: "$_SERVER",
    SOURCE_AMBIGUOUS: "$_REQUEST",
    SOURCE_LOCAL: "$local",
}

_PY_SOURCE = {
    SOURCE_QUERYSTRING: "request.args",
    SOURCE_FORM: "request.form",
    SOURCE_COOKIES: "request.cookies",
    SOURCE_SERVER: "request.environ",
    SOURCE_AMBIGUOUS: "request.values",
    SOURCE_LOCAL: "local",
}


def convert_vbscript_query(legacy_asp_code: str) -> Conversion:
    """Rebuild a concatenated VBScript query as a parameterised statement.

    An identifier interpolation stops the conversion rather than being
    wrapped in a placeholder. A placeholder cannot stand where a column
    name goes, so emitting one produces code that fails on the first run,
    and the developer's fix is to take the placeholder back out. Refusing
    and handing over an allow list is the only version of this that leaves
    the application safe.
    """
    source = str(legacy_asp_code or "")
    findings: list[Finding] = []

    body = ""
    for match in _ASSIGNMENT.finditer(source):
        candidate = match.group("body").strip()
        if "&" in candidate and '"' in candidate:
            body = candidate
            break
    if not body:
        quoted = re.findall(r'"[^"]*"', source)
        body = quoted[0] if quoted else ""

    parts = _split_concatenation(body) if body else []
    template_pieces: list[str] = []
    slots: list[Slot] = []
    position = 0

    for kind, text in parts:
        if kind == "literal":
            template_pieces.append(text)
            continue
        preceding = "".join(template_pieces)
        slot_kind = _classify_slot(preceding)
        origin, key = _classify_source(text)
        position += 1
        slot = Slot(position=position, expression=text, slot_kind=slot_kind,
                    source=origin, key=key, preceding_sql=preceding[-40:])
        slots.append(slot)
        if slot_kind == SLOT_VALUE:
            trimmed = "".join(template_pieces).rstrip()
            if trimmed.endswith("'"):
                template_pieces[-1] = template_pieces[-1].rstrip()[:-1]
            template_pieces.append(slot.placeholder)
        else:
            template_pieces.append(f"{{{{{slot.slot_kind}_{position}}}}}")

    template = "".join(template_pieces)
    template = re.sub(r"'\s*(:p\d+)\s*'", r"\1", template)
    template = re.sub(r"'\s*(:p\d+)", r"\1", template)
    template = re.sub(r"(:p\d+)\s*'", r"\1", template)
    template = re.sub(r"\s+", " ", template).strip()

    identifier_slots = [s for s in slots if not s.bindable]
    value_slots = [s for s in slots if s.bindable]

    if not slots:
        status = NOTHING_TO_DO
    elif identifier_slots:
        status = BLOCKED
    else:
        status = CONVERTED

    if status == CONVERTED:
        bindings = ",\n".join(
            f"    '{s.placeholder}' => {_PHP_SOURCE[s.source]}"
            f"['{s.key}'] ?? null" for s in value_slots)
        php = (
            "<?php\n"
            "// Prepared once, executed with the values bound. The driver\n"
            "// sends the statement and the values separately, so nothing\n"
            "// a visitor types can change the shape of the query.\n"
            "$stmt = $pdo->prepare(\n"
            f"    '{template}'\n"
            ");\n"
            "$stmt->execute([\n"
            f"{bindings}\n"
            "]);\n"
            "$rows = $stmt->fetchAll(PDO::FETCH_ASSOC);")

        py_args = ", ".join(
            f'"p{s.position}": {_PY_SOURCE[s.source]}.get("{s.key}")'
            for s in value_slots)
        py_template = re.sub(r":p(\d+)", r"%(p\1)s", template)
        python = (
            "import psycopg\n\n"
            "# psycopg sends the statement and the parameters over the\n"
            "# extended protocol, so the values are never parsed as SQL.\n"
            "with connection.cursor(row_factory=psycopg.rows.dict_row) "
            "as cur:\n"
            f'    cur.execute(\n        "{py_template}",\n'
            f"        {{{py_args}}},\n    )\n"
            "    rows = cur.fetchall()")
    else:
        php = ""
        python = ""

    if status == NOTHING_TO_DO:
        findings.append(Finding(
            code="ASP-NONE", severity=SEVERITY_WARN,
            title="No string concatenation was found in this snippet",
            detail=("Either the query is already a constant, or the "
                    "concatenation happens somewhere this snippet does not "
                    "show. A constant query needs no parameters and a "
                    "hidden one still needs finding."),
            fix="Paste the lines that build the SQL, including any helper."))
    elif identifier_slots:
        names = ", ".join(s.expression for s in identifier_slots)
        findings.append(Finding(
            code="ASP-IDENT", severity=SEVERITY_CRITICAL,
            title=f"An identifier is interpolated: {names}",
            detail=("A bound parameter can stand where a value goes and "
                    "nowhere else. A column or table name has to be part of "
                    "the statement text, so wrapping this in a placeholder "
                    "produces code that fails on its first run, and the fix "
                    "a developer reaches for under pressure is taking the "
                    "placeholder back out."),
            fix=("Emit an allow list: map the incoming value to a fixed set "
                 "of column names you control, and reject anything not in "
                 "the map.")))
        allow = "\n".join(
            f"// {s.expression} must resolve through a map you own:\n"
            f"$columns = ['newest' => 'created_at', 'title' => 'title'];\n"
            f"$column = $columns[{_PHP_SOURCE[s.source]}"
            f"['{s.key}'] ?? ''] ?? 'created_at';"
            for s in identifier_slots)
        php = ("<?php\n"
               "// No parameterised statement is emitted while an "
               "identifier\n"
               "// is interpolated. Resolve it through an allow list "
               "first.\n"
               f"{allow}")
        python = ("# No parameterised statement is emitted while an "
                  "identifier\n"
                  "# is interpolated. Resolve it through an allow list "
                  "first.\n"
                  'COLUMNS = {"newest": "created_at", "title": "title"}\n'
                  'column = COLUMNS.get(request.args.get("sort", ""), '
                  '"created_at")')
    else:
        findings.append(Finding(
            code="ASP-BOUND", severity=SEVERITY_OK,
            title=f"{len(value_slots)} value(s) bound, none interpolated",
            detail=("The driver sends the statement and the values "
                    "separately, so nothing a visitor types can change the "
                    "shape of the query. That is a property of the "
                    "protocol rather than of any escaping function."),
            fix="Keep the statement text a constant in the source file."))

    mixed_case = sorted({
        token for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b",
                                      template)
        if token.lower() != token and token.upper() != token
        and token.lower() not in ("select", "from", "where", "and", "or",
                                  "order", "group", "by", "join", "on",
                                  "insert", "update", "delete", "set",
                                  "values", "into", "null", "not", "like")})
    if mixed_case:
        findings.append(Finding(
            code="ASP-TABLECASE", severity=SEVERITY_CRITICAL,
            title=f"Mixed case identifiers survive into the new statement: "
                  f"{', '.join(mixed_case)}",
            detail=("The old server was Windows, where MySQL resolved "
                    "Questions and questions as the same table and "
                    "PostgreSQL was not in the picture. On Linux the table "
                    "name is a filename and the match is exact, and "
                    "PostgreSQL folds an unquoted identifier to lower case "
                    "so Questions becomes questions anyway. The statement "
                    "above is parameterised and will still fail on the "
                    "first run."),
            fix=("Lower case every table and column name in the migration "
                 "and in the query, in the same change.")))

    ambiguous = [s for s in slots if s.source == SOURCE_AMBIGUOUS]
    if ambiguous:
        findings.append(Finding(
            code="ASP-REQUEST", severity=SEVERITY_CRITICAL,
            title=f"{len(ambiguous)} bare Request() call(s) with no "
                  f"collection named",
            detail=(f"Classic ASP resolves Request(\"x\") against "
                    f"{', '.join(REQUEST_SEARCH_ORDER)}, in that order. A "
                    f"value the page expected from the query string can "
                    f"arrive in a cookie, and the cookie wins over the form "
                    f"post. Mapping this to $_REQUEST carries the ambiguity "
                    f"into the new stack rather than resolving it."),
            fix=("Decide which collection each call meant and map it to "
                 "$_GET or $_POST explicitly. Never to $_REQUEST.")))

    if re.search(r"Replace\s*\(.*?\"'\".*?\"''\"", source, re.IGNORECASE):
        findings.append(Finding(
            code="ASP-ESCAPE", severity=SEVERITY_CRITICAL,
            title="Doubling single quotes is not a defence",
            detail=("Replace(value, \"'\", \"''\") does nothing at all in a "
                    "numeric context, where the value is not quoted, and "
                    "nothing about backslash escaping. It is the escaping "
                    "idiom that makes a legacy page look protected in a "
                    "code review."),
            fix="Delete it. A bound parameter needs no escaping at all."))

    if re.search(r"conn(?:ection)?\s*\.\s*Execute", source, re.IGNORECASE):
        findings.append(Finding(
            code="ASP-EXECUTE", severity=SEVERITY_WARN,
            title="Connection.Execute takes a string and only a string",
            detail=("The ADO method used here has no parameter list, so "
                    "there was never a safe way to call it with visitor "
                    "input. The migration is the first opportunity this "
                    "code has had."),
            fix="Replace with a prepared statement, not with a safer string."))

    headline = (f"{len(value_slots)} value(s) bound, "
                f"{len(identifier_slots)} identifier(s) refused")

    return Conversion(
        legacy_asp_code=source, sql_template=template, slots=tuple(slots),
        php_pdo=php, python_psycopg=python, status=status,
        headline=headline, findings=tuple(findings))


SAMPLE_ASP_INJECTION = (
    'sql = "SELECT * FROM Questions WHERE QuestionID = " & '
    'Request.QueryString("id") & " AND CategoryName = \'" & '
    'Request("cat") & "\'"\n'
    "Set rs = conn.Execute(sql)")

SAMPLE_ASP_IDENTIFIER = (
    'sql = "SELECT * FROM Questions WHERE CategoryID = " & '
    'Request.QueryString("cat") & " ORDER BY " & '
    'Request.QueryString("sort")\n'
    "Set rs = conn.Execute(sql)")


# ---------------------------------------------------------------------------
# 2. Database schema mapping
# ---------------------------------------------------------------------------

TARGET_MYSQL = "MySQL 8"
TARGET_POSTGRES = "PostgreSQL 16"

TARGETS: tuple[str, ...] = (TARGET_MYSQL, TARGET_POSTGRES)


@dataclass(frozen=True)
class ColumnMapping:
    legacy_name: str
    legacy_type: str
    new_name: str
    mysql_type: str
    postgres_type: str
    nullable: bool
    note: str


@dataclass(frozen=True)
class SchemaMapping:
    legacy_table_name: str
    new_table_name: str
    columns: tuple[ColumnMapping, ...]
    mysql_ddl: str
    postgres_ddl: str
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def _snake(name: str) -> str:
    stepped = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name))
    stepped = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", stepped)
    return re.sub(r"[^0-9a-zA-Z]+", "_", stepped).strip("_").lower()


_LEGACY_TABLES: dict[str, tuple[ColumnMapping, ...]] = {
    "tblQuestions": (
        ColumnMapping("QuestionID", "AutoNumber", "question_id",
                      "BIGINT UNSIGNED NOT NULL AUTO_INCREMENT",
                      "BIGINT GENERATED ALWAYS AS IDENTITY", False,
                      "Access AutoNumber becomes an identity column."),
        ColumnMapping("CategoryID", "Long Integer", "category_id",
                      "BIGINT UNSIGNED NOT NULL", "BIGINT NOT NULL", False,
                      "Foreign key to the category table."),
        ColumnMapping("QuestionText", "Memo", "question_text",
                      "TEXT NOT NULL", "TEXT NOT NULL", False,
                      "Memo has no length limit and maps to TEXT."),
        ColumnMapping("Explanation", "Memo", "explanation",
                      "TEXT NULL", "TEXT", True,
                      "Shown after the attempt is scored."),
        ColumnMapping("Difficulty", "Text(50)", "difficulty",
                      "VARCHAR(50) NULL", "VARCHAR(50)", True,
                      "A free text field that wants to be an enum."),
        ColumnMapping("IsActive", "Yes/No", "is_active",
                      "TINYINT(1) NOT NULL DEFAULT 1",
                      "BOOLEAN NOT NULL DEFAULT TRUE", False,
                      "Access Yes/No cannot be null. The new column must "
                      "not be either, or a third state appears."),
        ColumnMapping("DateAdded", "Date/Time", "created_at",
                      "DATETIME NOT NULL", "TIMESTAMPTZ NOT NULL", False,
                      "Access stores no time zone at all, so the "
                      "conversion has to name one."),
    ),
    "tblAnswers": (
        ColumnMapping("AnswerID", "AutoNumber", "answer_id",
                      "BIGINT UNSIGNED NOT NULL AUTO_INCREMENT",
                      "BIGINT GENERATED ALWAYS AS IDENTITY", False,
                      "Identity column."),
        ColumnMapping("QuestionID", "Long Integer", "question_id",
                      "BIGINT UNSIGNED NOT NULL", "BIGINT NOT NULL", False,
                      "Foreign key to the question table."),
        ColumnMapping("AnswerText", "Text(255)", "answer_text",
                      "VARCHAR(255) NOT NULL", "VARCHAR(255) NOT NULL",
                      False, "Text(255) is the Access default width."),
        ColumnMapping("IsCorrect", "Yes/No", "is_correct",
                      "TINYINT(1) NOT NULL DEFAULT 0",
                      "BOOLEAN NOT NULL DEFAULT FALSE", False,
                      "A nullable version of this column would silently "
                      "mark an answer neither right nor wrong."),
        ColumnMapping("SortOrder", "Integer", "sort_order",
                      "SMALLINT NOT NULL DEFAULT 0",
                      "SMALLINT NOT NULL DEFAULT 0", False,
                      "Display order within the question."),
    ),
    "tblUsers": (
        ColumnMapping("UserID", "AutoNumber", "user_id",
                      "BIGINT UNSIGNED NOT NULL AUTO_INCREMENT",
                      "BIGINT GENERATED ALWAYS AS IDENTITY", False,
                      "Identity column."),
        ColumnMapping("UserName", "Text(100)", "username",
                      "VARCHAR(100) NOT NULL", "CITEXT NOT NULL", False,
                      "The legacy lookup was case insensitive. PostgreSQL "
                      "is not, so this needs CITEXT or a lowered index."),
        ColumnMapping("EmailAddress", "Text(255)", "email",
                      "VARCHAR(255) NOT NULL", "CITEXT NOT NULL", False,
                      "Same again, and this one is a login path."),
        ColumnMapping("PasswordHash", "Text(255)", "password_hash",
                      "VARCHAR(255) NOT NULL", "TEXT NOT NULL", False,
                      "If the legacy column held MD5 or plain text, the "
                      "migration is the moment to force a reset."),
        ColumnMapping("LastLogin", "Date/Time", "last_login_at",
                      "DATETIME NULL", "TIMESTAMPTZ", True,
                      "Null until the first sign in."),
    ),
    "tblTestAttempts": (
        ColumnMapping("AttemptID", "AutoNumber", "attempt_id",
                      "BIGINT UNSIGNED NOT NULL AUTO_INCREMENT",
                      "BIGINT GENERATED ALWAYS AS IDENTITY", False,
                      "Identity column."),
        ColumnMapping("UserID", "Long Integer", "user_id",
                      "BIGINT UNSIGNED NOT NULL", "BIGINT NOT NULL", False,
                      "Foreign key to the user table."),
        ColumnMapping("Score", "Double", "score",
                      "DECIMAL(5,2) NOT NULL", "NUMERIC(5,2) NOT NULL",
                      False,
                      "A percentage stored as a float rounds differently "
                      "on every platform. Decimal does not."),
        ColumnMapping("StartedOn", "Date/Time", "started_at",
                      "DATETIME NOT NULL", "TIMESTAMPTZ NOT NULL", False,
                      "Time zone has to be decided, not inherited."),
        ColumnMapping("CompletedOn", "Date/Time", "completed_at",
                      "DATETIME NULL", "TIMESTAMPTZ", True,
                      "Null while the attempt is in progress."),
    ),
}

LEGACY_TABLES: tuple[str, ...] = tuple(_LEGACY_TABLES)


def map_database_schema(legacy_table_name: str) -> SchemaMapping:
    """Map one legacy table onto MySQL and PostgreSQL, with the traps named.

    The type mapping is the easy half. The half that breaks a migration is
    that Access and SQL Server matched text without regard to case and the
    Linux targets do not, so a sign in that worked on the old box fails on
    the new one for every user who capitalised their name differently from
    the day they registered.
    """
    legacy = str(legacy_table_name or "").strip()
    if legacy not in _LEGACY_TABLES:
        raise ValueError(f"unknown legacy table {legacy_table_name!r}")

    columns = _LEGACY_TABLES[legacy]
    new_name = _snake(re.sub(r"^tbl", "", legacy))
    findings: list[Finding] = []

    mysql_lines = [f"CREATE TABLE `{new_name}` ("]
    for column in columns:
        mysql_lines.append(f"  `{column.new_name}` {column.mysql_type},")
    primary = columns[0].new_name
    mysql_lines.append(f"  PRIMARY KEY (`{primary}`)")
    mysql_lines.append(") ENGINE=InnoDB")
    mysql_lines.append("  DEFAULT CHARSET=utf8mb4")
    mysql_lines.append("  COLLATE=utf8mb4_0900_ai_ci;")
    mysql_ddl = "\n".join(mysql_lines)

    pg_lines = [f"CREATE TABLE {new_name} ("]
    for column in columns:
        pg_lines.append(f"  {column.new_name} {column.postgres_type},")
    pg_lines.append(f"  PRIMARY KEY ({primary})")
    pg_lines.append(");")
    postgres_ddl = "\n".join(pg_lines)

    findings.append(Finding(
        code="SCH-UTF8MB4", severity=SEVERITY_CRITICAL,
        title="MySQL utf8 is not UTF-8 and will truncate",
        detail=("The character set named utf8 in MySQL stores three bytes "
                "per character and cannot hold anything outside the basic "
                "multilingual plane. An emoji or a less common CJK "
                "character in a pasted question body is silently truncated "
                "or rejected. The four byte set is utf8mb4, which is what "
                "the DDL above uses."),
        fix="Set utf8mb4 on the database, the table, and the connection."))

    findings.append(Finding(
        code="SCH-CASE", severity=SEVERITY_CRITICAL,
        title="The old lookups were case insensitive and these are not",
        detail=("Access and SQL Server default collations match text "
                "without regard to case, so a sign in with Bob found the "
                "row stored as bob. PostgreSQL compares exactly, so the "
                "same sign in now fails for every user who typed their "
                "name differently from the day they registered, and it "
                "fails as a wrong password rather than as an error."),
        fix=("Use CITEXT in PostgreSQL for username and email, or index "
             "lower(column) and always compare lowered.")))

    findings.append(Finding(
        code="SCH-TABLECASE", severity=SEVERITY_CRITICAL,
        title="MySQL table names are case sensitive on Linux and were not "
              "on Windows",
        detail=("Table names are stored as filenames, so the same schema "
                "that resolved Questions and questions interchangeably on "
                "the old Windows server resolves only one of them here. "
                "Every query written with a different capitalisation "
                "fails, and they fail one page at a time."),
        fix=("Name every table in lower snake case, as above, and set "
             "lower_case_table_names before the first import rather than "
             "after.")))

    booleans = [c for c in columns if c.legacy_type == "Yes/No"]
    if booleans:
        findings.append(Finding(
            code="SCH-BOOL", severity=SEVERITY_WARN,
            title=f"{len(booleans)} Yes/No column(s) must stay not null",
            detail=("An Access Yes/No column has two states. A nullable "
                    "BOOLEAN has three, and the application has no branch "
                    "for the third. An answer that is neither correct nor "
                    "incorrect scores as incorrect in most code paths and "
                    "as correct in a few."),
            fix="Keep NOT NULL with a default, exactly as the DDL has it."))

    dates = [c for c in columns if c.legacy_type == "Date/Time"]
    if dates:
        findings.append(Finding(
            code="SCH-TZ", severity=SEVERITY_WARN,
            title=f"{len(dates)} date column(s) carry no time zone today",
            detail=("Access and the legacy SQL Server column store a local "
                    "wall clock with no zone, and the old server's zone "
                    "was whatever Windows was set to. The conversion has "
                    "to name a zone rather than inherit one, and the "
                    "decision belongs to whoever knows where the users "
                    "are."),
            fix=("Decide the source zone, convert on import, and store "
                 "TIMESTAMPTZ from then on.")))

    findings.append(Finding(
        code="SCH-RENAME", severity=SEVERITY_WARN,
        title="Renaming columns breaks every query at once, on purpose",
        detail=("Moving QuestionID to question_id means nothing compiles "
                "until it is all changed, which is the point. A migration "
                "that keeps the old names lets half converted pages run "
                "against the new database and fail somewhere else."),
        fix=("Rename in the migration and fix the call sites in the same "
             "change, with a view carrying the old names only if a third "
             "party integration needs it.")))

    headline = (f"{legacy} becomes {new_name}: {len(columns)} column(s) "
                f"mapped")

    return SchemaMapping(
        legacy_table_name=legacy, new_table_name=new_name, columns=columns,
        mysql_ddl=mysql_ddl, postgres_ddl=postgres_ddl, headline=headline,
        findings=tuple(findings))


# ---------------------------------------------------------------------------
# 3. SEO 301 redirect mapping
# ---------------------------------------------------------------------------

REDIRECT_EXACT = "Exact path, no query string"
REDIRECT_QUERY = "Query string carries the identifier"

_PATH_RENAMES = {
    "default": "", "index": "", "home": "",
    "quiz": "practice", "test": "practice", "question": "questions",
    "questions": "questions", "results": "results", "login": "sign-in",
    "register": "sign-up", "category": "categories",
}

_ID_KEYS = ("id", "qid", "questionid", "catid", "categoryid", "testid")


@dataclass(frozen=True)
class RedirectMapping:
    legacy_asp_url: str
    legacy_path: str
    query_pairs: tuple[tuple[str, str], ...]
    new_path: str
    strategy: str
    nginx_rule: str
    case_insensitive_rule: str
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def uses_query_string(self) -> bool:
        return self.strategy == REDIRECT_QUERY

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def generate_seo_301_mapping(legacy_asp_url: str) -> RedirectMapping:
    """Emit an Nginx 301 rule shaped by whether the old URL had arguments.

    An Nginx location block is matched against the normalised path and
    never against the query string, so the obvious rule for an ASP page
    with an identifier in its arguments matches nothing at all. The
    configuration loads, the server starts, and every one of those URLs
    serves a 404 with a redirect rule sitting in the file that looks like
    it covers them.
    """
    raw = str(legacy_asp_url or "").strip()
    if not raw:
        raise ValueError("a legacy URL is required")

    parsed = urlparse(raw if "//" in raw else f"http://legacy.invalid{raw}")
    path = parsed.path or "/"
    if not path.lower().endswith(".asp"):
        raise ValueError("this maps Classic ASP URLs, so the path has to "
                         "end in .asp")

    pairs = tuple(parse_qsl(parsed.query, keep_blank_values=True))
    stem = re.sub(r"\.asp$", "", path.rsplit("/", 1)[-1], flags=re.IGNORECASE)
    prefix = path.rsplit("/", 1)[0].strip("/")
    findings: list[Finding] = []

    segment = _PATH_RENAMES.get(stem.lower(), _snake(stem))
    identifier = next((value for key, value in pairs
                       if key.lower() in _ID_KEYS and value), "")

    segments = [part for part in (prefix, segment) if part]
    if identifier:
        segments.append(identifier)
    new_path = "/" + "/".join(segments)
    if not new_path.endswith("/"):
        new_path += "/"
    new_path = re.sub(r"/{2,}", "/", new_path)

    if pairs:
        strategy = REDIRECT_QUERY
        exact = path + "?" + parsed.query
        nginx_rule = (
            "# A location block is matched against the path only, so this\n"
            "# one has to key on $request_uri, which includes the query\n"
            "# string. The map runs before any server block is chosen.\n"
            "map $request_uri $legacy_301 {\n"
            '    default "";\n'
            f'    "{exact}" "{new_path}";\n'
            "}\n\n"
            "server {\n"
            "    # return inside if is one of the two safe uses of if.\n"
            "    if ($legacy_301) {\n"
            "        return 301 $legacy_301;\n"
            "    }\n"
            "}")
        case_rule = (
            "# IIS matched the path without regard to case and Nginx does\n"
            "# not, so the same page had several live URLs and now has\n"
            "# one. The regex location catches the rest.\n"
            f"location ~* ^{re.escape(path)}$ {{\n"
            f"    if ($arg_{next((k for k, _ in pairs), 'id')}) {{\n"
            f"        return 301 {new_path};\n"
            "    }\n"
            f"    return 301 {'/' + segment + '/' if segment else '/'};\n"
            "}")
    else:
        strategy = REDIRECT_EXACT
        nginx_rule = (
            "# An exact match location is the cheapest form Nginx has and\n"
            "# it is checked before any prefix or regex location.\n"
            f"location = {path} {{\n"
            f"    return 301 {new_path};\n"
            "}")
        case_rule = (
            "# Case insensitive regex, because IIS served this page at\n"
            "# every capitalisation and the links in the wild use all of\n"
            "# them.\n"
            f"location ~* ^{re.escape(path)}$ {{\n"
            f"    return 301 {new_path};\n"
            "}")

    findings.append(Finding(
        code="SEO-LOCATION", severity=SEVERITY_CRITICAL,
        title="A location block never sees the query string",
        detail=("Nginx matches a location against the normalised path "
                "alone. A rule written as a location on an ASP page whose "
                "identity lives in its arguments matches nothing, the "
                "configuration loads without complaint, and every one of "
                "those URLs serves a 404 while a redirect rule sits in the "
                "file looking like it covers them."),
        fix=("Key on $request_uri through a map, or test $arg_name inside "
             "the location, as the rules above do.")))

    findings.append(Finding(
        code="SEO-CASE", severity=SEVERITY_CRITICAL,
        title="IIS matched URLs without regard to case and Nginx does not",
        detail=("The same page was reachable at /Default.asp, /default.asp "
                "and /DEFAULT.ASP on the old server, and links in the wild "
                "use all of them. On Linux only the exact spelling "
                "resolves, so the rest become 404s carrying whatever "
                "ranking they had accumulated."),
        fix=("Add the case insensitive regex location beside the exact "
             "one, and keep the exact one first because it is cheaper.")))

    if pairs:
        findings.append(Finding(
            code="SEO-ARGS", severity=SEVERITY_WARN,
            title="A replacement URI with no query string keeps the old "
                  "arguments",
            detail=("Nginx appends the original query string to a 301 "
                    "target that does not already contain one. That is "
                    "usually wrong after a rewrite to a clean path, and it "
                    "leaves the old identifier on the end of the new URL "
                    "where a crawler will treat it as a separate page."),
            fix=("End the replacement with a question mark to strip the "
                 "arguments deliberately, for example return 301 "
                 f"{new_path}?")))

    if len(pairs) > 1:
        findings.append(Finding(
            code="SEO-PERMUTE", severity=SEVERITY_WARN,
            title=f"{len(pairs)} arguments means the order is not fixed",
            detail=("A map keyed on the exact request URI matches one "
                    "ordering of the arguments. Real links carry them in "
                    "whatever order they were written, so a map built from "
                    "the site's own links will miss the ones built by "
                    "hand."),
            fix=("Key on the arguments individually with $arg_name, or "
                 "generate a map entry per ordering found in the access "
                 "logs.")))

    findings.append(Finding(
        code="SEO-CHAIN", severity=SEVERITY_WARN,
        title="Map to the final URL, never through another redirect",
        detail=("Each hop costs a round trip on a connection the visitor "
                "is waiting on, and a chain that passes through a rule "
                "somebody later deletes breaks silently. The old URL "
                "should reach its destination in one response."),
        fix=("Resolve chains while the old site is still up and you can "
             "still follow them.")))

    findings.append(Finding(
        code="SEO-INVENTORY", severity=SEVERITY_CRITICAL,
        title="Build the list from logs and Search Console, not the sitemap",
        detail=("The pages that carry ranking are not the pages the old "
                "sitemap lists. They are the ones in the access logs and "
                "in the Search Console performance report, and those "
                "include URLs nothing on the site has linked to for a "
                "decade."),
        fix=("Export twelve months of access logs and the full Search "
             "Console URL list before the old server is switched off.")))

    headline = f"{path} becomes {new_path}"
    return RedirectMapping(
        legacy_asp_url=raw, legacy_path=path, query_pairs=pairs,
        new_path=new_path, strategy=strategy, nginx_rule=nginx_rule,
        case_insensitive_rule=case_rule, headline=headline,
        findings=tuple(findings))


SAMPLE_URLS: tuple[str, ...] = (
    "/quiz.asp?id=42",
    "/Default.asp",
    "/category.asp?catid=7&sort=newest",
    "/practice/results.asp?testid=1184",
    "/login.asp",
)
