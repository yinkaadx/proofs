"""Askew, a bespoke menswear commission engine, as a standalone app.

Run it on its own with `streamlit run askew_suit_engine.py`, or open the same
tool inside the Toolbench hub at /askew-suit-engine.

This file holds the standalone presentation only. The pricing, the deposit
split and the measurement validation live in `tools/askew_suit_engine/core.py`
and are imported here, so the standalone app and the hub page cost a commission
with the same arithmetic rather than with two copies of it that drift. Every
name this module used to define is still available from it, because the star
import below is backed by an explicit `__all__` in the core.
"""

from __future__ import annotations

from tools.askew_suit_engine.core import *  # noqa: F401,F403
from tools.askew_suit_engine.core import (
    APP_NAME,
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
    money,
    price,
    profile_rows,
    save_profile,
    validate_commission,
)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

STYLE = """
<style>
  .stApp { background: #f7f4ef; }
  .ask-wrap { color: #23201c; }
  .ask-hero {
    background: #17150f; color: #f3ece0; padding: 42px 40px 38px;
    border-radius: 2px; margin-bottom: 28px;
    border-bottom: 3px solid #b08d4f;
  }
  .ask-hero h1 {
    font-family: Georgia, 'Times New Roman', serif; font-weight: 400;
    font-size: 40px; letter-spacing: .16em; margin: 0 0 10px;
    text-transform: uppercase; color: #f3ece0;
  }
  .ask-hero p { margin: 0; color: #c9bda6; font-size: 15px;
                letter-spacing: .04em; max-width: 62ch; line-height: 1.6; }
  .ask-rule { height: 1px; background: #ddd3c2; margin: 26px 0 18px; }
  .ask-eyebrow {
    font-size: 11px; letter-spacing: .22em; text-transform: uppercase;
    color: #8a7a5e; margin-bottom: 6px;
  }
  .ask-h2 {
    font-family: Georgia, 'Times New Roman', serif; font-size: 25px;
    letter-spacing: .04em; margin: 0 0 14px; color: #23201c; font-weight: 400;
  }
  .ask-card {
    background: #fffdf9; border: 1px solid #e4dccd; border-radius: 2px;
    padding: 20px 22px; margin-bottom: 14px; color: #23201c;
  }
  .ask-card h4 {
    font-family: Georgia, 'Times New Roman', serif; font-weight: 400;
    margin: 0 0 6px; font-size: 18px; letter-spacing: .02em;
  }
  .ask-card p { margin: 0; color: #57503f; font-size: 14px; line-height: 1.6; }
  .ask-total {
    background: #17150f; color: #f3ece0; border-radius: 2px;
    padding: 24px 26px; margin-top: 6px;
  }
  .ask-line {
    display: flex; justify-content: space-between; padding: 7px 0;
    border-bottom: 1px solid #2e2a20; font-size: 14px; color: #cdc2ab;
  }
  .ask-line.grand {
    border-bottom: none; border-top: 2px solid #b08d4f; margin-top: 8px;
    padding-top: 14px; font-size: 19px; color: #f3ece0;
    font-family: Georgia, 'Times New Roman', serif;
  }
  .ask-deposit {
    background: #b08d4f; color: #17150f; padding: 16px 20px;
    border-radius: 2px; margin-top: 16px;
  }
  .ask-deposit .n {
    font-family: Georgia, 'Times New Roman', serif; font-size: 28px;
    letter-spacing: .02em;
  }
  .ask-deposit .l { font-size: 12px; letter-spacing: .14em;
                    text-transform: uppercase; opacity: .8; }
  .ask-swatch {
    display: inline-block; width: 54px; height: 54px; border-radius: 2px;
    border: 1px solid #d8cdb8; vertical-align: middle; margin-right: 12px;
  }
  .ask-mono {
    display: inline-block; font-family: Georgia, 'Times New Roman', serif;
    font-size: 26px; letter-spacing: .22em; color: #b08d4f;
    border: 1px solid #e4dccd; padding: 10px 18px; background: #fffdf9;
  }
  .ask-foot { color: #8a7a5e; font-size: 12px; line-height: 1.7;
              margin-top: 34px; letter-spacing: .02em; }
</style>
"""


