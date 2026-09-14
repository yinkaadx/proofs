"""Web3 SmartEscrow Architecture Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine can sit behind a real deployment script.
"""

from __future__ import annotations

import json

import streamlit as st

from shared.theme import esc, inject
from tools.web3_smart_escrow_console.core import (
    ACCEPTED,
    CONFIRMATIONS_REQUIRED,
    CONTRACT_NAME,
    DUPLICATE,
    ENGINE_VERSION,
    EVENT_SIGNATURES,
    EVM_VERSION,
    LISTENER_JS,
    NETWORKS,
    OZ_IMPORTS,
    PACKED_SLOTS,
    REGRESSION_SUITE,
    REJECTED,
    REORGED,
    SCHEMA_SQL,
    SOLC_VERSION,
    SUITE,
    UNPACKED_SLOTS,
    Index,
    confirmation_rows,
    deploy,
    deployment_rows,
    forge_output,
    fuzz_rows,
    gas_comparison,
    gas_rows,
    network_by_name,
    packing_note,
    parse_wei,
    replay,
    role_rows,
    sample_stream,
    savings_usd,
    summarise_fuzz,
)

STATE = "web3_escrow_state"

OUTCOME_TONE = {ACCEPTED: "ok", DUPLICATE: "info", REORGED: "warn",
                REJECTED: "crit"}


def _state() -> dict:
    if STATE not in st.session_state:
        st.session_state[STATE] = {"sequence": 0, "deployment": None}
    return st.session_state[STATE]


def _deploy() -> None:
    """Deploy with whatever is on screen, read from live widget state.

    Read here rather than passed as callback arguments, which are bound at the
    previous render and so are one interaction stale.
    """
    state = _state()
    network = network_by_name(st.session_state.get("w3_network",
                                                   NETWORKS[0].name))
    if network is None:
        return
    state["sequence"] += 1
    state["deployment"] = deploy(
        network, sequence=state["sequence"],
        fee_basis_points=int(st.session_state.get("w3_fee_bps", 125)),
        dispute_window_hours=int(st.session_state.get("w3_window", 72)))


