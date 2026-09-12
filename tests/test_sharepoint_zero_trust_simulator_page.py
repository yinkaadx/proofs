"""Page tests for the Secure SharePoint Architecture Console, via AppTest.

Written for pytest.

Run: pytest tests/test_sharepoint_zero_trust_simulator_page.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.sharepoint_zero_trust_simulator.core import (  # noqa: E402
    ADMIN,
    BAND_BLOCKED,
    BAND_READY,
    BLOCKED_COUNTRIES,
    CLIENT_LEGACY,
    DEFAULT_TENANT,
    DEVICE_BLOCK,
    EMPLOYEE,
    GUEST,
    HARDENED_TENANT,
    LINK_SPECIFIC,
    MEMBER,
    NO_ACCESS,
    OWNER,
    RISK_HIGH,
    SHARING_DISABLED,
    UNAUTHORIZED,
    USER_TYPES,
)
from tools.sharepoint_zero_trust_simulator.page import STATE  # noqa: E402

HARNESS = (Path(__file__).resolve().parent
           / "_page_harness_sharepoint_zero_trust_simulator.py")


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
                         f"have {[el.label for el in getattr(at, kind)]}")


def press(at: AppTest, label: str) -> AppTest:
    for button in at.button:
        if button.label == label:
            return button.click().run()
    raise AssertionError(f"no button labelled {label!r}; "
                         f"have {[b.label for b in at.button]}")


def hardened() -> AppTest:
    return press(run(), "Load the hardened posture")


def signed_in(user_type: str = EMPLOYEE, at: AppTest | None = None,
              **controls) -> AppTest:
    """Drive the login tab the way a person would, on a hardened tenant."""
    at = at or hardened()
    widget(at, "selectbox", "User type").set_value(user_type).run()
    for label, value in controls.items():
        kind = "checkbox" if isinstance(value, bool) else "selectbox"
        widget(at, kind, label.replace("_", " ")).set_value(value).run()
    return at


def config_of(at: AppTest):
    return at.session_state[STATE]["config"]


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------

def test_the_page_renders_with_no_exception():
    at = run()
    assert not at.exception, [str(e.value) for e in at.exception]


def test_the_hero_and_the_three_tabs_are_present():
    at = run()
    assert "Secure SharePoint Architecture Console" in text_of(at)
    labels = [tab.label for tab in at.tabs]
    for expected in ("Entra ID Login", "Access Matrix", "Deployment Audit"):
        assert expected in labels


def test_it_opens_on_the_microsoft_default_posture_which_fails_its_audit():
    at = run()
    assert config_of(at) == DEFAULT_TENANT
    assert BAND_BLOCKED in text_of(at)


def test_no_two_widgets_of_a_kind_share_a_label():
    at = run()
    for kind in ("selectbox", "checkbox", "number_input", "button"):
        labels = [el.label for el in getattr(at, kind)]
        assert len(labels) == len(set(labels)), f"{kind}: {labels}"


# ---------------------------------------------------------------------------
# Entra ID login simulator
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("user_type", USER_TYPES)
def test_every_user_type_produces_a_decision_without_crashing(user_type):
    at = signed_in(user_type)
    assert not at.exception, [str(e.value) for e in at.exception]
    body = text_of(at)
    assert ("Access granted" in body) or ("Access blocked" in body)


def test_an_admin_is_granted_owner_and_an_employee_member():
    assert f"Access granted as {OWNER}" in text_of(signed_in(ADMIN))
    assert f"Access granted as {MEMBER}" in text_of(signed_in(EMPLOYEE))


def test_an_unauthorized_user_is_blocked_at_the_directory():
    body = text_of(signed_in(UNAUTHORIZED))
    assert "Access blocked by Directory lookup" in body
    assert "not in the tenant directory" in body


def test_a_blocked_sign_in_reaches_no_libraries_on_screen():
    body = text_of(signed_in(UNAUTHORIZED))
    assert "This session may" not in body


def test_a_failed_second_factor_blocks_the_sign_in():
    at = signed_in(EMPLOYEE)
    widget(at, "checkbox", "Second factor satisfied").set_value(False).run()
    body = text_of(at)
    assert "Access blocked by Require multi factor authentication" in body


def test_a_legacy_client_is_blocked_on_a_hardened_tenant():
    at = signed_in(EMPLOYEE)
    widget(at, "selectbox", "Client").set_value(CLIENT_LEGACY).run()
    assert "Block legacy authentication" in text_of(at)
    assert "Access blocked" in text_of(at)


def test_an_untrusted_location_blocks_the_sign_in():
    at = signed_in(EMPLOYEE)
    widget(at, "selectbox", "Sign in location").set_value(
        BLOCKED_COUNTRIES[0]).run()
    assert "Access blocked by Named location restriction" in text_of(at)


def test_high_risk_blocks_the_sign_in():
    at = signed_in(EMPLOYEE)
    widget(at, "selectbox", "Sign in risk").set_value(RISK_HIGH).run()
    assert "Access blocked by Sign in risk" in text_of(at)


def test_an_admin_on_an_unmanaged_device_is_blocked_but_an_employee_is_limited():
    admin = signed_in(ADMIN)
    widget(admin, "checkbox", "Device is enrolled and compliant").set_value(
        False).run()
    assert "Access blocked" in text_of(admin)

    employee = signed_in(EMPLOYEE)
    widget(employee, "checkbox", "Device is enrolled and compliant").set_value(
        False).run()
    body = text_of(employee)
    assert "Access granted" in body
    assert "download, print and sync blocked" in body


def test_a_granted_session_lists_the_libraries_it_reaches():
    body = text_of(signed_in(EMPLOYEE))
    assert "This session may" in body
    assert "Project Workspaces" in body


def test_a_guest_session_is_shown_the_guest_column_not_the_visitor_column():
    """A guest holds Visitor, but the tenant closes confidential libraries to
    them. Showing the plain Visitor row would overstate what they reach."""
    body = text_of(signed_in(GUEST))
    assert "Access granted" in body
    assert "Finance and Payroll" in body
    assert "download blocked" in body


def test_a_session_with_no_controls_says_so_rather_than_showing_nothing():
    at = signed_in(EMPLOYEE, at=run())     # default posture, no controls at all
    body = text_of(at)
    assert "No session controls apply" in body


# ---------------------------------------------------------------------------
# Access matrix
# ---------------------------------------------------------------------------

def test_the_matrix_lists_every_library_with_its_sensitivity():
    body = text_of(run())
    for name in ("Company Policies", "Project Workspaces", "Client Deliverables",
                 "Finance and Payroll", "HR Records", "Site Configuration"):
        assert name in body
    assert "Confidential" in body


def test_the_matrix_names_the_libraries_no_guest_ever_reaches():
    assert "are never guest reachable" in text_of(run())


def test_disabling_external_sharing_empties_the_guest_column_on_screen():
    at = hardened()
    widget(at, "selectbox", "External sharing").set_value(SHARING_DISABLED).run()
    at = press(at, "Apply configuration")
    guest_cells = []
    for frame in at.get("dataframe"):
        value = getattr(frame, "value", None)
        if value is not None and "External guest" in getattr(value, "columns", []):
            guest_cells += list(value["External guest"])
    assert guest_cells
    assert set(guest_cells) == {NO_ACCESS}


# ---------------------------------------------------------------------------
# Deployment audit
# ---------------------------------------------------------------------------

def test_loading_the_hardened_posture_passes_every_control():
    at = hardened()
    assert config_of(at) == HARDENED_TENANT
    body = text_of(at)
    assert BAND_READY in body
    assert "Every control passes" in body


def test_loading_the_default_posture_again_reopens_the_controls():
    at = press(hardened(), "Load the Microsoft default posture")
    assert config_of(at) == DEFAULT_TENANT
    assert BAND_BLOCKED in text_of(at)


def test_loading_a_posture_resets_the_controls_bound_to_it():
    """A widget key that survived the load would put the old setting back on the
    next interaction, silently undoing the load."""
    at = hardened()
    assert widget(at, "checkbox",
                  "Multi factor authentication enforced").value is True
    assert widget(at, "selectbox", "Default sharing link").value == LINK_SPECIFIC


def test_a_control_does_not_take_effect_until_the_form_is_applied():
    at = hardened()
    widget(at, "selectbox", "External sharing").set_value(SHARING_DISABLED).run()
    assert config_of(at).external_sharing != SHARING_DISABLED
    at = press(at, "Apply configuration")
    assert config_of(at).external_sharing == SHARING_DISABLED


def test_applying_one_control_leaves_the_others_alone():
    at = hardened()
    widget(at, "checkbox", "Version history retained").set_value(False).run()
    at = press(at, "Apply configuration")
    config = config_of(at)
    assert config.versioning_enabled is False
    assert config.mfa_enforced is True
    assert config.external_sharing == HARDENED_TENANT.external_sharing


def test_the_open_controls_carry_a_severity_a_reason_and_a_script():
    body = text_of(run())
    assert "Critical" in body
    assert "Remediation" in body
    assert "Set-SPOTenant" in body


def test_the_remediation_script_shrinks_as_controls_are_closed():
    default_script = [c.value for c in run().code if "Set-SPOTenant" in c.value]
    at = hardened()
    widget(at, "checkbox", "Version history retained").set_value(False).run()
    at = press(at, "Apply configuration")
    partial = [c.value for c in at.code if "EnableVersionExpirationSetting"
               in c.value]
    assert default_script and partial
    assert len(partial[0]) < len(default_script[0])


def test_turning_off_sharing_blocks_the_guest_on_the_login_tab_too():
    """One switch, three tabs. If these ever disagree the console certifies a
    posture the tenant does not have."""
    at = hardened()
    widget(at, "selectbox", "External sharing").set_value(SHARING_DISABLED).run()
    at = press(at, "Apply configuration")
    widget(at, "selectbox", "User type").set_value(GUEST).run()
    body = text_of(at)
    assert "Access blocked by External sharing" in body
    assert BAND_READY in body


def test_blocking_unmanaged_devices_changes_the_employee_outcome():
    """The same employee, the same device, two tenant settings. Limited web only
    downgrades the session; block refuses it."""
    at = signed_in(EMPLOYEE)
    widget(at, "checkbox", "Device is enrolled and compliant").set_value(
        False).run()
    assert "Access granted" in text_of(at)

    widget(at, "selectbox", "Unmanaged devices").set_value(DEVICE_BLOCK).run()
    at = press(at, "Apply configuration")
    assert "Access blocked by Unmanaged device policy" in text_of(at)


@pytest.mark.parametrize("user_type", USER_TYPES)
def test_no_dash_characters_reach_the_screen(user_type):
    body = text_of(signed_in(user_type))
    assert "—" not in body
    assert "–" not in body
