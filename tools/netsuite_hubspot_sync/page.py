"""NetSuite HubSpot Idempotent Sync Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine can sit behind the real webhook handler.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.netsuite_hubspot_sync.core import (
    ACCEPTED,
    AUTO_LINK_CONFIDENCE,
    ENGINE_VERSION,
    FEED_STATUS_LABEL,
    MATCHED,
    PENDING,
    REJECTED,
    REVIEW_CONFIDENCE,
    ROLLED_BACK,
    SAMPLE_ACCOUNTS,
    SAMPLE_DEAL,
    STATUS_LABEL,
    STRATEGY_LABEL,
    VARIANCE,
    Deal,
    IdempotencyLedger,
    LineItem,
    canonical_payload,
    feed_totals,
    ledger_rows,
    match_account,
    merge_recommendation,
    payload_hash,
    reconcile_feeds,
    retention_rate,
    retention_report,
    sharepoint_audit,
    sync_kpis,
)

STATE = "nhs_state"

DECISION_TONE = {"link": "ok", "review": "warn", "create": "info"}
DECISION_LABEL = {
    "link": "Link to existing customer",
    "review": "Hold for human review",
    "create": "Create a new customer",
}
FEED_TONE = {MATCHED: "pass", VARIANCE: "fail", PENDING: "skip"}


def _rows_to_line_items(rows) -> tuple[LineItem, ...]:
    """Accept whatever the data editor returns without importing pandas."""
    if hasattr(rows, "to_dict"):
        rows = rows.to_dict("records")
    items = []
    for row in rows or []:
        sku = str(row.get("SKU", "") or "").strip()
        if not sku:
            continue
        try:
            quantity = int(row.get("Quantity", 0) or 0)
            unit_price = float(row.get("Unit price", 0) or 0)
        except (TypeError, ValueError):
            continue
        items.append(LineItem(sku, str(row.get("Description", "") or "").strip(),
                              quantity, unit_price))
    return tuple(items)


def _line_item_rows(deal: Deal) -> list[dict]:
    return [
        {"SKU": item.sku, "Description": item.description,
         "Quantity": item.quantity, "Unit price": item.unit_price}
        for item in deal.line_items
    ]


def _state() -> dict:
    if STATE not in st.session_state:
        ledger = IdempotencyLedger()
        deal = SAMPLE_DEAL
        outcome = match_account(deal, SAMPLE_ACCOUNTS)
        entry = ledger.submit(deal, "evt-0001")
        feeds = reconcile_feeds(deal, entry)
        st.session_state[STATE] = {
            "ledger": ledger, "deal": deal, "outcome": outcome,
            "entry": entry, "feeds": feeds, "events": 1,
        }
    return st.session_state[STATE]


def _run_sync(deal: Deal, event_id: str) -> None:
    state = _state()
    state["deal"] = deal
    state["outcome"] = match_account(deal, SAMPLE_ACCOUNTS)
    state["entry"] = state["ledger"].submit(deal, event_id)
    state["feeds"] = reconcile_feeds(deal, state["entry"])


def render() -> None:
    inject()
    state = _state()

    st.markdown(
        """
