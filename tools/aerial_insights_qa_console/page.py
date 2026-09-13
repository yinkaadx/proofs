"""Aerial Insights QA and Production Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine can run in CI as a pre launch gate.

Everything on this page is evaluated live from the controls. There is no
session state to go stale, which removes the class of bug where a card on
screen describes a run that was replaced two interactions ago.
"""

from __future__ import annotations

import json

import streamlit as st

from shared.theme import esc, inject
from tools.aerial_insights_qa_console.core import (
    ACCEPTED,
    BAD_SIGNATURE,
    BASE_RSS_MB,
    CONTAINER_LIMIT_MB,
    DEPLOY_STEPS,
    ENGINE_VERSION,
    LEAK_MB_PER_TASK,
    PRODUCTION_URL_BROKEN,
    PRODUCTION_URL_FIXED,
    REPLAYED,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    STALE,
    STEP_PASS,
    PoolConfig,
    audit_pool,
    pool_comparison,
    replay_deliveries,
    run_deploy,
    run_queue,
    sample_deliveries,
    sample_tiles,
    sign,
)

SEVERITY_TONE = {SEVERITY_CRITICAL: "crit", SEVERITY_OK: "ok"}
OUTCOME_TONE = {ACCEPTED: "ok", REPLAYED: "info", BAD_SIGNATURE: "crit",
                STALE: "crit"}

NOW = 1_789_000_000


