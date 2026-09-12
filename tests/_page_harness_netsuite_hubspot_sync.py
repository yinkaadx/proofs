"""Test harness: runs the NetSuite HubSpot sync page as a standalone Streamlit
script, the same way the hub renders it. Used by
tests/test_netsuite_hubspot_sync_page.py through AppTest.from_file, which needs
a real script rather than a function.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.netsuite_hubspot_sync.page import render  # noqa: E402

render()
