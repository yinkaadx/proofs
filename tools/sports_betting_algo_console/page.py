"""Sports Betting Algorithm & Edge Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine is unit testable on its own.

An educational simulator of the arithmetic. Not betting advice.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.sports_betting_algo_console.core import (
    ALPHA,
    BACKTEST_BETS,
    DISCLAIMER,
    ENGINE_VERSION,
    REFEREE_POOL,
    REFEREE_SAMPLES,
    SPORTS,
    bankroll_curve,
    calculate_ensemble_edge,
    analyze_referee_factor,
    console_summary,
    kelly_ladder,
    referee_rows,
    simulate_kelly_backtest,
)


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>Sports Betting Algorithm &amp; Edge Console</h1>
  <p>Three pieces of arithmetic decide whether a betting model is a business or
  a hobby that costs money. Whether a disagreement with the market clears the
  margin, which most do not. Whether a referee finding survives having searched
  thirty referees to find it. And whether the staking plan produces a drawdown
  the bettor can actually sit through.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    summary = console_summary()

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Edge**: raise the model probability and watch when the "
            "expected value finally turns positive.\n"
            "2. **Referee**: change the pool size and watch findings vanish.\n"
            "3. **Kelly**: compare the observed drawdown with the worst case.\n"
            "4. **Ladder**: what full Kelly costs in drawdown."
        )
        st.divider()
        st.caption(DISCLAIMER)
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_edge, tab_ref, tab_kelly, tab_ladder = st.tabs(
        ["Ensemble Edge", "Referee Factor", "Kelly Backtest", "Stake Ladder"])

    # -----------------------------------------------------------------
    # Edge
    # -----------------------------------------------------------------
    with tab_edge:
        st.markdown("#### A disagreement is not an edge until it clears the margin")
        st.caption(
            "De vigging recovers what the market actually thinks. The trap is "
            "what happens next: the model reads a few percent higher, that "
            "gets called an edge, and it is not one. Break even needs the "
            "model above one over the odds, which sits well above the de "
            "vigged market."
        )

        c1, c2, c3 = st.columns(3)
        p1 = c1.slider("Submodel A", 0.30, 0.80, 0.58, 0.005, key="sb_p1")
        p2 = c2.slider("Submodel B", 0.30, 0.80, 0.54, 0.005, key="sb_p2")
        p3 = c3.slider("Submodel C", 0.30, 0.80, 0.61, 0.005, key="sb_p3")

        c4, c5 = st.columns(2)
        odds = c4.number_input("Decimal odds, your side", 1.01, 20.0, 1.91,
                               0.01, key="sb_odds")
        other = c5.number_input("Decimal odds, other side", 1.01, 20.0, 1.91,
                                0.01, key="sb_other")

        edge = calculate_ensemble_edge((p1, p2, p3), odds, other)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">
    {edge.probability_edge_percent:+.2f}%</div>
    <div class="l">Probability edge, a disagreement</div></div>
  <div class="app-kpi warn">
    <div class="n">{edge.margin_hurdle_percent:+.2f}%</div>
    <div class="l">Margin hurdle to clear</div></div>
  <div class="app-kpi {edge.tone}">
    <div class="n">{edge.expected_value_percent:+.2f}%</div>
    <div class="l">Expected value, the money</div></div>
  <div class="app-kpi"><div class="n">{edge.overround:.4f}</div>
    <div class="l">Book overround</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        if edge.margin_illusion:
            st.markdown(
                f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">Margin illusion</span>
  Positive edge, negative money</h4>
  <p>The model is {edge.probability_edge_percent:,.2f} percent above the de
  vigged market and still loses
  {abs(edge.expected_value_percent):,.2f} percent on every unit staked,
  because break even needs a probability of
  {edge.break_even_probability:.4f} and the model gives
  {edge.model_probability:.4f}. This is the single most common way a betting
  dashboard shows green while the bankroll falls.</p>
  <div class="app-ev">Clear {edge.margin_hurdle_percent:,.2f} percent over the
  market before any of the disagreement is worth money.</div>
</div>
""",
                unsafe_allow_html=True,
            )
        elif edge.profitable:
            st.success(
                f"Expected value is {edge.expected_value_percent:+.2f} percent "
                f"a unit, having cleared the {edge.margin_hurdle_percent:.2f} "
                f"percent hurdle. Submodel spread is {edge.disagreement:.4f}.")
        else:
            st.error(
                f"Expected value is {edge.expected_value_percent:+.2f} percent "
                f"a unit. The model is below the market, so there is nothing "
                f"here to stake.")

        st.markdown("##### The numbers")
        st.dataframe(edge.rows(), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Referee
    # -----------------------------------------------------------------
    with tab_ref:
        st.markdown("#### A finding has to survive the search that found it")
        st.caption(
            f"Sweeping {REFEREE_POOL} referees at a {ALPHA:.0%} threshold "
            f"produces about {REFEREE_POOL * ALPHA:.1f} findings a season by "
            f"chance alone. A bare p value under 0.05 is therefore exactly "
            f"what noise looks like, which is why the threshold is divided by "
            f"the number of tests."
        )

        c1, c2 = st.columns(2)
        sport = c1.selectbox("Sport", list(SPORTS), key="sb_sport")
        pool = c2.slider("Referees searched", 1, 60, REFEREE_POOL,
                         key="sb_pool")
        names = [s.name for s in REFEREE_SAMPLES if s.sport == sport]
        referee = st.selectbox("Referee", names, key="sb_ref")

        factor = analyze_referee_factor(referee, sport, pool)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">
    {factor.foul_deviation:+.2f}</div>
    <div class="l">Fouls a match against league</div></div>
  <div class="app-kpi"><div class="n">{factor.over_bias:+.1%}</div>
    <div class="l">Over rate bias</div></div>
  <div class="app-kpi"><div class="n">{factor.p_value:.4f}</div>
    <div class="l">p value</div></div>
  <div class="app-kpi {factor.tone}">
    <div class="n">{factor.bonferroni_alpha:.5f}</div>
    <div class="l">Corrected threshold</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown(
            f"""
<div class="app-card {factor.tone}">
  <h4><span class="app-tag {factor.tone}">{esc(factor.significance)}</span>
  {esc(factor.name)}, {factor.matches} matches</h4>
  <p>{esc(factor.note)}</p>
  <div class="app-ev">z {factor.z_score:,.3f}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### Every referee in this sport")
        st.dataframe(referee_rows(sport, pool), width="stretch",
                     hide_index=True)

    # -----------------------------------------------------------------
    # Kelly
    # -----------------------------------------------------------------
    with tab_kelly:
        st.markdown("#### Two drawdowns, and the second is the one to survive")
        st.caption(
            "The observed drawdown comes from wins arriving evenly. The worst "
            "case assumes every loss arrives first, which is the sequence a "
            "bettor actually has to sit through without abandoning the plan. "
            "Reporting only the first is how a backtest persuades somebody to "
            "stake more than they can stand."
        )

        c1, c2, c3 = st.columns(3)
        win_rate = c1.slider("Win rate", 0.40, 0.70, 0.55, 0.005,
                             key="sb_wr")
        fraction = c2.select_slider("Kelly fraction",
                                    options=[0.125, 0.25, 0.5, 1.0],
                                    value=0.25, key="sb_frac")
        bank = c3.number_input("Bankroll", 100.0, 1_000_000.0, 10_000.0,
                               500.0, key="sb_bank")

        run = simulate_kelly_backtest(edge.expected_value_percent, win_rate,
                                      bank, odds, fraction)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{run.staked_fraction:.4f}</div>
    <div class="l">Staked per bet, {run.stake:,.0f}</div></div>
  <div class="app-kpi {run.tone}">
    <div class="n">{run.roi_percent:+.2f}%</div>
    <div class="l">ROI over {BACKTEST_BETS} bets</div></div>
  <div class="app-kpi"><div class="n">
    {run.max_drawdown_percent:.1f}%</div>
    <div class="l">Observed drawdown</div></div>
  <div class="app-kpi crit">
    <div class="n">{run.worst_case_drawdown_percent:.1f}%</div>
    <div class="l">Worst case drawdown</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### The bankroll, bet by bet")
        st.line_chart(bankroll_curve(win_rate, odds, bank, fraction))

        st.markdown(
            f"""
<div class="app-card {run.tone}">
  <h4><span class="app-tag {run.tone}">{esc(run.ruin_risk)}</span>
  Full Kelly would stake {run.full_kelly:.4f}</h4>
  <p>Almost nobody should bet full Kelly. The formula assumes the probability
  is known, and a model's probability is estimated, so the true optimum is
  always smaller than the number the formula returns.</p>
  <div class="app-ev">{run.wins} wins in {run.bets} bets at
  {run.win_rate:.1%}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.dataframe(run.rows(), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Ladder
    # -----------------------------------------------------------------
    with tab_ladder:
        st.markdown("#### What aggression costs")
        st.caption(
            "The same edge staked at four fractions. Read the worst case "
            "drawdown column rather than the ROI column, because the drawdown "
            "is what decides whether the plan is still being followed when "
            "the ROI arrives."
        )
        st.dataframe(kelly_ladder(win_rate, odds, bank), width="stretch",
                     hide_index=True)

        st.markdown(
            f"""
<div class="app-card warn">
  <h4><span class="app-tag warn">Estimation error</span>
  Why fractional Kelly is the default here</h4>
  <p>Kelly is optimal when the probability is known exactly. It never is. A
  model that thinks it wins {summary['probability_edge']:,.1f} percent more
  often than the market may be right, may be overfitted, and cannot tell you
  which. Staking a fraction is how that uncertainty is priced.</p>
</div>
""",
            unsafe_allow_html=True,
        )
        st.info(DISCLAIMER)

    st.markdown(
        f"""
<div class="app-foot">
Sports Betting Algorithm &amp; Edge Console, engine version {ENGINE_VERSION}.
A simulator: no market is read, no odds are fetched, no bet is placed and no
result is predicted. {esc(DISCLAIMER)}
</div>
""",
        unsafe_allow_html=True,
    )
