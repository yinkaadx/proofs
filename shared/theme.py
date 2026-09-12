"""Shared visual system: one stylesheet and one set of helpers, so every
tool in the hub looks like part of the same product.

The palette follows the theme Streamlit actually painted, in light and in dark.

Streamlit exposes no CSS variables for its theme, but it does stamp the active
theme onto `.stApp` as `color-scheme`, and that property inherits. `light-dark()`
resolves against it, so our colours track Streamlit's rather than the device's.
This matters: a `prefers-color-scheme` rule here once produced dark cards on
Streamlit's light page beside a light sidebar, and it would return the moment a
visitor overrode the theme in Streamlit's own menu. tests/test_theme.py measures
real rendered colours under both settings and holds the two together.

Import `esc` before interpolating anything user supplied into an
`unsafe_allow_html` block. Pasted markup rendered raw would become live HTML.
"""

from __future__ import annotations

from html import escape

import streamlit as st

STYLE = """
<style>
/* Light values alone, so a browser without light-dark() still gets a complete
   and internally consistent palette rather than a broken one. */
:root {
  --app-ink: #0f172a;
  --app-muted: #64748b;
  --app-line: #e2e8f0;
  --app-accent: #1d4ed8;
  --app-crit: #b91c1c;
  --app-warn: #b45309;
  --app-ok: #047857;
  --app-card: #ffffff;
  --app-soft: #f8fafc;
}
/* Streamlit stamps its ACTIVE theme onto .stApp as `color-scheme`, and
   color-scheme inherits, so light-dark() resolves against the theme Streamlit
   actually painted rather than against the device. That is the whole point:
   a visitor who overrides the theme in Streamlit's own menu still gets one
   coherent design. */
@supports (color: light-dark(#000, #fff)) {
  :root {
    --app-ink: light-dark(#0f172a, #e6edf7);
    --app-muted: light-dark(#64748b, #9aa8bd);
    --app-line: light-dark(#e2e8f0, #2b3648);
    --app-accent: light-dark(#1d4ed8, #7aa7ff);
    --app-crit: light-dark(#b91c1c, #ff8a8a);
    --app-warn: light-dark(#b45309, #f5b544);
    --app-ok: light-dark(#047857, #46d4a0);
    --app-card: light-dark(#ffffff, #151c28);
    --app-soft: light-dark(#f8fafc, #0f1622);
  }
}
.app-hero {
  border: 1px solid var(--app-line);
  border-left: 4px solid var(--app-accent);
  border-radius: 10px;
  padding: 1.35rem 1.6rem;
  background: var(--app-card);
  margin-bottom: 1.25rem;
}
.app-hero h1 {
  margin: 0 0 .3rem 0;
  font-size: 1.65rem;
  letter-spacing: -.02em;
  color: var(--app-ink);
}
.app-hero p { margin: 0; color: var(--app-muted); font-size: .95rem; line-height: 1.5; }
.app-kpis { display: flex; gap: .75rem; flex-wrap: wrap; margin: .25rem 0 1rem 0; }
.app-kpi {
  flex: 1 1 150px;
  border: 1px solid var(--app-line);
  border-radius: 10px;
  padding: .85rem 1rem;
  background: var(--app-card);
}
.app-kpi .n { font-size: 1.7rem; font-weight: 650; line-height: 1.1; color: var(--app-ink); }
.app-kpi .l {
  font-size: .72rem; text-transform: uppercase; letter-spacing: .09em;
  color: var(--app-muted); margin-top: .2rem;
}
.app-kpi.crit .n { color: var(--app-crit); }
.app-kpi.warn .n { color: var(--app-warn); }
.app-kpi.ok .n { color: var(--app-ok); }
.app-card {
  border: 1px solid var(--app-line);
  border-radius: 10px;
  padding: 1rem 1.15rem;
  background: var(--app-card);
  margin-bottom: .8rem;
}
.app-card.crit { border-left: 4px solid var(--app-crit); }
.app-card.warn { border-left: 4px solid var(--app-warn); }
.app-card.info { border-left: 4px solid var(--app-accent); }
.app-card h4 { margin: 0 0 .35rem 0; font-size: 1rem; color: var(--app-ink); }
.app-card p { margin: .3rem 0; color: var(--app-ink); font-size: .9rem; line-height: 1.55; }
.app-tag {
  display: inline-block; font-size: .68rem; font-weight: 700;
  text-transform: uppercase; letter-spacing: .08em;
  padding: .12rem .5rem; border-radius: 999px; margin-right: .5rem;
}
.app-tag.crit { background: color-mix(in srgb, var(--app-crit) 14%, transparent); color: var(--app-crit); }
.app-tag.warn { background: color-mix(in srgb, var(--app-warn) 14%, transparent); color: var(--app-warn); }
.app-tag.info { background: color-mix(in srgb, var(--app-accent) 14%, transparent); color: var(--app-accent); }
.app-ev {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: .8rem; background: var(--app-soft);
  border: 1px solid var(--app-line); border-radius: 6px;
  padding: .45rem .6rem; margin: .45rem 0; color: var(--app-ink);
  overflow-x: auto; white-space: pre-wrap; word-break: break-word;
}
.app-stage {
  display: flex; gap: .7rem; align-items: baseline;
  border-bottom: 1px solid var(--app-line); padding: .55rem .1rem;
}
.app-stage:last-child { border-bottom: 0; }
.app-stage .s { font-weight: 700; font-size: .72rem; letter-spacing: .07em; min-width: 4.2rem; }
.app-stage .s.pass { color: var(--app-ok); }
.app-stage .s.warn { color: var(--app-warn); }
.app-stage .s.fail { color: var(--app-crit); }
.app-stage .s.skip { color: var(--app-muted); }
.app-stage .n { font-weight: 600; min-width: 11rem; color: var(--app-ink); font-size: .88rem; }
.app-stage .d { color: var(--app-muted); font-size: .86rem; line-height: 1.5; }
.app-foot {
  border-top: 1px solid var(--app-line); margin-top: 2rem; padding-top: .9rem;
  color: var(--app-muted); font-size: .78rem; line-height: 1.6;
}
@media (max-width: 640px) {
  .app-stage { flex-direction: column; gap: .15rem; }
  .app-stage .n { min-width: 0; }
}

/* Hub landing page */
.app-tools { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: .85rem; margin: .4rem 0 1rem 0; }
.app-tool {
  border: 1px solid var(--app-line);
  border-radius: 10px;
  padding: 1rem 1.15rem;
  background: var(--app-card);
  display: flex; flex-direction: column; gap: .35rem;
}
.app-tool .ic { font-size: 1.4rem; line-height: 1; }
.app-tool h3 { margin: 0; font-size: 1.02rem; color: var(--app-ink); letter-spacing: -.01em; }
.app-tool p { margin: 0; color: var(--app-muted); font-size: .88rem; line-height: 1.5; }
.app-pill {
  display: inline-block; font-size: .66rem; font-weight: 700; letter-spacing: .09em;
  text-transform: uppercase; padding: .14rem .5rem; border-radius: 999px;
  background: color-mix(in srgb, var(--app-accent) 14%, transparent);
  color: var(--app-accent); align-self: flex-start;
}
</style>
"""


def inject() -> None:
    """Apply the shared stylesheet. Safe to call more than once."""
    st.markdown(STYLE, unsafe_allow_html=True)


def esc(text: str) -> str:
    """Escape text destined for an unsafe_allow_html block."""
    return escape(str(text or ""), quote=True)


def hero(title: str, subtitle: str) -> None:
    """Render the standard page header."""
    st.markdown(
        f'<div class="app-hero"><h1>{esc(title)}</h1><p>{esc(subtitle)}</p></div>',
        unsafe_allow_html=True,
    )
