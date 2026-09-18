"""iDRAC Rocky Linux Provisioning Console.

Rendered inside the hub app. Every block on this page is generated from the
controls on each run, so a config on screen can never describe a layout that
was replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.idrac_rocky_kickstart_console.core import (
    ENGINE_VERSION,
    HASH_COMMAND,
    LAYOUTS,
    MIB_PER_GIB,
    PHASES,
    ROOT_HASHED,
    ROOT_LOCKED,
    ROOT_PLAINTEXT,
    SERVERS_BEFORE_CONTENTION,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    STATUS_BLOCKED,
    generate_kickstart_config,
    simulate_idrac_mount,
    track_parallel_deployments,
)

TONE = {SEVERITY_OK: "ok", SEVERITY_WARN: "warn", SEVERITY_CRITICAL: "crit"}
LAYOUT_BY_TITLE = {layout.title: layout for layout in LAYOUTS}


def _kpis(items: list[tuple[str, str]]) -> None:
    cells = "".join(
        f'<div class="app-kpi"><b>{esc(value)}</b><span>{esc(label)}</span></div>'
        for label, value in items)
    st.markdown(f'<div class="app-kpis">{cells}</div>', unsafe_allow_html=True)


def _finding_card(finding) -> None:
    tone = TONE.get(finding.severity, "warn")
    st.markdown(
        f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(finding.code)}</span>
  {esc(finding.title)}</h4>
  <p>{esc(finding.detail)}</p>
  <div class="app-ev">{esc(finding.fix)}</div>
</div>
""",
        unsafe_allow_html=True,
    )


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>iDRAC Rocky Linux Provisioning Console</h1>
  <p>Racking twenty servers and installing them one at a time is a week of
  somebody's life. Doing it from the lights out controller with a Kickstart
  file is an afternoon, and it fails in three places that are all checkable
  before the first boot rather than after the first wipe. clearpart takes
  every disk it can see unless you scope it. A root password in a Kickstart
  is a root password every machine downloads. And twenty machines pulling one
  image from one share do not finish in the time one machine takes.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Kickstart**: pick a layout, leave the password blank, and "
            "read what the generator does instead of writing one.\n"
            "2. **Virtual media**: give it a path that is not a share and "
            "watch it refuse to emit commands at all.\n"
            "3. **Deployments**: raise the server count past four and watch "
            "the bottleneck move from the servers to the share."
        )
        st.divider()
        st.caption(
            "Nothing here touches a server. No iDRAC is contacted, no image "
            "is mounted and nothing is installed anywhere."
        )
        st.caption(
            "Type a real root password into no web page, this one included. "
            f"Run {HASH_COMMAND} on your own machine and paste the hash."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_ks, tab_drac, tab_fleet = st.tabs(
        ["Kickstart Generator", "iDRAC Virtual Media", "Parallel Deployments"])

    # -----------------------------------------------------------------
    with tab_ks:
        st.subheader("A ks.cfg that says what it will destroy")
        left, right = st.columns(2)
        with left:
            title = st.selectbox("Partition layout",
                                 [layout.title for layout in LAYOUTS])
        with right:
            root_password = st.text_input(
                "Root password hash, or blank to lock the account",
                value="", type="password")
            st.caption(
                "A string starting with a crypt prefix is used as a hash. "
                "Anything else is treated as plain text and flagged. Blank "
                "locks root, which is the better answer."
            )

        chosen = LAYOUT_BY_TITLE[title]
        result = generate_kickstart_config(chosen, root_password)
        mode_label = {ROOT_HASHED: "hash", ROOT_LOCKED: "locked",
                      ROOT_PLAINTEXT: "plain text"}[result.root_mode]

        _kpis([
            ("Install disk", result.layout.install_disk),
            ("Volume group", result.layout.volume_group),
            ("Fixed allocation", f"{result.allocated_gib} GiB"),
            ("Smallest disk", f"{result.minimum_disk_gib} GiB"),
            ("Root password", mode_label),
            ("Safe to serve", "yes" if result.is_safe_to_serve else "no"),
        ])
        st.caption(esc(result.layout.purpose))

        if not result.is_safe_to_serve:
            st.markdown(
                f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">DO NOT PUBLISH</span>
  This file carries a password in clear text</h4>
  <p>Every machine that installs from it downloads the password, and a copy
  stays at /root/anaconda-ks.cfg on each one afterwards.</p>
  <div class="app-ev">Run {esc(HASH_COMMAND)} locally and paste the hash
  above instead.</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("**The volume plan**")
        for volume in result.layout.volumes:
            size = ("grows into whatever is left" if volume.grow
                    else f"{volume.size_mib / MIB_PER_GIB:.0f} GiB")
            options = f", mounted {volume.fsoptions}" if volume.fsoptions else ""
            st.markdown(
                f"- `{volume.name}` at **{volume.mount}**, {size}, "
                f"{volume.fstype}{options}")
        st.caption(
            f"Boot and EFI take "
            f"{(result.layout.boot_mib + result.layout.efi_mib) / MIB_PER_GIB:.1f} "
            f"GiB, the fixed volumes take the rest of the "
            f"{result.allocated_gib} GiB, and that total is the sum of the "
            f"rows above."
        )

        st.markdown("**ks.cfg**")
        st.code(result.config, language="bash")

        st.markdown("**What this file will do to the machine**")
        for finding in result.findings:
            _finding_card(finding)

    # -----------------------------------------------------------------
    with tab_drac:
        st.subheader("Attach the image and boot from it once")
        left, right = st.columns(2)
        with left:
            server_ip = st.text_input("iDRAC address", value="10.20.30.11")
        with right:
            iso_path = st.text_input(
                "ISO on a share the iDRAC can reach",
                value="//nas01/isos/Rocky-9.5-x86_64-dvd.iso")

        plan = simulate_idrac_mount(server_ip, iso_path)
        _kpis([
            ("Status", plan.status),
            ("Share type", plan.share_type),
            ("Commands", str(len([c for c in plan.commands
                                  if not c.startswith("#")]))),
            ("Worst finding", plan.severity),
        ])

        if plan.status == STATUS_BLOCKED:
            st.markdown(
                """
<div class="app-card crit">
  <h4><span class="app-tag crit">BLOCKED</span> No commands were generated</h4>
  <p>A command line with a stand in where the address or the path should be
  is how a stand in gets pasted into a terminal, and the first four commands
  would run against the wrong machine before the fifth one failed.</p>
  <div class="app-ev">Fix the findings below and the sequence appears.</div>
</div>
""",
                unsafe_allow_html=True,
            )
        else:
            st.markdown("**The sequence**")
            st.code("\n".join(plan.commands), language="bash")
            st.markdown("**Afterwards**")
            st.code("\n".join(plan.teardown), language="bash")

        st.markdown("**Before you run it**")
        for finding in plan.findings:
            _finding_card(finding)

    # -----------------------------------------------------------------
    with tab_fleet:
        st.subheader("A rack installing at once")
        left, right = st.columns(2)
        with left:
            server_count = st.slider("Servers in this wave", 1, 48, 12, 1)
        with right:
            minutes = st.slider("Minutes into the window", 0, 120, 9, 1)

        board = track_parallel_deployments(int(server_count), int(minutes))
        _kpis([
            ("Servers", str(board.server_count)),
            ("Online", str(board.online)),
            ("Building", str(board.in_progress)),
            ("Stuck", str(board.stalled)),
            ("Wave finishes in", f"{board.parallel_minutes} min"),
            ("One at a time", f"{board.serial_minutes} min"),
        ])
        st.caption(
            f"{board.online} online plus {board.in_progress} building plus "
            f"{board.stalled} stuck equals {board.server_count} servers, "
            f"which is the whole wave."
        )

        tone = "warn" if board.server_count > SERVERS_BEFORE_CONTENTION else "ok"
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">BOTTLENECK</span>
  {esc(board.bottleneck)}</h4>
  <p>{esc(board.headline)}.</p>
  <div class="app-ev">{esc(board.fix)}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("**By phase**")
        for name, _minutes in PHASES:
            count = board.by_phase.get(name, 0)
            if count:
                st.markdown(f"- **{name}**: {count} server(s)")

        st.markdown("**Every machine**")
        for server in board.servers:
            row_tone = "ok" if server.phase == "Online" else (
                "crit" if not server.healthy else "info")
            remaining = ("done" if server.phase == "Online" else
                         "waiting on a person" if not server.healthy else
                         f"{server.minutes_remaining} min left")
            st.markdown(
                f"""
<div class="app-card {row_tone}">
  <h4><span class="app-tag {row_tone}">{server.percent_complete}%</span>
  {esc(server.hostname)} at {esc(server.idrac_ip)}</h4>
  <p>{esc(server.note)}</p>
  <div class="app-ev">{esc(server.phase)}, {esc(remaining)}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.subheader("What this forecast rests on")
        for line in board.assumptions:
            st.markdown(f"- {line}")
