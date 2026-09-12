"""Toolbench: the hub app.

This file is the permanent deploy target. Streamlit Community Cloud is pointed
at it once, and every tool added to `tools/registry.py` afterwards goes live on
the next push with no further setup.
"""

from __future__ import annotations

import streamlit as st

from shared import theme
from shared.build_info import build_label
from tools.registry import Tool, all_tools

st.set_page_config(
    page_title="Toolbench",
    page_icon="🧰",
    layout="wide",
    initial_sidebar_state="expanded",
)
theme.inject()

TOOLS: list[Tool] = all_tools()


def home() -> None:
    theme.inject()
    theme.hero(
        "Toolbench",
        "Diagnostic tools built for client work. Each one runs in the browser, "
        "keeps what you paste inside your session, and ends with something you "
        "can hand over: an exact fix, or a report.",
    )

    st.markdown(f"#### {len(TOOLS)} tool{'' if len(TOOLS) == 1 else 's'} available")

    columns = st.columns(min(3, max(1, len(TOOLS))))
    for index, (tool, page) in enumerate(zip(TOOLS, TOOL_PAGES)):
        with columns[index % len(columns)]:
            st.markdown(
                f'<div class="app-tool">'
                f'<div class="ic">{theme.esc(tool.icon)}</div>'
                f'<h3>{theme.esc(tool.title)}</h3>'
                f'<p>{theme.esc(tool.tagline)}</p>'
                f'<span class="app-pill">{theme.esc(tool.audience)}</span>'
                f'</div>',
                unsafe_allow_html=True,
            )
            st.page_link(page, label=f"Open {tool.title}", icon=tool.icon)

    st.markdown(
        '<div class="app-foot">Nothing you paste into these tools leaves your '
        'session: analysis runs in the app, no connection is opened to your '
        'site and no credential is stored. Always apply fixes on staging '
        'first.</div>',
        unsafe_allow_html=True,
    )


TOOL_PAGES = [
    st.Page(t.render, title=t.title, icon=t.icon, url_path=t.key)
    for t in TOOLS
]

HOME_PAGE = st.Page(home, title="Home", icon="🧰", url_path="home", default=True)

with st.sidebar:
    st.markdown("### Toolbench")
    st.caption("Diagnostic tools for client work.")
    # State the running build. A hosted Streamlit app can serve an old commit
    # without saying so, which has already caused a tool to be reported live at
    # a URL the running app had never heard of.
    st.caption(build_label())

st.navigation({"Overview": [HOME_PAGE], "Tools": TOOL_PAGES}).run()
