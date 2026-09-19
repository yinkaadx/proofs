"""Python Algorithmic Trading Console: the engine.

Three pieces of a trading system where the bug costs money directly rather
than costing a support ticket, so each one is built to fail in the safe
direction and the tests are written to attack the safe direction rather than
to confirm the happy path.

1. Position sizing. The stated risk percent is a ceiling and not a baseline,
   so a setup quality score may only ever scale the size down. A multiplier
   that can exceed one turns a good signal into a blown account on the day
   the signal is wrong, and it is the most common way a sizing engine gets
   written.
2. The profit ratchet. A protected floor that can move backward is not a
   floor. The property is checked against sequences designed to break it,
   including a loss larger than every gain that preceded it, rather than
   against a rising equity curve where any implementation looks correct.
3. Execution routing. Default deny. Live orders need the mode, the
   environment and an explicit arm token to agree, and every other
   combination of the three is blocked. The test enumerates the entire cross
   product and asserts that exactly one cell in it reaches a broker.

Nothing here places an order anywhere, connects to a broker or holds a
credential. It is an engineering demonstration of the safety properties, not
financial advice and not a trading system.

No Streamlit import lives here on purpose, so the same engine could sit behind
a real execution layer without a line changing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

ENGINE_VERSION = "1.0.0"

TWO_PLACES = Decimal("0.01")

SEVERITY_OK = "OK"
SEVERITY_WARN = "WARN"
SEVERITY_CRITICAL = "CRITICAL"


def money(value) -> Decimal:
    """Currency, rounded once, at the point it becomes currency."""
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def money_down(value) -> Decimal:
    """Currency rounded toward zero, for anything that is a risk amount.

    Rounding a risk figure up is how a cap gets exceeded by a cent at a time,
    so every number that represents exposure rounds down instead.
    """
    return Decimal(str(value)).quantize(TWO_PLACES, rounding=ROUND_DOWN)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


def _worst(findings) -> str:
    for level in (SEVERITY_CRITICAL, SEVERITY_WARN):
        if any(f.severity == level for f in findings):
            return level
    return SEVERITY_OK


# ---------------------------------------------------------------------------
# 1. Dynamic position sizing
# ---------------------------------------------------------------------------

# The hard ceiling on what one trade may risk, whatever is asked for. A
# request above this is clamped and reported rather than honoured, because a
# risk parameter is the one input most likely to be wrong by a factor of ten.
MAX_RISK_PERCENT = Decimal("2.0")

# The most of the account that may sit in one position at once, separate from
# what that position risks. A tight stop can make a tiny risk into an
# enormous notional, and the notional is what a gap moves through.
MAX_EXPOSURE_PERCENT = Decimal("20.0")

# Quality multipliers. Every one is at or below one, which is the property
# that matters: quality may reduce a size and may never raise it above the
# stated risk ceiling. A multiplier above one turns a good signal into a
# blown account on the day the signal is wrong.
SETUP_QUALITIES: tuple[tuple[str, Decimal, str], ...] = (
    ("A plus", Decimal("1.00"),
     "Every condition present. Full risk, which is the ceiling and not a "
     "reward."),
    ("A", Decimal("0.75"),
     "One condition soft. Three quarters of the ceiling."),
    ("B", Decimal("0.50"),
     "The setup is there and the context is not. Half."),
    ("C", Decimal("0.25"),
     "Taken for the data rather than for the money. A quarter."),
    ("No trade", Decimal("0.00"),
     "The setup does not qualify. Nothing is sized, because zero is a "
     "position and it is often the right one."),
)

QUALITY_BY_NAME = {name: (mult, note) for name, mult, note in SETUP_QUALITIES}


@dataclass(frozen=True)
class PositionSize:
    account_equity: Decimal
    requested_risk_percent: Decimal
    applied_risk_percent: Decimal
    setup_quality: str
    quality_multiplier: Decimal
    base_risk_amount: Decimal
    risk_amount: Decimal
    max_exposure_amount: Decimal
    effective_risk_percent: Decimal
    tradeable: bool
    risk_was_clamped: bool
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def within_hard_cap(self) -> bool:
        """The property the whole function exists to hold."""
        if self.account_equity <= 0:
            return self.risk_amount == money(0)
        ceiling = money_down(self.account_equity * MAX_RISK_PERCENT / 100)
        return self.risk_amount <= ceiling


def calculate_dynamic_position_size(account_equity, risk_percent,
                                    setup_quality: str) -> PositionSize:
    """Size a trade, where quality may only ever scale the risk down.

    The stated risk percent is a ceiling. Anything above the hard cap is
    clamped and reported rather than honoured, and the quality multiplier is
    at or below one for every grade, so no combination of inputs produces a
    position risking more than the cap allows.
    """
    equity = money(account_equity)
    requested = Decimal(str(risk_percent))
    quality = str(setup_quality or "").strip()

    if quality not in QUALITY_BY_NAME:
        raise ValueError(f"unknown setup quality: {setup_quality!r}")
    if requested < 0:
        raise ValueError("a risk percent cannot be negative")

    multiplier, quality_note = QUALITY_BY_NAME[quality]
    findings: list[Finding] = []

    clamped = requested > MAX_RISK_PERCENT
    applied = MAX_RISK_PERCENT if clamped else requested

    if equity <= 0:
        findings.append(Finding(
            code="SIZE-NO-EQUITY", severity=SEVERITY_CRITICAL,
            title="There is no account to size against",
            detail=("Equity is at or below zero, so every position is zero. "
                    "A sizing engine that returns a number here is one that "
                    "will trade an account that has already gone."),
            fix="Stop the strategy and reconcile the balance before anything else."))
        return PositionSize(
            account_equity=equity, requested_risk_percent=requested,
            applied_risk_percent=Decimal("0"), setup_quality=quality,
            quality_multiplier=multiplier, base_risk_amount=money(0),
            risk_amount=money(0), max_exposure_amount=money(0),
            effective_risk_percent=Decimal("0"), tradeable=False,
            risk_was_clamped=clamped,
            headline="No equity, so no position is sized",
            findings=tuple(findings))

    if clamped:
        findings.append(Finding(
            code="SIZE-CLAMPED", severity=SEVERITY_CRITICAL,
            title=f"{requested} percent was clamped to the {MAX_RISK_PERCENT} percent cap",
            detail=("A risk parameter is the input most likely to be wrong by "
                    "a factor of ten, so the cap is applied rather than the "
                    "request. The request was not honoured and this is not a "
                    "warning that can be dismissed by retrying."),
            fix=("Fix the configuration that produced the figure. A clamp "
                 "firing in production means something upstream believes a "
                 "number the engine will not act on.")))

    base_risk = money_down(equity * applied / 100)
    risk = money_down(base_risk * multiplier)
    exposure_cap = money_down(equity * MAX_EXPOSURE_PERCENT / 100)
    effective = (risk / equity * 100) if equity else Decimal("0")

    tradeable = risk > 0

    if multiplier == 0:
        findings.append(Finding(
            code="SIZE-NO-TRADE", severity=SEVERITY_OK,
            title="This setup does not qualify, so nothing is sized",
            detail=quality_note,
            fix=("Zero is a position. Logging the setups that were passed is "
                 "worth as much as logging the ones that were taken.")))
    elif multiplier < 1:
        findings.append(Finding(
            code="SIZE-SCALED", severity=SEVERITY_OK,
            title=f"Grade {quality} scales the risk to {multiplier} of the ceiling",
            detail=quality_note,
            fix=("Keep every multiplier at or below one. The day a grade is "
                 "allowed above one is the day the ceiling stops being a "
                 "ceiling.")))
    else:
        findings.append(Finding(
            code="SIZE-FULL", severity=SEVERITY_WARN,
            title="Full risk, which is the ceiling rather than a reward",
            detail=("The best grade takes the whole allowance and no more. "
                    "There is nothing above this and there should not be."),
            fix=("Check how often the top grade fires. A strategy where every "
                 "setup is the best grade has a grading function that is not "
                 "grading.")))

    findings.append(Finding(
        code="SIZE-EXPOSURE", severity=SEVERITY_WARN,
        title=f"Risk and exposure are different limits",
        detail=(f"This trade risks {risk} and may hold up to "
                f"{exposure_cap} of notional. A tight stop turns a small "
                f"risk into a large position, and a gap moves through the "
                f"notional rather than through the stop."),
        fix=("Check the position against the exposure cap as well as the "
             "risk figure. Only one of them survives a gap.")))

    findings.append(Finding(
        code="SIZE-NOT-ADVICE", severity=SEVERITY_WARN,
        title="This is an engineering demonstration and not advice",
        detail=("No order is placed, no broker is contacted and no market "
                "data is read. The numbers show how the safety properties "
                "behave, not what anyone should trade."),
        fix="Use your own risk parameters and your own judgement."))

    headline = (f"{equity} at grade {quality} sizes a risk of {risk}, which "
                f"is {effective:.3f} percent of the account against a "
                f"{MAX_RISK_PERCENT} percent cap")

    return PositionSize(
        account_equity=equity, requested_risk_percent=requested,
        applied_risk_percent=applied, setup_quality=quality,
        quality_multiplier=multiplier, base_risk_amount=base_risk,
        risk_amount=risk, max_exposure_amount=exposure_cap,
        effective_risk_percent=effective.quantize(Decimal("0.001")),
        tradeable=tradeable, risk_was_clamped=clamped, headline=headline,
        findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# 2. The profit ratchet
# ---------------------------------------------------------------------------

# Each tier locks a share of cumulative profit once it is reached. The share
# rises with the tier and never reaches one, because a floor equal to the
# balance is a system that cannot place another trade.
MILESTONE_TIERS: tuple[tuple[str, Decimal, Decimal], ...] = (
    ("None", Decimal("0"), Decimal("0.00")),
    ("Tier 1", Decimal("1000"), Decimal("0.50")),
    ("Tier 2", Decimal("2500"), Decimal("0.60")),
    ("Tier 3", Decimal("5000"), Decimal("0.70")),
    ("Tier 4", Decimal("10000"), Decimal("0.75")),
)

TIER_BY_NAME = {name: (threshold, lock)
                for name, threshold, lock in MILESTONE_TIERS}
TIER_NAMES: tuple[str, ...] = tuple(name for name, _t, _l in MILESTONE_TIERS)


@dataclass(frozen=True)
class RatchetState:
    daily_pnl: Decimal
    milestone_tier: str
    previous_floor: Decimal
    previous_cumulative: Decimal
    cumulative_profit: Decimal
    peak_profit: Decimal
    candidate_floor: Decimal
    protected_floor: Decimal
    floor_moved: bool
    floor_moved_backward: bool
    tier_threshold: Decimal
    lock_ratio: Decimal
    headline: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def simulate_profit_ratchet(daily_pnl, milestone_tier: str,
                            previous_floor=Decimal("0"),
                            previous_cumulative=Decimal("0"),
                            peak_profit=None) -> RatchetState:
    """Move the protected floor up, or leave it exactly where it was.

    The floor is the maximum of where it was and what this tier would lock
    against the highest cumulative profit yet reached. Taking the maximum
    against the previous value is what makes it a ratchet: a loss changes the
    cumulative profit and cannot touch the floor, however large it is.

    The peak is carried rather than recomputed from the running total,
    because locking against the current total would let a drawdown lower the
    lock and that is the same bug wearing a different hat.
    """
    pnl = money(daily_pnl)
    tier = str(milestone_tier or "").strip()
    if tier not in TIER_BY_NAME:
        raise ValueError(f"unknown milestone tier: {milestone_tier!r}")
    floor_before = money(previous_floor)
    cumulative_before = money(previous_cumulative)
    if floor_before < 0:
        raise ValueError("a protected floor cannot start below zero")

    threshold, lock = TIER_BY_NAME[tier]
    cumulative = money(cumulative_before + pnl)
    peak_before = (money(peak_profit) if peak_profit is not None
                   else max(cumulative_before, money(0)))
    peak = max(peak_before, cumulative)

    findings: list[Finding] = []

    if peak >= threshold and lock > 0:
        candidate = money_down(peak * lock)
    else:
        candidate = money(0)

    # The ratchet itself. One line, and the reason the whole function exists.
    protected = max(floor_before, candidate)
    moved = protected > floor_before
    moved_backward = protected < floor_before

    if peak < threshold and lock > 0:
        findings.append(Finding(
            code="RTC-BELOW-TIER", severity=SEVERITY_OK,
            title=f"{tier} needs {threshold} of peak profit and the peak is {peak}",
            detail="Nothing is locked yet, so the floor stays where it was.",
            fix="The tier locks the moment the peak reaches the threshold, not the balance."))
    elif moved:
        findings.append(Finding(
            code="RTC-RAISED", severity=SEVERITY_OK,
            title=f"The floor rises from {floor_before} to {protected}",
            detail=(f"{tier} locks {lock * 100:.0f} percent of a peak of "
                    f"{peak}. From here the strategy stops if equity falls "
                    f"back to that figure."),
            fix=("A floor is only real if something enforces it. If no code "
                 "path halts trading at this number, it is a number.")))
    else:
        findings.append(Finding(
            code="RTC-HELD", severity=SEVERITY_OK,
            title=f"The floor holds at {protected}",
            detail=(f"This tier would lock {candidate} against a peak of "
                    f"{peak}, which is not above where the floor already "
                    f"is, so nothing moves."),
            fix="Holding is the correct outcome. A floor that drops is not a floor."))

    if pnl < 0:
        findings.append(Finding(
            code="RTC-LOSS-SAFE", severity=SEVERITY_OK,
            title=f"A loss of {abs(pnl)} did not touch the floor",
            detail=("Cumulative profit fell and the protected floor did not, "
                    "which is the only behaviour that makes the word floor "
                    "mean anything."),
            fix=("The tests attack this with a loss larger than every gain "
                 "before it, because a rising equity curve makes any "
                 "implementation look correct.")))

    if lock >= 1:
        findings.append(Finding(
            code="RTC-LOCKED-OUT", severity=SEVERITY_CRITICAL,
            title="A lock ratio of one leaves nothing to trade with",
            detail=("Protecting the whole balance halts the strategy "
                    "permanently the moment the tier is reached."),
            fix="Keep every lock ratio below one."))

    findings.append(Finding(
        code="RTC-ENFORCE", severity=SEVERITY_WARN,
        title="The floor has to be enforced somewhere other than here",
        detail=("This function computes the number. Something else has to "
                "refuse to open a position when equity is at or below it, "
                "and that something is usually the part nobody writes."),
        fix=("Put the check in the order path rather than in the dashboard. "
             "A floor displayed and not enforced is worse than no floor, "
             "because it is believed.")))

    headline = (f"{tier} against a peak of {peak}: the floor is {protected}, "
                f"{'raised' if moved else 'unchanged'}")

    return RatchetState(
        daily_pnl=pnl, milestone_tier=tier, previous_floor=floor_before,
        previous_cumulative=cumulative_before, cumulative_profit=cumulative,
        peak_profit=peak, candidate_floor=candidate, protected_floor=protected,
        floor_moved=moved, floor_moved_backward=moved_backward,
        tier_threshold=threshold, lock_ratio=lock, headline=headline,
        findings=tuple(findings),
    )


def replay_ratchet(daily_results, milestone_tier: str = "Tier 1") -> dict:
    """Fold a sequence of daily results through the ratchet.

    The proof rather than the demonstration: whatever the sequence, the floor
    series must be non decreasing from start to end.
    """
    floor = money(0)
    cumulative = money(0)
    peak = money(0)
    rows: list[RatchetState] = []
    for pnl in daily_results:
        state = simulate_profit_ratchet(pnl, milestone_tier, floor,
                                        cumulative, peak)
        floor = state.protected_floor
        cumulative = state.cumulative_profit
        peak = state.peak_profit
        rows.append(state)
    floors = [row.protected_floor for row in rows]
    return {
        "days": len(rows),
        "final_floor": floor,
        "final_cumulative": cumulative,
        "peak": peak,
        "floors": tuple(floors),
        "monotonic": all(b >= a for a, b in zip(floors, floors[1:])),
        "ever_moved_backward": any(row.floor_moved_backward for row in rows),
        "rows": tuple(rows),
    }


# A sequence written to break a naive ratchet: it climbs past a tier, then
# gives back more than it ever made.
ADVERSARIAL_DAYS: tuple[str, ...] = (
    "400.00", "900.00", "1200.00", "-300.00", "800.00",
    "-4500.00", "200.00", "-900.00", "1500.00", "-2000.00",
)


# ---------------------------------------------------------------------------
# 3. Execution routing
# ---------------------------------------------------------------------------

MODE_PAPER = "Paper"
MODE_LIVE = "Live"
MODES: tuple[str, ...] = (MODE_PAPER, MODE_LIVE)

ENV_PAPER = "Paper"
ENV_LIVE = "Live"
ENVIRONMENTS: tuple[str, ...] = (ENV_PAPER, ENV_LIVE)

STATUS_SIMULATED = "SIMULATED"
STATUS_ROUTED_LIVE = "ROUTED TO BROKER"
STATUS_BLOCKED = "BLOCKED"

# The token that has to be presented alongside a live mode in a live
# environment. It exists so that reaching a broker takes a deliberate act
# rather than a configuration default, and so that a test can prove no
# accidental combination reaches one.
ARM_TOKEN = "ARM-LIVE-EXECUTION"

REQUIRED_ORDER_FIELDS: tuple[str, ...] = ("symbol", "side", "quantity")
VALID_SIDES: tuple[str, ...] = ("buy", "sell")


@dataclass(frozen=True)
class ExecutionResult:
    mode: str
    environment: str
    status: str
    reached_broker: bool
    reason: str
    order_payload: dict
    audit_line: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)


def route_broker_execution(mode: str, order_payload: dict,
                           environment: str = ENV_PAPER,
                           arm_token: str = "") -> ExecutionResult:
    """Decide whether an order reaches a broker, defaulting to no.

    Three things have to agree before anything leaves: the caller asked for
    live, the environment says live, and the arm token was presented. Every
    other combination is blocked, including an unknown mode and an unknown
    environment, because failing open on an unrecognised value is how a
    staging deploy sends real orders.
    """
    requested = str(mode or "").strip()
    env = str(environment or "").strip()
    token = str(arm_token or "").strip()
    payload = dict(order_payload or {})

    findings: list[Finding] = []

    def blocked(code: str, title: str, detail: str, fix: str,
                reason: str) -> ExecutionResult:
        findings.append(Finding(code=code, severity=SEVERITY_CRITICAL,
                                title=title, detail=detail, fix=fix))
        return ExecutionResult(
            mode=requested, environment=env, status=STATUS_BLOCKED,
            reached_broker=False, reason=reason, order_payload=payload,
            audit_line=(f"mode={requested or 'none'} env={env or 'none'} "
                        f"status={STATUS_BLOCKED} reached_broker=false "
                        f"reason={code}"),
            findings=tuple(findings))

    if requested not in MODES:
        return blocked(
            "EXE-MODE-UNKNOWN",
            f"{requested or 'The mode'} is not a mode this router accepts",
            ("An unrecognised mode is blocked rather than guessed. Treating "
             "an unknown value as paper would be safe today and unsafe the "
             "day somebody adds a third mode."),
            f"Pass one of {' or '.join(MODES)}.",
            "unknown mode")

    if env not in ENVIRONMENTS:
        return blocked(
            "EXE-ENV-UNKNOWN",
            f"{env or 'The environment'} is not a known environment",
            ("An unset or unrecognised environment flag is blocked. Failing "
             "open on a missing variable is how a staging deploy sends real "
             "orders on its first run."),
            ("Set the environment explicitly in the deployment rather than "
             "defaulting it in code."),
            "unknown environment")

    missing = tuple(f for f in REQUIRED_ORDER_FIELDS
                    if not str(payload.get(f, "")).strip())
    if missing:
        return blocked(
            "EXE-PAYLOAD",
            f"The order is missing {', '.join(missing)}",
            ("An incomplete order is blocked in both modes. A malformed "
             "order that a paper router accepts is an order the live router "
             "will be asked to accept later."),
            "Validate the payload where it is built, not at the boundary.",
            "incomplete payload")

    side = str(payload.get("side", "")).strip().lower()
    if side not in VALID_SIDES:
        return blocked(
            "EXE-SIDE",
            f"{payload.get('side')!r} is not a side",
            f"A side is {' or '.join(VALID_SIDES)} and nothing else.",
            "Reject it at the strategy rather than at the router.",
            "invalid side")

    try:
        quantity = Decimal(str(payload.get("quantity")))
    except Exception:                                  # noqa: BLE001
        quantity = Decimal("-1")
    if quantity <= 0:
        return blocked(
            "EXE-QUANTITY",
            f"{payload.get('quantity')!r} is not a tradeable quantity",
            "A quantity at or below zero is a bug, not an order.",
            "Check the sizing engine returned something before routing it.",
            "invalid quantity")

    # The gate. Live needs all three to agree, and nothing else does.
    if requested == MODE_LIVE:
        if env != ENV_LIVE:
            return blocked(
                "EXE-PAPER-ENV",
                "Live execution was requested in a paper environment",
                (f"The caller asked for {MODE_LIVE} while the environment "
                 f"flag says {env}. The environment wins, always, and this "
                 f"is the single check the whole router exists for."),
                ("If live trading is intended, change the environment "
                 "deliberately and present the arm token. Never change the "
                 "mode to work around the environment."),
                "live requested in paper environment")
        if token != ARM_TOKEN:
            return blocked(
                "EXE-NOT-ARMED",
                "Live execution is not armed",
                ("Mode and environment agree and the arm token was not "
                 "presented. Reaching a broker takes a deliberate act rather "
                 "than a configuration default."),
                ("Present the arm token from a place a person had to touch, "
                 "not from the same config file that sets the mode."),
                "not armed")

        findings.append(Finding(
            code="EXE-LIVE", severity=SEVERITY_CRITICAL,
            title="This order reaches a real broker",
            detail=("Mode, environment and arm token all agree. Money moves "
                    "on this one."),
            fix=("Log the payload and the decision before the call rather "
                 "than after the fill, so a rejected order is as traceable "
                 "as an accepted one.")))
        findings.append(Finding(
            code="EXE-NOT-ADVICE", severity=SEVERITY_WARN,
            title="This console contacts nothing",
            detail=("Even in this state no order is sent anywhere. The "
                    "status describes what the router would decide, not "
                    "something it did."),
            fix="Treat it as a demonstration of the gate and nothing more."))
        return ExecutionResult(
            mode=requested, environment=env, status=STATUS_ROUTED_LIVE,
            reached_broker=True, reason="all three gates agree",
            order_payload=payload,
            audit_line=(f"mode={requested} env={env} "
                        f"status={STATUS_ROUTED_LIVE} reached_broker=true "
                        f"symbol={payload.get('symbol')} side={side} "
                        f"quantity={quantity}"),
            findings=tuple(findings))

    findings.append(Finding(
        code="EXE-SIMULATED", severity=SEVERITY_OK,
        title="Simulated, and nothing left the process",
        detail=("Paper mode fills against the simulator. The same validation "
                "ran as would run live, on purpose, so a payload that works "
                "here is one that will not be rejected at the boundary "
                "later."),
        fix=("Keep the validation identical in both modes. Paper that is "
             "more forgiving than live teaches the strategy the wrong "
             "lesson.")))
    if env == ENV_LIVE:
        findings.append(Finding(
            code="EXE-PAPER-IN-LIVE", severity=SEVERITY_WARN,
            title="Paper mode inside a live environment",
            detail=("Safe, and worth noticing. A strategy left in paper mode "
                    "in the live environment is a strategy producing signals "
                    "nobody is acting on."),
            fix="Check whether this was intended or left over from a deploy."))

    return ExecutionResult(
        mode=requested, environment=env, status=STATUS_SIMULATED,
        reached_broker=False, reason="paper mode",
        order_payload=payload,
        audit_line=(f"mode={requested} env={env} status={STATUS_SIMULATED} "
                    f"reached_broker=false symbol={payload.get('symbol')} "
                    f"side={side} quantity={quantity}"),
        findings=tuple(findings),
    )


SAMPLE_ORDER: dict = {"symbol": "MSFT", "side": "buy", "quantity": "40"}
