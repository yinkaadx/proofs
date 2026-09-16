"""Sports betting quantitative edge engine.

An educational simulator of the arithmetic, not betting advice and not a
system. Every figure here comes from the inputs on screen; no market is read
and no result is predicted.

Pure logic, no Streamlit import, so this is unit testable on its own.
Deterministic: nothing reads the clock or a random source, so a backtest run
twice gives the same answer twice, which is the minimum a backtest has to do
before anybody believes one.

THE ERROR THIS ENGINE IS BUILT AROUND

One over the decimal odds is not the market's probability. A book prices both
sides so the raw reciprocals sum to more than one, and that excess is the
margin. De vigging removes it and recovers what the market actually thinks.

The trap is what people do next. Having de vigged, they compare the model to
the true market view, find the model is a few percent higher, and call that an
edge. It is not an edge. It is a disagreement, and a disagreement only becomes
money after it clears the margin.

The arithmetic is unforgiving about this. On 1.91 each side the true market
probability is 0.500 and the break even probability is 1/1.91, which is 0.5236.
A model that is three percent better than the true market says 0.515, looks
like it has found something, and has an expected value of 0.515 times 1.91
minus one, which is minus 1.6 percent. It loses money on every bet while its
dashboard shows a positive edge.

So this engine reports both numbers and never lets one stand alone. The
probability edge says how much the model disagrees with the market. The
expected value says whether that disagreement pays. When the first is positive
and the second is negative, the engine names it as the margin illusion rather
than showing a green number.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

DISCLAIMER = (
    "An educational simulator of the arithmetic. Not betting advice, not a "
    "system, and no guarantee of any outcome. A measured edge is an estimate "
    "with error bars, and the error bars are what ruin bankrolls."
)

# ---------------------------------------------------------------------------
# Odds and the margin
# ---------------------------------------------------------------------------


def implied_probability(decimal_odds: float) -> float:
    """The raw reciprocal. NOT the market's probability, because of the margin."""
    if decimal_odds <= 1.0:
        raise ValueError(
            f"decimal odds of {decimal_odds} are impossible. Decimal odds "
            f"include the stake, so they are always above 1.0.")
    return 1.0 / decimal_odds


def overround(odds_list) -> float:
    """How much more than one the raw probabilities sum to.

    This is the book's margin expressed as a total. A fair two way market sums
    to exactly 1.0; anything above it is the house.
    """
    return sum(implied_probability(o) for o in odds_list)


def devig(odds_list) -> list:
    """Strip the margin proportionally so the probabilities sum to one.

    The multiplicative method: divide each raw probability by the overround.
    It assumes the margin is spread evenly across the outcomes, which is the
    standard simple treatment and is stated here rather than hidden, because
    on heavy favourites the margin is not evenly spread and this understates
    the favourite's true price.
    """
    total = overround(odds_list)
    if total <= 0:
        raise ValueError("cannot de vig an empty market")
    return [implied_probability(o) / total for o in odds_list]


def fair_odds(probability: float) -> float:
    """The price a market with no margin would offer."""
    if not 0 < probability < 1:
        raise ValueError(f"a probability of {probability} is not between 0 "
                         f"and 1 exclusive")
    return 1.0 / probability


# ---------------------------------------------------------------------------
# Part one: referee factor
# ---------------------------------------------------------------------------

SPORT_FOOTBALL = "Football"
SPORT_BASKETBALL = "Basketball"
SPORTS: tuple = (SPORT_FOOTBALL, SPORT_BASKETBALL)

SIG_STRONG = "Significant after correction"
SIG_NOMINAL = "Nominally significant only"
SIG_NONE = "Not significant"

# How many referees a season's analysis typically sweeps. It matters because
# testing many of them at once is what turns noise into a finding.
REFEREE_POOL = 30
ALPHA = 0.05


@dataclass(frozen=True)
class RefereeSample:
    name: str
    sport: str
    matches: int
    fouls_per_match: float
    league_fouls_per_match: float
    fouls_sigma: float
    over_rate: float                 # share of matches going over the line
    league_over_rate: float


