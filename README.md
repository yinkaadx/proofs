# Toolbench

A single Streamlit app that hosts every client facing diagnostic tool in this
repository. It is deployed once and never needs deploying again: each new tool
becomes a page inside it and goes live on the next push.

**Live: https://proofs-toolbench.streamlit.app**

## Deployment facts

| Item | Value |
| --- | --- |
| Live URL | https://proofs-toolbench.streamlit.app |
| Host | Streamlit Community Cloud, signed in with GitHub |
| Entry file | `streamlit_app.py` at the repository root |
| Deploy branch | `claude/kind-feynman-489x72` |
| Redeploy | Automatic on every push to that branch |

Changing the entry file path or the branch means recreating the app, so both
stay as they are. Adding tools never touches either.

## Why a hub

Streamlit Community Cloud has no API, so creating an app is a manual step in the
browser. Creating one app per tool means one manual step per tool, forever. One
hub app means that step happens once in total, and every tool after that ships
by pushing code.

## Tool URLs

Never compose a tool URL by hand. Print them from the registry, which is the
same source the hub builds its navigation from:

```bash
python3 scripts/urls.py                 # live hub
python3 scripts/urls.py http://127.0.0.1:8600
```

A path is only *proven* once `tests/test_tool_urls.py` has opened it in a real
browser. That suite walks the registry, so every tool is covered automatically
the moment it is registered, and it fails if a page is Streamlit's not found
fallback, if a tool's title is missing, or if the hub is serving an older commit
than the checkout. It also checks that a deliberately invalid path still reports
not found, so the suite cannot pass vacuously.

The hub prints the commit it is running in its sidebar, as `build <sha>`. If
that does not match the commit you expect, the deployment is stale and needs a
reboot, not a fix.

## Tools

| Tool | Path | What it does |
| --- | --- | --- |
| [WP Form Debugger](tools/wp_form_debugger/README.md) | `/wp-form-debugger` | Finds why a WordPress form stopped submitting or delivering and returns the exact PHP, JavaScript and wp-config.php fix |
| [NetSuite HubSpot Idempotent Sync Console](tools/netsuite_hubspot_sync/README.md) | `/netsuite-hubspot-sync` | Deal sync simulator, account matching, SHA256 idempotency ledger and SharePoint audit feed |
| [Multi Channel Inventory Sync Engine](tools/multi_channel_inventory_sync/README.md) | `/multi-channel-inventory-sync` | Cross platform SKU mapping, stock deduction and negative inventory prevention for Amazon, eBay and Shopify |
| [Zero Trust Remote Access Console](tools/zero_trust_rmm_console/README.md) | `/zero-trust-rmm-console` | Multitenant RBAC simulator, MFA enforcement ledger and ad hoc session code generator |
| [Print on Demand Automation Router](tools/pod_automation_router/README.md) | `/pod-automation-router` | WooCommerce payload routing, Printful fulfilment simulation and multi channel tracking sync |

## Adding a tool

Three steps, no deployment and no settings change:

1. Create `tools/<your_tool>/` with a `page.py` exposing `render() -> None`, and
   keep the analysis logic in a separate module with no Streamlit import so it
   stays testable on its own.
2. Add one `Tool(...)` entry to `all_tools()` in `tools/registry.py`.
3. Push. The hub picks it up: navigation entry, landing page card and its own
   URL at `/<key>`.

The hub reads everything from the registry and hardcodes no tool, which
`tests/test_hub.py` asserts directly, so step 2 is genuinely the only wiring.

## Run locally

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

The hub serves on http://localhost:8501, and each tool at
http://localhost:8501/<key>.

## Tests

One command runs everything and prints a single summary:

```bash
scripts/check.sh          # every suite, about 17 seconds
scripts/check.sh fast     # skip the two browser suites, about 8 seconds
```

It reuses a hub on port 8600 if one is already running and starts one only if
needed, because starting Streamlit and waiting for it to answer was a large
part of the wall clock on earlier builds.

To add a tool, generate its scaffold rather than writing the eight files by
hand, then replace the placeholders:

```bash
python3 scripts/new_tool.py --key my-tool --title "My Tool" \
    --tagline "What it does, in one sentence." \
    --audience "Who it is for" --icon "🧰"
```

