"""CAT Psychometric and IRT Audit Console: the engine.

Three claims a computer adaptive test makes, and what each one costs when it
is not checked.

1. "The test reports a standard error of 0.20." That is a claim about Fisher
   information, and information has a ceiling set by the item parameters in
   the bank. A target standard error either is reachable inside the item
   limit or it is not, and the arithmetic settles it before the pilot does.
2. "The score is the student's ability." An EAP estimate with a grade level
   prior is a compromise between the response pattern and the prior, weighted
   by how much information the test carried. The compromise is the point of
   the method and it is also a systematic bias away from the prior mean, so
   the strongest and weakest students are the ones it misreports.
3. "The bank is large enough." Maximum information selection reaches for the
   same few items every time, so the usable pool is a fraction of the stated
   one, and a bank that looks ample by count can run dry inside a band.

Sections one and two are closed form results and the tests verify them
numerically against brute force rather than trusting the formula as typed.
Section three is a model with its assumptions written down, not a theorem,
and it says so.

No Streamlit import lives here on purpose, so the same engine could sit behind
a real scoring service without a line changing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

# The logistic scaling constant that puts the logistic model within 0.01 of
# the normal ogive. Reporting in this metric is what makes a theta of 1.0
# mean one population standard deviation.
D_SCALING = 1.702

MODEL_1PL = "1PL"
MODEL_2PL = "2PL"
MODEL_3PL = "3PL"
MODELS: tuple[str, ...] = (MODEL_1PL, MODEL_2PL, MODEL_3PL)

# A 1PL item has no discrimination parameter to vary: it is fixed at one.
RASCH_A = 1.0

# The exposure rate above which an item is treated as overexposed. 0.20 is
# the cap most operational programmes write into their specification.
EXPOSURE_CAP = 0.20


# ---------------------------------------------------------------------------
# 1. Fisher information and the standard error floor
# ---------------------------------------------------------------------------

def probability_correct(theta: float, a: float, b: float = 0.0,
                        c: float = 0.0) -> float:
    """The three parameter logistic response function."""
    return c + (1 - c) / (1 + math.exp(-D_SCALING * a * (theta - b)))


def item_information(theta: float, a: float, b: float = 0.0,
                     c: float = 0.0) -> float:
    """Fisher information a single item carries at one point on the scale.

    For the 3PL this is a squared times (P minus c) squared over P, times
    (1 minus P) over (1 minus c) squared. Setting c to zero collapses it to
    the 2PL form, a squared times P times one minus P, which is the same
    expression rather than a second one maintained beside it.
    """
    p = probability_correct(theta, a, b, c)
    if p <= 0 or p >= 1:
        return 0.0
    numerator = (p - c) ** 2 * (1 - p)
    denominator = p * (1 - c) ** 2
    return (D_SCALING ** 2) * (a ** 2) * numerator / denominator


def max_item_information(model_type: str, a: float, c: float = 0.0) -> float:
    """The most information one item of this kind can carry at any point.

    For the 1PL and the 2PL the maximum sits where the probability of a
    correct response is one half, giving D squared times a squared over four.
    For the 3PL guessing moves the maximum up the scale and lowers it, and
    the closed form is Lord's:

        D squared a squared over eight times one minus c squared, times
        one minus twenty c minus eight c squared plus one plus eight c
        raised to three halves.

    Setting c to zero returns the 2PL value, which is the first thing the
    tests check, and then they check the whole curve against a grid search.
    """
    a_value = RASCH_A if model_type == MODEL_1PL else float(a)
    if a_value <= 0:
        raise ValueError("discrimination must be positive")
    guess = 0.0 if model_type != MODEL_3PL else float(c)
    if not 0 <= guess < 1:
        raise ValueError("the guessing parameter must be at least zero and below one")
    if guess == 0:
        return (D_SCALING ** 2) * (a_value ** 2) / 4
    bracket = 1 - 20 * guess - 8 * guess ** 2 + (1 + 8 * guess) ** 1.5
    return (D_SCALING ** 2) * (a_value ** 2) * bracket / (8 * (1 - guess) ** 2)


def theta_of_max_information(model_type: str, a: float, b: float = 0.0,
                             c: float = 0.0) -> float:
    """Where on the scale that maximum sits.

    At the difficulty itself for the 1PL and 2PL. Above it for the 3PL,
    because a guesser gets easy items right without telling you anything.
    """
    a_value = RASCH_A if model_type == MODEL_1PL else float(a)
    guess = 0.0 if model_type != MODEL_3PL else float(c)
    if guess == 0:
        return float(b)
    return b + (1 / (D_SCALING * a_value)) * math.log(
        (1 + math.sqrt(1 + 8 * guess)) / 2)


@dataclass(frozen=True)
class InformationBounds:
    model_type: str
    a_param: float
    c_param: float
    max_items: int
    target_se: float
    max_item_information: float
    max_test_information: float
    min_standard_error: float
    items_needed_for_target: int
    target_reachable: bool
    headline: str
    fix: str

    @property
    def shortfall_items(self) -> int:
        """How many more items the target needs than the limit allows."""
        return max(0, self.items_needed_for_target - self.max_items)


def calculate_information_bounds(model_type: str, a_param: float,
                                 max_items: int, target_se: float,
                                 c_param: float = 0.0) -> InformationBounds:
    """The ceiling on test information and the floor on the standard error.

    Test information is additive, so the best a test of this length can do is
    every item carrying its own maximum at the point being estimated. That is
    optimistic by construction, which is what makes it a bound: a real
    adaptive test does worse, and if the target is out of reach even here it
    is out of reach in the field.

    The standard error is one over the square root of test information, so the
    item count a target demands is one over the target squared times the
    information a single item carries.
    """
    model = str(model_type or "").strip().upper()
    if model not in MODELS:
        raise ValueError(f"unknown model: {model_type!r}")
    if int(max_items) <= 0:
        raise ValueError("a test needs at least one item")
    if float(target_se) <= 0:
        raise ValueError("a target standard error must be above zero")

    per_item = max_item_information(model, a_param, c_param)
    items = int(max_items)
    target = float(target_se)
    max_info = per_item * items
    min_se = 1 / math.sqrt(max_info)
    needed = math.ceil(1 / (target ** 2 * per_item))
    reachable = min_se <= target

    if reachable:
        headline = (f"A {items} item {model} test at a of {a_param:g} can "
                    f"reach a standard error of {min_se:.3f}, inside the "
                    f"{target:.3f} target, and needs {needed} items to do it")
        fix = (f"The bound is optimistic: it assumes every item is perfectly "
               f"targeted. Build in headroom above {needed} items, because a "
               f"live bank never hands the selector its best item at every "
               f"step.")
    else:
        headline = (f"A {items} item {model} test at a of {a_param:g} floors "
                    f"out at a standard error of {min_se:.3f}, above the "
                    f"{target:.3f} target, which needs {needed} items")
        fix = (f"Three levers and no fourth. Raise the item limit to at least "
               f"{needed}, raise the discrimination of the bank, or move the "
               f"target. A test that reports {target:.3f} from {items} items "
               f"is reporting a number the information does not support.")

    return InformationBounds(
        model_type=model, a_param=float(a_param), c_param=float(c_param),
        max_items=items, target_se=target, max_item_information=per_item,
        max_test_information=max_info, min_standard_error=min_se,
        items_needed_for_target=needed, target_reachable=reachable,
        headline=headline, fix=fix,
    )


# ---------------------------------------------------------------------------
# 2. EAP shrinkage toward the prior
# ---------------------------------------------------------------------------

# A well targeted item in an adaptive test, at the default discrimination of
# one in the normal metric. Used to turn a test length into an information
# figure, so the shrinkage simulator takes a length rather than a bank.
INFORMATION_PER_ITEM = max_item_information(MODEL_2PL, 1.0)


@dataclass(frozen=True)
class EapEstimate:
    true_theta: float
    grade_prior_mean: float
    grade_prior_sd: float
    test_length: int
    test_information: float
    data_weight: float
    estimated_theta: float
    shrinkage_bias: float
    shrinkage_bias_magnitude: float
    posterior_sd: float
    ci_low: float
    ci_high: float
    true_theta_inside_ci: bool
    headline: str
    fix: str
    findings: tuple[str, ...] = field(default_factory=tuple)


def simulate_eap_shrinkage(true_theta: float, grade_prior_mean: float,
                           grade_prior_sd: float,
                           test_length: int) -> EapEstimate:
    """What an EAP estimate reports for a student whose ability is known.

    With a normal prior and a likelihood treated as normal around the true
    ability, the posterior mean is the precision weighted average of the two:
    test information times the ability, plus one over the prior variance
    times the prior mean, all over the sum of those two precisions.

    The consequence is the whole point of the audit. The estimate is pulled
    toward the prior mean by one minus the data weight, so the bias is exactly
    that fraction of the distance between the student and the prior. A student
    sitting on the prior mean is unbiased. A student two standard deviations
    above it is misreported by the same fraction of that whole distance, and
    no amount of correct scoring code changes that.
    """
    theta = float(true_theta)
    prior_mean = float(grade_prior_mean)
    prior_sd = float(grade_prior_sd)
    length = int(test_length)
    if prior_sd <= 0:
        raise ValueError("a prior standard deviation must be above zero")
    if length <= 0:
        raise ValueError("a test needs at least one item")

    info = INFORMATION_PER_ITEM * length
    prior_precision = 1 / (prior_sd ** 2)
    posterior_precision = info + prior_precision
    weight = info / posterior_precision
    estimate = (info * theta + prior_precision * prior_mean) / posterior_precision
    bias = estimate - theta
    posterior_sd = 1 / math.sqrt(posterior_precision)
    ci_low = estimate - 1.96 * posterior_sd
    ci_high = estimate + 1.96 * posterior_sd

    findings: list[str] = []
    distance = abs(theta - prior_mean)
    if distance >= 1.5 and abs(bias) >= 0.20:
        findings.append(
            f"This student sits {distance:.2f} logits from the grade prior "
            f"mean, and the estimate is pulled back {abs(bias):.3f} of that. "
            f"The further a student is from the middle of their grade, the "
            f"more the prior decides their score, which is the opposite of "
            f"what an adaptive test is sold on.")
    if weight < 0.80:
        findings.append(
            f"The prior carries {(1 - weight) * 100:.0f} percent of this "
            f"estimate. Below eighty percent data weight the report is a "
            f"statement about the grade as much as about the student.")
    if not (ci_low <= theta <= ci_high):
        findings.append(
            "The true ability falls outside the reported interval. The "
            "interval is a posterior credible interval and it is honest "
            "about the posterior, so a true value outside it is the "
            "shrinkage showing rather than a coverage failure.")

    headline = (f"A student at {theta:+.2f} is reported at {estimate:+.2f} "
                f"after {length} items, a pull of {bias:+.3f} toward a prior "
                f"mean of {prior_mean:+.2f}")
    if abs(bias) < 0.05:
        fix = ("Nothing to correct here. The student is close enough to the "
               "prior mean that the shrinkage is not material.")
    else:
        fix = (f"Report the interval beside the point estimate and never the "
               f"point alone, and state the prior that produced it. If the "
               f"decision is about students far from the grade mean, either "
               f"lengthen the test past {length} items or widen the prior "
               f"past {prior_sd:g}, because those are the only two levers "
               f"that move the data weight.")

    return EapEstimate(
        true_theta=theta, grade_prior_mean=prior_mean,
        grade_prior_sd=prior_sd, test_length=length, test_information=info,
        data_weight=weight, estimated_theta=estimate, shrinkage_bias=bias,
        shrinkage_bias_magnitude=abs(bias), posterior_sd=posterior_sd,
        ci_low=ci_low, ci_high=ci_high,
        true_theta_inside_ci=ci_low <= theta <= ci_high,
        headline=headline, fix=fix, findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# 3. Bank exhaustion, a model with its assumptions stated
# ---------------------------------------------------------------------------

# What share of a band's items a selector actually reaches. Maximum
# information selection concentrates on the few most discriminating items, so
# the usable pool is far smaller than the count on the inventory. These four
# numbers are this model's assumptions, not measured constants, and they are
# named here so a reviewer can disagree with one of them rather than with the
# verdict.
UTILISATION_UNCONTROLLED = 0.25
UTILISATION_GAIN_STRATIFICATION = 0.30
UTILISATION_GAIN_SYMPSON_HETTER = 0.25
UTILISATION_CEILING = 0.90

RISK_LOW = "LOW"
RISK_MODERATE = "MODERATE"
RISK_HIGH = "HIGH"
RISK_CERTAIN = "CERTAIN"


@dataclass(frozen=True)
class BankForecast:
    items_per_band: int
    test_length: int
    sittings_per_year: int
    use_a_stratification: bool
    use_sympson_hetter: bool
    utilisation: float
    effective_pool: float
    exposure_rate: float
    overexposed: bool
    duplicate_exposure_rate: float
    crash_probability: float
    exhaustion_risk: str
    headline: str
    fix: str
    assumptions: tuple[str, ...] = field(default_factory=tuple)


def simulate_bank_exhaustion(items_per_band: int, test_length: int,
                             sittings_per_year: int,
                             use_a_stratification: bool,
                             use_sympson_hetter: bool) -> BankForecast:
    """Forecast pool exhaustion, repeat exposure and the chance of a crash.

    This one is a model, not a closed form result, and the numbers it leans
    on are listed in the return value so they travel with the answer. What it
    encodes is the well established behaviour that maximum information
    selection reaches for the same small set of items, that stratifying on
    discrimination spreads the early items across the bank, and that an
    exposure control caps how often any one item can be served.
    """
    band = int(items_per_band)
    length = int(test_length)
    sittings = int(sittings_per_year)
    if band <= 0:
        raise ValueError("a band with no items cannot be modelled")
    if length <= 0:
        raise ValueError("a test needs at least one item")
    if sittings <= 0:
        raise ValueError("a year with no sittings cannot be modelled")

    utilisation = UTILISATION_UNCONTROLLED
    if use_a_stratification:
        utilisation += UTILISATION_GAIN_STRATIFICATION
    if use_sympson_hetter:
        utilisation += UTILISATION_GAIN_SYMPSON_HETTER
    utilisation = min(utilisation, UTILISATION_CEILING)

    effective_pool = band * utilisation
    exposure_rate = min(1.0, length / effective_pool)
    # The share of a final sitting's items the same examinee has already seen,
    # drawing without replacement inside a sitting and with replacement across
    # them.
    duplicate = 1 - (1 - min(1.0, length / effective_pool)) ** max(0, sittings - 1)

    if effective_pool < length:
        crash = 1.0
        risk = RISK_CERTAIN
    else:
        ratio = length / effective_pool
        crash = max(0.0, min(1.0, (ratio - 0.5) * 2))
        risk = (RISK_HIGH if crash >= 0.5 else
                RISK_MODERATE if crash > 0 else RISK_LOW)

    controls = []
    if use_a_stratification:
        controls.append("a stratification")
    if use_sympson_hetter:
        controls.append("Sympson Hetter exposure control")
    control_text = " and ".join(controls) if controls else "no exposure control"

    if risk == RISK_CERTAIN:
        headline = (f"With {control_text} the usable pool in this band is "
                    f"{effective_pool:.0f} items against a {length} item "
                    f"test, so the selector runs out inside a single sitting")
    else:
        headline = (f"With {control_text} the usable pool is "
                    f"{effective_pool:.0f} of {band} items, each served to "
                    f"{exposure_rate * 100:.0f} percent of examinees, and by "
                    f"sitting {sittings} an examinee has already seen "
                    f"{duplicate * 100:.0f} percent of what they are given")

    if risk in (RISK_CERTAIN, RISK_HIGH):
        fix = (f"Write items or narrow the band. Exposure control raises "
               f"utilisation, it does not create items, and at "
               f"{effective_pool:.0f} usable against {length} needed no "
               f"control setting closes that gap.")
    elif exposure_rate > EXPOSURE_CAP:
        fix = (f"Exposure is {exposure_rate * 100:.0f} percent against a "
               f"{EXPOSURE_CAP * 100:.0f} percent cap. Add "
               f"{math.ceil(length / EXPOSURE_CAP / utilisation) - band} "
               f"items to this band, or add the control that is switched off.")
    else:
        fix = ("Exposure sits inside the usual cap. Keep measuring it per "
               "band rather than across the bank, because a healthy average "
               "hides a band that is being drained.")

    return BankForecast(
        items_per_band=band, test_length=length, sittings_per_year=sittings,
        use_a_stratification=bool(use_a_stratification),
        use_sympson_hetter=bool(use_sympson_hetter),
        utilisation=utilisation, effective_pool=effective_pool,
        exposure_rate=exposure_rate, overexposed=exposure_rate > EXPOSURE_CAP,
        duplicate_exposure_rate=duplicate, crash_probability=crash,
        exhaustion_risk=risk, headline=headline, fix=fix,
        assumptions=(
            f"Maximum information selection reaches "
            f"{UTILISATION_UNCONTROLLED * 100:.0f} percent of a band on its own.",
            f"a stratification adds {UTILISATION_GAIN_STRATIFICATION * 100:.0f} "
            f"percentage points of utilisation.",
            f"Sympson Hetter adds {UTILISATION_GAIN_SYMPSON_HETTER * 100:.0f} "
            f"percentage points.",
            f"Utilisation is capped at {UTILISATION_CEILING * 100:.0f} percent, "
            f"because no control reaches every item.",
            "These four figures are this model's assumptions, not measured "
            "constants. Replace them with your own exposure study when you "
            "have one.",
        ),
    )
