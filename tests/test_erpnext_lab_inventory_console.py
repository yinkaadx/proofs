"""Engine tests for the ERPNext Laboratory and Equipment Console.

Written for pytest. Deterministic: nothing in the engine reads the clock or a
random source, and every date is a plain day count.

Run: pytest tests/test_erpnext_lab_inventory_console.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.erpnext_lab_inventory_console.core import (  # noqa: E402
    DT_EQUIPMENT,
    DT_ITEM,
    DT_MAINTENANCE,
    DT_SDS,
    DT_SOP,
    ENGINE_VERSION,
    FT_LINK,
    FT_TABLE,
    LOCAL_SERVICE,
    PERMS,
    PUBLIC_HOSTNAME,
    ROLE_ADMIN,
    ROLE_FACILITY,
    ROLE_LAB_TECH,
    ROLES,
    STATE_DEGRADED,
    STATE_DOWN,
    STATE_HEALTHY,
    backup_rows,
    expiry_report,
    get_linked_doctype_schema,
    get_rbac_and_backup_config,
    lab_summary,
    permission_matrix,
    relation_rows,
    schema_rows,
    simulate_cloudflare_tunnel_status,
    tunnel_config_yaml,
)


def check_named(status, name):
    return next(c for c in status.checks if c.name == name)


def profile_for(role, doctype):
    return next((p for p in get_rbac_and_backup_config()["profiles"]
                 if p.role == role and p.doctype == doctype), None)


def doctype_named(name):
    return next(d for d in get_linked_doctype_schema()["doctypes"]
                if d.name == name)


# ---------------------------------------------------------------------------
# Tunnel status
# ---------------------------------------------------------------------------

def test_all_four_parts_up_is_healthy_and_reachable():
    status = simulate_cloudflare_tunnel_status()
    assert status.state == STATE_HEALTHY
    assert status.reachable
    assert all(c.healthy for c in status.checks)


def test_a_tunnel_never_requires_an_inbound_port():
    """The whole point. The connector dials out, so nothing is forwarded."""
    for args in ((True, True, True, True), (False, False, False, False)):
        assert simulate_cloudflare_tunnel_status(*args).inbound_ports_required == 0


def test_a_missing_dns_record_fails_before_cloudflare_is_involved():
    status = simulate_cloudflare_tunnel_status(dns_record_present=False)
    check = check_named(status, "DNS routing")
    assert check.state == STATE_DOWN
    assert "not a tunnel fault" in check.detail
    assert "tunnel route dns" in check.fix


def test_a_stopped_connector_is_reported_as_the_lab_end():
    status = simulate_cloudflare_tunnel_status(connector_running=False)
    check = check_named(status, "Connector")
    assert check.state == STATE_DOWN
    assert "edge is fine and the lab end is not" in check.detail
    assert "cloudflared" in check.fix


def test_an_unmatched_ingress_rule_explains_the_missing_frappe_logs():
    """A 404 from the connector never reaches ERPNext, which is why looking
    in the Frappe logs finds nothing."""
    status = simulate_cloudflare_tunnel_status(ingress_rule_matches=False)
    check = check_named(status, "Ingress rule")
    assert check.state == STATE_DEGRADED
    assert "not from ERPNext" in check.detail
    assert "Frappe logs" in check.detail


def test_a_dead_origin_is_the_one_failure_that_means_the_tunnel_works():
    status = simulate_cloudflare_tunnel_status(origin_listening=False)
    check = check_named(status, "Origin service")
    assert check.state == STATE_DOWN
    assert "502" in check.detail
    assert "working perfectly" in check.detail


def test_the_four_failures_are_distinguishable_from_each_other():
    """If they all said the same thing the tool would be useless."""
    details = set()
    for kwargs in ({"dns_record_present": False}, {"connector_running": False},
                   {"ingress_rule_matches": False},
                   {"origin_listening": False}):
        status = simulate_cloudflare_tunnel_status(**kwargs)
        failing = [c for c in status.checks if not c.healthy]
        assert len(failing) == 1, kwargs
        details.add(failing[0].detail)
    assert len(details) == 4


def test_tls_is_healthy_even_when_everything_else_is_broken():
    """It terminates at the edge, so the origin's state cannot affect it."""
    status = simulate_cloudflare_tunnel_status(False, False, False, False)
    assert check_named(status, "TLS").state == STATE_HEALTHY
    assert "no open port" in check_named(status, "TLS").detail


