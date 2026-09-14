"""TradingView to MT5 bridge diagnostic engine.

A TradingView alert fires, a bridge forwards it, and MT5 fills it. The trader
then asks why the fill was not the price on the chart, and why a stop was hit
when the chart never touched the level. This answers both, with the arithmetic
shown rather than asserted.

The model that makes those answers possible:

  A TradingView chart plots the mid. A broker quotes a bid and an ask around it.
  A long fills at the ask, which is above the chart, and its stop is triggered
  on the bid, which is below the chart. So the chart only has to fall to
  `stop + half the spread` for the stop to fire, and the trader watching the
  chart sees a stop out at a price that never printed.

Pure logic, no Streamlit import, so this is unit testable on its own and can sit
behind the real bridge. Deterministic: nothing here reads the clock or a random
source unless the caller passes one in.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

ENGINE_VERSION = "1.0.0"

BUY = "BUY"
SELL = "SELL"
ACTIONS: tuple[str, ...] = (BUY, SELL)

# Parse failure codes. A refused alert always carries exactly one, so a caller
# branches on the code rather than parsing prose.
ERR_NONE = ""
ERR_INVALID_JSON = "INVALID_JSON"
ERR_MISSING_FIELD = "MISSING_FIELD"
ERR_BAD_NUMBER = "BAD_NUMBER"
ERR_BAD_ACTION = "BAD_ACTION"
ERR_ILLOGICAL_LEVELS = "ILLOGICAL_LEVELS"

ERROR_HEADLINE = {
    ERR_INVALID_JSON: "Payload is not valid JSON",
    ERR_MISSING_FIELD: "Payload is missing a required field",
    ERR_BAD_NUMBER: "A price field is not a number",
    ERR_BAD_ACTION: "Action is neither buy nor sell",
    ERR_ILLOGICAL_LEVELS: "Stop and target sit on the wrong side of entry",
}

REQUIRED_FIELDS: tuple[str, ...] = ("action", "ticker", "price", "sl", "tp")

# Stop out causes
CAUSE_SPREAD = "spread"
CAUSE_LATENCY = "latency"
CAUSE_BOTH = "spread_and_latency"
CAUSE_GENUINE = "genuine"
CAUSE_NONE = "not_stopped"

CAUSE_HEADLINE = {
    CAUSE_SPREAD: "Premature, caused by broker spread",
    CAUSE_LATENCY: "Premature, caused by webhook latency",
    CAUSE_BOTH: "Premature, caused by spread and latency together",
    CAUSE_GENUINE: "Genuine, the chart reached the stop",
    CAUSE_NONE: "Not stopped out",
}


# ---------------------------------------------------------------------------
# Instrument
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Symbol:
    name: str
    digits: int
    point: float           # smallest price increment
    points_per_pip: int    # 10 on a 5 digit forex feed

    def to_points(self, price_difference: float) -> float:
        return round(price_difference / self.point, 1)

    def to_pips(self, price_difference: float) -> float:
        return round(price_difference / (self.point * self.points_per_pip), 2)

    def round(self, price: float) -> float:
        return round(price, self.digits)


EURUSD = Symbol("EURUSD", 5, 0.00001, 10)
XAUUSD = Symbol("XAUUSD", 2, 0.01, 10)
SYMBOLS: tuple[Symbol, ...] = (EURUSD, XAUUSD)


def symbol_by_name(name: str, symbols=SYMBOLS) -> Symbol | None:
    for symbol in symbols:
        if symbol.name.upper() == str(name).strip().upper():
            return symbol
    return None


# ---------------------------------------------------------------------------
# The TradingView alert
# ---------------------------------------------------------------------------

class AlertParseError(Exception):
    """Raised when a webhook payload cannot become a usable alert."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class TvAlert:
    action: str
    ticker: str
    price: float          # the chart price at the moment the alert fired
    stop_loss: float
    take_profit: float
    contracts: float
    strategy: str
    fired_at: str

    @property
    def is_long(self) -> bool:
        return self.action == BUY

    def risk_price(self) -> float:
        """Distance from entry to stop, as the chart sees it."""
        return abs(self.price - self.stop_loss)


