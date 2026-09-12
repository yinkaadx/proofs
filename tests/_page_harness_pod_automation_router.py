"""Test harness: runs the Print on Demand Automation Router page as a standalone Streamlit script,
the same way the hub renders it. AppTest.from_file needs a real script rather
than a function.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.pod_automation_router.page import render  # noqa: E402

render()
