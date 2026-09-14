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

## Tools

| Tool | Path | What it does |
| --- | --- | --- |
| [WP Form Debugger](tools/wp_form_debugger/README.md) | `/wp-form-debugger` | Finds why a WordPress form stopped submitting or delivering and returns the exact PHP, JavaScript and wp-config.php fix |
| [NetSuite HubSpot Idempotent Sync Console](tools/netsuite_hubspot_sync/README.md) | `/netsuite-hubspot-sync` | Deal sync simulator, account matching, SHA256 idempotency ledger and SharePoint audit feed |
| [Pipedrive API & Integration Console](tools/pipedrive_integration_engine/README.md) | `/pipedrive-integration-engine` | Direct webhook dispatch without Zapier, Sinch AI SMS threading with sales rep handover, opt out synchronizer and a Power BI incremental sync ledger |
| [EHR Cloud Security & WIF Architecture Console](tools/cloud_security_wif_console/README.md) | `/cloud-security-wif-console` | Azure to GCP Workload Identity Federation simulator, GCS Credential Access Boundary evaluator, Key Vault versus Managed Identity matrix and the IIS static key failure mode inspector |

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

Run from the repository root:

```bash
python3 tests/test_hub.py                     # hub, registry and navigation
python3 tests/test_wp_form_debugger_core.py   # WP Form Debugger engine
python3 tests/test_app_smoke.py               # WP Form Debugger page
python3 tests/test_php_syntax.py              # generated PHP parsed by real PHP
python3 tests/test_theme.py                   # one consistent design, needs a running hub
python3 tests/test_netsuite_hubspot_sync.py       # sync engine
python3 tests/test_netsuite_hubspot_sync_page.py  # sync console page

# The Pipedrive suites run under pytest as well as standalone
python3 -m pytest tests/test_pipedrive_integration_engine.py \
                 tests/test_pipedrive_integration_engine_page.py -q

python3 tests/test_keep_streamlit_awake.py    # the keep awake prober

python3 -m pytest tests/test_cloud_security_wif_console.py \
                 tests/test_cloud_security_wif_console_page.py -q
python3 tests/test_promote_workflow.py        # the promotion workflow guarantees
```

Every suite declares its pass and fail markers before running and parses results
programmatically, so nothing is judged by eye. Clean runs print
`HUB RESULT: PASS 56/56`, `RESULT: PASS 164/164`, `UI RESULT: PASS 38/38`,
`PHP RESULT: PASS 30/30`, `THEME RESULT: PASS 14/14`,
`SYNC RESULT: PASS 117/117`, `SYNC UI RESULT: PASS 28/28`,
`PIPEDRIVE RESULT: PASS 47/47`, `PIPEDRIVE UI RESULT: PASS 24/24`,
`KEEPALIVE UNIT RESULT: PASS 22/22`, `WIF RESULT: PASS 57/57`,
`WIF UI RESULT: PASS 30/30` and `PROMOTE RESULT: PASS 22/22`, and each exits 0.
Any failure prints lines beginning `FAIL` and exits 1.

`tests/test_php_syntax.py` skips cleanly when no `php` binary is present, and
`tests/test_theme.py` skips cleanly when no hub is running or no browser is
available. To run it, start the hub first and pass its URL:

```bash
streamlit run streamlit_app.py --server.port 8503 --server.headless true &
python3 tests/test_theme.py http://127.0.0.1:8503
```

## How a tool reaches the live app

Community Cloud builds one branch and one branch only. Until work reaches that
branch it is invisible to every visitor, which is the gap that used to be closed
by hand. `.github/workflows/promote-to-deploy.yml` closes it automatically, and
only ever behind a green run.

On a push to any `claude/**` branch it checks whether that branch is already
contained in the deploy branch and stops if it is. Otherwise it runs every suite
in `tests/` the way this repository runs them, as `python3 tests/<file>.py`,
because most of them are script style and call `sys.exit` at module level, which
makes a bare `pytest tests/` error during collection and report a red run that
says nothing about the code. `tests/test_theme.py` is skipped: it needs a live
hub and a real browser.

Only if every suite passed does it merge the session branch into the deploy
branch with `--no-ff` and push. It never rebases, never force pushes and never
amends, because the live app is built from that branch and a rewritten history
under a running deploy makes a rollback impossible to reason about. A conflict
aborts the merge, names the conflicting files and pushes nothing.

It then runs the keep awake prober in the same job. A push made with
`GITHUB_TOKEN` does not trigger other workflows, so `keep-awake.yml` will not
fire for that push, and without this step a freshly merged tool would sit
undeployed until something else happened to wake the app.

`tests/test_promote_workflow.py` parses the workflow and holds each of those
guarantees in place, including the two that are easy to get wrong: the merge
step must name the test gate's own green output as a precondition rather than
relying on an implicit one, and every git command in the merge step must be
checked, because that script cannot use `set -e` and an unchecked failed push
would otherwise report a success that never happened.

## Staying awake, and staying current

Community Cloud sleeps an app after a spell with no visitor, and a plain HTTP
ping does not count as one: the app URL answers 200 from a static shell whether
the app is running or fast asleep, so an ordinary uptime monitor reports green
on a sleeping app. `.github/workflows/keep-awake.yml` drives a real headless
Chromium instead, every three hours and on every push.

One run opens each app in the list, clicks "Yes, get this app back up!" if the
sleep screen is showing, waits for the app itself to render inside the app
frame, then dwells long enough for Community Cloud to count the session as a
visit, which is what resets the inactivity clock. It saves a screenshot and a
JSON summary as evidence on every run.

The same run also proves the live app is current. It reads the tool list from
the branch Community Cloud actually deploys from, opens every registered tool
URL, and checks the tool really rendered. Streamlit does not error on an
unknown page path, it quietly serves the landing page instead, so the landing
page is the tell: if `/some-tool` renders the same main column as `/`, the
running app has never heard of that tool and the deploy is stale. That is the
failure that used to be found only by rebooting the app by hand.

| Item | Value |
| --- | --- |
| Apps kept awake | `.github/streamlit-apps.txt`, one URL per line |
| Schedule | Every three hours, plus every push, plus manual dispatch |
| Prober | `scripts/keep_streamlit_awake.py` |
| Evidence | Screenshot and `keepalive-summary.json`, kept as a run artifact |

Adding a future app to the guarantee is one line in
`.github/streamlit-apps.txt`. Nothing else changes.

Two limits worth knowing. GitHub only runs scheduled workflows from the
default branch, so the cron fires from `main`; pushes to any branch also
trigger it. And GitHub disables scheduled workflows in a repository with no
commits for 60 days, so a repository left completely idle for two months needs
one commit to re arm the schedule.

## Layout

| Path | Purpose |
| --- | --- |
| `streamlit_app.py` | The hub: page config, navigation, landing page. The permanent deploy target |
| `tools/registry.py` | The one place a tool is declared |
| `tools/<tool>/page.py` | A tool's Streamlit page, exposing `render()` |
| `tools/<tool>/core.py` | A tool's logic, with no Streamlit dependency |
| `shared/theme.py` | The shared stylesheet and helpers, so every tool looks like one product |
| `scripts/keep_streamlit_awake.py` | Wakes every live app and proves the deploy is current |
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
