"""Tests for the IPB V5 Upgrade Diagnostic Console.

Two things are worth proving. The scanner must never report a clean bill of
health for something it never read, because that is the failure that reaches
the maintenance window rather than the log. And every theme declaration the
mapper emits must carry a fallback, because the token names belong to the
theme installed on the site and a var() with a wrong name and no fallback
renders nothing at all.

Pass marker: pytest reports all tests passed, exit 0.
Fail marker: any failure or error line, exit non zero.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.ipb_v5_upgrade_diagnostic.core import (  # noqa: E402
    ENGINE_VERSION,
    HOOK_BY_NAME,
    HOOK_LIBRARY,
    KIND_COLOR,
    KIND_RADIUS,
    KIND_SPACING,
    LEGACY_LIBRARY,
    PHP_FLOOR_LABEL,
    PHP_FLOOR_V5,
    RECOVERY_BLOCKED,
    RECOVERY_READY,
    SAMPLE_PLUGINS,
    SAMPLE_TABLES,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    TOKEN_PREFIX,
    VERDICT_CLEAN,
    VERDICT_PORT,
    VERDICT_REWRITE,
    map_theme_variables,
    parse_php_version,
    scan_plugin_compatibility,
    simulate_database_recovery,
)

PHP_OK = "8.2"


# ---------------------------------------------------------------------------
# Plugin compatibility
# ---------------------------------------------------------------------------


def test_a_plugin_that_was_never_read_is_never_reported_clean():
    """The failure that reaches the window rather than the log."""
    scan = scan_plugin_compatibility("Some Plugin Nobody Indexed", PHP_OK)
    assert scan.verdict == "NOT SCANNED"
    assert scan.verdict != VERDICT_CLEAN
    assert any(f.code == "IPB5-UNKNOWN" and f.severity == SEVERITY_WARN
               for f in scan.findings)
    assert "not read" in scan.headline


def test_a_plugin_with_no_flagged_construct_is_clean_and_says_why():
    scan = scan_plugin_compatibility("Simple Copyright Line", PHP_OK)
    assert scan.verdict == VERDICT_CLEAN
    assert scan.deprecated_hooks == ()
    assert scan.rewrite_count == 0 and scan.port_count == 0
    assert any(f.code == "IPB5-NO-HOOKS" for f in scan.findings)
    assert "application" in scan.headline


def test_a_code_hook_or_theme_hook_forces_a_rewrite():
    for name in ("Video Embed Plus", "Member Badge Ribbons",
                 "Topic Auto Tagger", "Sidebar Advert Rotator"):
        scan = scan_plugin_compatibility(name, PHP_OK)
        assert scan.verdict == VERDICT_REWRITE, name
        assert scan.rewrite_count >= 1, name
        assert scan.severity == SEVERITY_CRITICAL, name


def test_the_counts_always_add_up_to_what_was_found():
    for name in SAMPLE_PLUGINS:
        scan = scan_plugin_compatibility(name, PHP_OK)
        assert scan.rewrite_count + scan.port_count == scan.total_constructs, name
        assert scan.total_constructs == len(SAMPLE_PLUGINS[name]), name
        assert len(scan.extension_points) == scan.total_constructs, name


def test_every_flagged_construct_names_where_it_goes():
    for name in SAMPLE_PLUGINS:
        scan = scan_plugin_compatibility(name, PHP_OK)
        for hook in scan.deprecated_hooks:
            assert hook.extension_point.strip(), hook.legacy_hook
            assert hook.effort in ("Rewrite", "Port"), hook.legacy_hook
            assert len(hook.detail) > 40, hook.legacy_hook


def test_the_library_is_internally_consistent():
    names = [hook.legacy_hook for hook in HOOK_LIBRARY]
    assert len(names) == len(set(names)), "a construct is listed twice"
    assert set(HOOK_BY_NAME) == set(names)
    for plugin, hooks in SAMPLE_PLUGINS.items():
        for hook in hooks:
            assert hook in HOOK_BY_NAME, (plugin, hook)


def test_the_php_floor_blocks_below_and_clears_at_and_above():
    below = scan_plugin_compatibility("Simple Copyright Line", "7.4")
    assert not below.php_meets_floor
    assert below.severity == SEVERITY_CRITICAL
    assert any(f.code == "IPB5-PHP" for f in below.findings)
    at_floor = scan_plugin_compatibility("Simple Copyright Line", PHP_FLOOR_LABEL)
    assert at_floor.php_meets_floor, "the floor itself must clear the floor"
    above = scan_plugin_compatibility("Simple Copyright Line", "8.3")
    assert above.php_meets_floor
    assert any(f.code == "IPB5-PHP-OK" for f in above.findings)


def test_the_php_floor_never_changes_the_construct_findings():
    """A runtime problem and a code problem are different problems."""
    old = scan_plugin_compatibility("Video Embed Plus", "7.4")
    new = scan_plugin_compatibility("Video Embed Plus", "8.3")
    assert old.deprecated_hooks == new.deprecated_hooks
    assert old.verdict == new.verdict
    assert old.rewrite_count == new.rewrite_count


def test_the_version_parser_takes_what_people_actually_type():
    assert parse_php_version("8.2") == (8, 2)
    assert parse_php_version("8") == (8, 0)
    assert parse_php_version("8.1.27") == (8, 1)
    assert parse_php_version(" 8.3.1-1ubuntu ") == (8, 3)
    assert parse_php_version("8.1") == PHP_FLOOR_V5
    for bad in ("", "php", "  ", "latest"):
        with pytest.raises(ValueError):
            parse_php_version(bad)


def test_a_scan_with_no_plugin_name_raises():
    with pytest.raises(ValueError):
        scan_plugin_compatibility("", PHP_OK)
    with pytest.raises(ValueError):
        scan_plugin_compatibility("   ", PHP_OK)


# ---------------------------------------------------------------------------
# Theme variable mapping
# ---------------------------------------------------------------------------


def test_every_declaration_carries_the_old_value_as_a_fallback():
    """The whole design. A wrong token name must render as it did before,
    not as nothing."""
    for legacy in LEGACY_LIBRARY:
        mapping = map_theme_variables(legacy)
        assert mapping.has_fallback, legacy
        assert f"var({mapping.token}, {mapping.legacy_value})" in mapping.declaration
        assert mapping.declaration.endswith(";")
        assert mapping.legacy_value in mapping.declaration, legacy


def test_an_unknown_declaration_still_gets_a_fallback_and_a_warning():
    mapping = map_theme_variables(".mySiteWidget background")
    assert not mapping.known
    assert mapping.has_fallback
    assert "var(" in mapping.declaration and ", inherit)" in mapping.declaration
    assert any(f.code == "THEME-UNKNOWN" for f in mapping.findings)


def test_no_mapping_ever_emits_a_var_without_a_fallback():
    for legacy in list(LEGACY_LIBRARY) + [".unknownThing padding",
                                          "#custom border-radius",
                                          ".thing font-size"]:
        declaration = map_theme_variables(legacy).declaration
        for call in re.findall(r"var\(([^)]*)\)", declaration):
            assert "," in call, (legacy, declaration)
            assert call.split(",", 1)[1].strip(), (legacy, declaration)


def test_every_token_follows_the_stated_convention():
    for legacy in LEGACY_LIBRARY:
        token = map_theme_variables(legacy).token
        assert token.startswith(f"{TOKEN_PREFIX}-"), legacy
        assert token == token.lower(), legacy
        assert " " not in token, legacy


def test_the_mapper_never_claims_the_token_name_is_authoritative():
    """It cannot read the installed theme, so it must not pretend to."""
    for legacy in list(LEGACY_LIBRARY)[:3] + [".anything color"]:
        mapping = map_theme_variables(legacy)
        assert any(f.code == "THEME-CONVENTION" for f in mapping.findings), legacy
        assert mapping.verification_step.strip()
        assert ":root" in mapping.verification_step


def test_the_kind_is_inferred_from_the_declaration():
    assert map_theme_variables(".ipsBox border-radius").kind == KIND_RADIUS
    assert map_theme_variables(".ipsBox background").kind == KIND_COLOR
    assert map_theme_variables(".ipsAreaBackground padding").kind == KIND_SPACING
    assert map_theme_variables(".custom margin").kind == KIND_SPACING
    assert map_theme_variables(".custom border-radius").kind == KIND_RADIUS


def test_a_colour_always_raises_the_dark_mode_point():
    colours = [k for k, v in LEGACY_LIBRARY.items() if v[0] == KIND_COLOR]
    assert colours, "the library should hold at least one colour"
    for legacy in colours:
        mapping = map_theme_variables(legacy)
        assert any(f.code == "THEME-DARK" for f in mapping.findings), legacy
    radius = map_theme_variables(".ipsBox border-radius")
    assert not any(f.code == "THEME-DARK" for f in radius.findings)


def test_the_property_name_comes_from_the_declaration_itself():
    assert map_theme_variables(".ipsBox background").property_name == "background"
    assert map_theme_variables(".ipsType_normal color").property_name == "color"
    assert map_theme_variables(".ipsBox border-radius").property_name == "border-radius"


def test_an_empty_declaration_raises():
    with pytest.raises(ValueError):
        map_theme_variables("")
    with pytest.raises(ValueError):
        map_theme_variables("   ")


# ---------------------------------------------------------------------------
# Record recovery
# ---------------------------------------------------------------------------


def test_a_valid_pages_table_produces_the_whole_sequence_in_order():
    plan = simulate_database_recovery("ips_cms_custom_database_4")
    assert plan.status == RECOVERY_READY
    assert plan.database_id == "4"
    assert len(plan.queries) == 5
    titles = [title for title, _query in plan.queries]
    assert titles[0].startswith("1.") and "Back up" in titles[0]
    assert "column map" in titles[1]
    assert "Count first" in titles[2]
    assert "field_database_id = 4" in plan.discovery_query


def test_the_discovery_query_comes_before_the_extraction():
    """field_N numbers are site specific, so guessing them is the bug."""
    plan = simulate_database_recovery("ips_cms_custom_database_7")
    order = [title for title, _q in plan.queries]
    discovery = next(i for i, t in enumerate(order) if "column map" in t)
    extract = next(i for i, t in enumerate(order) if "Extract" in t)
    assert discovery < extract
    assert "ips_cms_database_fields" in plan.discovery_query
    assert any(f.code == "DB-FIELD-MAP" and f.severity == SEVERITY_CRITICAL
               for f in plan.findings)


def test_nothing_in_the_plan_updates_or_deletes_the_source_table():
    """The safety property, checked against the SQL rather than claimed."""
    for table in SAMPLE_TABLES:
        plan = simulate_database_recovery(table)
        for _title, query in plan.queries:
            upper = query.upper()
            assert "DROP TABLE" not in upper, table
            assert "DELETE FROM" not in upper, table
            assert "TRUNCATE" not in upper, table
            assert not re.search(r"\bUPDATE\s+\w", upper), (table, query)


def test_the_load_step_writes_only_to_a_new_table():
    plan = simulate_database_recovery("ips_cms_custom_database_4")
    load = plan.load_query
    assert "CREATE TABLE ips_cms_custom_database_4_recovered" in load
    assert "INSERT INTO ips_cms_custom_database_4_recovered" in load
    assert "INSERT INTO ips_cms_custom_database_4 " not in load
    assert "SELECT COUNT(*)" in load, "the move must be provable afterwards"


def test_a_table_name_that_is_not_an_identifier_produces_no_sql_at_all():
    for bad in ("bad name; DROP TABLE users", "", "   ", "ips-cms-4",
                "1_starts_with_a_digit", "table`name", "a" * 70):
        plan = simulate_database_recovery(bad)
        assert plan.status == RECOVERY_BLOCKED, bad
        assert plan.queries == (), bad
        assert plan.extraction_query == "", bad
        assert plan.load_query == "", bad
        assert any(f.code == "DB-IDENT" and f.severity == SEVERITY_CRITICAL
                   for f in plan.findings), bad


def test_a_non_pages_table_is_accepted_but_flagged():
    plan = simulate_database_recovery("ips_core_members")
    assert plan.status == RECOVERY_READY
    assert plan.database_id == ""
    assert any(f.code == "DB-NOT-PAGES" for f in plan.findings)
    assert "<database_id>" in plan.discovery_query


def test_every_ready_plan_backs_up_first():
    for table in SAMPLE_TABLES:
        plan = simulate_database_recovery(table)
        assert plan.backup_command.startswith("mysqldump")
        assert table in plan.backup_command
        assert plan.queries[0][1] == plan.backup_command
        assert any(f.code == "DB-BACKUP" for f in plan.findings)


def test_the_extraction_warns_in_the_sql_itself_not_only_in_a_finding():
    """Whoever pastes this into a shell may never have seen the page."""
    plan = simulate_database_recovery("ips_cms_custom_database_4")
    assert "Do not assume field_1 is the title" in plan.extraction_query
    assert plan.extraction_query.lstrip().startswith("--")


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "ipb_v5_upgrade_diagnostic" / "core.py").read_text()
    assert "import streamlit" not in source
    probe = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.ipb_v5_upgrade_diagnostic.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT)
    )
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [f.title + f.detail + f.fix
         for name in list(SAMPLE_PLUGINS) + ["Unknown Thing"]
         for version in ("7.4", "8.2")
         for f in scan_plugin_compatibility(name, version).findings]
        + [scan_plugin_compatibility(name, "8.2").headline
           for name in SAMPLE_PLUGINS]
        + [hook.detail + hook.kind for hook in HOOK_LIBRARY]
        + [f.title + f.detail + f.fix
           for legacy in list(LEGACY_LIBRARY) + [".custom padding"]
           for f in map_theme_variables(legacy).findings]
        + [f.title + f.detail + f.fix
           for table in list(SAMPLE_TABLES) + ["bad name"]
           for f in simulate_database_recovery(table).findings]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools
    tools = all_tools()
    entry = next(t for t in tools if t.key == "ipb-v5-upgrade-diagnostic")
    assert entry.title == "IPB V5 Upgrade Diagnostic Console"
    icons = [t.icon for t in tools]
    assert icons.count(entry.icon) == 1, "this icon is already used by another tool"
    assert len(entry.tagline) > 30
