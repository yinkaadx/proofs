"""Enterprise AI Pipeline Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine could run in CI or behind a worker.

Everything on this page is evaluated live from the controls. There is no
session state to go stale, which removes the class of bug where a card on
screen describes a run that was replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.enterprise_ai_pipeline_console.core import (
    DEFAULT_ROWS,
    ENGINE_VERSION,
    INDEX_DDL,
    REVIEW_THRESHOLD,
    SAMPLE_DOCUMENT,
    STATE_COMPLETED,
    STATE_PENDING,
    STATE_PROCESSING,
    benchmark_comparison,
    benchmark_query,
    simulate_document_extraction,
    simulate_queue_dispatch,
)

STATE_TONE = {STATE_PENDING: "warn", STATE_PROCESSING: "info",
              STATE_COMPLETED: "ok"}


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>Enterprise AI Pipeline Console</h1>
  <p>A document arrives, a model is asked to turn it into a schema, a worker
  picks the job up in the background, and a dashboard queries the result. Each
  of those four steps has a number that decides whether the pipeline is
  trustworthy: how sure the extraction is, which state the job is stuck in, and
  whether the query still answers once the table is large. All three are on
  screen here rather than assumed.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Extraction**: delete the total line and watch confidence "
            "fall past the review threshold.\n"
            "2. **Queue**: rename the task and watch the three states and the "
            "latency change.\n"
            "3. **Query**: turn the index off and read the execution time."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. No model is called, no queue "
            "is connected and no database is queried."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_doc, tab_queue, tab_query = st.tabs(
        ["Document Extraction", "Background Queue", "Query Optimizer"])

    # -----------------------------------------------------------------
    # Intelligent document processing
    # -----------------------------------------------------------------
    with tab_doc:
        st.markdown("#### The document, and how sure the model is")
        st.caption(
            "Confidence is the weighted share of the schema actually found by "
            "a pattern, not a decoration. A document missing the total scores "
            "far worse than one missing the currency, and a field the model "
            "had to infer is reported as inferred rather than averaged in."
        )

        text = st.text_area("Document text", value=SAMPLE_DOCUMENT, height=200,
                            key="eap_doc")
        result = simulate_document_extraction(text)
        tone = "crit" if result.needs_review else "ok"

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {tone}"><div class="n">{result.confidence:.0%}</div>
    <div class="l">Document confidence</div></div>
  <div class="app-kpi ok"><div class="n">{len(result.found)}</div>
    <div class="l">Fields matched</div></div>
  <div class="app-kpi {'crit' if result.missing else 'ok'}">
    <div class="n">{len(result.missing)}</div>
    <div class="l">Fields not found</div></div>
  <div class="app-kpi"><div class="n">{result.characters}</div>
    <div class="l">Characters read</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(result.verdict)}</span>
  {result.confidence:.0%} against a {REVIEW_THRESHOLD:.0%} threshold</h4>
  <p>A pipeline that routes everything straight through is not automated, it
  is unsupervised. The threshold is where a person is asked to look.</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### The extracted fields")
        st.dataframe(result.rows(), width="stretch", hide_index=True)

        st.markdown("##### The schema the model returned")
        st.code(result.pretty_schema(), language="json")

    # -----------------------------------------------------------------
    # Background queue worker
    # -----------------------------------------------------------------
    with tab_queue:
        st.markdown("#### One task, three states, one latency")
        st.caption(
            "Always pending, then processing, then completed. A worker that "
            "reports completed without ever reporting processing is a worker "
            "nobody can debug, because the state a task is stuck in is the "
            "whole diagnosis when a queue backs up."
        )

        task = st.text_input("Task name", value="extract_invoice",
                             key="eap_task")
        dispatch = simulate_queue_dispatch(task)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{dispatch.latency_ms}</div>
    <div class="l">End to end latency, ms</div></div>
  <div class="app-kpi warn"><div class="n">{dispatch.queue_wait_ms}</div>
    <div class="l">Waiting, ms</div></div>
  <div class="app-kpi"><div class="n">{dispatch.work_ms}</div>
    <div class="l">Working, ms</div></div>
  <div class="app-kpi ok"><div class="n">{len(dispatch.states)}</div>
    <div class="l">States recorded</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for stage in dispatch.stages:
            card = STATE_TONE.get(stage.state, "warn")
            st.markdown(
                f"""
<div class="app-card {card}">
  <h4><span class="app-tag {card}">{esc(stage.state)}</span>
  {stage.duration_ms} ms, from {stage.entered_ms} ms to {stage.left_ms} ms</h4>
  <p>{esc(stage.note)}</p>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### The job record")
        st.dataframe(dispatch.rows(), width="stretch", hide_index=True)
        st.caption(
            f"Task id {esc(dispatch.task_id)}, derived from the task name, so "
            f"the same task is the same job rather than a new one each run."
        )

    # -----------------------------------------------------------------
    # PostgreSQL query optimizer
    # -----------------------------------------------------------------
    with tab_query:
        st.markdown("#### The same query, with the index and without")
        st.caption(
            "Without an index PostgreSQL reads every row and throws nearly all "
            "of them away, so the cost tracks the table. With one it descends "
            "a B tree and touches only the matching rows, so the cost tracks "
            "the answer. That is why a query that was instant at ten thousand "
            "rows is a timeout at two million, having changed not at all."
        )

        c1, c2 = st.columns([3, 2])
        rows = c1.slider("Rows in the table", min_value=10_000,
                         max_value=10_000_000, value=DEFAULT_ROWS,
                         step=10_000, key="eap_rows")
        indexed = c2.toggle("Index present", value=True, key="eap_indexed")

        result = benchmark_query(indexed=indexed, rows=rows)
        tone = "ok" if indexed else "crit"

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {tone}"><div class="n">{result.execution_ms:,.2f}</div>
    <div class="l">Execution time, ms</div></div>
  <div class="app-kpi {tone}"><div class="n">{result.rows_scanned:,}</div>
    <div class="l">Rows scanned</div></div>
  <div class="app-kpi ok"><div class="n">{result.rows_returned:,}</div>
    <div class="l">Rows returned</div></div>
  <div class="app-kpi"><div class="n">{result.planning_ms:.3f}</div>
    <div class="l">Planning time, ms</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### The plan")
        st.dataframe(result.rows_table(), width="stretch", hide_index=True)

        st.markdown("##### Both plans on this table")
        st.dataframe(benchmark_comparison(rows), width="stretch",
                     hide_index=True)

        st.markdown("##### EXPLAIN ANALYZE")
        st.code(result.explain(), language="text")

        st.markdown("##### The index this needs")
        st.code(INDEX_DDL, language="sql")
        st.caption(
            "CONCURRENTLY so the build does not hold a write lock on a live "
            "table, and partial on the status the query filters by, so the "
            "index stays small enough to stay in memory."
        )

    st.markdown(
        f"""
<div class="app-foot">
Enterprise AI Pipeline Console, engine version {ENGINE_VERSION}. A simulator:
no model is called, no queue is connected, no database is queried and no index
is built. Every figure is computed from the controls on screen, and every one
is derived from the input rather than from a random source, so the same
document scores the same twice.
</div>
""",
        unsafe_allow_html=True,
    )
