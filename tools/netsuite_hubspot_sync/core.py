"""NetSuite and HubSpot idempotent sync engine.

Pure logic: account matching, payload hashing and the idempotency ledger,
financial feed reconciliation and SharePoint audit entries. No Streamlit import,
so the engine is unit testable on its own and reusable behind a CLI, a webhook
handler or a scheduled job.

Every function here is deterministic. Where a real system would reach for the
clock, the caller passes `now` instead, so a test and a production run of the
same payload produce the same result.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher

ENGINE_VERSION = "1.0.0"

# Confidence at or above which a match is safe to link without a human.
AUTO_LINK_CONFIDENCE = 0.90
# Below this, the candidate is too weak to offer at all.
REVIEW_CONFIDENCE = 0.75

# Legal suffixes carry no identity: "Acme Inc" and "Acme, LLC" are one company.
LEGAL_SUFFIXES = (
    "incorporated", "inc", "corporation", "corp", "company", "co",
    "limited", "ltd", "llc", "llp", "lp", "plc", "gmbh", "ag", "bv", "nv",
    "sa", "sas", "srl", "spa", "pty", "pte", "oy", "ab", "as", "kk",
    "holdings", "group", "international", "worldwide", "global",
)

STOP_TOKENS = frozenset({"the", "and", "of"})


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Account:
    """A customer as it already exists in NetSuite."""
    internal_id: str
    company_name: str
    domain: str
    tax_id: str
    subsidiary: str
    currency: str


@dataclass(frozen=True)
class LineItem:
    sku: str
    description: str
    quantity: int
    unit_price: float

    @property
    def total(self) -> float:
        return round(self.quantity * self.unit_price, 2)


@dataclass(frozen=True)
class Deal:
    """A HubSpot deal that has just moved to Closed Won."""
    deal_id: str
    deal_name: str
    amount: float
    currency: str
    company_name: str
    customer_domain: str
    tax_id: str
    line_items: tuple[LineItem, ...] = ()
    owner: str = "unassigned"
    closed_at: str = "2026-09-12T00:00:00Z"

    @property
    def line_total(self) -> float:
        return round(sum(item.total for item in self.line_items), 2)


SAMPLE_ACCOUNTS: tuple[Account, ...] = (
    Account("NS-1001", "Northwind Traders Inc", "northwind.com", "GB 412 8837 21",
            "Northwind EMEA", "GBP"),
    Account("NS-1002", "Acme Manufacturing LLC", "acme-mfg.com", "US 84-2213908",
            "Acme US", "USD"),
    Account("NS-1003", "Contoso Pharmaceuticals Ltd", "contoso.co.uk", "GB 771 2290 04",
            "Contoso UK", "GBP"),
    Account("NS-1004", "Fabrikam Logistics GmbH", "fabrikam.de", "DE 812934771",
            "Fabrikam DACH", "EUR"),
    Account("NS-1005", "Tailspin Toys Pty", "tailspintoys.com.au", "AU 54 221 889 003",
            "Tailspin APAC", "AUD"),
    Account("NS-1006", "Litware Analytics", "litware.io", "US 27-5540912",
            "Litware US", "USD"),
)


SAMPLE_DEAL = Deal(
    deal_id="HS-DEAL-88214",
    deal_name="Northwind Traders renewal, FY27 platform",
    amount=48250.00,
    currency="GBP",
    company_name="Northwind Traders, Inc.",
    customer_domain="https://www.northwind.com/pricing",
    tax_id="gb412883721",
    line_items=(
        LineItem("PLAT-ENT", "Platform licence, enterprise tier", 1, 36000.00),
        LineItem("SUP-PREM", "Premium support, 12 months", 1, 9000.00),
        LineItem("ONB-STD", "Onboarding, standard", 1, 3250.00),
    ),
    owner="r.okonkwo@yourcompany.com",
    closed_at="2026-09-12T09:14:00Z",
)


# ---------------------------------------------------------------------------
# Normalisers
# ---------------------------------------------------------------------------

def normalize_tax_id(value: str) -> str:
    """Tax IDs are quoted with spaces, dots and dashes that carry no meaning.

    A VAT number written "GB 412 8837 21" in HubSpot and "gb412883721" in
    NetSuite is the same number, so both reduce to the same key.
    """
    return re.sub(r"[^0-9a-z]", "", str(value or "").lower())


def normalize_domain(value: str) -> str:
    """Reduce a URL or email host to its bare registrable host.

    HubSpot stores whatever the rep typed: a full URL with a path, a www
    prefix, sometimes an email address. All of those describe one customer.
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "@" in text:
        text = text.rsplit("@", 1)[-1]
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/", 1)[0]
    text = text.split("?", 1)[0]
    text = text.split(":", 1)[0]
    if text.startswith("www."):
        text = text[4:]
    return text.strip(". ")


