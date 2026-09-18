"""iDRAC Rocky Linux Provisioning Console: the engine.

Racking twenty servers and installing them one at a time by hand is a week of
somebody's life. Doing it from iDRAC with a Kickstart file is an afternoon,
and it fails in three specific places that are all checkable before the first
boot rather than after the first wipe.

1. clearpart is a destroying command. Unscoped, it takes every disk the
   installer can see, which on a server with a data array attached means the
   array. Scoping it to named drives and adding ignoredisk is the difference
   between a rebuild and an incident.
2. A root password in a Kickstart file is a root password in a file that a
   thousand machines fetch over the network. The generator refuses to be
   quiet about that: a hash is accepted, a plaintext string is emitted with
   the exact command to replace it, and a locked account is offered as the
   better answer.
3. Twenty machines pulling one ISO from one share do not install in the time
   one machine takes. The share is the bottleneck and the arithmetic says so
   before the maintenance window does.

No Streamlit import lives here on purpose, so the same engine could sit behind
a real provisioning runner without a line changing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

SEVERITY_OK = "OK"
SEVERITY_WARN = "WARN"
SEVERITY_CRITICAL = "CRITICAL"

MIB_PER_GIB = 1024


# ---------------------------------------------------------------------------
# 1. Kickstart generation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LogicalVolume:
    mount: str
    name: str
    size_mib: int
    fstype: str = "xfs"
    grow: bool = False
    fsoptions: str = ""


@dataclass(frozen=True)
class PartitionLayout:
    key: str
    title: str
    purpose: str
    install_disk: str
    volume_group: str
    boot_mib: int
    efi_mib: int
    volumes: tuple[LogicalVolume, ...]

    @property
    def allocated_mib(self) -> int:
        """Every fixed size volume added up, plus boot and efi."""
        return (self.boot_mib + self.efi_mib
                + sum(v.size_mib for v in self.volumes if not v.grow))

    @property
    def has_growing_volume(self) -> bool:
        return any(v.grow for v in self.volumes)


MINIMAL = PartitionLayout(
    key="minimal",
    title="Minimal single disk",
    purpose="A worker node that holds no state worth separating.",
    install_disk="sda", volume_group="vg_system",
    boot_mib=1024, efi_mib=600,
    volumes=(
        LogicalVolume("swap", "lv_swap", 8192, fstype="swap"),
        LogicalVolume("/", "lv_root", 0, grow=True),
    ),
)

STANDARD = PartitionLayout(
    key="standard",
    title="Standard LVM",
    purpose="A general server where a full /var must not fill /.",
    install_disk="sda", volume_group="vg_system",
    boot_mib=1024, efi_mib=600,
    volumes=(
        LogicalVolume("swap", "lv_swap", 16384, fstype="swap"),
        LogicalVolume("/", "lv_root", 51200),
        LogicalVolume("/var", "lv_var", 30720),
        LogicalVolume("/home", "lv_home", 20480, grow=True),
    ),
)

CIS_HARDENED = PartitionLayout(
    key="cis",
    title="CIS hardened",
    purpose=("Separate mounts with nodev, nosuid and noexec where the "
             "benchmark asks for them, so a full audit log cannot take the "
             "machine down and a dropped binary in /tmp cannot be run."),
    install_disk="sda", volume_group="vg_system",
    boot_mib=1024, efi_mib=600,
    volumes=(
        LogicalVolume("swap", "lv_swap", 16384, fstype="swap"),
        LogicalVolume("/", "lv_root", 30720),
        LogicalVolume("/home", "lv_home", 10240, fsoptions="nodev"),
        LogicalVolume("/tmp", "lv_tmp", 5120, fsoptions="nodev,nosuid,noexec"),
        LogicalVolume("/var", "lv_var", 20480, fsoptions="nodev"),
        LogicalVolume("/var/tmp", "lv_vartmp", 5120,
                      fsoptions="nodev,nosuid,noexec"),
        LogicalVolume("/var/log", "lv_varlog", 15360, fsoptions="nodev,nosuid,noexec"),
        LogicalVolume("/var/log/audit", "lv_audit", 10240,
                      fsoptions="nodev,nosuid,noexec", grow=True),
    ),
)

DATABASE = PartitionLayout(
    key="database",
    title="Database node",
    purpose=("The data directory gets its own volume so it can be grown, "
             "snapshotted and filled without touching the operating system."),
    install_disk="sda", volume_group="vg_system",
    boot_mib=1024, efi_mib=600,
    volumes=(
        LogicalVolume("swap", "lv_swap", 32768, fstype="swap"),
        LogicalVolume("/", "lv_root", 30720),
        LogicalVolume("/var", "lv_var", 20480),
        LogicalVolume("/var/lib/pgsql", "lv_pgdata", 204800, grow=True),
    ),
)

LAYOUTS: tuple[PartitionLayout, ...] = (MINIMAL, STANDARD, CIS_HARDENED, DATABASE)
LAYOUT_BY_KEY = {layout.key: layout for layout in LAYOUTS}
LAYOUT_BY_TITLE = {layout.title: layout for layout in LAYOUTS}

# A crypt hash, which is what belongs in a Kickstart file. The prefixes are
# the ones Rocky's shadow stack accepts.
CRYPT_HASH = re.compile(r"^\$(1|2[aby]|5|6|y|gy|7)\$[^\s:]+$")

ROOT_LOCKED = "LOCKED"
ROOT_HASHED = "HASHED"
ROOT_PLAINTEXT = "PLAINTEXT"

HASH_COMMAND = "openssl passwd -6"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


@dataclass(frozen=True)
class KickstartConfig:
    layout: PartitionLayout
    root_mode: str
    rootpw_line: str
    config: str
    allocated_gib: float
    minimum_disk_gib: int
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        for level in (SEVERITY_CRITICAL, SEVERITY_WARN):
            if any(f.severity == level for f in self.findings):
                return level
        return SEVERITY_OK

    @property
    def is_safe_to_serve(self) -> bool:
        """False when the file carries a secret that a fetch would leak."""
        return self.root_mode != ROOT_PLAINTEXT


def classify_root_password(root_password: str) -> str:
    """A hash, a plaintext string, or nothing at all."""
    value = str(root_password or "").strip()
    if not value:
        return ROOT_LOCKED
    return ROOT_HASHED if CRYPT_HASH.match(value) else ROOT_PLAINTEXT


def generate_kickstart_config(partition_layout, root_password: str) -> KickstartConfig:
    """Build a Rocky Linux ks.cfg for one layout, and say what it costs.

    The layout can be passed as the object, its key or its title, because the
    caller on a page has a label and the caller in a runner has a key.

    The file that comes back is complete and installable. What it is not is
    silent: clearpart is scoped to a named drive rather than left to take
    every disk the installer can see, and a plaintext root password comes
    with the command that replaces it and a finding that says the file must
    not be served openly until it has been.
    """
    layout = _resolve_layout(partition_layout)
    mode = classify_root_password(root_password)
    secret = str(root_password or "").strip()

    findings: list[Finding] = []

    if mode == ROOT_HASHED:
        rootpw_line = f"rootpw --iscrypted {secret}"
        findings.append(Finding(
            code="KS-ROOT-OK", severity=SEVERITY_OK,
            title="The root password is supplied as a hash",
            detail=("The file carries a crypt hash rather than a password, "
                    "which is what belongs in a Kickstart that machines fetch "
                    "over the network."),
            fix=("Still restrict the file. A hash is not a secret you want "
                 "farmed, so serve it from a host the build VLAN can reach "
                 "and nothing else can.")))
    elif mode == ROOT_LOCKED:
        rootpw_line = "rootpw --lock"
        findings.append(Finding(
            code="KS-ROOT-LOCKED", severity=SEVERITY_OK,
            title="Root login is locked and access comes from keys",
            detail=("No password is set, so there is no password in the file "
                    "to leak and none to rotate later. Access arrives through "
                    "the authorized key written in the post section."),
            fix=("Confirm at least one key is installed before the reboot. A "
                 "locked root with no key is a server that needs a trip to "
                 "the rack.")))
    else:
        rootpw_line = f"rootpw --plaintext {secret}"
        findings.append(Finding(
            code="KS-ROOT-PLAINTEXT", severity=SEVERITY_CRITICAL,
            title="This file now contains a root password in clear text",
            detail=("Every machine that installs from this file fetches the "
                    "password with it, usually over plain HTTP, and it stays "
                    "in the installer log at /root/anaconda-ks.cfg on every "
                    "one of them afterwards."),
            fix=(f"Run {HASH_COMMAND} on your own machine, paste the hash "
                 f"that comes back into this generator instead of the "
                 f"password, and the file becomes safe to serve.")))

    findings.append(Finding(
        code="KS-CLEARPART", severity=SEVERITY_WARN,
        title=f"clearpart is scoped to {layout.install_disk} and nothing else",
        detail=(f"clearpart --all with no drives argument takes every disk "
                f"the installer can see, which on a server with an array "
                f"attached takes the array. This file names "
                f"{layout.install_disk} and adds ignoredisk so the installer "
                f"cannot reach anything else."),
        fix=(f"Confirm {layout.install_disk} is the install disk on this "
             f"model before the first boot. Device naming is not stable "
             f"across controllers, and the safe habit is to unplug or "
             f"unmap data volumes for the duration of the build.")))

    if not layout.has_growing_volume:
        findings.append(Finding(
            code="KS-NO-GROW", severity=SEVERITY_WARN,
            title="No volume claims the remaining space",
            detail="Anything left in the volume group stays unallocated.",
            fix="Mark one volume with grow, or plan to extend it by hand."))

    if layout.key == "cis":
        findings.append(Finding(
            code="KS-CIS-NOEXEC", severity=SEVERITY_WARN,
            title="noexec on /var/tmp breaks some package scriptlets",
            detail=("A few vendor installers stage an executable under "
                    "/var/tmp and run it, and they fail on a hardened mount "
                    "with a permission error that does not mention noexec."),
            fix=("Keep the option and remount for the one install that needs "
                 "it, rather than dropping it from the build permanently.")))

    minimum = _minimum_disk_gib(layout)
    findings.append(Finding(
        code="KS-DISK-SIZE", severity=SEVERITY_WARN,
        title=f"This layout needs a disk of at least {minimum} GiB",
        detail=(f"Fixed allocations come to "
                f"{layout.allocated_mib / MIB_PER_GIB:.1f} GiB before any "
                f"growing volume takes a byte."),
        fix=(f"Check the smallest disk in the batch. An install that runs out "
             f"of space stops at the partitioning step and waits at a prompt "
             f"nobody is watching.")))

    config = _render_kickstart(layout, rootpw_line)
    return KickstartConfig(
        layout=layout, root_mode=mode, rootpw_line=rootpw_line, config=config,
        allocated_gib=round(layout.allocated_mib / MIB_PER_GIB, 1),
        minimum_disk_gib=minimum, findings=tuple(findings),
    )


def _resolve_layout(value) -> PartitionLayout:
    if isinstance(value, PartitionLayout):
        return value
    name = str(value or "").strip()
    if name in LAYOUT_BY_KEY:
        return LAYOUT_BY_KEY[name]
    if name in LAYOUT_BY_TITLE:
        return LAYOUT_BY_TITLE[name]
    raise ValueError(f"unknown partition layout: {value!r}")


def _minimum_disk_gib(layout: PartitionLayout) -> int:
    """Fixed allocations, plus headroom for whatever grows, rounded up."""
    fixed = layout.allocated_mib / MIB_PER_GIB
    headroom = 10 if layout.has_growing_volume else 0
    return int(fixed + headroom) + (1 if (fixed + headroom) % 1 else 0)


def _render_kickstart(layout: PartitionLayout, rootpw_line: str) -> str:
    disk = layout.install_disk
    lines = [
        "#version=RHEL9",
        f"# Rocky Linux Kickstart, layout: {layout.title}",
        f"# {layout.purpose}",
        "",
        "text",
        "cdrom",
        "lang en_US.UTF-8",
        "keyboard --xlayouts='us'",
        "timezone Etc/UTC --utc",
        "network --bootproto=dhcp --device=link --activate --onboot=on",
        "firstboot --disable",
        "selinux --enforcing",
        "firewall --enabled --service=ssh",
        "services --enabled=sshd,chronyd,auditd",
        rootpw_line,
        "",
        "# Only this disk is touched. Everything else is hidden from the",
        "# installer, so an attached array cannot be cleared by accident.",
        f"ignoredisk --only-use={disk}",
        "zerombr",
        f"clearpart --all --initlabel --drives={disk}",
        "",
        f"part /boot/efi --fstype=efi --size={layout.efi_mib} --ondisk={disk} "
        '--fsoptions="umask=0077,shortname=winnt"',
        f"part /boot --fstype=xfs --size={layout.boot_mib} --ondisk={disk}",
        f"part pv.01 --fstype=lvmpv --size=1 --grow --ondisk={disk}",
        f"volgroup {layout.volume_group} pv.01",
    ]
    for volume in layout.volumes:
        parts = [f"logvol {volume.mount}",
                 f"--vgname={layout.volume_group}",
                 f"--name={volume.name}",
                 f"--fstype={volume.fstype}"]
        parts.append("--size=1 --grow" if volume.grow
                     else f"--size={volume.size_mib}")
        if volume.fsoptions:
            parts.append(f'--fsoptions="{volume.fsoptions}"')
        lines.append(" ".join(parts))

    lines += [
        "",
        f"bootloader --location=mbr --boot-drive={disk} "
        '--append="crashkernel=auto"',
        "",
        "%packages",
        "@^minimal-environment",
        "openssh-server",
        "chrony",
        "audit",
        "tuned",
        "-iwl*firmware",
        "%end",
        "",
        "%addon com_redhat_kdump --disable",
        "%end",
        "",
        "%post --log=/root/ks-post.log",
        "# The installer writes a copy of this file to /root/anaconda-ks.cfg",
        "# on the built machine. Lock it down whatever it contains.",
        "chmod 600 /root/anaconda-ks.cfg",
        "systemctl enable --now chronyd",
        "%end",
        "",
        "reboot --eject",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 2. iDRAC virtual media
# ---------------------------------------------------------------------------

STATUS_READY = "READY TO MOUNT"
STATUS_BLOCKED = "BLOCKED"

SHARE_NFS = "NFS"
SHARE_CIFS = "CIFS"
SHARE_HTTP = "HTTP"
SHARE_UNKNOWN = "UNRECOGNISED"

PRIVATE_RANGES = ("10.", "192.168.", "172.16.", "172.17.", "172.18.",
                  "172.19.", "172.20.", "172.21.", "172.22.", "172.23.",
                  "172.24.", "172.25.", "172.26.", "172.27.", "172.28.",
                  "172.29.", "172.30.", "172.31.")


@dataclass(frozen=True)
class MountPlan:
    server_ip: str
    iso_path: str
    share_type: str
    status: str
    commands: tuple[str, ...]
    teardown: tuple[str, ...]
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        for level in (SEVERITY_CRITICAL, SEVERITY_WARN):
            if any(f.severity == level for f in self.findings):
                return level
        return SEVERITY_OK


def _is_ipv4(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit() or not 0 <= int(part) <= 255:
            return False
        if len(part) > 1 and part.startswith("0"):
            return False
    return True


def classify_share(iso_path: str) -> str:
    path = str(iso_path or "").strip()
    lowered = path.lower()
    if lowered.startswith("nfs://") or (
            ":" in path and path.split(":", 1)[1].startswith("/")
            and not lowered.startswith(("http", "//", "\\\\"))):
        return SHARE_NFS
    if path.startswith("//") or path.startswith("\\\\") or lowered.startswith("cifs://"):
        return SHARE_CIFS
    if lowered.startswith(("http://", "https://")):
        return SHARE_HTTP
    return SHARE_UNKNOWN


def simulate_idrac_mount(server_ip: str, iso_path: str) -> MountPlan:
    """The racadm sequence that attaches an ISO and boots the server from it.

    The commands are the real ones. What this adds is the checking that gets
    skipped at three in the morning: that the address is an address, that the
    path is a share rather than a folder on the laptop typing the command,
    and that the share has to be reachable from the iDRAC network interface
    rather than from the workstation, which is the failure that looks like a
    broken ISO and is not.
    """
    ip = str(server_ip or "").strip()
    path = str(iso_path or "").strip()
    findings: list[Finding] = []

    if not _is_ipv4(ip):
        findings.append(Finding(
            code="DRAC-ADDR", severity=SEVERITY_CRITICAL,
            title=f"{ip or 'the address field'} is not an IPv4 address",
            detail=("racadm needs the address of the iDRAC itself, which is "
                    "a different address from the host operating system on "
                    "the same machine."),
            fix="Take the iDRAC address from the DHCP reservation or the LCD panel."))

    share = classify_share(path)
    if not path:
        findings.append(Finding(
            code="DRAC-ISO-EMPTY", severity=SEVERITY_CRITICAL,
            title="No image path was given",
            detail="remoteimage has nothing to attach.",
            fix="Point it at the ISO on the share the iDRAC can reach."))
    elif share == SHARE_UNKNOWN:
        findings.append(Finding(
            code="DRAC-ISO-LOCAL", severity=SEVERITY_CRITICAL,
            title="That path is not a network share",
            detail=("remoteimage mounts from NFS or CIFS. A path like this "
                    "is read as a local path, and the iDRAC has no access to "
                    "the filesystem of the machine running racadm."),
            fix=("Publish the ISO on a share and give it as //host/share/"
                 "file.iso for CIFS or host:/export/file.iso for NFS.")))
    elif share == SHARE_HTTP:
        findings.append(Finding(
            code="DRAC-ISO-HTTP", severity=SEVERITY_WARN,
            title="An HTTP source is not accepted by every iDRAC generation",
            detail=("remoteimage is documented against NFS and CIFS. HTTP "
                    "support varies by generation and firmware."),
            fix=("Use NFS or CIFS unless this fleet is known to take HTTP, "
                 "or check the state with remoteimage -s before rebooting.")))

    if not path.lower().endswith(".iso") and path:
        findings.append(Finding(
            code="DRAC-ISO-EXT", severity=SEVERITY_WARN,
            title="The image does not end in .iso",
            detail="Virtual media expects an ISO image for a CD or DVD device.",
            fix="Point at the installer ISO rather than at a folder or an image of another kind."))

    findings.append(Finding(
        code="DRAC-NETWORK", severity=SEVERITY_WARN,
        title="The share must be reachable from the iDRAC, not from your desk",
        detail=("The iDRAC opens the connection itself over the management "
                "network. A share that mounts perfectly from the workstation "
                "and not from the management VLAN fails here as a timeout, "
                "which reads like a corrupt image and is not one."),
        fix=("Prove it with remoteimage -s after connecting, before spending "
             "a reboot on it.")))

    findings.append(Finding(
        code="DRAC-CREDS", severity=SEVERITY_CRITICAL,
        title="A password on the command line is visible to the whole host",
        detail=("Anything passed to -p appears in the shell history of the "
                "machine running it and in the process list while it runs, "
                "so any other user on that host can read it."),
        fix=("Use a provisioning account scoped to virtual media and power, "
             "rotate it when the build window closes, and never paste one of "
             "these lines into a ticket or a chat.")))

    if _is_ipv4(ip) and not ip.startswith(PRIVATE_RANGES):
        findings.append(Finding(
            code="DRAC-PUBLIC", severity=SEVERITY_CRITICAL,
            title=f"{ip} is not in a private range",
            detail=("An iDRAC answering on a routable address is a lights out "
                    "controller exposed to the internet, which is a full "
                    "hardware takeover rather than a server compromise."),
            fix="Put management on its own VLAN and reach it through a jump host."))

    blocked = any(f.severity == SEVERITY_CRITICAL and f.code != "DRAC-CREDS"
                  for f in findings)
    status = STATUS_BLOCKED if blocked else STATUS_READY

    # A blocked plan hands over nothing. Emitting a command line with a
    # stand in where the address or the path should be is how a stand in
    # gets pasted into a terminal, and the first four commands would run
    # against the wrong machine before the fifth one failed.
    if status == STATUS_BLOCKED:
        commands: tuple[str, ...] = ()
        teardown: tuple[str, ...] = ()
    else:
        base = f"racadm -r {ip} -u provisioning -p <password>"
        commands = (
            "# 1. Check what the virtual media is doing before touching it",
            f"{base} remoteimage -s",
            "# 2. Attach the installer ISO from the share",
            f"{base} remoteimage -c -l {path}",
            "# 3. Confirm it actually connected, do not assume it",
            f"{base} remoteimage -s",
            "# 4. Boot from virtual media once only, so the next boot is the disk",
            f"{base} set iDRAC.ServerBoot.FirstBootDevice VCD-DVD",
            f"{base} set iDRAC.ServerBoot.BootOnce Enabled",
            "# 5. Power cycle into the installer",
            f"{base} serveraction powercycle",
        )
        teardown = (
            "# After the install reboots, release the image so the next build can use it",
            f"{base} remoteimage -d",
            f"{base} remoteimage -s",
        )

    return MountPlan(
        server_ip=ip, iso_path=path, share_type=share, status=status,
        commands=commands, teardown=teardown, findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# 3. Parallel deployment tracking
# ---------------------------------------------------------------------------

PHASES: tuple[tuple[str, int], ...] = (
    ("Mounting virtual media", 2),
    ("Booting the installer", 4),
    ("Partitioning and formatting", 3),
    ("Installing packages", 12),
    ("Running the post section", 3),
    ("Rebooting into the new system", 2),
    ("Online", 0),
)

# The assumptions this tracker forecasts with, named so they can be replaced
# with a measurement rather than argued about.
SHARE_LINK_MBPS = 1000
ISO_SIZE_MIB = 2400
SERVERS_BEFORE_CONTENTION = 4


@dataclass(frozen=True)
class DeploymentStatus:
    hostname: str
    idrac_ip: str
    phase: str
    phase_index: int
    percent_complete: int
    minutes_elapsed: int
    minutes_remaining: int
    healthy: bool
    note: str


@dataclass(frozen=True)
class DeploymentBoard:
    server_count: int
    servers: tuple[DeploymentStatus, ...]
    by_phase: dict
    online: int
    in_progress: int
    stalled: int
    serial_minutes: int
    parallel_minutes: int
    share_throughput_mbps: float
    bottleneck: str
    headline: str
    fix: str
    assumptions: tuple[str, ...] = field(default_factory=tuple)


def track_parallel_deployments(server_count: int, minutes_elapsed: int = 9
                               ) -> DeploymentBoard:
    """A board of bare metal installs running at once, and what gates them.

    Deterministic on purpose. Each server is staggered by its position in the
    rack because that is how a runner fires them, so the same inputs always
    give the same board and a test can assert on it.

    The part worth reading is the arithmetic underneath. Installs do not run
    independently: they pull the same image from the same share, and past a
    handful of machines the share is the constraint rather than the servers.
    """
    count = int(server_count)
    if count <= 0:
        raise ValueError("a deployment needs at least one server")
    if count > 512:
        raise ValueError("a single share does not front 512 concurrent installs")
    elapsed_input = int(minutes_elapsed)
    if elapsed_input < 0:
        raise ValueError("elapsed time cannot be negative")

    total_minutes = sum(minutes for _name, minutes in PHASES)
    concurrent = max(1, count)
    throughput = SHARE_LINK_MBPS / concurrent
    contention = max(1.0, concurrent / SERVERS_BEFORE_CONTENTION)
    parallel_minutes = int(round(total_minutes * contention)) + (count - 1) // 8

    servers: list[DeploymentStatus] = []
    by_phase: dict = {name: 0 for name, _ in PHASES}
    stalled = 0
    for index in range(count):
        # A runner fires the rack in order, so machine n starts n staggers in.
        stagger = index % 4
        elapsed = max(0, elapsed_input - stagger)
        phase_index, phase_name, consumed = _phase_at(elapsed, contention)
        # One machine in twelve sits on a firmware prompt rather than booting.
        # Modelled, not observed, and the board says which one it is.
        is_stalled = (index + 1) % 12 == 0 and phase_index <= 1
        if is_stalled:
            phase_index, phase_name = 1, PHASES[1][0]
            stalled += 1
        scaled_total = total_minutes * contention
        percent = 100 if phase_name == "Online" else min(
            99, int(consumed / scaled_total * 100))
        remaining = 0 if phase_name == "Online" else max(
            1, int(round(scaled_total - consumed)))
        by_phase[phase_name] += 1
        servers.append(DeploymentStatus(
            hostname=f"rocky-node-{index + 1:02d}",
            idrac_ip=f"10.20.30.{index + 11}",
            phase=phase_name, phase_index=phase_index,
            percent_complete=percent, minutes_elapsed=elapsed,
            minutes_remaining=0 if is_stalled else remaining,
            healthy=not is_stalled,
            note=("Sitting at the boot menu with no keypress. Virtual media "
                  "attached but the boot order did not take, so this one "
                  "needs BootOnce set again and another power cycle."
                  if is_stalled else
                  f"{phase_name} at {percent} percent.")))

    online = by_phase.get("Online", 0)
    in_progress = count - online - stalled

    if concurrent > SERVERS_BEFORE_CONTENTION:
        bottleneck = (f"The share, at {throughput:.0f} Mbps per server across "
                      f"{count} of them")
        fix = (f"Stage the ISO on more than one share, or fire the rack in "
               f"waves of {SERVERS_BEFORE_CONTENTION}. Past that the servers "
               f"are waiting on the network rather than on themselves, and "
               f"adding machines to the window makes every machine slower.")
    else:
        bottleneck = "The servers themselves, which is where it should be"
        fix = ("This many at once is inside what one share carries. Keep the "
               "wave size here and the window stays predictable.")

    headline = (f"{count} servers running at once finish in about "
                f"{parallel_minutes} minutes against "
                f"{total_minutes * count} minutes one at a time, with "
                f"{online} online, {in_progress} building and {stalled} stuck")

    return DeploymentBoard(
        server_count=count, servers=tuple(servers), by_phase=by_phase,
        online=online, in_progress=in_progress, stalled=stalled,
        serial_minutes=total_minutes * count,
        parallel_minutes=parallel_minutes,
        share_throughput_mbps=round(throughput, 1), bottleneck=bottleneck,
        headline=headline, fix=fix,
        assumptions=(
            f"A clean install runs {total_minutes} minutes on its own.",
            f"The image is about {ISO_SIZE_MIB} MiB served over a "
            f"{SHARE_LINK_MBPS} Mbps link.",
            f"Past {SERVERS_BEFORE_CONTENTION} concurrent installs the share "
            f"is the constraint and every machine slows together.",
            "One machine in twelve is modelled as stuck at the boot menu, "
            "which is the common failure when BootOnce does not take.",
            "These four figures are this model's assumptions, not measured "
            "constants. Replace them with a timed run of your own fleet.",
        ),
    )


def _phase_at(elapsed: int, contention: float) -> tuple[int, str, float]:
    """Which phase a machine is in after so many minutes, and time consumed."""
    consumed = 0.0
    for index, (name, minutes) in enumerate(PHASES):
        scaled = minutes * contention
        if minutes == 0:
            return index, name, consumed
        if elapsed < consumed + scaled:
            return index, name, float(elapsed)
        consumed += scaled
    return len(PHASES) - 1, PHASES[-1][0], consumed
