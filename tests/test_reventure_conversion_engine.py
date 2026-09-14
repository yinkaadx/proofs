"""Engine tests for the Mobile Conversion and Release Engine.

Written for pytest. Deterministic throughout: bucketing is hashed from the user
id and nothing reads the clock or a random source.

Run: pytest tests/test_reventure_conversion_engine.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.reventure_conversion_engine.core import (  # noqa: E402
    ALLOWED,
    APPLE_TRANSACTION,
    APPLIED,
    BASELINE_CONTROL,
    BASELINE_TREATMENT,
    BLOCK_COOLDOWN,
    BLOCK_NOT_HIGH_VALUE,
    BLOCK_QUOTA,
    BLOCK_SAME_VERSION,
    BLOCK_TOO_NEW,
    CONTROL,
    COOLDOWN_DAYS,
    CUSTOMER_APPLE,
    CUSTOMER_WEB,
    DOUBLE_BILL_BLOCKED,
    DUPLICATE,
    ENTITLEMENT_ACTIVE,
    ENTITLEMENT_CANCELED,
    ENTITLEMENT_NONE,
    ENTITLEMENT_PAST_DUE,
    ENTITLEMENT_TRIALING,
    EVENT_CHECKOUT_COMPLETED,
    EVENT_INVOICE_FAILED,
    EVENT_INVOICE_PAID,
    EVENT_SUBSCRIPTION_CREATED,
    EVENT_SUBSCRIPTION_DELETED,
    EVENT_SUBSCRIPTION_UPDATED,
    FLAG_PRO_PAYWALL,
    HIGH_VALUE_TRIGGERS,
    HOLD,
    MAX_PROMPTS_PER_YEAR,
    MIN_DAYS,
    MIN_SESSIONS,
    MONTHLY_PRICE,
    MORE_DATA,
    ROLL_BACK,
    SHIP,
    SOURCE_APPLE,
    STALE,
    TOO_EARLY,
    TREATMENT,
    TRIGGER_APP_LAUNCH,
    TRIGGER_DEAL_SAVED,
    TRIGGER_DEAL_SHARED,
    TRIGGER_ERROR,
    TRIGGER_ONBOARDING,
    TRIGGER_SETTINGS,
    TRIGGERS,
    Arm,
    BillingLedger,
    FeatureFlag,
    RatingState,
    WebhookEvent,
    blended_conversion,
    bucket_of,
    evaluate_test,
    exposure_split,
    handle_event,
    metric_rows,
    norm_cdf,
    projected_monthly_revenue,
    recommend,
    record_prompt,
    replay,
    sample_user_ids,
    sample_webhooks,
    should_prompt,
    variant_for,
)


def flag(enabled: bool = True, rollout: int = 50) -> FeatureFlag:
    return FeatureFlag(FLAG_PRO_PAYWALL, "Pro Paywall", enabled, rollout,
                       "A harder paywall.")


def arms(control_paid: int = 61, treatment_paid: int = 88,
         users: int = 1000) -> tuple[Arm, Arm]:
    return (Arm(CONTROL, users, 214, control_paid),
            Arm(TREATMENT, users, 176, treatment_paid))


def event(event_id: str = "evt_1", type_: str = EVENT_INVOICE_PAID,
          customer: str = CUSTOMER_WEB, created: int = 100,
          **extra) -> WebhookEvent:
    return WebhookEvent(event_id, type_, customer, created, **extra)


# ---------------------------------------------------------------------------
# Feature flag bucketing
# ---------------------------------------------------------------------------

def test_a_user_keeps_the_same_bucket_on_every_call():
    """Re rolling per session would let the paywall appear and disappear for
    one person, which reads as a bug and poisons the measurement."""
    assert bucket_of("usr_000001", FLAG_PRO_PAYWALL) == \
        bucket_of("usr_000001", FLAG_PRO_PAYWALL)


def test_buckets_land_inside_the_hundred():
    for user_id in sample_user_ids(200):
        assert 0 <= bucket_of(user_id, FLAG_PRO_PAYWALL) < 100


def test_two_flags_do_not_put_the_same_people_in_treatment():
    one = {u for u in sample_user_ids(400)
           if bucket_of(u, "flag_a") < 50}
    two = {u for u in sample_user_ids(400)
           if bucket_of(u, "flag_b") < 50}
    assert one != two


def test_a_disabled_flag_puts_everyone_in_control():
    off = flag(enabled=False, rollout=100)
    split = exposure_split(sample_user_ids(500), off)
    assert split[TREATMENT] == 0
    assert split[CONTROL] == 500


def test_a_zero_rollout_exposes_nobody_and_a_full_one_exposes_everyone():
    assert exposure_split(sample_user_ids(500), flag(rollout=0))[TREATMENT] == 0
    assert exposure_split(sample_user_ids(500),
                          flag(rollout=100))[TREATMENT] == 500


def test_the_split_is_close_to_the_rollout_it_was_given():
    split = exposure_split(sample_user_ids(2000), flag(rollout=50))
    assert 900 <= split[TREATMENT] <= 1100


def test_raising_the_rollout_never_removes_anyone_from_treatment():
    """A user who has seen the new paywall must not be taken back off it."""
    users = sample_user_ids(500)
    smaller = {u for u in users if variant_for(u, flag(rollout=20)) == TREATMENT}
    larger = {u for u in users if variant_for(u, flag(rollout=60)) == TREATMENT}
    assert smaller <= larger


def test_a_rollout_outside_zero_to_a_hundred_is_refused():
    with pytest.raises(ValueError):
        FeatureFlag(FLAG_PRO_PAYWALL, "Pro Paywall", True, 140, "x")
    with pytest.raises(ValueError):
        FeatureFlag(FLAG_PRO_PAYWALL, "Pro Paywall", True, -1, "x")


def test_a_negative_user_count_is_refused():
    with pytest.raises(ValueError):
        sample_user_ids(-1)


# ---------------------------------------------------------------------------
# A/B metrics
# ---------------------------------------------------------------------------

def test_conversion_is_measured_against_everyone_exposed():
    """Measuring against trial starts would let a paywall that suppresses
    trials look like an improvement."""
    arm = Arm(CONTROL, users=1000, trials=200, paid=50)
    assert arm.conversion_rate == pytest.approx(0.05)
    assert arm.trial_rate == pytest.approx(0.2)


def test_an_empty_arm_reports_zero_rather_than_dividing_by_zero():
    arm = Arm(CONTROL, users=0, trials=0, paid=0)
    assert arm.conversion_rate == 0.0
    assert arm.trial_rate == 0.0


def test_the_normal_cdf_matches_its_known_values():
    assert norm_cdf(0) == pytest.approx(0.5)
    assert norm_cdf(1.959963984540054) == pytest.approx(0.975, abs=1e-6)
    assert norm_cdf(-1.959963984540054) == pytest.approx(0.025, abs=1e-6)


def test_the_z_test_matches_a_hand_computed_result():
    control, treatment = arms(61, 88)
    result = evaluate_test(control, treatment)
    # pooled p = 149/2000 = 0.0745, se = sqrt(.0745*.9255*(2/1000))
    assert result.z_score == pytest.approx(2.2992, abs=1e-3)
    assert result.p_value == pytest.approx(0.0215, abs=1e-3)
    assert result.significant is True


def test_identical_arms_produce_no_effect_and_no_significance():
    result = evaluate_test(Arm(CONTROL, 1000, 200, 70),
                           Arm(TREATMENT, 1000, 200, 70))
    assert result.z_score == pytest.approx(0.0)
    assert result.p_value == pytest.approx(1.0)
    assert result.significant is False
    assert result.absolute_difference == 0.0


def test_the_confidence_interval_brackets_the_difference():
    result = evaluate_test(*arms(61, 88))
    assert result.confidence_low < result.absolute_difference < \
        result.confidence_high


def test_a_significant_result_has_an_interval_clear_of_zero():
    result = evaluate_test(*arms(61, 88))
    assert result.interval_excludes_zero is True


def test_a_flat_result_has_an_interval_that_straddles_zero():
    result = evaluate_test(Arm(CONTROL, 1000, 200, 70),
                           Arm(TREATMENT, 1000, 200, 72))
    assert result.interval_excludes_zero is False


def test_a_larger_minimum_effect_needs_fewer_users_to_detect():
    """Sized from the effect declared in advance, not the one observed. Sizing
    against what you happened to measure makes a flat result look underpowered
    forever, so a test could never conclude that nothing happened."""
    fussy = evaluate_test(*arms(), minimum_detectable_effect=0.05)
    coarse = evaluate_test(*arms(), minimum_detectable_effect=0.40)
    assert fussy.required_per_arm > coarse.required_per_arm


def test_the_required_sample_does_not_move_with_the_result_observed():
    assert evaluate_test(*arms(61, 66)).required_per_arm == \
        evaluate_test(*arms(61, 130)).required_per_arm


def test_a_zero_minimum_effect_asks_for_no_sample_rather_than_dividing_by_zero():
    result = evaluate_test(*arms(), minimum_detectable_effect=0.0)
    assert result.required_per_arm == 0


def test_an_empty_test_is_handled_rather_than_crashing():
    result = evaluate_test(Arm(CONTROL, 0, 0, 0), Arm(TREATMENT, 0, 0, 0))
    assert result.p_value == 1.0
    assert result.significant is False


def test_the_lift_is_relative_to_control():
    result = evaluate_test(*arms(50, 100))
    assert result.lift == pytest.approx(1.0)


def test_the_metric_table_is_arrow_safe():
    rows = metric_rows(evaluate_test(*arms()))
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Rollout weighted reporting
# ---------------------------------------------------------------------------

def test_the_blended_rate_follows_the_rollout_rather_than_the_treatment():
    """A dashboard showing the treatment rate at a ten percent rollout is
    quietly claiming nine times the result."""
    result = evaluate_test(*arms(61, 88))
    assert blended_conversion(result, flag(rollout=0)) == \
        pytest.approx(result.control.conversion_rate)
    assert blended_conversion(result, flag(rollout=100)) == \
        pytest.approx(result.treatment.conversion_rate)
    assert blended_conversion(result, flag(rollout=50)) == \
        pytest.approx(0.0745)


def test_a_disabled_flag_blends_to_the_control_rate_whatever_the_rollout():
    result = evaluate_test(*arms())
    assert blended_conversion(result, flag(enabled=False, rollout=100)) == \
        pytest.approx(result.control.conversion_rate)


def test_projected_revenue_moves_with_the_rollout():
    result = evaluate_test(*arms(61, 88))
    off = projected_monthly_revenue(result, flag(rollout=0), MONTHLY_PRICE,
                                    40_000)
    full = projected_monthly_revenue(result, flag(rollout=100), MONTHLY_PRICE,
                                     40_000)
    assert full > off
    assert off == pytest.approx(0.061 * 40_000 * MONTHLY_PRICE, abs=1.0)


def test_a_negative_audience_is_refused():
    with pytest.raises(ValueError):
        projected_monthly_revenue(evaluate_test(*arms()), flag(), MONTHLY_PRICE,
                                  -5)


# ---------------------------------------------------------------------------
# App Store rating prompt
# ---------------------------------------------------------------------------

def test_the_prompt_is_blocked_on_app_launch():
    """The headline rule. Apple counts the attempt even when nothing is shown,
    so asking on launch spends an ask on somebody who has seen nothing."""
    decision = should_prompt(TRIGGER_APP_LAUNCH)
    assert decision.allowed is False
    assert decision.code == BLOCK_NOT_HIGH_VALUE


@pytest.mark.parametrize("trigger", [TRIGGER_APP_LAUNCH, TRIGGER_ONBOARDING,
                                     TRIGGER_SETTINGS, TRIGGER_ERROR])
def test_no_low_value_moment_is_ever_allowed(trigger):
    assert should_prompt(trigger).allowed is False


@pytest.mark.parametrize("trigger", HIGH_VALUE_TRIGGERS)
def test_a_high_value_action_is_allowed_on_a_healthy_user(trigger):
    decision = should_prompt(trigger, RatingState())
    assert decision.allowed is True
    assert decision.code == ALLOWED
    assert "requestReview" in decision.reason


@pytest.mark.parametrize("trigger", TRIGGERS)
def test_every_trigger_reaches_a_decision_with_a_reason_and_all_gates(trigger):
    decision = should_prompt(trigger)
    assert decision.reason
    assert len(decision.gates) == 5


def test_a_brand_new_user_is_too_new_to_ask():
    decision = should_prompt(TRIGGER_DEAL_SAVED,
                             RatingState(sessions=MIN_SESSIONS - 1))
    assert decision.allowed is False
    assert decision.code == BLOCK_TOO_NEW


def test_the_session_gate_turns_exactly_at_the_threshold():
    assert should_prompt(TRIGGER_DEAL_SAVED,
                         RatingState(sessions=MIN_SESSIONS)).allowed is True


def test_the_yearly_quota_stops_a_fourth_ask():
    decision = should_prompt(
        TRIGGER_DEAL_SAVED,
        RatingState(prompts_this_year=MAX_PROMPTS_PER_YEAR))
    assert decision.allowed is False
    assert decision.code == BLOCK_QUOTA
    assert "silently shows nothing" in decision.reason


def test_the_cooldown_blocks_a_second_ask_too_soon():
    state = RatingState(prompts_this_year=1, last_prompt_day=100)
    assert should_prompt(TRIGGER_DEAL_SAVED, state, day=140).code == \
        BLOCK_COOLDOWN


def test_the_cooldown_turns_exactly_where_it_should():
    state = RatingState(prompts_this_year=1, last_prompt_day=100)
    assert should_prompt(TRIGGER_DEAL_SAVED, state,
                         day=100 + COOLDOWN_DAYS - 1).allowed is False
    assert should_prompt(TRIGGER_DEAL_SAVED, state,
                         day=100 + COOLDOWN_DAYS).allowed is True


def test_asking_twice_on_one_version_is_blocked():
    state = RatingState(prompts_this_year=1, last_prompt_day=0,
                        asked_on_version=True)
    assert should_prompt(TRIGGER_DEAL_SAVED, state, day=500).code == \
        BLOCK_SAME_VERSION


def test_recording_a_prompt_spends_a_quota_and_starts_the_cooldown():
    state = record_prompt(RatingState(), day=120)
    assert state.prompts_this_year == 1
    assert state.last_prompt_day == 120
    assert state.asked_on_version is True


def test_a_second_high_value_action_is_refused_by_the_cooldown():
    """The sequence the app actually runs into."""
    state = RatingState()
    first = should_prompt(TRIGGER_DEAL_SAVED, state, day=120)
    assert first.allowed is True
    state = record_prompt(state, day=120)
    second = should_prompt(TRIGGER_DEAL_SHARED, state, day=121)
    assert second.allowed is False


def test_the_gate_table_is_arrow_safe():
    rows = should_prompt(TRIGGER_APP_LAUNCH).gates
    for column in {key for row in rows for key in row}:
        kinds = {type(row[column]).__name__ for row in rows}
        assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# Stripe webhook idempotency
# ---------------------------------------------------------------------------

def test_a_checkout_creates_a_trialing_entitlement():
    ledger = BillingLedger()
    entry = handle_event(ledger, event("evt_a", EVENT_CHECKOUT_COMPLETED))
    assert entry.outcome == APPLIED
    assert entry.status_before == ENTITLEMENT_NONE
    assert ledger.status_of(CUSTOMER_WEB) == ENTITLEMENT_TRIALING


def test_a_paid_invoice_activates_and_charges_once():
    ledger = BillingLedger()
    handle_event(ledger, event("evt_a", EVENT_CHECKOUT_COMPLETED, created=1))
    entry = handle_event(ledger, event("evt_b", EVENT_INVOICE_PAID, created=2))
    assert ledger.status_of(CUSTOMER_WEB) == ENTITLEMENT_ACTIVE
    assert entry.charged_cents == 1499
    assert ledger.charged_total() == 1499


def test_a_redelivered_event_changes_nothing_and_charges_nothing():
    """Stripe retries until it gets a 2xx. The event id is the only thing
    between a retry and a second charge."""
    ledger = BillingLedger()
    handle_event(ledger, event("evt_b", EVENT_INVOICE_PAID))
    again = handle_event(ledger, event("evt_b", EVENT_INVOICE_PAID))
    assert again.outcome == DUPLICATE
    assert again.charged_cents == 0
    assert ledger.charged_total() == 1499


def test_ten_redeliveries_still_charge_once():
    ledger = BillingLedger()
    for _ in range(10):
        handle_event(ledger, event("evt_b", EVENT_INVOICE_PAID))
    assert ledger.charged_total() == 1499
    assert ledger.count(DUPLICATE) == 9


def test_an_event_that_arrives_late_does_not_roll_the_entitlement_back():
    ledger = BillingLedger()
    handle_event(ledger, event("evt_a", EVENT_INVOICE_PAID, created=200))
    stale = handle_event(ledger, event("evt_old", EVENT_CHECKOUT_COMPLETED,
                                       created=100))
    assert stale.outcome == STALE
    assert ledger.status_of(CUSTOMER_WEB) == ENTITLEMENT_ACTIVE


def test_a_stale_event_is_remembered_so_its_own_retry_is_a_duplicate():
    ledger = BillingLedger()
    handle_event(ledger, event("evt_a", EVENT_INVOICE_PAID, created=200))
    handle_event(ledger, event("evt_old", EVENT_CHECKOUT_COMPLETED, created=100))
    again = handle_event(ledger, event("evt_old", EVENT_CHECKOUT_COMPLETED,
                                       created=100))
    assert again.outcome == DUPLICATE


def test_a_failed_invoice_moves_to_past_due_and_takes_no_money():
    ledger = BillingLedger()
    handle_event(ledger, event("evt_a", EVENT_CHECKOUT_COMPLETED, created=1))
    entry = handle_event(ledger, event("evt_f", EVENT_INVOICE_FAILED,
                                       created=2))
    assert ledger.status_of(CUSTOMER_WEB) == ENTITLEMENT_PAST_DUE
    assert entry.charged_cents == 0


def test_a_deleted_subscription_cancels_the_entitlement():
    ledger = BillingLedger()
    handle_event(ledger, event("evt_a", EVENT_SUBSCRIPTION_CREATED, created=1))
    handle_event(ledger, event("evt_d", EVENT_SUBSCRIPTION_DELETED, created=9))
    assert ledger.status_of(CUSTOMER_WEB) == ENTITLEMENT_CANCELED


def test_an_apple_purchase_links_its_transaction_to_the_customer():
    ledger = BillingLedger()
    handle_event(ledger, event("evt_ios", EVENT_SUBSCRIPTION_CREATED,
                               customer=CUSTOMER_APPLE, created=1,
                               source=SOURCE_APPLE,
                               apple_transaction_id=APPLE_TRANSACTION))
    assert ledger.apple_links[APPLE_TRANSACTION] == CUSTOMER_APPLE
    assert ledger.entitlements[CUSTOMER_APPLE]["apple_linked"] is True


def test_the_same_apple_purchase_reaching_stripe_is_not_charged_again():
    """The one that costs real money: Apple and Stripe do not know about each
    other, so the same subscription arrives twice."""
    ledger = BillingLedger()
    handle_event(ledger, event("evt_ios", EVENT_SUBSCRIPTION_CREATED,
                               customer=CUSTOMER_APPLE, created=1,
                               source=SOURCE_APPLE,
                               apple_transaction_id=APPLE_TRANSACTION))
    entry = handle_event(ledger, event("evt_web", EVENT_INVOICE_PAID,
                                       customer=CUSTOMER_APPLE, created=2,
                                       apple_transaction_id=APPLE_TRANSACTION))
    assert entry.outcome == DOUBLE_BILL_BLOCKED
    assert entry.charged_cents == 0
    assert ledger.charged_total() == 0


def test_an_apple_transaction_claimed_by_a_second_customer_is_refused():
    ledger = BillingLedger()
    handle_event(ledger, event("evt_ios", EVENT_SUBSCRIPTION_CREATED,
                               customer=CUSTOMER_APPLE, created=1,
                               apple_transaction_id=APPLE_TRANSACTION))
    entry = handle_event(ledger, event("evt_other", EVENT_INVOICE_PAID,
                                       customer=CUSTOMER_WEB, created=2,
                                       apple_transaction_id=APPLE_TRANSACTION))
    assert entry.outcome == DOUBLE_BILL_BLOCKED
    assert CUSTOMER_APPLE in entry.note


def test_two_customers_are_billed_independently():
    ledger = BillingLedger()
    handle_event(ledger, event("evt_1", EVENT_INVOICE_PAID,
                               customer=CUSTOMER_WEB, created=1))
    handle_event(ledger, event("evt_2", EVENT_INVOICE_PAID,
                               customer="cus_other", created=1))
    assert ledger.charged_total() == 2998


def test_the_sample_stream_exercises_all_three_hard_cases():
    ledger = replay(sample_webhooks())
    assert ledger.count(DUPLICATE) == 1
    assert ledger.count(STALE) == 1
    assert ledger.count(DOUBLE_BILL_BLOCKED) == 1


def test_the_sample_stream_charges_exactly_once():
    ledger = replay(sample_webhooks())
    assert ledger.charged_total() == 1499


def test_replaying_the_whole_stream_twice_charges_nothing_extra():
    once = replay(sample_webhooks())
    twice = replay(sample_webhooks(), replay(sample_webhooks()))
    assert twice.charged_total() == once.charged_total()


def test_the_ledger_tables_are_arrow_safe():
    ledger = replay(sample_webhooks())
    for rows in (ledger.entry_rows(), ledger.entitlement_rows()):
        for column in {key for row in rows for key in row}:
            kinds = {type(row[column]).__name__ for row in rows}
            assert len(kinds) == 1, f"{column!r} mixes {sorted(kinds)}"


# ---------------------------------------------------------------------------
# The recommendation
# ---------------------------------------------------------------------------

def test_a_significant_win_says_ship_with_the_rollout_step():
    result = evaluate_test(*arms(61, 88))
    recommendation = recommend(result, flag())
    assert recommendation.verdict == SHIP
    assert recommendation.confident is True
    assert "50 percent" in recommendation.next_step


def test_a_significant_loss_says_roll_back():
    result = evaluate_test(*arms(88, 61))
    recommendation = recommend(result, flag())
    assert recommendation.verdict == ROLL_BACK
    assert "0 percent" in recommendation.next_step


def test_a_test_read_before_a_week_is_refused_whatever_the_numbers():
    """Weekday and weekend buyers differ, so an early read is reading the
    calendar."""
    result = evaluate_test(*arms(61, 88), days_running=3)
    assert recommend(result, flag()).verdict == TOO_EARLY
    assert recommend(result, flag()).confident is False


def test_the_day_gate_turns_exactly_at_the_minimum():
    assert recommend(evaluate_test(*arms(61, 88), days_running=MIN_DAYS - 1),
                     flag()).verdict == TOO_EARLY
    assert recommend(evaluate_test(*arms(61, 88), days_running=MIN_DAYS),
                     flag()).verdict != TOO_EARLY


def test_a_small_underpowered_difference_asks_for_more_data():
    result = evaluate_test(*arms(61, 66))
    recommendation = recommend(result, flag())
    assert recommendation.verdict == MORE_DATA
    assert recommendation.confident is False
    assert "more users per arm" in recommendation.next_step


def test_a_flat_result_on_large_arms_says_hold():
    result = evaluate_test(Arm(CONTROL, 200_000, 40_000, 14_000),
                           Arm(TREATMENT, 200_000, 40_000, 14_010))
    recommendation = recommend(result, flag())
    assert recommendation.verdict == HOLD
    assert recommendation.confident is True


def test_a_win_on_arms_smaller_than_planned_warns_the_lift_is_flattering():
    """Significance on a small sample is still a result, and the measured size
    of it tends to be the high end of the range."""
    result = evaluate_test(*arms(61, 88))
    assert result.powered is False
    joined = " ".join(recommend(result, flag()).reasoning)
    assert "flattering" in joined


def test_the_reasoning_quotes_the_numbers_it_was_given():
    result = evaluate_test(*arms(61, 88))
    joined = " ".join(recommend(result, flag()).reasoning)
    assert "61 of 1000" in joined
    assert "88 of 1000" in joined
    assert f"{result.p_value:.4f}" in joined


def test_the_revenue_delta_follows_the_direction_of_the_result():
    assert recommend(evaluate_test(*arms(61, 88)),
                     flag()).monthly_revenue_delta > 0
    assert recommend(evaluate_test(*arms(88, 61)),
                     flag()).monthly_revenue_delta < 0


def test_every_verdict_carries_a_headline_a_reasoning_and_a_next_step():
    for control_paid, treatment_paid, days in ((61, 88, 14), (88, 61, 14),
                                               (61, 66, 14), (61, 88, 2)):
        recommendation = recommend(
            evaluate_test(*arms(control_paid, treatment_paid), days_running=days),
            flag())
        assert recommendation.headline
        assert len(recommendation.reasoning) >= 5
        assert recommendation.next_step


# ---------------------------------------------------------------------------
# Prose discipline
# ---------------------------------------------------------------------------

def test_no_dash_characters_in_any_user_facing_prose():
    parts: list[str] = []
    for trigger in TRIGGERS:
        decision = should_prompt(trigger)
        parts.append(decision.reason)
        parts += [str(value) for row in decision.gates for value in row.values()]
    for entry in replay(sample_webhooks()).entries:
        parts.append(entry.note)
    for control_paid, treatment_paid, days in ((61, 88, 14), (88, 61, 14),
                                               (61, 66, 14), (61, 88, 2)):
        recommendation = recommend(
            evaluate_test(*arms(control_paid, treatment_paid), days_running=days),
            flag())
        parts += [recommendation.headline, recommendation.next_step]
        parts += recommendation.reasoning
    prose = "\n".join(parts)
    assert "—" not in prose
    assert "–" not in prose
