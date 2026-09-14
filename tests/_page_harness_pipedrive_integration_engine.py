"""Test harness: runs the Pipedrive integration console as a standalone
Streamlit script, the same way the hub renders it. Used by
tests/test_pipedrive_integration_engine_page.py through AppTest.from_file,
which needs a real script rather than a function.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.pipedrive_integration_engine.page import render  # noqa: E402

render()