<div class="app-hero">
  <h1>NetSuite HubSpot Idempotent Sync Console</h1>
  <p>Trigger a Closed Won deal and watch it resolve to a NetSuite customer,
  hash into the idempotency ledger, reconcile across the finance systems and
  land in the SharePoint audit trail. Replay the same event as often as you
  like: it will be rejected rather than posted twice.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Deal Simulator**: set the deal and fire it.\n"
            "2. **Account Matching**: see which customer it resolved to and why.\n"
            "3. **Idempotency Ledger**: replay it and watch it be refused.\n"
            "4. **Feeds and Audit**: reconcile and read the audit trail."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. Matching, hashing and "
            "reconciliation run in this session against a sample NetSuite "
            "account book."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_exec, tab_deal, tab_match, tab_ledger, tab_audit = st.tabs(
        ["Executive Summary", "Deal Simulator", "Account Matching",
         "Idempotency Ledger", "Feeds and Audit"]
    )

    # -----------------------------------------------------------------
    # Executive summary
    # -----------------------------------------------------------------
    with tab_exec:
        deal, outcome, ledger = state["deal"], state["outcome"], state["ledger"]
        kpis = sync_kpis(deal, ledger, outcome)
        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{kpis.retention_rate:.0%}</div>
    <div class="l">Data retention</div></div>
  <div class="app-kpi ok"><div class="n">{kpis.duplicate_records_created}</div>
    <div class="l">Duplicate records</div></div>
  <div class="app-kpi"><div class="n">{kpis.match_ms:.2f} ms</div>
    <div class="l">Match latency</div></div>
  <div class="app-kpi warn"><div class="n">{kpis.duplicates_prevented}</div>
    <div class="l">Duplicates prevented</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        if kpis.retention_rate == 1.0:
            st.success(
                "Every mapped field survived the hop. No data was dropped between "
                "HubSpot and NetSuite."
            )
        else:
            missing = [row[0] for row in retention_report(deal) if not row[3]]
            st.warning(
                f"{kpis.retention_rate:.0%} retention. These fields arrived empty "
                f"and would post an incomplete record: {', '.join(missing)}."
            )

        if kpis.sub_second:
            st.caption(
                f"Account matching resolved in {kpis.match_ms:.2f} ms against "
                f"{len(SAMPLE_ACCOUNTS)} customers, comfortably inside the sub "
                "second budget a webhook handler has before HubSpot retries."
            )

        st.markdown("##### Field by field retention")
        st.dataframe(
            [
                {"HubSpot field": source, "NetSuite target": target,
                 "Value carried": value, "Retained": "Yes" if kept else "No"}
                for source, target, value, kept in retention_report(deal)
            ],
            width="stretch", hide_index=True,
        )

    # -----------------------------------------------------------------
    # Deal simulator
    # -----------------------------------------------------------------
    with tab_deal:
        st.markdown("#### Closed Won trigger")
        st.caption(
            "This is the payload HubSpot would post to the webhook the moment a "
            "deal stage changes to Closed Won."
        )
        current = state["deal"]

        c1, c2, c3 = st.columns([2, 1, 1])
        deal_id = c1.text_input("Deal ID", value=current.deal_id)
        amount = c2.number_input("Deal amount", min_value=0.0, step=250.0,
                                 value=float(current.amount), format="%.2f")
        currency = c3.selectbox("Currency", ["GBP", "USD", "EUR", "AUD"],
                                index=["GBP", "USD", "EUR", "AUD"].index(current.currency)
                                if current.currency in ("GBP", "USD", "EUR", "AUD") else 0)

        deal_name = st.text_input("Deal name", value=current.deal_name)

        c4, c5 = st.columns(2)
        company_name = c4.text_input("Company name", value=current.company_name)
        customer_domain = c5.text_input("Customer domain", value=current.customer_domain,
                                        help="A full URL, a bare domain or an email "
                                             "address all resolve to the same host.")
        c6, c7 = st.columns(2)
        tax_id = c6.text_input("Tax ID", value=current.tax_id,
                               help="Spaces, dots and dashes are ignored when matching.")
        owner = c7.text_input("Deal owner", value=current.owner)

        st.markdown("##### Line items")
        edited = st.data_editor(
            _line_item_rows(current),
            num_rows="dynamic",
            width="stretch",
            hide_index=True,
            key="nhs_lines",
        )
        line_items = _rows_to_line_items(edited)
        line_total = round(sum(i.total for i in line_items), 2)

        if abs(line_total - float(amount)) >= 0.01:
            st.warning(
                f"The lines sum to {line_total:,.2f} but the deal amount is "
                f"{float(amount):,.2f}. NetSuite will post the header and leave "
                f"the difference of {abs(line_total - float(amount)):,.2f} "
                f"{currency} unallocated, so this is worth fixing before release."
            )
        else:
            st.caption(f"Lines sum to {line_total:,.2f} {currency}, which ties to the header.")

        candidate = Deal(
            deal_id=deal_id.strip() or current.deal_id,
            deal_name=deal_name, amount=float(amount), currency=currency,
            company_name=company_name, customer_domain=customer_domain,
            tax_id=tax_id, line_items=line_items, owner=owner,
            closed_at=current.closed_at,
        )

        st.markdown("##### Payload identity")
        st.caption(
            "This is what gets hashed. Change the owner or reformat the tax ID and "
            "the hash holds, because neither changes what posts to NetSuite. Change "
            "the amount or a line and it moves."
        )
        st.code(canonical_payload(candidate), language="json")
        st.markdown(f"**SHA256** `{payload_hash(candidate)}`")

        b1, b2, b3 = st.columns([1, 1, 2])
        if b1.button("Trigger Closed Won sync", type="primary", width="stretch"):
            state["events"] += 1
            _run_sync(candidate, f"evt-{state['events']:04d}")
            st.rerun()
        if b2.button("Replay the same event", width="stretch",
                     help="Fires an identical payload again, exactly as a webhook "
                          "retry would. The ledger should refuse it."):
            state["events"] += 1
            _run_sync(candidate, f"evt-{state['events']:04d}")
            st.rerun()
        if b3.button("Reset to the sample deal", width="stretch"):
            del st.session_state[STATE]
            st.session_state.pop("nhs_lines", None)
            st.rerun()

    # -----------------------------------------------------------------
    # Account matching
    # -----------------------------------------------------------------
    with tab_match:
        outcome = state["outcome"]
        deal = state["deal"]
        tone = DECISION_TONE[outcome.decision]
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(DECISION_LABEL[outcome.decision])}</span>
  {esc(deal.company_name)}</h4>
  <p>{esc(outcome.recommendation)}</p>
