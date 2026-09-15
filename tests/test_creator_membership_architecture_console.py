"""Engine tests for the Premium Creator Membership Console.

Written for pytest. Deterministic: nothing in the engine reads the clock or a
random source, so a preview renders identically in a test and in the console.

The most important tests here are the ones that assert an absence. A paywall
is only a paywall if the withheld text is genuinely not in the response, and
that is a claim you have to check rather than assume.

Run: pytest tests/test_creator_membership_architecture_console.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.creator_membership_architecture_console.core import (  # noqa: E402
    ACCESS_GRANTED,
    ACCESS_RETAINED,
    ACCESS_REVOKED,
    ARTICLE_BODY,
    ENGINE_VERSION,
    EVENT_INVOICE_PAID,
    EVENT_PAYMENT_FAILED,
    EVENT_REFUNDED,
    EVENT_SUBSCRIPTION_CREATED,
    EVENT_SUBSCRIPTION_DELETED,
    LEDGER_ACTIVE,
    LEDGER_CANCELED,
    LEDGER_PAST_DUE,
    MILESTONE_COMPLETE,
    MILESTONE_HALFWAY,
    MILESTONE_HOME_STRAIGHT,
    MILESTONE_NOT_STARTED,
    MILESTONE_QUARTER,
    MILESTONE_UNDER_WAY,
    MODAL_NONE,
    MODAL_SIGN_UP,
    MODAL_UPGRADE,
    PRIVATE_EPISODES,
    TIER_ANONYMOUS,
    TIER_FOUNDING,
    TIER_FREE,
    TIER_MEMBER,
    TIERS,
    WEBHOOK_EVENTS,
    feed_rows,
    feed_token,
    get_private_audio_feed,
    has_access,
    membership_summary,
    paywall_comparison,
    simulate_paywall_preview,
    simulate_stripe_membership_webhook,
    tier_level,
    track_challenge_progress,
    webhook_ledger,
)


# ---------------------------------------------------------------------------
# The paywall: what is actually sent
# ---------------------------------------------------------------------------

def test_a_member_receives_the_whole_article():
    preview = simulate_paywall_preview(TIER_MEMBER)
    assert preview.visible == list(ARTICLE_BODY)
    assert preview.withheld_count == 0
    assert preview.gated is False
    assert preview.modal == MODAL_NONE


def test_a_founding_member_gets_everything_a_member_gets():
    assert simulate_paywall_preview(TIER_FOUNDING).visible == \
        simulate_paywall_preview(TIER_MEMBER).visible


def test_an_anonymous_reader_gets_one_paragraph_and_a_sign_up_modal():
    preview = simulate_paywall_preview(TIER_ANONYMOUS)
    assert preview.visible_count == 1
    assert preview.gated is True
    assert preview.modal == MODAL_SIGN_UP


def test_a_free_account_gets_a_real_excerpt_and_an_upgrade_modal():
    preview = simulate_paywall_preview(TIER_FREE)
    assert preview.visible_count == 2
    assert preview.modal == MODAL_UPGRADE
    assert preview.visible_count > \
        simulate_paywall_preview(TIER_ANONYMOUS).visible_count


@pytest.mark.parametrize("tier", [TIER_ANONYMOUS, TIER_FREE])
def test_the_withheld_text_is_not_anywhere_in_the_response(tier):
    """The whole point. A CSS paywall could never pass this test, because the
    text it hides is still in the document it sent."""
    preview = simulate_paywall_preview(tier)
    serialised = json.dumps(preview.payload)
    withheld = ARTICLE_BODY[preview.visible_count:]
    assert withheld, "this test is meaningless if nothing was withheld"
    for paragraph in withheld:
        assert paragraph not in serialised
        # Not even a fragment of it.
        assert paragraph[:40] not in serialised


def test_the_payload_reports_how_much_was_withheld_without_including_it():
    preview = simulate_paywall_preview(TIER_FREE)
    payload = preview.payload
    assert payload["withheld_paragraph_count"] == 3
    assert len(payload["paragraphs"]) == 2
    assert payload["gated"] is True


def test_the_payload_serialises_as_json():
    preview = simulate_paywall_preview(TIER_FREE)
    assert json.loads(preview.pretty_payload()) == preview.payload


def test_an_unknown_tier_is_treated_as_the_least_privileged():
    """Failing open on a paywall gives the archive away."""
    preview = simulate_paywall_preview("lifetime_vip")
    assert preview.tier == TIER_ANONYMOUS
    assert preview.visible_count == 1
    assert preview.gated is True


@pytest.mark.parametrize("tier", TIERS)
def test_no_tier_ever_receives_more_than_the_article_holds(tier):
    preview = simulate_paywall_preview(tier)
    assert preview.visible_count <= len(ARTICLE_BODY)
    assert preview.visible_count + preview.withheld_count == len(ARTICLE_BODY)


def test_access_increases_monotonically_with_tier():
    counts = [simulate_paywall_preview(tier).visible_count for tier in TIERS]
    assert counts == sorted(counts)


def test_a_gated_preview_always_offers_a_way_through():
    for tier in (TIER_ANONYMOUS, TIER_FREE):
        preview = simulate_paywall_preview(tier)
        assert preview.call_to_action
        assert preview.modal != MODAL_NONE


def test_the_comparison_table_is_arrow_safe():
    for row in paywall_comparison() + simulate_paywall_preview(TIER_FREE).rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# Tier entitlement
# ---------------------------------------------------------------------------

def test_entitlement_is_a_comparison_not_a_list_of_cases():
    assert has_access(TIER_FOUNDING, TIER_MEMBER)
    assert has_access(TIER_MEMBER, TIER_MEMBER)
    assert not has_access(TIER_FREE, TIER_MEMBER)
    assert not has_access(TIER_ANONYMOUS, TIER_FREE)


def test_an_unknown_tier_has_no_entitlement_at_all():
    assert tier_level("lifetime_vip") == -1
    assert not has_access("lifetime_vip", TIER_ANONYMOUS)


# ---------------------------------------------------------------------------
# The private audio feed
# ---------------------------------------------------------------------------

def test_a_member_gets_the_member_episodes_and_not_the_founding_ones():
    feed = get_private_audio_feed(TIER_MEMBER)
    assert len(feed["episodes"]) == 3
    assert len(feed["locked"]) == 2
    assert all(ep.required_tier == TIER_FOUNDING for ep in feed["locked"])


def test_a_founding_member_gets_every_episode():
    feed = get_private_audio_feed(TIER_FOUNDING)
    assert len(feed["episodes"]) == len(PRIVATE_EPISODES)
    assert feed["locked"] == []


def test_a_free_account_gets_no_private_episodes_at_all():
    assert get_private_audio_feed(TIER_FREE)["episodes"] == []


def test_every_episode_carries_a_duration_and_topic_tags():
    for episode in PRIVATE_EPISODES:
        assert episode.duration_seconds > 0
        assert episode.topics
        assert ":" in episode.duration
        assert episode.summary


def test_the_duration_is_formatted_as_minutes_and_padded_seconds():
    episode = PRIVATE_EPISODES[0]
    minutes, seconds = divmod(episode.duration_seconds, 60)
    assert episode.duration == f"{minutes}:{seconds:02d}"


def test_the_feed_url_is_per_member_so_a_leak_identifies_the_account():
    first = get_private_audio_feed(TIER_MEMBER, "mem_1")["feed_url"]
    second = get_private_audio_feed(TIER_MEMBER, "mem_2")["feed_url"]
    assert first != second
    assert feed_token("mem_1") in first
    assert feed_token("mem_1") not in second


def test_the_feed_token_is_stable_for_a_member():
    assert feed_token("mem_1") == feed_token("mem_1")
    assert len(feed_token("mem_1")) == 20


def test_the_feed_url_never_contains_the_raw_member_id():
    """Otherwise the URL is guessable and the token buys nothing."""
    assert "mem_8241" not in get_private_audio_feed(TIER_MEMBER,
                                                    "mem_8241")["feed_url"]


def test_the_runtime_only_counts_what_the_tier_can_play():
    member = get_private_audio_feed(TIER_MEMBER)["total_seconds"]
    founding = get_private_audio_feed(TIER_FOUNDING)["total_seconds"]
    assert member < founding


def test_the_feed_rows_list_every_episode_and_are_arrow_safe():
    rows = feed_rows(TIER_MEMBER)
    assert len(rows) == len(PRIVATE_EPISODES)
    assert {row["Playable"] for row in rows} == {"yes", "no"}
    for row in rows:
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# Challenge progress
# ---------------------------------------------------------------------------

def test_a_normal_challenge_reports_the_right_percentage():
    progress = track_challenge_progress(12, 30)
    assert progress.percentage == 40.0
    assert progress.days_remaining == 18
    assert progress.milestone == MILESTONE_QUARTER


@pytest.mark.parametrize("done,total,expected", [
    (0, 30, MILESTONE_NOT_STARTED),
    (1, 30, MILESTONE_UNDER_WAY),
    (8, 30, MILESTONE_QUARTER),
    (15, 30, MILESTONE_HALFWAY),
    (25, 30, MILESTONE_HOME_STRAIGHT),
    (30, 30, MILESTONE_COMPLETE),
])
def test_each_milestone_band_is_reached_at_the_right_point(done, total, expected):
    assert track_challenge_progress(done, total).milestone == expected


def test_a_total_of_zero_does_not_divide_by_zero():
    progress = track_challenge_progress(5, 0)
    assert progress.percentage == 0.0
    assert progress.milestone == MILESTONE_NOT_STARTED
    assert progress.total_days == 0


def test_more_days_completed_than_exist_is_clamped_not_reported():
    """Progress over a hundred percent makes a member distrust everything
    else on the page."""
    progress = track_challenge_progress(45, 30)
    assert progress.percentage == 100.0
    assert progress.completed_days == 30
    assert progress.days_remaining == 0
    assert progress.complete


def test_negative_input_is_floored_at_zero():
    progress = track_challenge_progress(-5, 30)
    assert progress.completed_days == 0
    assert progress.percentage == 0.0


def test_the_percentage_is_never_outside_nought_to_a_hundred():
    for done in range(-3, 40):
        for total in (0, 1, 7, 30):
            percentage = track_challenge_progress(done, total).percentage
            assert 0.0 <= percentage <= 100.0, (done, total, percentage)


def test_only_a_finished_challenge_reports_complete():
    assert not track_challenge_progress(29, 30).complete
    assert track_challenge_progress(30, 30).complete


def test_the_progress_rows_are_arrow_safe():
    for row in track_challenge_progress(12, 30).rows():
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# The Stripe webhook
# ---------------------------------------------------------------------------

def test_a_new_subscription_grants_access_immediately():
    """A member staring at a paywall they have just paid to pass is the worst
    first minute a publication can offer."""
    result = simulate_stripe_membership_webhook(EVENT_SUBSCRIPTION_CREATED)
    assert result.ledger_state == LEDGER_ACTIVE
    assert result.access == ACCESS_GRANTED
    assert result.authorized


def test_a_paid_invoice_keeps_access_and_clears_the_ledger():
    result = simulate_stripe_membership_webhook(EVENT_INVOICE_PAID)
    assert result.ledger_state == LEDGER_ACTIVE
    assert result.access == ACCESS_RETAINED
    assert result.dunning is False


def test_a_failed_payment_does_not_revoke_access():
    """The single most important rule here. Stripe retries, most declines
    clear, and a member locked out over a card that worked two days later
    cancels on principle."""
    result = simulate_stripe_membership_webhook(EVENT_PAYMENT_FAILED)
    assert result.ledger_state == LEDGER_PAST_DUE
    assert result.access == ACCESS_RETAINED
    assert result.authorized is True
    assert result.dunning is True


def test_a_failed_payment_still_changes_the_ledger():
    """Keeping access is not the same as pretending nothing happened."""
    paid = simulate_stripe_membership_webhook(EVENT_INVOICE_PAID)
    failed = simulate_stripe_membership_webhook(EVENT_PAYMENT_FAILED)
    assert failed.ledger_state != paid.ledger_state
    assert failed.dunning and not paid.dunning


def test_only_cancellation_and_refund_revoke_access():
    revoking = [event for event in WEBHOOK_EVENTS
                if simulate_stripe_membership_webhook(event).access
                == ACCESS_REVOKED]
    assert revoking == [EVENT_SUBSCRIPTION_DELETED, EVENT_REFUNDED]


def test_a_cancelled_member_drops_to_free_rather_than_being_deleted():
    result = simulate_stripe_membership_webhook(EVENT_SUBSCRIPTION_DELETED)
    assert result.ledger_state == LEDGER_CANCELED
    assert result.tier == TIER_FREE
    assert not result.authorized


def test_a_refund_removes_the_access_the_charge_paid_for():
    result = simulate_stripe_membership_webhook(EVENT_REFUNDED)
    assert result.access == ACCESS_REVOKED
    assert result.tier == TIER_FREE


def test_an_unhandled_event_is_refused_rather_than_silently_ignored():
    """A webhook handler that returns 200 for events it does not understand is
    how a cancellation goes unprocessed for a month."""
    with pytest.raises(ValueError):
        simulate_stripe_membership_webhook("customer.subscription.trial_will_end")


@pytest.mark.parametrize("event", WEBHOOK_EVENTS)
def test_every_handled_event_returns_a_complete_result(event):
    result = simulate_stripe_membership_webhook(event)
    assert result.ledger_state and result.access and result.note
    assert result.tier in TIERS
    for row in result.rows():
        assert all(isinstance(v, str) for v in row.values()), row


def test_webhook_results_are_deterministic():
    assert simulate_stripe_membership_webhook(EVENT_PAYMENT_FAILED) == \
        simulate_stripe_membership_webhook(EVENT_PAYMENT_FAILED)


def test_the_ledger_table_covers_every_handled_event_and_is_arrow_safe():
    rows = webhook_ledger()
    assert len(rows) == len(WEBHOOK_EVENTS)
    for row in rows:
        assert all(isinstance(v, str) for v in row.values()), row


# ---------------------------------------------------------------------------
# The dashboard, and house rules
# ---------------------------------------------------------------------------

def test_the_summary_agrees_with_the_parts_it_counts():
    summary = membership_summary()
    assert summary["article_paragraphs"] == len(ARTICLE_BODY)
    assert summary["free_sees"] == simulate_paywall_preview(TIER_FREE).visible_count
    assert summary["episodes_total"] == len(PRIVATE_EPISODES)
    assert summary["episodes_for_founding"] > summary["episodes_for_member"]
    assert summary["events_revoking"] == 2


def test_the_engine_is_versioned():
    assert ENGINE_VERSION == "1.0.0"


def test_the_core_imports_no_streamlit():
    source = (Path(__file__).resolve().parents[1] / "tools"
              / "creator_membership_architecture_console" / "core.py").read_text()
    assert "streamlit" not in source


def test_no_dash_characters_reach_the_screen():
    text = " ".join(
        list(ARTICLE_BODY)
        + [ep.summary + ep.title for ep in PRIVATE_EPISODES]
        + [simulate_stripe_membership_webhook(e).note for e in WEBHOOK_EVENTS]
        + [simulate_paywall_preview(t).reason for t in TIERS])
    assert "—" not in text
    assert "–" not in text
