"""Page tests for the ASKEW Bespoke Pricing Engine, via AppTest.

Written for pytest.

Run: pytest tests/test_askew_suit_engine_page.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.askew_suit_engine.core import (  # noqa: E402
    BASE_SUITS,
    ERR_MONOGRAM_EMPTY,
    ERR_MONOGRAM_LENGTH,
    ERR_MONOGRAM_SHAPE,
    ERR_WAIST_OVER_CHEST,
    LINING_COLOURS,
    MEASUREMENTS,
    UPGRADES,
    money,
)
from tools.askew_suit_engine.page import STATE  # noqa: E402

HARNESS = (Path(__file__).resolve().parent
           / "_page_harness_askew_suit_engine.py")

SAVE = "Save to the client record"
LINING = "Premium lining, $50.00"
MONOGRAM = "Monogram, $25.00"
CUFFS = "Working cuffs, $75.00"
TROUSER = "Second trouser, $275.00"


def text_of(at: AppTest) -> str:
    parts: list[str] = []
    for attr in ("markdown", "text", "info", "success", "warning", "error",
                 "caption", "code", "header", "subheader", "title"):
        try:
            parts += [str(getattr(el, "value", "")) for el in getattr(at, attr)]
        except Exception:
            pass
    try:
        for frame in at.get("dataframe"):
            value = getattr(frame, "value", None)
            if value is not None and hasattr(value, "to_numpy"):
                parts += [str(cell) for cell in value.to_numpy().ravel()]
                parts += [str(col) for col in value.columns]
    except Exception:
        pass
    return "\n".join(parts)


def run_app(timeout: int = 180) -> AppTest:
    at = AppTest.from_file(str(HARNESS), default_timeout=timeout)
    at.run()
    return at


def widget(at: AppTest, kind: str, label: str):
    for el in getattr(at, kind):
        if el.label == label:
            return el
    raise AssertionError(f"no {kind} labelled {label!r}; "
                         f"have {[el.label for el in getattr(at, kind)]}")


def press(at: AppTest, label: str) -> AppTest:
    for button in at.button:
        if button.label == label:
            return button.click().run()
    raise AssertionError(f"no button labelled {label!r}; "
                         f"have {[b.label for b in at.button]}")


def set_upgrade(at: AppTest, label: str, on: bool) -> AppTest:
    widget(at, "checkbox", label).set_value(on).run()
    return at


def set_measure(at: AppTest, key: str, value: float, at_: AppTest | None = None):
    measurement = next(m for m in MEASUREMENTS if m.key == key)
    widget(at, "number_input",
           f"{measurement.label} ({measurement.unit})").set_value(value).run()
    return at


def profile_of(at: AppTest):
    return at.session_state[STATE]["profile"]


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------

def test_the_page_renders_with_no_exception():
    at = run_app()
    assert not at.exception, [str(e.value) for e in at.exception]


def test_the_hero_and_the_three_tabs_are_present():
    at = run_app()
    assert "ASKEW Bespoke Pricing Engine" in text_of(at)
    labels = [tab.label for tab in at.tabs]
    for expected in ("Build Your Suit", "Order Summary", "Measurement Profile"):
        assert expected in labels


def test_no_two_widgets_of_a_kind_share_a_label():
    at = run_app()
    for kind in ("checkbox", "number_input", "text_input", "selectbox",
                 "radio", "button"):
        labels = [el.label for el in getattr(at, kind)]
        assert len(labels) == len(set(labels)), f"{kind}: {labels}"


def test_it_says_plainly_that_no_payment_is_taken():
    assert "no payment is taken" in text_of(run_app())


# ---------------------------------------------------------------------------
# Configurator
# ---------------------------------------------------------------------------

def test_every_base_suit_is_offered_with_its_cloth_and_price():
    body = text_of(run_app())
    for suit in BASE_SUITS:
        assert suit.name in body
        assert suit.cloth in body
    assert money(BASE_SUITS[0].price_cents) in body


def test_every_upgrade_is_offered_with_its_price():
    labels = [c.label for c in run_app().checkbox]
    assert len(labels) == len(UPGRADES)
    for upgrade in UPGRADES:
        assert any(upgrade.name in label and money(upgrade.price_cents) in label
                   for label in labels)


def test_the_page_opens_on_a_base_suit_with_the_lining_on():
    at = run_app()
    body = text_of(at)
    assert "$1,050.00" in body       # base 1000 plus lining 50
    assert "Ready" in body


def test_turning_an_upgrade_on_moves_the_total():
    at = set_upgrade(run_app(), MONOGRAM, True)
    assert "$1,075.00" in text_of(at)


def test_turning_every_upgrade_on_prices_them_all():
    at = run_app()
    for label in (LINING, MONOGRAM, CUFFS, TROUSER):
        at = set_upgrade(at, label, True)
    # 1000 base plus 50 plus 25 plus 75 plus 275
    assert "$1,425.00" in text_of(at)


def test_turning_every_upgrade_off_leaves_the_base_price():
    at = run_app()
    for label in (LINING, MONOGRAM, CUFFS, TROUSER):
        at = set_upgrade(at, label, False)
    body = text_of(at)
    assert "$1,000.00" in body
    assert "$500.00" in body         # the deposit


def test_the_lining_colour_only_appears_once_the_lining_is_chosen():
    at = set_upgrade(run_app(), LINING, False)
    assert "Lining colour" not in [s.label for s in at.selectbox]
    at = set_upgrade(at, LINING, True)
    assert "Lining colour" in [s.label for s in at.selectbox]


def test_choosing_a_lining_colour_names_it_on_the_line():
    at = run_app()
    widget(at, "selectbox", "Lining colour").set_value(LINING_COLOURS[1]).run()
    assert f"{LINING_COLOURS[1]} cupro" in text_of(at)


def test_a_monogram_with_no_initials_is_refused_at_the_order():
    at = set_upgrade(run_app(), MONOGRAM, True)
    widget(at, "text_input", "Monogram, up to 3 letters").set_value("").run()
    body = text_of(at)
    assert ERR_MONOGRAM_EMPTY in body
    assert "stops at the workroom" in body


def test_a_monogram_with_digits_is_refused():
    at = set_upgrade(run_app(), MONOGRAM, True)
    widget(at, "text_input", "Monogram, up to 3 letters").set_value("A1").run()
    assert ERR_MONOGRAM_SHAPE in text_of(at)


def test_a_monogram_longer_than_the_facing_takes_is_refused():
    at = set_upgrade(run_app(), MONOGRAM, True)
    widget(at, "text_input", "Monogram, up to 3 letters").set_value("ABCD").run()
    assert ERR_MONOGRAM_LENGTH in text_of(at)


def test_a_refused_commission_produces_no_summary():
    at = set_upgrade(run_app(), MONOGRAM, True)
    widget(at, "text_input", "Monogram, up to 3 letters").set_value("").run()
    body = text_of(at)
    assert "No summary yet" in body
    assert "Line by line" not in body


# ---------------------------------------------------------------------------
# Order summary and the deposit
# ---------------------------------------------------------------------------

def test_the_summary_splits_the_total_in_half():
    body = text_of(run_app())
    assert "$1,050.00" in body
    assert "$525.00" in body
    assert "Deposit due today" in body
    assert "Balance at fitting" in body


def test_the_two_halves_add_back_to_the_total_on_screen():
    at = run_app()
    for label in (LINING, MONOGRAM, CUFFS, TROUSER):
        at = set_upgrade(at, label, True)
    body = text_of(at)
    assert "$1,425.00" in body
    assert "$712.50" in body        # half of an odd number of dollars


def test_the_summary_lists_every_chosen_line():
    at = set_upgrade(run_app(), MONOGRAM, True)
    body = text_of(at)
    assert "Premium lining" in body
    assert "Monogram" in body
    assert "Super 120s wool" in body


def test_the_order_payload_reports_the_amounts_in_cents():
    body = text_of(run_app())
    assert "total       105000 cents" in body
    assert "deposit     52500 cents" in body


def test_the_split_is_explained_rather_than_only_shown():
    assert "add back to" in text_of(run_app())


# ---------------------------------------------------------------------------
# Measurement profile
# ---------------------------------------------------------------------------

def test_every_measurement_has_a_field_and_a_note():
    at = run_app()
    labels = [n.label for n in at.number_input]
    for measurement in MEASUREMENTS:
        assert f"{measurement.label} ({measurement.unit})" in labels
    assert "tape level under the arms" in text_of(at)


def test_nothing_is_saved_until_the_button_is_pressed():
    at = run_app()
    assert profile_of(at) is None
    assert "Nothing saved yet" in text_of(at)


def test_a_complete_profile_saves_with_a_reference():
    at = press(run_app(), SAVE)
    profile = profile_of(at)
    assert profile.saved is True
    assert profile.reference.startswith("ASK-")
    assert "Profile saved" in text_of(at)


def test_the_same_profile_always_produces_the_same_reference():
    first = profile_of(press(run_app(), SAVE)).reference
    second = profile_of(press(run_app(), SAVE)).reference
    assert first == second


def test_a_profile_with_no_name_is_refused():
    at = run_app()
    widget(at, "text_input", "Client name").set_value("").run()
    at = press(at, SAVE)
    assert profile_of(at).saved is False
    assert "needs a name on it" in text_of(at)


def test_a_waist_larger_than_the_chest_is_held_for_confirmation():
    """It happens, and it is also what a transposed pair of numbers looks
    like."""
    at = run_app()
    at = set_measure(at, "waist", 48.0)
    at = press(at, SAVE)
    assert profile_of(at).saved is False
    assert ERR_WAIST_OVER_CHEST in text_of(at)


def test_a_measurement_outside_its_range_is_refused():
    at = run_app()
    at = set_measure(at, "chest", 12.0)
    at = press(at, SAVE)
    assert profile_of(at).saved is False
    assert "outside" in text_of(at)


def test_a_saved_profile_lists_what_was_recorded():
    at = press(run_app(), SAVE)
    body = text_of(at)
    assert "Chest" in body
    assert "40 in" in body


@pytest.mark.parametrize("suit", [s.key for s in BASE_SUITS])
def test_every_base_suit_renders_without_an_exception(suit):
    at = run_app()
    widget(at, "radio", "Base suit").set_value(suit).run()
    assert not at.exception, [str(e.value) for e in at.exception]


def test_no_dash_characters_reach_the_screen():
    at = press(set_upgrade(run_app(), MONOGRAM, True), SAVE)
    body = text_of(at)
    assert "—" not in body
    assert "–" not in body
