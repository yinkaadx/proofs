"""Server Side Tracking and Claude Agent Console: the engine.

Three things go wrong on a Stape server side container and each one is
checkable rather than arguable.

1. Deduplication. A browser Pixel event and a server CAPI event describe one
   action. Meta folds them into one only when the event name and the event id
   both match. Miss either and the platform counts the action twice, which
   inflates the conversion count and splits the match signals across two
   records instead of concentrating them on one.
2. Consent. Consent Mode v2 added ad_user_data and ad_personalization on top
   of the storage signals, and a server container that forwards regardless is
   the failure mode nobody sees until an audit. What a tag may do differs by
   region, and Quebec is stricter than the rest of Canada.
3. Auditing the container itself. Clicking through a GTM workspace by hand is
   how a stale tag survives three reviews, so the third section emits a Claude
   Code skill manifest that reads the container through the Tag Manager API
   and reports rather than edits.

No Streamlit import lives here on purpose, so the same engine could sit behind
a real integration without a line changing.

Nothing in this file is legal advice. The consent rules encoded here are the
rules this tester applies, named in the open so a reviewer can disagree with a
specific line rather than with a verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ENGINE_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# 1. Meta CAPI deduplication
# ---------------------------------------------------------------------------

# The standard event names Meta recognises. A custom name is allowed by the
# platform but it is not one of these, and the distinction matters because a
# typo in a standard name silently becomes a custom event that no standard
# optimisation reads.
STANDARD_EVENTS: tuple[str, ...] = (
    "AddPaymentInfo", "AddToCart", "AddToWishlist", "CompleteRegistration",
    "Contact", "CustomizeProduct", "Donate", "FindLocation",
    "InitiateCheckout", "Lead", "PageView", "Purchase", "Schedule",
    "Search", "StartTrial", "SubmitApplication", "Subscribe", "ViewContent",
)

MATCH_DEDUPLICATED = "DEDUPLICATED"
MATCH_ID_MISMATCH = "ID_MISMATCH"
MATCH_BROWSER_ID_MISSING = "BROWSER_ID_MISSING"
MATCH_SERVER_ID_MISSING = "SERVER_ID_MISSING"
MATCH_BOTH_IDS_MISSING = "BOTH_IDS_MISSING"

SEVERITY_OK = "OK"
SEVERITY_HIGH = "HIGH"
SEVERITY_CRITICAL = "CRITICAL"

# This engine's own estimate of the damage on Meta's ten point Event Match
# Quality scale, stated as an estimate and not as a platform figure. The
# reasoning is the part that travels: a duplicate splits the customer
# parameters of one action across two records, so neither record carries the
# full match signal the scale rewards.
EMQ_PENALTY_DOUBLE_COUNT = -2.5
EMQ_PENALTY_HALF_BLIND = -1.5


@dataclass(frozen=True)
class DedupResult:
    event_name: str
    browser_event_id: str
    server_event_id: str
    match_status: str
    deduplicated: bool
    counted_events: int
    is_standard_event: bool
    emq_impact_points: float
    severity: str
    headline: str
    fix: str

    @property
    def inflation_pct(self) -> float:
        """How much this event overstates the conversion count, as a percent."""
        return 0.0 if self.counted_events == 1 else 100.0


def validate_capi_dedup(browser_event_id: str, server_event_id: str,
                        event_name: str) -> DedupResult:
    """Decide whether Meta will fold a browser event and a server event into one.

    The rule the platform applies is simple and unforgiving: the event name and
    the event id must both match. This returns the match status, whether the
    pair deduplicates, how many events the platform ends up counting, and an
    estimate of the effect on Event Match Quality.
    """
    name = str(event_name or "").strip()
    browser_id = str(browser_event_id or "").strip()
    server_id = str(server_event_id or "").strip()
    if not name:
        raise ValueError("an event with no name cannot be deduplicated")
    standard = name in STANDARD_EVENTS

    if not browser_id and not server_id:
        return _dedup_failure(
            name, browser_id, server_id, MATCH_BOTH_IDS_MISSING, standard,
            EMQ_PENALTY_DOUBLE_COUNT, SEVERITY_CRITICAL,
            headline=(f"Neither side of {name} carries an event id, so Meta "
                      f"has nothing to match on and counts the action twice"),
            fix=("Generate one id per action in the browser, send it as "
                 "eventID on the Pixel call and as event_id on the CAPI "
                 "payload. A UUID from the page is the usual source, and the "
                 "server container must forward the one it was given rather "
                 "than mint a second one."),
        )
    if not browser_id:
        return _dedup_failure(
            name, browser_id, server_id, MATCH_BROWSER_ID_MISSING, standard,
            EMQ_PENALTY_DOUBLE_COUNT, SEVERITY_CRITICAL,
            headline=(f"The server event for {name} has an id and the browser "
                      f"event does not, so the pair cannot be matched"),
            fix=("Set eventID on the Pixel call. This is the common Stape "
                 "case: the server tag was built carefully and the browser "
                 "tag was left as the default snippet."),
        )
    if not server_id:
        return _dedup_failure(
            name, browser_id, server_id, MATCH_SERVER_ID_MISSING, standard,
            EMQ_PENALTY_DOUBLE_COUNT, SEVERITY_CRITICAL,
            headline=(f"The browser event for {name} has an id and the server "
                      f"event does not, so the pair cannot be matched"),
            fix=("Map the browser event id into the server container as a "
                 "variable and set event_id on the CAPI tag from it. Do not "
                 "let the server tag generate its own."),
        )
    if browser_id != server_id:
        return _dedup_failure(
            name, browser_id, server_id, MATCH_ID_MISMATCH, standard,
            EMQ_PENALTY_DOUBLE_COUNT, SEVERITY_CRITICAL,
            headline=(f"Both sides of {name} carry an id and the two ids "
                      f"differ, so the action is counted twice"),
            fix=("Two generators are running. Pick one, almost always the "
                 "browser, and have the server container read that value "
                 "instead of creating its own timestamp based id."),
        )

    severity = SEVERITY_OK if standard else SEVERITY_HIGH
    headline = (f"{name} deduplicates: both sides carry {browser_id} and the "
                f"platform counts one event")
    fix = "Nothing to change on deduplication for this event."
    penalty = 0.0
    if not standard:
        headline += (f", though {name} is not one of the standard event names, "
                     f"so standard optimisation will not read it")
        fix = (f"Confirm {name} is a deliberate custom event. If it was meant "
               f"to be a standard one, a single character difference is "
               f"enough to make it custom.")
        penalty = EMQ_PENALTY_HALF_BLIND

    return DedupResult(
        event_name=name, browser_event_id=browser_id,
        server_event_id=server_id, match_status=MATCH_DEDUPLICATED,
        deduplicated=True, counted_events=1, is_standard_event=standard,
        emq_impact_points=penalty, severity=severity,
        headline=headline, fix=fix,
    )


def _dedup_failure(name: str, browser_id: str, server_id: str, status: str,
                   standard: bool, penalty: float, severity: str,
                   headline: str, fix: str) -> DedupResult:
    return DedupResult(
        event_name=name, browser_event_id=browser_id,
        server_event_id=server_id, match_status=status, deduplicated=False,
        counted_events=2, is_standard_event=standard,
        emq_impact_points=penalty, severity=severity,
        headline=headline, fix=fix,
    )


SAMPLE_EVENT_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("evt-9f1c-4a7e", "evt-9f1c-4a7e", "Purchase"),
    ("evt-2b40-11de", "evt-7c88-90aa", "Purchase"),
    ("", "evt-5510-22bc", "AddToCart"),
    ("evt-6631-77ef", "", "Lead"),
    ("evt-8812-33cd", "evt-8812-33cd", "PurchaseComplete"),
)


def dedup_board(pairs: tuple[tuple[str, str, str], ...] = SAMPLE_EVENT_PAIRS
                ) -> dict:
    """Every sample pair rated, with totals that equal the rows."""
    results = [validate_capi_dedup(*pair) for pair in pairs]
    clean = sum(1 for r in results if r.deduplicated)
    broken = sum(1 for r in results if not r.deduplicated)
    counted = sum(r.counted_events for r in results)
    assert clean + broken == len(results), "every pair is one or the other"
    return {
        "total": len(results),
        "deduplicated": clean,
        "double_counted": broken,
        "actions_taken": len(results),
        "events_meta_counts": counted,
        "inflation_pct": round(
            (counted - len(results)) / len(results) * 100, 1) if results else 0.0,
        "results": tuple(results),
    }


# ---------------------------------------------------------------------------
# 2. Consent Mode v2
# ---------------------------------------------------------------------------

REGION_QUEBEC = "Quebec"
REGION_CANADA = "Canada outside Quebec"
REGION_EEA = "European Economic Area"
REGION_REST = "Rest of world"

REGIONS: tuple[str, ...] = (REGION_QUEBEC, REGION_CANADA, REGION_EEA, REGION_REST)

DISPATCH_FULL = "FULL"
DISPATCH_RESTRICTED = "RESTRICTED"
DISPATCH_MODELLED_ONLY = "MODELLED_ONLY"

# The rule each region imposes on the default state before any choice is made.
# Quebec Law 25 and the EEA both require a choice before the tracking runs, so
# the honest default there is denied. Elsewhere the default is looser, which
# is a business decision rather than a technical one.
EXPRESS_CONSENT_REGIONS: tuple[str, ...] = (REGION_QUEBEC, REGION_EEA)

REGION_BASIS = {
    REGION_QUEBEC: (
        "Quebec Law 25 treats technology that identifies, locates or profiles "
        "a person as something a person must be told about and must be able "
        "to switch off. The practical reading for a tag: no identifier and no "
        "profiling signal before an explicit choice, and a pre ticked box is "
        "not a choice."
    ),
    REGION_CANADA: (
        "PIPEDA asks for meaningful consent, judged by how sensitive the data "
        "is and how reasonably a person would expect it to be used. Implied "
        "consent can carry ordinary analytics. It does not carry sensitive "
        "categories or onward sale."
    ),
    REGION_EEA: (
        "Consent Mode v2 is the mechanism, not the permission. Ad user data "
        "and ad personalization both need a prior affirmative act, and a "
        "denied signal has to actually stop the identifier leaving."
    ),
    REGION_REST: (
        "No regional rule is applied here beyond the signals themselves. The "
        "signals are still honoured, because a tag that ignores a denial in "
        "one market is a tag that ignores it in every market."
    ),
}


@dataclass(frozen=True)
class ConsentDecision:
    region: str
    ad_user_data_granted: bool
    ad_personalization_granted: bool
    dispatch_status: str
    legal_basis: str
    tags_allowed: tuple[str, ...]
    tags_blocked: tuple[str, ...]
    identifiers_leave_the_browser: bool
    express_consent_required: bool
    headline: str
    fix: str
    findings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def compliant_default(self) -> bool:
        """True when nothing identifying moves without an affirmative grant."""
        if not self.express_consent_required:
            return True
        return self.ad_user_data_granted or not self.identifiers_leave_the_browser


def evaluate_consent_mode_v2(ad_user_data_granted: bool,
                             ad_personalization_granted: bool,
                             region: str) -> ConsentDecision:
    """What a tag may dispatch, given the two v2 signals and the region.

    ad_user_data governs whether an identifier may leave at all. That one is
    the gate. ad_personalization governs what the identifier may then be used
    for, so personalization granted while user data is denied is incoherent
    rather than permissive, and this reports it as such rather than quietly
    picking one.
    """
    user_data = bool(ad_user_data_granted)
    personalization = bool(ad_personalization_granted)
    name = str(region or "").strip() or REGION_REST
    if name not in REGIONS:
        raise ValueError(f"unknown region: {region!r}")
    express = name in EXPRESS_CONSENT_REGIONS
    basis = REGION_BASIS[name]
    findings: list[str] = []

    if personalization and not user_data:
        findings.append(
            "Personalization is granted while user data is denied. Nothing "
            "can be personalised to a person whose identifier never arrives, "
            "so the stricter signal wins and the pair should be corrected in "
            "the consent banner rather than reconciled in the container.")

    if user_data and personalization:
        allowed = ("GA4 server client", "Meta CAPI with customer parameters",
                   "Google Ads conversion with user data",
                   "Remarketing audience build")
        blocked = ()
        status = DISPATCH_FULL
        headline = (f"Full dispatch in {name}: identifiers may leave and may "
                    f"be used to personalise")
        fix = ("Log the consent record with the event, not just in the banner. "
               "A granted signal you cannot evidence later is a denied one "
               "under audit.")
        identifiers = True
    elif user_data and not personalization:
        allowed = ("GA4 server client", "Meta CAPI with customer parameters",
                   "Google Ads conversion with user data")
        blocked = ("Remarketing audience build", "Personalised ad targeting",
                   "Lookalike seeding from this event")
        status = DISPATCH_RESTRICTED
        headline = (f"Restricted dispatch in {name}: conversions may be "
                    f"measured, personalisation may not be built")
        fix = ("Split the tags. One conversion tag that fires on user data, "
               "one audience tag that fires on personalization. A single tag "
               "carrying both jobs cannot honour this state.")
        identifiers = True
    else:
        allowed = ("Cookieless ping for conversion modelling",)
        blocked = ("Meta CAPI with customer parameters",
                   "Google Ads conversion with user data",
                   "Remarketing audience build", "Personalised ad targeting")
        status = DISPATCH_MODELLED_ONLY
        headline = (f"Modelled only in {name}: no identifier leaves, and the "
                    f"conversion count becomes an estimate")
        fix = ("Strip customer parameters in the server container rather than "
               "trusting each tag template to do it. One place to check beats "
               "eleven, and the server is the last point where a mistake is "
               "still yours to catch.")
        identifiers = False
        if personalization:
            fix = ("Fix the banner so the two signals cannot disagree, then "
                   + fix[0].lower() + fix[1:])

    if express and identifiers:
        findings.append(
            f"{name} expects an affirmative choice before this runs. Confirm "
            f"the grant came from a click and not from a default, because the "
            f"container cannot tell the difference and an auditor can.")
    if express and not identifiers:
        findings.append(
            f"This is the correct default state for {name} before a choice is "
            f"made. Nothing identifying is moving.")

    return ConsentDecision(
        region=name, ad_user_data_granted=user_data,
        ad_personalization_granted=personalization, dispatch_status=status,
        legal_basis=basis, tags_allowed=allowed, tags_blocked=blocked,
        identifiers_leave_the_browser=identifiers,
        express_consent_required=express, headline=headline, fix=fix,
        findings=tuple(findings),
    )


def consent_matrix(region: str) -> tuple[ConsentDecision, ...]:
    """All four signal combinations for one region, in a fixed order."""
    return tuple(
        evaluate_consent_mode_v2(user_data, personalization, region)
        for user_data, personalization in (
            (True, True), (True, False), (False, True), (False, False))
    )


# ---------------------------------------------------------------------------
# 3. Claude Code skill manifest for GTM container auditing
# ---------------------------------------------------------------------------

SKILL_NAME = "gtm-container-auditor"
GTM_API_BASE = "https://tagmanager.googleapis.com/tagmanager/v2"
GTM_READONLY_SCOPE = "https://www.googleapis.com/auth/tagmanager.readonly"

SKILL_MANIFEST = """---
name: gtm-container-auditor
description: >-
  Audit a Google Tag Manager container through the Tag Manager API v2 and
  report what is actually published: tags with no trigger, triggers with no
  tag, tags that fire before consent is known, Meta CAPI tags with no event
  id mapping, and variables referenced by nothing. Use when asked to review,
  audit or clean a GTM or server side container, or to explain why a tag is
  not firing. Reads only. It never publishes a version.
