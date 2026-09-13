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

# The tool list lives in a top bar, not in the sidebar, so the sidebar belongs
# to the tool you are actually using.
#
# It sat in the sidebar until sixteen tools made that untenable. The list alone
# ran to 563 pixels before the current tool's "How to use" even began, and at a
# larger text size it pushed the help off the bottom entirely. Reordering the
# sidebar did not fix that, because the order was never the problem: a list
# that grows every week was sitting in front of the one block that changes with
# the page you are on.
#
# The top bar reads better too. It shows every tool name in full, where the
# sidebar truncated the longer ones with an ellipsis.
st.navigation({"Overview": [HOME_PAGE], "Tools": TOOL_PAGES},
              position="top").run()

with st.sidebar:
    st.divider()
    st.markdown("### Toolbench")
    st.caption("Diagnostic tools for client work.")
    # State the running build. A hosted Streamlit app can serve an old commit
    # without saying so, which has already caused a tool to be reported live at
    # a URL the running app had never heard of.
    st.caption(build_label())
