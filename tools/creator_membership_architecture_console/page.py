"""Premium Creator Membership Console.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine could sit behind the real publication.

Everything is evaluated live from the controls, so no card on screen can
describe a tier that was changed two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.creator_membership_architecture_console.core import (
    ARTICLE_DECK,
    ARTICLE_TITLE,
    ENGINE_VERSION,
    MODAL_NONE,
    PRIVATE_EPISODES,
    TIER_LABEL,
    TIER_MEMBER,
    TIERS,
    WEBHOOK_EVENTS,
    feed_rows,
    feed_runtime,
    get_private_audio_feed,
    membership_summary,
    paywall_comparison,
    simulate_paywall_preview,
    simulate_stripe_membership_webhook,
    track_challenge_progress,
    webhook_ledger,
)

TIER_BY_LABEL = {TIER_LABEL[tier]: tier for tier in TIERS}


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>Premium Creator Membership Console</h1>
  <p>A paid publication is four decisions wearing one coat. Whether the
  paywall actually withholds anything, or merely hides it in the browser where
  View Source undoes the whole business. Whether a private feed can be traced
  when it leaks. Whether a member can see where they are. And whether a
  declined card takes their access away on the wrong day. Each one is on
  screen here with the mechanism showing.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    summary = membership_summary()

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Paywall**: change the tier and read the JSON, not the page.\n"
            "2. **Audio**: see which episodes a tier may actually play.\n"
            "3. **Challenge**: drag the days and watch the milestone.\n"
            "4. **Billing**: send a failed payment and see what it does not do."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. No Stripe endpoint is called, "
            "no feed is served and no member record exists."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_wall, tab_audio, tab_challenge, tab_billing = st.tabs(
        ["Editorial Paywall", "Private Audio", "Challenge", "Stripe Billing"])

    # -----------------------------------------------------------------
    # The paywall
    # -----------------------------------------------------------------
    with tab_wall:
        st.markdown("#### The gate is in the response, not in the stylesheet")
        st.caption(
            "The common way to build this is to send the whole article and "
            "hide the rest with CSS, or blur it under a modal. That is a "
            "decoration over a document anyone can read with View Source. "
            "Here the allowance is applied by slicing the source before it is "
            "serialised, so the withheld paragraphs have no path into the "
            "payload at all."
        )

        label = st.radio("Reading as", [TIER_LABEL[t] for t in TIERS],
                         index=1, horizontal=True, key="cma_tier")
        preview = simulate_paywall_preview(TIER_BY_LABEL[label])
        tone = "warn" if preview.gated else "ok"

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {tone}"><div class="n">{preview.visible_count}</div>
    <div class="l">Paragraphs sent</div></div>
  <div class="app-kpi {'crit' if preview.withheld_count else 'ok'}">
    <div class="n">{preview.withheld_count}</div>
    <div class="l">Never left the server</div></div>
  <div class="app-kpi"><div class="n">{esc(preview.modal)}</div>
    <div class="l">Modal triggered</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        body = "".join(f"<p>{esc(par)}</p>" for par in preview.visible)
        st.markdown(
            f"""
<div class="app-card">
  <h4>{esc(ARTICLE_TITLE)}</h4>
  <p><em>{esc(ARTICLE_DECK)}</em></p>
  {body}
</div>
""",
            unsafe_allow_html=True,
        )

        if preview.gated:
            st.markdown(
                f"""
<div class="app-card warn">
  <h4><span class="app-tag warn">{esc(preview.modal)}</span>
  {esc(preview.call_to_action)}</h4>
  <p>{esc(preview.reason)}</p>
  <div class="app-ev">{preview.withheld_count} paragraph(s) withheld at the
  server, so there is nothing further down this page to reveal.</div>
</div>
""",
                unsafe_allow_html=True,
            )
        else:
            st.success(preview.reason)

        st.markdown("##### The response the browser actually receives")
        st.code(preview.pretty_payload(), language="json")
        st.caption(
            "Read the JSON rather than the article. The withheld text is not "
            "in it, which is the claim a stylesheet based paywall could never "
            "make."
        )

        st.markdown("##### Every tier side by side")
        st.dataframe(paywall_comparison(), width="stretch", hide_index=True)
        st.markdown("##### Paragraph delivery for this tier")
        st.dataframe(preview.rows(), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Private audio
    # -----------------------------------------------------------------
    with tab_audio:
        st.markdown("#### A private feed, not a public one with a password")
        st.caption(
            "Every member gets their own URL, so a feed found in the wild "
            "identifies the account it came from and can be revoked without "
            "disturbing anybody else."
        )

        label = st.selectbox("Member tier", [TIER_LABEL[t] for t in TIERS],
                             index=2, key="cma_feed_tier")
        tier = TIER_BY_LABEL[label]
        feed = get_private_audio_feed(tier)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{len(feed['episodes'])}</div>
    <div class="l">Episodes playable</div></div>
  <div class="app-kpi {'warn' if feed['locked'] else 'ok'}">
    <div class="n">{len(feed['locked'])}</div>
    <div class="l">Locked to a higher tier</div></div>
  <div class="app-kpi"><div class="n">{esc(feed_runtime(tier))}</div>
    <div class="l">Runtime available</div></div>
  <div class="app-kpi ok"><div class="n">{len(PRIVATE_EPISODES)}</div>
    <div class="l">Episodes in the series</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### This member's feed URL")
        st.code(feed["feed_url"], language="text")
        st.caption(
            "The token is per member and revocable on its own, so losing one "
            "feed does not mean rotating everybody's."
        )

        for episode in PRIVATE_EPISODES:
            playable = episode in feed["episodes"]
            card = "ok" if playable else "warn"
            st.markdown(
                f"""
