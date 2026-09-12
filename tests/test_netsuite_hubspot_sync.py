"""Ground truth tests for the NetSuite and HubSpot sync engine.

Pass and fail markers are declared before execution: every case names the
decision, hash property or ledger status it must produce, and the runner parses
results programmatically so nothing is judged by eye.

Run: python3 tests/test_netsuite_hubspot_sync.py
Pass marker: final line is exactly "SYNC RESULT: PASS <n>/<n>" and exit code 0.
Fail marker: any line starting "FAIL", plus exit code 1.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.netsuite_hubspot_sync.core import (  # noqa: E402
    ACCEPTED,
    AUTO_LINK_CONFIDENCE,
    MATCHED,
    REJECTED,
    REVIEW_CONFIDENCE,
    ROLLED_BACK,
    SAMPLE_ACCOUNTS,
    SAMPLE_DEAL,
    VARIANCE,
    Account,
    Deal,
    IdempotencyLedger,
    LineItem,
    canonical_payload,
    feed_totals,
    ledger_rows,
    match_account,
    merge_recommendation,
    name_similarity,
    normalize_company,
    normalize_domain,
    normalize_tax_id,
    payload_hash,
    reconcile_feeds,
    retention_rate,
    retention_report,
    sharepoint_audit,
    sync_kpis,
)

failures: list[str] = []
checks = 0

FIXED_NOW = "2026-09-12T09:14:00Z"


def expect(condition: bool, label: str) -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


def deal_with(**overrides) -> Deal:
    return replace(SAMPLE_DEAL, **overrides)


print("== Normalisers reduce the same identity to the same key ==")
for raw, expected in (
    ("GB 412 8837 21", "gb412883721"),
    ("gb412883721", "gb412883721"),
    ("US 84-2213908", "us842213908"),
    ("  84.2213908  ", "842213908"),
    ("", ""),
):
    expect(normalize_tax_id(raw) == expected,
           f"tax ID {raw!r} normalises to {expected!r} (got {normalize_tax_id(raw)!r})")

for raw, expected in (
    ("https://www.northwind.com/pricing", "northwind.com"),
    ("HTTP://Northwind.com", "northwind.com"),
    ("www.northwind.com", "northwind.com"),
    ("northwind.com", "northwind.com"),
    ("buyer@northwind.com", "northwind.com"),
    ("northwind.com:443/path?q=1", "northwind.com"),
    ("", ""),
):
    expect(normalize_domain(raw) == expected,
           f"domain {raw!r} normalises to {expected!r} (got {normalize_domain(raw)!r})")

for raw, expected in (
    ("Acme Manufacturing, LLC", "acme manufacturing"),
    ("acme manufacturing", "acme manufacturing"),
    ("Northwind Traders, Inc.", "northwind traders"),
    ("Fabrikam Logistics GmbH", "fabrikam logistics"),
    ("The Contoso Group Ltd", "contoso"),
):
    expect(normalize_company(raw) == expected,
           f"company {raw!r} normalises to {expected!r} (got {normalize_company(raw)!r})")

expect(normalize_company("LLC") != "", "a name made only of suffixes does not normalise to blank")

print("== Name similarity behaves like a confidence score ==")
expect(name_similarity("Acme Manufacturing LLC", "acme manufacturing") == 1.0,
       "identical after normalisation scores 1.0")
expect(name_similarity("Contoso Pharmaceuticals Ltd", "Contoso Pharma") >= REVIEW_CONFIDENCE,
       f"a genuine abbreviation clears the review threshold "
       f"(got {name_similarity('Contoso Pharmaceuticals Ltd', 'Contoso Pharma'):.3f})")
expect(name_similarity("Northwind Traders", "Tailspin Toys") < REVIEW_CONFIDENCE,
       f"two unrelated companies stay below the threshold "
       f"(got {name_similarity('Northwind Traders', 'Tailspin Toys'):.3f})")
expect(name_similarity("", "Acme") == 0.0, "an empty name scores 0.0")
expect(0.0 <= name_similarity("Acme Co", "Acme Holdings") <= 1.0,
       "similarity stays within 0.0 and 1.0")

print("== Matching: an exact tax ID is conclusive ==")
outcome = match_account(SAMPLE_DEAL)
expect(outcome.decision == "link", f"the sample deal links (got {outcome.decision})")
expect(outcome.best is not None and outcome.best.strategy == "tax_id",
       f"it matches on tax ID (got {outcome.best.strategy if outcome.best else None})")
expect(outcome.best is not None and outcome.best.account.internal_id == "NS-1001",
       "it resolves to NS-1001")
expect(outcome.best is not None and outcome.best.confidence == 1.0,
       "an exact tax ID scores 1.0")
expect(outcome.elapsed_ms < 1000.0,
       f"matching completes in under a second (took {outcome.elapsed_ms:.3f} ms)")

print("== Matching: a tax ID wins even when the name is wrong ==")
outcome = match_account(deal_with(company_name="Totally Different Holdings",
                                  customer_domain="unrelated.example"))
expect(outcome.best is not None and outcome.best.strategy == "tax_id",
       "tax ID still wins with a misleading name and domain")

print("== Matching: domain carries it when the tax ID is missing ==")
outcome = match_account(deal_with(tax_id="", customer_domain="https://acme-mfg.com/about",
                                  company_name="Unknown Trading"))
expect(outcome.decision == "link", f"a domain match links (got {outcome.decision})")
expect(outcome.best is not None and outcome.best.strategy == "domain",
       f"the strategy is domain (got {outcome.best.strategy if outcome.best else None})")
expect(outcome.best is not None and outcome.best.account.internal_id == "NS-1002",
       "it resolves to NS-1002")

print("== Matching: a strong name links, but never claims certainty ==")
outcome = match_account(deal_with(tax_id="", customer_domain="",
                                  company_name="Contoso Pharmaceutical Limited"))
expect(outcome.best is not None and outcome.best.strategy == "name",
       f"the only candidate is a name match "
       f"(got {outcome.best.strategy if outcome.best else None})")
expect(outcome.decision == "link",
       f"a very close name clears the auto link threshold (got {outcome.decision})")
expect(outcome.best is not None and outcome.best.confidence < 1.0,
       f"a fuzzy name never scores 1.0, which is reserved for an exact tax ID "
       f"(got {outcome.best.confidence if outcome.best else None})")

print("== Matching: a middling name is held for review, never merged ==")
# This case must land inside the review band. If the scorer is retuned so that
# it no longer does, the assertions below fail rather than silently skipping,
# which is how an earlier version of this test missed the review path entirely.
outcome = match_account(deal_with(tax_id="", customer_domain="",
                                  company_name="Contoso Pharma Group"))
expect(outcome.best is not None and outcome.best.strategy == "name",
       f"the candidate comes from the name strategy "
       f"(got {outcome.best.strategy if outcome.best else None})")
expect(outcome.best is not None
       and REVIEW_CONFIDENCE <= outcome.best.confidence < AUTO_LINK_CONFIDENCE,
       f"the score lands inside the review band, between "
       f"{REVIEW_CONFIDENCE} and {AUTO_LINK_CONFIDENCE} "
       f"(got {outcome.best.confidence if outcome.best else None})")
expect(outcome.decision == "review",
       f"a name below the auto link threshold is held for review "
       f"(got {outcome.decision})")
steps = merge_recommendation(outcome)
expect(any("Do not merge" in s for s in steps),
       f"the recommendation refuses to merge on a name alone (got {steps[:1]})")
expect(any("tax ID" in s for s in steps),
       "the recommendation asks for the tax ID as the way to resolve it")
expect("below the" in outcome.recommendation,
       "the recommendation explains why it stopped short of linking")

print("== Matching: nothing plausible means create, not guess ==")
outcome = match_account(deal_with(tax_id="ZZ 999 0000 11",
                                  customer_domain="brand-new-prospect.example",
                                  company_name="Brand New Prospect"))
expect(outcome.decision == "create", f"an unknown customer creates (got {outcome.decision})")
expect(outcome.best is None, "no candidate is offered")
expect(outcome.candidates == [], "the candidate list is empty")
expect(any("Create a new NetSuite customer" in r for r in merge_recommendation(outcome)),
       "the recommendation says to create the customer")

print("== Matching: an empty account book cannot crash or link ==")
outcome = match_account(SAMPLE_DEAL, accounts=())
expect(outcome.decision == "create", "no accounts means create")

print("== Idempotency: the same economic event always hashes the same ==")
first, second = payload_hash(SAMPLE_DEAL), payload_hash(replace(SAMPLE_DEAL))
expect(first == second, "hashing is deterministic across identical deals")
expect(len(first) == 64, f"the digest is 64 hex characters (got {len(first)})")
expect(all(c in "0123456789abcdef" for c in first), "the digest is lowercase hex")

print("== Idempotency: cosmetic differences do not create a new event ==")
for label, changed in (
    ("the rep who owns it", deal_with(owner="someone.else@yourcompany.com")),
    ("the tax ID formatting", deal_with(tax_id="GB 412 8837 21")),
    ("the domain formatting", deal_with(customer_domain="www.northwind.com")),
    ("the company name punctuation", deal_with(company_name="Northwind Traders Inc")),
    ("the line item order", deal_with(line_items=tuple(reversed(SAMPLE_DEAL.line_items)))),
):
    expect(payload_hash(changed) == first, f"changing {label} keeps the same hash")

print("== Idempotency: a real change produces a different event ==")
for label, changed in (
    ("the amount", deal_with(amount=48251.00)),
    ("the deal id", deal_with(deal_id="HS-DEAL-00001")),
    ("the currency", deal_with(currency="USD")),
    ("a line quantity", deal_with(line_items=(
        LineItem("PLAT-ENT", "Platform licence, enterprise tier", 2, 36000.00),))),
    ("a unit price", deal_with(line_items=(
        LineItem("PLAT-ENT", "Platform licence, enterprise tier", 1, 36001.00),))),
    ("the customer", deal_with(tax_id="US 84-2213908")),
):
    expect(payload_hash(changed) != first, f"changing {label} changes the hash")

expect(canonical_payload(SAMPLE_DEAL) == canonical_payload(replace(SAMPLE_DEAL)),
       "the canonical payload itself is stable")
expect("owner" not in canonical_payload(SAMPLE_DEAL),
       "the canonical payload excludes fields that do not change what is posted")

print("== Ledger: the first event posts, the retry does not ==")
ledger = IdempotencyLedger()
accepted = ledger.submit(SAMPLE_DEAL, "evt-001", now=FIXED_NOW)
expect(accepted.status == ACCEPTED, f"the first event is accepted (got {accepted.status})")
expect(accepted.netsuite_record.startswith("SO-"),
       f"it creates a NetSuite record (got {accepted.netsuite_record!r})")

duplicate = ledger.submit(SAMPLE_DEAL, "evt-001-retry", now=FIXED_NOW)
expect(duplicate.status == REJECTED, f"the retry is rejected (got {duplicate.status})")
expect(duplicate.netsuite_record == "", "the rejected retry creates no NetSuite record")
expect(duplicate.duplicate_of == accepted.sequence,
       "the rejection points at the entry it duplicates")
expect(ledger.duplicates_prevented == 1, "one duplicate is counted as prevented")
expect(len(ledger.accepted) == 1, "exactly one posting exists")

print("== Ledger: a genuinely different deal still posts ==")
other = ledger.submit(deal_with(deal_id="HS-DEAL-99999"), "evt-002", now=FIXED_NOW)
expect(other.status == ACCEPTED, f"a different deal is accepted (got {other.status})")
expect(len(ledger.accepted) == 2, "two postings now exist")

print("== Ledger: rollback reverses the posting and frees the hash ==")
rolled = ledger.rollback(accepted.sequence, now=FIXED_NOW)
expect(rolled.status == ROLLED_BACK, f"the entry is rolled back (got {rolled.status})")
expect(len(ledger.accepted) == 1, "the rolled back posting no longer counts as accepted")

resubmitted = ledger.submit(SAMPLE_DEAL, "evt-003", now=FIXED_NOW)
expect(resubmitted.status == ACCEPTED,
       f"the same payload may post again after a rollback (got {resubmitted.status})")

print("== Ledger: invalid rollbacks are refused, not silently ignored ==")
try:
    ledger.rollback(duplicate.sequence, now=FIXED_NOW)
    expect(False, "rolling back a rejected entry raises")
except ValueError:
    expect(True, "rolling back a rejected entry raises ValueError")
try:
    ledger.rollback(9999, now=FIXED_NOW)
    expect(False, "rolling back an unknown entry raises")
except KeyError:
    expect(True, "rolling back an unknown entry raises KeyError")

expect(all(e.sequence == i + 1 for i, e in enumerate(ledger.entries)),
       "ledger sequences stay contiguous and append only")

print("== Ledger display rows survive table serialisation ==")
# The table widget serialises through Arrow, which refuses a column that mixes
# types. An earlier version put an int in "Duplicate of" for a rejection and an
# empty string everywhere else, and it rendered fine under AppTest while
# throwing in a real browser. These checks hold that shape.
rows = ledger_rows(ledger)
expect(len(rows) == len(ledger.entries), "one display row per ledger entry")
expect(rows, "the ledger produces display rows at all")
columns = {key for row in rows for key in row}
for column in columns:
    kinds = {type(row[column]).__name__ for row in rows}
    expect(len(kinds) == 1,
           f"column {column!r} holds a single type (got {sorted(kinds)})")
expect(any(r["Duplicate of"] for r in rows),
       "a rejected duplicate names the entry it duplicates")
expect(all(isinstance(r["Duplicate of"], str) for r in rows),
       "the duplicate reference is a string on every row, blank included")

try:
    import pyarrow  # noqa: F401
    from pyarrow import Table  # noqa: F401
    import pandas
    Table.from_pandas(pandas.DataFrame(rows))
    expect(True, "Arrow serialises the ledger table, which is what the browser does")
except ImportError:
    expect(True, "Arrow not installed here, serialisation check skipped")
except Exception as exc:  # noqa: BLE001
    expect(False, f"Arrow refused the ledger table: {exc}")

print("== Data retention: every mapped field is carried ==")
rows = retention_report(SAMPLE_DEAL)
expect(len(rows) >= 10, f"the field map covers at least ten fields (got {len(rows)})")
expect(retention_rate(SAMPLE_DEAL) == 1.0,
       f"the sample deal retains 100 percent (got {retention_rate(SAMPLE_DEAL):.2%})")
expect(all(isinstance(r[3], bool) for r in rows), "each row reports carried as a boolean")
expect(retention_rate(deal_with(tax_id="", owner="")) < 1.0,
       "dropping fields lowers the retention rate, so the metric is real")

print("== Financial feeds reconcile deterministically ==")
entry = IdempotencyLedger().submit(SAMPLE_DEAL, "evt-feed", now=FIXED_NOW)
feeds_a = reconcile_feeds(SAMPLE_DEAL, entry)
feeds_b = reconcile_feeds(SAMPLE_DEAL, entry)
expect(feeds_a == feeds_b, "the same event reconciles identically every time")
systems = {line.system for line in feeds_a}
for system in ("NetSuite", "ADP", "Ramp", "Chase"):
    expect(system in systems, f"the feed covers {system}")
expect(feeds_a[0].status == MATCHED,
       f"the sample header ties to its lines (got {feeds_a[0].status})")

mismatched = deal_with(amount=50000.00)
expect(reconcile_feeds(mismatched, entry)[0].status == VARIANCE,
       "a header that does not tie to its lines is flagged as a variance")

totals = feed_totals(feeds_a)
expect(set(totals) == {"reconciled", "variance", "pending"},
       f"totals cover every status (got {sorted(totals)})")
expect(totals["pending"] > 0, "the bank deposit is pending until settlement")

print("== SharePoint audit feed ==")
outcome = match_account(SAMPLE_DEAL)
audit = sharepoint_audit(SAMPLE_DEAL, entry, outcome, feeds_a, now=FIXED_NOW)
expect(len(audit) >= 5, f"the audit feed has at least five rows (got {len(audit)})")
expect(all(a.timestamp == FIXED_NOW for a in audit),
       "every row carries the supplied timestamp, so the feed is testable")
expect(len({a.correlation_id for a in audit}) == 1,
       "every row shares one correlation ID")
expect(all(a.item.startswith("/sites/") for a in audit),
       "every row names a SharePoint item path")
operations = {a.operation for a in audit}
expect("FileUploaded" in operations, "the payload archive is recorded")
expect(any("payload.json" in a.item for a in audit), "the canonical payload is archived")
expect(any("reconciliation" in a.item for a in audit), "the reconciliation is archived")

rejected_entry = IdempotencyLedger()
rejected_entry.submit(SAMPLE_DEAL, "evt-a", now=FIXED_NOW)
dupe = rejected_entry.submit(SAMPLE_DEAL, "evt-b", now=FIXED_NOW)
dupe_audit = sharepoint_audit(SAMPLE_DEAL, dupe, outcome, feeds_a, now=FIXED_NOW)
expect(any("duplicate-rejected" in a.item for a in dupe_audit),
       "a rejected duplicate is written to the audit feed too")
expect(not any(".pdf" in a.item for a in dupe_audit),
       "a rejected duplicate archives no sales order document")

print("== Executive KPIs ==")
kpis = sync_kpis(SAMPLE_DEAL, ledger, outcome)
expect(kpis.retention_rate == 1.0, "retention reports 100 percent")
expect(kpis.duplicate_records_created == 0, "no duplicate records are created")
expect(kpis.duplicates_prevented >= 1, "prevented duplicates are counted")
expect(kpis.sub_second, f"matching is sub second (took {kpis.match_ms:.3f} ms)")

print("== Punctuation discipline: no dash characters in user facing prose ==")
banned = ("—", "–")
prose: list[tuple[str, str]] = []
for account in SAMPLE_ACCOUNTS:
    prose.append((f"account {account.internal_id}", account.company_name))
out = match_account(SAMPLE_DEAL)
prose.append(("match recommendation", out.recommendation))
prose += [(f"candidate {c.account.internal_id}", c.rationale) for c in out.candidates]
prose += [(f"merge step {i}", s) for i, s in enumerate(merge_recommendation(out))]
prose += [(f"feed {l.system}", l.note) for l in feeds_a]
prose += [(f"feed desc {l.system}", l.description) for l in feeds_a]
prose += [(f"audit {a.sequence}", a.detail) for a in audit]
prose += [(f"ledger {e.sequence}", e.reason) for e in ledger.entries]
offenders = [name for name, text in prose if any(b in (text or "") for b in banned)]
expect(not offenders, f"no em or en dashes in prose (offenders: {offenders[:5]})")

print()
if failures:
    print(f"SYNC RESULT: FAIL {len(failures)} of {checks} checks failed")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"SYNC RESULT: PASS {checks}/{checks}")
sys.exit(0)