SAMPLE_PAYLOAD = json.dumps({
    "action": "buy",
    "ticker": "EURUSD",
    "price": 1.08540,
    "sl": 1.08340,
    "tp": 1.08940,
    "contracts": 1.0,
    "strategy": "London breakout v3",
    "time": "2026-09-12T08:30:00Z",
}, indent=2)


def _as_float(raw, field_name: str) -> float:
    """TradingView sends numbers as numbers or as strings, depending on how the
    alert message was written. Both are accepted; anything else is refused."""
    if isinstance(raw, bool):
        raise AlertParseError(
            ERR_BAD_NUMBER,
            f"{field_name} came through as a boolean, which is never a price.")
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            raise AlertParseError(
                ERR_BAD_NUMBER,
                f"{field_name} is {raw!r}, which is not a number. A placeholder "
                f"left unsubstituted in the alert message usually looks like "
                f"this.") from None
    else:
        raise AlertParseError(
            ERR_BAD_NUMBER,
            f"{field_name} is of type {type(raw).__name__}, which is not a price.")
    if math.isnan(value) or math.isinf(value):
        raise AlertParseError(ERR_BAD_NUMBER,
                              f"{field_name} is {value}, which is not a usable price.")
    return value


def parse_alert(raw: str, now: str = "2026-09-12T08:30:00Z") -> TvAlert:
    """Turn a raw webhook body into an alert, or refuse it with a reason.

    Refusing loudly matters here more than anywhere else in the chain. A bridge
    that accepts a malformed alert places a real order with a wrong level, and
    the first sign of trouble is a filled position nobody intended.
    """
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AlertParseError(
            ERR_INVALID_JSON,
            f"The webhook body is not valid JSON: {exc}. TradingView sends the "
            f"alert message verbatim, so an unquoted key or a trailing comma in "
            f"the message arrives exactly as typed.") from None

    if not isinstance(body, dict):
        raise AlertParseError(
            ERR_INVALID_JSON,
            f"The webhook body parsed as {type(body).__name__}, not an object. "
            f"The bridge expects a JSON object with the alert fields.")

    missing = [name for name in REQUIRED_FIELDS if name not in body]
    if missing:
        raise AlertParseError(
            ERR_MISSING_FIELD,
            f"The payload is missing {', '.join(missing)}. The bridge will not "
            f"guess a level, because a guessed stop is a real loss.")

    action = str(body["action"]).strip().upper()
    if action not in ACTIONS:
        raise AlertParseError(
            ERR_BAD_ACTION,
            f"Action {body['action']!r} is neither buy nor sell, so the bridge "
            f"cannot tell which side to take.")

    price = _as_float(body["price"], "price")
    stop_loss = _as_float(body["sl"], "sl")
    take_profit = _as_float(body["tp"], "tp")
    contracts = _as_float(body.get("contracts", 1.0), "contracts")

    if action == BUY and not (stop_loss < price < take_profit):
        raise AlertParseError(
            ERR_ILLOGICAL_LEVELS,
            f"A buy needs its stop below entry and its target above: got stop "
            f"{stop_loss}, entry {price}, target {take_profit}. Sending this to "
            f"the broker would open a position that is already beyond one of "
            f"its own levels.")
    if action == SELL and not (take_profit < price < stop_loss):
        raise AlertParseError(
            ERR_ILLOGICAL_LEVELS,
            f"A sell needs its stop above entry and its target below: got stop "
            f"{stop_loss}, entry {price}, target {take_profit}.")

    return TvAlert(
        action=action,
        ticker=str(body["ticker"]).strip().upper(),
        price=price, stop_loss=stop_loss, take_profit=take_profit,
        contracts=contracts,
        strategy=str(body.get("strategy", "unnamed strategy")),
        fired_at=str(body.get("time", now)),
    )


# ---------------------------------------------------------------------------
# Broker quote and execution
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Quote:
    """A broker's two sided price built around the chart mid."""
    mid: float
    spread_price: float

    @property
    def bid(self) -> float:
        return self.mid - self.spread_price / 2

    @property
    def ask(self) -> float:
        return self.mid + self.spread_price / 2

    def side_price(self, action: str) -> float:
        """A buy pays the ask, a sell receives the bid."""
        return self.ask if action == BUY else self.bid


