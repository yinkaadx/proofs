"""Test harness: runs the multi channel inventory sync page as a standalone
Streamlit script, the same way the hub renders it. Used by
tests/test_multi_channel_inventory_sync_page.py through AppTest.from_file,
which needs a real script rather than a function.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.multi_channel_inventory_sync.page import render  # noqa: E402

render()
