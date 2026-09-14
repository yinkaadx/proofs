"""Unit tests for the Askew bespoke commission engine.

Written for pytest. These exercise the pricing and the state handling directly:
the module keeps its logic in plain functions above `main()`, so nothing here
needs a browser or a Streamlit runtime.

Run: pytest tests/test_askew_suit_engine.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from askew_suit_engine import (  # noqa: E402
    BASE_SUITS,
    DEPOSIT_PERCENT,
    ERR_LINING_UNKNOWN,
    ERR_MISSING,
    ERR_MONOGRAM_EMPTY,
    ERR_MONOGRAM_LENGTH,
    ERR_MONOGRAM_SHAPE,
    ERR_NAME,
    ERR_NOT_A_NUMBER,
    ERR_RANGE,
    ERR_UNKNOWN_BASE,
    ERR_WAIST_OVER_CHEST,
    LINING_COLOURS,
    LINING_KEY,
    LINING_SWATCH,
    MAX_MONOGRAM,
    MEASUREMENTS,
    MONOGRAM_KEY,
    UPGRADES,
    Commission,
    base_by_key,
    deposit_for,
    measurement_by_key,
    money,
    price,
    profile_rows,
    save_profile,
    upgrade_by_key,
    validate_commission,
)

BASE = "two_piece"
GOOD_MEASUREMENTS = {m.key: m.default for m in MEASUREMENTS}


def commission(*upgrades: str, monogram: str = "AAO",
               lining: str = LINING_COLOURS[0]) -> Commission:
    return Commission(BASE, tuple(upgrades), monogram, lining)


# ---------------------------------------------------------------------------
# The catalogue the brief specifies
# ---------------------------------------------------------------------------

def test_every_base_suit_is_one_thousand_dollars():
    for suit in BASE_SUITS:
        assert suit.price_cents == 100_000
        assert money(suit.price_cents) == "$1,000.00"


def test_the_premium_lining_is_fifty_dollars():
    assert upgrade_by_key(LINING_KEY).price_cents == 5_000
    assert money(5_000) == "$50.00"


def test_the_monogram_is_twenty_five_dollars():
    assert upgrade_by_key(MONOGRAM_KEY).price_cents == 2_500
    assert money(2_500) == "$25.00"


def test_the_deposit_requirement_is_fifty_percent():
    assert DEPOSIT_PERCENT == Decimal("50")


def test_an_unknown_base_or_upgrade_is_refused_rather_than_guessed():
    assert base_by_key("velvet_dinner_jacket") is None
    assert upgrade_by_key("gold_buttons") is None


def test_every_lining_colour_has_a_swatch():
    assert set(LINING_SWATCH) == set(LINING_COLOURS)
    for value in LINING_SWATCH.values():
        assert value.startswith("#") and len(value) == 7


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cents,text", [
    (0, "$0.00"), (5, "$0.05"), (2_500, "$25.00"), (107_500, "$1,075.00"),
    (1_000_000, "$10,000.00"), (-2_500, "-$25.00"),
])
def test_amounts_are_formatted_to_the_cent(cents, text):
    assert money(cents) == text


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def test_a_base_suit_alone_totals_one_thousand():
    quote = price(commission())
    assert quote.subtotal_cents == 100_000
    assert quote.total_cents == 100_000
    assert len(quote.items) == 1


def test_the_worked_example_from_the_brief():
    """A thousand dollar suit with the lining and the monogram is 1075, and
    the deposit is 537.50."""
    quote = price(commission(LINING_KEY, MONOGRAM_KEY))
    assert quote.total_cents == 107_500
    assert money(quote.total_cents) == "$1,075.00"
    assert money(quote.deposit_cents) == "$537.50"
    assert money(quote.balance_cents) == "$537.50"


def test_each_upgrade_adds_exactly_its_own_price():
    bare = price(commission()).total_cents
    for upgrade in UPGRADES:
        quote = price(commission(upgrade.key))
        assert quote.total_cents == bare + upgrade.price_cents


def test_every_upgrade_together_adds_every_price():
    keys = [upgrade.key for upgrade in UPGRADES]
    quote = price(commission(*keys))
    assert quote.total_cents == 100_000 + sum(u.price_cents for u in UPGRADES)
    assert len(quote.items) == 1 + len(UPGRADES)


def test_the_line_items_add_up_to_the_subtotal():
    quote = price(commission(LINING_KEY, MONOGRAM_KEY))
    assert sum(item.amount_cents for item in quote.items) == quote.subtotal_cents


def test_the_lining_line_names_the_colour_chosen():
    quote = price(commission(LINING_KEY, lining="Forest"))
    lining = next(item for item in quote.items if item.label == "Premium lining")
    assert "Forest" in lining.detail


def test_the_monogram_line_carries_the_initials():
    quote = price(commission(MONOGRAM_KEY, monogram="JRB"))
    mono = next(item for item in quote.items if item.label == "Monogram")
    assert "JRB" in mono.detail


def test_pricing_an_unknown_suit_is_refused():
    with pytest.raises(ValueError):
        price(Commission("not_a_suit", ()))


def test_the_summary_table_holds_one_type_per_column():
    """The table widget serialises through Arrow, which refuses a mixed
    column."""
    rows = price(commission(LINING_KEY, MONOGRAM_KEY)).rows()
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# The deposit
# ---------------------------------------------------------------------------

def test_the_deposit_is_half_of_the_total():
    assert deposit_for(100_000) == 50_000
    assert deposit_for(107_500) == 53_750


def test_the_deposit_and_the_balance_add_back_to_the_total():
    for total in (0, 1, 3, 99, 100_000, 107_500, 123_457):
        deposit = deposit_for(total)
        assert deposit + (total - deposit) == total


def test_half_of_an_odd_number_of_cents_rounds_up_rather_than_vanishing():
    """This is where a float loses a cent, and a deposit that disagrees with
    the invoice by a cent is exactly what a bespoke client notices."""
    assert deposit_for(1_001) == 501
    assert deposit_for(1_001) + (1_001 - deposit_for(1_001)) == 1_001


def test_a_deposit_of_nothing_is_nothing():
    assert deposit_for(0) == 0


@pytest.mark.parametrize("percent,expected", [
    (Decimal("0"), 0), (Decimal("25"), 26_875), (Decimal("50"), 53_750),
    (Decimal("100"), 107_500),
])
def test_the_percentage_is_a_parameter_rather_than_a_number_in_the_maths(
        percent, expected):
    assert deposit_for(107_500, percent) == expected


def test_a_negative_total_or_an_impossible_percentage_is_refused():
    with pytest.raises(ValueError):
        deposit_for(-1)
    with pytest.raises(ValueError):
        deposit_for(1_000, Decimal("140"))


def test_the_quote_carries_the_deposit_computed_from_its_own_total():
    quote = price(commission(*[u.key for u in UPGRADES]))
    assert quote.deposit_cents == deposit_for(quote.total_cents)
    assert quote.deposit_cents + quote.balance_cents == quote.total_cents


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def test_selecting_an_upgrade_returns_a_new_commission_and_leaves_the_old():
    """Frozen on purpose. Mutating a shared basket is how a total on screen
    comes to describe a selection the customer already changed."""
    original = commission()
    updated = original.with_upgrade(LINING_KEY, True)
    assert original.upgrade_keys == ()
    assert updated.upgrade_keys == (LINING_KEY,)
    assert updated is not original


def test_deselecting_an_upgrade_removes_it():
    updated = commission(LINING_KEY, MONOGRAM_KEY).with_upgrade(LINING_KEY,
                                                                False)
    assert updated.upgrade_keys == (MONOGRAM_KEY,)


def test_selecting_the_same_upgrade_twice_does_not_charge_twice():
    once = commission().with_upgrade(MONOGRAM_KEY, True)
    twice = once.with_upgrade(MONOGRAM_KEY, True)
    assert twice.upgrade_keys == (MONOGRAM_KEY,)
    assert price(twice).total_cents == price(once).total_cents


def test_deselecting_something_that_was_never_selected_changes_nothing():
    updated = commission().with_upgrade(LINING_KEY, False)
    assert updated.upgrade_keys == ()


def test_upgrades_stay_in_catalogue_order_however_they_were_added():
    """So the summary reads the same way twice, whatever order the boxes were
    ticked in."""
    forwards = commission().with_upgrade(LINING_KEY, True) \
                           .with_upgrade("second_trouser", True)
    backwards = commission().with_upgrade("second_trouser", True) \
                            .with_upgrade(LINING_KEY, True)
    assert forwards.upgrade_keys == backwards.upgrade_keys
    assert [item.label for item in price(forwards).items] == \
        [item.label for item in price(backwards).items]


def test_an_upgrade_that_is_not_in_the_catalogue_is_refused():
    with pytest.raises(ValueError):
        commission().with_upgrade("gold_buttons", True)


def test_has_reports_what_is_selected():
    subject = commission(LINING_KEY)
    assert subject.has(LINING_KEY) is True
    assert subject.has(MONOGRAM_KEY) is False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_a_complete_commission_validates():
    ok, code, message = validate_commission(commission(LINING_KEY,
                                                       MONOGRAM_KEY))
    assert ok is True
    assert code == "" and message == ""


def test_a_monogram_with_no_initials_is_refused_at_the_order():
    ok, code, message = validate_commission(
        commission(MONOGRAM_KEY, monogram="  "))
    assert ok is False
    assert code == ERR_MONOGRAM_EMPTY
    assert "workroom" in message


def test_initials_have_to_be_letters():
    assert validate_commission(
        commission(MONOGRAM_KEY, monogram="A1O"))[1] == ERR_MONOGRAM_SHAPE


def test_more_initials_than_the_facing_takes_are_refused():
    long = "A" * (MAX_MONOGRAM + 1)
    assert validate_commission(
        commission(MONOGRAM_KEY, monogram=long))[1] == ERR_MONOGRAM_LENGTH


def test_initials_are_fine_at_exactly_the_limit():
    assert validate_commission(
        commission(MONOGRAM_KEY, monogram="A" * MAX_MONOGRAM))[0] is True


def test_empty_initials_do_not_matter_when_no_monogram_was_ordered():
    assert validate_commission(commission(LINING_KEY, monogram=""))[0] is True


def test_a_lining_colour_outside_the_house_range_is_refused():
    assert validate_commission(
        commission(LINING_KEY, lining="Neon"))[1] == ERR_LINING_UNKNOWN


def test_an_unknown_base_is_refused_before_anything_else():
    ok, code, _ = validate_commission(Commission("not_a_suit", ()))
    assert ok is False
    assert code == ERR_UNKNOWN_BASE


# ---------------------------------------------------------------------------
# The measurement profile
# ---------------------------------------------------------------------------

def test_a_complete_profile_saves_and_returns_a_reference():
    result = save_profile("Amara Okafor", GOOD_MEASUREMENTS)
    assert result.saved is True
    assert result.reference.startswith("ASK-")
    assert "Amara Okafor" in result.message
    assert result.values["chest"] == 40.0


def test_the_same_profile_always_produces_the_same_reference():
    first = save_profile("Amara Okafor", GOOD_MEASUREMENTS)
    second = save_profile("Amara Okafor", GOOD_MEASUREMENTS)
    assert first.reference == second.reference


def test_a_different_client_gets_a_different_reference():
    other = save_profile("Tunde Eze", GOOD_MEASUREMENTS)
    assert other.reference != save_profile("Amara Okafor",
                                           GOOD_MEASUREMENTS).reference


def test_a_changed_measurement_changes_the_reference():
    changed = {**GOOD_MEASUREMENTS, "chest": 42.0}
    assert save_profile("Amara Okafor", changed).reference != \
        save_profile("Amara Okafor", GOOD_MEASUREMENTS).reference


def test_a_profile_without_a_name_is_refused():
    result = save_profile("   ", GOOD_MEASUREMENTS)
    assert result.saved is False
    assert result.code == ERR_NAME


def test_a_missing_measurement_is_refused_rather_than_defaulted():
    incomplete = {k: v for k, v in GOOD_MEASUREMENTS.items() if k != "sleeve"}
    result = save_profile("Amara Okafor", incomplete)
    assert result.code == ERR_MISSING
    assert "Sleeve" in result.message


def test_a_measurement_that_is_not_a_number_is_refused():
    result = save_profile("Amara Okafor",
                          {**GOOD_MEASUREMENTS, "chest": "about forty"})
    assert result.code == ERR_NOT_A_NUMBER


@pytest.mark.parametrize("key", [m.key for m in MEASUREMENTS])
def test_every_measurement_is_range_checked_on_both_sides(key):
    measurement = measurement_by_key(key)
    below = {**GOOD_MEASUREMENTS, key: measurement.minimum - 0.1}
    above = {**GOOD_MEASUREMENTS, key: measurement.maximum + 0.1}
    assert save_profile("Amara Okafor", below).code == ERR_RANGE
    assert save_profile("Amara Okafor", above).code == ERR_RANGE


@pytest.mark.parametrize("key", [m.key for m in MEASUREMENTS])
def test_the_range_includes_its_own_endpoints(key):
    measurement = measurement_by_key(key)
    for edge in (measurement.minimum, measurement.maximum):
        values = {**GOOD_MEASUREMENTS, key: edge}
        result = save_profile("Amara Okafor", values)
        # Only the waist against chest rule may still object at an endpoint.
        assert result.saved or result.code == ERR_WAIST_OVER_CHEST


def test_a_waist_larger_than_the_chest_is_held_for_confirmation():
    """It happens, and it is also what a transposed pair of numbers looks
    like, so the cutter confirms it before the cloth is touched."""
    result = save_profile("Amara Okafor",
                          {**GOOD_MEASUREMENTS, "waist": 44.0, "chest": 40.0})
    assert result.saved is False
    assert result.code == ERR_WAIST_OVER_CHEST


def test_a_refused_profile_returns_no_values_and_no_reference():
    result = save_profile("", GOOD_MEASUREMENTS)
    assert result.values == {}
    assert result.reference == ""


def test_the_success_message_says_plainly_that_this_is_a_simulation():
    message = save_profile("Amara Okafor", GOOD_MEASUREMENTS).message
    assert "in production" in message.lower()
    assert "single transaction" in message


def test_the_profile_table_holds_one_type_per_column():
    rows = profile_rows(GOOD_MEASUREMENTS)
    assert len(rows) == len(MEASUREMENTS)
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Prose discipline
# ---------------------------------------------------------------------------

def test_no_dash_characters_in_any_user_facing_prose():
    parts = [suit.note for suit in BASE_SUITS]
    parts += [upgrade.note for upgrade in UPGRADES]
    parts += [measurement.note for measurement in MEASUREMENTS]
    parts.append(save_profile("Amara Okafor", GOOD_MEASUREMENTS).message)
    for subject in (commission(MONOGRAM_KEY, monogram=""),
                    commission(MONOGRAM_KEY, monogram="ABCD"),
                    commission(LINING_KEY, lining="Neon")):
        parts.append(validate_commission(subject)[2])
    prose = "\n".join(parts)
    assert "—" not in prose
    assert "–" not in prose


# ---------------------------------------------------------------------------
# One engine, two front doors
# ---------------------------------------------------------------------------

def test_the_standalone_app_and_the_hub_tool_share_one_engine():
    """Two implementations of the same pricing is how a deposit comes to
    disagree with an invoice. The root module re-exports the package rather
    than holding a second copy, so there is nothing to drift."""
    import askew_suit_engine as standalone
    from tools.askew_suit_engine import core

    assert standalone.price is core.price
    assert standalone.Commission is core.Commission
    assert standalone.deposit_for is core.deposit_for
    assert standalone.save_profile is core.save_profile


def test_the_engine_module_carries_no_streamlit_import():
    """The hub renders it, pytest imports it, and neither needs a runtime."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "tools" / \
        "askew_suit_engine" / "core.py"
    assert "import streamlit" not in source.read_text()


def test_every_public_name_the_standalone_app_needs_is_exported():
    import askew_suit_engine as standalone
    from tools.askew_suit_engine import core

    for name in core.__all__:
        assert hasattr(standalone, name), name
