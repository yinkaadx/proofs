"""Tests for the iDRAC Rocky Linux Provisioning Console.

Two things are worth proving. A generated Kickstart must never widen what it
destroys: clearpart is scoped to a named drive in every layout, ignoredisk
backs it up, and the file is checked structurally rather than by eye. And the
iDRAC plan must emit no command at all when its inputs are wrong, because a
command line carrying a stand in is one paste away from running against the
wrong machine.

Pass marker: pytest reports all tests passed, exit 0.
Fail marker: any failure or error line, exit non zero.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.idrac_rocky_kickstart_console.core import (  # noqa: E402
    CIS_HARDENED,
    ENGINE_VERSION,
    HASH_COMMAND,
    LAYOUTS,
    MIB_PER_GIB,
    MINIMAL,
    PHASES,
    ROOT_HASHED,
    ROOT_LOCKED,
    ROOT_PLAINTEXT,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARN,
    SHARE_CIFS,
    SHARE_HTTP,
    SHARE_NFS,
    SHARE_UNKNOWN,
    STANDARD,
    STATUS_BLOCKED,
    STATUS_READY,
    classify_root_password,
    classify_share,
    generate_kickstart_config,
    simulate_idrac_mount,
    track_parallel_deployments,
)

GOOD_IP = "10.20.30.11"
GOOD_ISO = "//nas01/isos/Rocky-9.5-x86_64-dvd.iso"
SHA512 = "$6$rounds=5000$abcdefgh$" + "x" * 60


def lines_of(config: str) -> list[str]:
    return [line.strip() for line in config.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Kickstart generation
# ---------------------------------------------------------------------------


def test_every_layout_scopes_clearpart_to_one_named_disk():
    """The destroying command, checked for every layout rather than one."""
    for layout in LAYOUTS:
        config = generate_kickstart_config(layout, SHA512).config
        clearpart = [line for line in lines_of(config)
                     if line.startswith("clearpart")]
        assert len(clearpart) == 1, layout.key
        assert f"--drives={layout.install_disk}" in clearpart[0], layout.key
        assert f"ignoredisk --only-use={layout.install_disk}" in config, layout.key


def test_no_layout_ever_emits_an_unscoped_clearpart():
    for layout in LAYOUTS:
        config = generate_kickstart_config(layout, SHA512).config
        for line in lines_of(config):
            if line.startswith("clearpart"):
                assert "--drives=" in line, (layout.key, line)


def test_the_file_carries_the_directives_an_install_cannot_start_without():
    config = generate_kickstart_config(STANDARD, SHA512).config
    for directive in ("#version=RHEL9", "text", "lang ", "keyboard ",
                      "timezone ", "network ", "bootloader ", "zerombr",
                      "%packages", "%end", "%post", "reboot"):
        assert directive in config, directive
    assert config.count("%end") >= 3, "each block section must be closed"
    assert config.endswith("\n")


def test_every_volume_in_the_layout_reaches_the_file():
    for layout in LAYOUTS:
        config = generate_kickstart_config(layout, SHA512).config
        logvols = [line for line in lines_of(config) if line.startswith("logvol")]
        assert len(logvols) == len(layout.volumes), layout.key
        for volume in layout.volumes:
            # Whole token, not a substring: --name=lv_var is a prefix of
            # --name=lv_vartmp, and a loose match here would pass three
            # lines off as one.
            matching = [line for line in logvols
                        if f"--name={volume.name}" in line.split()]
            assert len(matching) == 1, (layout.key, volume.name)
            line = matching[0]
            assert line.startswith(f"logvol {volume.mount} "), volume.mount
            assert f"--vgname={layout.volume_group}" in line
            assert f"--fstype={volume.fstype}" in line
            tokens = line.split()
            if volume.grow:
                assert "--grow" in tokens
            else:
                assert f"--size={volume.size_mib}" in tokens
                assert "--grow" not in tokens
            if volume.fsoptions:
                assert f'--fsoptions="{volume.fsoptions}"' in line


def test_exactly_one_volume_grows_in_each_layout():
    """Two growing volumes is a layout that will not allocate as intended."""
    for layout in LAYOUTS:
        growing = [v for v in layout.volumes if v.grow]
        assert len(growing) == 1, layout.key


def test_the_allocation_total_equals_the_sum_of_the_rows():
    for layout in LAYOUTS:
        result = generate_kickstart_config(layout, SHA512)
        fixed = sum(v.size_mib for v in layout.volumes if not v.grow)
        assert layout.allocated_mib == layout.boot_mib + layout.efi_mib + fixed
        assert result.allocated_gib == round(
            layout.allocated_mib / MIB_PER_GIB, 1)
        assert result.minimum_disk_gib >= result.allocated_gib


def test_the_cis_layout_hardens_the_mounts_the_benchmark_names():
    config = generate_kickstart_config(CIS_HARDENED, SHA512).config
    mounts = {v.mount: v.fsoptions for v in CIS_HARDENED.volumes}
    for mount in ("/tmp", "/var/tmp", "/var/log", "/var/log/audit"):
        assert mount in mounts, mount
        assert "noexec" in mounts[mount], mount
        assert "nodev" in mounts[mount] and "nosuid" in mounts[mount], mount
    assert "/var/log/audit" in config
    assert "nodev" in mounts["/home"]


def test_a_hash_is_used_as_a_hash_and_marked_safe_to_serve():
    result = generate_kickstart_config(STANDARD, SHA512)
    assert result.root_mode == ROOT_HASHED
    assert result.rootpw_line == f"rootpw --iscrypted {SHA512}"
    assert result.is_safe_to_serve
    assert f"rootpw --iscrypted {SHA512}" in result.config
    assert "--plaintext" not in result.config


def test_a_plaintext_password_is_critical_and_names_the_command_that_fixes_it():
    result = generate_kickstart_config(STANDARD, "hunter2")
    assert result.root_mode == ROOT_PLAINTEXT
    assert result.severity == SEVERITY_CRITICAL
    assert not result.is_safe_to_serve
    assert "rootpw --plaintext hunter2" in result.config
    fix = " ".join(f.fix for f in result.findings)
    assert HASH_COMMAND in fix, "the fix must name the exact command"


def test_a_blank_password_locks_the_account_rather_than_inventing_one():
    for blank in ("", "   ", None):
        result = generate_kickstart_config(MINIMAL, blank)
        assert result.root_mode == ROOT_LOCKED, repr(blank)
        assert result.rootpw_line == "rootpw --lock"
        assert result.is_safe_to_serve
        assert "rootpw --lock" in result.config


def test_the_password_classifier_knows_a_hash_from_a_string():
    for hashed in ("$6$salt$rest", "$5$salt$rest", "$2b$12$abcdef",
                   "$y$j9T$abc$def", SHA512):
        assert classify_root_password(hashed) == ROOT_HASHED, hashed
    for plain in ("hunter2", "$notahash", "correct horse", "6$salt$rest"):
        assert classify_root_password(plain) == ROOT_PLAINTEXT, plain
    assert classify_root_password("") == ROOT_LOCKED


def test_exactly_one_rootpw_line_reaches_the_file():
    for secret in (SHA512, "hunter2", ""):
        config = generate_kickstart_config(STANDARD, secret).config
        rootpw = [line for line in lines_of(config) if line.startswith("rootpw")]
        assert len(rootpw) == 1, secret


def test_every_layout_warns_about_what_clearpart_destroys():
    for layout in LAYOUTS:
        result = generate_kickstart_config(layout, SHA512)
        codes = {f.code for f in result.findings}
        assert "KS-CLEARPART" in codes, layout.key
        assert "KS-DISK-SIZE" in codes, layout.key


def test_a_layout_can_be_named_by_object_key_or_title():
    by_object = generate_kickstart_config(STANDARD, SHA512).config
    by_key = generate_kickstart_config("standard", SHA512).config
    by_title = generate_kickstart_config("Standard LVM", SHA512).config
    assert by_object == by_key == by_title


def test_an_unknown_layout_raises_rather_than_falling_back():
    with pytest.raises(ValueError):
        generate_kickstart_config("raid0-everything", SHA512)


# ---------------------------------------------------------------------------
# iDRAC virtual media
# ---------------------------------------------------------------------------


def test_a_good_address_and_share_produce_the_full_sequence():
    plan = simulate_idrac_mount(GOOD_IP, GOOD_ISO)
    assert plan.status == STATUS_READY
    assert plan.share_type == SHARE_CIFS
    joined = "\n".join(plan.commands)
    assert f"racadm -r {GOOD_IP}" in joined
    assert f"remoteimage -c -l {GOOD_ISO}" in joined
    assert "remoteimage -s" in joined
    assert "iDRAC.ServerBoot.FirstBootDevice VCD-DVD" in joined
    assert "iDRAC.ServerBoot.BootOnce Enabled" in joined
    assert "serveraction powercycle" in joined
    assert "remoteimage -d" in "\n".join(plan.teardown)


def test_the_status_is_checked_before_and_after_the_attach():
    """Assuming the mount worked is how a reboot gets spent on nothing."""
    plan = simulate_idrac_mount(GOOD_IP, GOOD_ISO)
    runnable = [c for c in plan.commands if not c.startswith("#")]
    attach = next(i for i, c in enumerate(runnable) if "remoteimage -c" in c)
    statuses = [i for i, c in enumerate(runnable) if "remoteimage -s" in c]
    assert any(i < attach for i in statuses), "no check before attaching"
    assert any(i > attach for i in statuses), "no check after attaching"


def test_the_boot_is_set_once_so_the_next_boot_is_the_disk():
    runnable = [c for c in simulate_idrac_mount(GOOD_IP, GOOD_ISO).commands
                if not c.startswith("#")]
    boot_once = next(i for i, c in enumerate(runnable) if "BootOnce" in c)
    cycle = next(i for i, c in enumerate(runnable) if "powercycle" in c)
    assert boot_once < cycle, "the machine is cycled before the boot order is set"


def test_a_blocked_plan_emits_no_commands_and_no_stand_in():
    """A command line carrying a stand in is one paste from the wrong machine."""
    for ip, iso in (("not an ip", GOOD_ISO),
                    (GOOD_IP, "/home/me/rocky.iso"),
                    (GOOD_IP, ""),
                    ("", "")):
        plan = simulate_idrac_mount(ip, iso)
        assert plan.status == STATUS_BLOCKED, (ip, iso)
        assert plan.commands == ()
        assert plan.teardown == ()
        assert not any("MISSING" in c for c in plan.commands)


def test_a_local_path_is_refused_because_the_idrac_cannot_read_your_disk():
    plan = simulate_idrac_mount(GOOD_IP, "/home/me/rocky.iso")
    codes = {f.code for f in plan.findings}
    assert "DRAC-ISO-LOCAL" in codes
    assert plan.share_type == SHARE_UNKNOWN


def test_the_share_classifier_separates_the_three_kinds():
    assert classify_share("//nas01/isos/rocky.iso") == SHARE_CIFS
    assert classify_share("\\\\nas01\\isos\\rocky.iso") == SHARE_CIFS
    assert classify_share("nfs://nas01/export/rocky.iso") == SHARE_NFS
    assert classify_share("nas01:/export/rocky.iso") == SHARE_NFS
    assert classify_share("https://mirror.example/rocky.iso") == SHARE_HTTP
    assert classify_share("/home/me/rocky.iso") == SHARE_UNKNOWN
    assert classify_share("") == SHARE_UNKNOWN


def test_an_address_that_is_not_an_address_is_caught():
    for bad in ("not an ip", "10.20.30", "10.20.30.999", "10.20.30.011",
                "10.20.30.11.12", ""):
        plan = simulate_idrac_mount(bad, GOOD_ISO)
        assert plan.status == STATUS_BLOCKED, bad
        assert any(f.code == "DRAC-ADDR" for f in plan.findings), bad
    assert simulate_idrac_mount("192.168.1.50", GOOD_ISO).status == STATUS_READY


def test_a_routable_idrac_address_is_blocked_as_a_hardware_takeover_risk():
    plan = simulate_idrac_mount("8.8.8.8", GOOD_ISO)
    assert any(f.code == "DRAC-PUBLIC" and f.severity == SEVERITY_CRITICAL
               for f in plan.findings)
    assert plan.status == STATUS_BLOCKED
    for private in ("10.0.0.5", "192.168.10.10", "172.20.5.5"):
        assert not any(f.code == "DRAC-PUBLIC"
                       for f in simulate_idrac_mount(private, GOOD_ISO).findings)


def test_every_plan_warns_that_the_password_is_visible_on_the_host():
    for ip, iso in ((GOOD_IP, GOOD_ISO), ("8.8.8.8", GOOD_ISO)):
        plan = simulate_idrac_mount(ip, iso)
        assert any(f.code == "DRAC-CREDS" for f in plan.findings)


def test_the_network_warning_is_always_present_because_it_is_always_true():
    plan = simulate_idrac_mount(GOOD_IP, GOOD_ISO)
    assert any(f.code == "DRAC-NETWORK" for f in plan.findings)
    assert plan.status == STATUS_READY, (
        "a warning must not block a plan that is otherwise correct")


# ---------------------------------------------------------------------------
# Parallel deployments
# ---------------------------------------------------------------------------


def test_the_board_holds_every_server_exactly_once():
    for count in (1, 3, 12, 40):
        board = track_parallel_deployments(count)
        assert len(board.servers) == count
        assert len({s.hostname for s in board.servers}) == count
        assert len({s.idrac_ip for s in board.servers}) == count
        assert sum(board.by_phase.values()) == count


def test_the_three_buckets_add_up_to_the_wave():
    for count in (1, 5, 12, 24, 48):
        for minutes in (0, 9, 40, 120):
            board = track_parallel_deployments(count, minutes)
            assert board.online + board.in_progress + board.stalled == count, (
                count, minutes)


def test_running_in_parallel_beats_running_one_at_a_time():
    for count in (2, 8, 24):
        board = track_parallel_deployments(count)
        assert board.parallel_minutes < board.serial_minutes, count
        assert board.serial_minutes == count * sum(m for _n, m in PHASES)


def test_the_share_becomes_the_bottleneck_past_the_stated_wave_size():
    small = track_parallel_deployments(3)
    large = track_parallel_deployments(32)
    assert "servers themselves" in small.bottleneck
    assert "share" in large.bottleneck
    assert large.share_throughput_mbps < small.share_throughput_mbps


def test_more_machines_in_one_wave_makes_every_machine_slower():
    previous = 0
    for count in (4, 8, 16, 32):
        board = track_parallel_deployments(count)
        assert board.parallel_minutes > previous, count
        previous = board.parallel_minutes


def test_the_board_is_deterministic():
    """Same inputs, same board, or a test cannot assert on any of it."""
    first = track_parallel_deployments(16, 20)
    second = track_parallel_deployments(16, 20)
    assert [s.phase for s in first.servers] == [s.phase for s in second.servers]
    assert first.parallel_minutes == second.parallel_minutes
    assert first.stalled == second.stalled


def test_a_stuck_machine_is_named_and_never_counted_as_progressing():
    board = track_parallel_deployments(24, 9)
    stuck = [s for s in board.servers if not s.healthy]
    assert stuck, "the model should surface at least one stuck machine here"
    assert len(stuck) == board.stalled
    for server in stuck:
        assert server.minutes_remaining == 0
        assert "BootOnce" in server.note
        assert server.phase != "Online"


def test_every_percentage_stays_a_percentage():
    for count in (1, 10, 48):
        for minutes in (0, 5, 30, 120):
            for server in track_parallel_deployments(count, minutes).servers:
                assert 0 <= server.percent_complete <= 100
                assert (server.percent_complete == 100) == (server.phase == "Online")


def test_the_model_ships_its_assumptions_with_every_board():
    board = track_parallel_deployments(8)
    assert len(board.assumptions) >= 4
    assert any("not measured constants" in line for line in board.assumptions)


def test_an_impossible_wave_raises():
    with pytest.raises(ValueError):
        track_parallel_deployments(0)
    with pytest.raises(ValueError):
        track_parallel_deployments(-4)
    with pytest.raises(ValueError):
        track_parallel_deployments(513)
    with pytest.raises(ValueError):
        track_parallel_deployments(4, -1)


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (ROOT / "tools" / "idrac_rocky_kickstart_console" / "core.py").read_text()
    assert "import streamlit" not in source
    probe = (
        "import sys; sys.modules['streamlit'] = None; "
        "sys.path.insert(0, %r); "
        "import tools.idrac_rocky_kickstart_console.core as c; "
        "print(c.ENGINE_VERSION)" % str(ROOT)
    )
    done = subprocess.run([sys.executable, "-c", probe],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "1.0.0" in done.stdout


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [f.title + f.detail + f.fix
         for layout in LAYOUTS
         for secret in (SHA512, "hunter2", "")
         for f in generate_kickstart_config(layout, secret).findings]
        + [layout.purpose + layout.title for layout in LAYOUTS]
        + [f.title + f.detail + f.fix
           for ip, iso in ((GOOD_IP, GOOD_ISO), ("8.8.8.8", "/home/me/x.iso"))
           for f in simulate_idrac_mount(ip, iso).findings]
        + [board.headline + board.fix + board.bottleneck
           + " ".join(board.assumptions)
           + " ".join(s.note for s in board.servers)
           for board in (track_parallel_deployments(3),
                         track_parallel_deployments(24))]
    )
    assert "—" not in text
    assert "–" not in text


def test_the_tool_is_registered_with_a_unique_icon():
    from tools.registry import all_tools
    tools = all_tools()
    entry = next(t for t in tools if t.key == "idrac-rocky-kickstart-console")
    assert entry.title == "iDRAC Rocky Linux Provisioning Console"
    icons = [t.icon for t in tools]
    assert icons.count(entry.icon) == 1, "this icon is already used by another tool"
    assert len(entry.tagline) > 30