def quote_from_spread(mid: float, spread_points: float, symbol: Symbol) -> Quote:
    return Quote(mid=mid, spread_price=spread_points * symbol.point)


@dataclass(frozen=True)
class Latency:
    """Where the milliseconds went between the alert and the fill."""
    webhook_ms: int      # TradingView out to the bridge
    bridge_ms: int       # the bridge's own processing
    broker_ms: int       # the bridge to MT5, and MT5 to a fill

    @property
    def total_ms(self) -> int:
        return self.webhook_ms + self.bridge_ms + self.broker_ms

    @property
    def worst_stage(self) -> str:
        stages = {"webhook": self.webhook_ms, "bridge": self.bridge_ms,
                  "broker": self.broker_ms}
        return max(stages, key=stages.get)

    def rows(self) -> list[dict]:
        """One row per stage, with shares that add up to 100 percent.

        Rounding each share on its own gives 51, 12 and 38, which is 101 and
        looks like a bug to anyone reading the table. The remainder is handed
        to the largest fractions instead, so the column always totals 100.
        """
        stages = (("TradingView to bridge", self.webhook_ms),
                  ("Bridge processing", self.bridge_ms),
                  ("Bridge to MT5 fill", self.broker_ms))
        total = self.total_ms
        if total <= 0:
            return [{"Stage": label, "Milliseconds": value, "Share": "0%"}
                    for label, value in stages]

        exact = [value * 100 / total for _, value in stages]
        shares = [int(value) for value in exact]
        remainder = 100 - sum(shares)
        for index in sorted(range(len(exact)),
                            key=lambda i: exact[i] - shares[i],
                            reverse=True)[:remainder]:
            shares[index] += 1
        return [
            {"Stage": label, "Milliseconds": value, "Share": f"{share}%"}
            for (label, value), share in zip(stages, shares)
        ]


@dataclass(frozen=True)
class Execution:
    alert: TvAlert
    symbol: Symbol
    quote_at_alert: Quote
    quote_at_fill: Quote
    latency: Latency
    fill_price: float
    executed_at: str

    @property
    def spread_points(self) -> float:
        return self.symbol.to_points(self.quote_at_fill.spread_price)

    @property
    def market_move_price(self) -> float:
        """How far the mid travelled while the order was in flight."""
        return self.quote_at_fill.mid - self.quote_at_alert.mid

    @property
    def drift_price(self) -> float:
        """Fill against the price the chart showed when the alert fired."""
        return self.fill_price - self.alert.price

    @property
    def drift_points(self) -> float:
        return self.symbol.to_points(self.drift_price)

    @property
    def spread_component_price(self) -> float:
        """The half spread paid for crossing to the other side of the book."""
        return self.quote_at_fill.spread_price / 2

    @property
    def latency_component_price(self) -> float:
        """Drift that the spread does not explain, which is the market moving
        during the delay."""
        signed = self.market_move_price
        return signed if self.alert.is_long else -signed

    @property
    def slippage_points(self) -> float:
        """Adverse drift in points, positive meaning worse for the trader."""
        adverse = self.drift_price if self.alert.is_long else -self.drift_price
        return self.symbol.to_points(adverse)

    @property
    def adverse(self) -> bool:
        return self.slippage_points > 0


def simulate_execution(alert: TvAlert, symbol: Symbol, spread_points: float,
                       latency: Latency, market_move_points: float = 0.0,
                       now: str | None = None) -> Execution:
    """Fill the alert the way a broker would, and record why the price differs.

    Two effects, kept separate because the trader needs to know which one cost
    them: crossing the spread, which is immediate and known in advance, and the
    market moving during the delay, which is not.
    """
    if spread_points < 0:
        raise ValueError("A spread cannot be negative.")

    quote_at_alert = quote_from_spread(alert.price, spread_points, symbol)
    mid_at_fill = alert.price + market_move_points * symbol.point
    quote_at_fill = quote_from_spread(mid_at_fill, spread_points, symbol)
    fill_price = symbol.round(quote_at_fill.side_price(alert.action))

    fired = _parse(alert.fired_at)
    executed = fired + timedelta(milliseconds=latency.total_ms)
    return Execution(
        alert=alert, symbol=symbol, quote_at_alert=quote_at_alert,
        quote_at_fill=quote_at_fill, latency=latency, fill_price=fill_price,
        executed_at=now or _format(executed))


