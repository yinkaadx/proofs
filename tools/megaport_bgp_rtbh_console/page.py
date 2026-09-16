"""Megaport BGP RTBH & Blackhole Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the generator and the validator are unit testable on their own.

The output here gets pasted into a production router, so the validator refuses
rather than warns and the rollback is generated with the change.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.megaport_bgp_rtbh_console.core import (
    COMMUNITY_NOTE,
    DEFAULT_NEIGHBOR,
    DEFAULT_PARENT,
    DEFAULT_VICTIM,
    DROP_LOCAL,
    DROP_UPSTREAM,
    ENGINE_VERSION,
    MEGAPORT_BLACKHOLE,
    WELL_KNOWN_BLACKHOLE,
    console_summary,
    drop_comparison,
    generate_cisco_rtbh_config,
    normalise_host,
    simulate_megaport_edge_drop,
    validate_blackhole_prefix,
)


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>Megaport BGP RTBH &amp; Blackhole Console</h1>
  <p>Two things make a remotely triggered black hole fail without producing a
  single error. The communities are stripped on the way out because the
  neighbour was never told to send them, so the upstream sees an ordinary
  prefix. And the drop happens on your own border, after the packets have
  already crossed the circuit they were meant to save. Both are modelled here,
  because both look like success from the router you are logged into.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    summary = console_summary()

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Validate**: try a /24 and see it refused, not warned.\n"
            "2. **Config**: the generated lines, with the rollback.\n"
            "3. **Drop**: turn the upstream off and watch the circuit stay "
            "full.\n"
            "4. Confirm the provider community before an incident."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. No router is configured, no "
            "session is established and no route is announced."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_validate, tab_config, tab_drop = st.tabs(
        ["Prefix Validator", "Cisco Configuration", "Circuit Drop"])

    c1, c2, c3 = st.columns([2, 2, 2])
    victim = c1.text_input("Victim host", value=DEFAULT_VICTIM,
                           key="rtbh_victim")
    parent = c2.text_input("Parent prefix you originate", value=DEFAULT_PARENT,
                           key="rtbh_parent")
    neighbor = c3.text_input("Upstream neighbour", value=DEFAULT_NEIGHBOR,
                             key="rtbh_neighbor")

    try:
        host = normalise_host(victim)
        host_error = ""
    except ValueError as exc:
        host = victim
        host_error = str(exc)

    validation = validate_blackhole_prefix(host, parent)

    # -----------------------------------------------------------------
    # Validator
    # -----------------------------------------------------------------
    with tab_validate:
        st.markdown("#### Every check is a refusal, not a warning")
        st.caption(
            "A warning on a blackhole generator is a warning somebody "
            "dismisses at three in the morning, and the cost of dismissing it "
            "is dropping traffic for a whole subnet instead of for one host."
        )

        if host_error:
            st.error(host_error)

        passed = len([c for c in validation.checks if c.passed])
        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {validation.tone}">
    <div class="n">{esc(validation.status)}</div>
    <div class="l">Validation</div></div>
  <div class="app-kpi"><div class="n">{passed}</div>
    <div class="l">of {len(validation.checks)} checks passing</div></div>
  <div class="app-kpi"><div class="n">
    {'IPv' + str(validation.version) if validation.version else 'n/a'}</div>
    <div class="l">Address family</div></div>
  <div class="app-kpi"><div class="n">
    {'/' + str(validation.host_length) if validation.host_length else 'n/a'}
    </div><div class="l">Required prefix length</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for check in validation.checks:
            st.markdown(
                f"""
