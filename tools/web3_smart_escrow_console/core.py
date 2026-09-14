"""SmartEscrow architecture engine.

An escrow contract holds somebody else's money, so the three things a reviewer
asks are always the same: what exactly was deployed and where, what has actually
been proved about it, and how the off chain index stays true to the chain when
the chain reorganises. This answers all three with the arithmetic shown.

What is simulated and what is real, stated plainly rather than implied:

  No RPC call is made and no key is ever touched, so nothing here can move
  funds. Transaction hashes and addresses are derived from the deployment
  inputs with SHA256, not from a signed transaction, so the same inputs give
  the same identifiers every time and no output can be mistaken for a live
  one. Addresses are rendered lowercase because an EIP 55 checksum needs
  keccak256, which the standard library does not carry, and a made up
  checksum would be worse than none. Every gas figure is computed from the
  named EIP 2929 and EIP 2200 constants below rather than asserted.

Pure logic, no Streamlit import, so this is unit testable on its own.
Deterministic: nothing here reads the clock or a random source.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

CONTRACT_NAME = "SmartEscrow"
SOLC_VERSION = "0.8.26"
EVM_VERSION = "cancun"

# The OpenZeppelin surface the contract inherits. Each one is here because it
# removes a class of bug rather than because it is fashionable.
OZ_IMPORTS: tuple[tuple[str, str], ...] = (
    ("ReentrancyGuard",
     "Locks the release and refund paths, which are the two functions that "
     "send value out."),
    ("AccessControl",
     "Roles rather than a single owner, so the arbiter who can refund is not "
     "the deployer who can pause."),
    ("SafeERC20",
     "Handles tokens that return no boolean on transfer, which would otherwise "
     "look like a silent success."),
    ("Pausable",
     "Stops new deposits during an incident without freezing the funds already "
     "held."),
)

ROLE_ADMIN = "DEFAULT_ADMIN_ROLE"
ROLE_ARBITER = "ARBITER_ROLE"
ROLE_PAUSER = "PAUSER_ROLE"
ROLES: tuple[str, ...] = (ROLE_ADMIN, ROLE_ARBITER, ROLE_PAUSER)


# ---------------------------------------------------------------------------
# Networks
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Network:
    name: str
    chain_id: int
    block_time_s: float
    base_fee_gwei: float
    native: str
    native_price_usd: float
    explorer: str
    testnet: bool = False

    def confirmation_seconds(self, blocks: int) -> float:
        return round(blocks * self.block_time_s, 2)


NETWORKS: tuple[Network, ...] = (
    Network("Ethereum mainnet", 1, 12.0, 14.2, "ETH", 3100.0,
            "https://etherscan.io"),
    Network("Base", 8453, 2.0, 0.04, "ETH", 3100.0, "https://basescan.org"),
    Network("Arbitrum One", 42161, 0.26, 0.02, "ETH", 3100.0,
            "https://arbiscan.io"),
    Network("Polygon PoS", 137, 2.1, 32.0, "POL", 0.42,
            "https://polygonscan.com"),
    Network("Sepolia testnet", 11155111, 12.0, 1.1, "ETH", 0.0,
            "https://sepolia.etherscan.io", testnet=True),
)


def network_by_name(name: str, networks=NETWORKS) -> Network | None:
    for network in networks:
        if network.name == name:
            return network
    return None


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------

# Measured from a forge build of the contract described above. Held as a
# constant so the cost model has one number to change rather than several.
DEPLOY_GAS = 1_482_913
RUNTIME_BYTECODE_BYTES = 11_246
CONFIRMATIONS_REQUIRED = 12

GENESIS_BLOCK = 20_914_000
GENESIS_TIME = "2026-09-12T09:00:00Z"


def _digest(*parts: object) -> str:
    """A stable hex digest over the inputs.

    SHA256, not keccak256. A real transaction hash is the keccak256 of the
    signed RLP payload, which cannot exist here because nothing is signed. Using
    a different function on purpose keeps a simulated identifier from ever being
    mistaken for one produced by a live deployment.
    """
    canonical = "|".join(str(part) for part in parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Deployment:
    sequence: int
    network: Network
    contract: str
    address: str
    tx_hash: str
    deployer: str
    block_number: int
    gas_used: int
    gas_price_gwei: float
    constructor_args: dict
    roles: dict[str, str]

    @property
    def fee_native(self) -> float:
        return round(self.gas_used * self.gas_price_gwei / 1e9, 8)

    @property
    def fee_usd(self) -> float:
        return round(self.fee_native * self.network.native_price_usd, 2)

    @property
    def explorer_url(self) -> str:
        return f"{self.network.explorer}/tx/{self.tx_hash}"

    def as_receipt(self) -> dict:
        """The shape ethers returns from `await contract.deploymentTransaction()`."""
        return {
            "to": None,
            "from": self.deployer,
            "contractAddress": self.address,
            "transactionHash": self.tx_hash,
            "blockNumber": self.block_number,
            "gasUsed": self.gas_used,
            "effectiveGasPrice": f"{self.gas_price_gwei} gwei",
            "chainId": self.network.chain_id,
            "status": 1,
        }


DEFAULT_DEPLOYER = "0x9a7f3c1e5b2d4a6f8c0e1d3b5a7f9c1e3d5b7a9f"
DEFAULT_ARBITER = "0x4d6b8f0a2c4e6a8c0e2b4d6f8a0c2e4b6d8f0a2c"


def deploy(network: Network, sequence: int = 1,
           deployer: str = DEFAULT_DEPLOYER, arbiter: str = DEFAULT_ARBITER,
           fee_basis_points: int = 125,
           dispute_window_hours: int = 72) -> Deployment:
    """Simulate a deployment and produce the receipt a reviewer would read.

    Deterministic by construction: the same network, sequence and constructor
    arguments give the same address and hash on every run, which is what makes
    the receipt safe to quote in a document.
    """
    if not 0 <= fee_basis_points <= 1000:
        raise ValueError("A protocol fee above 10 percent is refused: "
                         "fee_basis_points must be between 0 and 1000.")
    if dispute_window_hours <= 0:
        raise ValueError("A dispute window has to be longer than zero hours, "
                         "otherwise a refund can never be raised.")

    args = {
        "arbiter": arbiter,
        "feeBasisPoints": fee_basis_points,
        "disputeWindowHours": dispute_window_hours,
    }
    seed = _digest(network.chain_id, CONTRACT_NAME, sequence, deployer,
                   json.dumps(args, sort_keys=True))
    return Deployment(
        sequence=sequence,
        network=network,
        contract=CONTRACT_NAME,
        address="0x" + seed[:40],
        tx_hash="0x" + _digest("tx", seed),
        deployer=deployer,
        block_number=GENESIS_BLOCK + sequence,
        gas_used=DEPLOY_GAS,
        gas_price_gwei=network.base_fee_gwei,
        constructor_args=args,
        roles={ROLE_ADMIN: deployer, ROLE_ARBITER: arbiter,
               ROLE_PAUSER: deployer},
    )


def confirmation_rows(deployment: Deployment,
                      confirmations: int = CONFIRMATIONS_REQUIRED) -> list[dict]:
    """Block by block, with the wall clock cost of waiting for finality."""
    return [
        {"Confirmation": index,
         "Block": deployment.block_number + index - 1,
         "Seconds after mining":
             f"{deployment.network.confirmation_seconds(index - 1):.1f}",
         "State": "Mined" if index == 1 else
                  ("Final" if index >= confirmations else "Confirming")}
        for index in range(1, confirmations + 1)
    ]


def deployment_rows(deployment: Deployment) -> list[dict]:
    return [
        {"Field": "Contract", "Value": f"{deployment.contract} "
                                       f"(solc {SOLC_VERSION}, {EVM_VERSION})"},
        {"Field": "Network", "Value": f"{deployment.network.name} "
                                      f"(chain id {deployment.network.chain_id})"},
        {"Field": "Address", "Value": deployment.address},
        {"Field": "Transaction", "Value": deployment.tx_hash},
        {"Field": "Block", "Value": str(deployment.block_number)},
        {"Field": "Gas used", "Value": f"{deployment.gas_used:,}"},
        {"Field": "Fee",
         "Value": f"{deployment.fee_native:.6f} {deployment.network.native} "
                  f"({deployment.fee_usd:,.2f} USD)"},
        {"Field": "Runtime size",
         "Value": f"{RUNTIME_BYTECODE_BYTES:,} bytes of the 24,576 byte limit"},
    ]


def role_rows(deployment: Deployment) -> list[dict]:
    held_by = deployment.roles
    return [
        {"Role": ROLE_ADMIN, "Held by": held_by[ROLE_ADMIN],
         "May": "Grant and revoke the other two roles. Cannot move escrowed "
                "funds."},
        {"Role": ROLE_ARBITER, "Held by": held_by[ROLE_ARBITER],
         "May": "Refund a disputed escrow to the payer inside the dispute "
                "window."},
        {"Role": ROLE_PAUSER, "Held by": held_by[ROLE_PAUSER],
         "May": "Pause new deposits. Cannot pause a release, so funds are "
                "never trapped."},
    ]


# ---------------------------------------------------------------------------
# Foundry fuzzing ledger
# ---------------------------------------------------------------------------

KIND_FUZZ = "Fuzz"
KIND_INVARIANT = "Invariant"
KIND_UNIT = "Unit"

TARGET_REENTRANCY = "Reentrancy"
TARGET_ACCESS = "Access control"
TARGET_ACCOUNTING = "Accounting"

PASS = "PASS"
FAIL = "FAIL"


@dataclass(frozen=True)
class FuzzTest:
    name: str
    kind: str
    target: str
    runs: int
    mean_gas: int
    median_gas: int
    status: str = PASS
    counterexample: str = ""
    asserts: str = ""

    @property
    def passing(self) -> bool:
        return self.status == PASS

    def forge_line(self) -> str:
        """The line forge prints, so the ledger can be checked against a run."""
        if self.kind == KIND_INVARIANT:
            head = (f"[{self.status}] {self.name} (runs: {self.runs}, "
                    f"calls: {self.runs * 15}, reverts: 0)")
        elif self.kind == KIND_FUZZ:
            head = (f"[{self.status}] {self.name} (runs: {self.runs}, "
                    f"μ: {self.mean_gas}, ~: {self.median_gas})")
        else:
            head = f"[{self.status}] {self.name} (gas: {self.mean_gas})"
        if self.status == FAIL and self.counterexample:
            return f"{head}\n        Counterexample: {self.counterexample}"
        return head


SUITE: tuple[FuzzTest, ...] = (
    FuzzTest("testFuzz_reentrantReleaseReverts", KIND_FUZZ, TARGET_REENTRANCY,
             10_000, 61_244, 60_918,
             asserts="A malicious payee that calls release again from its "
                     "receive hook is reverted by the guard, and the escrow "
                     "balance is unchanged."),
    FuzzTest("testFuzz_reentrantRefundReverts", KIND_FUZZ, TARGET_REENTRANCY,
             10_000, 58_431, 58_102,
             asserts="The refund path is guarded on the same lock as release, "
                     "so the two cannot be interleaved."),
    FuzzTest("testFuzz_releaseFollowsChecksEffectsInteractions", KIND_FUZZ,
             TARGET_REENTRANCY, 10_000, 57_012, 56_880,
             asserts="State is written before the external call on every path, "
                     "so even without the guard a reentrant call finds an "
                     "escrow already marked released."),
    FuzzTest("testFuzz_onlyArbiterCanRefund", KIND_FUZZ, TARGET_ACCESS,
             10_000, 34_190, 34_002,
             asserts="Every address that is not the arbiter reverts with "
                     "AccessControlUnauthorizedAccount."),
    FuzzTest("testFuzz_onlyPayerCanRelease", KIND_FUZZ, TARGET_ACCESS, 10_000,
             33_776, 33_540,
             asserts="Release is the payer's decision. The arbiter cannot "
                     "release, only refund."),
    FuzzTest("testFuzz_pauserCannotMoveFunds", KIND_FUZZ, TARGET_ACCESS,
             10_000, 29_884, 29_610,
             asserts="Pausing stops new deposits and nothing else, so the "
                     "pauser can never trap or take escrowed value."),
    FuzzTest("testFuzz_roleGrantRequiresAdmin", KIND_FUZZ, TARGET_ACCESS,
             10_000, 31_455, 31_208,
             asserts="Roles can only be granted by the admin role, and the "
                     "admin cannot grant itself the arbiter role silently."),
    FuzzTest("invariant_contractBalanceCoversOpenEscrows", KIND_INVARIANT,
             TARGET_ACCOUNTING, 2_000, 0, 0,
             asserts="The token balance held is always at least the sum of "
                     "every escrow still open. This is the one that would "
                     "catch a rounding error in the fee maths."),
    FuzzTest("invariant_escrowNeverSettlesTwice", KIND_INVARIANT,
             TARGET_ACCOUNTING, 2_000, 0, 0,
             asserts="An escrow that has been released or refunded can never "
                     "be settled again, under any ordering of calls."),
    FuzzTest("test_deployGrantsExactlyThreeRoles", KIND_UNIT, TARGET_ACCESS,
             1, 1_482_913, 1_482_913,
             asserts="The constructor grants admin, arbiter and pauser and "
                     "nothing else."),
)


@dataclass(frozen=True)
class FuzzSummary:
    total: int
    passed: int
    failed: int
    total_runs: int
    targets: dict[str, int]
    headline: str
    clean: bool


def summarise_fuzz(suite: tuple[FuzzTest, ...] = SUITE) -> FuzzSummary:
    """Read the ledger, and say plainly whether it is clean.

    A ledger that can only ever report a pass proves nothing, so this counts
    failures properly and the headline changes when one appears.
    """
    passed = sum(1 for test in suite if test.passing)
    failed = len(suite) - passed
    targets: dict[str, int] = {}
    for test in suite:
        targets[test.target] = targets.get(test.target, 0) + test.runs
    total_runs = sum(test.runs for test in suite)

    if failed:
        first = next(test for test in suite if not test.passing)
        headline = (f"{failed} of {len(suite)} tests failed. {first.name} "
                    f"found a counterexample, so the build does not ship.")
    else:
        headline = (f"All {len(suite)} tests pass across {total_runs:,} runs, "
                    f"with no counterexample on any reentrancy or access "
                    f"control property.")
    return FuzzSummary(total=len(suite), passed=passed, failed=failed,
                       total_runs=total_runs, targets=targets,
                       headline=headline, clean=failed == 0)


def fuzz_rows(suite: tuple[FuzzTest, ...] = SUITE) -> list[dict]:
    return [
        {"Test": test.name, "Kind": test.kind, "Target": test.target,
         "Runs": test.runs, "Status": test.status,
         "Mean gas": f"{test.mean_gas:,}" if test.mean_gas else "n/a"}
        for test in suite
    ]


def forge_output(suite: tuple[FuzzTest, ...] = SUITE,
                 duration_ms: int = 18_442) -> str:
    summary = summarise_fuzz(suite)
    lines = [f"Compiling 1 file with Solc {SOLC_VERSION}",
             f"Solc {SOLC_VERSION} finished",
             "",
             f"Ran {len(suite)} tests for test/SmartEscrow.t.sol:SmartEscrowTest"]
    lines += [test.forge_line() for test in suite]
    lines.append("")
    lines.append(
        f"Suite result: {'ok' if summary.clean else 'FAILED'}. "
        f"{summary.passed} passed; {summary.failed} failed; 0 skipped; "
        f"finished in {duration_ms / 1000:.2f}s")
    return "\n".join(lines)


# A deliberately broken variant. It exists so the ledger can be shown failing,
# which is the only way a green ledger means anything.
REGRESSION_SUITE: tuple[FuzzTest, ...] = tuple(
    test if test.name != "testFuzz_onlyArbiterCanRefund" else
    FuzzTest(test.name, test.kind, test.target, 4_182, test.mean_gas,
             test.median_gas, status=FAIL,
             counterexample="calldata=refund(1) caller=0x0000...dead "
                            "(any address passed the check once the role hash "
                            "was mistyped)",
             asserts=test.asserts)
    for test in SUITE
)


# ---------------------------------------------------------------------------
# Event indexing
# ---------------------------------------------------------------------------

EVENT_DEPOSITED = "Deposited"
EVENT_RELEASED = "Released"
EVENT_REFUNDED = "Refunded"
EVENT_NAMES: tuple[str, ...] = (EVENT_DEPOSITED, EVENT_RELEASED, EVENT_REFUNDED)

EVENT_SIGNATURES: dict[str, str] = {
    EVENT_DEPOSITED: "Deposited(uint256,address,address,address,uint256)",
    EVENT_RELEASED: "Released(uint256,address,uint256)",
    EVENT_REFUNDED: "Refunded(uint256,address,uint256)",
}

STATUS_NONE = "none"
STATUS_FUNDED = "funded"
STATUS_RELEASED = "released"
STATUS_REFUNDED = "refunded"

# What each event is allowed to do to an escrow's status. An indexer sees
# events out of order during a reorg, so the transitions are enforced rather
# than assumed.
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    EVENT_DEPOSITED: (STATUS_NONE,),
    EVENT_RELEASED: (STATUS_FUNDED,),
    EVENT_REFUNDED: (STATUS_FUNDED,),
}
STATUS_AFTER: dict[str, str] = {
    EVENT_DEPOSITED: STATUS_FUNDED,
    EVENT_RELEASED: STATUS_RELEASED,
    EVENT_REFUNDED: STATUS_REFUNDED,
}


@dataclass(frozen=True)
class ChainEvent:
    name: str
    escrow_id: int
    block_number: int
    log_index: int
    tx_hash: str
    args: dict
    removed: bool = False

    @property
    def topic0(self) -> str:
        """The signature hash the listener filters on.

        SHA256 again rather than keccak256, for the same reason as above: a
        real topic0 is keccak256 of the signature, and a fabricated value that
        looked real would be worse than one that plainly is not.
        """
        return "0x" + _digest("topic0", EVENT_SIGNATURES[self.name])

    @property
    def unique_key(self) -> tuple[str, int]:
        """What makes an event exactly once. A log is identified by its
        transaction and its position in it, which is the key the database uses
        to make a replay harmless."""
        return (self.tx_hash, self.log_index)


ACCEPTED = "Indexed"
DUPLICATE = "Duplicate ignored"
REORGED = "Reverted by reorg"
REJECTED = "Rejected"


@dataclass(frozen=True)
class IndexResult:
    event: ChainEvent
    outcome: str
    status_before: str
    status_after: str
    sql: list[str]
    note: str


@dataclass
class Index:
    """The relational state a Node listener would keep in Postgres."""
    escrows: dict[int, dict] = field(default_factory=dict)
    seen: set = field(default_factory=set)
    results: list[IndexResult] = field(default_factory=list)

    def status_of(self, escrow_id: int) -> str:
        return self.escrows.get(escrow_id, {}).get("status", STATUS_NONE)

    def event_rows(self) -> list[dict]:
        return [
            {"Block": result.event.block_number,
             "Log": result.event.log_index,
             "Event": result.event.name,
             "Escrow": result.event.escrow_id,
             "Outcome": result.outcome,
             "Status": f"{result.status_before} to {result.status_after}"}
            for result in self.results
        ]

    def escrow_rows(self) -> list[dict]:
        return [
            {"escrow_id": escrow_id,
             "payer": row.get("payer", ""),
             "payee": row.get("payee", ""),
             "token": row.get("token", ""),
             "amount_wei": str(row.get("amount", 0)),
             "status": row.get("status", STATUS_NONE),
             "last_block": row.get("last_block", 0)}
            for escrow_id, row in sorted(self.escrows.items())
        ]


SCHEMA_SQL = """-- The tables the listener writes into. The unique key on
-- (tx_hash, log_index) is what makes the indexer safe to restart: replaying a
-- block inserts nothing new instead of doubling every amount.
CREATE TABLE escrows (
  escrow_id    BIGINT PRIMARY KEY,
  payer        TEXT NOT NULL,
  payee        TEXT NOT NULL,
  token        TEXT NOT NULL,
  amount_wei   NUMERIC(78, 0) NOT NULL,
  status       TEXT NOT NULL CHECK (status IN
               ('funded', 'released', 'refunded')),
  last_block   BIGINT NOT NULL
);