<div class="app-card {card}">
  <h4><span class="app-tag {card}">
  {esc('Playable' if playable else TIER_LABEL[episode.required_tier])}</span>
  {episode.number}. {esc(episode.title)}</h4>
  <p>{esc(episode.summary)}</p>
  <div class="app-ev">{esc(episode.duration)} &nbsp;
  {esc(', '.join(episode.topics))}</div>
</div>
""",
                unsafe_allow_html=True,
            )

        st.markdown("##### The feed")
        st.dataframe(feed_rows(tier), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Challenge progress
    # -----------------------------------------------------------------
    with tab_challenge:
        st.markdown("#### Where a member is, without ever reporting nonsense")
        st.caption(
            "The inputs come from a database rather than a form, so the two "
            "guards matter: a total of zero would divide by zero, and a "
            "completed count above the total would report progress over a "
            "hundred percent, which makes a member distrust everything else "
            "on the page."
        )

        c1, c2 = st.columns(2)
        total = c1.slider("Days in the challenge", min_value=0, max_value=90,
                          value=30, key="cma_total")
        done = c2.slider("Days completed", min_value=0, max_value=90, value=12,
                         key="cma_done")

        progress = track_challenge_progress(done, total)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {progress.tone}">
    <div class="n">{progress.percentage:.0f}%</div>
    <div class="l">Complete</div></div>
  <div class="app-kpi"><div class="n">{progress.completed_days}</div>
    <div class="l">Days done</div></div>
  <div class="app-kpi"><div class="n">{progress.days_remaining}</div>
    <div class="l">Days remaining</div></div>
  <div class="app-kpi {progress.tone}">
    <div class="n">{esc(progress.milestone)}</div>
    <div class="l">Milestone</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.progress(min(progress.percentage / 100, 1.0))
        if done > total:
            st.info(
                f"{done} completed against a total of {total} was clamped to "
                f"{progress.completed_days}. Progress never exceeds 100 "
                f"percent here, whatever the record says.")

        st.markdown("##### The numbers")
        st.dataframe(progress.rows(), width="stretch", hide_index=True)

    # -----------------------------------------------------------------
    # Stripe billing
    # -----------------------------------------------------------------
    with tab_billing:
        st.markdown("#### The failed payment is the one that matters")
        st.caption(
            "The obvious handler revokes access the moment a card is declined. "
            "Stripe retries on a dunning schedule, most declines clear on the "
            "retry, and a member locked out over a card that worked two days "
            "later cancels on principle."
        )

        event = st.selectbox("Webhook event", list(WEBHOOK_EVENTS), index=2,
                             key="cma_event")
        result = simulate_stripe_membership_webhook(event)

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi {result.tone}"><div class="n">
    {esc(result.ledger_state)}</div>
    <div class="l">Ledger state</div></div>
  <div class="app-kpi {'ok' if result.authorized else 'crit'}">
    <div class="n">{esc(result.access)}</div>
    <div class="l">Access</div></div>
  <div class="app-kpi {'warn' if result.dunning else 'ok'}">
    <div class="n">{'Yes' if result.dunning else 'No'}</div>
    <div class="l">In dunning</div></div>
  <div class="app-kpi crit"><div class="n">{summary['events_revoking']}</div>
    <div class="l">Events that revoke, of
    {summary['events_handled']}</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown(
            f"""
<div class="app-card {result.tone}">
  <h4><span class="app-tag {result.tone}">{esc(result.access)}</span>
  {esc(result.event_type)}</h4>
  <p>{esc(result.note)}</p>
  <div class="app-ev">Ledger {esc(result.ledger_state)}, tier after the event
  {esc(TIER_LABEL[result.tier])}</div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### This event in full")
        st.dataframe(result.rows(), width="stretch", hide_index=True)

        st.markdown("##### Every handled event")
        st.dataframe(webhook_ledger(), width="stretch", hide_index=True)
        st.caption(
            "Only two of the five take access away, and neither of them is "
            "the declined card."
        )

    st.markdown(
        f"""
<div class="app-foot">
Premium Creator Membership Console, engine version {ENGINE_VERSION}. A
simulator: no Stripe endpoint is called, no feed is served, no audio is hosted
and no member record exists. Every figure is computed from the controls on
screen, and nothing here reads the clock or a random source.
</div>
""",
        unsafe_allow_html=True,
    )
