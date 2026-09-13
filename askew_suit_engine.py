"""Askew, a bespoke menswear commission engine.

A single file Streamlit application in three sections: a suit configurator, a
live order summary with the deposit stated plainly, and a measurement profile
that simulates a save.

The money and the state logic sit above `main()` as plain functions and frozen
dataclasses, with no Streamlit call between them. That is what makes them
testable: `pytest` imports this module and exercises the pricing and the
validation without a browser, while `streamlit run askew_suit_engine.py`
executes the script as __main__ and draws the page.

Every amount is an integer number of cents. A half of an odd total is where a
float quietly loses a cent, and a deposit that disagrees with the invoice by a
cent is the kind of detail a bespoke client notices.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

APP_NAME = "Askew"
APP_TAGLINE = "Bespoke tailoring, commissioned in three steps."
ENGINE_VERSION = "1.0.0"

DEPOSIT_PERCENT = Decimal("50")


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BaseSuit:
    key: str
    name: str
    cloth: str
    price_cents: int
    note: str


BASE_SUITS: tuple[BaseSuit, ...] = (
    BaseSuit("two_piece", "The Askew Two Piece", "Super 120s wool, Huddersfield",
             100_000,
             "Half canvassed, two button, a natural shoulder cut close to the "
             "body."),
    BaseSuit("three_piece", "The Askew Three Piece",
             "Super 120s wool, Huddersfield", 100_000,
             "The two piece with a matching six button waistcoat, cut for "
             "evening as readily as for the office."),
    BaseSuit("travel", "The Travel Suit", "High twist wool, Biella", 100_000,
             "A high twist cloth that leaves a suitcase without a crease in "
             "it."),
)

MONOGRAM_KEY = "monogram"
LINING_KEY = "premium_lining"


@dataclass(frozen=True)
class Upgrade:
    key: str
    name: str
    price_cents: int
    note: str


UPGRADES: tuple[Upgrade, ...] = (
    Upgrade(LINING_KEY, "Premium lining", 5_000,
            "Bemberg cupro in one of four house colours, cut and set by hand."),
    Upgrade(MONOGRAM_KEY, "Monogram", 2_500,
            "Three initials in silk thread, inside the left facing."),
    Upgrade("surgeon_cuffs", "Working cuffs", 7_500,
            "Four working buttonholes on each sleeve, finished by hand."),
    Upgrade("second_trouser", "Second trouser", 27_500,
            "A second pair in the same cloth, cut at the same fitting."),
)

LINING_COLOURS: tuple[str, ...] = ("Oxblood", "Forest", "Midnight", "Ivory")
LINING_SWATCH: dict[str, str] = {
    "Oxblood": "#6b1f28",
    "Forest": "#1f3d2b",
    "Midnight": "#1b2340",
    "Ivory": "#efe7d8",
}


def base_by_key(key: str, catalogue=BASE_SUITS) -> BaseSuit | None:
    for suit in catalogue:
        if suit.key == key:
            return suit
    return None


def upgrade_by_key(key: str, catalogue=UPGRADES) -> Upgrade | None:
    for upgrade in catalogue:
        if upgrade.key == key:
            return upgrade
    return None


# ---------------------------------------------------------------------------
# The commission
# ---------------------------------------------------------------------------

MAX_MONOGRAM = 3

ERR_UNKNOWN_BASE = "UNKNOWN_BASE"
ERR_UNKNOWN_UPGRADE = "UNKNOWN_UPGRADE"
ERR_MONOGRAM_EMPTY = "MONOGRAM_EMPTY"
ERR_MONOGRAM_SHAPE = "MONOGRAM_NOT_LETTERS"
ERR_MONOGRAM_LENGTH = "MONOGRAM_TOO_LONG"
ERR_LINING_UNKNOWN = "LINING_UNKNOWN"


@dataclass(frozen=True)
class LineItem:
    label: str
    detail: str
    amount_cents: int


@dataclass(frozen=True)
class Commission:
    base_key: str
    upgrade_keys: tuple[str, ...]
    monogram: str = ""
    lining_colour: str = LINING_COLOURS[0]

    def with_upgrade(self, key: str, selected: bool) -> Commission:
        """Return the commission with one upgrade turned on or off.

        Frozen on purpose. Mutating a shared basket in place is how a total on
        screen comes to describe a selection the customer already changed.
        """
        if upgrade_by_key(key) is None:
            raise ValueError(f"{key!r} is not an upgrade in the catalogue.")
        keys = [k for k in self.upgrade_keys if k != key]
        if selected:
            keys.append(key)
        ordered = tuple(u.key for u in UPGRADES if u.key in keys)
        return Commission(self.base_key, ordered, self.monogram,
                          self.lining_colour)

    def has(self, key: str) -> bool:
        return key in self.upgrade_keys


@dataclass(frozen=True)
class Quote:
    commission: Commission
    items: list
    subtotal_cents: int
    total_cents: int
    deposit_cents: int
    balance_cents: int

    def rows(self) -> list[dict]:
        return [
            {"Item": item.label, "Detail": item.detail,
             "Amount": money(item.amount_cents)}
            for item in self.items
        ]


def deposit_for(total_cents: int,
                percent: Decimal = DEPOSIT_PERCENT) -> int:
    """The deposit, rounded to the cent rather than left as a fraction.

    Half of an odd number of cents is half a cent. Rounding half up keeps the
    deposit and the balance adding back to the total exactly, which is the only
    property that matters here.
    """
    if total_cents < 0:
        raise ValueError("A total cannot be negative.")
    if not 0 <= percent <= 100:
        raise ValueError("A deposit percentage runs from 0 to 100.")
    exact = Decimal(total_cents) * Decimal(percent) / 100
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def price(commission: Commission,
          percent: Decimal = DEPOSIT_PERCENT) -> Quote:
    """Cost a commission, line by line."""
    base = base_by_key(commission.base_key)
    if base is None:
        raise ValueError(f"{commission.base_key!r} is not a suit in the "
                         f"catalogue.")

    items = [LineItem(base.name, base.cloth, base.price_cents)]
    for upgrade in UPGRADES:
        if not commission.has(upgrade.key):
            continue
        detail = upgrade.note
        if upgrade.key == LINING_KEY:
            detail = f"{commission.lining_colour} cupro"
        elif upgrade.key == MONOGRAM_KEY and commission.monogram:
            detail = f"{commission.monogram} in silk thread"
        items.append(LineItem(upgrade.name, detail, upgrade.price_cents))

    subtotal = sum(item.amount_cents for item in items)
    total = subtotal
    deposit = deposit_for(total, percent)
    return Quote(commission=commission, items=items, subtotal_cents=subtotal,
                 total_cents=total, deposit_cents=deposit,
                 balance_cents=total - deposit)


def validate_commission(commission: Commission) -> tuple[bool, str, str]:
    """Check a commission before it is quoted, and name what is wrong.

    A monogram upgrade with no initials is the one that reaches the workroom
    and stops there, so it is refused at the point of selection rather than
    discovered at the cutting table.
    """
    if base_by_key(commission.base_key) is None:
        return False, ERR_UNKNOWN_BASE, "Choose a suit to begin."

    for key in commission.upgrade_keys:
        if upgrade_by_key(key) is None:
            return False, ERR_UNKNOWN_UPGRADE, f"{key} is not an upgrade we offer."

    if commission.has(MONOGRAM_KEY):
        initials = commission.monogram.strip()
        if not initials:
            return False, ERR_MONOGRAM_EMPTY, (
                "The monogram needs initials. A monogram with nothing to "
                "embroider stops at the workroom rather than at the order.")
        if not re.fullmatch(r"[A-Za-z]+", initials):
            return False, ERR_MONOGRAM_SHAPE, (
                f"{initials!r} contains something other than letters. The "
                f"thread is cut to letterforms.")
        if len(initials) > MAX_MONOGRAM:
            return False, ERR_MONOGRAM_LENGTH, (
                f"{len(initials)} letters is more than the {MAX_MONOGRAM} the "
                f"facing takes.")

    if commission.has(LINING_KEY) and \
            commission.lining_colour not in LINING_COLOURS:
        return False, ERR_LINING_UNKNOWN, (
            f"{commission.lining_colour} is not one of the house colours.")

    return True, "", ""


# ---------------------------------------------------------------------------
# The measurement profile
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Measurement:
    key: str
    label: str
    unit: str
    minimum: float
    maximum: float
    default: float
    note: str


MEASUREMENTS: tuple[Measurement, ...] = (
    Measurement("chest", "Chest", "in", 30.0, 60.0, 40.0,
                "Around the fullest part, tape level under the arms."),
    Measurement("waist", "Waist", "in", 26.0, 56.0, 34.0,
                "At the natural waist, not at the belt line."),
    Measurement("hip", "Seat", "in", 30.0, 60.0, 39.0,
                "Around the fullest part of the seat."),
    Measurement("sleeve", "Sleeve", "in", 28.0, 42.0, 34.5,
                "Centre back to wrist bone, arm relaxed."),
    Measurement("inseam", "Inseam", "in", 24.0, 40.0, 32.0,
                "Crotch seam to the top of the shoe."),
    Measurement("height", "Height", "in", 54.0, 84.0, 70.0,
                "Without shoes, standing square."),
)


def measurement_by_key(key: str, catalogue=MEASUREMENTS) -> Measurement | None:
    for measurement in catalogue:
        if measurement.key == key:
            return measurement
    return None


ERR_MISSING = "MEASUREMENT_MISSING"
ERR_RANGE = "MEASUREMENT_OUT_OF_RANGE"
ERR_NOT_A_NUMBER = "MEASUREMENT_NOT_A_NUMBER"
ERR_NAME = "NAME_REQUIRED"
ERR_WAIST_OVER_CHEST = "WAIST_EXCEEDS_CHEST"


@dataclass(frozen=True)
class ProfileResult:
    saved: bool
    reference: str
    code: str
    message: str
    values: dict


def save_profile(name: str, values: dict,
                 catalogue=MEASUREMENTS) -> ProfileResult:
    """Validate a measurement set and simulate the save.

    Nothing is written anywhere. The reference is derived from the name and the
    measurements, so the same profile always produces the same reference, which
    is what makes this demonstrable rather than merely convincing.
    """
    clean_name = str(name).strip()
    if not clean_name:
        return ProfileResult(False, "", ERR_NAME,
                             "A profile needs a name on it. Two clients with "
                             "the same chest are not the same client.", {})

    read: dict[str, float] = {}
    for measurement in catalogue:
        if measurement.key not in values:
            return ProfileResult(
                False, "", ERR_MISSING,
                f"{measurement.label} is missing. A pattern cut from an "
                f"incomplete profile is cut twice.", {})
        raw = values[measurement.key]
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return ProfileResult(
                False, "", ERR_NOT_A_NUMBER,
                f"{measurement.label} reads {raw!r}, which is not a "
                f"measurement.", {})
        if not measurement.minimum <= value <= measurement.maximum:
            return ProfileResult(
                False, "", ERR_RANGE,
                f"{measurement.label} of {value:g} {measurement.unit} is "
                f"outside {measurement.minimum:g} to {measurement.maximum:g}. "
                f"A tape read against the wrong edge usually lands here.", {})
        read[measurement.key] = value

    if read.get("waist", 0) > read.get("chest", 0):
        return ProfileResult(
            False, "", ERR_WAIST_OVER_CHEST,
            f"The waist at {read['waist']:g} in is larger than the chest at "
            f"{read['chest']:g} in. That happens, and it is also what a "
            f"transposed pair of numbers looks like, so the cutter confirms it "
            f"before the cloth is touched.", {})

    digest = hashlib.sha256(
        "|".join([clean_name] + [f"{key}={read[key]:g}"
                                 for key in sorted(read)]).encode("utf-8")
    ).hexdigest()[:8].upper()
    reference = f"ASK-{digest}"
    return ProfileResult(
        True, reference, "",
        f"Profile saved for {clean_name} as {reference}. In production this is "
        f"one insert into the clients table and one into measurements, written "
        f"in a single transaction so a half saved profile cannot exist.",
        read)


def profile_rows(values: dict, catalogue=MEASUREMENTS) -> list[dict]:
    return [
        {"Measurement": measurement.label,
         "Value": f"{values.get(measurement.key, 0):g} {measurement.unit}",
         "Taken": measurement.note}
        for measurement in catalogue
    ]


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
