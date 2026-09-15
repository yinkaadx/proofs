"""ERPNext laboratory and equipment engine.

Three things have to hold for a lab running ERPNext behind a Cloudflare Tunnel:
the tunnel has to be genuinely up rather than merely resolving, the schema has
to link equipment to the documents that govern its use, and the backups and
roles have to be configured before anyone needs them.

Pure logic, no Streamlit import, so this is unit testable on its own.
Deterministic: nothing reads the clock or a random source, and every date is a
plain day count rather than a wall clock reading, so a test and a run agree.

THE SCHEMA DECISION THIS ENCODES

The obvious way to attach a safety data sheet to a chemical is an Attach field
on the Item. It works on the first day and fails on every day after, for two
reasons. One sheet covers a chemical grade rather than a single item, so an
attachment duplicates the same PDF across every pack size. And an attachment
cannot be queried, so nobody can answer the only question that matters, which
is which chemicals in this building have a sheet that is out of date.

So the sheet is its own DocType with a revision date, linked from the item, and
the expiry question becomes a report instead of a cupboard search.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Part one: the Cloudflare Tunnel
# ---------------------------------------------------------------------------

LOCAL_SERVICE = "http://localhost:8000"
PUBLIC_HOSTNAME = "lab.example-research.org"
TUNNEL_UUID = "7c2f9a41-0d3b-4e88-9a15-6b0f2d4c8e73"

STATE_HEALTHY = "Healthy"
STATE_DEGRADED = "Degraded"
STATE_DOWN = "Down"

TONE = {STATE_HEALTHY: "ok", STATE_DEGRADED: "warn", STATE_DOWN: "crit"}


@dataclass(frozen=True)
class TunnelCheck:
    name: str
    state: str
    detail: str
    fix: str = ""

    @property
    def tone(self) -> str:
        return TONE.get(self.state, "warn")

    @property
    def healthy(self) -> bool:
        return self.state == STATE_HEALTHY


@dataclass
class TunnelStatus:
    connector_running: bool
    dns_record_present: bool
    ingress_rule_matches: bool
    origin_listening: bool
    checks: list = field(default_factory=list)

    @property
    def state(self) -> str:
        if any(c.state == STATE_DOWN for c in self.checks):
            return STATE_DOWN
        if any(c.state == STATE_DEGRADED for c in self.checks):
            return STATE_DEGRADED
        return STATE_HEALTHY

    @property
    def tone(self) -> str:
        return TONE.get(self.state, "warn")

    @property
    def reachable(self) -> bool:
        """Whether a browser on the internet would actually get ERPNext."""
        return all(c.healthy for c in self.checks)

    @property
    def inbound_ports_required(self) -> int:
        """Zero, always. This is the whole point of a tunnel.

        The connector dials out to Cloudflare and keeps that connection open,
        so traffic arrives down a socket the lab opened. Nothing is forwarded
        at the firewall and nothing is exposed if the tunnel is misconfigured:
        the failure mode is unreachable rather than open to the world, which
        is the opposite of a port forward.
        """
        return 0

    def rows(self) -> list:
        return [
            {"Check": c.name, "State": c.state, "Detail": c.detail,
             "Fix": c.fix or "none needed"}
            for c in self.checks
        ]


def simulate_cloudflare_tunnel_status(connector_running: bool = True,
                                      dns_record_present: bool = True,
                                      ingress_rule_matches: bool = True,
                                      origin_listening: bool = True
                                      ) -> TunnelStatus:
    """Judge the four things that must all be true for the tunnel to serve.

    They fail differently and that difference is the diagnosis. A missing DNS
    record gives a browser error before Cloudflare is involved. A connector
    that is down gives Cloudflare's own error page, which means the edge is
    fine and the lab end is not. An ingress rule that does not match the
    hostname gives a 404 from the connector rather than from ERPNext. And an
    origin that is not listening gives a 502, which is the only one of the four
    that means the tunnel itself is working perfectly.
    """
    status = TunnelStatus(connector_running, dns_record_present,
                          ingress_rule_matches, origin_listening)

    status.checks.append(TunnelCheck(
        "DNS routing", STATE_HEALTHY if dns_record_present else STATE_DOWN,
        (f"{PUBLIC_HOSTNAME} resolves to the tunnel through a proxied CNAME "
         f"at {TUNNEL_UUID}.cfargotunnel.com."
         if dns_record_present else
         f"{PUBLIC_HOSTNAME} has no record, so the browser fails before "
         f"Cloudflare is ever asked. This is not a tunnel fault."),
        "" if dns_record_present else
        f"cloudflared tunnel route dns {TUNNEL_UUID} {PUBLIC_HOSTNAME}"))

    status.checks.append(TunnelCheck(
        "Connector", STATE_HEALTHY if connector_running else STATE_DOWN,
        ("cloudflared is running and holding outbound connections to four "
         "edge locations."
         if connector_running else
         "cloudflared is not running, so Cloudflare serves its own error "
         "page. The edge is fine and the lab end is not."),
        "" if connector_running else
        "systemctl status cloudflared, then systemctl restart cloudflared"))

    status.checks.append(TunnelCheck(
        "Ingress rule",
        STATE_HEALTHY if ingress_rule_matches else STATE_DEGRADED,
        (f"The hostname rule for {PUBLIC_HOSTNAME} routes to "
         f"{LOCAL_SERVICE}, with a catch all http_status:404 last."
         if ingress_rule_matches else
         "No ingress rule matches this hostname, so the catch all answers "
         "404. The 404 comes from the connector, not from ERPNext, which is "
         "why nothing appears in the Frappe logs."),
        "" if ingress_rule_matches else
        "Add the hostname to the ingress list above the catch all rule, then "
        "cloudflared tunnel ingress validate"))

    status.checks.append(TunnelCheck(
        "Origin service", STATE_HEALTHY if origin_listening else STATE_DOWN,
        (f"ERPNext answers on {LOCAL_SERVICE} and the connector reaches it."
         if origin_listening else
         f"Nothing is listening on {LOCAL_SERVICE}, so the connector returns "
         f"502. This is the one failure that means the tunnel itself is "
         f"working perfectly."),
        "" if origin_listening else
        "bench start, or supervisorctl status frappe-bench-web"))

    status.checks.append(TunnelCheck(
        "TLS", STATE_HEALTHY,
        ("Terminated at the Cloudflare edge with a managed certificate. The "
         "hop from the edge to the connector is the tunnel's own mutually "
         "authenticated connection, so the origin needs no certificate and "
         "no open port."),
        ""))
    return status


def tunnel_config_yaml() -> str:
    """The config the healthy state above describes."""
    return (f"tunnel: {TUNNEL_UUID}\n"
            f"credentials-file: /etc/cloudflared/{TUNNEL_UUID}.json\n"
            f"\n"
            f"ingress:\n"
            f"  - hostname: {PUBLIC_HOSTNAME}\n"
            f"    service: {LOCAL_SERVICE}\n"
            f"    originRequest:\n"
            f"      httpHostHeader: {PUBLIC_HOSTNAME}\n"
            f"  # The catch all is mandatory and must be last. Without it\n"
            f"  # cloudflared refuses to start rather than guessing.\n"
            f"  - service: http_status:404\n")


# ---------------------------------------------------------------------------
# Part two: the DocType schema
# ---------------------------------------------------------------------------

DT_EQUIPMENT = "Lab Equipment"
DT_MAINTENANCE = "Equipment Maintenance Log"
DT_SDS = "Chemical Safety Data Sheet"
DT_SOP = "Laboratory SOP"
DT_ITEM = "Item"

# Frappe fieldtypes used here. A Link stores the name of one target document.
# A Table stores child rows that belong to the parent and are deleted with it.
FT_LINK = "Link"
FT_TABLE = "Table"
FT_DATE = "Date"
FT_SELECT = "Select"
FT_DATA = "Data"
FT_CHECK = "Check"


@dataclass(frozen=True)
class SchemaField:
    fieldname: str
    fieldtype: str
    options: str
    reqd: bool
    note: str

    @property
    def is_relation(self) -> bool:
        return self.fieldtype in (FT_LINK, FT_TABLE)


@dataclass(frozen=True)
class DocType:
    name: str
    istable: bool
    purpose: str
    fields: tuple

    @property
    def links(self) -> tuple:
        return tuple(f for f in self.fields if f.fieldtype == FT_LINK)

    @property
    def tables(self) -> tuple:
        return tuple(f for f in self.fields if f.fieldtype == FT_TABLE)


def get_linked_doctype_schema() -> dict:
    """The relational model, with the reason each relation is the shape it is.

    A Link rather than an Attach for the safety sheet, because one sheet covers
    a chemical grade rather than a pack size, and because an attachment cannot
    answer which sheets are out of date. A Table for the maintenance log,
    because a log entry has no meaning without its equipment and should be
    removed with it. A Link for the SOP, because one procedure governs many
    instruments and must not be deleted when one of them is retired.
    """
    equipment = DocType(
        DT_EQUIPMENT, False,
        "One physical instrument, the record everything else hangs from.",
        (
            SchemaField("equipment_id", FT_DATA, "", True,
                        "The asset tag physically on the instrument."),
            SchemaField("item", FT_LINK, DT_ITEM, True,
                        "Links to stock so consumables and the instrument "
                        "share one item master."),
            SchemaField("status", FT_SELECT,
                        "In Service\nOut of Calibration\nUnder Maintenance\n"
                        "Retired", True,
                        "Out of Calibration is a status rather than a note, "
                        "so it can block a booking."),
            SchemaField("calibration_due_day", FT_DATE, "", True,
                        "Held as a date so a report can find what is overdue "
                        "without anybody opening a record."),
            SchemaField("governing_sop", FT_LINK, DT_SOP, True,
                        "One SOP governs many instruments, so a Link and not "
                        "a Table. Retiring an instrument must not delete the "
                        "procedure."),
            SchemaField("maintenance_log", FT_TABLE, DT_MAINTENANCE, False,
                        "A log entry means nothing without its equipment, so "
                        "it is a child table and dies with the parent."),
        ))

    maintenance = DocType(
        DT_MAINTENANCE, True,
        "One service event. A child of the equipment it belongs to.",
        (
            SchemaField("performed_on_day", FT_DATE, "", True,
                        "When the work happened."),
            SchemaField("technician", FT_LINK, "User", True,
                        "Who signed it off, from the user list rather than a "
                        "free text name, so it survives a leaver."),
            SchemaField("action", FT_SELECT,
                        "Calibration\nPreventive\nRepair\nDecommission", True,
                        "Calibration is the one that moves the due date."),
            SchemaField("notes", FT_DATA, "", False, "What was done."),
        ))

    sds = DocType(
        DT_SDS, False,
        "One safety data sheet for one chemical grade, not one pack size.",
        (
            SchemaField("chemical_name", FT_DATA, "", True,
                        "The substance the sheet covers."),
            SchemaField("supplier", FT_LINK, "Supplier", True,
                        "Sheets are supplier specific and differ between "
                        "them for the same substance."),
            SchemaField("revision_day", FT_DATE, "", True,
                        "The date on the sheet itself, which is what decides "
                        "whether it is current."),
            SchemaField("hazard_class", FT_SELECT,
                        "Flammable\nCorrosive\nToxic\nOxidiser\nInert", True,
                        "Drives storage separation rules."),
            SchemaField("linked_items", FT_TABLE, DT_ITEM, False,
                        "Every pack size this one sheet covers, so the PDF is "
                        "stored once rather than per item."),
        ))

    sop = DocType(
        DT_SOP, False,
        "One written procedure. Governs many instruments and many people.",
        (
            SchemaField("title", FT_DATA, "", True, "What the procedure is."),
            SchemaField("version", FT_DATA, "", True,
                        "Referenced by training records, so it is explicit."),
            SchemaField("review_due_day", FT_DATE, "", True,
                        "An unreviewed SOP is an audit finding on its own."),
            SchemaField("requires_training", FT_CHECK, "", False,
                        "Gates who may be assigned work under it."),
        ))

    return {
        "doctypes": (equipment, maintenance, sds, sop),
        "root": DT_EQUIPMENT,
        "relations": (
            (DT_EQUIPMENT, FT_LINK, DT_ITEM,
             "Equipment is an Item, so stock and assets share one master."),
            (DT_EQUIPMENT, FT_LINK, DT_SOP,
             "Many instruments to one procedure."),
            (DT_EQUIPMENT, FT_TABLE, DT_MAINTENANCE,
             "One instrument to many log rows, owned by the parent."),
            (DT_SDS, FT_TABLE, DT_ITEM,
             "One sheet to many pack sizes, so the PDF is stored once."),
            (DT_SDS, FT_LINK, "Supplier",
             "Sheets differ by supplier for the same substance."),
        ),
    }


def schema_rows() -> list:
    rows = []
    for doctype in get_linked_doctype_schema()["doctypes"]:
        for entry in doctype.fields:
            rows.append({
                "DocType": doctype.name,
                "Field": entry.fieldname,
                "Type": entry.fieldtype,
                "Targets": entry.options.replace("\n", ", ") or "n/a",
                "Required": "yes" if entry.reqd else "no",
            })
    return rows


def relation_rows() -> list:
    return [
        {"From": source, "Via": kind, "To": target, "Why": why}
        for source, kind, target, why in get_linked_doctype_schema()["relations"]
    ]


def expiry_report(revision_day: int, today_day: int,
                  review_period_days: int = 1095) -> dict:
    """Whether a safety sheet is still current, as a query rather than a search.

    The review period is a parameter and not a constant, because the obligation
    differs by jurisdiction and by substance. Three years is a common internal
    default rather than a universal rule, so it is stated as a default and can
    be overridden rather than presented as the law.
    """
    age = today_day - revision_day
    remaining = review_period_days - age
    return {
        "age_days": age,
        "remaining_days": remaining,
        "expired": remaining < 0,
        "due_soon": 0 <= remaining <= 90,
        "review_period_days": review_period_days,
    }


# ---------------------------------------------------------------------------
# Part three: backups and roles
# ---------------------------------------------------------------------------

ROLE_LAB_TECH = "Lab Technician"
ROLE_FACILITY = "Facility Manager"
ROLE_ADMIN = "System Manager"

ROLES: tuple = (ROLE_LAB_TECH, ROLE_FACILITY, ROLE_ADMIN)

# Frappe permission flags, named as the Role Permission Manager names them.
PERMS: tuple = ("read", "write", "create", "delete", "submit", "cancel",
                "amend")


@dataclass(frozen=True)
class RoleProfile:
    role: str
    doctype: str
    permissions: tuple
    permlevel: int
    if_owner: bool
    note: str

    def allows(self, action: str) -> bool:
        return action in self.permissions


@dataclass(frozen=True)
class BackupJob:
    name: str
    schedule: str
    command: str
    retention: str
    offsite: bool
    note: str

    @property
    def tone(self) -> str:
        return "ok" if self.offsite else "warn"


def get_rbac_and_backup_config() -> dict:
    """Roles and backups, both stated as configuration rather than intention.

    The role that matters is the technician. The tempting configuration gives
    them write on equipment so they can update a status, and that quietly lets
    them move a calibration due date, which is the one field the whole
    compliance story rests on. So they write the maintenance log, which is
    evidence, and the due date moves as a consequence of a logged calibration
    rather than by hand.
    """
    profiles = (
        RoleProfile(ROLE_LAB_TECH, DT_EQUIPMENT, ("read",), 0, False,
                    "Read only. A technician who can edit the calibration due "
                    "date can make an overdue instrument look compliant."),
        RoleProfile(ROLE_LAB_TECH, DT_MAINTENANCE,
                    ("read", "write", "create"), 0, True,
                    "Creates and edits their own log entries. if_owner stops "
                    "one technician rewriting another's signed work."),
        RoleProfile(ROLE_LAB_TECH, DT_SDS, ("read",), 0, False,
                    "Must be able to read a safety sheet at any hour. Never "
                    "needs to change one."),
        RoleProfile(ROLE_LAB_TECH, DT_SOP, ("read",), 0, False,
                    "Reads the procedure they work under."),
        RoleProfile(ROLE_FACILITY, DT_EQUIPMENT,
                    ("read", "write", "create"), 0, False,
                    "Owns the equipment register, including calibration "
                    "dates. Cannot delete, so a retired instrument keeps its "
                    "history."),
        RoleProfile(ROLE_FACILITY, DT_MAINTENANCE,
                    ("read", "write", "create", "submit"), 0, False,
                    "Signs off maintenance. Submit is what makes an entry "
                    "immutable evidence."),
        RoleProfile(ROLE_FACILITY, DT_SDS,
                    ("read", "write", "create"), 0, False,
                    "Keeps safety sheets current, which is the job the "
                    "expiry report exists to prompt."),
        RoleProfile(ROLE_FACILITY, DT_SOP, ("read", "write", "create"), 1,
                    False,
                    "Drafts procedures at permlevel 1, so the approved "
                    "version field stays out of reach."),
        RoleProfile(ROLE_ADMIN, DT_EQUIPMENT, PERMS, 0, False,
                    "Full control including delete, which is why it is not "
                    "given to the people doing daily work."),
        RoleProfile(ROLE_ADMIN, DT_SOP, PERMS, 1, False,
                    "Approves an SOP version, the one action that must not "
                    "sit with the person who drafted it."),
    )

    backups = (
        BackupJob(
            "Nightly database", "0 2 * * *",
            "bench --site lab.example-research.org backup",
            "14 nightly copies", True,
            "Database only, so it is fast enough to finish before the lab "
            "opens. Files are handled separately because they dominate size "
            "and change rarely."),
        BackupJob(
            "Weekly with files", "0 3 * * 0",
            "bench --site lab.example-research.org backup --with-files",
            "8 weekly copies", True,
            "Includes public and private files, which is where the safety "
            "sheets and calibration certificates actually live. A database "
            "only backup restores a lab with no documents in it."),
        BackupJob(
            "Pre migration snapshot", "before every bench update",
            "bench --site lab.example-research.org backup --with-files",
            "kept until the next clean update", False,
            "Taken before the change rather than after, because the point of "
            "this one is to be the rollback."),
        BackupJob(
            "Restore rehearsal", "0 4 1 * *",
            "bench --site restore-test.local restore <latest>",
            "overwritten monthly", False,
            "A backup nobody has restored is a hope. This one proves the "
            "archive opens, on a site that is safe to break."),
    )

    return {
        "profiles": profiles,
        "backups": backups,
        "retention_key": "backup_limit",
        "scheduler_note": (
            "Frappe's own scheduler must be enabled or none of these run. "
            "bench --site <site> enable-scheduler, and check it with "
            "bench doctor, because a disabled scheduler fails silently and "
            "the first sign is an empty backup directory."),
    }


def permission_matrix() -> list:
    """Every role against every DocType, so a gap is visible as a blank."""
    config = get_rbac_and_backup_config()
    doctypes = (DT_EQUIPMENT, DT_MAINTENANCE, DT_SDS, DT_SOP)
    rows = []
    for doctype in doctypes:
        row = {"DocType": doctype}
        for role in ROLES:
            found = [p for p in config["profiles"]
                     if p.role == role and p.doctype == doctype]
            row[role] = (", ".join(found[0].permissions) if found
                         else "no access")
        rows.append(row)
    return rows


def backup_rows() -> list:
    return [
        {"Job": job.name, "Schedule": job.schedule, "Command": job.command,
         "Retention": job.retention,
         "Offsite": "yes" if job.offsite else "no"}
        for job in get_rbac_and_backup_config()["backups"]
    ]


def lab_summary() -> dict:
    config = get_rbac_and_backup_config()
    schema = get_linked_doctype_schema()
    return {
        "doctypes": len(schema["doctypes"]),
        "relations": len(schema["relations"]),
        "roles": len(ROLES),
        "profiles": len(config["profiles"]),
        "backup_jobs": len(config["backups"]),
        "offsite_jobs": len([j for j in config["backups"] if j.offsite]),
        "inbound_ports": simulate_cloudflare_tunnel_status().inbound_ports_required,
    }
