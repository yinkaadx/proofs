"""Engine tests for the Web3 SmartEscrow Architecture Console.

Written for pytest. Deterministic throughout: the engine reads no clock and no
random source, so the same inputs give the same addresses, hashes and gas
figures on every run.

Run: pytest tests/test_web3_smart_escrow_console.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.web3_smart_escrow_console.core import (  # noqa: E402
    ACCEPTED,
    COLD_SLOAD,
    CONFIRMATIONS_REQUIRED,
    CONTRACT_NAME,
    DEFAULT_ARBITER,
    DEFAULT_DEPLOYER,
    DEPLOY_GAS,
    DUPLICATE,
    ESCROW_PAYEE,
    ESCROW_PAYER,
    EVENT_DEPOSITED,
    EVENT_NAMES,
    EVENT_REFUNDED,
    EVENT_RELEASED,
    EVENT_SIGNATURES,
    FAIL,
    JS_SAFE_INTEGER,
    KIND_FUZZ,
    KIND_INVARIANT,
    NETWORKS,
    OZ_IMPORTS,
    PACKED_SLOTS,
    PASS,
    REGRESSION_SUITE,
    REJECTED,
    REORGED,
    ROLE_ADMIN,
    ROLE_ARBITER,
    ROLE_PAUSER,
    ROLES,
    SCHEMA_SQL,
    SSTORE_SET,
    STATUS_FUNDED,
    STATUS_NONE,
    STATUS_REFUNDED,
    STATUS_RELEASED,
    SUITE,
    TARGET_ACCESS,
    TARGET_REENTRANCY,
    UINT96_MAX,
    UNPACKED_SLOTS,
    USDC,
    ChainEvent,
    Index,
    confirmation_rows,
    deploy,
    deployment_rows,
    forge_output,
    fuzz_rows,
    gas_comparison,
    gas_rows,
    index_event,
    network_by_name,
    packing_note,
    parse_wei,
    replay,
    role_rows,
    sample_stream,
    savings_usd,
    summarise_fuzz,
)

MAINNET = NETWORKS[0]
BASE = NETWORKS[1]
SEPOLIA = NETWORKS[4]

ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")
TX_HASH = re.compile(r"^0x[0-9a-f]{64}$")


def event(name=EVENT_DEPOSITED, escrow_id=1, block=100, log_index=0,
          tx="0x" + "a" * 64, removed=False, **args):
    payload = args or {"payer": ESCROW_PAYER, "payee": ESCROW_PAYEE,
                       "token": USDC, "amount": 1_000_000}
    return ChainEvent(name, escrow_id, block, log_index, tx, payload,
                      removed=removed)


# ---------------------------------------------------------------------------
# Deployment logging
# ---------------------------------------------------------------------------

def test_a_deployment_produces_a_well_formed_address_and_hash():
    deployment = deploy(MAINNET)
    assert ADDRESS.match(deployment.address), deployment.address
    assert TX_HASH.match(deployment.tx_hash), deployment.tx_hash


def test_addresses_are_lowercase_because_a_checksum_needs_keccak():
    """An EIP 55 checksum cannot be computed without keccak256, and a made up
    one would be worse than none, so the address is plain lowercase."""
    assert deploy(MAINNET).address.islower()
    for address in (DEFAULT_DEPLOYER, DEFAULT_ARBITER, ESCROW_PAYER,
                    ESCROW_PAYEE, USDC):
        assert ADDRESS.match(address), address


def test_the_same_inputs_always_produce_the_same_receipt():
    assert deploy(MAINNET, sequence=7) == deploy(MAINNET, sequence=7)


def test_a_different_network_produces_a_different_address():
    assert deploy(MAINNET, sequence=1).address != deploy(BASE, sequence=1).address


def test_a_different_sequence_produces_a_different_deployment():
    first, second = deploy(MAINNET, sequence=1), deploy(MAINNET, sequence=2)
    assert first.address != second.address
    assert first.tx_hash != second.tx_hash
    assert second.block_number == first.block_number + 1


def test_changing_a_constructor_argument_changes_the_address():
    """The arguments are part of the deployment, so the identifier has to move
    with them or the receipt would not identify what was deployed."""
    assert (deploy(MAINNET, fee_basis_points=125).address
            != deploy(MAINNET, fee_basis_points=250).address)


def test_the_fee_is_the_gas_times_the_price_in_the_native_token():
    deployment = deploy(MAINNET)
    assert deployment.gas_used == DEPLOY_GAS
    assert deployment.fee_native == pytest.approx(
        DEPLOY_GAS * MAINNET.base_fee_gwei / 1e9)
    assert deployment.fee_usd == pytest.approx(
        round(deployment.fee_native * MAINNET.native_price_usd, 2))


def test_a_cheap_chain_costs_less_than_mainnet_for_the_same_deployment():
    assert deploy(BASE).fee_usd < deploy(MAINNET).fee_usd


def test_a_testnet_deployment_costs_nothing_in_real_money():
    assert deploy(SEPOLIA).fee_usd == 0.0
    assert SEPOLIA.testnet is True


def test_an_absurd_protocol_fee_is_refused_rather_than_deployed():
    with pytest.raises(ValueError):
        deploy(MAINNET, fee_basis_points=5000)


def test_a_dispute_window_of_zero_is_refused():
    """With no window nobody could ever raise a refund, so the escrow would be
    one sided from the moment it deployed."""
    with pytest.raises(ValueError):
        deploy(MAINNET, dispute_window_hours=0)


def test_the_receipt_matches_what_ethers_returns():
    receipt = deploy(MAINNET).as_receipt()
    assert set(receipt) == {"to", "from", "contractAddress", "transactionHash",
                            "blockNumber", "gasUsed", "effectiveGasPrice",
                            "chainId", "status"}
    assert receipt["to"] is None          # a deployment has no recipient
    assert receipt["status"] == 1
    assert receipt["chainId"] == MAINNET.chain_id
    assert json.loads(json.dumps(receipt))["gasUsed"] == DEPLOY_GAS


def test_the_explorer_link_points_at_the_right_chain():
    assert deploy(BASE).explorer_url.startswith("https://basescan.org/tx/0x")


def test_the_constructor_grants_exactly_three_roles():
    deployment = deploy(MAINNET)
    assert set(deployment.roles) == set(ROLES)
    assert deployment.roles[ROLE_ARBITER] == DEFAULT_ARBITER
    assert deployment.roles[ROLE_ADMIN] == DEFAULT_DEPLOYER
    assert len(role_rows(deployment)) == 3


def test_no_role_can_both_move_funds_and_change_who_may_move_them():
    rows = {row["Role"]: row["May"] for row in role_rows(deploy(MAINNET))}
    assert "Cannot move escrowed funds" in rows[ROLE_ADMIN]
    assert "Cannot pause a release" in rows[ROLE_PAUSER]
    assert "Refund" in rows[ROLE_ARBITER]


def test_confirmations_run_from_mined_to_final():
    rows = confirmation_rows(deploy(MAINNET))
    assert len(rows) == CONFIRMATIONS_REQUIRED
    assert rows[0]["State"] == "Mined"
    assert rows[-1]["State"] == "Final"
    assert rows[1]["State"] == "Confirming"


def test_confirmation_time_follows_the_chains_own_block_time():
    fast = confirmation_rows(deploy(BASE))[-1]["Seconds after mining"]
    slow = confirmation_rows(deploy(MAINNET))[-1]["Seconds after mining"]
    assert float(fast) < float(slow)
    assert float(slow) == pytest.approx(
        (CONFIRMATIONS_REQUIRED - 1) * MAINNET.block_time_s)


def test_the_deployment_and_confirmation_tables_are_arrow_safe():
    deployment = deploy(MAINNET)
    for rows in (deployment_rows(deployment), confirmation_rows(deployment),
                 role_rows(deployment)):
        for column in {key for row in rows for key in row}:
            kinds = {type(row[column]).__name__ for row in rows}
            assert len(kinds) == 1, f"column {column!r} mixes {sorted(kinds)}"


def test_every_openzeppelin_import_says_why_it_is_there():
    assert len(OZ_IMPORTS) == 4
    for name, why in OZ_IMPORTS:
        assert name and len(why) > 30


def test_an_unknown_network_is_refused_rather_than_guessed():
    assert network_by_name("Bitcoin") is None
    assert network_by_name("Base") is BASE


# ---------------------------------------------------------------------------
# Foundry fuzzing ledger
# ---------------------------------------------------------------------------

def test_the_shipped_suite_is_clean():
    summary = summarise_fuzz(SUITE)
    assert summary.clean is True
    assert summary.failed == 0
    assert summary.passed == summary.total == len(SUITE)


def test_the_ledger_can_report_a_failure_so_a_pass_means_something():
    """A ledger that can only ever say pass proves nothing. This is the
    control that makes the green one worth reading."""
    summary = summarise_fuzz(REGRESSION_SUITE)
    assert summary.clean is False
    assert summary.failed == 1
    assert "does not ship" in summary.headline


def test_the_regression_build_differs_from_the_shipped_one_in_one_test_only():
    shipped = {test.name: test.status for test in SUITE}
    broken = {test.name: test.status for test in REGRESSION_SUITE}
    differing = [name for name in shipped if shipped[name] != broken[name]]
    assert differing == ["testFuzz_onlyArbiterCanRefund"]


def test_the_failing_test_carries_a_counterexample():
    failing = next(test for test in REGRESSION_SUITE if not test.passing)
    assert failing.counterexample
    assert failing.status == FAIL


def test_reentrancy_and_access_control_are_both_fuzzed_not_unit_tested():
    for target in (TARGET_REENTRANCY, TARGET_ACCESS):
        fuzzed = [test for test in SUITE
                  if test.target == target and test.kind == KIND_FUZZ]
        assert len(fuzzed) >= 2, target
        assert all(test.runs >= 10_000 for test in fuzzed)


def test_the_accounting_properties_are_invariants_rather_than_single_cases():
    invariants = [test for test in SUITE if test.kind == KIND_INVARIANT]
    assert len(invariants) == 2
    assert all(test.runs >= 1_000 for test in invariants)
    assert any("settles twice" in test.name.lower()
               or "SettlesTwice" in test.name for test in invariants)


def test_every_test_states_what_it_actually_asserts():
    for test in SUITE:
        assert len(test.asserts) > 40, test.name


def test_the_run_totals_add_up_per_target():
    summary = summarise_fuzz(SUITE)
    assert summary.total_runs == sum(test.runs for test in SUITE)
    assert sum(summary.targets.values()) == summary.total_runs
    assert summary.targets[TARGET_REENTRANCY] == sum(
        test.runs for test in SUITE if test.target == TARGET_REENTRANCY)


def test_the_forge_line_matches_the_format_forge_prints():
    fuzz = next(test for test in SUITE if test.kind == KIND_FUZZ)
    line = fuzz.forge_line()
    assert line.startswith(f"[{PASS}] {fuzz.name}")
    assert f"runs: {fuzz.runs}" in line
    assert "μ:" in line


def test_a_failing_forge_line_shows_its_counterexample():
    failing = next(test for test in REGRESSION_SUITE if not test.passing)
    assert "Counterexample" in failing.forge_line()
    assert f"[{FAIL}]" in failing.forge_line()


def test_the_forge_output_reports_ok_only_when_the_suite_is_clean():
    assert "Suite result: ok" in forge_output(SUITE)
    assert "Suite result: FAILED" in forge_output(REGRESSION_SUITE)


def test_the_forge_output_has_one_line_per_test():
    body = forge_output(SUITE)
    for test in SUITE:
        assert test.name in body


def test_the_fuzz_table_is_arrow_safe():
    for suite in (SUITE, REGRESSION_SUITE):
        rows = fuzz_rows(suite)
        for column in {key for row in rows for key in row}:
            kinds = {type(row[column]).__name__ for row in rows}
            assert len(kinds) == 1, f"column {column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Event indexing
# ---------------------------------------------------------------------------

def test_every_event_has_a_signature_and_a_topic():
    assert set(EVENT_SIGNATURES) == set(EVENT_NAMES)
    for name in EVENT_NAMES:
        assert TX_HASH.match(event(name=name, escrow_id=1).topic0)


def test_each_event_filters_on_its_own_topic():
    topics = {event(name=name).topic0 for name in EVENT_NAMES}
    assert len(topics) == len(EVENT_NAMES)


def test_a_deposit_creates_the_escrow_row():
    index = Index()
    result = index_event(index, event(amount=5_000, payer=ESCROW_PAYER,
                                      payee=ESCROW_PAYEE, token=USDC))
    assert result.outcome == ACCEPTED
    assert result.status_before == STATUS_NONE
    assert result.status_after == STATUS_FUNDED
    assert index.escrows[1]["payer"] == ESCROW_PAYER
    assert index.escrows[1]["amount"] == 5_000


def test_a_release_moves_a_funded_escrow_to_released():
    index = Index()
    index_event(index, event())
    result = index_event(index, event(name=EVENT_RELEASED, block=101,
                                      tx="0x" + "b" * 64, to=ESCROW_PAYEE,
                                      amount=5_000))
    assert result.outcome == ACCEPTED
    assert index.status_of(1) == STATUS_RELEASED


def test_a_refund_moves_a_funded_escrow_to_refunded():
    index = Index()
    index_event(index, event())
    index_event(index, event(name=EVENT_REFUNDED, block=101,
                             tx="0x" + "c" * 64, to=ESCROW_PAYER, amount=5_000))
    assert index.status_of(1) == STATUS_REFUNDED


def test_a_replayed_log_changes_nothing():
    """Restarting the listener replays the block it was on. The unique key on
    the transaction and log index is what makes that harmless."""
    index = Index()
    first = index_event(index, event())
    again = index_event(index, event())
    assert first.outcome == ACCEPTED
    assert again.outcome == DUPLICATE
    assert len(index.escrows) == 1
    assert index.status_of(1) == STATUS_FUNDED


def test_the_same_block_can_carry_two_different_logs():
    index = Index()
    index_event(index, event(escrow_id=1, log_index=0, tx="0x" + "a" * 64))
    second = index_event(index, event(escrow_id=2, log_index=1,
                                      tx="0x" + "a" * 64))
    assert second.outcome == ACCEPTED
    assert len(index.escrows) == 2


def test_a_release_on_an_unknown_escrow_is_refused():
    index = Index()
    result = index_event(index, event(name=EVENT_RELEASED, to=ESCROW_PAYEE,
                                      amount=1))
    assert result.outcome == REJECTED
    assert index.escrows == {}
    assert result.sql == []


def test_an_escrow_cannot_be_settled_twice():
    index = Index()
    index_event(index, event())
    index_event(index, event(name=EVENT_RELEASED, block=101,
                             tx="0x" + "b" * 64, to=ESCROW_PAYEE, amount=1))
    result = index_event(index, event(name=EVENT_REFUNDED, block=102,
                                      tx="0x" + "c" * 64, to=ESCROW_PAYER,
                                      amount=1))
    assert result.outcome == REJECTED
    assert index.status_of(1) == STATUS_RELEASED


def test_a_reorg_undoes_a_release_back_to_funded():
    index = Index()
    index_event(index, event())
    index_event(index, event(name=EVENT_RELEASED, block=101,
                             tx="0x" + "b" * 64, to=ESCROW_PAYEE, amount=1))
    result = index_event(index, event(name=EVENT_RELEASED, block=101,
                                      tx="0x" + "b" * 64, removed=True,
                                      to=ESCROW_PAYEE, amount=1))
    assert result.outcome == REORGED
    assert index.status_of(1) == STATUS_FUNDED


def test_a_reorg_that_removes_a_deposit_deletes_the_escrow_row():
    index = Index()
    index_event(index, event())
    result = index_event(index, event(removed=True))
    assert result.outcome == REORGED
    assert index.escrows == {}
    assert any("DELETE FROM escrows" in statement for statement in result.sql)


def test_a_reorged_log_can_be_reapplied_when_the_chain_settles():
    index = Index()
    index_event(index, event())
    index_event(index, event(removed=True))
    result = index_event(index, event())
    assert result.outcome == ACCEPTED
    assert index.status_of(1) == STATUS_FUNDED


def test_a_reorg_for_a_log_never_held_is_refused_rather_than_applied():
    index = Index()
    result = index_event(index, event(removed=True))
    assert result.outcome == REJECTED
    assert index.escrows == {}


def test_the_insert_is_written_with_on_conflict_do_nothing():
    index = Index()
    result = index_event(index, event())
    joined = " ".join(result.sql)
    assert "INSERT INTO escrows" in joined
    assert "ON CONFLICT (tx_hash, log_index) DO NOTHING" in joined


def test_a_settlement_updates_rather_than_reinserting_the_escrow():
    index = Index()
    index_event(index, event())
    result = index_event(index, event(name=EVENT_RELEASED, block=101,
                                      tx="0x" + "b" * 64, to=ESCROW_PAYEE,
                                      amount=1))
    joined = " ".join(result.sql)
    assert "UPDATE escrows SET status = 'released'" in joined
    assert "INSERT INTO escrows" not in joined


def test_the_schema_carries_the_unique_key_the_indexer_depends_on():
    assert "UNIQUE (tx_hash, log_index)" in SCHEMA_SQL
    assert "REFERENCES escrows(escrow_id)" in SCHEMA_SQL
    assert "NUMERIC(78, 0)" in SCHEMA_SQL       # a uint256 does not fit BIGINT


def test_the_sample_stream_exercises_the_two_cases_that_matter():
    index = replay(sample_stream())
    outcomes = [result.outcome for result in index.results]
    assert DUPLICATE in outcomes
    assert REORGED in outcomes
    assert outcomes.count(ACCEPTED) == 4


def test_the_sample_stream_ends_in_a_state_the_chain_agrees_with():
    index = replay(sample_stream())
    assert index.status_of(1) == STATUS_RELEASED
    assert index.status_of(2) == STATUS_FUNDED     # the refund was reorged out


def test_replaying_the_whole_stream_twice_changes_nothing():
    once = replay(sample_stream())
    twice = replay(sample_stream(), replay(sample_stream()))
    assert twice.escrow_rows() == once.escrow_rows()


def test_the_index_tables_are_arrow_safe():
    index = replay(sample_stream())
    for rows in (index.event_rows(), index.escrow_rows()):
        for column in {key for row in rows for key in row}:
            kinds = {type(row[column]).__name__ for row in rows}
            assert len(kinds) == 1, f"column {column!r} mixes {sorted(kinds)}"


def test_a_uint256_amount_is_carried_as_a_string_in_the_table():
    """A uint256 overflows a JavaScript number and a Postgres BIGINT alike, so
    it never goes into a numeric column here."""
    index = Index()
    index_event(index, event(amount=2 ** 200))
    assert index.escrow_rows()[0]["amount_wei"] == str(2 ** 200)


# ---------------------------------------------------------------------------
# Gas optimisation
# ---------------------------------------------------------------------------

def test_packing_six_fields_into_two_slots_is_what_saves_the_gas():
    comparison = gas_comparison()
    assert comparison.unpacked_slots == len(UNPACKED_SLOTS) == 6
    assert comparison.packed_slots == len(PACKED_SLOTS) == 2


def test_the_deposit_cost_is_computed_from_the_named_eip_constants():
    comparison = gas_comparison()
    assert comparison.deposit_unpacked == 6 * (SSTORE_SET + COLD_SLOAD)
    assert comparison.deposit_packed == 2 * (SSTORE_SET + COLD_SLOAD)
    assert comparison.deposit_saved == 4 * (SSTORE_SET + COLD_SLOAD)


def test_the_saving_is_two_thirds_of_the_storage_cost():
    assert gas_comparison().deposit_saved_percent == pytest.approx(66.7)


def test_reads_are_cheaper_too_which_is_the_saving_people_forget():
    comparison = gas_comparison()
    assert comparison.read_unpacked == 6 * COLD_SLOAD
    assert comparison.read_packed == 2 * COLD_SLOAD
    assert comparison.read_saved == 4 * COLD_SLOAD


def test_the_money_saved_scales_with_volume_and_with_the_chain():
    comparison = gas_comparison()
    assert savings_usd(comparison, 0, MAINNET) == 0.0
    small = savings_usd(comparison, 1_000, MAINNET)
    large = savings_usd(comparison, 25_000, MAINNET)
    assert large > small > 0
    assert savings_usd(comparison, 25_000, BASE) < large


def test_a_negative_deposit_count_is_refused():
    with pytest.raises(ValueError):
        savings_usd(gas_comparison(), -1, MAINNET)


def test_the_gas_table_is_arrow_safe():
    rows = gas_rows(gas_comparison())
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"column {column!r} mixes {sorted(kinds)}"


def test_an_amount_inside_the_uint96_ceiling_packs_exactly():
    fits, note = packing_note(2_500_000_000)
    assert fits is True
    assert "fits in uint96" in note


def test_an_amount_above_the_ceiling_reverts_rather_than_truncating():
    """The honest half of the optimisation: a silent truncation here would lose
    the difference forever."""
    fits, note = packing_note(UINT96_MAX + 1)
    assert fits is False
    assert "reverts on deposit rather" in note


def test_the_ceiling_itself_still_fits():
    assert packing_note(UINT96_MAX)[0] is True


# ---------------------------------------------------------------------------
# Wei parsing
# ---------------------------------------------------------------------------

def test_a_whole_number_of_wei_parses():
    ok, value, _ = parse_wei("2500000000")
    assert ok is True
    assert value == 2_500_000_000


def test_separators_people_actually_type_are_accepted():
    assert parse_wei("2,500,000,000")[1] == 2_500_000_000
    assert parse_wei("2_500_000_000")[1] == 2_500_000_000
    assert parse_wei("  42 ")[1] == 42


def test_a_decimal_amount_is_refused_because_wei_is_an_integer():
    ok, _, message = parse_wei("1.5")
    assert ok is False
    assert "integer" in message


def test_an_empty_amount_is_refused_without_guessing_a_default():
    assert parse_wei("")[0] is False
    assert parse_wei("   ")[0] is False


def test_an_amount_above_the_javascript_limit_is_flagged_not_rejected():
    """It is a real wei value, so it parses. It cannot round trip through a
    JavaScript number, so it is carried as a bigint and the note says so."""
    ok, value, note = parse_wei(str(JS_SAFE_INTEGER + 1))
    assert ok is True
    assert value == JS_SAFE_INTEGER + 1
    assert "bigint" in note


def test_an_amount_inside_the_javascript_limit_says_it_is_exact():
    assert "Exact" in parse_wei(str(JS_SAFE_INTEGER))[2]


# ---------------------------------------------------------------------------
# Prose discipline
# ---------------------------------------------------------------------------

def test_no_dash_characters_in_any_user_facing_prose():
    parts: list[str] = [why for _, why in OZ_IMPORTS]
    parts += [row["May"] for row in role_rows(deploy(MAINNET))]
    for suite in (SUITE, REGRESSION_SUITE):
        parts.append(summarise_fuzz(suite).headline)
        parts += [test.asserts for test in suite]
    index = replay(sample_stream())
    parts += [result.note for result in index.results]
    parts.append(packing_note(UINT96_MAX + 1)[1])
    parts.append(parse_wei("1.5")[2])
    prose = "\n".join(parts)
    assert "—" not in prose
    assert "–" not in prose
