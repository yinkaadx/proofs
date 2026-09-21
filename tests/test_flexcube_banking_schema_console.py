"""Tests for the Core Banking Schema and Reporting Console.

The schema tests are about roles rather than column lists, because the
role is what stops a report being written against a cache. The
reconciliation tests are about the gap between a ledger that ties and a
ledger that is right, and they check that every amount is a Decimal, since
a float that has drifted looks exactly like a real break.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from decimal import Decimal

import pytest

from tools.flexcube_banking_schema_console.core import (
    BASEL_MINIMA,
    BLOCKED,
    ENGINE_VERSION,
    EXCEPTION_KINDS,
    KIND_UNMAPPED,
    MODULE_CASA,
    MODULE_GL,
    MODULE_TERM_DEPOSIT,
    MODULES,
    NET_BLIND_SPOT_RATIO,
    READY,
    REPORT_CAPITAL,
    REPORT_EXPOSURE,
    REPORT_TYPES,
    ROLE_DERIVED,
    ROLE_MOVEMENT,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    STANDARD_BASEL,
    STANDARD_LOCAL,
    STANDARDS,
    ZERO,
    extract_regulatory_audit,
    map_core_banking_schema,
    money,
    simulate_gl_reconciliation,
)
from tools.registry import all_tools

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL_DIR = ROOT / "tools" / "flexcube_banking_schema_console"


# ---------------------------------------------------------------------------
# Schema mapping
# ---------------------------------------------------------------------------

def test_every_module_maps_to_a_non_empty_schema():
    for module in MODULES:
        schema = map_core_banking_schema(module)
        assert schema.module == module
        assert schema.tables
        for table in schema.tables:
            assert table.columns, table.name
            assert table.primary_key, table.name


def test_every_module_names_its_caches_as_caches():
    """A report written against a cache returns a fast answer that says
    nothing about whether it is the right one."""
    for module in MODULES:
        schema = map_core_banking_schema(module)
        assert schema.cache_tables, module
        for table in schema.cache_tables:
            assert table.role == ROLE_DERIVED
            assert not table.is_record_of_truth
        assert "SCH-CACHE" in {f.code for f in schema.findings}, module


def test_every_module_has_an_append_only_movement_log():
    for module in MODULES:
        schema = map_core_banking_schema(module)
        assert schema.movement_tables, module
        for table in schema.movement_tables:
            assert table.role == ROLE_MOVEMENT
            assert table.is_record_of_truth


def test_the_general_ledger_carries_both_a_journal_and_a_suspense_log():
    """Suspense is a balanced line, which is how a ledger ties while being
    wrong."""
    schema = map_core_banking_schema(MODULE_GL)
    names = {t.name for t in schema.movement_tables}
    assert "CB_GL_POSTING" in names
    assert "CB_GL_SUSPENSE" in names


def test_no_monetary_column_is_a_binary_float():
    """A binary float cannot represent a tenth exactly, and a day of
    postings summed in one drifts."""
    for module in MODULES:
        for table in map_core_banking_schema(module).tables:
            for column in table.columns:
                assert "FLOAT" not in column.sql_type.upper(), column.name
                assert "DOUBLE" not in column.sql_type.upper(), column.name


def test_the_casa_movement_log_separates_value_date_from_booking_date():
    """They give different answers at a period boundary and only one
    matches the return."""
    schema = map_core_banking_schema(MODULE_CASA)
    movement = schema.movement_tables[0]
    names = {c.name for c in movement.columns}
    assert "VALUE_DATE" in names
    assert "BOOKING_DATE" in names


def test_the_term_deposit_module_accrues_before_it_pays():
    schema = map_core_banking_schema(MODULE_TERM_DEPOSIT)
    names = {t.name for t in schema.tables}
    assert "CB_TD_ACCRUAL" in names


def test_the_map_always_warns_that_the_names_are_a_pattern():
    """A migration written against names from a document fails on its
    first run against the real installation."""
    for module in MODULES:
        codes = {f.code for f in map_core_banking_schema(module).findings}
        assert "SCH-NAMES" in codes, module


def test_an_unknown_module_raises():
    with pytest.raises(ValueError):
        map_core_banking_schema("Trade Finance")


# ---------------------------------------------------------------------------
# GL reconciliation
# ---------------------------------------------------------------------------

def test_matched_plus_exceptions_equals_the_volume():
    for volume, rate in ((0, 0.0), (1, 0.5), (1200, 0.02), (50000, 0.05),
                         (999, 1.0)):
        recon = simulate_gl_reconciliation(volume, rate)
        assert recon.matched + recon.exceptions == volume, (volume, rate)
        assert recon.matched >= 0 and recon.exceptions >= 0


def test_the_ledger_always_ties_whatever_the_exceptions_are():
    """Every posting is a balanced pair, including the ones in suspense.
    Tying proves the journal is well formed and nothing more."""
    for rate in (0.0, 0.01, 0.10, 0.20, 1.0):
        recon = simulate_gl_reconciliation(5000, rate)
        assert recon.total_debits == recon.total_credits, rate
        assert recon.balanced, rate
        assert "REC-TIE" in {f.code for f in recon.findings}, rate


def test_every_monetary_result_is_a_decimal_not_a_float():
    recon = simulate_gl_reconciliation(20000, 0.05)
    for value in (recon.total_debits, recon.total_credits,
                  recon.net_variance, recon.gross_variance,
                  recon.suspense_balance):
        assert isinstance(value, Decimal), value
    for bucket in recon.buckets:
        assert isinstance(bucket.net_amount, Decimal)
        assert isinstance(bucket.gross_amount, Decimal)


def test_gross_variance_never_falls_below_the_size_of_the_net():
    """Errors cancel in the net and do not cancel in the gross, which is
    the whole reason both are reported."""
    for rate in (0.0, 0.005, 0.02, 0.08, 0.2):
        recon = simulate_gl_reconciliation(30000, rate)
        assert recon.gross_variance >= abs(recon.net_variance), rate


def test_the_bucket_totals_equal_the_reported_variances():
    recon = simulate_gl_reconciliation(40000, 0.06)
    assert sum((b.count for b in recon.buckets), 0) == recon.exceptions
    assert sum((b.net_amount for b in recon.buckets), ZERO) == \
        recon.net_variance
    assert sum((b.gross_amount for b in recon.buckets), ZERO) == \
        recon.gross_variance


def test_netting_hiding_errors_is_raised_as_critical_when_it_happens():
    """The reading that gets signed off is the one that should not be."""
    recon = simulate_gl_reconciliation(50000, 0.05)
    assert recon.netting_hides_errors
    assert abs(recon.net_variance) < recon.gross_variance * \
        NET_BLIND_SPOT_RATIO
    assert "REC-NETTING" in {f.code for f in recon.findings}
    assert recon.severity == SEVERITY_CRITICAL


def test_a_clean_day_reports_no_variance_and_no_suspense():
    recon = simulate_gl_reconciliation(8000, 0.0)
    assert recon.exceptions == 0
    assert recon.buckets == ()
    assert recon.net_variance == ZERO
    assert recon.gross_variance == ZERO
    assert recon.suspense_count == 0
    assert not recon.netting_hides_errors


def test_unmapped_postings_and_only_those_reach_suspense():
    recon = simulate_gl_reconciliation(30000, 0.06)
    unmapped = [b for b in recon.buckets if b.kind == KIND_UNMAPPED]
    assert unmapped
    assert recon.suspense_count == unmapped[0].count
    assert "REC-SUSPENSE" in {f.code for f in recon.findings}


def test_every_exception_kind_is_reachable():
    recon = simulate_gl_reconciliation(60000, 0.08)
    assert {b.kind for b in recon.buckets} == set(EXCEPTION_KINDS)


def test_the_reconciliation_is_deterministic_for_the_same_inputs():
    """A variance that moves between two runs of the same day cannot be
    investigated."""
    first = simulate_gl_reconciliation(12000, 0.03)
    second = simulate_gl_reconciliation(12000, 0.03)
    assert first.net_variance == second.net_variance
    assert first.gross_variance == second.gross_variance
    assert first.exceptions == second.exceptions


def test_a_negative_volume_or_an_out_of_range_rate_raises():
    with pytest.raises(ValueError):
        simulate_gl_reconciliation(-1, 0.05)
    with pytest.raises(ValueError):
        simulate_gl_reconciliation(100, 1.5)
    with pytest.raises(ValueError):
        simulate_gl_reconciliation(100, -0.01)


def test_money_quantises_to_minor_units_exactly():
    assert money("10.005") == Decimal("10.00")
    assert money("10.015") == Decimal("10.02")
    assert money(0.1) + money(0.2) == Decimal("0.30")


# ---------------------------------------------------------------------------
# Regulatory extraction
# ---------------------------------------------------------------------------

def test_a_fully_traced_report_is_ready_to_file():
    extract = extract_regulatory_audit(REPORT_CAPITAL, STANDARD_BASEL)
    assert extract.status == READY
    assert extract.ready
    assert extract.missing_lineage == ()
    assert all(f.has_lineage for f in extract.fields)


def test_a_field_without_lineage_blocks_the_whole_extract():
    """A pack with a gap in it still looks complete to the person signing
    it."""
    extract = extract_regulatory_audit(REPORT_EXPOSURE, STANDARD_BASEL)
    assert extract.status == BLOCKED
    assert not extract.ready
    assert "Eligible collateral" in extract.missing_lineage
    assert "REG-LINEAGE" in {f.code for f in extract.findings}


def test_blocking_is_driven_by_lineage_and_nothing_else():
    for report in REPORT_TYPES:
        for standard in STANDARDS:
            extract = extract_regulatory_audit(report, standard)
            expected = BLOCKED if extract.missing_lineage else READY
            assert extract.status == expected, (report, standard)


def test_the_basel_minima_are_the_published_framework_numbers():
    measures = {row[0]: row[1] for row in BASEL_MINIMA}
    assert measures["Common equity tier 1"] == "4.5% of risk weighted assets"
    assert measures["Tier 1 capital"] == "6.0% of risk weighted assets"
    assert measures["Total capital"] == "8.0% of risk weighted assets"
    assert measures["Liquidity coverage ratio"] == "100%"
    assert measures["Net stable funding ratio"] == "100%"
    assert measures["Leverage ratio"] == "3.0%"


def test_basel_thresholds_are_labelled_as_minima_not_as_the_requirement():
    """National authorities implement on top, and their number binds."""
    extract = extract_regulatory_audit(REPORT_CAPITAL, STANDARD_BASEL)
    assert extract.thresholds == BASEL_MINIMA
    assert "REG-BASEL" in {f.code for f in extract.findings}


def test_no_local_thresholds_are_invented():
    """Shipping a default would be guessing at the number a bank is
    measured against."""
    extract = extract_regulatory_audit(REPORT_CAPITAL, STANDARD_LOCAL)
    assert extract.thresholds == ()
    assert "REG-LOCAL" in {f.code for f in extract.findings}
    assert any(f.severity == SEVERITY_CRITICAL for f in extract.findings)


def test_every_extract_raises_the_as_at_date_ambiguity():
    for report in REPORT_TYPES:
        codes = {f.code for f in
                 extract_regulatory_audit(report, STANDARD_BASEL).findings}
        assert "REG-ASOF" in codes, report


def test_every_traced_field_points_at_a_table_this_engine_maps():
    """Lineage that names a table nobody can find is not lineage."""
    known = {t.name for module in MODULES
             for t in map_core_banking_schema(module).tables}
    for report in REPORT_TYPES:
        extract = extract_regulatory_audit(report, STANDARD_BASEL)
        for one in extract.fields:
            if one.has_lineage:
                assert one.source_table in known, (report, one.name)
                table = next(t for module in MODULES
                             for t in map_core_banking_schema(module).tables
                             if t.name == one.source_table)
                columns = {c.name for c in table.columns}
                assert one.source_column in columns, (report, one.name)


def test_an_unknown_report_or_standard_raises():
    with pytest.raises(ValueError):
        extract_regulatory_audit("Interest rate risk", STANDARD_BASEL)
    with pytest.raises(ValueError):
        extract_regulatory_audit(REPORT_CAPITAL, "Basel II")


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
        "import tools.flexcube_banking_schema_console.core as c; "
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
    assert "flexcube-banking-schema-console" in keys
    icons = [t.icon for t in tools]
    assert len(icons) == len(set(icons))
    assert SEVERITY_OK != SEVERITY_CRITICAL
