"""ERPNext Laboratory & Equipment Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine could run as a configuration check in CI.

Everything is evaluated live from the controls, so no card on screen can
describe a tunnel state that was changed two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.erpnext_lab_inventory_console.core import (
    DT_EQUIPMENT,
    ENGINE_VERSION,
    LOCAL_SERVICE,
    PUBLIC_HOSTNAME,
    ROLES,
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


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>ERPNext Laboratory &amp; Equipment Console</h1>
  <p>A lab running ERPNext behind a Cloudflare Tunnel has three things to get
  right. The tunnel has to be genuinely serving rather than merely resolving,
  and its four failure modes look nothing alike. The schema has to link an
  instrument to the documents that govern its use, which is where attaching a
  safety sheet to an item quietly stops working. And the backups and roles have
  to exist before anybody needs them.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    summary = lab_summary()

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Tunnel**: switch off each part and read the different "
            "failure.\n"
            "2. **Schema**: see why the safety sheet is a DocType, not an "
            "attachment.\n"
            "3. **Access**: the technician row is the one worth reading.\n"
            "4. **Backups**: four jobs, and one of them is a rehearsal."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. No tunnel is contacted, no "
            "site is backed up and no ERPNext instance exists."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_tunnel, tab_schema, tab_access, tab_backup = st.tabs(
        ["Tunnel Ingress", "DocType Schema", "Roles", "Backups"])

    # -----------------------------------------------------------------
    # Tunnel
    # -----------------------------------------------------------------
    with tab_tunnel:
        st.markdown("#### Four things must be true, and they fail differently")
        st.caption(
            "That difference is the diagnosis. A missing DNS record fails "
            "before Cloudflare is involved. A stopped connector gives "
            "Cloudflare's own error page. An ingress rule that does not match "
            "gives a 404 from the connector, which is why nothing appears in "
            "the Frappe logs. And a dead origin gives a 502, the only one of "
            "the four that means the tunnel is working perfectly."
        )

        c1, c2, c3, c4 = st.columns(4)
        dns = c1.toggle("DNS record", value=True, key="erp_dns")
        connector = c2.toggle("Connector running", value=True, key="erp_conn")
        ingress = c3.toggle("Ingress matches", value=True, key="erp_ingress")
        origin = c4.toggle("Origin listening", value=True, key="erp_origin")

        status = simulate_cloudflare_tunnel_status(connector, dns, ingress,
                                                   origin)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {status.tone}"><div class="n">{esc(status.state)}</div>
    <div class="l">Tunnel state</div></div>
  <div class="app-kpi {'ok' if status.reachable else 'crit'}">
    <div class="n">{'Yes' if status.reachable else 'No'}</div>
    <div class="l">Reachable from the internet</div></div>
  <div class="app-kpi ok">
    <div class="n">{status.inbound_ports_required}</div>
    <div class="l">Inbound ports opened</div></div>
  <div class="app-kpi">
    <div class="n">{len([c for c in status.checks if c.healthy])}</div>
    <div class="l">of {len(status.checks)} checks passing</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for check in status.checks:
            st.markdown(
                f"""
<div class="app-card {check.tone}">
  <h4><span class="app-tag {check.tone}">{esc(check.state)}</span>
  {esc(check.name)}</h4>
  <p>{esc(check.detail)}</p>
  {'<div class="app-ev">' + esc(check.fix) + '</div>' if check.fix else ''}
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### Every check")
        st.dataframe(status.rows(), width="stretch", hide_index=True)

        st.markdown("##### The config the healthy state describes")
        st.code(tunnel_config_yaml(), language="yaml")
        st.caption(
            f"The connector dials out to Cloudflare and keeps that connection "
            f"open, so traffic to {PUBLIC_HOSTNAME} arrives down a socket the "
            f"lab opened. Nothing is forwarded at the firewall, and a "
            f"misconfiguration leaves {LOCAL_SERVICE} unreachable rather than "
            f"exposed, which is the opposite of a port forward."
        )

    # -----------------------------------------------------------------
    # Schema
    # -----------------------------------------------------------------
    with tab_schema:
        st.markdown("#### Why the safety sheet is a DocType, not an attachment")
        st.caption(
            "An Attach field on the Item works on the first day and fails on "
            "every day after. One sheet covers a chemical grade rather than a "
            "pack size, so an attachment duplicates the same PDF everywhere. "
            "And an attachment cannot be queried, so nobody can answer the "
            "only question that matters: which chemicals in this building "
            "have a sheet that is out of date."
        )

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{summary['doctypes']}</div>
    <div class="l">DocTypes</div></div>
  <div class="app-kpi"><div class="n">{summary['relations']}</div>
    <div class="l">Relations</div></div>
  <div class="app-kpi ok"><div class="n">{esc(DT_EQUIPMENT)}</div>
    <div class="l">Root record</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for doctype in get_linked_doctype_schema()["doctypes"]:
            tone = "warn" if doctype.istable else "ok"
            kind = "child table" if doctype.istable else "standalone"
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(kind)}</span>
  {esc(doctype.name)}</h4>
  <p>{esc(doctype.purpose)}</p>
  <div class="app-ev">{len(doctype.links)} link field(s),
  {len(doctype.tables)} child table(s)</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### The relations, and why each is that shape")
        st.dataframe(relation_rows(), width="stretch", hide_index=True)

        st.markdown("##### Every field")
        st.dataframe(schema_rows(), width="stretch", hide_index=True)

        st.markdown("##### The query an attachment cannot answer")
        c1, c2 = st.columns(2)
        age = c1.slider("Days since the sheet was revised", min_value=0,
                        max_value=2000, value=1200, key="erp_age")
        period = c2.slider("Internal review period, days", min_value=180,
                           max_value=1825, value=1095, step=30,
                           key="erp_period")
        report = expiry_report(0, age, period)
        tone = "crit" if report["expired"] else (
            "warn" if report["due_soon"] else "ok")
        state = ("Expired" if report["expired"]
                 else "Due soon" if report["due_soon"] else "Current")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(state)}</span>
  {report['remaining_days']} day(s) remaining</h4>
  <p>The review period is a parameter and not a constant, because the
  obligation differs by jurisdiction and by substance. This is an internal
  default that can be overridden, not a statement of the law.</p>
