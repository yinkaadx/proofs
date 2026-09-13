"""Askew bespoke commission engine.

The pricing, the deposit split and the measurement validation for a bespoke
menswear commission. No Streamlit import, so the same arithmetic runs behind
the hub page, behind the standalone `askew_suit_engine.py` at the repository
root, and inside pytest without a runtime.

Every amount is an integer number of cents. Half of an odd total is where a
float quietly loses one, and a deposit that disagrees with the invoice by a
cent is the kind of detail a bespoke client notices.

Frozen dataclasses throughout: every selection returns a new commission rather
than mutating a shared one, because a basket mutated in place is how a total on
screen comes to describe a selection the customer already changed.
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


__all__ = [
    "APP_NAME",
    "APP_TAGLINE",
    "ENGINE_VERSION",
    "DEPOSIT_PERCENT",
    "money",
    "BaseSuit",
    "BASE_SUITS",
    "Upgrade",
    "UPGRADES",
    "MONOGRAM_KEY",
    "LINING_KEY",
    "LINING_COLOURS",
    "LINING_SWATCH",
    "base_by_key",
    "upgrade_by_key",
    "MAX_MONOGRAM",
    "ERR_UNKNOWN_BASE",
    "ERR_UNKNOWN_UPGRADE",
    "ERR_MONOGRAM_EMPTY",
    "ERR_MONOGRAM_SHAPE",
    "ERR_MONOGRAM_LENGTH",
    "ERR_LINING_UNKNOWN",
    "LineItem",
    "Commission",
    "Quote",
    "deposit_for",
    "price",
    "validate_commission",
    "Measurement",
    "MEASUREMENTS",
    "measurement_by_key",
    "ERR_MISSING",
    "ERR_RANGE",
    "ERR_NOT_A_NUMBER",
    "ERR_NAME",
    "ERR_WAIST_OVER_CHEST",
    "ProfileResult",
    "save_profile",
    "profile_rows",
]
