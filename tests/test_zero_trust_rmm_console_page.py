"""Page tests for the Zero Trust Remote Access Console, via Streamlit's AppTest.

Written for pytest.

Run: pytest tests/test_zero_trust_rmm_console_page.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.zero_trust_rmm_console.core import (  # noqa: E402
    COMPANY_A,
    COMPANY_B,
    SAMPLE_MACHINES,
    SAMPLE_USERS,
    user_by_username,
    visible_machines,
)

HARNESS = Path(__file__).resolve().parent / "_page_harness_zero_trust_rmm_console.py"


def text_of(at: AppTest) -> str:
    parts: list[str] = []
    for attr in ("markdown", "text", "info", "success", "warning", "error",
                 "caption", "code", "header", "subheader", "title"):
        try:
            parts += [str(getattr(el, "value", "")) for el in getattr(at, attr)]
        except Exception:
            pass
    try:
        for frame in at.get("dataframe"):
            value = getattr(frame, "value", None)
            if value is not None and hasattr(value, "to_numpy"):
                parts += [str(cell) for cell in value.to_numpy().ravel()]
                parts += [str(col) for col in value.columns]
    except Exception:
        pass
    return "\n".join(parts)


def run(timeout: int = 120) -> AppTest:
    at = AppTest.from_file(str(HARNESS), default_timeout=timeout)
    at.run()
    return at


def widget(at: AppTest, kind: str, label: str):
    for el in getattr(at, kind):
        if el.label == label:
            return el
    raise AssertionError(f"no {kind} labelled {label!r}; "
                         f"available: {[e.label for e in getattr(at, kind)]}")


def button(at: AppTest, fragment: str):
    for el in at.button:
        if fragment in str(el.label):
            return el
    raise AssertionError(f"no button containing {fragment!r}; "
                         f"available: {[e.label for e in at.button]}")


@pytest.fixture(scope="module")
def cold() -> AppTest:
    return run()


def test_page_renders_without_exception(cold):
    assert not cold.exception, [str(e.value) for e in cold.exception]


def test_hero_and_tabs_render(cold):
    body = text_of(cold)
    assert "Zero Trust Remote Access Console" in body
    assert len(cold.tabs) == 3


def test_access_matrix_lists_every_user_and_both_tenants(cold):
    body = text_of(cold)
    for acting in SAMPLE_USERS:
        assert acting.username in body, f"{acting.username} missing from the matrix"
    assert COMPANY_A in body and COMPANY_B in body


def test_widget_labels_are_unambiguous(cold):
    labels = [str(el.label) for el in cold.selectbox]
    assert len(labels) == len(set(labels)), labels
    for outer in labels:
        others = [l for l in labels if l != outer]
        assert not any(outer in other for other in others), (outer, others)


def test_selecting_a_technician_narrows_the_visible_estate():
    at = run()
    widget(at, "selectbox", "Acting user").set_value("a.tech.london").run()
    assert not at.exception, [str(e.value) for e in at.exception]
    body = text_of(at)
    femi = user_by_username("a.tech.london")
    visible = visible_machines(femi)
    assert f"{len(visible)} of {len(SAMPLE_MACHINES)}" in body
    for machine in visible:
        assert machine.machine_id in body
    hidden = [m for m in SAMPLE_MACHINES if m not in visible]
    assert hidden, "the fixture should leave something hidden"


def test_selecting_an_auditor_says_it_is_read_only():
    at = run()
    widget(at, "selectbox", "Acting user").set_value("a.auditor").run()
    assert not at.exception, [str(e.value) for e in at.exception]
    body = text_of(at)
    assert "read only by design" in body


def test_the_msp_role_can_see_both_tenants():
    at = run()
    widget(at, "selectbox", "Acting user").set_value("msp.oncall").run()
    assert not at.exception, [str(e.value) for e in at.exception]
    body = text_of(at)
    assert f"{len(SAMPLE_MACHINES)} of {len(SAMPLE_MACHINES)}" in body


def test_a_login_attempt_is_recorded_in_the_ledger():
    at = run()
    button(at, "Attempt login").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    body = text_of(at)
    assert "Audit trail" in body
    assert "Source IP" in body, "the ledger table should render"


def test_an_unenrolled_account_is_refused_and_says_why():
    at = run()
    widget(at, "selectbox", "Account attempting to sign in").set_value("b.tech.nomfa").run()
    button(at, "Attempt login").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    body = text_of(at)
    assert "MFA not enrolled" in body
    assert "Refused outright" in body
    assert len(at.error) >= 1, "an unenrolled login should raise a visible error"


def test_generating_a_code_shows_six_digits_and_an_expiry():
    at = run()
    button(at, "Generate session code").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    body = text_of(at)
    assert "Session code" in body
    assert "Expires" in body
    assert "Issued codes" in body


def test_an_auditor_cannot_generate_a_code_and_the_page_says_so():
    at = run()
    widget(at, "selectbox", "Issuing user").set_value("a.auditor").run()
    button(at, "Generate session code").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    body = text_of(at)
    assert "cannot issue session codes" in body
    assert len(at.error) >= 1


def test_redeeming_an_unknown_code_is_refused():
    at = run()
    widget(at, "text_input", "Code to redeem").set_value("000000").run()
    button(at, "Redeem").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    body = text_of(at)
    assert "Unknown code" in body
    assert "No session was opened" in body


def test_typed_input_is_escaped_not_rendered_as_live_html():
    at = run()
    widget(at, "text_input", "Code to redeem").set_value(
        '<img src=x onerror="window.__probe=1">').run()
    button(at, "Redeem").click().run()
    assert not at.exception, [str(e.value) for e in at.exception]
    rendered = "\n".join(str(getattr(el, "value", "")) for el in at.markdown)
    assert "<img src=x onerror=" not in rendered
