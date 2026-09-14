# Web3 SmartEscrow Architecture Console

EVM SmartEscrow deployment simulator, Foundry fuzzing ledger, and event indexing
stream.

Live at `/web3-smart-escrow-console` on the hub.

## What is simulated, stated rather than implied

No RPC call is made, no key is loaded and no transaction is signed, so nothing
in this console can move funds. Two consequences are visible in the output and
are deliberate:

- Transaction hashes and addresses are derived from the deployment inputs with
  **SHA256, not keccak256**. A real transaction hash is the keccak256 of a
  signed RLP payload, which cannot exist here. Using a different function on
  purpose means a simulated identifier can never be mistaken for a live one.
- Addresses are rendered **lowercase**. An EIP 55 checksum needs keccak256,
  which the Python standard library does not carry, and a fabricated checksum
  would be worse than none. A lowercase address is still a valid one.

Every gas figure is computed from the named EIP 2929 and EIP 2200 constants in
`core.py` rather than asserted, so the arithmetic can be checked against the
yellow paper instead of trusted.

## The four tabs

**Deployment simulator.** Five networks with their real chain ids and block
times. The receipt is the shape ethers returns, the confirmation table runs from
mined to final at the chain's own block time, and the constructor grants three
roles where no single role can both move funds and change who may move them.

**Foundry fuzzing ledger.** Ten tests: seven fuzz runs at ten thousand runs each
against reentrancy and access control, two invariants on the accounting, and one
unit test on the constructor. A ledger that can only ever report a pass proves
nothing, so a regression build is included that fails one access control test
with a counterexample, and the console can be toggled onto it.

**Event indexing stream.** A Node listener over Deposited, Released and Refunded,
mapped to a two table Postgres schema. The interesting part is not the happy
path: it is the replayed log after a restart, which the unique key on
`(tx_hash, log_index)` absorbs, and the log a reorg removes, which is undone
rather than ignored. An event that does not fit the escrow's current status is
refused, because writing it would leave the database claiming something the
chain never said.

**Gas optimisation tracker.** Six fields in six slots against the same six
packed into two, costed per deposit and per read, with the yearly saving priced
on a chosen network. The narrowing is not free and the console says so: a
deposit above the uint96 ceiling reverts rather than truncating, since a silent
truncation would lose the difference forever.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | The engine. No Streamlit import, so it can sit behind a real deployment script |
| `page.py` | The page, rendered by the hub |
| `../../tests/test_web3_smart_escrow_console.py` | Engine tests, 71 checks |
| `../../tests/test_web3_smart_escrow_console_page.py` | Page tests via AppTest, 34 checks |

## Rules worth keeping when this is extended

- Every table column holds one type, because the table widget serialises
  through Arrow and Arrow refuses a mixed column.
- A wei amount is read through a text field, not a number input. A JavaScript
  number is exact only to 2^53 - 1, which a wei value passes at about nine
  thousandths of an ether, and `st.number_input` refuses a bound above it
  outright. That is the same reason `amount_wei` is `NUMERIC(78, 0)` in the
  schema and a string in the table rows.
- Callbacks read live widget state out of `st.session_state` rather than
  arguments bound at render time, which are one interaction stale.