</div>
""",
            unsafe_allow_html=True,
        )

    # -----------------------------------------------------------------
    # Roles
    # -----------------------------------------------------------------
    with tab_access:
        st.markdown("#### The technician row is the one worth reading")
        st.caption(
            "The tempting configuration gives a technician write on equipment "
            "so they can update a status, and that quietly lets them move a "
            "calibration due date, which is the field the whole compliance "
            "story rests on. So they write the maintenance log, which is "
            "evidence, and the due date moves as a consequence of a logged "
            "calibration rather than by hand."
        )

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{summary['roles']}</div>
    <div class="l">Roles</div></div>
  <div class="app-kpi"><div class="n">{summary['profiles']}</div>
    <div class="l">Permission rules</div></div>
  <div class="app-kpi ok"><div class="n">read</div>
    <div class="l">Technician on equipment</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### The matrix")
        st.dataframe(permission_matrix(), width="stretch", hide_index=True)
        st.caption(
            "A blank in this table is a decision, not an omission. "
            + ", ".join(ROLES) + " are the only roles defined."
        )

        st.markdown("##### Every rule, with its reason")
        for profile in get_rbac_and_backup_config()["profiles"]:
            owner = " if_owner" if profile.if_owner else ""
            st.markdown(
                f"""
<div class="app-card">
  <h4><span class="app-tag">{esc(profile.role)}</span>
  {esc(profile.doctype)}</h4>
  <p>{esc(profile.note)}</p>
  <div class="app-ev">{esc(', '.join(profile.permissions))} at permlevel
  {profile.permlevel}{esc(owner)}</div>
</div>
""",
                unsafe_allow_html=True,
            )

    # -----------------------------------------------------------------
    # Backups
    # -----------------------------------------------------------------
    with tab_backup:
        st.markdown("#### Four jobs, and one of them is a rehearsal")
        config = get_rbac_and_backup_config()

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{summary['backup_jobs']}</div>
    <div class="l">Scheduled jobs</div></div>
  <div class="app-kpi ok"><div class="n">{summary['offsite_jobs']}</div>
    <div class="l">Copied offsite</div></div>
  <div class="app-kpi warn"><div class="n">1</div>
    <div class="l">Restore rehearsal</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        for job in config["backups"]:
            st.markdown(
                f"""
<div class="app-card {job.tone}">
  <h4><span class="app-tag {job.tone}">{esc(job.schedule)}</span>
  {esc(job.name)}</h4>
  <p>{esc(job.note)}</p>
  <div class="app-ev">{esc(job.retention)}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### The schedule")
        st.dataframe(backup_rows(), width="stretch", hide_index=True)

        st.warning(config["scheduler_note"])
        st.caption(
            f"Retention is set with the {config['retention_key']} key, so the "
            f"archive does not grow until the disk fills and every job starts "
            f"failing at once."
        )

    st.markdown(
        f"""
<div class="app-foot">
ERPNext Laboratory &amp; Equipment Console, engine version {ENGINE_VERSION}. A
simulator: no tunnel is contacted, no DNS is resolved, no site is backed up and
no ERPNext instance exists. Every figure is computed from the controls on
screen, and nothing here reads the clock or a random source.
</div>
""",
        unsafe_allow_html=True,
    )
