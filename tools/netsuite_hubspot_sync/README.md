# NetSuite HubSpot Idempotent Sync Console

A console for the integration between HubSpot deals and NetSuite sales orders.
Fire a Closed Won deal and watch it resolve to a customer, hash into the
idempotency ledger, reconcile across the finance systems and land in the
SharePoint audit trail. Replay the event as often as you like: it is refused
rather than posted twice.

Live at `/netsuite-hubspot-sync` on the hub.

## The three problems it answers

**Which customer is this?** Matching runs three strategies in descending order
of trust. An exact tax ID is conclusive, because it is a government issued
identifier, and formatting is ignored, so `GB 412 8837 21` and `gb412883721`
are one customer. A domain is strong, and a full URL, a bare host or an email
address all reduce to the same key. A company name is weakest: it is scored
against every account, and it only links on its own above 90 percent. Between
75 and 90 percent it is held for human review, because a wrong link merges two
real customers and unmerging in NetSuite is manual and lossy. A fuzzy name can
never score 1.0, which is reserved for an exact match.

**Has this already posted?** Every event is reduced to a canonical payload,
with fixed key order, normalised identifiers and sorted line items, then hashed
with SHA256. Fields that do not change what posts to NetSuite are excluded, so
reassigning a deal to another rep does not create a second order. Webhooks
retry, and a retry carries the same payload, so it hashes the same and is
rejected. Rolling a posting back releases its hash, because once the NetSuite
record is reversed the same deal legitimately needs to post again.

**Can finance trace it?** Each event reconciles across NetSuite, ADP, Ramp and
Chase, and writes a SharePoint Online style audit feed: one row per operation,
each naming the actor, the item path and a correlation ID.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | Matching, hashing, ledger, reconciliation and audit. No Streamlit import |
| `page.py` | The five tab console |
| `../../tests/test_netsuite_hubspot_sync.py` | Engine tests, 117 checks |
| `../../tests/test_netsuite_hubspot_sync_page.py` | Page tests via AppTest, 28 checks |

`core.py` is deterministic: where a real system would read the clock, the caller
passes `now`, so a test and a production run of the same payload agree. That is
what lets the same engine sit behind the real webhook handler.
