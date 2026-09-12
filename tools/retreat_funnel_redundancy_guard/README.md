# Retreat Funnel Redundancy Guard

FG Funnels webhook simulator, automated Slack alert routing, and webinar metrics
tracking.

Live at `/retreat-funnel-redundancy-guard` on the hub.

## The failure this exists to catch

A retreat sells from a webinar, and every step between the first DM and the
offer is a webhook. A webhook that fails quietly costs a seat, and it does it
without producing a complaint: the lead is not angry, they simply never got the
join link. Nobody finds out until the show up rate is read a week later, and by
then it reads as a weak audience rather than a broken integration.

So the guard measures an alert against the clock. The manual fallback takes
about fifteen minutes once somebody reads it, so an alert raised ninety minutes
before the webinar is a rescue and one raised five minutes before is only a
record. The verdict says which it is instead of just raising and hoping.

## The three tabs

**Lead simulator.** Generates the inbound DM payload FG Funnels posts, with the
tags assigned by rule rather than by hand, because every automation downstream
keys off them and a tag applied by hand is a tag that is missing at 2am.

**Redundancy guard.** Seven webhooks, with a chosen failure injected. A
transient failure recovers on retry and the run still reports the time it lost,
because a retry that works costs time the reminder schedule is counting on. A
hard failure stops the pipeline rather than continuing, because a confirmation
email sent with no join link in it is worse than one not sent at all, and the
Slack payload it produces carries the lead, the failing webhook, the money at
risk and the exact manual fallback.

**Metrics dashboard.** Opens, clicks, show up rate and close rate against
targets. The show up rate is the load bearing one and is flagged as critical,
with the gap turned into the number of people who registered and never arrived.
Its note points back at the second tab, because a lead who never received a
join link is counted here as a no show and looks exactly like disinterest.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | The engine. No Streamlit import, so it can sit behind the real webhook |
| `page.py` | The page, rendered by the hub |
| `../../tests/test_retreat_funnel_redundancy_guard.py` | Engine tests, 76 checks |
| `../../tests/test_retreat_funnel_redundancy_guard_page.py` | Page tests via AppTest, 27 checks |

## Determinism

Nothing in `core.py` reads the clock or a random source. The lead generator
takes an injected `random.Random`, so a seeded run reproduces exactly, and every
timestamp is the lead's own arrival time plus an elapsed duration the engine
computed. That is what makes the audit trail usable as evidence rather than as
an illustration.

## Rules worth keeping when this is extended

- Every table column holds one type, because the table widget serialises
  through Arrow and Arrow refuses a mixed column.
- Callbacks read live widget state out of `st.session_state` rather than
  arguments bound at render time, which are one interaction stale.
- The failure selector formats its option labels. This version of Streamlit
  stores the raw value under the widget key, which was verified rather than
  assumed, and `test_the_failure_chosen_on_screen_is_the_one_actually_injected`
  pins it: if a formatted label ever reached the engine instead of the code, the
  console would quietly run a clean pipeline and report no failure at all.
