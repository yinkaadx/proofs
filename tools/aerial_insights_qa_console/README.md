# Aerial Insights QA and Production Console

Background worker diagnostics, Prisma connection pooling auditor, and Stripe
idempotency ledger.

Live at `/aerial-insights-qa-console` on the hub.

## Four launch blockers, each invisible until the load arrives

**Two workers claim the same tile.** The claim is a read then a write, so every
worker polling on the same tick reads the same head row as queued and takes it.
The same image is inferred twice, paid for twice and written twice.

**Resident memory climbs with every tile.** Tensors and decoded images are never
released, so a worker crosses its container limit after nine tiles and is
killed. The tile it was holding still reads running, so nothing picks it up
again and it is simply lost.

**Every serverless instance opens its own pool.** Forty instances at a
connection limit of five demand two hundred connections against ninety seven
available, and Prisma raises P2037. Staging never reaches it because staging
never runs forty instances at once.

**A webhook is delivered twice.** Stripe retries until it gets a 2xx, so the
second delivery has to return the stored response rather than charge again.

## The race is reproduced, not sampled

The scheduler is tick based rather than threaded, and the double claim is the
actual shape of the bug rather than a dice roll. Modelling it as a random event
would be dishonest: the bug is deterministic given the poll pattern, and that is
precisely why it survives a staging environment running one worker. The console
shows a single worker never racing itself, which is the reason nobody catches it
before launch.

The same discipline applies to the memory leak. Twenty four tiles over three
workers is eight each and survives; thirty is ten each and kills every worker.
The arithmetic is `(2048 - 512) / 180`, and both cases are pinned by tests.

## The Stripe gates, in Stripe's own order

Verify the signature before parsing anything, refuse a timestamp outside the
five minute tolerance so a captured request cannot be replayed later, check the
livemode so a test event cannot grant a live entitlement, and only then check
whether the event id has already been handled. The sample set exercises all four
in one run and charges exactly once.

The signing secret in this module is deliberately not shaped like a real one.
Nothing in a repository should look like a credential even when it is not, and a
scanner refusing the push would be right to. A test asserts that.

## Deployment closure

Dry run against a restored snapshot, evidence captured before anything is
touched, rollback rehearsed rather than written, and only then apply. Fail any
gate and the apply step reads blocked rather than skipped, because a pipeline
that continues past a failed gate and reports success is worse than one with no
gate at all.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | The engine. No Streamlit import, so it can run in CI as a pre launch gate |
| `page.py` | The page, rendered by the hub |
| `../../tests/test_aerial_insights_qa_console.py` | Engine tests, 55 checks |
| `../../tests/test_aerial_insights_qa_console_page.py` | Page tests via AppTest, 33 checks |

## Rules worth keeping when this is extended

- Every table column holds one type, because the table widget serialises
  through Arrow and Arrow refuses a mixed column.
- Nothing on this page is stored between reruns. Everything is evaluated live
  from the controls, so no card can describe a run that was replaced two
  interactions ago.
- A simulated failure is modelled as its real mechanism, never as randomness. A
  race that only happens sometimes teaches the reader the wrong thing about
  why it happens.
