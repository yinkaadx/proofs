# ASKEW Bespoke Pricing Engine

Visual base plus upgrade configurator, fifty percent split deposit logic, and
customer measurement database simulator.

Live at `/askew-suit-engine` on the hub.

## One engine, two front doors

This tool already existed in the repository as a standalone Streamlit app at
`askew_suit_engine.py`. Rather than write a second pricing implementation for
the hub, the logic moved into `core.py` here and the standalone file now imports
it. Both run the same arithmetic.

That matters more than it sounds. Two implementations of one deposit rule is
exactly how a figure on a quote comes to disagree with the figure on an invoice,
and the disagreement is usually a cent, discovered by the client. A test pins
that the two front doors hold the same function objects, so they cannot drift.

- `streamlit run askew_suit_engine.py` still opens the standalone app with its
  own bespoke styling.
- `/askew-suit-engine` opens the same engine inside the hub, using
  `shared/theme.py`.

## The money

Every amount is an integer number of cents. Half of an odd total is half a cent,
and a float loses it quietly. The deposit is rounded half up and the balance is
the remainder, so the two always add back to the total exactly. The existing
suite pins that property for odd amounts.

## Two refusals worth naming

A monogram ordered with no initials is stopped at the order rather than at the
cutting table, where it becomes a stalled commission and a phone call. And a
waist larger than the chest is held for confirmation rather than saved, because
it happens and it is also exactly what a transposed pair of numbers looks like.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | The engine. No Streamlit import, shared with the standalone app |
| `page.py` | The hub page, using `shared/theme.py` |
| `../../askew_suit_engine.py` | The standalone app, importing this core |
| `../../tests/test_askew_suit_engine.py` | Engine tests, 73 checks |
| `../../tests/test_askew_suit_engine_page.py` | Hub page tests via AppTest, 33 checks |

## Rules worth keeping when this is extended

- The commission is frozen and every selection returns a new one. A basket
  mutated in place is how a total on screen comes to describe a selection the
  customer already changed.
- Every table column holds one type, because the table widget serialises
  through Arrow and Arrow refuses a mixed column.
- The hub page derives the commission live from the controls. Only the saved
  profile is held in session state, because that is the one thing meant to
  outlive the interaction that created it.