# ---------------------------------------------------------------------------
# Stop out diagnosis
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StopOutDiagnosis:
    stopped_out: bool
    cause: str
    headline: str
    explanation: str
    chart_extreme: float
    trigger_price: float          # the chart level that actually fires the stop
    spread_gap_points: float      # how far short of the stop the chart can stop
    latency_cost_points: float
    verdict_rows: list[dict] = field(default_factory=list)


def diagnose_stop_out(execution: Execution, chart_extreme: float) -> StopOutDiagnosis:
    """Explain a stop out against what the chart actually printed.

    `chart_extreme` is the furthest the chart mid travelled against the trade:
    the session low for a long, the high for a short.

    The mechanism that surprises people: a long is stopped on the bid, which
    sits half a spread below the chart, so the chart only needs to reach
    `stop + half spread` for the stop to fire. The trader sees a stop out at a
    price that never printed on their screen and concludes the broker is hunting
    them. Usually it is arithmetic.
    """
    alert = execution.alert
    symbol = execution.symbol
    half_spread = execution.quote_at_fill.spread_price / 2

    if alert.is_long:
        # Stop fires when bid <= stop, and bid = mid - half spread.
        trigger_mid = alert.stop_loss + half_spread
        stopped = chart_extreme <= trigger_mid
        chart_reached_stop = chart_extreme <= alert.stop_loss
    else:
        trigger_mid = alert.stop_loss - half_spread
        stopped = chart_extreme >= trigger_mid
        chart_reached_stop = chart_extreme >= alert.stop_loss

    spread_gap_points = symbol.to_points(half_spread)
    latency_cost_points = symbol.to_points(abs(execution.latency_component_price))
    latency_hurt = execution.latency_component_price > 0

    if not stopped:
        cause = CAUSE_NONE
        explanation = (
            f"The chart reached {symbol.round(chart_extreme)} and the stop "
            f"fires at {symbol.round(trigger_mid)} on this spread, so the "
            f"position was not stopped out.")
    elif chart_reached_stop and not latency_hurt:
        cause = CAUSE_GENUINE
        explanation = (
            f"The chart itself reached {symbol.round(chart_extreme)}, at or "
            f"beyond the stop at {symbol.round(alert.stop_loss)}. The spread "
            f"made no difference here: the trade was stopped because price got "
            f"there.")
    elif chart_reached_stop and latency_hurt:
        cause = CAUSE_LATENCY
        explanation = (
            f"The chart did reach the stop, so the stop out was going to "
            f"happen. What latency cost was the entry: {latency_cost_points:.0f} "
            f"points of adverse movement during {execution.latency.total_ms} ms "
            f"in flight, which is real money on a trade that was stopped anyway.")
    else:
        # The chart never touched the stop, yet the stop fired.
        if latency_hurt and latency_cost_points >= spread_gap_points:
            cause = CAUSE_BOTH
            explanation = (
                f"The chart never reached {symbol.round(alert.stop_loss)}, its "
                f"worst print was {symbol.round(chart_extreme)}, and the "
                f"position was stopped anyway. Two causes, and latency is the "
                f"larger: the stop fires on the bid, which sits "
                f"{spread_gap_points:.0f} points inside the chart, and the "
                f"{execution.latency.total_ms} ms delay moved the entry "
                f"{latency_cost_points:.0f} points against the trade before it "
                f"even opened.")
        elif latency_hurt:
            cause = CAUSE_SPREAD
            explanation = (
                f"The chart never reached {symbol.round(alert.stop_loss)}, its "
                f"worst print was {symbol.round(chart_extreme)}. The stop fires "
                f"on the bid, which sits {spread_gap_points:.0f} points inside "
                f"the chart, so the stop triggered at a chart price of "
                f"{symbol.round(trigger_mid)}. Latency added "
                f"{latency_cost_points:.0f} points on entry, but the spread "
                f"alone accounts for the stop out.")
        else:
            cause = CAUSE_SPREAD
            explanation = (
                f"The chart never reached {symbol.round(alert.stop_loss)}, its "
                f"worst print was {symbol.round(chart_extreme)}, yet the "
                f"position was stopped. This is the spread, not the broker "
                f"hunting: the stop fires on the bid, which sits "
                f"{spread_gap_points:.0f} points inside the chart, so a chart "
                f"price of {symbol.round(trigger_mid)} is enough to trigger it. "
                f"Widen the stop by at least the spread, or place it on the "
                f"correct side of the book.")

    rows = [
        {"Measure": "Chart price at alert",
         "Value": f"{symbol.round(execution.alert.price)}"},
        {"Measure": "Fill price",
         "Value": f"{symbol.round(execution.fill_price)}"},
        {"Measure": "Spread",
         "Value": f"{execution.spread_points:.0f} points"},
        {"Measure": "Half spread inside the chart",
         "Value": f"{spread_gap_points:.0f} points"},
        {"Measure": "Stop on the chart",
         "Value": f"{symbol.round(alert.stop_loss)}"},
        {"Measure": "Chart price that actually triggers the stop",
         "Value": f"{symbol.round(trigger_mid)}"},
        {"Measure": "Worst chart print",
         "Value": f"{symbol.round(chart_extreme)}"},
        {"Measure": "Latency in flight",
         "Value": f"{execution.latency.total_ms} ms"},
        {"Measure": "Latency cost on entry",
         "Value": f"{latency_cost_points:.0f} points "
                  f"{'against' if latency_hurt else 'in favour'}"},
    ]

    return StopOutDiagnosis(
        stopped_out=stopped, cause=cause, headline=CAUSE_HEADLINE[cause],
        explanation=explanation, chart_extreme=chart_extreme,
        trigger_price=trigger_mid, spread_gap_points=spread_gap_points,
        latency_cost_points=latency_cost_points, verdict_rows=rows)


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TrailEntry:
    sequence: int
    timestamp: str
    stage: str
    detail: str
    elapsed_ms: int


