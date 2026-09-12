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
```

Every suite declares its pass and fail markers before running and parses results
programmatically, so nothing is judged by eye. Clean runs print
`HUB RESULT: PASS 23/23`, `RESULT: PASS 164/164`, `UI RESULT: PASS 38/38` and
`PHP RESULT: PASS 30/30`, and each exits 0. Any failure prints lines beginning
`FAIL` and exits 1.

`tests/test_php_syntax.py` skips cleanly when no `php` binary is present.

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