allowed-tools: Bash, Read, Grep
---

# GTM Container Auditor

Read only by construction. Every call below is a GET, the workspace is never
written to, and no version is ever published. If a fix is needed, the report
says what to change and a person changes it.

## Before the first call

The account must already hold a token with the read only scope
`https://www.googleapis.com/auth/tagmanager.readonly`. Confirm it is present
rather than asking for a new one:

```bash
gcloud auth print-access-token --scopes=https://www.googleapis.com/auth/tagmanager.readonly
```

## Reading the container

Base URL: `https://tagmanager.googleapis.com/tagmanager/v2`

| What | Call |
| --- | --- |
| Accounts | `GET /accounts` |
| Containers | `GET /accounts/{account_id}/containers` |
| Workspaces | `GET /accounts/{account_id}/containers/{container_id}/workspaces` |
| Tags | `GET /{workspace_path}/tags` |
| Triggers | `GET /{workspace_path}/triggers` |
| Variables | `GET /{workspace_path}/variables` |
| Live version | `GET /accounts/{a}/containers/{c}/versions:live` |

Read the live version as well as the workspace. A workspace shows what someone
intends to publish and the live version shows what is running, and the gap
between the two is where most of the surprises live.

## What to report

1. Tags with an empty `firingTriggerId`, which never fire and still look busy.
2. Triggers referenced by no tag, which are the leftovers of a removed tag.
3. Tags whose `consentSettings` is `NOT_SET` where the container serves a
   region that requires an affirmative choice.
