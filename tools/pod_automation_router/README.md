# Print on Demand Automation Router

An order lands from WooCommerce, becomes a Printful fulfilment request, and the
tracking number that comes back is pushed to the shop and to the marketplace the
buyer actually ordered from.

Live at `/pod-automation-router` on the hub.

## The three jobs

**Translation.** WooCommerce speaks in SKUs and sizes, Printful in numeric
variant ids, and the postcode is called `zip` on the other side. The translation
is the only place those differences should exist. It refuses rather than
half succeeds: an unmapped size, an empty order or an incomplete address holds
the order, because a partial submission ships an incomplete parcel with nothing
in the log to explain it, and guessing a size ships the wrong garment.

**The stock exception guard.** Printful answers an out of stock blank with an
error, and the naive integration swallows it: the shop shows the order as
processing, nothing is printed, and the first person to notice is the buyer.
Here it becomes an alert naming the SKU, the size, what is in stock, what was
needed, and what to do about it.

**Tracking synchronisation.** WooCommerce is the shop of record and always
receives the tracking number. The originating marketplace receives it too,
because that is where the buyer will look. No other channel is touched: pushing
to a marketplace that never saw the order fails on their API and buries real
errors in noise.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | Translation, the stock guard, tracking sync and the router log. No Streamlit import |
| `page.py` | The four tab console |
| `../../tests/test_pod_automation_router.py` | Engine tests, pytest |
| `../../tests/test_pod_automation_router_page.py` | Page tests via AppTest, pytest |

These suites are written for pytest:

```bash
python3 -m pytest tests/test_pod_automation_router.py \
                 tests/test_pod_automation_router_page.py -q
```

The catalogue deliberately seeds two blanks with zero stock, so the guard can be
demonstrated rather than described.