def test_any_single_down_check_makes_the_whole_tunnel_down():
    for kwargs in ({"dns_record_present": False},
                   {"connector_running": False},
                   {"origin_listening": False}):
        status = simulate_cloudflare_tunnel_status(**kwargs)
        assert status.state == STATE_DOWN, kwargs
        assert not status.reachable


def test_only_a_degraded_ingress_leaves_the_tunnel_degraded_not_down():
    status = simulate_cloudflare_tunnel_status(ingress_rule_matches=False)
    assert status.state == STATE_DEGRADED
    assert not status.reachable


def test_a_healthy_check_carries_no_fix_and_a_broken_one_does():
    status = simulate_cloudflare_tunnel_status(connector_running=False)
    for check in status.checks:
        if check.healthy:
            assert check.fix == "", check.name
        else:
            assert check.fix, check.name


def test_the_config_names_the_hostname_the_service_and_the_catch_all():
    config = tunnel_config_yaml()
    assert PUBLIC_HOSTNAME in config
    assert LOCAL_SERVICE in config
    assert "http_status:404" in config
    assert config.index("hostname") < config.index("http_status:404"), \
        "the catch all must come last or it swallows every hostname"


def test_the_status_rows_are_arrow_safe():
    for row in simulate_cloudflare_tunnel_status(origin_listening=False).rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# The DocType schema
# ---------------------------------------------------------------------------

def test_the_schema_holds_the_four_named_doctypes():
    names = {d.name for d in get_linked_doctype_schema()["doctypes"]}
    assert names == {DT_EQUIPMENT, DT_MAINTENANCE, DT_SDS, DT_SOP}


def test_equipment_is_the_root_everything_hangs_from():
    assert get_linked_doctype_schema()["root"] == DT_EQUIPMENT


def test_the_maintenance_log_is_a_child_table_of_equipment():
    """A log entry means nothing without its equipment."""
    equipment = doctype_named(DT_EQUIPMENT)
    tables = [f for f in equipment.tables if f.options == DT_MAINTENANCE]
    assert tables, "maintenance must be a child table on equipment"
    assert doctype_named(DT_MAINTENANCE).istable is True


def test_the_sop_is_a_link_not_a_table():
    """One procedure governs many instruments, so retiring one instrument
    must not delete the procedure."""
    equipment = doctype_named(DT_EQUIPMENT)
    sop_fields = [f for f in equipment.fields if f.options == DT_SOP]
    assert sop_fields
    assert sop_fields[0].fieldtype == FT_LINK
    assert doctype_named(DT_SOP).istable is False


def test_the_safety_sheet_is_its_own_doctype_not_an_attachment():
    """The decision the whole schema rests on."""
    sds = doctype_named(DT_SDS)
    assert sds.istable is False
    fieldtypes = {f.fieldtype for f in sds.fields}
    assert "Attach" not in fieldtypes


def test_one_safety_sheet_covers_many_items():
    """So the PDF is stored once rather than per pack size."""
    sds = doctype_named(DT_SDS)
    item_tables = [f for f in sds.tables if f.options == DT_ITEM]
    assert item_tables, "the sheet must link out to many items"


def test_the_safety_sheet_carries_a_revision_date_so_expiry_is_queryable():
    """An attachment cannot answer which sheets are out of date."""
    sds = doctype_named(DT_SDS)
    revision = [f for f in sds.fields if f.fieldname == "revision_day"]
    assert revision and revision[0].fieldtype == "Date"
    assert revision[0].reqd is True


