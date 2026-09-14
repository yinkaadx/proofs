"""Mobile Conversion and Release Engine.

Rendered inside the hub app. All logic lives in core.py, which has no Streamlit
dependency, so the same engine can sit behind the real webhook endpoint and the
real flag service.
"""

from __future__ import annotations

import json

import streamlit as st

from shared.theme import esc, inject
from tools.reventure_conversion_engine.core import (
    APP_NAME,
    APPLIED,
    Arm,
    BASELINE_CONTROL,
    BASELINE_TREATMENT,
    BillingLedger,
    COOLDOWN_DAYS,
    DOUBLE_BILL_BLOCKED,
    DUPLICATE,
    ENGINE_VERSION,
    FLAG_PRO_PAYWALL,
    FeatureFlag,
    HIGH_VALUE_TRIGGERS,
    MAX_PROMPTS_PER_YEAR,
    MIN_SESSIONS,
    MONTHLY_PRICE,
    MORE_DATA,
    RatingState,
    ROLL_BACK,
    SHIP,
    STALE,
    TOO_EARLY,
    TRIGGER_APP_LAUNCH,
    TRIGGER_DEAL_SAVED,
    TRIGGER_DEAL_SHARED,
    TRIGGER_ERROR,
    TRIGGER_ONBOARDING,
    TRIGGER_SETTINGS,
    blended_conversion,
    bucket_of,
    evaluate_test,
    exposure_split,
    metric_rows,
    projected_monthly_revenue,
    recommend,
    record_prompt,
    replay,
    sample_user_ids,
    sample_webhooks,
    should_prompt,
    variant_for,
)

STATE = "reventure_state"

VERDICT_TONE = {SHIP: "ok", ROLL_BACK: "crit", MORE_DATA: "warn",
                TOO_EARLY: "warn"}
OUTCOME_TONE = {APPLIED: "ok", DUPLICATE: "info", STALE: "warn",
                DOUBLE_BILL_BLOCKED: "crit"}

# The buttons on the rating tab, in the order a user meets them.
RATING_BUTTONS: tuple[tuple[str, str], ...] = (
    (TRIGGER_APP_LAUNCH, "App launch"),
    (TRIGGER_ONBOARDING, "Finish onboarding"),
    (TRIGGER_SETTINGS, "Open settings"),
    (TRIGGER_ERROR, "Hit an error screen"),
    (TRIGGER_DEAL_SAVED, "Save a deal that beat asking price"),
    (TRIGGER_DEAL_SHARED, "Share a deal with an agent"),
)

SAMPLE_USERS = 2000


def _state() -> dict:
    if STATE not in st.session_state:
        st.session_state[STATE] = {
            "rating": RatingState(),
            "day": 120,
            "log": [],
        }
    return st.session_state[STATE]


def _fire(trigger: str) -> None:
    """Run the gate for one trigger, and record the prompt only if it fired."""
    state = _state()
    decision = should_prompt(trigger, state["rating"], state["day"])
    state["log"].append(decision)
    if decision.allowed:
        state["rating"] = record_prompt(state["rating"], state["day"])
        # Time moves on, so the cooldown is visible on the next attempt.
        state["day"] += 1


def _reset_rating() -> None:
    state = _state()
    state["rating"] = RatingState()
    state["day"] = 120
    state["log"] = []