<div class="app-card {check.tone}">
  <h4><span class="app-tag {check.tone}">
  {'pass' if check.passed else 'FAIL'}</span> {esc(check.name)}</h4>
  <p>{esc(check.detail)}</p>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### Every check")
        st.dataframe(validation.rows(), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Configuration
    # -----------------------------------------------------------------
    with tab_config:
        st.markdown("#### The configuration, and the way back out")

        community = st.text_input(
            "Provider blackhole community", value=MEGAPORT_BLACKHOLE,
            key="rtbh_community")
        asn = st.number_input("Your local ASN", min_value=1,
                              max_value=4_294_967_294, value=65001,
                              key="rtbh_asn")

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{esc(community)}</div>
    <div class="l">Provider community</div></div>
  <div class="app-kpi"><div class="n">{esc(WELL_KNOWN_BLACKHOLE)}</div>
    <div class="l">RFC 7999 well known</div></div>
  <div class="app-kpi ok"><div class="n">no-export</div>
    <div class="l">Stops it travelling further</div></div>
  <div class="app-kpi"><div class="n">{summary['config_lines']}</div>
    <div class="l">Lines generated</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        if not validation.valid:
            st.error(
                "No configuration is generated while the prefix is invalid. "
                "A generator that hands back a config for a /24 and leaves "
                "the checking to the operator has moved the failure from the "
                "tool to the router, which is the wrong place for it.")
        else:
            config = generate_cisco_rtbh_config(host, parent, neighbor,
                                                community, int(asn))
            st.markdown("##### Apply")
            st.code(config.config, language="text")

            st.markdown("##### Roll back")
            st.code(config.removal, language="text")

            for warning in config.warnings:
                st.warning(warning)

    # -----------------------------------------------------------------
    # Circuit drop
    # -----------------------------------------------------------------
    with tab_drop:
        st.markdown("#### Which side is dropping decides everything")
        st.caption(
            "Discarding at your own border does nothing for a saturated "
            "circuit, because the packets crossed the link to reach the "
            "router that discards them. Only the upstream acting on the "
            "community keeps them off the access port."
        )

        d1, d2, d3 = st.columns(3)
        circuit = d1.number_input("Circuit, Gbps", 0.1, 400.0, 10.0, 1.0,
                                  key="rtbh_circuit")
        attack = d2.number_input("Attack, Gbps", 0.0, 1000.0, 18.0, 1.0,
                                 key="rtbh_attack")
        baseline = d3.number_input("Legitimate, Gbps", 0.0, 400.0, 2.4, 0.1,
                                   key="rtbh_base")

        d4, d5 = st.columns(2)
        blackholed = d4.toggle("Blackhole announced", value=True,
                               key="rtbh_on")
        honours = d5.toggle("Upstream acts on the community", value=True,
                            key="rtbh_up")

        drop = simulate_megaport_edge_drop(blackholed, attack, circuit,
                                           baseline, honours)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi crit">
    <div class="n">{drop.utilisation_before:.0%}</div>
    <div class="l">Utilisation before</div></div>
  <div class="app-kpi {drop.tone}">
    <div class="n">{drop.utilisation_after:.0%}</div>
    <div class="l">Utilisation after</div></div>
  <div class="app-kpi {drop.tone}">
    <div class="n">{drop.delivered_gbps:,.1f}</div>
    <div class="l">On the circuit, Gbps</div></div>
  <div class="app-kpi {'ok' if drop.recovered else 'crit'}">
    <div class="n">{'Yes' if drop.recovered else 'No'}</div>
    <div class="l">Circuit recovered</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown(
            f"""
<div class="app-card {drop.tone}">
  <h4><span class="app-tag {drop.tone}">{esc(drop.state)}</span>
  {drop.offered_gbps:,.1f} Gbps offered against a
  {drop.circuit_gbps:,.1f} Gbps circuit</h4>
  <p>{esc(drop.note)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        if drop.state == DROP_LOCAL:
            st.error(
                "This is the state most easily mistaken for a working "
                "blackhole. The discard counters on Null0 climb steadily "
                "while the link stays completely full.")
        elif drop.state == DROP_UPSTREAM:
            st.success(
                "The circuit is back for every other service on it. The host "
                "under attack stays unreachable, which is the trade RTBH "
                "makes and not a side effect of it.")

        st.markdown("##### The numbers")
        st.dataframe(drop.rows(), width="stretch", hide_index=True)

        st.markdown("##### All three states together")
        st.dataframe(drop_comparison(attack, circuit, baseline),
                     width="stretch", hide_index=True)
        st.caption(COMMUNITY_NOTE)

    st.markdown(
        f"""
<div class="app-foot">
Megaport BGP RTBH &amp; Blackhole Console, engine version {ENGINE_VERSION}. A
generator and a simulator: no router is configured, no BGP session is
established, no route is announced and no traffic is measured. Validate the
generated configuration against your own change process before applying it.
</div>
""",
        unsafe_allow_html=True,
    )
