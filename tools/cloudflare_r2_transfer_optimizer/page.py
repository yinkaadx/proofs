"""Cloudflare R2 Transfer Optimization Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine could run as a capacity check in CI.

Everything is evaluated live from the controls, so no card on screen can
describe a plan that was replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.cloudflare_r2_transfer_optimizer.core import (
    ENGINE_VERSION,
    MAX_PRESIGN_SECONDS,
    NETWORK_TIERS,
    PROTOCOL_EFFICIENCY,
    PROTOCOLS,
    calculate_transfer_math,
    get_latency_breakdown,
    latency_rows,
    multipart_sweep,
    presign_validate,
    r2_limits,
    simulate_multipart_upload,
    single_stream_mbps,
    tier_table,
)

TIER_NAMES = [tier.name for tier in NETWORK_TIERS]


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>Cloudflare R2 Transfer Optimization Console</h1>
  <p>Three questions decide whether a large upload lands or times out. How much
  bandwidth the deadline actually demands, which is where reading megabytes as
  megabits makes a plan look eight times easier than it is. Whether splitting
  the file into parts helps at all, which depends on distance rather than on
  file size. And where the time goes on a request that has not sent a byte of
  payload yet.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Bandwidth**: set a size and a deadline, read the tier.\n"
            "2. **Multipart**: switch between a near link and a long haul one "
            "and watch the speedup appear.\n"
            "3. **Latency**: see how little of a small request is payload.\n"
            "4. **Limits**: the R2 rules a plan has to satisfy."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. No bucket is contacted, no "
            "object is uploaded and no presigned URL is issued."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_math, tab_parts, tab_latency, tab_limits = st.tabs(
        ["Bandwidth Math", "Multipart Planner", "Edge Latency", "R2 Limits"])

    # -----------------------------------------------------------------
    # Bandwidth math
    # -----------------------------------------------------------------
    with tab_math:
        st.markdown("#### What the deadline actually demands")
        st.caption(
            "A megabyte is eight megabits. Reading MB as Mb is the most common "
            "error in a transfer estimate, it is wrong by eight hundred "
            "percent, and it always makes the plan look achievable."
        )

        c1, c2 = st.columns(2)
        size = c1.number_input("File size, MB", min_value=1.0,
                               max_value=5_000_000.0, value=2048.0, step=128.0,
                               key="r2_size")
        deadline = c2.number_input("Deadline, seconds", min_value=1.0,
                                   max_value=86_400.0, value=120.0, step=10.0,
                                   key="r2_deadline")

        math = calculate_transfer_math(size, deadline)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {math.tone}">
    <div class="n">{math.required_mbps:,.0f}</div>
    <div class="l">Payload Mbps needed</div></div>
  <div class="app-kpi {math.tone}">
    <div class="n">{math.required_with_overhead_mbps:,.0f}</div>
    <div class="l">With protocol overhead</div></div>
  <div class="app-kpi {math.tone}"><div class="n">{esc(math.status)}</div>
    <div class="l">Verdict</div></div>
  <div class="app-kpi"><div class="n">{esc(math.minimum_tier)}</div>
    <div class="l">Smallest tier that clears it</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown(
            f"""
