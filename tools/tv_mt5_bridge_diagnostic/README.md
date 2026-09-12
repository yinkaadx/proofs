# TradingView MT5 Bridge Diagnostic Console

Webhook payload inspector, latency analyzer, and execution drift monitor for
TradingView to MT5 synchronization.

Live at `/tv-mt5-bridge-diagnostic` on the hub.

## What it answers

Two questions follow every bad day on a bridged strategy, and both are answered
here with the arithmetic shown rather than asserted.

**Why was the fill not the price on the chart.** A TradingView chart plots the
mid. A broker quotes a bid and an ask around it. A buy pays the ask and a sell
receives the bid, so a fill is never the chart price. On top of that the market
keeps moving while the alert is in flight. The console separates those two
costs, because only one of them can be negotiated with a broker.

**Why did the stop fire at a price that never printed.** A long is stopped on
the bid, which sits half a spread below the chart, so the chart only has to fall
to the stop plus half the spread. A trader watching the chart sees a stop out at
a level that never appeared on screen and concludes the broker is hunting them.
Usually it is arithmetic, and the console says which of spread, latency, both,
or genuine price movement caused it.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | The engine. No Streamlit import, so it can sit behind the real bridge |
| `page.py` | The page, rendered by the hub |
| `../../tests/test_tv_mt5_bridge_diagnostic.py` | Engine tests, 62 checks |
| `../../tests/test_tv_mt5_bridge_diagnostic_page.py` | Page tests via AppTest, 25 checks |

## Determinism

Nothing in `core.py` reads the clock or a random source. The alert time comes
from the payload, `parse_alert` takes `now` for the case where the payload omits
it, and every timestamp downstream is that moment plus a latency the caller
supplies. A given input therefore produces the same trail on every run, which is
what makes the audit trail usable as evidence on a broker ticket.

## Rules worth keeping when this is extended

- Every table column holds one type, because the table widget serialises
  through Arrow and Arrow refuses a mixed column.
- Do not fold changing values into a selectbox's option labels. Streamlit
  stores the formatted label under the widget key, so a label that moves makes
  the stored value go stale and the widget silently resets.
- Button callbacks read live widget state out of `st.session_state` rather than
  arguments bound at render time. An argument captured on the previous render is
  one interaction stale, so a user who edits the payload and clicks parse would
  otherwise have the old text parsed.