REFEREE_SAMPLES: tuple = (
    RefereeSample("M. Oliveira", SPORT_FOOTBALL, 148, 27.4, 22.1, 4.8,
                  0.601, 0.512),
    RefereeSample("K. Anderson", SPORT_FOOTBALL, 96, 23.8, 22.1, 4.8,
                  0.541, 0.512),
    RefereeSample("R. Nakamura", SPORT_FOOTBALL, 41, 25.9, 22.1, 4.8,
                  0.585, 0.512),
    RefereeSample("D. Whitfield", SPORT_BASKETBALL, 210, 44.6, 41.2, 5.6,
                  0.557, 0.504),
    RefereeSample("S. Petrov", SPORT_BASKETBALL, 63, 40.1, 41.2, 5.6,
                  0.476, 0.504),
)


def normal_two_tail_p(z: float) -> float:
    """Two tailed p value from a z score, via the error function.

    math.erfc is exact enough here and needs no dependency, which keeps this
    module importable anywhere.
    """
    return math.erfc(abs(z) / math.sqrt(2.0))


@dataclass(frozen=True)
class RefereeFactor:
    name: str
    sport: str
    matches: int
    foul_deviation: float            # fouls per match above the league mean
    foul_deviation_percent: float
    over_bias: float                 # over rate above the league rate
    z_score: float
    p_value: float
    bonferroni_alpha: float
    significance: str
    note: str

    @property
    def significant(self) -> bool:
        return self.significance == SIG_STRONG

    @property
    def tone(self) -> str:
        return {SIG_STRONG: "ok", SIG_NOMINAL: "warn"}.get(
            self.significance, "crit")

    def rows(self) -> list:
        return [
            {"Measure": "Matches in sample", "Value": f"{self.matches}"},
            {"Measure": "Fouls per match against league",
             "Value": f"{self.foul_deviation:+.2f} "
                      f"({self.foul_deviation_percent:+.1f}%)"},
            {"Measure": "Over rate against league",
             "Value": f"{self.over_bias:+.1%}"},
            {"Measure": "z score", "Value": f"{self.z_score:.3f}"},
            {"Measure": "p value", "Value": f"{self.p_value:.4f}"},
            {"Measure": "Corrected threshold",
             "Value": f"{self.bonferroni_alpha:.5f}"},
            {"Measure": "Verdict", "Value": self.significance},
        ]


def analyze_referee_factor(referee_name: str = "M. Oliveira",
                           sport: str = SPORT_FOOTBALL,
                           pool_size: int = REFEREE_POOL) -> RefereeFactor:
    """Measure one referee's deviation, and judge it against the whole search.

    The correction is the point. Sweeping thirty referees at a five percent
    threshold produces roughly one and a half false findings per season by
    chance alone, so a bare p value under 0.05 is exactly what noise looks
    like. Bonferroni divides the threshold by the number of tests, which is
    conservative and is the right direction to be conservative in when the
    output is a stake.
    """
    if sport not in SPORTS:
        raise ValueError(f"unknown sport {sport!r}. Defined: {list(SPORTS)}.")
    sample = next((s for s in REFEREE_SAMPLES
                   if s.name == referee_name and s.sport == sport), None)
    if sample is None:
        raise ValueError(f"no sample for {referee_name!r} in {sport}.")

    deviation = sample.fouls_per_match - sample.league_fouls_per_match
    percent = (deviation / sample.league_fouls_per_match) * 100
    standard_error = sample.fouls_sigma / math.sqrt(max(sample.matches, 1))
    z = deviation / standard_error if standard_error else 0.0
    p = normal_two_tail_p(z)
    corrected = ALPHA / max(pool_size, 1)

    if p < corrected:
        significance = SIG_STRONG
        note = (f"Survives correction for searching {pool_size} referees. "
                f"p of {p:.4g} is below the corrected threshold of "
                f"{corrected:.5f}, so this is not what noise across a pool "
                f"this size looks like.")
    elif p < ALPHA:
        significance = SIG_NOMINAL
        note = (f"Below 0.05 but above the corrected threshold of "
                f"{corrected:.5f}. Testing {pool_size} referees at five "
                f"percent yields about {pool_size * ALPHA:.1f} findings a "
                f"season by chance, so a result in this band is the most "
                f"likely thing to find and the least safe thing to stake on.")
    else:
        significance = SIG_NONE
        note = (f"p of {p:.4g} is not significant even before correcting for "
                f"the {pool_size} referees searched.")

    return RefereeFactor(
        sample.name, sample.sport, sample.matches, round(deviation, 3),
        round(percent, 2), round(sample.over_rate - sample.league_over_rate, 4),
        round(z, 4), round(p, 6), round(corrected, 6), significance, note)