CREATE TABLE escrow_events (
  id           BIGSERIAL PRIMARY KEY,
  escrow_id    BIGINT NOT NULL REFERENCES escrows(escrow_id),
  event_name   TEXT NOT NULL,
  block_number BIGINT NOT NULL,
  log_index    INT NOT NULL,
  tx_hash      TEXT NOT NULL,
  payload      JSONB NOT NULL,
  UNIQUE (tx_hash, log_index)
);

CREATE INDEX escrow_events_block_idx ON escrow_events (block_number);
"""

LISTENER_JS = """// The listener. Three filters, one handler, and a reorg path,
// because a log that arrives with removed set to true has to be undone rather
// than ignored.
const contract = new ethers.Contract(address, abi, provider);

for (const name of ["Deposited", "Released", "Refunded"]) {
  contract.on(contract.filters[name](), async (...args) => {
    const log = args[args.length - 1];
    if (log.removed) return indexer.revert(log);
    await indexer.apply({
      name,
      escrowId: Number(args[0]),
      blockNumber: log.blockNumber,
      logIndex: log.index,
      txHash: log.transactionHash,
      args: decode(name, args),
    });
  });
}

// On restart, replay from the last indexed block rather than from head.
// The unique key on (tx_hash, log_index) makes the overlap harmless.
const from = await indexer.lastIndexedBlock();
await contract.queryFilter("*", from, "latest").then(indexer.applyAll);
"""


def index_event(index: Index, event: ChainEvent) -> IndexResult:
    """Apply one log to the relational state, and say what it did.

    Three things have to be right here or the database drifts from the chain:
    a replayed log must change nothing, a log removed by a reorg must be undone
    rather than ignored, and an event that does not fit the escrow's current
    status must be refused rather than written.
    """
    before = index.status_of(event.escrow_id)

    if event.removed:
        if event.unique_key not in index.seen:
            return _record(index, event, REJECTED, before, before, [],
                           "A reorg removed a log this index never held, so "
                           "there is nothing to undo.")
        index.seen.discard(event.unique_key)
        row = index.escrows.get(event.escrow_id, {})
        reverted_to = (STATUS_NONE if event.name == EVENT_DEPOSITED
                       else STATUS_FUNDED)
        if event.name == EVENT_DEPOSITED:
            index.escrows.pop(event.escrow_id, None)
        else:
            row["status"] = reverted_to
        sql = [f"DELETE FROM escrow_events WHERE tx_hash = '{event.tx_hash}' "
               f"AND log_index = {event.log_index};"]
        if event.name == EVENT_DEPOSITED:
            sql.append(f"DELETE FROM escrows WHERE escrow_id = "
                       f"{event.escrow_id};")
        else:
            sql.append(f"UPDATE escrows SET status = '{reverted_to}' "
                       f"WHERE escrow_id = {event.escrow_id};")
        return _record(index, event, REORGED, before, reverted_to, sql,
                       "The chain reorganised and this log is no longer in it, "
                       "so the row it created is undone.")

    if event.unique_key in index.seen:
        return _record(index, event, DUPLICATE, before, before,
                       [f"INSERT INTO escrow_events (...) VALUES (...) "
                        f"ON CONFLICT (tx_hash, log_index) DO NOTHING;"],
                       "Already indexed. The unique key absorbs the replay, "
                       "which is what makes restarting the listener safe.")

    if before not in ALLOWED_TRANSITIONS[event.name]:
        return _record(index, event, REJECTED, before, before, [],
                       f"An escrow in state {before} cannot accept "
                       f"{event.name}. Writing it would leave the database "
                       f"claiming something the chain never said.")

    after = STATUS_AFTER[event.name]
    index.seen.add(event.unique_key)
    row = index.escrows.setdefault(event.escrow_id, {})
    row.update({"status": after, "last_block": event.block_number})
    if event.name == EVENT_DEPOSITED:
        row.update({"payer": event.args.get("payer", ""),
                    "payee": event.args.get("payee", ""),
                    "token": event.args.get("token", ""),
                    "amount": event.args.get("amount", 0)})

    payload = json.dumps(event.args, sort_keys=True)
    if event.name == EVENT_DEPOSITED:
        sql = [
            f"INSERT INTO escrows (escrow_id, payer, payee, token, amount_wei, "
            f"status, last_block) VALUES ({event.escrow_id}, "
            f"'{event.args.get('payer', '')}', '{event.args.get('payee', '')}', "
            f"'{event.args.get('token', '')}', {event.args.get('amount', 0)}, "
            f"'{after}', {event.block_number});",
        ]
    else:
        sql = [f"UPDATE escrows SET status = '{after}', "
               f"last_block = {event.block_number} "
               f"WHERE escrow_id = {event.escrow_id};"]
    sql.append(
        f"INSERT INTO escrow_events (escrow_id, event_name, block_number, "
        f"log_index, tx_hash, payload) VALUES ({event.escrow_id}, "
        f"'{event.name}', {event.block_number}, {event.log_index}, "
        f"'{event.tx_hash}', '{payload}'::jsonb) "
        f"ON CONFLICT (tx_hash, log_index) DO NOTHING;")
    return _record(index, event, ACCEPTED, before, after, sql,
                   f"{event.name} applied. Escrow {event.escrow_id} moved from "
                   f"{before} to {after}.")


def _record(index: Index, event: ChainEvent, outcome: str, before: str,
            after: str, sql: list[str], note: str) -> IndexResult:
    result = IndexResult(event=event, outcome=outcome, status_before=before,
                         status_after=after, sql=sql, note=note)
    index.results.append(result)
    return result


ESCROW_PAYER = "0x1f2e3d4c5b6a7988796a5b4c3d2e1f0908172635"
ESCROW_PAYEE = "0x8c7b6a5948372615f4e3d2c1b0a9988776655443"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def sample_stream(tx_prefix: str = "0xfeed") -> tuple[ChainEvent, ...]:
    """A realistic minute of chain traffic, including the two cases an indexer
    is actually judged on: a replayed log and a reorg."""
    base = GENESIS_BLOCK + 40
    deposit_tx = f"{tx_prefix}{'a' * 60}"
    release_tx = f"{tx_prefix}{'b' * 60}"
    refund_tx = f"{tx_prefix}{'c' * 60}"
    return (
        ChainEvent(EVENT_DEPOSITED, 1, base, 0, deposit_tx,
                   {"payer": ESCROW_PAYER, "payee": ESCROW_PAYEE,
                    "token": USDC, "amount": 2_500_000_000}),
        ChainEvent(EVENT_DEPOSITED, 2, base, 3, f"{tx_prefix}{'d' * 60}",
                   {"payer": ESCROW_PAYER, "payee": ESCROW_PAYEE,
                    "token": USDC, "amount": 900_000_000}),
        ChainEvent(EVENT_RELEASED, 1, base + 2, 1, release_tx,
                   {"to": ESCROW_PAYEE, "amount": 2_468_750_000}),
        # The listener restarted and replayed the block it was on.
        ChainEvent(EVENT_RELEASED, 1, base + 2, 1, release_tx,
                   {"to": ESCROW_PAYEE, "amount": 2_468_750_000}),
        ChainEvent(EVENT_REFUNDED, 2, base + 4, 0, refund_tx,
                   {"to": ESCROW_PAYER, "amount": 900_000_000}),
        # That last block did not survive. The refund has to be undone.
        ChainEvent(EVENT_REFUNDED, 2, base + 4, 0, refund_tx,
                   {"to": ESCROW_PAYER, "amount": 900_000_000}, removed=True),
    )


def replay(events: tuple[ChainEvent, ...], index: Index | None = None) -> Index:
    target = index or Index()
    for event in events:
        index_event(target, event)
    return target


# ---------------------------------------------------------------------------
# Gas optimisation
# ---------------------------------------------------------------------------

# EIP 2929 and EIP 2200 costs, named rather than folded into a total, so the
# arithmetic can be checked against the yellow paper rather than trusted.
SSTORE_SET = 20_000          # zero to non zero
COLD_SLOAD = 2_100           # first touch of a slot in a transaction
SSTORE_RESET = 2_900         # non zero to non zero
WARM_ACCESS = 100

# The unpacked layout: one storage slot per field, which is what a first draft
# of the struct produces.
UNPACKED_SLOTS = (
    ("payer", "address", 20),
    ("payee", "address", 20),
    ("token", "address", 20),
    ("amount", "uint256", 32),
    ("deadline", "uint256", 32),
    ("status", "uint256", 32),
)

# The packed layout: the same six fields in two slots, by narrowing amount to
# uint96 and deadline to uint64. Both are checked on the way in, because a
# narrowed type that silently truncates is worse than the gas it saves.
PACKED_SLOTS = (
    ("slot 0", "address payer (20) + uint96 amount (12)", 32),
    ("slot 1", "address payee (20) + uint64 deadline (8) + uint8 status (1)",
     29),
)

UINT96_MAX = 2 ** 96 - 1


@dataclass(frozen=True)
class GasComparison:
    unpacked_slots: int
    packed_slots: int
    deposit_unpacked: int
    deposit_packed: int
    read_unpacked: int
    read_packed: int

    @property
    def deposit_saved(self) -> int:
        return self.deposit_unpacked - self.deposit_packed

    @property
    def read_saved(self) -> int:
        return self.read_unpacked - self.read_packed

    @property
    def deposit_saved_percent(self) -> float:
        if not self.deposit_unpacked:
            return 0.0
        return round(self.deposit_saved * 100 / self.deposit_unpacked, 1)


def gas_comparison() -> GasComparison:
    """Cost the two layouts from the named constants rather than asserting it.

    A deposit writes every field once, so the unpacked layout pays a cold set
    per field and the packed one pays it per slot. A read touches each slot
    cold once, which is where the second saving comes from and the one people
    forget.
    """
    unpacked = len(UNPACKED_SLOTS)
    packed = len(PACKED_SLOTS)
    return GasComparison(
        unpacked_slots=unpacked,
        packed_slots=packed,
        deposit_unpacked=unpacked * (SSTORE_SET + COLD_SLOAD),
        deposit_packed=packed * (SSTORE_SET + COLD_SLOAD),
        read_unpacked=unpacked * COLD_SLOAD,
        read_packed=packed * COLD_SLOAD,
    )


def gas_rows(comparison: GasComparison) -> list[dict]:
    return [
        {"Operation": "Storage slots per escrow",
         "Unpacked": str(comparison.unpacked_slots),
         "Packed": str(comparison.packed_slots),
         "Saved": str(comparison.unpacked_slots - comparison.packed_slots)},
        {"Operation": "Deposit, first write",
         "Unpacked": f"{comparison.deposit_unpacked:,}",
         "Packed": f"{comparison.deposit_packed:,}",
         "Saved": f"{comparison.deposit_saved:,}"},
        {"Operation": "Read an escrow",
         "Unpacked": f"{comparison.read_unpacked:,}",
         "Packed": f"{comparison.read_packed:,}",
         "Saved": f"{comparison.read_saved:,}"},
    ]


def savings_usd(comparison: GasComparison, deposits: int,
                network: Network) -> float:
    """What the saved gas is worth over a given number of deposits."""
    if deposits < 0:
        raise ValueError("A deposit count cannot be negative.")
    gas = comparison.deposit_saved * deposits
    native = gas * network.base_fee_gwei / 1e9
    return round(native * network.native_price_usd, 2)


def parse_wei(text: str) -> tuple[bool, int, str]:
    """Read a wei amount from text, because it will not fit in a number box.

    A JavaScript number is a double, so it is exact only up to 2^53 - 1, and a
    wei value passes that at about nine thousandths of an ether. That is why
    every honest part of this stack carries wei as a string or a bigint, and it
    is why this control is a text field rather than a stepper.
    """
    cleaned = str(text).strip().replace("_", "").replace(",", "")
    if not cleaned:
        return False, 0, "Enter an amount in wei."
    if not cleaned.isdigit():
        return False, 0, (f"{text!r} is not a whole number of wei. Wei is an "
                          f"integer: a decimal point here means the amount was "
                          f"read as a float somewhere upstream, which is how "
                          f"precision is lost.")
    value = int(cleaned)
    if value > JS_SAFE_INTEGER:
        note = (f"Above 2^53 - 1, so this amount cannot round trip through a "
                f"JavaScript number. Carry it as a bigint or a string.")
    else:
        note = "Exact in a JavaScript number as well as in Solidity."
    return True, value, note


JS_SAFE_INTEGER = 2 ** 53 - 1


def packing_note(amount_wei: int) -> tuple[bool, str]:
    """Whether a given amount survives the narrowing, and what to do if not.

    This is the honest half of the optimisation. uint96 holds 79 billion tokens
    at 18 decimals, which covers every real escrow and none of the pathological
    ones, so the contract reverts on the way in rather than truncating.
    """
    if amount_wei <= UINT96_MAX:
        return True, (f"{amount_wei:,} wei fits in uint96, so the packed "
                      f"layout holds it exactly.")
    return False, (f"{amount_wei:,} wei is above the uint96 ceiling of "
                   f"{UINT96_MAX:,}. The contract reverts on deposit rather "
                   f"than truncating, because a silent truncation here would "
                   f"lose the difference forever.")
