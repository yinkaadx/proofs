"""Engine tests for the WasteTab Logistics and Financial Engine.

Written for pytest. Deterministic throughout: every amount is integer pence and
nothing reads the clock or a random source.

Run: pytest tests/test_wastetab_dispatch_engine.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.wastetab_dispatch_engine.core import (  # noqa: E402
    BOOKED,
    CANCELLED,
    CARRIERS,
    COMPLETED,
    DEFAULT_MARGIN_PERCENT,
    ERR_POSTCODE_EMPTY,
    ERR_POSTCODE_SHAPE,
    FEE_FIXED_PENCE,
    GRAB,
    LINK_QUOTE,
    LINK_SURCHARGE,
    NEW_ENQUIRY,
    NO_MATCH_AREA,
    NO_MATCH_SERVICE,
    NO_MATCH_WASTE,
    ON_HOLD,
    QUOTE_SENT,
    SKIP_4,
    SKIP_8,
    SKIP_12,
    STATES,
    SURCHARGE_BANDS,
    TERMINAL,
    TRANSITIONS,
    VALVE_NO_CHANGE,
    VALVE_PAUSED,
    VALVE_REFUSED,
    VALVE_RESUMED,
    WASTE_CONSTRUCTION,
    WASTE_GENERAL,
    WASTE_GREEN,
    WASTE_HAZARDOUS,
    build_quote,
    dispatch,
    gross_up,
    link_rows,
    move,
    net_after_fee,
    open_job,
    parse_postcode,
    payment_link,
    pounds,
    processor_fee,
    resolve_safety_valve,
    sample_enquiry,
    to_pence,
    trigger_safety_valve,
)


def job_at(state: str = NEW_ENQUIRY, postcode: str = "SE15 4RT",
           waste: str = WASTE_GENERAL):
    job = open_job(sample_enquiry(postcode=postcode, waste_type=waste))
    path = {NEW_ENQUIRY: [], QUOTE_SENT: [QUOTE_SENT],
            BOOKED: [QUOTE_SENT, BOOKED],
            COMPLETED: [QUOTE_SENT, BOOKED, COMPLETED],
            CANCELLED: [CANCELLED]}[state]
    for step in path:
        move(job, step)
    return job


# ---------------------------------------------------------------------------
# Money reading
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("typed,expected", [
    ("100", 10000), ("100.00", 10000), ("£100.00", 10000),
    ("1,250.50", 125050), (" 99.99 ", 9999), ("0.01", 1), ("0", 0),
])
def test_pounds_are_read_however_they_are_typed(typed, expected):
    assert to_pence(typed) == expected


def test_a_third_of_a_penny_rounds_to_the_nearest_penny():
    assert to_pence("1.005") == 101
    assert to_pence("1.004") == 100


def test_something_that_is_not_money_is_refused():
    for typed in ("", "   ", "one hundred", "12.3.4"):
        with pytest.raises(ValueError):
            to_pence(typed)


def test_a_negative_amount_is_refused():
    with pytest.raises(ValueError):
        to_pence("-10")


def test_pence_are_formatted_as_pounds():
    assert pounds(13337) == "£133.37"
    assert pounds(5) == "£0.05"
    assert pounds(0) == "£0.00"
    assert pounds(-250) == "-£2.50"


# ---------------------------------------------------------------------------
# The processor fee and the gross up
# ---------------------------------------------------------------------------

def test_the_fee_is_the_percentage_rounded_to_a_penny_plus_the_fixed_part():
    assert processor_fee(10000) == 230 + FEE_FIXED_PENCE   # 2.3% of £100
    assert processor_fee(13337) == 337
    assert processor_fee(0) == FEE_FIXED_PENCE


def test_a_negative_charge_is_refused():
    with pytest.raises(ValueError):
        processor_fee(-1)


def test_the_worked_example_lands_exactly_on_the_target():
    """A hundred pound bid, thirty percent margin, a hundred and thirty net."""
    quote = build_quote(10000)
    assert quote.margin_pence == 3000
    assert quote.net_target_pence == 13000
    assert quote.charge_pence == 13337
    assert quote.fee_pence == 337
    assert quote.net_received_pence == 13000
    assert quote.surplus_pence == 0


def test_the_naive_link_is_short_by_the_whole_fee():
    quote = build_quote(10000)
    assert quote.naive_charge_pence == 13000
    assert quote.naive_shortfall_pence == 329
    assert net_after_fee(13000) == 12671


@pytest.mark.parametrize("net", [1, 50, 99, 100, 999, 1000, 5000, 13000,
                                 99999, 100000, 250000])
def test_the_gross_up_never_lands_short(net):
    assert net_after_fee(gross_up(net)) >= net


@pytest.mark.parametrize("net", [1, 50, 99, 100, 999, 1000, 5000, 13000,
                                 99999, 100000, 250000])
def test_the_gross_up_is_the_smallest_charge_that_clears_the_target(net):
    gross = gross_up(net)
    assert net_after_fee(gross - 1) < net


def test_the_gross_up_holds_for_every_penny_across_the_working_range():
    """The property that matters, checked exhaustively rather than sampled.

    A formula that is right at a hundred pounds and a penny short at ninety
    nine is worse than no formula, because nobody looks again.
    """
    for net in range(1, 20001):
        gross = gross_up(net)
        assert net_after_fee(gross) >= net, net
        assert net_after_fee(gross - 1) < net, net


def test_the_gross_up_of_nothing_is_nothing():
    assert gross_up(0) == 0


def test_a_negative_net_is_refused():
    with pytest.raises(ValueError):
        gross_up(-1)


def test_a_bigger_charge_carries_a_bigger_fee():
    assert processor_fee(20000) > processor_fee(10000)


@pytest.mark.parametrize("margin,expected_net", [
    (Decimal("0"), 10000), (Decimal("10"), 11000), (Decimal("30"), 13000),
    (Decimal("50"), 15000),
])
def test_the_margin_is_added_to_the_bid_before_the_gross_up(margin,
                                                            expected_net):
    quote = build_quote(10000, margin)
    assert quote.net_target_pence == expected_net
    assert quote.net_received_pence == expected_net


def test_a_margin_that_lands_on_half_a_penny_rounds_up():
    quote = build_quote(3333, Decimal("15"))
    assert quote.margin_pence == 500      # 499.95 rounds to 500
    assert quote.net_target_pence == 3833


def test_a_negative_bid_or_margin_is_refused():
    with pytest.raises(ValueError):
        build_quote(-1)
    with pytest.raises(ValueError):
        build_quote(10000, Decimal("-5"))


def test_the_quote_table_is_arrow_safe():
    rows = build_quote(10000).rows()
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Postcodes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("typed,outward,area", [
    ("SE15 4RT", "SE15", "SE"), ("se15 4rt", "SE15", "SE"),
    ("SE154RT", "SE15", "SE"), ("  se15   4rt ", "SE15", "SE"),
    ("G1 2FF", "G1", "G"), ("EC1A 1BB", "EC1A", "EC"),
    ("W1A 0AX", "W1A", "W"), ("B33 8TH", "B33", "B"),
])
def test_a_real_postcode_is_read_however_it_is_typed(typed, outward, area):
    result = parse_postcode(typed)
    assert result.ok is True
    assert result.outward == outward
    assert result.area == area


def test_the_formatted_postcode_carries_the_single_space():
    assert parse_postcode("se154rt").formatted == "SE15 4RT"


def test_an_empty_postcode_is_refused():
    result = parse_postcode("")
    assert result.ok is False
    assert result.error_code == ERR_POSTCODE_EMPTY


@pytest.mark.parametrize("typed", ["not a postcode", "12345", "SE15",
                                   "4RT", "ZZZZ 9ZZ", "SE15 4R"])
def test_anything_that_is_not_a_postcode_is_refused(typed):
    result = parse_postcode(typed)
    assert result.ok is False
    assert result.error_code == ERR_POSTCODE_SHAPE


def test_a_refused_postcode_is_held_for_a_human():
    assert "held for a human" in parse_postcode("nonsense").message


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def test_a_london_postcode_finds_the_london_carriers():
    result = dispatch("SE15 4RT", SKIP_8, WASTE_GENERAL)
    assert result.matched is True
    assert {carrier.code for carrier in result.matches} == {"CAR-01", "CAR-02"}


def test_carriers_are_ordered_cheapest_first():
    result = dispatch("SE15 4RT", SKIP_8, WASTE_GENERAL)
    bids = [carrier.bid_pence for carrier in result.matches]
    assert bids == sorted(bids)
    assert result.best.code == "CAR-01"


def test_a_leeds_postcode_finds_only_the_leeds_carrier():
    result = dispatch("LS1 4DY", SKIP_12, WASTE_CONSTRUCTION)
    assert [carrier.code for carrier in result.matches] == ["CAR-03"]


def test_an_area_nobody_covers_is_refused_rather_than_dispatched():
    result = dispatch("EH1 1YZ", SKIP_8, WASTE_GENERAL)
    assert result.matched is False
    assert NO_MATCH_AREA in result.reason
    assert "partner desk" in result.reason


def test_a_service_no_local_carrier_runs_is_refused():
    result = dispatch("LS1 4DY", GRAB, WASTE_GENERAL)
    assert result.matched is False
    assert NO_MATCH_SERVICE in result.reason


def test_waste_nobody_local_is_licensed_for_is_refused():
    result = dispatch("SE15 4RT", SKIP_8, WASTE_HAZARDOUS)
    assert result.matched is False
    assert NO_MATCH_WASTE in result.reason
    assert "comes back" in result.reason


def test_the_one_carrier_licensed_for_hazardous_is_found_in_its_own_area():
    result = dispatch("G1 2FF", SKIP_8, WASTE_HAZARDOUS)
    assert [carrier.code for carrier in result.matches] == ["CAR-04"]


def test_a_dormant_carrier_is_never_dispatched():
    """It covers the area and undercuts everyone, and it is switched off."""
    dormant = next(c for c in CARRIERS if not c.active)
    assert dormant.bid_pence < min(c.bid_pence for c in CARRIERS if c.active)
    result = dispatch("SE15 4RT", SKIP_8, WASTE_GENERAL)
    assert dormant.code not in {carrier.code for carrier in result.matches}


def test_a_bad_postcode_never_reaches_the_carrier_filter():
    result = dispatch("not a postcode", SKIP_8, WASTE_GENERAL)
    assert result.matched is False
    assert result.postcode.ok is False


def test_the_carrier_table_is_arrow_safe():
    rows = dispatch("SE15 4RT", SKIP_8, WASTE_GENERAL).rows()
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------

def test_the_happy_path_runs_end_to_end():
    job = open_job(sample_enquiry())
    assert job.state == NEW_ENQUIRY
    for step in (QUOTE_SENT, BOOKED, COMPLETED):
        assert move(job, step).ok is True
    assert job.state == COMPLETED
    assert [entry.state_to for entry in job.entries] == [QUOTE_SENT, BOOKED,
                                                         COMPLETED]


def test_a_job_cannot_skip_straight_to_completed():
    """A job completed from an enquiry is an invoice nobody can explain."""
    job = open_job(sample_enquiry())
    result = move(job, COMPLETED)
    assert result.ok is False
    assert job.state == NEW_ENQUIRY
    assert "may only go to" in result.message


def test_a_quote_cannot_be_completed_without_being_booked():
    job = job_at(QUOTE_SENT)
    assert move(job, COMPLETED).ok is False


def test_a_finished_job_does_not_move_again():
    for state in TERMINAL:
        job = job_at(COMPLETED if state == COMPLETED else CANCELLED)
        result = move(job, BOOKED)
        assert result.ok is False
        assert "does not move again" in result.message


def test_a_state_that_does_not_exist_is_refused():
    job = open_job(sample_enquiry())
    assert move(job, "INVOICED").ok is False


def test_every_state_is_reachable_from_the_map():
    reachable = {NEW_ENQUIRY}
    for state, onward in TRANSITIONS.items():
        reachable.update(onward)
    assert reachable == set(STATES)


def test_a_refused_move_writes_nothing_to_the_ledger():
    job = open_job(sample_enquiry())
    move(job, COMPLETED)
    assert job.entries == []


def test_the_ledger_records_who_moved_it_and_what_was_charged():
    job = job_at(QUOTE_SENT)
    move(job, BOOKED, actor="operator", charge_pence=13337)
    entry = job.entries[-1]
    assert entry.actor == "operator"
    assert entry.charge_pence == 13337
    assert job.charged_pence == 13337
    assert job.net_pence == 13000


def test_the_ledger_table_is_arrow_safe():
    job = job_at(BOOKED)
    rows = job.rows()
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Payment links
# ---------------------------------------------------------------------------

def test_a_payment_link_carries_the_charge_the_fee_and_the_net():
    job = job_at(QUOTE_SENT)
    link = payment_link(job, 13337, LINK_QUOTE)
    assert link.amount_pence == 13337
    assert link.fee_pence == 337
    assert link.net_pence == 13000
    assert link.url.endswith(link.reference)


def test_the_same_request_produces_the_same_link_reference():
    """A duplicated send cannot create a second charge for the same money."""
    job = job_at(QUOTE_SENT)
    first = payment_link(job, 13337, LINK_QUOTE)
    second = payment_link(job, 13337, LINK_QUOTE)
    assert first.reference == second.reference


def test_a_different_amount_produces_a_different_link():
    job = job_at(QUOTE_SENT)
    assert payment_link(job, 13337).reference != \
        payment_link(job, 13338).reference


def test_a_link_for_nothing_is_refused():
    job = job_at(QUOTE_SENT)
    with pytest.raises(ValueError):
        payment_link(job, 0)


def test_the_link_table_is_arrow_safe():
    job = job_at(QUOTE_SENT)
    payment_link(job, 13337)
    rows = link_rows(job)
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# The safety valve
# ---------------------------------------------------------------------------

def test_the_valve_only_fires_on_a_booked_job():
    for state in (NEW_ENQUIRY, QUOTE_SENT):
        result = trigger_safety_valve(job_at(state), WASTE_CONSTRUCTION)
        assert result.triggered is False
        assert result.outcome == VALVE_NO_CHANGE


def test_the_same_waste_as_quoted_changes_nothing():
    job = job_at(BOOKED)
    result = trigger_safety_valve(job, WASTE_GENERAL)
    assert result.triggered is False
    assert job.state == BOOKED


def test_waste_with_no_uplift_is_absorbed_rather_than_stopping_a_van():
    job = job_at(BOOKED, waste=WASTE_CONSTRUCTION)
    result = trigger_safety_valve(job, WASTE_GENERAL)
    assert result.triggered is False
    assert "costs more than the difference" in result.message
    assert job.state == BOOKED


def test_materially_different_waste_pauses_the_job_and_raises_a_surcharge():
    job = job_at(BOOKED)
    result = trigger_safety_valve(job, WASTE_CONSTRUCTION)
    assert result.triggered is True
    assert result.outcome == VALVE_PAUSED
    assert job.state == ON_HOLD
    assert result.surcharge is not None
    assert result.surcharge.kind == LINK_SURCHARGE


def test_the_surcharge_is_grossed_up_like_any_other_charge():
    job = job_at(BOOKED)
    result = trigger_safety_valve(job, WASTE_CONSTRUCTION)
    expected = build_quote(SURCHARGE_BANDS[WASTE_CONSTRUCTION])
    assert result.surcharge.amount_pence == expected.charge_pence
    assert result.surcharge.net_pence == expected.net_target_pence


@pytest.mark.parametrize("found", [WASTE_CONSTRUCTION, WASTE_HAZARDOUS,
                                   WASTE_GREEN])
def test_every_uplift_band_produces_its_own_surcharge(found):
    job = job_at(BOOKED)
    result = trigger_safety_valve(job, found)
    assert result.triggered is True
    assert result.surcharge.net_pence == build_quote(
        SURCHARGE_BANDS[found]).net_target_pence


def test_paying_the_surcharge_resumes_the_collection_and_records_the_money():
    job = job_at(BOOKED)
    trigger_safety_valve(job, WASTE_CONSTRUCTION)
    result = resolve_safety_valve(job, paid=True)
    assert result.outcome == VALVE_RESUMED
    assert job.state == BOOKED
    assert job.charged_pence == build_quote(
        SURCHARGE_BANDS[WASTE_CONSTRUCTION]).charge_pence


def test_declining_the_surcharge_cancels_rather_than_running_at_a_loss():
    job = job_at(BOOKED)
    trigger_safety_valve(job, WASTE_CONSTRUCTION)
    result = resolve_safety_valve(job, paid=False)
    assert result.outcome == VALVE_REFUSED
    assert job.state == CANCELLED
    assert job.charged_pence == 0


def test_a_resumed_job_can_still_be_completed():
    job = job_at(BOOKED)
    trigger_safety_valve(job, WASTE_CONSTRUCTION)
    resolve_safety_valve(job, paid=True)
    assert move(job, COMPLETED).ok is True


def test_resolving_when_nothing_is_paused_changes_nothing():
    job = job_at(BOOKED)
    result = resolve_safety_valve(job, paid=True)
    assert result.triggered is False
    assert job.state == BOOKED


# ---------------------------------------------------------------------------
# Prose discipline
# ---------------------------------------------------------------------------

def test_no_dash_characters_in_any_user_facing_prose():
    parts: list[str] = []
    for postcode in ("SE15 4RT", "EH1 1YZ", "nonsense", ""):
        parsed = parse_postcode(postcode)
        parts.append(parsed.message)
        parts.append(dispatch(postcode, SKIP_8, WASTE_HAZARDOUS).reason)
    for service in (SKIP_4, SKIP_12, GRAB):
        parts.append(dispatch("LS1 4DY", service, WASTE_GENERAL).reason)
    job = open_job(sample_enquiry())
    parts.append(move(job, COMPLETED).message)
    parts.append(move(job, QUOTE_SENT).message)
    booked = job_at(BOOKED)
    valve = trigger_safety_valve(booked, WASTE_HAZARDOUS)
    parts += [valve.message, valve.surcharge.description]
    parts.append(resolve_safety_valve(booked, paid=False).message)
    prose = "\n".join(parts)
    assert "—" not in prose
    assert "–" not in prose
