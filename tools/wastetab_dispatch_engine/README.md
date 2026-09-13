# WasteTab Logistics and Financial Engine

Regional dispatch routing, GoDaddy gross up fee calculator, and emergency safety
valve simulator.

Live at `/wastetab-dispatch-engine` on the hub.

## The money problem, which is the whole point

Charge a customer £130 through a processor taking 2.3 percent plus 30p and
£126.71 arrives. The invoice says the right number and the bank says a different
one, so the margin quietly pays the fee on every single job and nothing appears
as a line anywhere.

Grossing up means asking the opposite question: what must be charged so that
£130 lands. The algebra gives £133.37, and the algebra alone is not enough. The
processor rounds its percentage to the penny before adding the fixed part, so a
one line formula can land a penny either side of the target.

So `gross_up` starts from the formula and then checks its answer against the
same arithmetic the processor uses, walking to the smallest charge that still
clears the target. That property is verified exhaustively rather than sampled:
for every net from one penny to two thousand pounds, the charge is never short
and never a penny more than it has to be.

Every amount in the module is an integer number of pence. Floats are the reason
a gross up lands a penny short.

## The other three

**Regional dispatch.** Routing runs on the outward half of a UK postcode, since
the inward half identifies a street and tells a dispatcher nothing. Carriers are
filtered by area, then by service, then by waste licence, and the refusal says
which of the three failed. An area nobody covers is held for the partner desk
rather than sent to whoever is first in the list.

**The state machine.** NEW ENQUIRY to QUOTE SENT to BOOKED to COMPLETED, with
ON HOLD for the safety valve and CANCELLED throughout. A transition not on the
map is refused rather than written, because a job that reaches completed without
passing booked is an invoice nobody can explain. The console offers a control
that attempts any transition directly, so the refusal is visible rather than
merely a disabled button.

**The safety valve.** A carrier finding materially different waste pauses the
collection and raises a supplementary link, grossed up the same way. Waste with
no uplift is absorbed instead, because stopping a van over nothing costs more
than the difference. Declining the surcharge cancels the job rather than running
it at a loss.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | The engine. No Streamlit import, so it can sit behind the real WordPress webhook |
| `page.py` | The page, rendered by the hub |
| `../../tests/test_wastetab_dispatch_engine.py` | Engine tests, 102 checks |
| `../../tests/test_wastetab_dispatch_engine_page.py` | Page tests via AppTest, 33 checks |

## Rules worth keeping when this is extended

- Money is integer pence, everywhere, and `Decimal` does the rounding. A float
  anywhere in this file is a defect waiting for a quarter end.
- Every table column holds one type, because the table widget serialises
  through Arrow and Arrow refuses a mixed column.
- Callbacks read live widget state out of `st.session_state` rather than
  arguments bound at render time, which are one interaction stale.
- An expected figure in a test is worked by hand, not read off the engine. A
  test that takes its expectation from the code it is testing only agrees with
  itself, which is how the £332.96 case was nearly written wrong.
