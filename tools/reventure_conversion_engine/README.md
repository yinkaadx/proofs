# Mobile Conversion and Release Engine

Feature flag A/B test controller, App Store rating logic, and Stripe
subscription webhook pipeline.

Live at `/reventure-conversion-engine` on the hub.

## Three failures that are invisible in testing

A flag rolled to everyone on a result that was noise ships a paywall that
converts worse than the one it replaced. A rating prompt fired on launch burns
one of the three asks Apple allows in a year on somebody who has not yet seen
the app do anything. A webhook handled twice charges a card twice, and the
customer finds that before the dashboard does.

So the statistics are computed rather than asserted, the rating gates are
enforced rather than documented, and the webhook ledger is keyed on the event id
the way a real handler has to be.

## The four tabs

**Feature flag and A/B controller.** Assignment is a hash of the user id and the
flag key together, so a person keeps the same variant across sessions and
devices, and two flags do not put the same people in treatment. Raising the
rollout never takes anyone back off the treatment, which a test pins. The
headline number is the blended rate the whole audience actually sees, not the
treatment rate: showing the treatment rate at a ten percent rollout quietly
claims nine times the result.

**App Store rating optimizer.** Five gates: the action has to be high value, the
user needs three sessions, Apple's three per year has to be unspent, the ninety
day cooldown has to be clear, and the version must not already have asked. App
launch is refused on the first gate. Apple counts an attempt even when it
displays nothing, so asking on launch spends an ask and the app believes it
succeeded.

**Stripe pipeline.** A redelivered event changes nothing, because Stripe retries
until it gets a 2xx. An event created before one already applied does not roll
the entitlement backwards. And an Apple in app purchase that also reaches Stripe
is not charged again, because the two systems do not know about each other. The
whole sample stream takes 14.99 exactly once.

**Recommendation.** The statistics turned into a decision: ship, roll back, keep
running or hold, with the reasoning quoting the same numbers the table shows. It
is computed rather than generated on purpose. A real deployment hands these
figures to Claude for the write up; doing the arithmetic here means the
recommendation cannot contradict the table it is describing, which is the
failure that matters when somebody ships on the strength of a paragraph.

## The statistics

A two proportion z test written out: pooled standard error for the p value,
unpooled for the interval, and `math.erf` for the normal distribution. No
library, so there is nothing to take on trust.

Power is sized against a minimum effect declared in advance, currently a 15
percent relative lift, rather than against the difference observed. Sizing
against what you happened to measure makes a flat result look underpowered
forever, so a test could never conclude that nothing happened. That was a real
defect during the build: the hold verdict was unreachable.

When a result clears significance on arms smaller than the planning calculation
wanted, the recommendation still says ship and adds that the measured lift is
probably flattering, because a difference that clears the bar early tends to be
the high end of its range.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | The engine. No Streamlit import, so it can sit behind the real webhook endpoint |
| `page.py` | The page, rendered by the hub |
| `../../tests/test_reventure_conversion_engine.py` | Engine tests, 76 checks |
| `../../tests/test_reventure_conversion_engine_page.py` | Page tests via AppTest, 36 checks |

## Rules worth keeping when this is extended

- Every table column holds one type, because the table widget serialises
  through Arrow and Arrow refuses a mixed column.
- Callbacks read live widget state out of `st.session_state` rather than
  arguments bound at render time, which are one interaction stale.
- Bucketing hashes the user id with the flag key. Hashing the id alone would
  correlate every flag, so the same people would always be in treatment.