def _tone(severity: str) -> str:
    return SEVERITY_TONE.get(severity, "warn")


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>Aerial Insights QA &amp; Production Console</h1>
  <p>Four things stand between a working demo and a production launch, and each
  is invisible until the load arrives: two workers claiming the same tile, a
  worker whose memory climbs until the container kills it, every serverless
  instance opening its own database pool, and a Stripe webhook delivered twice.
  None is hard to fix. All four are hard to see, so each one is made visible
  here with the error code the real system returns and the same run with the
  fix applied.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Worker Queue**: run the batch broken, then turn on the two "
            "fixes and run it again.\n"
            "2. **Prisma Auditor**: raise the instance count until the pool "
            "gives way, then add PgBouncer.\n"
            "3. **Stripe Ledger**: watch a retry, a forgery and a replay all "
            "refused.\n"
            "4. **Deployment Closure**: fail any gate and see production "
            "left alone."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. The workers, the database and "
            "Stripe are simulated, and no migration is ever applied."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_queue, tab_pool, tab_stripe, tab_deploy = st.tabs(
        ["Worker Queue", "Prisma Auditor", "Stripe Ledger",
         "Deployment Closure"]
    )

    # -----------------------------------------------------------------
    # YOLO worker queue
    # -----------------------------------------------------------------
    with tab_queue:
        st.markdown("#### The tile batch")
        st.caption(
            "The race is not modelled as a dice roll. It is the shape of the "
            "bug: without an atomic claim, every worker polling on the same "
            "tick reads the same head row as queued and takes it. A dice roll "
            "would be dishonest, because this is exactly why the bug survives "
            "a staging environment running one worker."
        )

        c1, c2, c3 = st.columns(3)
        tile_count = c1.slider("Tiles in the batch", min_value=4, max_value=40,
                               value=24, step=4, key="aiq_tiles")
        workers = c2.slider("Workers", min_value=1, max_value=6, value=3,
                            step=1, key="aiq_workers")
        c3.caption(
            f"Each worker starts at {BASE_RSS_MB} MB with the model loaded, "
            f"and the container limit is {CONTAINER_LIMIT_MB} MB."
        )

        c4, c5 = st.columns(2)
        atomic = c4.toggle("Claim atomically, with FOR UPDATE SKIP LOCKED",
                           value=False, key="aiq_atomic")
        releases = c5.toggle("Release tensors and images after each tile",
                             value=False, key="aiq_release")

        run = run_queue(sample_tiles(tile_count), worker_count=workers,
                        atomic_claim=atomic, releases_memory=releases)
        fixed = run_queue(sample_tiles(tile_count), worker_count=workers,
                          atomic_claim=True, releases_memory=True)
        tone = "ok" if run.healthy else "crit"

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {tone}"><div class="n">{run.wasted_inferences}</div>
    <div class="l">Inferences nobody asked for</div></div>
  <div class="app-kpi crit"><div class="n">{run.restarts}</div>
    <div class="l">OOM restarts</div></div>
  <div class="app-kpi crit"><div class="n">{run.lost}</div>
    <div class="l">Tiles lost</div></div>
  <div class="app-kpi"><div class="n">{run.ticks}</div>
    <div class="l">Ticks to drain, against {fixed.ticks} fixed</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for finding in run.findings:
            card = _tone(finding.severity)
            st.markdown(
                f"""
<div class="app-card {card}">
  <h4><span class="app-tag {card}">{esc(finding.code)}</span>
  {esc(finding.title)}</h4>
  <p>{esc(finding.detail)}</p>
  <div class="app-ev">{esc(finding.fix)}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### Workers")
        st.dataframe(run.worker_rows(), width="stretch", hide_index=True)

        st.markdown("##### Tiles")
        st.dataframe(run.tile_rows(), width="stretch", hide_index=True)

        st.markdown("##### The same batch with both fixes on")
        st.dataframe(
            [{"Measure": "Inferences nobody asked for",
              "This run": str(run.wasted_inferences),
              "Both fixes on": str(fixed.wasted_inferences)},
             {"Measure": "OOM restarts", "This run": str(run.restarts),
              "Both fixes on": str(fixed.restarts)},
             {"Measure": "Tiles lost", "This run": str(run.lost),
              "Both fixes on": str(fixed.lost)},
             {"Measure": "Tiles completed", "This run": str(run.completed),
              "Both fixes on": str(fixed.completed)},
             {"Measure": "Ticks to drain the batch", "This run": str(run.ticks),
              "Both fixes on": str(fixed.ticks)}],
            width="stretch", hide_index=True)
        st.caption(
            f"The leak is {LEAK_MB_PER_TASK} MB a tile, so a worker crosses "
            f"the container limit after "
            f"{(CONTAINER_LIMIT_MB - BASE_RSS_MB) // LEAK_MB_PER_TASK} tiles. "
            f"That reads as a slow leak in staging and an hourly crash in "
            f"production."
        )

    # -----------------------------------------------------------------
    # Prisma connection pooling
    # -----------------------------------------------------------------
    with tab_pool:
        st.markdown("#### Connections against the database")
        st.caption(
            "Every serverless instance opens its own Prisma pool. The database "
            "sees instances multiplied by connection_limit, which is a number "
            "staging never reaches because staging never runs forty instances "
            "at once."
        )

        c1, c2, c3 = st.columns(3)
        instances = c1.slider("Concurrent instances", min_value=1,
                              max_value=200, value=40, step=1,
                              key="aiq_instances")
        limit = c2.slider("Prisma connection_limit", min_value=1, max_value=20,
                          value=5, step=1, key="aiq_limit")
        max_connections = c3.slider("Postgres max_connections", min_value=20,
                                    max_value=500, value=100, step=10,
                                    key="aiq_max")

        c4, c5 = st.columns(2)
        bouncer = c4.toggle("PgBouncer in transaction mode", value=False,
                            key="aiq_bouncer")
        timeout = c5.slider("pool_timeout seconds", min_value=2, max_value=60,
                            value=10, step=1, key="aiq_timeout")

        config = PoolConfig(max_connections=max_connections, instances=instances,
                            connection_limit=limit, pool_timeout_s=timeout,
                            pgbouncer=bouncer)
        findings = audit_pool(config)
        tone = "crit" if config.exhausted else (
            "warn" if config.utilisation > 0.8 else "ok")

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {tone}"><div class="n">{config.demanded}</div>
    <div class="l">Connections opened</div></div>
  <div class="app-kpi"><div class="n">{config.available}</div>
    <div class="l">Available to the app</div></div>
  <div class="app-kpi {tone}"><div class="n">{config.utilisation:.0%}</div>
    <div class="l">Pool utilisation</div></div>
  <div class="app-kpi {'crit' if config.headroom < 0 else 'ok'}">
    <div class="n">{config.headroom}</div>
    <div class="l">Headroom</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for finding in findings:
            card = _tone(finding.severity)
            st.markdown(
                f"""
<div class="app-card {card}">
  <h4><span class="app-tag {card}">{esc(finding.code)}</span>
  {esc(finding.title)}</h4>
  <p>{esc(finding.detail)}</p>
  <div class="app-ev">{esc(finding.fix)}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### The numbers")
        st.dataframe(config.rows(), width="stretch", hide_index=True)

        st.markdown("##### The same load, with and without the bouncer")
        st.dataframe(pool_comparison(config), width="stretch", hide_index=True)

        st.markdown("##### The connection string")
        st.code(f"# what is deployed now\n{PRODUCTION_URL_BROKEN}\n\n"
                f"# what it should be\n{PRODUCTION_URL_FIXED}",
                language="bash")
        st.caption(
            "connection_limit drops to 1 when the bouncer is in front. Two "
            "pools in series queue twice, and the outer one times out first."
        )

    # -----------------------------------------------------------------
    # Stripe idempotency ledger
    # -----------------------------------------------------------------
    with tab_stripe:
        st.markdown("#### Four deliveries, one charge")
        st.caption(
            "The gates run in the order Stripe's own guidance puts them: "
            "verify the signature before parsing anything, refuse a timestamp "
            "outside tolerance so a captured request cannot be replayed later, "
            "check the livemode, and only then check whether this event id has "
            "already been handled."
        )

        deliveries = sample_deliveries(NOW)
        delivered = st.slider("Deliveries received", min_value=1,
                              max_value=len(deliveries), value=len(deliveries),
                              key="aiq_deliveries")
        ledger = replay_deliveries(deliveries[:delivered], NOW)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{ledger.count(ACCEPTED)}</div>
    <div class="l">Accepted</div></div>
  <div class="app-kpi"><div class="n">{ledger.count(REPLAYED)}</div>
    <div class="l">Retries blocked</div></div>
  <div class="app-kpi crit"><div class="n">{ledger.count(BAD_SIGNATURE)}</div>
    <div class="l">Signatures rejected</div></div>
  <div class="app-kpi crit"><div class="n">{ledger.count(STALE)}</div>
    <div class="l">Replays refused</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        last = ledger.results[-1]
        tone = OUTCOME_TONE.get(last.outcome, "warn")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(last.outcome)}</span>
  {esc(last.delivery.event_id)}</h4>
  <div class="app-ev">{esc(last.delivery.event_type)} &nbsp;
  {last.charged_cents / 100:.2f} charged</div>
  <p>{esc(last.detail)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### The ledger")
        st.dataframe(ledger.rows(), width="stretch", hide_index=True)

        st.markdown(
            f"""
<div class="app-card ok">
  <h4><span class="app-tag ok">Charged once</span>
  {ledger.charged_cents / 100:.2f} taken across {delivered} delivery(s)</h4>
  <p>The retry returned the stored response rather than being processed again,
  and neither the forged request nor the hour old capture reached the
  handler.</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### The signature header, computed the way Stripe does")
        st.code(
            f"Stripe-Signature: {sign(deliveries[0].payload, NOW)}\n\n"
            f"# signed payload is the timestamp, a full stop, then the exact\n"
            f"# request body. Signing a re serialised body is the mistake that\n"
            f"# fails in production and passes in a test that serialises the\n"
            f"# same way twice.",
            language="text")

        st.markdown("##### The event as delivered")
        st.code(json.dumps({
            "id": last.delivery.event_id,
            "type": last.delivery.event_type,
            "created": last.delivery.timestamp,
            "livemode": last.delivery.livemode,
            "data": {"object": last.delivery.payload},
        }, indent=2), language="json")

    # -----------------------------------------------------------------
    # Deployment closure
    # -----------------------------------------------------------------
    with tab_deploy:
        st.markdown("#### Nothing touches production out of order")
        st.caption(
            "A migration applied without a rehearsed rollback is not a "
            "deployment, it is a bet. The evidence has to exist before the "
            "change rather than be reconstructed afterwards from memory."
        )

        c1, c2, c3 = st.columns(3)
        dry_run_ok = c1.toggle("Dry run completes", value=True,
                               key="aiq_dry_run")
        evidence_ok = c2.toggle("Evidence captured", value=True,
                                key="aiq_evidence")
        rollback_ok = c3.toggle("Rollback rehearsed", value=True,
                                key="aiq_rollback")

        result = run_deploy(dry_run_ok, evidence_ok, rollback_ok)
        tone = "ok" if result.applied else "crit"

        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">
  {esc('Applied' if result.applied else 'Production untouched')}</span>
  {len([s for s in result.statuses.values() if s == STEP_PASS])} of
  {len(DEPLOY_STEPS)} steps passed</h4>
  <p>{esc(result.blocked_reason or 'Every gate passed in order, so the '
          'migration ran against production with the rollback already proved '
          'on a snapshot.')}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### The sequence")
        st.dataframe(result.rows(), width="stretch", hide_index=True)

        st.markdown("##### Terminal")
        st.code(result.terminal(), language="bash")

        if not result.applied:
            st.error(
                "The apply step is blocked, not skipped. A pipeline that "
                "continues past a failed gate and reports success is worse "
                "than one that has no gate at all."
            )

    st.markdown(
        f"""
<div class="app-foot">
Aerial Insights QA &amp; Production Console, engine version {ENGINE_VERSION}. A
simulator: no worker runs, no database is connected, no Stripe endpoint is
called and no migration is ever applied. Every figure is computed from the
controls on screen.
</div>
""",
        unsafe_allow_html=True,
    )
