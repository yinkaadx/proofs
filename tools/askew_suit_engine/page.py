"""ASKEW Bespoke Pricing Engine.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency and is shared with the standalone `askew_suit_engine.py` at the
repository root, so the two cost a commission with the same arithmetic rather
than with two copies of it.

The commission is derived live from the controls. Only the saved profile is
held in session state, because that is the one thing on the page that is meant
to persist after the interaction that created it.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.askew_suit_engine.core import (
    APP_TAGLINE,
    BASE_SUITS,
    DEPOSIT_PERCENT,
    ENGINE_VERSION,
    LINING_COLOURS,
    LINING_KEY,
    LINING_SWATCH,
    MAX_MONOGRAM,
    MEASUREMENTS,
    MONOGRAM_KEY,
    UPGRADES,
    Commission,
    base_by_key,
    money,
    price,
    profile_rows,
    save_profile,
    validate_commission,
)

STATE = "askew_state"


def _state() -> dict:
    if STATE not in st.session_state:
        st.session_state[STATE] = {"profile": None}
    return st.session_state[STATE]


def _save_profile() -> None:
    """Save the measurements currently on screen.

    Read from live widget state rather than from arguments bound at the
    previous render, which are one interaction stale.
    """
    values = {measurement.key: st.session_state.get(
        f"askew_m_{measurement.key}", measurement.default)
        for measurement in MEASUREMENTS}
    _state()["profile"] = save_profile(
        str(st.session_state.get("askew_name", "")), values)


def render() -> None:
    inject()
    state = _state()

    st.markdown(
        f"""
