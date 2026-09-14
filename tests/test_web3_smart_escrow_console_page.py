"""Page tests for the Web3 SmartEscrow Architecture Console, via AppTest.

Written for pytest.

Run: pytest tests/test_web3_smart_escrow_console_page.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.web3_smart_escrow_console.core import (  # noqa: E402
    CONTRACT_NAME,
    JS_SAFE_INTEGER,
    NETWORKS,
    SUITE,
    UINT96_MAX,
    sample_stream,
)
from tools.web3_smart_escrow_console.page import STATE  # noqa: E402

HARNESS = (Path(__file__).resolve().parent
           / "_page_harness_web3_smart_escrow_console.py")

MAINNET = NETWORKS[0].name
BASE = NETWORKS[1].name
SEPOLIA = NETWORKS[4].name

ADDRESS = re.compile(r"0x[0-9a-f]{40}")


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


def run_app(timeout: int = 180) -> AppTest:
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


def deployed(network: str = MAINNET, at: AppTest | None = None) -> AppTest:
    at = at or run_app()
    widget(at, "selectbox", "Network").set_value(network).run()
    return press(at, "Deploy contract")


def state_of(at: AppTest) -> dict:
    return at.session_state[STATE]


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------

def test_the_page_renders_with_no_exception():
    at = run_app()
    assert not at.exception, [str(e.value) for e in at.exception]


def test_the_hero_and_the_four_tabs_are_present():
    at = run_app()
    assert "Web3 SmartEscrow Architecture Console" in text_of(at)
    labels = [tab.label for tab in at.tabs]
    for expected in ("Deployment", "Foundry Ledger", "Event Indexing",
                     "Gas Tracker"):
        assert expected in labels


def test_it_says_plainly_that_nothing_can_move_funds():
    body = text_of(run_app())
    assert "no key is ever loaded" in body
    assert "SHA256" in body


def test_nothing_is_deployed_until_the_button_is_pressed():
    at = run_app()
    assert state_of(at)["deployment"] is None
    assert "Not deployed yet" in text_of(at)


def test_no_two_widgets_of_a_kind_share_a_label():
    at = deployed()
    for kind in ("selectbox", "number_input", "button", "slider", "text_input"):
        labels = [el.label for el in getattr(at, kind)]
        assert len(labels) == len(set(labels)), f"{kind}: {labels}"


# ---------------------------------------------------------------------------
# Deployment simulator
# ---------------------------------------------------------------------------

def test_deploying_logs_an_address_a_hash_and_a_block():
    at = deployed()
    body = text_of(at)
    deployment = state_of(at)["deployment"]
    assert deployment is not None
    assert ADDRESS.search(body)
    assert deployment.address in body
    assert deployment.tx_hash in body
    assert str(deployment.block_number) in body.replace(",", "")


def test_the_receipt_is_on_screen_in_the_shape_ethers_returns():
    body = text_of(deployed())
    assert '"contractAddress"' in body
    assert '"transactionHash"' in body
    assert '"status": 1' in body


def test_the_chosen_network_is_the_one_deployed_to():
    at = deployed(BASE)
    assert state_of(at)["deployment"].network.name == BASE
    assert "basescan.org" in text_of(at) or BASE in text_of(at)


def test_a_testnet_deployment_is_labelled_as_one():
    body = text_of(deployed(SEPOLIA))
    assert "Testnet" in body


def test_deploying_twice_produces_two_different_contracts():
    at = deployed()
    first = state_of(at)["deployment"].address
    at = press(at, "Deploy contract")
    second = state_of(at)["deployment"].address
    assert first != second
    assert state_of(at)["sequence"] == 2


def test_changing_the_constructor_arguments_changes_the_deployment():
    """The arguments are part of what was deployed, so the identifier has to
    move with them."""
    at = deployed()
    first = state_of(at)["deployment"].address
    widget(at, "number_input", "Protocol fee, basis points").set_value(250).run()
    at = press(at, "Deploy contract")
    assert state_of(at)["deployment"].constructor_args["feeBasisPoints"] == 250
    assert state_of(at)["deployment"].address != first


def test_the_confirmation_table_runs_to_final():
    body = text_of(deployed())
    assert "Mined" in body
    assert "Confirming" in body
    assert "Final" in body


def test_the_roles_are_listed_with_what_each_one_may_do():
    body = text_of(deployed())
    for role in ("DEFAULT_ADMIN_ROLE", "ARBITER_ROLE", "PAUSER_ROLE"):
        assert role in body
    assert "Cannot move escrowed funds" in body


def test_the_openzeppelin_surface_is_shown_with_reasons():
    body = text_of(run_app())
    for name in ("ReentrancyGuard", "AccessControl", "SafeERC20", "Pausable"):
        assert name in body
    assert CONTRACT_NAME in body


# ---------------------------------------------------------------------------
# Foundry ledger
# ---------------------------------------------------------------------------

def test_the_ledger_opens_clean_with_every_test_passing():
    body = text_of(run_app())
    assert "All 10 tests pass" in body
    assert "Suite result: ok" in body
    assert "testFuzz_reentrantReleaseReverts" in body


def test_every_test_in_the_suite_is_listed():
    body = text_of(run_app())
    for test in SUITE:
        assert test.name in body


def test_the_regression_build_shows_the_ledger_failing():
    """The control that makes the green ledger mean something."""
    at = run_app()
    widget(at, "toggle",
           "Load the regression build, where one access control test fails"
           ).set_value(True).run()
    body = text_of(at)
    assert "does not ship" in body
    assert "Counterexample" in body
    assert "Suite result: FAILED" in body


def test_the_clean_ledger_shows_no_counterexample():
    assert "Counterexample" not in text_of(run_app())


def test_the_run_counts_are_on_screen_for_both_target_properties():
    body = text_of(run_app())
    assert "Reentrancy runs" in body
    assert "Access control runs" in body
    assert "30,000" in body     # three reentrancy tests at ten thousand runs


# ---------------------------------------------------------------------------
# Event indexing
# ---------------------------------------------------------------------------

def test_the_stream_indexes_the_happy_path_and_both_hard_cases():
    body = text_of(run_app())
    assert "Logs indexed" in body
    assert "Replays absorbed" in body
    assert "Reverted by reorg" in body


def test_stepping_the_stream_back_shows_the_state_before_the_reorg():
    at = run_app()
    widget(at, "slider", "Logs delivered").set_value(5).run()
    assert "refunded" in text_of(at)

    widget(at, "slider", "Logs delivered").set_value(len(sample_stream())).run()
    body = text_of(at)
    assert "Reverted by reorg" in body
    assert "no longer in it" in body


def test_the_first_log_writes_an_insert_with_on_conflict_do_nothing():
    at = run_app()
    widget(at, "slider", "Logs delivered").set_value(1).run()
    body = text_of(at)
    assert "INSERT INTO escrows" in body
    assert "ON CONFLICT (tx_hash, log_index) DO NOTHING" in body


def test_a_replayed_log_is_shown_as_absorbed_rather_than_applied():
    at = run_app()
    widget(at, "slider", "Logs delivered").set_value(4).run()
    body = text_of(at)
    assert "Duplicate ignored" in body
    assert "makes restarting the listener safe" in body


def test_the_schema_and_the_listener_are_both_on_screen():
    body = text_of(run_app())
    assert "CREATE TABLE escrows" in body
    assert "UNIQUE (tx_hash, log_index)" in body
    assert "contract.on(" in body


def test_every_filtered_event_is_documented():
    body = text_of(run_app())
    for signature in ("Deposited(uint256,address,address,address,uint256)",
                      "Released(uint256,address,uint256)",
                      "Refunded(uint256,address,uint256)"):
        assert signature in body


# ---------------------------------------------------------------------------
# Gas tracker
# ---------------------------------------------------------------------------

def test_the_gas_saving_is_on_screen_with_the_two_layouts():
    body = text_of(run_app())
    assert "Gas saved per deposit" in body
    assert "88,400" in body
    assert "67%" in body


def test_the_money_saved_moves_with_the_volume():
    at = run_app()
    widget(at, "number_input", "Deposits per year").set_value(1000).run()
    small = text_of(at)
    widget(at, "number_input", "Deposits per year").set_value(100000).run()
    large = text_of(at)
    assert small != large
    assert "USD saved per year" in large


def test_a_wei_amount_inside_the_ceiling_reports_that_it_packs():
    at = run_app()
    widget(at, "text_input", "Deposit amount in wei").set_value("2500000000").run()
    body = text_of(at)
    assert "Fits uint96" in body
    assert "Exact in a JavaScript number" in body


def test_a_wei_amount_above_the_ceiling_reports_the_revert():
    at = run_app()
    widget(at, "text_input",
           "Deposit amount in wei").set_value(str(UINT96_MAX + 1)).run()
    body = text_of(at)
    assert "Above the ceiling" in body
    assert "reverts on deposit rather" in body


def test_a_wei_amount_above_the_javascript_limit_is_still_handled():
    """This is the case that made the control a text box: a number input cannot
    even accept this value."""
    at = run_app()
    widget(at, "text_input",
           "Deposit amount in wei").set_value(str(JS_SAFE_INTEGER + 1)).run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "bigint" in text_of(at)


def test_a_decimal_wei_amount_is_refused_on_screen():
    at = run_app()
    widget(at, "text_input", "Deposit amount in wei").set_value("1.5").run()
    body = text_of(at)
    assert "Refused" in body
    assert "integer" in body


@pytest.mark.parametrize("network", [MAINNET, BASE, SEPOLIA])
def test_no_dash_characters_reach_the_screen(network):
    body = text_of(deployed(network))
    assert "—" not in body
    assert "–" not in body
