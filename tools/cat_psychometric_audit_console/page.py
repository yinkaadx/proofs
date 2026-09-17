"""CAT Psychometric and IRT Audit Console.

Rendered inside the hub app. Every figure on this page is computed from the
controls on each run, so nothing on screen can describe a bank or a student
that was replaced two interactions ago.
"""

from __future__ import annotations

import streamlit as st

from shared.theme import esc, inject
from tools.cat_psychometric_audit_console.core import (
    D_SCALING,
    ENGINE_VERSION,
    EXPOSURE_CAP,
    INFORMATION_PER_ITEM,
    MODEL_1PL,
    MODEL_2PL,
    MODEL_3PL,
    MODELS,
    RISK_CERTAIN,
    RISK_HIGH,
    RISK_LOW,
    RISK_MODERATE,
    calculate_information_bounds,
    item_information,
    max_item_information,
    simulate_bank_exhaustion,
    simulate_eap_shrinkage,
    theta_of_max_information,
)

RISK_TONE = {RISK_LOW: "ok", RISK_MODERATE: "warn", RISK_HIGH: "crit",
             RISK_CERTAIN: "crit"}


def _kpis(items: list[tuple[str, str]]) -> None:
    cells = "".join(
        f'<div class="app-kpi"><b>{esc(value)}</b><span>{esc(label)}</span></div>'
        for label, value in items)
    st.markdown(f'<div class="app-kpis">{cells}</div>', unsafe_allow_html=True)


def _verdict(tone: str, tag: str, title: str, body: str, fix: str) -> None:
    st.markdown(
        f"""
<div class="app-card {tone}">
  <h4><span class="app-tag {tone}">{esc(tag)}</span> {esc(title)}</h4>
  <p>{esc(body)}</p>
  <div class="app-ev">{esc(fix)}</div>
</div>
""",
        unsafe_allow_html=True,
    )


def _theta_grid(low: float = -4.0, high: float = 4.0, steps: int = 81):
    span = (high - low) / (steps - 1)
    return [low + span * index for index in range(steps)]