def render() -> None:
    inject()
    state = _state()
    deployment = state["deployment"]

    st.markdown(
        """
<div class="app-hero">
  <h1>Web3 SmartEscrow Architecture Console</h1>
  <p>An escrow contract holds somebody else's money, so a reviewer asks the
  same three questions every time: what exactly was deployed and where, what
  has actually been proved about it, and how the off chain index stays true to
  the chain when the chain reorganises. This answers all three with the
  arithmetic shown rather than asserted.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Deployment**: pick a network and deploy the contract.\n"
            "2. **Foundry Ledger**: read the fuzz and invariant results, and "
            "load the regression build to see the ledger fail.\n"
            "3. **Event Indexing**: step the listener through a stream that "
            "includes a replay and a reorg.\n"
            "4. **Gas**: compare the unpacked and packed storage layouts."
        )
        st.divider()
        st.caption(
            "Nothing here touches a chain. No RPC call is made and no key is "
            "ever loaded, so nothing in this session can move funds."
        )
        st.caption(
            "Hashes and addresses are derived from the inputs with SHA256, not "
            "keccak256, so a simulated identifier can never be mistaken for a "
            "live one."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_deploy, tab_fuzz, tab_index, tab_gas = st.tabs(
        ["Deployment", "Foundry Ledger", "Event Indexing", "Gas Tracker"]
    )

    # -----------------------------------------------------------------
    # Deployment simulator
    # -----------------------------------------------------------------
    with tab_deploy:
        st.markdown(f"#### Deploy {CONTRACT_NAME}")
        st.caption(
            f"Compiled with solc {SOLC_VERSION} targeting {EVM_VERSION}, "
            f"inheriting four OpenZeppelin contracts, each of which is there "
            f"because it removes a class of bug."
        )

        c1, c2, c3 = st.columns([2, 1, 1])
        c1.selectbox("Network", [n.name for n in NETWORKS], key="w3_network")
        c2.number_input("Protocol fee, basis points", min_value=0,
                        max_value=1000, value=125, step=25, key="w3_fee_bps")
        c3.number_input("Dispute window, hours", min_value=1, max_value=720,
                        value=72, step=12, key="w3_window")

        st.button("Deploy contract", type="primary", on_click=_deploy,
                  key="w3_deploy_btn")

        if deployment is None:
            st.info("Not deployed yet. Deploy to see the receipt and the "
                    "confirmations.")
        else:
            network = deployment.network
            tone = "warn" if network.testnet else "ok"
            tag = "Testnet" if network.testnet else "Confirmed"
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(tag)}</span>{esc(deployment.contract)}
  on {esc(network.name)}</h4>
  <div class="app-ev">{esc(deployment.address)}</div>
  <p>Mined in block {deployment.block_number:,} for
  {deployment.gas_used:,} gas at {network.base_fee_gwei} gwei, which is
  {deployment.fee_native:.6f} {esc(network.native)}
  ({deployment.fee_usd:,.2f} USD). Twelve confirmations take
  {network.confirmation_seconds(CONFIRMATIONS_REQUIRED):.0f} seconds on this
  chain.</p>
</div>
""",
                unsafe_allow_html=True,
            )

            st.markdown("##### Deployment record")
            st.dataframe(deployment_rows(deployment), width="stretch",
                         hide_index=True)

            st.markdown("##### Block confirmations")
            st.dataframe(confirmation_rows(deployment), width="stretch",
                         hide_index=True)

            st.markdown("##### Roles granted by the constructor")
            st.caption(
                "Roles rather than a single owner, so the arbiter who can "
                "refund is not the deployer who can pause. No role can both "
                "move funds and change who may move them."
            )
            st.dataframe(role_rows(deployment), width="stretch",
                         hide_index=True)

            st.markdown("##### The receipt")
            st.code(json.dumps(deployment.as_receipt(), indent=2),
                    language="json")
            st.caption(
                "The transaction hash is derived from the deployment inputs "
                "with SHA256 rather than taken from a signed transaction, so "
                "the same inputs give the same receipt and nothing here can be "
                "confused with a live deployment."
            )

        st.markdown("##### Inherited from OpenZeppelin")
        st.dataframe(
            [{"Contract": name, "Why it is here": why}
             for name, why in OZ_IMPORTS],
            width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Foundry fuzzing ledger
    # -----------------------------------------------------------------
    with tab_fuzz:
        st.markdown("#### Foundry results")
        st.caption(
            "Reentrancy and access control are the two properties an escrow is "
            "actually judged on, so both are fuzzed rather than unit tested. "
            "A ledger that can only ever report a pass proves nothing, so the "
            "regression build below shows what a failure looks like."
        )

        show_regression = st.toggle(
            "Load the regression build, where one access control test fails",
            value=False, key="w3_regression")
        suite = REGRESSION_SUITE if show_regression else SUITE
        summary = summarise_fuzz(suite)
        tone = "ok" if summary.clean else "crit"

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {tone}"><div class="n">{summary.passed}/{summary.total}</div>
    <div class="l">Tests passing</div></div>
  <div class="app-kpi"><div class="n">{summary.total_runs:,}</div>
    <div class="l">Fuzz runs</div></div>
  <div class="app-kpi"><div class="n">{summary.targets.get('Reentrancy', 0):,}</div>
    <div class="l">Reentrancy runs</div></div>
  <div class="app-kpi"><div class="n">{summary.targets.get('Access control', 0):,}</div>
    <div class="l">Access control runs</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc('Clean' if summary.clean else 'Failing')}</span>
  forge test</h4>
  <p>{esc(summary.headline)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.dataframe(fuzz_rows(suite), width="stretch", hide_index=True)

        failing = [test for test in suite if not test.passing]
        for test in failing:
            st.markdown(
                f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">Counterexample</span>{esc(test.name)}</h4>
  <div class="app-ev">{esc(test.counterexample)}</div>
  <p>{esc(test.asserts)}</p>
</div>
""",
                unsafe_allow_html=True,
            )
        if failing:
            st.error(
                "A counterexample on an access control property means any "
                "address could call the function. This build does not ship."
            )

        st.markdown("##### What each property actually asserts")
        st.dataframe(
            [{"Test": test.name, "Asserts": test.asserts} for test in suite],
            width="stretch", hide_index=True)

        st.markdown("##### forge test output")
        st.code(forge_output(suite), language="text")

    # -----------------------------------------------------------------
    # Event indexing stream
    # -----------------------------------------------------------------
    with tab_index:
        st.markdown("#### The listener")
        st.caption(
            "Three filters and one handler. The interesting part is not the "
            "happy path: it is the replayed log after a restart and the log a "
            "reorg removes, because those are what pull a database out of step "
            "with the chain."
        )

        stream = sample_stream()
        steps = st.slider("Logs delivered", min_value=1, max_value=len(stream),
                          value=len(stream), key="w3_stream_steps")
        index: Index = replay(stream[:steps])

        accepted = sum(1 for r in index.results if r.outcome == ACCEPTED)
        duplicates = sum(1 for r in index.results if r.outcome == DUPLICATE)
        reorged = sum(1 for r in index.results if r.outcome == REORGED)
        rejected = sum(1 for r in index.results if r.outcome == REJECTED)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{accepted}</div>
    <div class="l">Logs indexed</div></div>
  <div class="app-kpi"><div class="n">{duplicates}</div>
    <div class="l">Replays absorbed</div></div>
  <div class="app-kpi warn"><div class="n">{reorged}</div>
    <div class="l">Reverted by reorg</div></div>
  <div class="app-kpi crit"><div class="n">{rejected}</div>
    <div class="l">Refused</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### Log by log")
        st.dataframe(index.event_rows(), width="stretch", hide_index=True)

        last = index.results[-1]
        tone = OUTCOME_TONE.get(last.outcome, "info")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(last.outcome)}</span>
  {esc(last.event.name)} on escrow {last.event.escrow_id}</h4>
  <div class="app-ev">block {last.event.block_number:,} &nbsp; log
  {last.event.log_index} &nbsp; {esc(last.event.tx_hash[:18])}</div>
  <p>{esc(last.note)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        if last.sql:
            st.markdown("##### What it wrote")
            st.code("\n".join(last.sql), language="sql")
        else:
            st.warning("Nothing was written, which is the point: the database "
                       "never records something the chain did not say.")

        st.markdown("##### The escrows table right now")
        if index.escrow_rows():
            st.dataframe(index.escrow_rows(), width="stretch", hide_index=True)
        else:
            st.info("No escrow rows yet.")

        st.markdown("##### Events the listener filters on")
        st.dataframe(
            [{"Event": name, "Signature": signature,
              "Writes": "Row into escrows plus escrow_events"
                        if name == "Deposited" else
                        "Status update plus escrow_events"}
             for name, signature in EVENT_SIGNATURES.items()],
            width="stretch", hide_index=True)

        st.markdown("##### Relational schema")
        st.code(SCHEMA_SQL, language="sql")

        st.markdown("##### The Node listener")
        st.code(LISTENER_JS, language="javascript")

    # -----------------------------------------------------------------
    # Gas optimisation tracker
    # -----------------------------------------------------------------
    with tab_gas:
        comparison = gas_comparison()
        st.markdown("#### Packed structs against separate slots")
        st.caption(
            "Six fields in six slots is what a first draft produces. The same "
            "six fit in two by narrowing the amount to uint96 and the deadline "
            "to uint64. Every figure below is computed from the EIP 2929 and "
            "EIP 2200 constants rather than asserted."
        )

        c1, c2 = st.columns(2)
        network_name = c1.selectbox("Price it on",
                                    [n.name for n in NETWORKS if not n.testnet],
                                    key="w3_gas_network")
        deposits = c2.number_input("Deposits per year", min_value=0,
                                   max_value=10_000_000, value=25_000,
                                   step=1_000, key="w3_deposits")
        network = network_by_name(network_name) or NETWORKS[0]
        saved_usd = savings_usd(comparison, int(deposits), network)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{comparison.deposit_saved:,}</div>
    <div class="l">Gas saved per deposit</div></div>
  <div class="app-kpi ok"><div class="n">{comparison.deposit_saved_percent:.0f}%</div>
    <div class="l">Of the storage cost</div></div>
  <div class="app-kpi"><div class="n">{comparison.read_saved:,}</div>
    <div class="l">Gas saved per read</div></div>
  <div class="app-kpi"><div class="n">{saved_usd:,.0f}</div>
    <div class="l">USD saved per year</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### Where the saving comes from")
        st.dataframe(gas_rows(comparison), width="stretch", hide_index=True)

        c3, c4 = st.columns(2)
        with c3:
            st.markdown("**Unpacked, one slot per field**")
            st.dataframe(
                [{"Field": name, "Type": kind, "Bytes": size}
                 for name, kind, size in UNPACKED_SLOTS],
                width="stretch", hide_index=True)
        with c4:
            st.markdown("**Packed, two slots**")
            st.dataframe(
                [{"Slot": name, "Holds": holds, "Bytes used": size}
                 for name, holds, size in PACKED_SLOTS],
                width="stretch", hide_index=True)

        st.markdown("##### The cost of narrowing, stated rather than hidden")
        st.caption(
            "This is a text box rather than a stepper on purpose. A JavaScript "
            "number is exact only to 2^53 - 1, which a wei value passes at "
            "about nine thousandths of an ether, so wei is carried as a string "
            "or a bigint everywhere in this stack."
        )
        amount_text = st.text_input("Deposit amount in wei",
                                    value="2500000000", key="w3_amount")
        parsed, amount, precision_note = parse_wei(amount_text)
        if not parsed:
            st.markdown(
                f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">Refused</span>Packing check</h4>
  <p>{esc(precision_note)}</p>
</div>
""",
                unsafe_allow_html=True,
            )
        else:
            fits, note = packing_note(amount)
            tone = "ok" if fits else "crit"
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">
  {esc('Fits uint96' if fits else 'Above the ceiling')}</span>
  Packing check</h4>
  <div class="app-ev">{esc(precision_note)}</div>
  <p>{esc(note)}</p>
</div>
""",
                unsafe_allow_html=True,
            )

    st.markdown(
        f"""
<div class="app-foot">
Web3 SmartEscrow Architecture Console, engine version {ENGINE_VERSION}. A
simulator: no RPC endpoint is called, no key is loaded and no transaction is
signed, so nothing here can move funds. Identifiers are derived from their
inputs with SHA256 rather than keccak256, and addresses are rendered lowercase
because an EIP 55 checksum needs keccak256 and a fabricated checksum would be
worse than none.
</div>
""",
        unsafe_allow_html=True,
    )
