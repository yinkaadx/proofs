"""Test harness: runs the HubSpot B2B Network Architect page as a standalone
Streamlit script, the same way the hub renders it. AppTest.from_file needs a
real script rather than a function.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.hubspot_b2b_network_architect.page import render  # noqa: E402

render()