def referee_rows(sport: str = SPORT_FOOTBALL, pool_size: int = REFEREE_POOL
                 ) -> list:
    rows = []
    for sample in REFEREE_SAMPLES:
        if sample.sport != sport:
            continue
        factor = analyze_referee_factor(sample.name, sport, pool_size)
        rows.append({
            "Referee": factor.name,
            "Matches": str(factor.matches),
            "Foul deviation": f"{factor.foul_deviation:+.2f}",
            "Over bias": f"{factor.over_bias:+.1%}",
            "p value": f"{factor.p_value:.4f}",
            "Verdict": factor.significance,
        })
    return rows


# ---------------------------------------------------------------------------
# Part two: the ensemble edge
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnsembleEdge:
    submodel_probs: tuple
    weights: tuple
    model_probability: float
    break_even_probability: float
    market_probability: float
    overround: float
    decimal_odds: float
    opposing_odds: float
    probability_edge_percent: float
    expected_value_percent: float
    margin_hurdle_percent: float
    fair_odds: float
    disagreement: float

    @property
    def margin_illusion(self) -> bool:
        """The model disagrees with the market and still loses money.

        The single most common way a betting dashboard lies. A positive
        probability edge that does not clear the margin is not a small edge,
        it is a negative one.
        """
        return self.probability_edge_percent > 0 >= self.expected_value_percent

    @property
    def profitable(self) -> bool:
        """The only test that matters. Expected value, not disagreement."""
        return self.expected_value_percent > 0

    @property
    def tone(self) -> str:
        if not self.profitable:
            return "crit"
        return "warn" if self.disagreement > 0.08 else "ok"

    def rows(self) -> list:
        return [
            {"Measure": "Composite model probability",
             "Value": f"{self.model_probability:.4f}"},
            {"Measure": "Break even probability, 1 over the odds",
             "Value": f"{self.break_even_probability:.4f}"},
            {"Measure": "Market probability after de vig",
             "Value": f"{self.market_probability:.4f}"},
            {"Measure": "Book overround", "Value": f"{self.overround:.4f}"},
            {"Measure": "Fair odds on the model",
             "Value": f"{self.fair_odds:.3f}"},
            {"Measure": "Probability edge over the market",
             "Value": f"{self.probability_edge_percent:+.2f}%"},
            {"Measure": "Margin hurdle to clear",
             "Value": f"{self.margin_hurdle_percent:+.2f}%"},
            {"Measure": "Expected value per unit staked",
             "Value": f"{self.expected_value_percent:+.2f}%"},
            {"Measure": "Submodel disagreement",
             "Value": f"{self.disagreement:.4f}"},
        ]


def calculate_ensemble_edge(submodel_probs=(0.58, 0.54, 0.61),
                            bookmaker_odds: float = 1.91,
                            opposing_odds: float = 1.91,
                            weights=None) -> EnsembleEdge:
    """Combine submodels, strip the margin, and report the edge honestly.

    Two numbers are returned on purpose. The raw edge is what a model looks
    like when the margin is left in, and the de vigged edge is what it is. The
    difference between them is the phantom edge, and showing it is the only way
    to make the mistake visible rather than merely avoided.
    """
    probs = tuple(float(p) for p in submodel_probs)
    if not probs:
        raise ValueError("an ensemble needs at least one submodel")
    for prob in probs:
        if not 0 < prob < 1:
            raise ValueError(f"a submodel probability of {prob} is not "
                             f"between 0 and 1 exclusive")

    if weights is None:
        weights = tuple(1.0 / len(probs) for _ in probs)
    weights = tuple(float(w) for w in weights)
    if len(weights) != len(probs):
        raise ValueError("one weight per submodel is required")
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("submodel weights must sum to more than zero")
    weights = tuple(w / total_weight for w in weights)

    model_p = sum(p * w for p, w in zip(probs, weights))
    break_even = implied_probability(bookmaker_odds)
    book = overround((bookmaker_odds, opposing_odds))
    true_market = devig((bookmaker_odds, opposing_odds))[0]

    # How much the model disagrees with what the market actually thinks.
    # This is a disagreement, not a profit, and it is labelled as one.
    probability_edge = ((model_p - true_market) / true_market) * 100
    # What a unit stake is actually worth. This is the number that decides.
    expected_value = (model_p * bookmaker_odds - 1.0) * 100
    # How far above the true market a model has to be merely to break even.
    hurdle = ((break_even - true_market) / true_market) * 100

    spread = max(probs) - min(probs)
    return EnsembleEdge(
        probs, weights, round(model_p, 6), round(break_even, 6),
        round(true_market, 6), round(book, 6), bookmaker_odds, opposing_odds,
        round(probability_edge, 4), round(expected_value, 4),
        round(hurdle, 4), round(fair_odds(model_p), 4), round(spread, 6))


