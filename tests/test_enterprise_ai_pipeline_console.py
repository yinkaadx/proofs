"""Engine tests for the Enterprise AI Pipeline Console.

Written for pytest. Deterministic throughout: nothing in the engine reads the
clock or a random source, so the same document scores the same confidence on a
test runner and in the console.

Run: pytest tests/test_enterprise_ai_pipeline_console.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.enterprise_ai_pipeline_console.core import (  # noqa: E402
    DEFAULT_ROWS,
    ENGINE_VERSION,
    EXTRACTION_FIELDS,
    FIELD_CURRENCY,
    FIELD_DATE,
    FIELD_INVOICE,
    FIELD_TOTAL,
    FIELD_VENDOR,
    INDEX_DDL,
    INDEX_SCAN,
    MAX_WEIGHT,
    QUEUE_STATES,
    REVIEW_THRESHOLD,
    SAMPLE_DOCUMENT,
    SEQ_SCAN,
    STATE_COMPLETED,
    STATE_PENDING,
    STATE_PROCESSING,
    benchmark_comparison,
    benchmark_query,
    simulate_document_extraction,
    simulate_queue_dispatch,
)


def field_named(extraction, name):
    return next(f for f in extraction.fields if f.name == name)


# ---------------------------------------------------------------------------
# Document extraction
# ---------------------------------------------------------------------------

def test_the_sample_document_extracts_every_field():
    result = simulate_document_extraction()
    assert len(result.found) == len(EXTRACTION_FIELDS)
    assert result.missing == []
    assert result.confidence == 1.0


def test_a_complete_document_does_not_need_review():
    result = simulate_document_extraction()
    assert not result.needs_review
    assert result.verdict == "Safe to process without review"


def test_the_named_fields_carry_the_values_that_are_in_the_document():
    result = simulate_document_extraction()
    assert field_named(result, FIELD_INVOICE).value == "INV-2026-0041"
    assert field_named(result, FIELD_DATE).value == "2026-09-15"
    assert field_named(result, FIELD_CURRENCY).value == "GBP"
    assert field_named(result, FIELD_TOTAL).value == "1326.50"


def test_extraction_is_deterministic():
    """The same document must score the same twice, or a demonstration is a
    different demonstration each time somebody looks at it."""
    first = simulate_document_extraction(SAMPLE_DOCUMENT)
    second = simulate_document_extraction(SAMPLE_DOCUMENT)
    assert first.schema() == second.schema()


def test_losing_the_total_costs_more_confidence_than_losing_the_currency():
    """Confidence is weighted, not a count. The total is worth more."""
    no_total = simulate_document_extraction(
        SAMPLE_DOCUMENT.replace("Total due 1326.50", ""))
    no_currency = simulate_document_extraction(
        SAMPLE_DOCUMENT.replace("Currency GBP", ""))
    assert no_total.confidence < no_currency.confidence


def test_a_document_missing_the_total_is_routed_to_a_human():
    result = simulate_document_extraction(
        SAMPLE_DOCUMENT.replace("Total due 1326.50", ""))
    assert not field_named(result, FIELD_TOTAL).found
    assert result.needs_review
    assert result.verdict == "Route to a human reviewer"


def test_an_empty_document_finds_nothing_and_scores_zero():
    result = simulate_document_extraction("")
    assert result.found == []
    assert result.confidence == 0.0
    assert result.needs_review
    assert result.characters == 0


@pytest.mark.parametrize("text", ["", "   ", "nothing useful here at all"])
def test_a_useless_document_never_crashes(text):
    result = simulate_document_extraction(text)
    assert result.schema()["needs_review"] is True


def test_a_found_field_is_more_confident_than_an_inferred_one():
    result = simulate_document_extraction(
        SAMPLE_DOCUMENT.replace("Vendor: Northwind Freight", ""))
    assert field_named(result, FIELD_INVOICE).confidence > \
        field_named(result, FIELD_VENDOR).confidence


def test_an_inferred_field_says_so_rather_than_being_averaged_in():
    result = simulate_document_extraction("Currency GBP")
    inferred = field_named(result, FIELD_TOTAL)
    assert not inferred.found
    assert inferred.source == "inferred, nothing matched"
    assert inferred.value == ""


def test_the_schema_is_valid_json_with_only_found_fields_required():
    result = simulate_document_extraction(
        SAMPLE_DOCUMENT.replace("Currency GBP", ""))
    schema = json.loads(result.pretty_schema())
    assert schema["type"] == "object"
    assert FIELD_CURRENCY not in schema["required"]
    assert FIELD_INVOICE in schema["required"]
    assert set(schema["properties"]) == {f.name for f in result.fields}


def test_every_confidence_is_a_proportion():
    result = simulate_document_extraction(SAMPLE_DOCUMENT)
    assert 0.0 <= result.confidence <= 1.0
    for entry in result.fields:
        assert 0.0 <= entry.confidence <= 1.0


def test_the_review_threshold_is_the_only_thing_that_decides_review():
    result = simulate_document_extraction()
    assert result.needs_review == (result.confidence < REVIEW_THRESHOLD)


def test_the_field_weights_add_up_to_the_maximum():
    assert MAX_WEIGHT == sum(weight for _, _, _, weight in EXTRACTION_FIELDS)
    assert MAX_WEIGHT > 0


def test_the_extraction_rows_are_arrow_safe():
    """Arrow refuses a column that mixes types, so every cell is a string."""
    for row in simulate_document_extraction().rows():
        assert all(isinstance(value, str) for value in row.values()), row


# ---------------------------------------------------------------------------
# The background queue
# ---------------------------------------------------------------------------

def test_a_task_passes_through_all_three_states_in_order():
    dispatch = simulate_queue_dispatch("extract_invoice")
    assert dispatch.states == [STATE_PENDING, STATE_PROCESSING, STATE_COMPLETED]
    assert list(QUEUE_STATES) == dispatch.states


def test_a_dispatch_finishes():
    assert simulate_queue_dispatch("extract_invoice").completed


def test_latency_is_the_sum_of_every_stage():
    dispatch = simulate_queue_dispatch("reindex_documents")
    assert dispatch.latency_ms == sum(s.duration_ms for s in dispatch.stages)
    assert dispatch.latency_ms == dispatch.queue_wait_ms + dispatch.work_ms + \
        dispatch.stages[-1].duration_ms


def test_the_stages_are_contiguous_with_no_unaccounted_gap():
    dispatch = simulate_queue_dispatch("extract_invoice")
    for earlier, later in zip(dispatch.stages, dispatch.stages[1:]):
        assert later.entered_ms == earlier.left_ms


def test_waiting_and_working_are_reported_apart():
    """The number to reduce is the waiting one, so it cannot be hidden inside
    a single total."""
    dispatch = simulate_queue_dispatch("extract_invoice")
    assert dispatch.queue_wait_ms > 0
    assert dispatch.work_ms > 0
    assert dispatch.queue_wait_ms != dispatch.latency_ms


def test_dispatch_is_deterministic_for_the_same_task_name():
    first = simulate_queue_dispatch("extract_invoice")
    second = simulate_queue_dispatch("extract_invoice")
    assert first.task_id == second.task_id
    assert first.latency_ms == second.latency_ms


def test_a_different_task_gets_a_different_job():
    assert simulate_queue_dispatch("extract_invoice").task_id != \
        simulate_queue_dispatch("rebuild_embeddings").task_id


@pytest.mark.parametrize("name", ["", "   ", "a", "a_very_long_task_name_x9"])
def test_any_task_name_produces_a_complete_dispatch(name):
    dispatch = simulate_queue_dispatch(name)
    assert dispatch.completed
    assert dispatch.task_id.startswith("job_")
    assert dispatch.task_name


def test_the_queue_rows_are_arrow_safe():
    for row in simulate_queue_dispatch("extract_invoice").rows():
        assert all(isinstance(value, str) for value in row.values()), row


# ---------------------------------------------------------------------------
# The query optimizer
# ---------------------------------------------------------------------------

def test_the_index_makes_the_query_faster():
    assert benchmark_query(True).execution_ms < benchmark_query(False).execution_ms


def test_without_an_index_every_row_is_scanned():
    result = benchmark_query(indexed=False, rows=DEFAULT_ROWS)
    assert result.rows_scanned == DEFAULT_ROWS
    assert result.plan == SEQ_SCAN


def test_with_an_index_only_the_matching_rows_are_touched():
    result = benchmark_query(indexed=True, rows=DEFAULT_ROWS)
    assert result.rows_scanned == result.rows_returned
    assert result.rows_scanned < DEFAULT_ROWS
    assert result.plan == INDEX_SCAN


def test_the_sequential_scan_cost_tracks_the_table_and_the_index_tracks_the_answer():
    """The whole point, stated so that it is actually testable.

    A first attempt compared the two across table sizes at a fixed selectivity
    fraction, which is not the claim: at a fixed fraction the answer grows with
    the table too, so the index cost grows with it and the comparison proves
    nothing. Holding the number of returned rows constant is what isolates the
    property, and it is the honest demonstration as well.
    """
    returned = 200
    small, large = 100_000, 10_000_000

    small_seq = benchmark_query(False, small, returned / small).execution_ms
    large_seq = benchmark_query(False, large, returned / large).execution_ms
    small_idx = benchmark_query(True, small, returned / small).execution_ms
    large_idx = benchmark_query(True, large, returned / large).execution_ms

    # A hundredfold bigger table costs a hundred times as much to scan.
    assert large_seq / small_seq > 50
    # The same answer from a hundredfold bigger table costs barely more, since
    # only the depth of the B tree changed.
    assert large_idx / small_idx < 1.5


@pytest.mark.parametrize("rows", [10_000, 250_000, 2_000_000, 10_000_000])
def test_the_index_wins_at_every_table_size_worth_indexing(rows):
    assert benchmark_query(True, rows).execution_ms < \
        benchmark_query(False, rows).execution_ms


def test_on_a_tiny_table_the_index_does_not_win():
    """Not a defect, and worth showing rather than hiding.

    PostgreSQL picks a sequential scan on a small table for exactly this
    reason: descending a B tree costs more than reading the whole thing. An
    engine that claimed the index always wins would be teaching the mistake
    that puts an index on every column.
    """
    assert benchmark_query(True, rows=1).execution_ms > \
        benchmark_query(False, rows=1).execution_ms


def test_a_sequential_scan_is_never_free():
    """One page still has to be read, so zero milliseconds is not a number a
    database ever produces."""
    assert benchmark_query(False, rows=1).execution_ms > 0


def test_benchmarks_are_deterministic():
    assert benchmark_query(True) == benchmark_query(True)
    assert benchmark_query(False) == benchmark_query(False)


def test_total_time_includes_planning():
    result = benchmark_query(True)
    assert result.total_ms == round(result.execution_ms + result.planning_ms, 3)


def test_at_least_one_row_is_returned_even_at_a_tiny_selectivity():
    assert benchmark_query(True, rows=100, selectivity=0.0).rows_returned == 1


def test_the_comparison_states_the_speedup_rather_than_implying_it():
    rows = {r["Measure"]: r for r in benchmark_comparison()}
    assert rows["Plan"]["No index"] == SEQ_SCAN
    assert rows["Plan"]["With index"] == INDEX_SCAN
    assert rows["Speedup"]["No index"] == "1x"
    assert rows["Speedup"]["With index"].endswith("x")


def test_the_comparison_and_the_plan_table_are_arrow_safe():
    for row in benchmark_comparison() + benchmark_query(True).rows_table():
        assert all(isinstance(value, str) for value in row.values()), row


def test_explain_analyze_names_the_plan_and_the_filter():
    explained = benchmark_query(False).explain()
    assert SEQ_SCAN in explained
    assert "Rows Removed by Filter" in explained
    assert "Execution Time" in explained
    assert "using" in benchmark_query(True).explain()


def test_the_index_is_built_without_locking_a_live_table():
    assert "CONCURRENTLY" in INDEX_DDL
    assert "WHERE status = 'pending'" in INDEX_DDL


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------

def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (Path(__file__).resolve().parents[1] / "tools"
              / "enterprise_ai_pipeline_console" / "core.py").read_text()
    assert "streamlit" not in source


def test_no_dash_characters_reach_the_screen():
    text = " ".join([
        simulate_document_extraction().verdict,
        " ".join(s.note for s in simulate_queue_dispatch("x").stages),
        benchmark_query(True).explain(), INDEX_DDL,
    ])
    assert "—" not in text
    assert "–" not in text