</div>
""",
            unsafe_allow_html=True,
        )
        st.caption(
            f"Resolved in {outcome.elapsed_ms:.2f} ms against "
            f"{len(SAMPLE_ACCOUNTS)} NetSuite customers. Strategies run in order of "
            f"trust: an exact tax ID is conclusive, a domain is strong, a name alone "
            f"only links above {AUTO_LINK_CONFIDENCE:.0%} and is otherwise held for "
            f"review."
        )

        if outcome.candidates:
            st.markdown("##### Candidates considered")
            for candidate in outcome.candidates:
                bar = "█" * max(1, int(round(candidate.confidence * 20)))
                st.markdown(
                    f"""
<div class="app-card">
  <h4><span class="app-tag info">{esc(STRATEGY_LABEL[candidate.strategy])}</span>
  {esc(candidate.account.internal_id)} &middot; {esc(candidate.account.company_name)}</h4>
  <div class="app-ev">{esc(bar)} {candidate.confidence:.2%} confidence</div>
  <p>{esc(candidate.rationale)}</p>
  <p><strong>Subsidiary:</strong> {esc(candidate.account.subsidiary)} &middot;
     <strong>Currency:</strong> {esc(candidate.account.currency)} &middot;
     <strong>Tax ID:</strong> {esc(candidate.account.tax_id)}</p>
</div>
""",
                    unsafe_allow_html=True,
                )
        else:
            st.info(
                f"No customer scored above the {REVIEW_CONFIDENCE:.0%} review "
                "threshold, so nothing is offered to merge against. Creating a new "
                "customer is the safe outcome here."
            )

        st.markdown("##### Recommended next steps")
        for step in merge_recommendation(outcome):
            st.markdown(f"- {step}")

        with st.expander("The NetSuite account book being matched against"):
            st.dataframe(
                [
                    {"Internal ID": a.internal_id, "Company": a.company_name,
                     "Domain": a.domain, "Tax ID": a.tax_id,
                     "Subsidiary": a.subsidiary, "Currency": a.currency}
                    for a in SAMPLE_ACCOUNTS
                ],
                width="stretch", hide_index=True,
            )

    # -----------------------------------------------------------------
    # Idempotency ledger
    # -----------------------------------------------------------------
    with tab_ledger:
        ledger, entry = state["ledger"], state["entry"]
        st.markdown("#### Latest event")
        tone = {"accepted": "ok", "rejected_duplicate": "warn", "rolled_back": "info"}
        st.markdown(
            f"""
