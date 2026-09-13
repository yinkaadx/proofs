"""Page tests for the Mobile Conversion and Release Engine, via AppTest.

Written for pytest.

Run: pytest tests/test_reventure_conversion_engine_page.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.reventure_conversion_engine.core import (  # noqa: E402
    APPLE_TRANSACTION,
    BLOCK_COOLDOWN,
    BLOCK_NOT_HIGH_VALUE,
    DOUBLE_BILL_BLOCKED,
    MAX_PROMPTS_PER_YEAR,
    MORE_DATA,
    ROLL_BACK,
    SHIP,
    TOO_EARLY,
    sample_webhooks,
)
from tools.reventure_conversion_engine.page import STATE  # noqa: E402

HARNESS = (Path(__file__).resolve().parent
           / "_page_harness_reventure_conversion_engine.py")

LAUNCH = "App launch"
DEAL = "Save a deal that beat asking price"
SHARE = "Share a deal with an agent"
SETTINGS = "Open settings"


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


def state_of(at: AppTest) -> dict:
    return at.session_state[STATE]


# ---------------------------------------------------------------------------
# Cold start
# ---------------------------------------------------------------------------

def test_the_page_renders_with_no_exception():
    at = run_app()
    assert not at.exception, [str(e.value) for e in at.exception]


def test_the_hero_and_the_four_tabs_are_present():
    at = run_app()
    assert "Mobile Conversion &amp; Release Engine" in text_of(at)
    labels = [tab.label for tab in at.tabs]
    for expected in ("Feature Flag", "Rating Prompt", "Stripe Pipeline",
                     "Recommendation"):
        assert expected in labels


def test_no_two_widgets_of_a_kind_share_a_label():
    at = run_app()
    for kind in ("button", "slider", "number_input", "toggle", "selectbox"):
        labels = [el.label for el in getattr(at, kind)]
        assert len(labels) == len(set(labels)), f"{kind}: {labels}"


def test_it_says_plainly_that_no_charge_is_ever_made():
    assert "no charge is ever made" in text_of(run_app())


# ---------------------------------------------------------------------------
# Feature flag controller
# ---------------------------------------------------------------------------

def test_the_flag_opens_live_at_half_rollout():
    body = text_of(run_app())
    assert "Live" in body
    assert "pro_paywall" in body
    assert "Blended free to paid" in body


def test_moving_the_rollout_moves_the_blended_conversion():
    at = run_app()
    widget(at, "slider", "Rollout percent").set_value(0).run()
    at_zero = text_of(at)
    widget(at, "slider", "Rollout percent").set_value(100).run()
    at_full = text_of(at)
    assert "6.10%" in at_zero
    assert "8.80%" in at_full


def test_turning_the_flag_off_returns_the_blend_to_control():
    at = run_app()
    widget(at, "toggle", "Flag enabled").set_value(False).run()
    body = text_of(at)
    assert "Off" in body
    assert "6.10%" in body


def test_the_exposure_split_follows_the_rollout():
    at = run_app()
    widget(at, "slider", "Rollout percent").set_value(0).run()
    assert "0 of 2,000" in text_of(at)


def test_changing_the_conversions_changes_the_statistics_on_screen():
    at = run_app()
    before = text_of(at)
    widget(at, "number_input", "Pro paywall conversions").set_value(61).run()
    after = text_of(at)
    assert before != after
    assert "lift +0.0%" in after
    assert "p 1.0000" in after


def test_the_bucket_table_shows_where_a_user_lands():
    body = text_of(run_app())
    assert "usr_000000" in body
    assert "Bucket" in body


# ---------------------------------------------------------------------------
# Rating prompt
# ---------------------------------------------------------------------------

def test_nothing_has_been_tried_before_the_first_press():
    at = run_app()
    assert state_of(at)["log"] == []
    assert "Press App launch first" in text_of(at)


def test_app_launch_is_refused():
    """The headline rule of this tab."""
    at = press(run_app(), LAUNCH)
    body = text_of(at)
    assert BLOCK_NOT_HIGH_VALUE in body
    assert "requestReview not called" in body
    assert state_of(at)["rating"].prompts_this_year == 0


@pytest.mark.parametrize("label", [LAUNCH, "Finish onboarding", SETTINGS,
                                   "Hit an error screen"])
def test_no_low_value_button_ever_shows_the_prompt(label):
    at = press(run_app(), label)
    assert state_of(at)["log"][-1].allowed is False
    assert state_of(at)["rating"].prompts_this_year == 0


def test_a_high_value_action_shows_the_prompt_and_spends_an_ask():
    at = press(run_app(), DEAL)
    body = text_of(at)
    assert "requestReview called" in body
    assert state_of(at)["rating"].prompts_this_year == 1


def test_a_second_high_value_action_is_refused_by_the_cooldown():
    at = press(run_app(), DEAL)
    at = press(at, SHARE)
    body = text_of(at)
    assert BLOCK_COOLDOWN in body
    assert state_of(at)["rating"].prompts_this_year == 1


def test_the_gate_table_shows_every_gate_on_the_last_attempt():
    at = press(run_app(), LAUNCH)
    body = text_of(at)
    assert "High value action" in body
    assert "Prompts used this year" in body
    assert "Days since the last prompt" in body


def test_the_attempt_log_keeps_every_press_in_order():
    at = press(run_app(), LAUNCH)
    at = press(at, SETTINGS)
    at = press(at, DEAL)
    log = state_of(at)["log"]
    assert [decision.allowed for decision in log] == [False, False, True]


def test_resetting_puts_the_user_back_to_a_clean_state():
    at = press(run_app(), DEAL)
    assert state_of(at)["rating"].prompts_this_year == 1
    at = press(at, "Reset this user")
    assert state_of(at)["rating"].prompts_this_year == 0
    assert state_of(at)["log"] == []


def test_the_yearly_quota_is_stated_on_screen():
    assert f"/{MAX_PROMPTS_PER_YEAR}" in text_of(run_app())


# ---------------------------------------------------------------------------
# Stripe pipeline
# ---------------------------------------------------------------------------

def test_the_pipeline_shows_all_three_hard_cases():
    body = text_of(run_app())
    assert "Retries absorbed" in body
    assert "Out of order" in body
    assert "Double charges blocked" in body


def test_the_whole_stream_charges_exactly_once():
    body = text_of(run_app())
    assert "14.99 taken across" in body


def test_stepping_back_shows_the_state_before_the_duplicate():
    at = run_app()
    widget(at, "slider", "Webhooks delivered").set_value(3).run()
    body = text_of(at)
    assert "Applied" in body
    assert "Duplicate ignored" not in body


def test_the_duplicate_step_explains_why_the_event_id_matters():
    at = run_app()
    widget(at, "slider", "Webhooks delivered").set_value(4).run()
    body = text_of(at)
    assert "Duplicate ignored" in body
    assert "retries until the endpoint" in body


def test_the_apple_purchase_reaching_stripe_is_blocked_on_screen():
    at = run_app()
    widget(at, "slider", "Webhooks delivered").set_value(len(sample_webhooks())).run()
    body = text_of(at)
    assert DOUBLE_BILL_BLOCKED in body
    assert APPLE_TRANSACTION in body


def test_the_entitlement_table_is_on_screen():
    body = text_of(run_app())
    assert "Entitlement" in body
    assert "cus_Q1web8823" in body


def test_the_raw_event_is_shown_in_the_shape_stripe_sends_it():
    body = text_of(run_app())
    assert '"amount_paid"' in body
    assert '"apple_original_transaction_id"' in body


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------

def test_the_default_numbers_recommend_shipping():
    body = text_of(run_app())
    assert SHIP in body
    assert "Ship or hold" in body


def test_a_losing_result_recommends_rolling_back():
    at = run_app()
    widget(at, "number_input", "Pro paywall conversions").set_value(20).run()
    body = text_of(at)
    assert ROLL_BACK in body
    assert "0 percent" in body


def test_a_small_difference_asks_for_more_data():
    at = run_app()
    widget(at, "number_input", "Pro paywall conversions").set_value(66).run()
    body = text_of(at)
    assert MORE_DATA in body
    assert "more users per arm" in body


def test_reading_the_test_too_early_is_refused_whatever_the_numbers():
    at = run_app()
    widget(at, "number_input", "Days running").set_value(3).run()
    body = text_of(at)
    assert TOO_EARLY in body
    assert "reading the calendar" in body


def test_the_recommendation_shows_the_statistics_it_used():
    body = text_of(run_app())
    assert "Two proportion z" in body
    assert "95 percent interval" in body
    assert "80 percent power" in body


def test_the_page_says_the_recommendation_is_computed_not_written():
    body = text_of(run_app())
    assert "cannot contradict the table" in body


def test_no_dash_characters_reach_the_screen():
    at = press(run_app(), DEAL)
    body = text_of(at)
    assert "—" not in body
    assert "–" not in body
