"""Test harness: runs the EHR cloud security and WIF console as a standalone
Streamlit script, the same way the hub renders it. Used by
tests/test_cloud_security_wif_console_page.py through AppTest.from_file,
which needs a real script rather than a function.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.cloud_security_wif_console.page import render  # noqa: E402

render()
