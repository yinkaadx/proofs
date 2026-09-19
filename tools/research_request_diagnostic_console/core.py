"""Research Request Diagnostic Console: the engine.

Three things go wrong between a client asking for research and a decision
being made better, and all three happen before any fieldwork starts.

1. The request names a method, not a decision. "Run a survey" is an answer
   to a question nobody has written down yet. The reframer moves from the
   method back to the decision, and where no decision can be named it says
   do not commission rather than quoting for it, because research that
   changes nothing is an invoice with a deck attached.
2. The method is chosen before the question type is known. Behaviour belongs
   in behavioural data, because what people do and what people say they do
   are different measurements. Attitude and motivation have to be asked,
   because no log records why. Choosing the method first is how a survey
   ends up asking people to remember their own purchase history.
3. The finding is read as if it travelled. A number from one region, one age
   band or one income band is a number about that group, and a national
   average is a number about nobody in particular.

What this file does not do is characterise a group of people. Section three
returns the questions to ask of a finding and the structural facts about
where it came from. It does not assert what any demographic thinks, wants or
values, because that is not knowable from a region label and inventing it
would be both wrong and the exact failure the section exists to prevent.

No Streamlit import lives here on purpose, so the same engine could sit behind
a real intake form without a line changing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

SEVERITY_OK = "OK"
SEVERITY_WARN = "WARN"
SEVERITY_CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    fix: str


def _worst(findings) -> str:
    for level in (SEVERITY_CRITICAL, SEVERITY_WARN):
        if any(f.severity == level for f in findings):
            return level
    return SEVERITY_OK


# ---------------------------------------------------------------------------
# 1. Reframing the request
# ---------------------------------------------------------------------------

VERDICT_COMMISSION = "WORTH COMMISSIONING"
VERDICT_REFRAME_FIRST = "REFRAME BEFORE QUOTING"
VERDICT_DO_NOT_COMMISSION = "DO NOT COMMISSION"
VERDICT_UNCLASSIFIED = "NOT CLASSIFIED"

# The question that decides whether any of this is worth doing. If the answer
# is that nothing changes either way, the research is an invoice with a deck
# attached and the honest move is to say so before quoting.
PIVOTAL_QUESTION = (
    "What will you do differently depending on the answer? If the answer is "
    "nothing, this should not be commissioned."
)


@dataclass(frozen=True)
class RequestArchetype:
    key: str
    label: str
    triggers: tuple[str, ...]
    surface_reading: str
    underlying_problem: str
    real_decision: str
    missing_context: tuple[str, ...]
    trap: str


ARCHETYPES: tuple[RequestArchetype, ...] = (
    RequestArchetype(
        key="churn",
        label="Churn diagnosis",
        triggers=("churn", "leaving", "cancel", "retention", "losing customers",
                  "why are we losing"),
        surface_reading="Find out why customers are leaving.",
        underlying_problem=(
            "Revenue is falling and nobody can say which of the plausible "
            "causes is the live one, so every proposed fix is a guess with a "
            "budget attached."),
        real_decision=(
            "Which single retention intervention gets funded next quarter, "
            "and what it has to move to be judged worth keeping."),
        missing_context=(
            "What is the churn rate now, and what was it twelve months ago?",
            "Does churn cluster by plan, tenure, acquisition channel or "
            "support contact history?",
            "Has anyone read the cancellation reasons already captured at the "
            "point of cancelling?",
            "Which interventions are actually affordable if the answer points "
            "at them?"),
        trap=(
            "Asking people who left why they left produces the reason they "
            "are willing to give, months after the fact. The behavioural "
            "record of what they did before leaving is usually already in the "
            "product database and nobody has looked.")),
    RequestArchetype(
        key="pricing",
        label="Pricing sensitivity",
        # "pay more for" rather than "pay more", because "pay more
        # attention" is ordinary English in a brief and matching it would
        # return a pricing reframe for a request about nothing of the kind.
        # A reframe that fires on the wrong request is the same failure as a
        # reframe that fits every request.
        triggers=("price", "pricing", "pay more for", "willingness to pay",
                  "how much would", "charge", "would pay", "pay for",
                  "too expensive", "discount"),
        surface_reading="Find out what customers would pay.",
        underlying_problem=(
            "A price change is being considered and the downside of getting "
            "it wrong is larger than the upside of getting it right, so "
            "nobody will commit without cover."),
        real_decision=(
            "Whether to change the price, by how much, and for which "
            "segment, with a defined threshold at which the change is "
            "reversed."),
        missing_context=(
            "What price is being considered, and against what alternative?",
            "Is a live price test possible on a slice of traffic?",
            "What is the current price elasticity from past changes, if any "
            "were ever measured?",
            "Which costs move with volume, so the break even is known before "
            "the question is asked?"),
        trap=(
            "Asked in a survey, willingness to pay is systematically "
            "overstated, because agreeing to a price costs nothing. A live "
            "price test on a traffic slice measures the same thing with "
            "money on the table.")),
    RequestArchetype(
        key="market_size",
        label="Market sizing",
        triggers=("market size", "how big", "tam", "total addressable",
                  "opportunity size", "size the market"),
        surface_reading="Work out how big the market is.",
        underlying_problem=(
            "A number is needed for a document that is going to a board or "
            "an investor, and the number needs to be defensible rather than "
            "precise."),
        real_decision=(
            "Whether to enter or expand, and what the entry budget is worth "
            "risking against the realistic share of the market reachable in "
            "the first two years."),
        missing_context=(
            "Who is the number for, and what decision does that reader make "
            "with it?",
            "Is the relevant figure the total market, the market this "
            "business can serve, or the share it could realistically win?",
            "What existing published sources already cover this category, "
            "and why are they not sufficient?",
            "What is the entry cost, so the number has something to be "
            "compared against?"),
        trap=(
            "A large total market number persuades nobody who is paying "
            "attention and reassures anyone who is not. The reachable share "
            "is the number that constrains the decision, and it is smaller "
            "and harder to produce.")),
    RequestArchetype(
        key="concept_test",
        label="Concept or feature test",
        triggers=("test this", "concept", "new product", "would customers use",
                  "feature", "launch", "interest in"),
        surface_reading="Find out whether people want this thing.",
        underlying_problem=(
            "A build has already been part scoped and the team wants "
            "permission to continue or a reason to stop, but no threshold "
            "for either has been written down."),
        real_decision=(
            "Whether this goes into the roadmap, at what priority, and what "
            "result would be enough to stop it."),
        missing_context=(
            "What result would be bad enough to cancel this? Write it down "
            "before fielding, not after.",
            "How far into the build is it already, and does that make the "
            "answer academic?",
            "What is the alternative use of the same engineering time?",
            "Is there an existing behaviour in the product that already "
            "proxies for this demand?"),
        trap=(
            "Concept tests without a stop threshold set in advance never "
            "stop anything. Whatever comes back is read as encouraging, "
            "because by the time it comes back the team is committed.")),
    RequestArchetype(
        key="brand",
        label="Brand perception",
        triggers=("brand", "perception", "how are we seen", "awareness",
                  "reputation", "positioning"),
        surface_reading="Find out how the brand is perceived.",
        underlying_problem=(
            "Marketing spend is being questioned and there is no agreed "
            "measure of what it has bought, so the argument is about opinion."),
        real_decision=(
            "Whether the positioning changes, and what the baseline is "
            "against which the next twelve months of spend gets judged."),
        missing_context=(
            "Perceived by whom? Customers, lapsed customers and people who "
            "have never heard of you are three different studies.",
            "Is there a prior wave to compare against, or is this the "
            "baseline?",
            "What would a bad result cause anyone to actually change?",
            "Which competitors should be measured alongside, so the number "
            "means something?"),
        trap=(
            "Brand perception measured once is a number with nothing to "
            "compare it to. The value is in the second wave, so the first "
            "one has to be designed to be repeatable.")),
    RequestArchetype(
        key="segmentation",
        label="Segmentation or targeting",
        triggers=("segment", "ideal customer", "target audience", "persona",
                  "who should we target", "icp"),
        surface_reading="Work out who the ideal customer is.",
        underlying_problem=(
            "Acquisition spend is spread across everyone and performing "
            "averagely, and nobody can defend cutting any of it."),
        real_decision=(
            "Which segments get budget next year and which get none, which "
            "is a decision about what to stop as much as what to start."),
        missing_context=(
            "Which segments can actually be reached differently once "
            "identified? A segment you cannot target separately is a "
            "description, not a decision.",
            "What does the existing customer base already show about value "
            "by segment?",
            "Is the organisation willing to say no to a segment, or is this "
            "an exercise in describing everyone?",
            "How is customer value measured, so segments can be ranked by "
            "something other than size?"),
        trap=(
            "Segmentations that produce segments nobody can reach or is "
            "willing to drop become wall posters. The test of a segment is "
            "whether budget can move because of it.")),
    RequestArchetype(
        key="campaign",
        label="Campaign performance",
        triggers=("campaign", "underperform", "did not work", "ads",
                  "conversion", "why did", "funnel"),
        surface_reading="Find out why the campaign did not work.",
        underlying_problem=(
            "Spend has been committed and the result is disappointing, and "
            "the question is whether the problem is the message, the "
            "audience, the offer or the landing experience."),
        real_decision=(
            "Whether to fix and rerun, reallocate the budget, or stop, and "
            "which of the four candidate causes gets the fix."),
        missing_context=(
            "What does the funnel data already show about which step lost "
            "people?",
            "Was anything held constant, so a comparison is possible at all?",
            "What was the campaign supposed to achieve, stated in a number "
            "agreed before it ran?",
            "Is there an audience overlap problem with another live campaign?"),
        trap=(
            "This is almost always answerable from analytics already "
            "collected. Commissioning primary research here usually means "
            "the analytics were never instrumented, and that is the thing to "
            "fix first, because it will happen again next quarter.")),
)

ARCHETYPE_BY_KEY = {a.key: a for a in ARCHETYPES}

# Phrases that say the request has no decision behind it. These are the
# sentences that should stop a quote rather than start one.
NO_DECISION_MARKERS: tuple[str, ...] = (
    "just curious", "for interest", "nice to know", "good to know",
    "for the record", "background", "no particular reason", "just want to know",
)


@dataclass(frozen=True)
class RequestReframe:
    surface_request: str
    archetype_key: str
    archetype_label: str
    underlying_problem: str
    real_decision: str
    missing_context: tuple[str, ...]
    trap: str
    verdict: str
    matched_triggers: tuple[str, ...]
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)

    @property
    def classified(self) -> bool:
        return self.archetype_key != ""

    @property
    def question_count(self) -> int:
        return len(self.missing_context)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def reframe_client_request(surface_request: str) -> RequestReframe:
    """Move from the method a client asked for to the decision underneath it.

    An unmatched request is reported as unmatched. The alternative, returning
    a generic business problem that fits anything, is the thing this tool
    exists to argue against: a reframe that would have been produced whatever
    was typed is not a reframe.
    """
    text = _normalise(surface_request)
    if not text:
        raise ValueError("a reframe needs a request to work from")

    findings: list[Finding] = []

    matched: list[str] = []
    archetype: RequestArchetype | None = None
    for candidate in ARCHETYPES:
        hits = tuple(t for t in candidate.triggers if t in text)
        if hits and len(hits) > len(matched):
            archetype, matched = candidate, list(hits)

    no_decision = tuple(m for m in NO_DECISION_MARKERS if m in text)
    if no_decision:
        findings.append(Finding(
            code="REQ-NO-DECISION", severity=SEVERITY_CRITICAL,
            title="This request says out loud that nothing depends on it",
            detail=(f"The phrase {no_decision[0]} is in the request. Research "
                    f"that changes no decision is an invoice with a deck "
                    f"attached, and agreeing to it costs the relationship "
                    f"more than declining it does."),
            fix=(f"Ask the one question that settles it. {PIVOTAL_QUESTION}")))

    if archetype is None:
        findings.append(Finding(
            code="REQ-UNMATCHED", severity=SEVERITY_WARN,
            title="This request does not match a known archetype",
            detail=("Nothing was recognised, so nothing has been reframed. "
                    "Returning a business problem that fits any request "
                    "would be the failure this whole tool argues against."),
            fix=(f"Take it to the client directly. {PIVOTAL_QUESTION}")))
        verdict = (VERDICT_DO_NOT_COMMISSION if no_decision
                   else VERDICT_UNCLASSIFIED)
        return RequestReframe(
            surface_request=str(surface_request).strip(), archetype_key="",
            archetype_label="", underlying_problem="", real_decision="",
            missing_context=(PIVOTAL_QUESTION,),
            trap="", verdict=verdict, matched_triggers=(),
            findings=tuple(findings))

    findings.append(Finding(
        code="REQ-TRAP", severity=SEVERITY_WARN,
        title=f"The usual trap in a {archetype.label.lower()} request",
        detail=archetype.trap,
        fix=("Check this before the proposal, not in the debrief, because "
             "after fieldwork it is an excuse rather than a finding.")))

    findings.append(Finding(
        code="REQ-DECISION", severity=SEVERITY_OK,
        title="The decision this research is for",
        detail=archetype.real_decision,
        fix=(f"Put that sentence at the top of the proposal. "
             f"{PIVOTAL_QUESTION}")))

    verdict = (VERDICT_DO_NOT_COMMISSION if no_decision
               else VERDICT_REFRAME_FIRST)

    return RequestReframe(
        surface_request=str(surface_request).strip(),
        archetype_key=archetype.key, archetype_label=archetype.label,
        underlying_problem=archetype.underlying_problem,
        real_decision=archetype.real_decision,
        missing_context=archetype.missing_context, trap=archetype.trap,
        verdict=verdict, matched_triggers=tuple(matched),
        findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# 2. Methodology scoring
# ---------------------------------------------------------------------------

METHOD_SURVEY = "Custom survey design"
METHOD_EXISTING = "Existing data analysis"
METHOD_BOTH = "Existing data first, then a narrow survey on what is left"

# The weights, stated so they can be argued with rather than reverse
# engineered from the answer. They add to one hundred and a test checks it.
DIMENSIONS: tuple[tuple[str, int, str], ...] = (
    ("Fit to the question type", 30,
     "Behaviour belongs in behavioural data and motivation has to be asked. "
     "This is the dimension that decides most cases on its own."),
    ("Precision on this specific question", 20,
     "Whether the method can answer the question as asked, rather than "
     "something adjacent to it."),
    ("Coverage of the population that matters", 20,
     "Whether the people whose answer counts are reachable by this method at "
     "all."),
    ("Speed to an answer", 15,
     "Fieldwork takes weeks. A query against data already collected takes "
     "an afternoon, when the data exists."),
    ("Cost", 15,
     "Including the analyst time, which is the part left out of most "
     "comparisons."),
)

TOTAL_WEIGHT = sum(weight for _name, weight, _note in DIMENSIONS)

# Below this gap the two methods are too close to call, and the honest answer
# is the sequenced one rather than a coin toss dressed as a recommendation.
DECISIVE_MARGIN = 8


@dataclass(frozen=True)
class GoalProfile:
    key: str
    label: str
    question_type: str
    survey_scores: tuple[int, ...]
    existing_scores: tuple[int, ...]
    note: str


# Scores are out of ten per dimension. The question type drives the first
# dimension and everything else follows from how the data is actually
# obtained.
GOALS: tuple[GoalProfile, ...] = (
    GoalProfile(
        key="churn", label="Understand why customers are churning",
        question_type="Behavioural",
        survey_scores=(3, 4, 4, 3, 4),
        existing_scores=(9, 8, 9, 9, 9),
        note=("What they did before leaving is recorded. What they say "
              "afterwards is a reconstruction, and a polite one.")),
    GoalProfile(
        key="pricing", label="Decide whether to change the price",
        question_type="Behavioural, with an attitudinal fringe",
        survey_scores=(4, 4, 6, 4, 5),
        existing_scores=(8, 7, 7, 8, 8),
        note=("Stated willingness to pay is overstated because agreeing "
              "costs nothing. A live price test measures the same thing "
              "with money on the table.")),
    GoalProfile(
        key="campaign", label="Diagnose why a campaign underperformed",
        question_type="Behavioural",
        survey_scores=(3, 3, 4, 3, 4),
        existing_scores=(9, 9, 8, 9, 9),
        note=("Almost always answerable from analytics already collected. "
              "If it is not, the instrumentation is the thing to fix.")),
    GoalProfile(
        key="brand", label="Measure how the brand is perceived",
        question_type="Attitudinal",
        survey_scores=(9, 8, 8, 5, 5),
        existing_scores=(2, 3, 3, 8, 9),
        note=("No log records why somebody thinks what they think. This one "
              "has to be asked, and asked again later to mean anything.")),
    GoalProfile(
        key="motivation", label="Understand why customers chose a competitor",
        question_type="Attitudinal",
        survey_scores=(9, 7, 6, 5, 5),
        existing_scores=(3, 3, 3, 8, 9),
        note=("The people who matter here are not in your data at all, "
              "which settles the coverage dimension before anything else.")),
    GoalProfile(
        key="concept", label="Decide whether to build a new feature",
        question_type="Mixed",
        survey_scores=(6, 6, 6, 5, 6),
        existing_scores=(6, 6, 5, 8, 8),
        note=("Existing behaviour often already proxies for the demand. "
              "Where it does not, a narrow test beats a broad survey.")),
    GoalProfile(
        key="segmentation", label="Decide which segments to fund",
        question_type="Mixed",
        survey_scores=(6, 6, 7, 5, 5),
        existing_scores=(7, 7, 6, 8, 8),
        note=("Value by segment is usually already in the customer base. "
              "What is missing is why, and only that part needs asking.")),
    GoalProfile(
        key="sizing", label="Size the market for an entry decision",
        question_type="Secondary",
        survey_scores=(3, 4, 4, 3, 3),
        existing_scores=(8, 7, 7, 9, 9),
        note=("Published sources and internal analogues do most of this. A "
              "survey sizes awareness, which is a different number.")),
)

GOAL_BY_KEY = {g.key: g for g in GOALS}
GOAL_BY_LABEL = {g.label: g for g in GOALS}


@dataclass(frozen=True)
class ScoreRow:
    dimension: str
    weight: int
    survey_score: int
    existing_score: int
    survey_weighted: float
    existing_weighted: float


@dataclass(frozen=True)
class MethodologyVerdict:
    goal_key: str
    goal_label: str
    question_type: str
    rows: tuple[ScoreRow, ...]
    survey_total: float
    existing_total: float
    margin: float
    recommendation: str
    decisive: bool
    note: str
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def rows_survey_total(self) -> float:
        return round(sum(r.survey_weighted for r in self.rows), 2)

    @property
    def rows_existing_total(self) -> float:
        return round(sum(r.existing_weighted for r in self.rows), 2)


def _resolve_goal(value) -> GoalProfile:
    if isinstance(value, GoalProfile):
        return value
    name = str(value or "").strip()
    if name in GOAL_BY_KEY:
        return GOAL_BY_KEY[name]
    if name in GOAL_BY_LABEL:
        return GOAL_BY_LABEL[name]
    raise ValueError(f"unknown business goal: {value!r}")


def evaluate_methodology(business_goal) -> MethodologyVerdict:
    """Score a custom survey against analysing what is already collected.

    The weights are published rather than implied, the rows are shown, and
    the totals are the sum of the rows. Where the two methods land within the
    decisive margin the answer is the sequenced one, because recommending a
    coin toss with a decimal place on it is worse than admitting it is close.
    """
    goal = _resolve_goal(business_goal)

    rows: list[ScoreRow] = []
    for index, (dimension, weight, _note) in enumerate(DIMENSIONS):
        survey = goal.survey_scores[index]
        existing = goal.existing_scores[index]
        rows.append(ScoreRow(
            dimension=dimension, weight=weight, survey_score=survey,
            existing_score=existing,
            survey_weighted=round(survey * weight / 10, 2),
            existing_weighted=round(existing * weight / 10, 2)))

    survey_total = round(sum(r.survey_weighted for r in rows), 2)
    existing_total = round(sum(r.existing_weighted for r in rows), 2)
    margin = round(abs(survey_total - existing_total), 2)
    decisive = margin >= DECISIVE_MARGIN

    if not decisive:
        recommendation = METHOD_BOTH
    elif existing_total > survey_total:
        recommendation = METHOD_EXISTING
    else:
        recommendation = METHOD_SURVEY

    findings: list[Finding] = []

    findings.append(Finding(
        code="METH-TYPE", severity=SEVERITY_OK,
        title=f"This is a {goal.question_type.lower()} question",
        detail=goal.note,
        fix=("Settle the question type before the method. It carries thirty "
             "of the hundred points on its own and it is the dimension most "
             "often skipped.")))

    if not decisive:
        findings.append(Finding(
            code="METH-CLOSE", severity=SEVERITY_WARN,
            title=f"The two methods are {margin} points apart, which is too close to call",
            detail=(f"Anything under {DECISIVE_MARGIN} points is inside the "
                    f"noise of a scoring model built out of judgement. "
                    f"Declaring a winner here would be a coin toss with a "
                    f"decimal place on it."),
            fix=("Run the existing data first because it is cheap and fast, "
                 "then field a narrow survey on whatever it could not "
                 "answer. That sequence costs less than either method alone "
                 "run badly.")))
    else:
        findings.append(Finding(
            code="METH-CLEAR", severity=SEVERITY_OK,
            title=f"{recommendation} wins by {margin} points",
            detail=(f"Survey {survey_total} against existing data "
                    f"{existing_total}, out of {TOTAL_WEIGHT}."),
            fix=("The losing method still has a role in the follow up. A "
                 "clear winner on the primary question is not a reason to "
                 "never ask anybody anything.")))

    if recommendation == METHOD_EXISTING:
        findings.append(Finding(
            code="METH-INSTRUMENT", severity=SEVERITY_WARN,
            title="If the existing data cannot answer this, that is the finding",
            detail=("A question that should be answerable from collected data "
                    "and is not means the instrumentation is missing. That "
                    "recurs every quarter until it is fixed, and fixing it "
                    "costs less than one round of fieldwork."),
            fix=("Report the gap explicitly rather than quietly substituting "
                 "a survey for it.")))

    return MethodologyVerdict(
        goal_key=goal.key, goal_label=goal.label,
        question_type=goal.question_type, rows=tuple(rows),
        survey_total=survey_total, existing_total=existing_total,
        margin=margin, recommendation=recommendation, decisive=decisive,
        note=goal.note, findings=tuple(findings),
    )


# ---------------------------------------------------------------------------
# 3. US market context
# ---------------------------------------------------------------------------

REGION_NATIONAL = "National"
REGION_NORTHEAST = "Northeast"
REGION_MIDWEST = "Midwest"
REGION_SOUTH = "South"
REGION_WEST = "West"

# The four Census regions, plus the national roll up. Named because they are
# the units US sample frames are actually built on, so a finding is usually
# reported against one of them whether or not anyone says so.
REGIONS: tuple[str, ...] = (REGION_NATIONAL, REGION_NORTHEAST, REGION_MIDWEST,
                            REGION_SOUTH, REGION_WEST)

# Demographic cuts scoped deliberately to life stage, household income band
# and urbanicity. These are the cuts where the structural caution is about
# measurable economics and sample construction rather than about what a group
# of people is supposedly like.
DEMOGRAPHICS: tuple[str, ...] = (
    "All adults",
    "Under 30",
    "30 to 49",
    "50 and over",
    "Lower income households",
    "Middle income households",
    "Higher income households",
    "Urban",
    "Suburban",
    "Rural",
)

CONFIDENCE_DIRECTIONAL = "DIRECTIONAL ONLY"
CONFIDENCE_QUALIFIED = "USABLE WITH THE CAVEAT STATED"
CONFIDENCE_UNKNOWN = "UNKNOWN UNTIL THE BASE SIZE IS GIVEN"


@dataclass(frozen=True)
class ContextLayer:
    raw_finding: str
    region: str
    demographic: str
    confidence: str
    travels_nationally: bool
    interpretation: str
    questions_to_ask: tuple[str, ...]
    structural_notes: tuple[str, ...]
    refusals: tuple[str, ...]
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def severity(self) -> str:
        return _worst(self.findings)


# Said plainly rather than implied, because a tool that quietly declined to
# answer would look broken instead of principled.
REFUSALS: tuple[str, ...] = (
    "This layer does not tell you what any group of people thinks, wants or "
    "values. A region label and an age band do not contain that, and a tool "
    "that produced it would be inventing it.",
    "It does not supply population statistics, market sizes or spend figures. "
    "Those have a source or they do not exist, and a number without one is "
    "worse than no number because it gets quoted.",
    "What it does give you is where the finding came from, what that means "
    "for how far it travels, and the questions to put to it before it reaches "
    "a slide.",
)


def apply_us_market_context(raw_finding: str, region: str,
                            demographic: str) -> ContextLayer:
    """Say how far a finding travels, and what to ask before it reaches a slide.

    Deliberately not a generator of cultural characterisations. The honest
    output is the provenance of the number and the questions it has not been
    asked yet, because that is what a region label and a demographic cut
    genuinely support.
    """
    text = str(raw_finding or "").strip()
    if not text:
        raise ValueError("a context layer needs a finding to work from")
    place = str(region or "").strip()
    if place not in REGIONS:
        raise ValueError(f"unknown region: {region!r}")
    group = str(demographic or "").strip()
    if group not in DEMOGRAPHICS:
        raise ValueError(f"unknown demographic: {demographic!r}")

    findings: list[Finding] = []
    notes: list[str] = []
    questions: list[str] = [
        "What was the base size for this exact cut, before any weighting?",
        "Is this cut one the study was designed to read, or one produced "
        "afterwards because the data allowed it?",
        "What is the equivalent number for the group this is being compared "
        "against, and was that comparison planned?",
    ]

    national = place == REGION_NATIONAL
    all_adults = group == "All adults"

    if national:
        notes.append(
            "A national figure is an average across four regions whose cost "
            "of living, housing cost and channel structure differ enough "
            "that the average may describe nowhere in particular.")
        findings.append(Finding(
            code="CTX-NATIONAL", severity=SEVERITY_WARN,
            title="A national average can hide the variance that matters",
            detail=("If the decision is being made market by market, the "
                    "national number is the wrong unit, and the regional "
                    "spread is the finding rather than a footnote."),
            fix=("Ask for the same number by region before presenting the "
                 "average. If the spread is wide, the average should not "
                 "lead the slide.")))
    else:
        notes.append(
            f"This is a {place} figure. Whether it travels to the other three "
            f"regions is an empirical question that this study has not "
            f"answered, and presenting it without the region named invites "
            f"the reader to assume it did.")
        findings.append(Finding(
            code="CTX-REGION", severity=SEVERITY_WARN,
            title=f"This number is about the {place}, and only about the {place}",
            detail=("Cost of living, housing tenure, commuting patterns and "
                    "retail channel mix differ across the four Census "
                    "regions. A price or convenience finding in particular "
                    "carries those differences with it."),
            fix=(f"Label the chart {place} rather than leaving it unlabelled, "
                 f"which is what makes a regional number get read as a "
                 f"national one.")))

    if not all_adults:
        questions.append(
            f"Was {group.lower()} a quota in the sample design, or a subgroup "
            f"read out of a general population sample?")
        findings.append(Finding(
            code="CTX-SUBGROUP", severity=SEVERITY_CRITICAL,
            title=f"A {group.lower()} read needs its base size before it means anything",
            detail=("A subgroup cut from a general population sample often "
                    "has a base small enough that the difference being "
                    "pointed at is inside the margin of error. That is the "
                    "single most common way a research finding becomes "
                    "confidently wrong."),
            fix=("Get the unweighted base for this cut. If it is small, "
                 "report the direction and refuse to report the size.")))

    if group in ("Lower income households", "Middle income households",
                 "Higher income households"):
        notes.append(
            "Income bands are not comparable across regions without adjusting "
            "for local cost of living, so the same household income places a "
            "household differently depending on where it is.")
        questions.append(
            "Was the income band defined nationally or adjusted for local "
            "cost of living?")
    if group in ("Urban", "Suburban", "Rural"):
        notes.append(
            "Urbanicity changes what is physically available: delivery "
            "coverage, store density and broadband quality differ, so a "
            "preference finding may be measuring availability.")
        questions.append(
            "Could this result be explained by what is available to this "
            "group rather than by what it prefers?")
    if group in ("Under 30", "30 to 49", "50 and over"):
        notes.append(
            "Age bands differ in how they were reached. Online panels, phone "
            "frames and address based samples each under or over represent "
            "different ages, so mode is part of the result.")
        questions.append(
            "What mode was this collected in, and how does that mode perform "
            "for this age band?")

    findings.append(Finding(
        code="CTX-SCOPE", severity=SEVERITY_OK,
        title="What this layer will not do",
        detail=REFUSALS[0],
        fix=("Use it for provenance and for the questions. For anything about "
             "what people think, ask them and cite the study.")))

    if national and all_adults:
        confidence = CONFIDENCE_QUALIFIED
        travels = True
        interpretation = (
            f"Read as a national all adult figure, this travels as far as a "
            f"national all adult figure goes, which is to say it describes "
            f"the aggregate and not any particular market. Before it drives "
            f"a market by market decision, ask for the regional spread.")
    elif all_adults:
        confidence = CONFIDENCE_QUALIFIED
        travels = False
        interpretation = (
            f"This describes adults in the {place} and nothing has been "
            f"established about the other three regions. Presented without "
            f"the region on the label, it will be read as national, and that "
            f"is a reading the study cannot support.")
    else:
        confidence = CONFIDENCE_UNKNOWN
        travels = False
        interpretation = (
            f"This is a {group.lower()} cut within {place}, so two limits "
            f"apply at once: it is not established outside {place}, and the "
            f"subgroup base is unknown until someone supplies it. Until it is "
            f"supplied, this is a direction and not a size.")

    return ContextLayer(
        raw_finding=text, region=place, demographic=group,
        confidence=confidence, travels_nationally=travels,
        interpretation=interpretation, questions_to_ask=tuple(questions),
        structural_notes=tuple(notes), refusals=REFUSALS,
        findings=tuple(findings),
    )