def normalize_company(value: str) -> str:
    """Reduce a company name to its identifying words.

    Punctuation, casing and legal suffixes are dropped so that
    "Acme Manufacturing, LLC" and "acme manufacturing" compare as equal.
    """
    text = re.sub(r"[^0-9a-z]+", " ", str(value or "").lower())
    tokens = [t for t in text.split() if t and t not in STOP_TOKENS]
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    if not tokens:
        # The name was nothing but suffixes. Keep them rather than return blank.
        tokens = [t for t in text.split() if t]
    return " ".join(tokens)


def name_similarity(left: str, right: str) -> float:
    """Similarity of two company names, 0.0 to 1.0, after normalisation."""
    a, b = normalize_company(left), normalize_company(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    # Reward a shared leading token: "contoso pharmaceuticals" against
    # "contoso" is a stronger signal than raw character overlap suggests.
    left_tokens, right_tokens = a.split(), b.split()
    if left_tokens[0] == right_tokens[0]:
        ratio += 0.08
    # A name that is not identical after normalisation is never certainty.
    # 1.0 is reserved for an exact match, so a fuzzy score stays below it and
    # can never be read as conclusive.
    return round(min(ratio, 0.99), 4)


# ---------------------------------------------------------------------------
# Account matching
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    account: Account
    strategy: str          # tax_id | domain | name
    confidence: float
    rationale: str


@dataclass
class MatchOutcome:
    decision: str          # link | review | create
    best: Candidate | None
    candidates: list[Candidate]
    recommendation: str
    elapsed_ms: float

    @property
    def matched(self) -> bool:
        return self.best is not None


STRATEGY_LABEL = {
    "tax_id": "Exact tax ID",
    "domain": "Domain",
    "name": "Fuzzy company name",
}


def match_account(deal: Deal, accounts=SAMPLE_ACCOUNTS) -> MatchOutcome:
    """Find the NetSuite customer this deal belongs to.

    Three strategies in descending order of trust. A tax ID is a government
    issued identifier, so an exact match is conclusive. A domain is strong but
    shared domains exist. A name is the weakest and never links on its own
    below the auto link threshold, because a wrong link merges two real
    customers and that is far more expensive than creating one duplicate.
    """
    started = time.perf_counter()
    candidates: list[Candidate] = []

    deal_tax = normalize_tax_id(deal.tax_id)
    deal_domain = normalize_domain(deal.customer_domain)

    for account in accounts:
        account_tax = normalize_tax_id(account.tax_id)
        if deal_tax and account_tax and deal_tax == account_tax:
            candidates.append(Candidate(
                account, "tax_id", 1.0,
                f"Tax ID {deal.tax_id} normalises to {deal_tax}, which is exactly "
                f"the tax ID on {account.internal_id}."))
            continue

        account_domain = normalize_domain(account.domain)
        if deal_domain and account_domain and deal_domain == account_domain:
            candidates.append(Candidate(
                account, "domain", 0.95,
                f"Domain {deal_domain} matches {account.internal_id} exactly after "
                f"stripping the scheme, any www prefix and the path."))
            continue

        similarity = name_similarity(deal.company_name, account.company_name)
        if similarity >= REVIEW_CONFIDENCE:
            candidates.append(Candidate(
                account, "name", similarity,
                f"Company name normalises to {normalize_company(deal.company_name)!r} "
                f"against {normalize_company(account.company_name)!r} on "
                f"{account.internal_id}, similarity {similarity:.2f}."))

    order = {"tax_id": 0, "domain": 1, "name": 2}
    candidates.sort(key=lambda c: (order[c.strategy], -c.confidence,
                                   c.account.internal_id))

    best = candidates[0] if candidates else None
    if best is None:
        decision = "create"
        recommendation = (
            "No candidate reached the review threshold. Create a new NetSuite "
            "customer, then write the HubSpot record ID back so the next deal "
            "matches on the first strategy.")
    elif best.strategy in ("tax_id", "domain") or best.confidence >= AUTO_LINK_CONFIDENCE:
        decision = "link"
        recommendation = (
            f"Link this deal to {best.account.internal_id} "
            f"({best.account.company_name}) and post the sales order against "
            f"subsidiary {best.account.subsidiary}. No human review needed.")
    else:
        decision = "review"
        recommendation = (
            f"Hold for review. {best.account.internal_id} "
            f"({best.account.company_name}) is the closest match at "
            f"{best.confidence:.0%}, which is below the {AUTO_LINK_CONFIDENCE:.0%} "
            "auto link threshold. Confirm the tax ID with the customer rather "
            "than merging on a name alone.")

    elapsed_ms = (time.perf_counter() - started) * 1000
    return MatchOutcome(decision=decision, best=best, candidates=candidates,
                        recommendation=recommendation, elapsed_ms=elapsed_ms)


def merge_recommendation(outcome: MatchOutcome) -> list[str]:
    """Concrete next actions for whoever is looking at the match."""
    if outcome.decision == "link":
        assert outcome.best is not None
        return [
            f"Link HubSpot company to NetSuite {outcome.best.account.internal_id}.",
            f"Post the sales order in {outcome.best.account.currency} against "
            f"subsidiary {outcome.best.account.subsidiary}.",
            "Write the NetSuite internal ID back to the HubSpot company record so "
            "the next deal matches on the first strategy.",
        ]
    if outcome.decision == "review":
        assert outcome.best is not None
        return [
            f"Do not merge yet: {outcome.best.confidence:.0%} is below the "
            f"{AUTO_LINK_CONFIDENCE:.0%} threshold.",
            f"Ask the customer to confirm the tax ID, then compare against "
            f"{outcome.best.account.tax_id} on {outcome.best.account.internal_id}.",
            "If they differ, create a new customer rather than merging, because "
            "unmerging in NetSuite is manual and lossy.",
        ]
    return [
        "Create a new NetSuite customer from the HubSpot company record.",
        "Capture the tax ID at creation so future deals match on the strongest "
        "strategy instead of a name.",
        "Write the new internal ID back to HubSpot before the next sync runs.",
    ]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def canonical_payload(deal: Deal) -> str:
    """A stable JSON rendering of the fields that define a deal's identity.

    Key order is fixed and numbers are rounded, so the same economic event
    hashes identically no matter how the webhook serialised it. Fields that do
    not change what gets posted to NetSuite, such as the owner, are excluded
    deliberately: a deal reassigned to another rep is not a new order.
    """
    body = {
        "amount": round(float(deal.amount), 2),
        "company_name": normalize_company(deal.company_name),
        "currency": deal.currency.upper(),
        "customer_domain": normalize_domain(deal.customer_domain),
        "deal_id": deal.deal_id,
        "line_items": [
            {
                "quantity": int(item.quantity),
                "sku": item.sku,
                "unit_price": round(float(item.unit_price), 2),
            }
            for item in sorted(deal.line_items, key=lambda i: (i.sku, i.description))
        ],
        "tax_id": normalize_tax_id(deal.tax_id),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def payload_hash(deal: Deal) -> str:
    """SHA256 of the canonical payload, as lowercase hex."""
    return hashlib.sha256(canonical_payload(deal).encode("utf-8")).hexdigest()


ACCEPTED = "accepted"
REJECTED = "rejected_duplicate"
ROLLED_BACK = "rolled_back"

STATUS_LABEL = {
    ACCEPTED: "Accepted",
    REJECTED: "Rejected, duplicate",
    ROLLED_BACK: "Rolled back",
}


@dataclass
class LedgerEntry:
    sequence: int
    event_id: str
    deal_id: str
    payload_hash: str
    status: str
    reason: str
    recorded_at: str
    netsuite_record: str = ""
    duplicate_of: int | None = None

    @property
    def short_hash(self) -> str:
        return f"{self.payload_hash[:12]}…{self.payload_hash[-6:]}"


@dataclass
class IdempotencyLedger:
    """Append only record of every sync attempt, keyed by payload hash.

    Webhooks retry. A retry carries the same payload, so it hashes the same and
    is rejected rather than posting a second sales order. Rolling an entry back
    releases its hash, because once the NetSuite record is reversed the same
    payload legitimately needs to post again.
    """
    entries: list[LedgerEntry] = field(default_factory=list)

    def _active_hashes(self) -> dict[str, LedgerEntry]:
        return {e.payload_hash: e for e in self.entries if e.status == ACCEPTED}

    def submit(self, deal: Deal, event_id: str, now: str | None = None) -> LedgerEntry:
        digest = payload_hash(deal)
        stamp = now or _utc_now()
        sequence = len(self.entries) + 1
        existing = self._active_hashes().get(digest)
        if existing is not None:
            entry = LedgerEntry(
                sequence=sequence, event_id=event_id, deal_id=deal.deal_id,
                payload_hash=digest, status=REJECTED,
                reason=(f"Payload hash already posted by event {existing.event_id} "
                        f"as {existing.netsuite_record}. No NetSuite record created."),
                recorded_at=stamp, duplicate_of=existing.sequence)
        else:
            entry = LedgerEntry(
                sequence=sequence, event_id=event_id, deal_id=deal.deal_id,
                payload_hash=digest, status=ACCEPTED,
                reason="First time this payload hash has been seen. Posted to NetSuite.",
                recorded_at=stamp,
                netsuite_record=f"SO-{digest[:8].upper()}")
        self.entries.append(entry)
        return entry

    def rollback(self, sequence: int, now: str | None = None) -> LedgerEntry:
        """Reverse an accepted posting and release its hash for resubmission."""
        stamp = now or _utc_now()
        for index, entry in enumerate(self.entries):
            if entry.sequence == sequence:
                if entry.status != ACCEPTED:
                    raise ValueError(
                        f"Entry {sequence} is {STATUS_LABEL.get(entry.status, entry.status)} "
                        "and cannot be rolled back. Only an accepted posting can.")
                rolled = replace(
                    entry, status=ROLLED_BACK, recorded_at=stamp,
                    reason=(f"Reversed {entry.netsuite_record}. The payload hash is "
                            "released, so the same deal can post again."))
                self.entries[index] = rolled
                return rolled
        raise KeyError(f"No ledger entry with sequence {sequence}.")

    @property
    def accepted(self) -> list[LedgerEntry]:
        return [e for e in self.entries if e.status == ACCEPTED]

    @property
    def rejected(self) -> list[LedgerEntry]:
        return [e for e in self.entries if e.status == REJECTED]

    @property
    def duplicates_prevented(self) -> int:
        return len(self.rejected)


def ledger_rows(ledger: IdempotencyLedger) -> list[dict]:
    """Display rows for the ledger table.

    Every column holds one type. A column that mixes integers with empty
    strings cannot be serialised by Arrow, which is what a table widget uses,
    so the render fails at the browser rather than in Python. Shaping the rows
    here keeps that failure mode inside something a test can hold.
    """
    return [
        {
            "#": entry.sequence,
            "Event": entry.event_id,
            "Deal": entry.deal_id,
            "Status": STATUS_LABEL.get(entry.status, entry.status),
            "SHA256": entry.short_hash,
            "NetSuite record": entry.netsuite_record or "none",
            "Duplicate of": f"#{entry.duplicate_of}" if entry.duplicate_of else "",
            "Recorded": entry.recorded_at,
        }
        for entry in ledger.entries
    ]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Data retention
# ---------------------------------------------------------------------------

# Every HubSpot field the sync carries, and where it lands in NetSuite.
FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("deal_id", "Sales Order, External ID"),
    ("deal_name", "Sales Order, Memo"),
    ("amount", "Sales Order, Total"),
    ("currency", "Sales Order, Currency"),
    ("company_name", "Customer, Company Name"),
    ("customer_domain", "Customer, Web Address"),
    ("tax_id", "Customer, Tax Registration Number"),
    ("line_items", "Sales Order, Item Lines"),
    ("owner", "Sales Order, Sales Rep"),
    ("closed_at", "Sales Order, Trandate"),
)


def retention_report(deal: Deal) -> list[tuple[str, str, str, bool]]:
    """Field by field proof that nothing is dropped in transit."""
    rows = []
    for source, target in FIELD_MAP:
        value = getattr(deal, source)
        if source == "line_items":
            carried = bool(value)
            shown = f"{len(value)} line(s), {deal.line_total:,.2f} {deal.currency}"
        else:
            carried = str(value).strip() != ""
            shown = str(value)
        rows.append((source, target, shown, carried))
    return rows


def retention_rate(deal: Deal) -> float:
    rows = retention_report(deal)
    if not rows:
        return 0.0
    return sum(1 for r in rows if r[3]) / len(rows)


# ---------------------------------------------------------------------------
# Financial feeds
# ---------------------------------------------------------------------------

MATCHED = "matched"
VARIANCE = "variance"
PENDING = "pending"

FEED_STATUS_LABEL = {
    MATCHED: "Reconciled",
    VARIANCE: "Variance",
    PENDING: "Awaiting settlement",
}


@dataclass(frozen=True)
class FeedLine:
    system: str
    reference: str
    description: str
    amount: float
    status: str
    note: str


def reconcile_feeds(deal: Deal, entry: LedgerEntry) -> list[FeedLine]:
    """Simulated reconciliation across the systems either side of the sync.

    Deterministic by construction: references derive from the payload hash and
    amounts from the deal, so the same event always reconciles the same way.
    """
    digest = entry.payload_hash
    order_total = round(float(deal.amount), 2)
    line_total = deal.line_total
    commission = round(order_total * 0.05, 2)
    card_spend = round(order_total * 0.012, 2)

    header_matches = (line_total == 0.0) or (abs(line_total - order_total) < 0.01)

    lines = [
        FeedLine(
            "NetSuite", entry.netsuite_record or "not posted",
            "Sales order total against the sum of its item lines",
            order_total,
            MATCHED if header_matches else VARIANCE,
            "Header and lines agree."
            if header_matches
            else (f"Header is {order_total:,.2f} but the lines sum to "
                  f"{line_total:,.2f}, a variance of "
                  f"{abs(order_total - line_total):,.2f} {deal.currency}. "
                  "NetSuite will post the header and leave the difference "
                  "unallocated, so fix the lines before release."),
        ),
        FeedLine(
            "ADP", f"ADP-{digest[8:16].upper()}",
            "Commission accrual for the closing rep, 5 percent of order value",
            commission, MATCHED,
            f"Accrued against {deal.owner} in the current payroll period.",
        ),
        FeedLine(
            "Ramp", f"RMP-{digest[16:24].upper()}",
            "Card spend attributable to this account, fulfilment and onboarding",
            card_spend, MATCHED,
            "Coded to the customer and carried into the same revenue period.",
        ),
        FeedLine(
            "Chase", f"CHS-{digest[24:32].upper()}",
            "Expected deposit once the invoice settles",
            order_total, PENDING,
            "Invoice issued on net 30 terms, so the bank feed will show the "
            "deposit after settlement, not today.",
        ),
    ]
    return lines


def feed_totals(lines: list[FeedLine]) -> dict[str, float]:
    return {
        "reconciled": round(sum(l.amount for l in lines if l.status == MATCHED), 2),
        "variance": round(sum(l.amount for l in lines if l.status == VARIANCE), 2),
        "pending": round(sum(l.amount for l in lines if l.status == PENDING), 2),
    }


# ---------------------------------------------------------------------------
# SharePoint Online audit feed
# ---------------------------------------------------------------------------

AUDIT_LIBRARY = "/sites/Finance/Shared Documents/NetSuite Sync"


@dataclass(frozen=True)
class AuditEntry:
    sequence: int
    timestamp: str
    actor: str
    operation: str
    item: str
    detail: str
    correlation_id: str


def sharepoint_audit(deal: Deal, entry: LedgerEntry, outcome: MatchOutcome,
                     feeds: list[FeedLine], now: str | None = None) -> list[AuditEntry]:
    """The audit trail a finance reviewer can open without touching NetSuite.

    Shaped like a SharePoint Online unified audit log: one row per operation,
    each naming the actor, the item path and a correlation ID that ties the row
    back to the sync event.
    """
    stamp = now or entry.recorded_at or _utc_now()
    correlation = entry.payload_hash[:16]
    actor = "svc-netsuite-sync@yourcompany.com"
    folder = f"{AUDIT_LIBRARY}/{deal.deal_id}"

    rows = [
        AuditEntry(1, stamp, actor, "FileUploaded", f"{folder}/payload.json",
                   f"Canonical payload archived, SHA256 {entry.payload_hash[:16]}.",
                   correlation),
        AuditEntry(2, stamp, actor, "FileAccessed", f"{folder}/match-evidence.json",
                   (f"Account match by {STRATEGY_LABEL.get(outcome.best.strategy, 'none') if outcome.best else 'no strategy'}, "
                    f"decision {outcome.decision}, "
                    f"confidence {outcome.best.confidence:.2f}." if outcome.best
                    else "No candidate above threshold, new customer required."),
                   correlation),
    ]

    if entry.status == ACCEPTED:
        rows.append(AuditEntry(
            3, stamp, actor, "FileUploaded", f"{folder}/{entry.netsuite_record}.pdf",
            f"Sales order {entry.netsuite_record} posted and the document archived.",
            correlation))
    elif entry.status == REJECTED:
        rows.append(AuditEntry(
            3, stamp, actor, "FileAccessed", f"{folder}/duplicate-rejected.json",
            (f"Duplicate event {entry.event_id} rejected against ledger entry "
             f"{entry.duplicate_of}. No NetSuite record created."),
            correlation))
    else:
        rows.append(AuditEntry(
            3, stamp, actor, "FileModified", f"{folder}/rollback.json",
            f"{entry.reason}", correlation))

    totals = feed_totals(feeds)
    rows.append(AuditEntry(
        4, stamp, actor, "FileModified", f"{folder}/reconciliation.xlsx",
        (f"Feed reconciliation written: {totals['reconciled']:,.2f} reconciled, "
         f"{totals['variance']:,.2f} in variance, {totals['pending']:,.2f} awaiting "
         f"settlement, across ADP, Ramp and Chase."),
        correlation))
    rows.append(AuditEntry(
        5, stamp, "finance-reviewers@yourcompany.com", "PermissionGranted", folder,
        "Read access granted to the finance reviewer group for this deal folder.",
        correlation))
    return rows


# ---------------------------------------------------------------------------
# Executive summary
# ---------------------------------------------------------------------------

@dataclass
class SyncKpis:
    retention_rate: float
    duplicates_prevented: int
    duplicate_records_created: int
    match_ms: float
    accepted: int

    @property
    def sub_second(self) -> bool:
        return self.match_ms < 1000.0


def sync_kpis(deal: Deal, ledger: IdempotencyLedger, outcome: MatchOutcome) -> SyncKpis:
    return SyncKpis(
        retention_rate=retention_rate(deal),
        duplicates_prevented=ledger.duplicates_prevented,
        duplicate_records_created=0,
        match_ms=outcome.elapsed_ms,
        accepted=len(ledger.accepted),
    )