def test_equipment_links_to_item_so_stock_and_assets_share_one_master():
    equipment = doctype_named(DT_EQUIPMENT)
    assert any(f.options == DT_ITEM and f.fieldtype == FT_LINK
               for f in equipment.fields)


def test_calibration_due_is_a_date_not_a_note():
    """So a report can find what is overdue without opening a record."""
    equipment = doctype_named(DT_EQUIPMENT)
    due = [f for f in equipment.fields if f.fieldname == "calibration_due_day"]
    assert due and due[0].fieldtype == "Date" and due[0].reqd


def test_every_relation_names_a_real_doctype_or_a_frappe_builtin():
    known = {DT_EQUIPMENT, DT_MAINTENANCE, DT_SDS, DT_SOP, DT_ITEM,
             "Supplier", "User"}
    for source, kind, target, why in get_linked_doctype_schema()["relations"]:
        assert source in known and target in known, (source, target)
        assert kind in (FT_LINK, FT_TABLE)
        assert why, "a relation without a reason is a guess"


def test_every_link_and_table_field_names_its_target():
    for doctype in get_linked_doctype_schema()["doctypes"]:
        for entry in doctype.fields:
            if entry.is_relation:
                assert entry.options, f"{doctype.name}.{entry.fieldname}"


def test_the_schema_tables_are_arrow_safe():
    for row in schema_rows() + relation_rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# Safety sheet expiry
# ---------------------------------------------------------------------------

def test_a_sheet_past_the_review_period_is_expired():
    report = expiry_report(0, 1200, 1095)
    assert report["expired"] is True
    assert report["remaining_days"] == -105


def test_a_fresh_sheet_is_neither_expired_nor_due_soon():
    report = expiry_report(0, 30, 1095)
    assert not report["expired"]
    assert not report["due_soon"]


def test_a_sheet_inside_ninety_days_is_flagged_due_soon():
    report = expiry_report(0, 1050, 1095)
    assert report["due_soon"] is True
    assert not report["expired"]


def test_the_review_period_is_a_parameter_not_a_hardcoded_rule():
    """The obligation differs by jurisdiction, so the tool must not state one."""
    lenient = expiry_report(0, 1200, 1825)
    strict = expiry_report(0, 1200, 365)
    assert not lenient["expired"]
    assert strict["expired"]


def test_the_boundary_day_is_not_yet_expired():
    assert not expiry_report(0, 1095, 1095)["expired"]
    assert expiry_report(0, 1096, 1095)["expired"]


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

def test_a_technician_cannot_edit_equipment():
    """The whole point of the role design. A technician who can move a
    calibration due date can make an overdue instrument look compliant."""
    profile = profile_for(ROLE_LAB_TECH, DT_EQUIPMENT)
    assert profile.permissions == ("read",)
    assert not profile.allows("write")
    assert not profile.allows("delete")


def test_a_technician_writes_the_maintenance_log_which_is_the_evidence():
    profile = profile_for(ROLE_LAB_TECH, DT_MAINTENANCE)
    assert profile.allows("create") and profile.allows("write")
    assert profile.if_owner is True, (
        "without if_owner one technician can rewrite another's signed work")


def test_a_technician_can_always_read_a_safety_sheet_and_never_change_one():
    profile = profile_for(ROLE_LAB_TECH, DT_SDS)
    assert profile.allows("read")
    assert not profile.allows("write")


def test_no_role_below_admin_can_delete_anything():
    for profile in get_rbac_and_backup_config()["profiles"]:
        if profile.role != ROLE_ADMIN:
            assert not profile.allows("delete"), (profile.role, profile.doctype)