def build_trail(execution: Execution,
                diagnosis: StopOutDiagnosis | None = None) -> list[TrailEntry]:
    """The exact sequence, with the clock running from the alert."""
    alert = execution.alert
    symbol = execution.symbol
    fired = _parse(alert.fired_at)
    latency = execution.latency

    marks = [
        (0, "TradingView alert",
         f"{alert.action} {alert.ticker} {alert.contracts:g} at chart price "
         f"{symbol.round(alert.price)}, stop {symbol.round(alert.stop_loss)}, "
         f"target {symbol.round(alert.take_profit)}. Strategy "
         f"{alert.strategy}."),
        (latency.webhook_ms, "Bridge received",
         f"Webhook arrived after {latency.webhook_ms} ms in transit from "
         f"TradingView."),
        (latency.webhook_ms + latency.bridge_ms, "Bridge processed",
         f"Payload parsed and translated to an MT5 order in "
         f"{latency.bridge_ms} ms."),
        (latency.total_ms, "MT5 execution",
         f"Filled at {symbol.round(execution.fill_price)} against a "
         f"{execution.spread_points:.0f} point spread. Drift from the chart "
         f"price is {execution.drift_points:+.0f} points, of which "
         f"{symbol.to_points(execution.spread_component_price):.0f} is the half "
         f"spread."),
    ]
    if diagnosis is not None:
        marks.append((latency.total_ms, "Trail update", diagnosis.headline
                      + ". " + diagnosis.explanation))

    return [
        TrailEntry(index + 1, _format(fired + timedelta(milliseconds=offset)),
                   stage, detail, offset)
        for index, (offset, stage, detail) in enumerate(marks)
    ]


def trail_rows(trail: list[TrailEntry]) -> list[dict]:
    return [
        {"#": entry.sequence, "Timestamp": entry.timestamp,
         "Elapsed ms": entry.elapsed_ms, "Stage": entry.stage,
         "Detail": entry.detail}
        for entry in trail
    ]


def _parse(moment: str) -> datetime:
    text = str(moment).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = datetime(2026, 9, 12, 8, 30, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