<div class="app-card {tone.get(entry.status, 'info')}">
  <h4><span class="app-tag {tone.get(entry.status, 'info')}">
  {esc(STATUS_LABEL.get(entry.status, entry.status))}</span>
  Event {esc(entry.event_id)}</h4>
  <div class="app-ev">SHA256 {esc(entry.payload_hash)}</div>
  <p>{esc(entry.reason)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        if entry.status == REJECTED:
            st.success(
                "This is the behaviour that matters. The retry carried an identical "
                "payload, so it hashed identically and was refused. No second sales "
                "order exists."
            )
        elif entry.status == ACCEPTED:
            st.info(
                f"Posted as {entry.netsuite_record}. Use Replay the same event on the "
                "Deal Simulator tab to prove a retry cannot post it twice."
            )

        st.markdown("##### Ledger")
        st.dataframe(ledger_rows(ledger), width="stretch", hide_index=True)

        accepted = ledger.accepted
        st.markdown("##### Rollback")
        if accepted:
            st.caption(
                "Reversing a posting releases its payload hash, because once the "
                "NetSuite record is gone the same deal legitimately needs to post "
                "again."
            )
            options = {
                f"#{e.sequence} {e.netsuite_record} ({e.deal_id})": e.sequence
                for e in accepted
            }
            chosen = st.selectbox("Posting to reverse", list(options))
            if st.button("Roll back this posting"):
                rolled = ledger.rollback(options[chosen])
                st.session_state[STATE]["entry"] = rolled
                st.rerun()
        else:
            st.info("Nothing is currently posted, so there is nothing to reverse.")

    # -----------------------------------------------------------------
    # Feeds and audit
    # -----------------------------------------------------------------
    with tab_audit:
        deal, entry, feeds = state["deal"], state["entry"], state["feeds"]
        totals = feed_totals(feeds)
        st.markdown("#### Financial feed reconciliation")
        rows = "".join(
            f'<div class="app-stage">'
            f'<div class="s {FEED_TONE.get(line.status, "skip")}">'
            f'{esc(FEED_STATUS_LABEL.get(line.status, line.status)).upper()}</div>'
            f'<div class="n">{esc(line.system)} &middot; {esc(line.reference)}</div>'
            f'<div class="d">{esc(line.description)}: '
            f'<strong>{line.amount:,.2f} {esc(deal.currency)}</strong>. '
            f'{esc(line.note)}</div></div>'
            for line in feeds
        )
        st.markdown(f'<div class="app-card">{rows}</div>', unsafe_allow_html=True)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{totals['reconciled']:,.0f}</div>
    <div class="l">Reconciled</div></div>
  <div class="app-kpi crit"><div class="n">{totals['variance']:,.0f}</div>
    <div class="l">In variance</div></div>
  <div class="app-kpi"><div class="n">{totals['pending']:,.0f}</div>
    <div class="l">Awaiting settlement</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        if totals["variance"] > 0:
            st.warning(
                "A variance means the sales order header does not tie to its own "
                "lines. Fix the lines on the Deal Simulator tab before this is "
                "released to finance."
            )

        st.markdown("#### SharePoint Online audit feed")
        st.caption(
            "One row per operation, shaped like the unified audit log, so a "
            "reviewer can follow the whole event without opening NetSuite."
        )
        audit = sharepoint_audit(deal, entry, state["outcome"], feeds)
        st.dataframe(
            [
                {"#": a.sequence, "Timestamp": a.timestamp, "Actor": a.actor,
                 "Operation": a.operation, "Item": a.item, "Detail": a.detail,
                 "Correlation": a.correlation_id}
                for a in audit
            ],
            width="stretch", hide_index=True,
        )

    st.markdown(
        f"""
<div class="app-foot">
NetSuite HubSpot Idempotent Sync Console, engine version {ENGINE_VERSION}.
A simulator: no NetSuite, HubSpot, ADP, Ramp, Chase or SharePoint system is
contacted, and nothing you enter leaves this session. Matching thresholds are
{AUTO_LINK_CONFIDENCE:.0%} to link automatically and {REVIEW_CONFIDENCE:.0%} to
offer for review.
</div>
""",
        unsafe_allow_html=True,
    )