def test_the_facility_manager_owns_the_equipment_register_but_cannot_delete():
    profile = profile_for(ROLE_FACILITY, DT_EQUIPMENT)
    assert profile.allows("write") and profile.allows("create")
    assert not profile.allows("delete"), (
        "a retired instrument must keep its history")


def test_drafting_and_approving_an_sop_are_separated_by_permlevel():
    """The one action that must not sit with whoever drafted it."""
    drafter = profile_for(ROLE_FACILITY, DT_SOP)
    approver = profile_for(ROLE_ADMIN, DT_SOP)
    assert drafter.permlevel == approver.permlevel == 1
    assert not drafter.allows("submit")
    assert approver.allows("submit")


def test_the_admin_has_every_permission():
    assert profile_for(ROLE_ADMIN, DT_EQUIPMENT).permissions == PERMS


def test_the_matrix_covers_every_doctype_and_marks_gaps_explicitly():
    rows = permission_matrix()
    assert len(rows) == 4
    for row in rows:
        assert set(ROLES).issubset(row)
        assert all(isinstance(v, str) for v in row.values()), row
    assert any("no access" in " ".join(row.values()) for row in rows), (
        "a gap should read as a decision rather than an empty cell")


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

def test_there_is_a_database_job_and_a_with_files_job():
    """A database only backup restores a lab with no documents in it."""
    commands = " ".join(j.command for j in get_rbac_and_backup_config()["backups"])
    assert "bench" in commands
    assert "--with-files" in commands


def test_a_restore_rehearsal_exists():
    """A backup nobody has restored is a hope."""
    jobs = get_rbac_and_backup_config()["backups"]
    assert any("restore" in j.command for j in jobs)


def test_the_pre_migration_snapshot_is_taken_before_the_change():
    job = next(j for j in get_rbac_and_backup_config()["backups"]
               if "migration" in j.name.lower())
    assert "before" in job.schedule
    assert "rollback" in job.note


def test_every_job_names_a_retention_and_a_command():
    for job in get_rbac_and_backup_config()["backups"]:
        assert job.command and job.retention and job.schedule and job.note


def test_the_scheduler_warning_is_present_because_it_fails_silently():
    note = get_rbac_and_backup_config()["scheduler_note"]
    assert "enable-scheduler" in note
    assert "silently" in note


def test_retention_is_configured_rather_than_left_to_fill_the_disk():
    assert get_rbac_and_backup_config()["retention_key"] == "backup_limit"


def test_the_backup_rows_are_arrow_safe():
    for row in backup_rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# Summary and house rules
# ---------------------------------------------------------------------------

def test_the_summary_agrees_with_the_parts_it_counts():
    summary = lab_summary()
    assert summary["doctypes"] == len(get_linked_doctype_schema()["doctypes"])
    assert summary["relations"] == len(get_linked_doctype_schema()["relations"])
    assert summary["roles"] == len(ROLES)
    assert summary["backup_jobs"] == len(get_rbac_and_backup_config()["backups"])
    assert summary["inbound_ports"] == 0


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (Path(__file__).resolve().parents[1] / "tools"
              / "erpnext_lab_inventory_console" / "core.py").read_text()
    assert "streamlit" not in source


def test_results_are_deterministic():
    assert simulate_cloudflare_tunnel_status().state == \
        simulate_cloudflare_tunnel_status().state
    assert expiry_report(0, 500) == expiry_report(0, 500)


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        [c.detail + c.fix for c in
         simulate_cloudflare_tunnel_status(False, False, False, False).checks]
        + [d.purpose for d in get_linked_doctype_schema()["doctypes"]]
        + [f.note for d in get_linked_doctype_schema()["doctypes"]
           for f in d.fields]
        + [p.note for p in get_rbac_and_backup_config()["profiles"]]
        + [j.note for j in get_rbac_and_backup_config()["backups"]]
        + [get_rbac_and_backup_config()["scheduler_note"]])
    assert "—" not in text
    assert "–" not in text