def main() -> None:  # pragma: no cover, drawn by Streamlit rather than tested
    import streamlit as st

    st.set_page_config(page_title=f"{APP_NAME} bespoke", page_icon="🧵",
                       layout="wide", initial_sidebar_state="collapsed")
    st.markdown(STYLE, unsafe_allow_html=True)
    st.markdown(
        f"""
<div class="ask-hero">
  <h1>{APP_NAME}</h1>
  <p>{APP_TAGLINE} Choose the cloth and the finishing, read the commission back
  with the deposit stated plainly, and leave your measurements on file.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    left, right = st.columns([3, 2], gap="large")

    # -- Section one: the configurator ---------------------------------
    with left:
        st.markdown('<div class="ask-eyebrow">Section one</div>'
                    '<div class="ask-h2">Build your suit</div>',
                    unsafe_allow_html=True)

        base_key = st.radio(
            "Base commission", [suit.key for suit in BASE_SUITS],
            format_func=lambda key: (f"{base_by_key(key).name} "
                                     f"{money(base_by_key(key).price_cents)}"),
            key="ask_base")
        base = base_by_key(base_key)
        st.markdown(
            f'<div class="ask-card"><h4>{base.name}</h4>'
            f'<p>{base.cloth}. {base.note}</p></div>',
            unsafe_allow_html=True)

        st.markdown('<div class="ask-rule"></div>', unsafe_allow_html=True)
        st.markdown('<div class="ask-eyebrow">Finishing</div>',
                    unsafe_allow_html=True)

        chosen: list[str] = []
        for upgrade in UPGRADES:
            if st.checkbox(f"{upgrade.name}  {money(upgrade.price_cents)}",
                           key=f"ask_up_{upgrade.key}"):
                chosen.append(upgrade.key)
            st.caption(upgrade.note)

        lining_colour = LINING_COLOURS[0]
        monogram = ""
        if LINING_KEY in chosen:
            lining_colour = st.selectbox("Lining colour", LINING_COLOURS,
                                         key="ask_lining")
            st.markdown(
                f'<span class="ask-swatch" style="background:'
                f'{LINING_SWATCH[lining_colour]}"></span>'
                f'<span style="color:#57503f;font-size:14px">'
                f'{lining_colour} cupro, cut and set by hand</span>',
                unsafe_allow_html=True)
        if MONOGRAM_KEY in chosen:
            monogram = st.text_input("Initials", value="AAO", max_chars=6,
                                     key="ask_monogram").strip().upper()
            if monogram:
                st.markdown(f'<span class="ask-mono">{monogram}</span>',
                            unsafe_allow_html=True)

        commission = Commission(base_key, tuple(chosen), monogram,
                                lining_colour)
        ok, code, problem = validate_commission(commission)

    # -- Section two: the order summary --------------------------------
    with right:
        st.markdown('<div class="ask-eyebrow">Section two</div>'
                    '<div class="ask-h2">Your commission</div>',
                    unsafe_allow_html=True)

        if not ok:
            st.markdown(
                f'<div class="ask-card"><h4>{code}</h4><p>{problem}</p></div>',
                unsafe_allow_html=True)
            st.warning("The commission is not ready to quote yet.")
        else:
            quote = price(commission)
            lines = "".join(
                f'<div class="ask-line"><span>{item.label}</span>'
                f'<span>{money(item.amount_cents)}</span></div>'
                for item in quote.items)
            st.markdown(
                f"""
<div class="ask-total">
  {lines}
  <div class="ask-line grand"><span>Total</span>
  <span>{money(quote.total_cents)}</span></div>
  <div class="ask-deposit">
    <div class="l">Deposit due today, {DEPOSIT_PERCENT:g} percent</div>
    <div class="n">{money(quote.deposit_cents)}</div>
    <div class="l">Balance {money(quote.balance_cents)} on final fitting</div>
  </div>
</div>
""",
                unsafe_allow_html=True)

            st.markdown('<div class="ask-rule"></div>', unsafe_allow_html=True)
            st.dataframe(quote.rows(), width="stretch", hide_index=True)
            st.caption(
                f"The deposit is exactly {DEPOSIT_PERCENT:g} percent of "
                f"{money(quote.total_cents)}, rounded to the cent, and the "
                f"balance is the remainder. The two add back to the total."
            )

    # -- Section three: the measurement profile ------------------------
    st.markdown('<div class="ask-rule"></div>', unsafe_allow_html=True)
    st.markdown('<div class="ask-eyebrow">Section three</div>'
                '<div class="ask-h2">Measurement profile</div>',
                unsafe_allow_html=True)
    st.caption(
        "Taken at the first fitting and kept on file, so a second commission "
        "starts from cloth rather than from a tape measure."
    )

    with st.form("ask_profile"):
        client_name = st.text_input("Client name", value="Amara Okafor",
                                    key="ask_name")
        columns = st.columns(3)
        values: dict[str, float] = {}
        for index, measurement in enumerate(MEASUREMENTS):
            values[measurement.key] = columns[index % 3].number_input(
                f"{measurement.label} ({measurement.unit})",
                min_value=0.0, max_value=120.0, value=measurement.default,
                step=0.5, key=f"ask_m_{measurement.key}")
        submitted = st.form_submit_button("Save to the client record")

    if submitted:
        result = save_profile(client_name, values)
        if result.saved:
            st.success(result.message)
            st.dataframe(profile_rows(result.values), width="stretch",
                         hide_index=True)
        else:
            st.error(f"{result.code}: {result.message}")

    st.markdown(
        f"""
<div class="ask-foot">
{APP_NAME} commission engine, version {ENGINE_VERSION}. A demonstration: no
order is placed, no payment is taken and no record is written. Every amount is
held as an integer number of cents, because half of an odd total is where a
float quietly loses one.
</div>
""",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