# ---------------------------------------------------------------------------
# Part three: Kelly and the backtest
# ---------------------------------------------------------------------------

# Almost nobody should bet full Kelly. The formula assumes the probability is
# known, and a model's probability is estimated, so the true optimum is always
# smaller than the one the formula returns. A quarter is the common compromise.
DEFAULT_KELLY_FRACTION = 0.25
BACKTEST_BETS = 100


def kelly_fraction(win_probability: float, decimal_odds: float) -> float:
    """Full Kelly. f = (bp - q) / b, with b the net odds.

    Returns zero rather than a negative number when there is no edge, because
    a negative Kelly means bet the other side, and silently returning it is
    how a backtest ends up staking against its own model.
    """
    if not 0 <= win_probability <= 1:
        raise ValueError(f"a win probability of {win_probability} is not "
                         f"between 0 and 1")
    net = decimal_odds - 1.0
    if net <= 0:
        raise ValueError("decimal odds must exceed 1.0 to have net odds")
    lose = 1.0 - win_probability
    raw = (net * win_probability - lose) / net
    return max(raw, 0.0)


@dataclass(frozen=True)
class KellyBacktest:
    edge_percent: float
    win_rate: float
    bankroll: float
    decimal_odds: float
    kelly_fraction_used: float
    full_kelly: float
    staked_fraction: float
    stake: float
    bets: int
    wins: int
    final_bankroll: float
    roi_percent: float
    max_drawdown_percent: float
    worst_case_drawdown_percent: float
    ruin_risk: str

    @property
    def profitable(self) -> bool:
        return self.final_bankroll > self.bankroll

    @property
    def tone(self) -> str:
        if not self.profitable:
            return "crit"
        return "warn" if self.max_drawdown_percent > 25 else "ok"

    def rows(self) -> list:
        return [
            {"Measure": "Full Kelly", "Value": f"{self.full_kelly:.4f}"},
            {"Measure": "Fraction applied",
             "Value": f"{self.kelly_fraction_used:.2f}"},
            {"Measure": "Staked per bet",
             "Value": f"{self.staked_fraction:.4f} "
                      f"({self.stake:,.2f} of {self.bankroll:,.0f})"},
            {"Measure": f"Bankroll after {self.bets} bets",
             "Value": f"{self.final_bankroll:,.2f}"},
            {"Measure": "ROI", "Value": f"{self.roi_percent:+.2f}%"},
            {"Measure": "Max drawdown observed",
             "Value": f"{self.max_drawdown_percent:.2f}%"},
            {"Measure": "Worst case ordering drawdown",
             "Value": f"{self.worst_case_drawdown_percent:.2f}%"},
            {"Measure": "Risk of ruin", "Value": self.ruin_risk},
        ]


def _deterministic_sequence(bets: int, wins: int) -> list:
    """Spread the wins evenly through the run rather than drawing them.

    A random sequence would make a backtest unreproducible, which is the first
    thing wrong with most of them. Even spacing is the neutral ordering; the
    worst case ordering is computed separately and reported beside it, because
    the bettor has to survive the bad ordering, not the average one.
    """
    if bets <= 0:
        return []
    wins = max(0, min(wins, bets))
    sequence = []
    carried = 0.0
    rate = wins / bets
    for _ in range(bets):
        carried += rate
        if carried >= 1.0:
            sequence.append(True)
            carried -= 1.0
        else:
            sequence.append(False)
    return sequence


