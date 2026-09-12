# Multi Channel Inventory Sync Engine

One stock figure at the centre, three marketplaces that each call the same
product something different. Sell on any channel and the engine deducts once,
then pushes the new quantity to the other two. Ask for more than exists and it
refuses.

Live at `/multi-channel-inventory-sync` on the hub.

## The three problems it answers

**Which product did this channel just sell?** Amazon, eBay and Shopify each
invent their own code for the same item, and sellers rarely type them
consistently. Mapping accepts either a channel SKU or a marketplace listing ID,
ignores case and whitespace, and refuses to resolve an identifier across
channels, so an Amazon SKU can never accidentally match an eBay listing. An
unmapped identifier blocks the order rather than guessing, because deducting
from the wrong SKU oversells a different product.

**How do the other channels find out?** A sale deducts once at the centre, then
broadcasts the new figure to every other channel that carries the SKU. The
selling channel is not written back to, since it already decremented its own
copy when it took the order. A product listed on two channels broadcasts to one,
never to a channel where it has no listing.

**What stops an oversell?** The guard runs before anything is written. If the
requested quantity exceeds what is on hand, the order is refused and the ledger
is left byte for byte as it was, with an error naming the SKU, what remains and
the shortfall. Stock can be taken to exactly zero, never past it. A partially
applied order is worse than a refused one, because the centre and the channels
would then disagree about reality.

## Failed syncs

Push a channel offline from the Channel Sales tab and sell again. The sale still
completes and the centre stays correct, but that channel's update fails and is
queued, which is what fills the error log. Bring it back and replay: the queued
write carries the current figure rather than the stale one that failed.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | Mapping, deduction, the guard, broadcasting and the audit trail. No Streamlit import |
| `page.py` | The four tab console |
| `../../tests/test_multi_channel_inventory_sync.py` | Engine tests, 132 checks |
| `../../tests/test_multi_channel_inventory_sync_page.py` | Page tests via AppTest, 52 checks |
| `../../tests/test_browser_inventory_flow.py` | Real browser flow, 11 checks |

## A note on the browser test

The page suite drives Streamlit's AppTest, which re-executes the script
directly. That missed a real defect. A selectbox whose option labels carried a
live stock count stored a stale formatted label under its key, so after a sale
the widget fell back to the first product and the next order went against the
wrong SKU. Only a browser, clicking twice in sequence, exposed it. Option labels
are now constant and the stock count sits beside the control, and
`test_browser_inventory_flow.py` holds that behaviour in a real browser.