4. Meta CAPI tags with no `event_id` parameter, which cannot deduplicate
   against the browser Pixel and so double count every conversion.
5. Variables referenced by no tag, trigger or other variable.
6. Any tag present in the workspace but absent from the live version, with
   the date the workspace entry was last changed.

## How to report it

One table of findings, worst first, each row naming the tag by its GTM name
and its numeric id. Then the one sentence a reader needs: how many tags are
live, how many fire on consent, and how many of those would double count.
State the counts so they add up. Never edit, never publish, never delete.
"""


@dataclass(frozen=True)
class SkillManifest:
    name: str
    description: str
    allowed_tools: tuple[str, ...]
    frontmatter: str
    body: str
    manifest: str
    install_path: str
    api_base: str
    scope: str

    @property
    def is_read_only(self) -> bool:
        """The audit promise, checked against the manifest text itself."""
        lowered = self.manifest.lower()
        return ("never publishes" in lowered or "never publish" in lowered) \
            and "delete" not in self.body.lower().split("never edit")[0]


def generate_claude_skill_manifest() -> SkillManifest:
    """The copyable Claude Code skill definition for GTM container auditing.

    Returned parsed as well as whole, so a page can show the frontmatter and
    the body separately and a test can assert on the fields rather than on a
    substring of one long string.
    """
    parts = SKILL_MANIFEST.split("---\n")
    frontmatter = parts[1]
    body = "---\n".join(parts[2:]).strip()
    description = " ".join(
        line.strip() for line in
        re.search(r"description: >-\n(.*?)\nallowed-tools:", frontmatter,
                  re.S).group(1).splitlines()).strip()
    tools = tuple(t.strip() for t in
                  re.search(r"allowed-tools: (.+)", frontmatter).group(1).split(","))
    return SkillManifest(
        name=SKILL_NAME,
        description=description,
        allowed_tools=tools,
        frontmatter=frontmatter.strip(),
        body=body,
        manifest=SKILL_MANIFEST,
        install_path=f".claude/skills/{SKILL_NAME}/SKILL.md",
        api_base=GTM_API_BASE,
        scope=GTM_READONLY_SCOPE,
    )