def render() -> None:
    inject()

    st.markdown(
        """
<div class="app-hero">
  <h1>CAT Psychometric and IRT Audit Console</h1>
  <p>Three claims an adaptive test makes, each with arithmetic behind it that
  settles the argument before a pilot does. A reported standard error is a
  claim about Fisher information, and information has a ceiling. An EAP score
  is a compromise between the student and the grade prior, so the students
  furthest from the middle are the ones it misreports. And a bank that looks
  ample by count is smaller than it reads, because maximum information
  selection reaches for the same few items every time.</p>
</div>
""",
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.subheader("How to use")
        st.markdown(
            "1. **Information**: set the target standard error below what the "
            "item limit can carry and read the shortfall.\n"
            "2. **Shrinkage**: move the student far from the prior mean and "
            "watch the bias grow with the distance.\n"
            "3. **Bank**: turn both exposure controls off and find the band "
            "size where the selector runs out."
        )
        st.divider()
        st.caption(
            "Sections one and two are closed form results and the test suite "
            "verifies them against a brute force search rather than trusting "
            "the formula as typed. Section three is a model and it lists its "
            "own assumptions on screen."
        )
        st.caption(
            f"Logistic scaling constant D is {D_SCALING}, so a theta of 1.0 "
            "is one population standard deviation."
        )
        st.caption(f"Engine version {ENGINE_VERSION}")

    tab_info, tab_eap, tab_bank = st.tabs(
        ["Information Bounds", "EAP Shrinkage", "Bank Exhaustion"])

    # -----------------------------------------------------------------
    with tab_info:
        st.subheader("Can this test reach the standard error it reports")
        st.caption(
            "Test information is additive, so the ceiling is every item "
            "carrying its own maximum at the point being estimated. That is "
            "optimistic on purpose: a live adaptive test does worse, so a "
            "target out of reach here is out of reach in the field."
        )

        left, right = st.columns(2)
        with left:
            model = st.selectbox("Model", list(MODELS),
                                 index=list(MODELS).index(MODEL_2PL))
            a_param = st.slider("Discrimination a", 0.20, 2.50, 1.00, 0.05,
                                disabled=(model == MODEL_1PL))
            if model == MODEL_1PL:
                st.caption("The 1PL fixes discrimination at 1.0 by definition.")
        with right:
            max_items = st.slider("Maximum items in the test", 5, 80, 30, 1)
            target_se = st.slider("Target standard error", 0.10, 0.50, 0.20, 0.01)
            c_param = st.slider("Guessing c", 0.00, 0.35, 0.20, 0.01,
                                disabled=(model != MODEL_3PL))
            if model != MODEL_3PL:
                st.caption("Guessing applies to the 3PL only.")

        bounds = calculate_information_bounds(
            model, a_param, int(max_items), float(target_se),
            c_param=c_param if model == MODEL_3PL else 0.0)

        _kpis([
            ("Information per item", f"{bounds.max_item_information:.4f}"),
            ("Test information ceiling", f"{bounds.max_test_information:.2f}"),
            ("Standard error floor", f"{bounds.min_standard_error:.4f}"),
            ("Items the target needs", str(bounds.items_needed_for_target)),
        ])
        _verdict(
            "ok" if bounds.target_reachable else "crit",
            "REACHABLE" if bounds.target_reachable else "OUT OF REACH",
            f"{bounds.model_type} at a of {bounds.a_param:g}",
            bounds.headline + ".", bounds.fix)
        if bounds.shortfall_items:
            st.caption(
                f"The shortfall is {bounds.shortfall_items} items. No scoring "
                f"change closes it, because the ceiling is set by the item "
                f"parameters and the item limit and by nothing else."
            )

        st.subheader("Where that information sits on the scale")
        effective_c = c_param if model == MODEL_3PL else 0.0
        peak = theta_of_max_information(model, bounds.a_param, 0.0, effective_c)
        curve = {
            "Information at this ability": [
                item_information(theta, bounds.a_param, 0.0, effective_c)
                for theta in _theta_grid()
            ]
        }
        st.line_chart(curve, height=220)
        st.caption(
            f"Ability runs from minus 4 to plus 4 across the chart, with the "
            f"item difficulty at zero. The peak sits at {peak:+.3f}, "
            f"carrying {bounds.max_item_information:.4f}. For the 1PL and 2PL "
            f"the peak is at the difficulty itself. For the 3PL guessing "
            f"pushes it up the scale and lowers it, because a guesser gets "
            f"easy items right without telling you anything."
        )

        st.subheader("The same question across the three models")
        for candidate in MODELS:
            per_item = max_item_information(
                candidate, bounds.a_param,
                c_param if candidate == MODEL_3PL else 0.0)
            reachable = calculate_information_bounds(
                candidate, bounds.a_param, int(max_items), float(target_se),
                c_param=c_param if candidate == MODEL_3PL else 0.0)
            st.markdown(
                f"- **{candidate}** carries {per_item:.4f} per item, floors at "
                f"a standard error of {reachable.min_standard_error:.4f}, and "
                f"needs {reachable.items_needed_for_target} items for the "
                f"{target_se:.2f} target")

    # -----------------------------------------------------------------
    with tab_eap:
        st.subheader("What the grade prior does to the score")
        st.caption(
            "The posterior mean is the precision weighted average of the "
            "response pattern and the prior. The estimate is pulled toward "
            "the prior mean by one minus the data weight, so the bias is "
            "exactly that fraction of the distance between the student and "
            "the prior mean. A student on the mean is unbiased. A student far "
            "from it is not, and no amount of correct scoring code changes it."
        )

        left, right = st.columns(2)
        with left:
            true_theta = st.slider("True ability of the student", -3.0, 3.0,
                                   2.00, 0.05)
            test_length = st.slider("Items administered", 5, 60, 20, 1)
        with right:
            prior_mean = st.slider("Grade prior mean", -2.0, 2.0, 0.00, 0.05)
            prior_sd = st.slider("Grade prior standard deviation", 0.30, 2.00,
                                 1.00, 0.05)

        estimate = simulate_eap_shrinkage(true_theta, prior_mean, prior_sd,
                                          int(test_length))
        _kpis([
            ("Reported ability", f"{estimate.estimated_theta:+.3f}"),
            ("Shrinkage bias", f"{estimate.shrinkage_bias:+.3f}"),
            ("Data weight", f"{estimate.data_weight * 100:.1f} percent"),
            ("Interval",
             f"{estimate.ci_low:+.2f} to {estimate.ci_high:+.2f}"),
        ])
        tone = ("ok" if estimate.shrinkage_bias_magnitude < 0.10 else
                "warn" if estimate.shrinkage_bias_magnitude < 0.30 else "crit")
        _verdict(tone, f"{estimate.shrinkage_bias_magnitude:.3f} logits",
                 f"{int(test_length)} items against a prior of "
                 f"{prior_mean:+.2f} plus or minus {prior_sd:g}",
                 estimate.headline + ".", estimate.fix)
        for note in estimate.findings:
            st.markdown(
                f'<div class="app-card info"><p>{esc(note)}</p></div>',
                unsafe_allow_html=True)

        st.subheader("Bias across the whole ability range")
        abilities = [-3.0 + 0.25 * step for step in range(25)]
        st.line_chart(
            {
                "Reported ability": [
                    simulate_eap_shrinkage(theta, prior_mean, prior_sd,
                                           int(test_length)).estimated_theta
                    for theta in abilities
                ],
                "True ability": abilities,
            },
            height=240,
        )
        st.caption(
            "Ability runs from minus 3 to plus 3 across the chart. The two "
            "lines meet at the prior mean and separate everywhere else, and "
            "the gap between them is the bias. That fan shape is the whole "
            "audit finding: the students the test is least accurate about are "
            "the ones a placement decision is most likely to be made on."
        )

        st.subheader("How test length moves the data weight")
        for length in (10, 20, 30, 45, 60):
            row = simulate_eap_shrinkage(true_theta, prior_mean, prior_sd, length)
            st.markdown(
                f"- **{length} items** carry information "
                f"{row.test_information:.2f}, giving "
                f"{row.data_weight * 100:.1f} percent data weight and a bias "
                f"of {row.shrinkage_bias:+.3f}")
        st.caption(
            f"Each item contributes {INFORMATION_PER_ITEM:.4f} at the "
            "discrimination of 1.0 this simulator assumes for a well "
            "targeted adaptive item."
        )

    # -----------------------------------------------------------------
    with tab_bank:
        st.subheader("Will the bank hold")
        st.caption(
            "Maximum information selection reaches for the same few items in "
            "a band, so the usable pool is a fraction of the inventory count. "
            "This is a model rather than a closed form result, and it lists "
            "its own assumptions below so a reviewer can disagree with a line "
            "rather than with the verdict."
        )

        left, right = st.columns(2)
        with left:
            items_per_band = st.slider("Items in the band", 20, 600, 200, 10)
            test_length_bank = st.slider("Items per sitting", 5, 60, 30, 1)
        with right:
            sittings = st.slider("Sittings per examinee per year", 1, 6, 3, 1)
            stratification = st.toggle("a stratification", value=False)
            sympson_hetter = st.toggle("Sympson Hetter exposure control",
                                       value=False)

        forecast = simulate_bank_exhaustion(
            int(items_per_band), int(test_length_bank), int(sittings),
            bool(stratification), bool(sympson_hetter))

        _kpis([
            ("Usable pool",
             f"{forecast.effective_pool:.0f} of {forecast.items_per_band}"),
            ("Exposure per item",
             f"{forecast.exposure_rate * 100:.0f} percent"),
            ("Repeat exposure by final sitting",
             f"{forecast.duplicate_exposure_rate * 100:.0f} percent"),
            ("Chance of running out",
             f"{forecast.crash_probability * 100:.0f} percent"),
        ])
        _verdict(RISK_TONE.get(forecast.exhaustion_risk, "warn"),
                 forecast.exhaustion_risk,
                 f"{forecast.items_per_band} items in the band",
                 forecast.headline + ".", forecast.fix)
        if forecast.overexposed:
            st.caption(
                f"Exposure of {forecast.exposure_rate * 100:.0f} percent is "
                f"above the {EXPOSURE_CAP * 100:.0f} percent cap most "
                f"programmes write into their specification, which is the "
                f"line at which an item is treated as compromised."
            )

        st.subheader("What each control is worth here")
        for strat, sh, label in ((False, False, "Neither control"),
                                 (True, False, "a stratification only"),
                                 (False, True, "Sympson Hetter only"),
                                 (True, True, "Both controls")):
            row = simulate_bank_exhaustion(
                int(items_per_band), int(test_length_bank), int(sittings),
                strat, sh)
            st.markdown(
                f"- **{label}**: {row.effective_pool:.0f} usable items, "
                f"{row.exposure_rate * 100:.0f} percent exposure, risk "
                f"{row.exhaustion_risk}")

        st.subheader("The assumptions this forecast rests on")
        for line in forecast.assumptions:
            st.markdown(f"- {line}")