<div class="app-card {math.tone}">
  <h4><span class="app-tag {math.tone}">{esc(math.status)}</span>
  {size:,.0f} MB in {deadline:,.0f} seconds</h4>
  <p>{esc(math.note)}</p>
  <div class="app-ev">Rated speed is not delivered speed. The overhead figure
  assumes {PROTOCOL_EFFICIENCY:.0%} efficiency for TCP, TLS and HTTP framing,
  because planning at line rate is how a window gets missed.</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### The arithmetic")
        st.dataframe(math.rows(), width="stretch", hide_index=True)

        st.markdown("##### Every tier against this deadline")
        st.dataframe(tier_table(size, deadline), width="stretch",
                     hide_index=True)

    # -----------------------------------------------------------------
    # Multipart planner
    # -----------------------------------------------------------------
    with tab_parts:
        st.markdown("#### Parallel parts do not create bandwidth")
        st.caption(
            "One TCP stream can have a single congestion window in flight per "
            "round trip, and that ceiling does not care how fat the pipe is. "
            "Several streams fill a pipe that one stream leaves mostly empty, "
            "which is why chunking is worth a great deal at distance and "
            "nothing at all next door."
        )

        c1, c2, c3 = st.columns([3, 2, 2])
        tier_name = c1.selectbox("Link", TIER_NAMES, index=2, key="r2_tier")
        parts = c2.slider("Parts", min_value=1, max_value=64, value=8,
                          key="r2_parts")
        protocol = c3.selectbox("Protocol", list(PROTOCOLS), index=0,
                                key="r2_proto")

        plan = simulate_multipart_upload(size, parts, protocol, tier_name)
        tier = next(t for t in NETWORK_TIERS if t.name == tier_name)
        one_stream = single_stream_mbps(tier)
        window_bound = one_stream < tier.megabits * PROTOCOL_EFFICIENCY - 0.01

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {plan.tone}"><div class="n">{plan.speedup:,.2f}x</div>
    <div class="l">Speedup from parallel parts</div></div>
  <div class="app-kpi">
    <div class="n">{plan.single_stream_seconds:,.1f}</div>
    <div class="l">One stream, seconds</div></div>
  <div class="app-kpi {plan.tone}">
    <div class="n">{plan.parallel_seconds:,.1f}</div>
    <div class="l">{parts} parts, seconds</div></div>
  <div class="app-kpi"><div class="n">{one_stream:,.0f}</div>
    <div class="l">One stream ceiling, Mbps</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        if not plan.valid:
            st.markdown(
                f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">Rejected by R2</span>
  This plan would not complete</h4>
  <p>{esc(' '.join(plan.violations))}</p>
</div>
""",
                unsafe_allow_html=True,
            )
        else:
            verdict = ("Worth splitting" if plan.worth_it
                       else "Not worth splitting")
            reason = (
                f"{tier.name} is limited by the round trip rather than by the "
                f"link: one stream reaches {one_stream:,.0f} Mbps of a "
                f"{tier.megabits:,.0f} Mbps pipe, so parts fill the rest."
                if window_bound else
                f"{tier.name} is limited by the link, not the round trip. One "
                f"stream already reaches {one_stream:,.0f} Mbps, so extra "
                f"parts add {plan.overhead_seconds:,.2f} seconds of overhead "
                f"and buy nothing.")
            st.markdown(
                f"""
<div class="app-card {plan.tone}">
  <h4><span class="app-tag {plan.tone}">{esc(verdict)}</span>
  {plan.part_size_mib:,.1f} MiB per part</h4>
  <p>{esc(reason)}</p>
  <div class="app-ev">Round trip {tier.rtt_ms} ms.
  {esc(protocol)} costs {plan.overhead_seconds:,.2f} s across {parts}
  part(s).</div>
</div>
""",
                unsafe_allow_html=True,
            )
            st.markdown("##### This plan")
            st.dataframe(plan.rows(), width="stretch", hide_index=True)

        st.markdown("##### Where more parts stop helping")
        st.dataframe(multipart_sweep(size, protocol, tier_name),
                     width="stretch", hide_index=True)
        st.caption(
            "Read down the speedup column. It climbs, plateaus at the link "
            "ceiling, then falls as per part overhead outgrows the benefit."
        )

    # -----------------------------------------------------------------
    # Edge latency
    # -----------------------------------------------------------------
    with tab_latency:
        st.markdown("#### Most of a small request is not payload")
        st.caption(
            "DNS, the TLS handshake and time to first byte all happen before "
            "a byte of the object moves. On a small object that is most of the "
            "wall clock, so tuning the transfer is tuning the wrong thing: the "
            "win is a warm connection, not a faster pipe."
        )

        edges = get_latency_breakdown()
        nearest, furthest = edges[0], edges[-1]

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{nearest.total_ms:,.0f}</div>
    <div class="l">{esc(nearest.location)} total, ms</div></div>
  <div class="app-kpi crit"><div class="n">{furthest.total_ms:,.0f}</div>
    <div class="l">{esc(furthest.location)} total, ms</div></div>
  <div class="app-kpi warn">
    <div class="n">{nearest.overhead_share:.0%}</div>
    <div class="l">Spent before payload, nearest</div></div>
  <div class="app-kpi warn">
    <div class="n">{furthest.overhead_share:.0%}</div>
    <div class="l">Spent before payload, furthest</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for edge in edges:
            tone = "ok" if edge.total_ms < 150 else (
                "warn" if edge.total_ms < 280 else "crit")
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{edge.total_ms:,.1f} ms</span>
  {esc(edge.location)}</h4>
  <p>DNS {edge.dns_ms:,.1f} ms, TLS {edge.tls_ms:,.1f} ms,
  TTFB {edge.ttfb_ms:,.1f} ms, payload {edge.payload_ms:,.1f} ms.</p>
  <div class="app-ev">{edge.before_payload_ms:,.1f} ms, or
  {edge.overhead_share:.0%}, is gone before the object starts moving.</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### The breakdown")
        st.dataframe(latency_rows(), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # R2 limits
    # -----------------------------------------------------------------
    with tab_limits:
        st.markdown("#### The rules a plan has to satisfy")
        st.caption(
            "Verified against Cloudflare's own documentation. The equal part "
            "size rule is stricter than S3 and is the one most often met as a "
            "mysterious failure on part two rather than at the start."
        )
        st.dataframe(r2_limits(), width="stretch", hide_index=True)

        st.markdown("##### Presigned URL lifetime")
        seconds = st.number_input(
            "Expiry, seconds", min_value=0, max_value=1_000_000,
            value=3600, step=60, key="r2_presign")
        ok, message = presign_validate(int(seconds))
        (st.success if ok else st.error)(message)
        st.caption(
            f"R2 accepts one second to {MAX_PRESIGN_SECONDS:,} seconds. "
            f"Anything longer is refused at signing rather than failing later."
        )

    st.markdown(
        f"""
<div class="app-foot">
Cloudflare R2 Transfer Optimization Console, engine version {ENGINE_VERSION}. A
simulator: no bucket is contacted, no object is uploaded, no presigned URL is
issued and no network is measured. Every figure is computed from the controls
on screen, and nothing here reads the clock or a random source.
</div>
""",
        unsafe_allow_html=True,
    )
