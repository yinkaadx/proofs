"""Tests for the Classic ASP to Linux Migration Console.

The conversion tests are about the refusal, because a converter that
wraps an identifier in a placeholder ships code that fails on its first
run and gets unwrapped by hand. The redirect tests are about the Nginx
rule shape, because the wrong shape matches nothing and the server starts
without complaint.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

import pytest

from tools.classic_asp_linux_migration_console.core import (
    BLOCKED,
    CONVERTED,
    ENGINE_VERSION,
    LEGACY_TABLES,
    NOTHING_TO_DO,
    REDIRECT_EXACT,
    REDIRECT_QUERY,
    REQUEST_SEARCH_ORDER,
    SAMPLE_ASP_IDENTIFIER,
    SAMPLE_ASP_INJECTION,
    SAMPLE_URLS,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SLOT_IDENTIFIER,
    SLOT_VALUE,
    SOURCE_AMBIGUOUS,
    SOURCE_FORM,
    SOURCE_QUERYSTRING,
    convert_vbscript_query,
    generate_seo_301_mapping,
    map_database_schema,
)
from tools.registry import all_tools

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL_DIR = ROOT / "tools" / "classic_asp_linux_migration_console"


# ---------------------------------------------------------------------------
# VBScript conversion
# ---------------------------------------------------------------------------

def test_a_concatenated_query_becomes_a_bound_statement():
    conversion = convert_vbscript_query(SAMPLE_ASP_INJECTION)
    assert conversion.status == CONVERTED
    assert conversion.converted
    assert len(conversion.value_slots) == 2
    assert conversion.identifier_slots == ()


def test_the_quotes_inside_a_request_call_are_not_treated_as_sql():
    """A splitter that does not track parenthesis depth turns the argument
    of Request.QueryString into a SQL fragment and the parse into
    nonsense."""
    conversion = convert_vbscript_query(SAMPLE_ASP_INJECTION)
    assert conversion.sql_template == (
        "SELECT * FROM Questions WHERE QuestionID = :p1 "
        "AND CategoryName = :p2")
    assert [s.key for s in conversion.slots] == ["id", "cat"]


def test_the_surrounding_single_quotes_are_removed_with_the_placeholder():
    """A placeholder inside quotes is a literal string containing a colon,
    not a parameter."""
    conversion = convert_vbscript_query(SAMPLE_ASP_INJECTION)
    assert "':p" not in conversion.sql_template
    assert "'" not in conversion.sql_template


def test_an_interpolated_identifier_blocks_the_whole_conversion():
    conversion = convert_vbscript_query(SAMPLE_ASP_IDENTIFIER)
    assert conversion.status == BLOCKED
    assert not conversion.converted
    assert len(conversion.identifier_slots) == 1
    assert conversion.identifier_slots[0].key == "sort"
    assert "ASP-IDENT" in {f.code for f in conversion.findings}


def test_a_blocked_conversion_emits_no_prepared_statement():
    """Emitting one would ship code that fails on its first run, and the
    fix reached for under pressure is taking the placeholder back out."""
    conversion = convert_vbscript_query(SAMPLE_ASP_IDENTIFIER)
    assert "prepare(" not in conversion.php_pdo
    assert "cur.execute" not in conversion.python_psycopg
    assert "allow list" in conversion.php_pdo


def test_a_blocked_conversion_still_hands_over_an_allow_list():
    conversion = convert_vbscript_query(SAMPLE_ASP_IDENTIFIER)
    assert "$columns = [" in conversion.php_pdo
    assert "COLUMNS = {" in conversion.python_psycopg


def test_order_by_group_by_and_from_all_classify_as_identifiers():
    cases = {
        'sql = "SELECT * FROM t ORDER BY " & Request.QueryString("s")':
            "ORDER BY",
        'sql = "SELECT * FROM t GROUP BY " & Request.QueryString("s")':
            "GROUP BY",
        'sql = "SELECT * FROM " & Request.QueryString("s")': "FROM",
    }
    for snippet, label in cases.items():
        conversion = convert_vbscript_query(snippet)
        assert conversion.status == BLOCKED, label
        assert conversion.slots[-1].slot_kind == SLOT_IDENTIFIER, label


def test_a_value_after_a_comparison_is_never_classified_as_an_identifier():
    conversion = convert_vbscript_query(
        'sql = "SELECT * FROM t WHERE id = " & Request.QueryString("id")')
    assert conversion.slots[0].slot_kind == SLOT_VALUE
    assert conversion.status == CONVERTED


def test_each_request_collection_maps_to_its_own_superglobal():
    conversion = convert_vbscript_query(
        'sql = "SELECT * FROM t WHERE a = " & Request.QueryString("a") & '
        '" AND b = " & Request.Form("b")')
    assert conversion.slots[0].source == SOURCE_QUERYSTRING
    assert conversion.slots[1].source == SOURCE_FORM
    assert "$_GET['a']" in conversion.php_pdo
    assert "$_POST['b']" in conversion.php_pdo


def test_a_bare_request_call_is_flagged_as_ambiguous():
    """Classic ASP resolves it against five collections in a fixed order,
    and a cookie beats a form post."""
    conversion = convert_vbscript_query(SAMPLE_ASP_INJECTION)
    ambiguous = [s for s in conversion.slots
                 if s.source == SOURCE_AMBIGUOUS]
    assert ambiguous
    assert "ASP-REQUEST" in {f.code for f in conversion.findings}
    assert REQUEST_SEARCH_ORDER[0] == "QueryString"
    assert "Cookies" in REQUEST_SEARCH_ORDER


def test_the_quote_doubling_idiom_is_called_out_as_no_defence():
    """Replace on single quotes does nothing at all in a numeric context,
    where the value is never quoted in the first place."""
    quote = chr(39)
    snippet = (
        'sql = "SELECT * FROM t WHERE n = ' + quote + '" & '
        'Replace(Request("n"), "' + quote + '", "' + quote * 2 + '") & '
        '"' + quote + '"')
    conversion = convert_vbscript_query(snippet)
    assert "ASP-ESCAPE" in {f.code for f in conversion.findings}


def test_mixed_case_identifiers_are_flagged_for_the_linux_move():
    """The statement is parameterised and will still fail on the first run
    against a case sensitive table lookup."""
    mixed = convert_vbscript_query(SAMPLE_ASP_INJECTION)
    assert "ASP-TABLECASE" in {f.code for f in mixed.findings}
    lower = convert_vbscript_query(
        'sql = "SELECT * FROM questions WHERE question_id = " & '
        'Request.QueryString("id")')
    assert "ASP-TABLECASE" not in {f.code for f in lower.findings}


def test_a_snippet_with_no_concatenation_says_so():
    conversion = convert_vbscript_query(
        'sql = "SELECT * FROM questions"')
    assert conversion.status == NOTHING_TO_DO
    assert conversion.slots == ()
    assert "ASP-NONE" in {f.code for f in conversion.findings}


def test_the_generated_php_has_no_duplicated_separators():
    conversion = convert_vbscript_query(SAMPLE_ASP_INJECTION)
    assert ",," not in conversion.php_pdo
    assert conversion.php_pdo.count("$stmt->prepare") == 0
    assert conversion.php_pdo.count("$pdo->prepare") == 1


def test_the_python_output_uses_named_pyformat_parameters():
    conversion = convert_vbscript_query(SAMPLE_ASP_INJECTION)
    assert "%(p1)s" in conversion.python_psycopg
    assert ":p1" not in conversion.python_psycopg


def test_every_placeholder_is_unique_and_sequential():
    conversion = convert_vbscript_query(
        'sql = "SELECT * FROM t WHERE a = " & Request.QueryString("a") & '
        '" AND b = " & Request.QueryString("b") & '
        '" AND c = " & Request.QueryString("c")')
    placeholders = re.findall(r":p\d+", conversion.sql_template)
    assert placeholders == [":p1", ":p2", ":p3"]
    assert len(set(placeholders)) == 3


# ---------------------------------------------------------------------------
# Schema mapping
# ---------------------------------------------------------------------------

def test_every_legacy_table_maps_to_a_lower_snake_case_name():
    for table in LEGACY_TABLES:
        schema = map_database_schema(table)
        assert schema.new_table_name == schema.new_table_name.lower()
        assert " " not in schema.new_table_name
        assert not schema.new_table_name.startswith("tbl")


def test_every_column_is_renamed_to_lower_snake_case():
    for table in LEGACY_TABLES:
        for column in map_database_schema(table).columns:
            assert column.new_name == column.new_name.lower(), column
            assert re.fullmatch(r"[a-z][a-z0-9_]*", column.new_name), column


def test_the_ddl_never_uses_the_three_byte_utf8_character_set():
    """MySQL utf8 cannot hold anything outside the basic multilingual
    plane, so a pasted emoji is silently truncated."""
    for table in LEGACY_TABLES:
        schema = map_database_schema(table)
        assert "utf8mb4" in schema.mysql_ddl, table
        assert not re.search(r"utf8(?!mb4)", schema.mysql_ddl), table
        assert "SCH-UTF8MB4" in {f.code for f in schema.findings}, table


def test_every_mapping_warns_about_case_sensitivity_twice_over():
    """Once for text comparison and once for the table name lookup, which
    are different problems with the same cause."""
    for table in LEGACY_TABLES:
        codes = {f.code for f in map_database_schema(table).findings}
        assert "SCH-CASE" in codes, table
        assert "SCH-TABLECASE" in codes, table


def test_a_yes_no_column_never_becomes_nullable():
    """A nullable boolean has a third state the application has no branch
    for."""
    for table in LEGACY_TABLES:
        for column in map_database_schema(table).columns:
            if column.legacy_type == "Yes/No":
                assert not column.nullable, column.new_name
                assert "NOT NULL" in column.mysql_type
                assert "NOT NULL" in column.postgres_type


def test_the_user_table_keeps_case_insensitive_lookups_working():
    schema = map_database_schema("tblUsers")
    lookups = {c.new_name: c for c in schema.columns}
    assert "CITEXT" in lookups["username"].postgres_type
    assert "CITEXT" in lookups["email"].postgres_type


def test_a_percentage_score_is_decimal_and_never_a_float():
    schema = map_database_schema("tblTestAttempts")
    score = next(c for c in schema.columns if c.new_name == "score")
    assert "DECIMAL" in score.mysql_type
    assert "NUMERIC" in score.postgres_type


def test_both_ddl_statements_carry_every_column_and_a_primary_key():
    for table in LEGACY_TABLES:
        schema = map_database_schema(table)
        for column in schema.columns:
            assert column.new_name in schema.mysql_ddl, column.new_name
            assert column.new_name in schema.postgres_ddl, column.new_name
        assert "PRIMARY KEY" in schema.mysql_ddl
        assert "PRIMARY KEY" in schema.postgres_ddl


def test_every_column_carries_a_migration_note():
    for table in LEGACY_TABLES:
        for column in map_database_schema(table).columns:
            assert column.note.strip(), column.new_name


def test_an_unknown_legacy_table_raises():
    with pytest.raises(ValueError):
        map_database_schema("tblOrders")


# ---------------------------------------------------------------------------
# 301 redirect mapping
# ---------------------------------------------------------------------------

def test_a_plain_asp_page_gets_an_exact_match_location():
    mapping = generate_seo_301_mapping("/login.asp")
    assert mapping.strategy == REDIRECT_EXACT
    assert not mapping.uses_query_string
    assert "location = /login.asp" in mapping.nginx_rule
    assert "return 301 /sign-in/" in mapping.nginx_rule


def test_a_query_string_url_never_gets_a_plain_location_rule():
    """A location block is matched against the path alone, so the obvious
    rule matches nothing and the server starts without complaint."""
    mapping = generate_seo_301_mapping("/quiz.asp?id=42")
    assert mapping.strategy == REDIRECT_QUERY
    assert mapping.uses_query_string
    assert "map $request_uri" in mapping.nginx_rule
    assert "location = /quiz.asp {" not in mapping.nginx_rule
    assert "SEO-LOCATION" in {f.code for f in mapping.findings}


def test_the_map_key_carries_the_full_request_uri_including_arguments():
    mapping = generate_seo_301_mapping("/quiz.asp?id=42")
    assert '"/quiz.asp?id=42"' in mapping.nginx_rule
    assert '"/practice/42/"' in mapping.nginx_rule


def test_the_identifier_is_lifted_out_of_the_query_string_into_the_path():
    cases = {
        "/quiz.asp?id=42": "/practice/42/",
        "/category.asp?catid=7&sort=newest": "/categories/7/",
        "/practice/results.asp?testid=1184": "/practice/results/1184/",
    }
    for url, expected in cases.items():
        assert generate_seo_301_mapping(url).new_path == expected, url


def test_a_default_page_maps_to_the_directory_root():
    assert generate_seo_301_mapping("/Default.asp").new_path == "/"
    assert generate_seo_301_mapping("/index.asp").new_path == "/"


def test_every_mapping_supplies_a_case_insensitive_fallback():
    """IIS served the same page at every capitalisation and the links in
    the wild use all of them."""
    for url in SAMPLE_URLS:
        mapping = generate_seo_301_mapping(url)
        assert "~*" in mapping.case_insensitive_rule, url
        assert "SEO-CASE" in {f.code for f in mapping.findings}, url


def test_every_rule_is_a_301_and_never_a_302():
    for url in SAMPLE_URLS:
        mapping = generate_seo_301_mapping(url)
        assert "return 301" in mapping.nginx_rule, url
        assert "302" not in mapping.nginx_rule, url
        assert "return 301" in mapping.case_insensitive_rule, url


def test_every_new_path_is_clean_lower_case_and_slash_terminated():
    for url in SAMPLE_URLS:
        path = generate_seo_301_mapping(url).new_path
        assert path.startswith("/"), url
        assert path.endswith("/"), url
        assert ".asp" not in path, url
        assert "?" not in path, url
        assert "//" not in path, url


def test_a_multi_argument_url_warns_that_the_ordering_is_not_fixed():
    mapping = generate_seo_301_mapping("/category.asp?catid=7&sort=newest")
    assert len(mapping.query_pairs) == 2
    assert "SEO-PERMUTE" in {f.code for f in mapping.findings}
    single = generate_seo_301_mapping("/quiz.asp?id=42")
    assert "SEO-PERMUTE" not in {f.code for f in single.findings}


def test_every_mapping_says_to_build_the_list_from_logs():
    for url in SAMPLE_URLS:
        codes = {f.code for f in generate_seo_301_mapping(url).findings}
        assert "SEO-INVENTORY" in codes, url
        assert "SEO-CHAIN" in codes, url


def test_a_non_asp_or_empty_url_is_refused():
    for url in ("", "/about.php", "/", "https://example.com/page.html"):
        with pytest.raises(ValueError):
            generate_seo_301_mapping(url)


# ---------------------------------------------------------------------------
# House invariants
# ---------------------------------------------------------------------------

def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    """The engine has to run outside the app, or it is not an engine."""
    script = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.classic_asp_linux_migration_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT))
    done = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    for name in ("core.py", "page.py"):
        text = (TOOL_DIR / name).read_text(encoding="utf-8")
        assert "—" not in text, name
        assert "–" not in text, name


def test_the_tool_is_registered_with_a_unique_icon():
    tools = [t for t in all_tools() if not t.key.startswith("synthetic-")]
    keys = [t.key for t in tools]
    assert "classic-asp-linux-migration-console" in keys
    icons = [t.icon for t in tools]
    assert len(icons) == len(set(icons))
    assert SEVERITY_OK != SEVERITY_CRITICAL