def simulate_kelly_backtest(edge_percent: float = 3.0,
                            win_rate: float = 0.55,
                            bankroll: float = 10_000.0,
                            decimal_odds: float = 1.91,
                            kelly_fraction_used: float = DEFAULT_KELLY_FRACTION,
                            bets: int = BACKTEST_BETS) -> KellyBacktest:
    """Stake fractional Kelly across a fixed run and report what it costs.

    Two drawdowns are reported. The observed one comes from the even ordering,
    and the worst case one assumes every loss arrives first, which is the
    sequence a bettor actually has to survive without abandoning the strategy.
    Reporting only the first is how a backtest persuades somebody to stake more
    than they can sit through.
    """
    if not 0 <= win_rate <= 1:
        raise ValueError(f"a win rate of {win_rate} is not between 0 and 1")
    if bankroll <= 0:
        raise ValueError("a backtest needs a positive bankroll")
    if not 0 < kelly_fraction_used <= 1:
        raise ValueError("the Kelly fraction must be above 0 and at most 1")

    full = kelly_fraction(win_rate, decimal_odds)
    staked = full * kelly_fraction_used
    net = decimal_odds - 1.0

    wins = int(round(win_rate * bets))
    sequence = _deterministic_sequence(bets, wins)

    balance = bankroll
    peak = bankroll
    trough_ratio = 1.0
    for won in sequence:
        stake = balance * staked
        balance = balance + stake * net if won else balance - stake
        peak = max(peak, balance)
        if peak > 0:
            trough_ratio = min(trough_ratio, balance / peak)

    # Worst case: every loss first, compounding downwards.
    losses = bets - wins
    worst = bankroll * ((1.0 - staked) ** losses)
    worst_drawdown = (1.0 - worst / bankroll) * 100 if bankroll else 0.0

    roi = ((balance - bankroll) / bankroll) * 100 if bankroll else 0.0
    observed_drawdown = (1.0 - trough_ratio) * 100

    if staked == 0:
        ruin = "No stake, because there is no edge at these odds"
    elif worst_drawdown > 60:
        ruin = "Severe. The worst ordering removes most of the bankroll"
    elif worst_drawdown > 30:
        ruin = "Material. Survivable only if the staking rule is kept to"
    else:
        ruin = "Contained at this fraction"

    return KellyBacktest(
        edge_percent=round(float(edge_percent), 4), win_rate=win_rate,
        bankroll=bankroll, decimal_odds=decimal_odds,
        kelly_fraction_used=kelly_fraction_used, full_kelly=round(full, 6),
        staked_fraction=round(staked, 6), stake=round(bankroll * staked, 2),
        bets=bets, wins=wins, final_bankroll=round(balance, 2),
        roi_percent=round(roi, 4),
        max_drawdown_percent=round(observed_drawdown, 4),
        worst_case_drawdown_percent=round(worst_drawdown, 4),
        ruin_risk=ruin)


def bankroll_curve(win_rate: float = 0.55, decimal_odds: float = 1.91,
                   bankroll: float = 10_000.0,
                   kelly_fraction_used: float = DEFAULT_KELLY_FRACTION,
                   bets: int = BACKTEST_BETS) -> list:
    """The balance after every bet, for plotting.

    Built from the same deterministic sequence the backtest uses, so the curve
    and the headline figures cannot disagree.
    """
    full = kelly_fraction(win_rate, decimal_odds)
    staked = full * kelly_fraction_used
    net = decimal_odds - 1.0
    wins = int(round(win_rate * bets))
    balance = bankroll
    curve = [round(balance, 2)]
    for won in _deterministic_sequence(bets, wins):
        stake = balance * staked
        balance = balance + stake * net if won else balance - stake
        curve.append(round(balance, 2))
    return curve


def kelly_ladder(win_rate: float = 0.55, decimal_odds: float = 1.91,
                 bankroll: float = 10_000.0) -> list:
    """The same edge at several fractions, so the cost of aggression is visible."""
    rows = []
    for fraction in (1.0, 0.5, 0.25, 0.125):
        run = simulate_kelly_backtest(0.0, win_rate, bankroll, decimal_odds,
                                      fraction)
        rows.append({
            "Kelly fraction": f"{fraction:.3f}",
            "Staked per bet": f"{run.staked_fraction:.4f}",
            "ROI over 100": f"{run.roi_percent:+.2f}%",
            "Observed drawdown": f"{run.max_drawdown_percent:.1f}%",
            "Worst case drawdown": f"{run.worst_case_drawdown_percent:.1f}%",
        })
    return rows


def console_summary() -> dict:
    edge = calculate_ensemble_edge()
    run = simulate_kelly_backtest()
    return {
        "overround": edge.overround,
        "margin_hurdle": edge.margin_hurdle_percent,
        "probability_edge": edge.probability_edge_percent,
        "expected_value": edge.expected_value_percent,
        "full_kelly": run.full_kelly,
        "staked": run.staked_fraction,
        "roi": run.roi_percent,
        "referees": len(REFEREE_SAMPLES),
        "corrected_alpha": round(ALPHA / REFEREE_POOL, 6),
    }