<div class="app-hero">
  <h1>ASKEW Bespoke Pricing Engine</h1>
  <p>{esc(APP_TAGLINE)} A commission is chosen, costed and confirmed in one
  pass: the cloth, the upgrades that change the price, the deposit stated
  plainly rather than implied, and a measurement profile that is checked before
  anything is cut. Every amount is an integer number of cents, because half of
  an odd total is where a float quietly loses one.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Build**: choose a cloth and turn upgrades on.\n"
            "2. Add a monogram and watch it refused without initials.\n"
            "3. **Summary**: read the total and the deposit split.\n"
            "4. **Profile**: enter measurements and save, then try a waist "
            "larger than the chest."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. No order is placed, no "
            "payment is taken and no record is written."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_build, tab_summary, tab_profile = st.tabs(
        ["Build Your Suit", "Order Summary", "Measurement Profile"]
    )

    # -----------------------------------------------------------------
    # Configurator
    # -----------------------------------------------------------------
    with tab_build:
        st.markdown("#### The cloth")
        base_key = st.radio(
            "Base suit", [suit.key for suit in BASE_SUITS],
            format_func=lambda key: (f"{base_by_key(key).name}, "
                                     f"{money(base_by_key(key).price_cents)}"),
            key="askew_base")

        columns = st.columns(len(BASE_SUITS))
        for index, suit in enumerate(BASE_SUITS):
            with columns[index]:
                chosen = suit.key == base_key
                st.markdown(
                    f"""
<div class="app-tool">
  <h3>{esc(suit.name)}</h3>
  <p>{esc(suit.note)}</p>
  <span class="app-pill">{esc(suit.cloth)}</span>
  <div class="app-ev">{esc(money(suit.price_cents))}
  {' &middot; selected' if chosen else ''}</div>
</div>
""",
                    unsafe_allow_html=True,
                )

        st.markdown("#### The upgrades")
        st.caption(
            "Each one is priced on its own line in the summary. Nothing is "
            "bundled, because a bundled price is the one a client asks to have "
            "broken down."
        )

        selected: list[str] = []
        upgrade_columns = st.columns(2)
        for index, upgrade in enumerate(UPGRADES):
            with upgrade_columns[index % 2]:
                on = st.checkbox(
                    f"{upgrade.name}, {money(upgrade.price_cents)}",
                    value=upgrade.key == LINING_KEY,
                    key=f"askew_u_{upgrade.key}")
                st.caption(upgrade.note)
                if on:
                    selected.append(upgrade.key)

        lining_colour = LINING_COLOURS[0]
        monogram = ""
        if LINING_KEY in selected:
            lining_colour = st.selectbox("Lining colour", LINING_COLOURS,
                                         key="askew_lining")
            swatch = LINING_SWATCH.get(lining_colour, "#000000")
            st.markdown(
                f'<div class="app-ev"><span style="display:inline-block;'
                f'width:28px;height:14px;border-radius:2px;background:'
                f'{esc(swatch)};vertical-align:middle;margin-right:8px;'
                f'border:1px solid rgba(0,0,0,.25)"></span>'
                f'{esc(lining_colour)} cupro</div>',
                unsafe_allow_html=True)
        if MONOGRAM_KEY in selected:
            monogram = st.text_input(
                f"Monogram, up to {MAX_MONOGRAM} letters", value="AJK",
                key="askew_monogram")

        commission = Commission(base_key, tuple(selected), monogram,
                                lining_colour)
        ok, code, message = validate_commission(commission)

        if not ok:
            st.markdown(
                f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">{esc(code)}</span>Not ready to order</h4>
  <p>{esc(message)}</p>
</div>
""",
                unsafe_allow_html=True,
            )
        else:
            quote = price(commission)
            st.markdown(
                f"""
<div class="app-card ok">
  <h4><span class="app-tag ok">Ready</span>{esc(base_by_key(base_key).name)}
  with {len(selected)} upgrade(s)</h4>
  <div class="app-ev">{esc(money(quote.total_cents))} total &nbsp;
  {esc(money(quote.deposit_cents))} due today</div>
  <p>Change anything above and both numbers move with it.</p>
</div>
""",
                unsafe_allow_html=True,
            )

    # -----------------------------------------------------------------
    # Order summary
    # -----------------------------------------------------------------
    with tab_summary:
        if not ok:
            st.markdown(
                f"""
<div class="app-card crit">
  <h4><span class="app-tag crit">{esc(code)}</span>No summary yet</h4>
  <p>{esc(message)}</p>
</div>
""",
                unsafe_allow_html=True,
            )
        else:
            quote = price(commission)
            st.markdown(
                f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{esc(money(quote.total_cents))}</div>
    <div class="l">Commission total</div></div>
  <div class="app-kpi ok"><div class="n">{esc(money(quote.deposit_cents))}</div>
    <div class="l">Deposit due today</div></div>
  <div class="app-kpi"><div class="n">{esc(money(quote.balance_cents))}</div>
    <div class="l">Balance at fitting</div></div>
  <div class="app-kpi"><div class="n">{DEPOSIT_PERCENT:g}%</div>
    <div class="l">Split</div></div>
</div>
""",
                unsafe_allow_html=True,
            )

            st.markdown("##### Line by line")
            st.dataframe(quote.rows(), width="stretch", hide_index=True)

            st.markdown(
                f"""
<div class="app-card info">
  <h4><span class="app-tag info">The split</span>
  {esc(money(quote.deposit_cents))} now, {esc(money(quote.balance_cents))}
  later</h4>
  <p>The deposit is rounded to the cent rather than left as a fraction, and the
  two halves add back to {esc(money(quote.total_cents))} exactly. Half of an odd
  number of cents is half a cent, and a deposit that disagrees with the invoice
  by one is the detail a bespoke client notices.</p>
</div>
""",
                unsafe_allow_html=True,
            )

            st.markdown("##### What the order would post")
            st.code(
                "\n".join([
                    f"base        {commission.base_key}",
                    f"upgrades    {', '.join(commission.upgrade_keys) or 'none'}",
                    f"monogram    {commission.monogram or 'none'}",
                    f"lining      {commission.lining_colour}",
                    f"total       {quote.total_cents} cents",
                    f"deposit     {quote.deposit_cents} cents",
                    f"balance     {quote.balance_cents} cents",
                ]), language="text")

    # -----------------------------------------------------------------
    # Measurement profile
    # -----------------------------------------------------------------
    with tab_profile:
        st.markdown("#### The measurement profile")
        st.caption(
            "Checked before anything is cut. A pattern cut from an incomplete "
            "profile is cut twice, and a tape read against the wrong edge "
            "looks exactly like a client who is a different shape."
        )

        st.text_input("Client name", value="Amara Okafor", key="askew_name")

        measurement_columns = st.columns(3)
        for index, measurement in enumerate(MEASUREMENTS):
            with measurement_columns[index % 3]:
                st.number_input(
                    f"{measurement.label} ({measurement.unit})",
                    min_value=0.0, max_value=120.0,
                    value=float(measurement.default), step=0.5,
                    key=f"askew_m_{measurement.key}")
                st.caption(measurement.note)

        st.button("Save to the client record", type="primary",
                  on_click=_save_profile, key="askew_save_btn")

        profile = state["profile"]
        if profile is None:
            st.info("Nothing saved yet. Enter the measurements and save, then "
                    "try a waist larger than the chest.")
        else:
            tone = "ok" if profile.saved else "crit"
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">
  {esc(profile.reference if profile.saved else profile.code)}</span>
  {esc('Profile saved' if profile.saved else 'Not saved')}</h4>
  <p>{esc(profile.message)}</p>
</div>
""",
                unsafe_allow_html=True,
            )
            if profile.saved:
                st.markdown("##### What was recorded")
                st.dataframe(profile_rows(profile.values), width="stretch",
                             hide_index=True)

    st.markdown(
        f"""
<div class="app-foot">
ASKEW Bespoke Pricing Engine, engine version {ENGINE_VERSION}. A simulator: no
order is placed, no payment is taken and no record is written. The same engine
runs the standalone app at askew_suit_engine.py, so a commission costs the same
in both places rather than in two implementations that drift.
</div>
""",
        unsafe_allow_html=True,
    )