def render() -> None:
    inject()
    state = _state()

    st.markdown(
        f"""
<div class="app-hero">
  <h1>Mobile Conversion &amp; Release Engine</h1>
  <p>Three things decide whether a {APP_NAME} release makes money or costs it:
  the flag that decides who sees the paywall, the moment the app asks for a
  rating, and the pipeline that turns a payment into an entitlement exactly
  once. Each has a failure that is invisible in testing and expensive in
  production, so the statistics here are computed, the rating gates are
  enforced, and the webhook ledger is keyed the way a real handler has to
  be.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Feature Flag**: move the rollout and watch conversion and "
            "revenue move with it.\n"
            "2. **Rating Prompt**: press the buttons in order. Only a high "
            "value action gets through.\n"
            "3. **Stripe Pipeline**: step the webhooks and read the retry, "
            "the out of order event and the double billing block.\n"
            "4. **Recommendation**: the statistics turned into a decision."
        )
        st.divider()
        st.caption(
            "Nothing here touches a live system. No flag service, App Store "
            "or Stripe account is contacted and no charge is ever made."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_flag, tab_rating, tab_billing, tab_insight = st.tabs(
        ["Feature Flag", "Rating Prompt", "Stripe Pipeline", "Recommendation"]
    )

    # -----------------------------------------------------------------
    # Feature flag and A/B controller
    # -----------------------------------------------------------------
    with tab_flag:
        st.markdown("#### The Pro Paywall flag")
        st.caption(
            "Assignment is hashed from the user id, so a person keeps the same "
            "variant across sessions and devices. Re rolling per session would "
            "let the paywall appear and disappear for one user, which reads as "
            "a bug and poisons the measurement."
        )

        c1, c2, c3 = st.columns([1, 2, 1])
        enabled = c1.toggle("Flag enabled", value=True, key="rev_flag_enabled")
        rollout = c2.slider("Rollout percent", min_value=0, max_value=100,
                            value=50, step=5, key="rev_rollout")
        monthly_users = c3.number_input("Monthly active users", min_value=0,
                                        max_value=5_000_000, value=40_000,
                                        step=5_000, key="rev_mau")

        flag = FeatureFlag(
            key=FLAG_PRO_PAYWALL, label="Pro Paywall", enabled=enabled,
            rollout_percent=int(rollout),
            description="A harder paywall shown after the third saved deal.")

        c4, c5, c6 = st.columns(3)
        control_paid = c4.number_input("Control conversions", min_value=0,
                                       max_value=1000, value=61, step=1,
                                       key="rev_control_paid")
        treatment_paid = c5.number_input("Pro paywall conversions", min_value=0,
                                         max_value=1000, value=88, step=1,
                                         key="rev_treatment_paid")
        days_running = c6.number_input("Days running", min_value=1,
                                       max_value=90, value=14, step=1,
                                       key="rev_days")

        control = Arm("control", BASELINE_CONTROL.users, BASELINE_CONTROL.trials,
                      int(control_paid))
        treatment = Arm("treatment", BASELINE_TREATMENT.users,
                        BASELINE_TREATMENT.trials, int(treatment_paid))
        result = evaluate_test(control, treatment, int(days_running))

        blended = blended_conversion(result, flag)
        revenue = projected_monthly_revenue(result, flag, MONTHLY_PRICE,
                                            int(monthly_users))
        baseline_flag = FeatureFlag(FLAG_PRO_PAYWALL, "Pro Paywall", False, 0,
                                    "")
        baseline_revenue = projected_monthly_revenue(
            result, baseline_flag, MONTHLY_PRICE, int(monthly_users))
        delta = round(revenue - baseline_revenue, 2)
        tone = "ok" if delta > 0 else ("crit" if delta < 0 else "info")

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{blended:.2%}</div>
    <div class="l">Blended free to paid</div></div>
  <div class="app-kpi"><div class="n">{result.treatment.conversion_rate:.2%}</div>
    <div class="l">Inside the treatment</div></div>
  <div class="app-kpi"><div class="n">{revenue:,.0f}</div>
    <div class="l">Monthly revenue</div></div>
  <div class="app-kpi {tone}"><div class="n">{delta:+,.0f}</div>
    <div class="l">Against the flag off</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.caption(
            "The blended rate is what the whole audience actually sees at this "
            "rollout. Showing the treatment rate while the flag sits at ten "
            "percent would quietly claim nine times the result."
        )

        split = exposure_split(sample_user_ids(SAMPLE_USERS), flag)
        st.markdown(
            f"""
<div class="app-card {'ok' if enabled else 'info'}">
  <h4><span class="app-tag {'ok' if enabled else 'info'}">
  {esc('Live' if enabled else 'Off')}</span>{esc(flag.label)}</h4>
  <div class="app-ev">{esc(flag.key)} &nbsp; rollout
  {flag.rollout_percent}% &nbsp; {split['treatment']:,} of {SAMPLE_USERS:,}
  sampled users in treatment</div>
  <p>{esc(flag.description)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### Arm by arm")
        st.dataframe(metric_rows(result, MONTHLY_PRICE), width="stretch",
                     hide_index=True)

        st.markdown("##### Where a given user lands")
        st.dataframe(
            [{"User id": user_id,
              "Bucket": str(bucket_of(user_id, flag.key)),
              "Variant": variant_for(user_id, flag)}
             for user_id in sample_user_ids(8)],
            width="stretch", hide_index=True)
        st.caption(
            "The bucket is a hash of the user id and the flag key together, so "
            "it is the same on every device and two flags do not put the same "
            "people in treatment."
        )

    # -----------------------------------------------------------------
    # App Store rating optimizer
    # -----------------------------------------------------------------
    with tab_rating:
        rating = state["rating"]
        st.markdown("#### When the app may ask for a rating")
        st.caption(
            f"Apple allows {MAX_PROMPTS_PER_YEAR} prompts per user per 365 "
            f"days and silently shows nothing beyond that, which is worse than "
            f"refusing: the app believes it asked. So the prompt goes after a "
            f"good outcome, never on launch."
        )

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi"><div class="n">{rating.sessions}</div>
    <div class="l">Sessions</div></div>
  <div class="app-kpi"><div class="n">{rating.prompts_this_year}/{MAX_PROMPTS_PER_YEAR}</div>
    <div class="l">Prompts used this year</div></div>
  <div class="app-kpi"><div class="n">{state['day']}</div>
    <div class="l">Day in the year</div></div>
  <div class="app-kpi"><div class="n">{len(state['log'])}</div>
    <div class="l">Triggers fired</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### Fire a trigger")
        columns = st.columns(3)
        for index, (trigger, label) in enumerate(RATING_BUTTONS):
            high_value = trigger in HIGH_VALUE_TRIGGERS
            columns[index % 3].button(
                label, on_click=_fire, args=(trigger,),
                type="primary" if high_value else "secondary",
                key=f"rev_trigger_{index}")

        st.button("Reset this user", on_click=_reset_rating,
                  key="rev_reset_rating")

        if not state["log"]:
            st.info(
                "No trigger fired yet. Press App launch first: it is refused, "
                "which is the whole point."
            )
        else:
            last = state["log"][-1]
            tone = "ok" if last.allowed else "crit"
            st.markdown(
                f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(last.code)}</span>
  {esc(last.trigger)}</h4>
  <div class="app-ev">
  {esc('SKStoreReviewController.requestReview called' if last.allowed
       else 'requestReview not called')}</div>
  <p>{esc(last.reason)}</p>
</div>
""",
                unsafe_allow_html=True,
            )

            st.markdown("##### Every gate on that attempt")
            st.dataframe(last.gates, width="stretch", hide_index=True)

            st.markdown("##### What has been tried")
            st.dataframe(
                [{"#": index + 1, "Trigger": decision.trigger,
                  "Outcome": decision.code,
                  "Prompt shown": "Yes" if decision.allowed else "No"}
                 for index, decision in enumerate(state["log"])],
                width="stretch", hide_index=True)

        st.caption(
            f"A user needs {MIN_SESSIONS} sessions before the first ask, and "
            f"{COOLDOWN_DAYS} days between asks. Press a high value action "
            f"twice to watch the cooldown refuse the second one."
        )

    # -----------------------------------------------------------------
    # Stripe subscription pipeline
    # -----------------------------------------------------------------
    with tab_billing:
        st.markdown("#### Webhooks into entitlements")
        st.caption(
            "Stripe retries until the endpoint answers with a 2xx, and "
            "webhooks arrive out of order. The event id is the only thing "
            "standing between a retry and a second charge."
        )

        stream = sample_webhooks()
        delivered = st.slider("Webhooks delivered", min_value=1,
                              max_value=len(stream), value=len(stream),
                              key="rev_webhooks")
        ledger: BillingLedger = replay(stream[:delivered])

        st.markdown(
            f"""
<div class="app-kpis">
  <div class="app-kpi ok"><div class="n">{ledger.count(APPLIED)}</div>
    <div class="l">Applied</div></div>
  <div class="app-kpi"><div class="n">{ledger.count(DUPLICATE)}</div>
    <div class="l">Retries absorbed</div></div>
  <div class="app-kpi warn"><div class="n">{ledger.count(STALE)}</div>
    <div class="l">Out of order</div></div>
  <div class="app-kpi crit"><div class="n">{ledger.count(DOUBLE_BILL_BLOCKED)}</div>
    <div class="l">Double charges blocked</div></div>
</div>
""",
            unsafe_allow_html=True,
        )

        last_entry = ledger.entries[-1]
        tone = OUTCOME_TONE.get(last_entry.outcome, "info")
        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(last_entry.outcome)}</span>
  {esc(last_entry.event.type)}</h4>
  <div class="app-ev">{esc(last_entry.event.event_id)} &nbsp;
  {esc(last_entry.event.customer_id)} &nbsp;
  {esc(last_entry.event.source)}</div>
  <p>{esc(last_entry.note)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### The ledger")
        st.dataframe(ledger.entry_rows(), width="stretch", hide_index=True)

        st.markdown("##### Entitlements now")
        st.dataframe(ledger.entitlement_rows(), width="stretch",
                     hide_index=True)

        charged = ledger.charged_total() / 100
        st.markdown(
            f"""
<div class="app-card ok">
  <h4><span class="app-tag ok">Charged once</span>{charged:.2f} taken across
  {delivered} delivered webhook(s)</h4>
  <p>Every retry, every out of order event and the Apple purchase that also
  reached Stripe were all seen, and none of them took money a second time.</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### The event as Stripe sends it")
        st.code(json.dumps({
            "id": last_entry.event.event_id,
            "type": last_entry.event.type,
            "created": last_entry.event.created,
            "data": {"object": {
                "customer": last_entry.event.customer_id,
                "amount_paid": last_entry.event.amount_cents,
                "metadata": {
                    "source": last_entry.event.source,
                    "apple_original_transaction_id":
                        last_entry.event.apple_transaction_id or None,
                },
            }},
        }, indent=2), language="json")

    # -----------------------------------------------------------------
    # Recommendation
    # -----------------------------------------------------------------
    with tab_insight:
        recommendation = recommend(result, flag, MONTHLY_PRICE,
                                   int(monthly_users))
        tone = VERDICT_TONE.get(recommendation.verdict, "info")

        st.markdown("#### Ship or hold")
        st.caption(
            "Computed from the numbers rather than written about them. A real "
            "deployment hands these same figures to Claude for the write up, "
            "and doing the arithmetic here means the recommendation cannot "
            "contradict the table it is describing."
        )

        st.markdown(
            f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(recommendation.verdict)}</span>
  {esc(flag.label)} at {flag.rollout_percent}%</h4>
  <div class="app-ev">p {result.p_value:.4f} &nbsp; z
  {result.z_score:.2f} &nbsp; lift {result.lift:+.1%}</div>
  <p>{esc(recommendation.headline)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### How that was reached")
        for line in recommendation.reasoning:
            st.markdown(f"- {line}")

        st.markdown("##### Next step")
        st.markdown(
            f"""
<div class="app-card info">
  <p>{esc(recommendation.next_step)}</p>
</div>
""",
            unsafe_allow_html=True,
        )

        st.markdown("##### The statistics behind it")
        st.dataframe(
            [{"Quantity": "Two proportion z", "Value": f"{result.z_score:.4f}"},
             {"Quantity": "p value", "Value": f"{result.p_value:.6f}"},
             {"Quantity": "Absolute difference",
              "Value": f"{result.absolute_difference:+.4%}"},
             {"Quantity": "95 percent interval",
              "Value": f"{result.confidence_low:+.4%} to "
                       f"{result.confidence_high:+.4%}"},
             {"Quantity": f"Users per arm to detect a "
                          f"{result.minimum_detectable_effect:.0%} lift at 80 "
                          f"percent power",
              "Value": f"{result.required_per_arm:,}"},
             {"Quantity": "Smaller arm",
              "Value": f"{min(result.control.users, result.treatment.users):,}"}],
            width="stretch", hide_index=True)

    st.markdown(
        f"""
<div class="app-foot">
Mobile Conversion &amp; Release Engine, engine version {ENGINE_VERSION}. A
simulator: no flag service, App Store or Stripe account is contacted and no
charge is ever made. The recommendation is computed from the figures on screen
rather than written about them, so it cannot describe a result the table does
not show.
</div>
""",
        unsafe_allow_html=True,
    )