Individual suites, run from the repository root:

```bash
python3 tests/test_hub.py                     # hub, registry and navigation
python3 tests/test_wp_form_debugger_core.py   # WP Form Debugger engine
python3 tests/test_app_smoke.py               # WP Form Debugger page
python3 tests/test_php_syntax.py              # generated PHP parsed by real PHP
python3 tests/test_theme.py                   # one consistent design, needs a running hub
python3 tests/test_netsuite_hubspot_sync.py       # sync engine
python3 tests/test_netsuite_hubspot_sync_page.py  # sync console page
python3 tests/test_multi_channel_inventory_sync.py       # inventory engine
python3 tests/test_multi_channel_inventory_sync_page.py  # inventory console page
python3 tests/test_browser_inventory_flow.py             # real browser flow, needs a hub
```

Every suite declares its pass and fail markers before running and parses results
programmatically, so nothing is judged by eye. Clean runs print
`HUB RESULT: PASS 46/46`, `RESULT: PASS 164/164`, `UI RESULT: PASS 38/38`,
`PHP RESULT: PASS 30/30`, `THEME RESULT: PASS 14/14`,
`SYNC RESULT: PASS 117/117`, `SYNC UI RESULT: PASS 28/28`,
`INVENTORY RESULT: PASS 132/132`, `INVENTORY UI RESULT: PASS 52/52` and
`BROWSER RESULT: PASS 11/11`, 632 checks in total, and each exits 0. Any
failure prints lines beginning `FAIL` and exits 1.

Two suites need a running hub and a browser, and skip cleanly without them:
`test_theme.py` and `test_browser_inventory_flow.py`. AppTest re-executes the
script directly and cannot see widget state lost across a real rerun, which is
why the browser suite exists.

`tests/test_php_syntax.py` skips cleanly when no `php` binary is present, and
`tests/test_theme.py` skips cleanly when no hub is running or no browser is
available. To run it, start the hub first and pass its URL:

```bash
streamlit run streamlit_app.py --server.port 8503 --server.headless true &
python3 tests/test_theme.py http://127.0.0.1:8503
```

## Layout

| Path | Purpose |
| --- | --- |
| `streamlit_app.py` | The hub: page config, navigation, landing page. The permanent deploy target |
| `tools/registry.py` | The one place a tool is declared |
| `tools/<tool>/page.py` | A tool's Streamlit page, exposing `render()` |
| `tools/<tool>/core.py` | A tool's logic, with no Streamlit dependency |
| `shared/theme.py` | The shared stylesheet and helpers, so every tool looks like one product |
| `tests/` | All test suites |
| `.streamlit/config.toml` | Theme and client settings |
| `requirements.txt` | Runtime dependencies |

The HTML files at the repository root are separate static proof pages published
by GitHub Pages. They are unrelated to the hub and unaffected by it.

## Security posture

- Anything a user pastes is HTML escaped before it reaches an
  `unsafe_allow_html` block, so untrusted markup cannot become live elements.
- Generated PHP is a fragment for a file that has already opened a PHP block, so
  no snippet carries its own opening tag, and every interpolated value is
  escaped as a valid PHP string literal.
- Downloadable reports redact credentials. Redaction is the default in the
  generator, so a caller that forgets cannot leak one.

## Light and dark, bound to Streamlit's actual theme

The app follows each visitor's device, in light and in dark, and the two halves
of the page cannot drift apart.

Streamlit exposes no CSS variables for its theme, so our stylesheet cannot read
the colours it painted. It does, however, stamp the active theme onto `.stApp`
as `color-scheme`, and that property inherits. The palette in `shared/theme.py`
is therefore expressed with `light-dark()`, which resolves against that
inherited value, so our components follow the theme Streamlit actually rendered
rather than the device.

The distinction matters. An earlier version keyed the palette to
`prefers-color-scheme`, which produced dark cards on Streamlit's light page
beside a light sidebar for any visitor whose device was set to dark. Keying to
the device would also break again the moment someone overrode the theme in
Streamlit's own menu.

`tests/test_theme.py` drives a real browser under both device settings and
checks that every surface agrees on one scheme and that text on those surfaces
clears WCAG AA contrast.
