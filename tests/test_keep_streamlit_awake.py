"""Ground truth tests for the Streamlit keep awake prober.

Only the pure logic is covered here: URL building, the app list, reading the
tool registry without importing it, and the verdict the workflow is parsed
for. The browser half is exercised for real by the workflow itself, against
the live app, and saves a screenshot as evidence.

Run either way:
    python3 -m pytest tests/test_keep_streamlit_awake.py -q
    python3 tests/test_keep_streamlit_awake.py

Pass marker under pytest: every test passes, zero errors, exit code 0.
Pass marker under python3: final line is exactly
"KEEPALIVE UNIT RESULT: PASS <n>/<n>" and exit code 0.
Fail marker: any line starting "FAIL", plus exit code 1.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import keep_streamlit_awake as ka  # noqa: E402


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------

def test_a_bare_host_becomes_a_full_https_url():
    assert ka.normalize_app_url("proofs-toolbench.streamlit.app") == \
        "https://proofs-toolbench.streamlit.app"
    assert ka.normalize_app_url("https://proofs-toolbench.streamlit.app/") == \
        "https://proofs-toolbench.streamlit.app"
    assert ka.normalize_app_url("  https://x.streamlit.app///  ") == "https://x.streamlit.app"
    assert ka.normalize_app_url("") == ""


def test_the_health_url_is_the_one_that_cannot_lie():
    # The root URL answers 200 from a static shell even while the app sleeps,
    # so the prober has to ask the Streamlit health endpoint instead.
    assert ka.health_url("proofs-toolbench.streamlit.app") == \
        "https://proofs-toolbench.streamlit.app/~/+/_stcore/health"
    assert ka.HEALTH_PATH.startswith(ka.APP_FRAME_PATH)


def test_a_tool_url_is_the_app_url_plus_the_registry_key():
    app = "https://proofs-toolbench.streamlit.app"
    assert ka.tool_url(app, "pipedrive-integration-engine") == \
        "https://proofs-toolbench.streamlit.app/pipedrive-integration-engine"
    assert ka.tool_url(app + "/", "/wp-form-debugger/") == \
        "https://proofs-toolbench.streamlit.app/wp-form-debugger"


# ---------------------------------------------------------------------------
# The app list
# ---------------------------------------------------------------------------

def test_the_app_list_ignores_comments_and_blank_lines():
    text = """
# every app that must never sleep
https://one.streamlit.app

two.streamlit.app   # a bare host is fine

https://one.streamlit.app
"""
    assert ka.parse_app_list(text) == ["https://one.streamlit.app",
                                       "https://two.streamlit.app"]


def test_an_empty_or_missing_list_falls_back_to_the_hub():
    assert ka.parse_app_list("") == []
    assert ka.parse_app_list("# only a comment\n") == []
    assert ka.load_apps(Path("/nonexistent/apps.txt")) == list(ka.FALLBACK_APPS)
    assert ka.load_apps(None) == list(ka.FALLBACK_APPS)


def test_the_shipped_app_list_names_the_hub():
    apps = ka.load_apps(ROOT / ".github" / "streamlit-apps.txt")
    assert "https://proofs-toolbench.streamlit.app" in apps


# ---------------------------------------------------------------------------
# Reading the registry without importing it
# ---------------------------------------------------------------------------

def test_tool_keys_are_read_without_importing_streamlit():
    source = (ROOT / "tools" / "registry.py").read_text()
    tools = ka.extract_tools(source)
    keys = [key for key, _ in tools]
    assert "wp-form-debugger" in keys
    assert "netsuite-hubspot-sync" in keys
    assert "pipedrive-integration-engine" in keys
    titles = dict(tools)
    assert titles["pipedrive-integration-engine"] == "Pipedrive API & Integration Console"
    # The prober must never import the registry: that would drag in Streamlit
    # and every tool page into a job whose only business is opening a browser.
    probe_source = (ROOT / "scripts" / "keep_streamlit_awake.py").read_text()
    assert "import streamlit" not in probe_source
    assert "from tools" not in probe_source


def test_a_registry_with_no_tools_reads_as_empty():
    assert ka.extract_tools("") == []
    assert ka.extract_tools("def all_tools():\n    return []\n") == []


def test_keys_and_titles_stay_paired_in_order():
    source = '''
        Tool(key="alpha", title="Alpha Tool", icon="a"),
        Tool(key="beta", title="Beta Tool", icon="b"),
    '''
    assert ka.extract_tools(source) == [("alpha", "Alpha Tool"),
                                        ("beta", "Beta Tool")]


# ---------------------------------------------------------------------------
# Telling a real tool page apart from a silent fallback
# ---------------------------------------------------------------------------

def test_the_fingerprint_survives_whitespace_and_case():
    assert ka.fingerprint("  Toolbench\n\n  Diagnostic tools  ") == \
        ka.fingerprint("toolbench Diagnostic tools")
    assert ka.fingerprint("") == ""
    # Only the opening of the column is signed, so a KPI ticking over further
    # down the page cannot make the same page look like a different one.
    shared = "Same opening. " + ("a" * 300)      # longer than the signature
    assert ka.fingerprint(shared + "1 ms") == ka.fingerprint(shared + "9 ms")


def test_a_tool_page_that_rendered_its_own_content_is_live():
    home = ka.fingerprint("Toolbench. Diagnostic tools built for client work.")
    assert ka.tool_is_live(
        "Pipedrive API and Integration Console. A Pipedrive stage change "
        "calls Sinch directly.", home) is True


def test_a_silent_fallback_to_the_landing_page_is_not_live():
    # Streamlit does not error on an unknown page path, it quietly serves the
    # default page, so an HTTP check sees success while the tool is absent.
    home_text = "Toolbench. Diagnostic tools built for client work."
    home = ka.fingerprint(home_text)
    assert ka.tool_is_live(home_text, home) is False
    assert ka.tool_is_live("   " + home_text.upper() + "  ", home) is False


def test_an_empty_main_column_is_never_counted_as_live():
    home = ka.fingerprint("Toolbench")
    assert ka.tool_is_live("", home) is False
    assert ka.tool_is_live("   \n  ", home) is False


def test_an_empty_main_column_is_unchecked_not_missing(monkeypatch):
    """Nothing painted in time is the prober's failure. Counting it as the
    landing page made one slow paint on a quiet branch positive staleness,
    which past the grace is a failure every three hours."""
    page = _FakePage([{"a", "b", "c"}], empty={"b"})
    results = _drive(monkeypatch, page)
    result = results[0]
    assert result.missing_tools == []
    assert result.unchecked_tools == ["b (empty main column)"]
    assert result.deploy_status == "UNPROVEN"
    result.stale_for = 96 * 3600
    assert not ka.past_grace(result, 3 * 3600)
    code, _ = ka.verdict(results, stale_is_failure=False,
                         stale_grace_seconds=3 * 3600)
    assert code == 0


def test_the_checker_reads_the_main_column_not_the_whole_body():
    # The sidebar lists every registered tool on every page, so a check over
    # the whole body would pass for a tool whose page never rendered.
    source = (ROOT / "scripts" / "keep_streamlit_awake.py").read_text()
    assert 'MAIN_SELECTOR = \'[data-testid="stMain"]\'' in source
    assert "inner_text(MAIN_SELECTOR" in source
    assert 'inner_text("body"' not in source


# ---------------------------------------------------------------------------
# The verdict a CI log is parsed for
# ---------------------------------------------------------------------------

def test_an_app_that_was_already_awake_passes():
    result = ka.AppResult(url="https://x.streamlit.app", reachable=True,
                          was_asleep=False, awake=True, health="ok")
    code, lines = ka.verdict([result])
    assert code == 0
    assert lines[-1] == "KEEPALIVE RESULT: PASS 1/1 apps awake"
    assert "already awake" in lines[0]


def test_an_app_that_had_to_be_woken_still_passes_and_says_so():
    result = ka.AppResult(url="https://x.streamlit.app", reachable=True,
                          was_asleep=True, awake=True, health="ok")
    code, lines = ka.verdict([result])
    assert code == 0
    assert "woken from sleep" in lines[0]
    assert lines[-1] == "KEEPALIVE RESULT: PASS 1/1 apps awake"


def test_an_app_that_never_rendered_fails_loudly():
    result = ka.AppResult(url="https://x.streamlit.app", reachable=True,
                          awake=False, error="the app never rendered: TimeoutError")
    code, lines = ka.verdict([result])
    assert code == 1
    assert any(line.strip().startswith("FAIL") for line in lines)
    assert lines[-1] == "KEEPALIVE RESULT: FAIL 1 of 1 apps, 1 not awake"


def test_a_stale_deploy_fails_and_names_the_missing_tools():
    # This is the case that used to need a manual reboot to notice: the tool
    # is in the repository but the running app has never picked it up.
    result = ka.AppResult(url="https://x.streamlit.app", reachable=True,
                          awake=True, health="ok",
                          missing_tools=["pipedrive-integration-engine "
                                         "(the live app fell back to the landing page)"])
    assert result.ok is False
    code, lines = ka.verdict([result])
    assert code == 1
    assert "registered tools missing from the live app" in lines[0]
    assert "pipedrive-integration-engine" in lines[0]


def test_a_tool_that_could_not_be_read_is_not_called_missing():
    # The first live run declared all seventeen tools missing from an app that
    # was serving every one of them, because a lookup timeout was recorded the
    # same way as a landing page fallback. A failed read means the prober did
    # not look, which is not evidence that the tool is absent.
    #
    # This test used to go one step further and assert the run PASSED, which
    # was the defect: not evidence of absence is not evidence of presence
    # either, so a run that read nothing certified a deploy it never saw. The
    # distinction the test is named for still holds and is what it checks now.
    result = ka.AppResult(url="https://x.streamlit.app", reachable=True,
                          awake=True, health="ok", tools_requested=19,
                          unchecked_tools=["wp-form-debugger (TimeoutError)"])
    assert result.missing_tools == [], (
        "an unreadable tool page must never be reported as missing")
    assert result.unproven, "the deploy is unproven, neither fresh nor stale"
    code, lines = ka.verdict([result])
    assert code == 1, "a run that verified nothing must not exit 0"
    body = "\n".join(lines)
    assert "could not read" in body, "the unread tool is not reported at all"
    assert "wp-form-debugger" in body
    assert "stale" in body and "UNPROVEN" in body, (
        "the report must say unproven rather than accusing the deploy")


def test_a_proved_stale_deploy_still_fails():
    # The distinction only earns its keep if the positive case still fails.
    result = ka.AppResult(url="https://x.streamlit.app", reachable=True,
                          awake=True, health="ok",
                          missing_tools=["new-tool (the live app fell back to "
                                         "the landing page)"],
                          unchecked_tools=["other-tool (TimeoutError)"])
    assert result.ok is False
    code, _ = ka.verdict([result])
    assert code == 1


def test_the_prober_waits_for_the_app_frame_before_querying_it():
    # Community Cloud creates the app frame after navigation resolves. Looking
    # it up immediately falls back to the outer shell, where no app selector
    # ever appears, so every query burns its whole timeout for nothing.
    source = (ROOT / "scripts" / "keep_streamlit_awake.py").read_text()
    assert "def wait_for_app_frame(" in source
    assert "frame = wait_for_app_frame(page)" in source, (
        "read_main does not wait for the app frame")
    # And the per tool budget must be well under the resolve timeout, or one
    # bad run costs half an hour.
    assert ka.TOOL_TIMEOUT_MS < ka.RESOLVE_TIMEOUT_MS
    assert ka.FRAME_TIMEOUT_MS <= ka.TOOL_TIMEOUT_MS


def test_one_bad_app_among_several_fails_the_whole_run():
    good = ka.AppResult(url="https://a.streamlit.app", reachable=True,
                        awake=True, health="ok")
    bad = ka.AppResult(url="https://b.streamlit.app", error="navigation failed")
    code, lines = ka.verdict([good, bad])
    assert code == 1
    assert lines[-1] == "KEEPALIVE RESULT: FAIL 1 of 2 apps, 1 not awake"


def test_the_selectors_match_what_community_cloud_actually_renders():
    # Both ids exist because Community Cloud shows a different wake button to
    # the owner than to an ordinary viewer, and the job runs as neither.
    assert "wakeup-button-viewer" in ka.SLEEP_SELECTOR
    assert "wakeup-button-owner" in ka.SLEEP_SELECTOR
    assert "stApp" in ka.AWAKE_SELECTOR
    # A cold boot genuinely takes minutes, so the wake wait must outlast it.
    assert ka.WAKE_TIMEOUT_MS >= 300_000
    # The dwell is what makes the visit count rather than read as a bounce.
    assert ka.DWELL_MS >= 10_000


# ---------------------------------------------------------------------------
# The workflow that runs it
# ---------------------------------------------------------------------------

def test_the_workflow_is_valid_yaml_and_scheduled_often_enough():
    import yaml  # ships with the Actions runner and with most environments

    raw = (ROOT / ".github" / "workflows" / "keep-awake.yml").read_text()
    data = yaml.safe_load(raw)
    # PyYAML reads a bare `on:` key as the boolean True, so accept either.
    triggers = data.get("on", data.get(True))
    assert triggers is not None, "the workflow declares no triggers"
    crons = [entry["cron"] for entry in triggers["schedule"]]
    assert crons, "the workflow is not scheduled at all"
    # The property is the interval, not one exact string. Pinning the string
    # made this test refuse a change that was made on GitHub's own advice.
    minutes, hours = crons[0].split()[0], crons[0].split()[1]
    assert hours.startswith("*/"), f"not an every N hours schedule: {crons}"
    every = int(hours[2:])
    assert 1 <= every <= 3, (
        f"a wake every {every} hours leaves too long a gap before Community "
        f"Cloud sleeps the app: {crons}")
    # And not on the hour. GitHub documents that the schedule event is delayed
    # under high load, that load peaks at the start of every hour, and that
    # queued jobs may be dropped then. A dropped wake is a sleeping app.
    assert minutes.isdigit() and int(minutes) != 0, (
        f"the wake is scheduled on the busiest minute of the hour: {crons}")
    assert "workflow_dispatch" in triggers
    assert "push" in triggers, "a push must wake the app so a new tool shows up"
    # Read only credentials: this job has no business writing to the repository.
    assert data["permissions"] == {"contents": "read"}
    steps = data["jobs"]["wake"]["steps"]
    assert any("--verify-tools" in str(step.get("run", "")) for step in steps)


def test_no_em_or_en_dashes_in_the_new_files():
    # Spelled by code point so the detector itself carries no dash.
    banned = (chr(8212), chr(8211))
    for name in ("scripts/keep_streamlit_awake.py",
                 ".github/workflows/keep-awake.yml",
                 ".github/streamlit-apps.txt",
                 "tests/test_keep_streamlit_awake.py"):
        text = (ROOT / name).read_text()
        for bad in banned:
            assert bad not in text, f"a dash character reached {name}"


if __name__ == "__main__":
    tests = [(name, obj) for name, obj in sorted(globals().items())
             if name.startswith("test_") and callable(obj)]
    failures: list[str] = []
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            print(f"  FAIL {name}: {exc}")
            failures.append(name)
    print()
    if failures:
        print(f"KEEPALIVE UNIT RESULT: FAIL {len(failures)} of {len(tests)} checks failed")
        for name in failures:
            print(f"  - {name}")
        sys.exit(1)
    print(f"KEEPALIVE UNIT RESULT: PASS {len(tests)}/{len(tests)}")
    sys.exit(0)


# ---------------------------------------------------------------------------
# A prober that read nothing has not proved anything
# ---------------------------------------------------------------------------

APP = "https://proofs-toolbench.streamlit.app"


def test_a_run_that_could_not_read_one_tool_does_not_report_pass():
    """The confirmed defect: unchecked tools were ignored by the verdict.

    A failed read is not evidence that a tool is absent. It is not evidence
    that it is present either, and only the first half was implemented, so a
    run in which every single read timed out found nothing missing and
    certified the deploy as current.
    """
    every_read_failed = ka.AppResult(
        url=APP, reachable=True, awake=True, health="ok", tools_requested=19,
        unchecked_tools=[f"tool-{i} (TimeoutError)" for i in range(19)])
    assert not every_read_failed.ok
    assert every_read_failed.unproven
    code, lines = ka.verdict([every_read_failed])
    assert code == 1, "a run that read nothing exited 0"
    assert "UNPROVEN" in "\n".join(lines)


def test_one_unreadable_tool_is_still_not_a_pass():
    result = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                          tools_requested=19,
                          unchecked_tools=["askew-suit-engine (TimeoutError)"])
    assert not result.ok
    assert ka.verdict([result])[0] == 1


def test_an_unproven_deploy_is_not_reported_as_a_sleeping_app():
    """Two different failures. Saying the wrong one sends someone to the wrong
    place: the app in this case is awake and serving."""
    unproven = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                            tools_requested=19,
                            unchecked_tools=["a (TimeoutError)"])
    asleep = ka.AppResult(url=APP, error="the app never rendered")
    assert "awake but unproven" in ka.verdict([unproven])[1][-1]
    assert "not awake" in ka.verdict([asleep])[1][-1]


def test_a_genuinely_stale_deploy_still_fails_as_stale_not_unproven():
    stale = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=19,
                         missing_tools=["pipedrive-integration-engine (landing page)"])
    assert not stale.ok
    assert not stale.unproven, "a confirmed missing tool is stale, not unproven"
    assert "missing from the live app" in "\n".join(ka.verdict([stale])[1])


def test_every_tool_confirmed_is_the_only_thing_that_passes():
    clean = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=19)
    assert clean.ok
    code, lines = ka.verdict([clean])
    assert code == 0
    assert "all 19 tools confirmed" in "\n".join(lines)


def test_a_wake_only_run_does_not_demand_tool_verification():
    """Without --verify-tools nothing was asked about tools, so nothing is
    owed about them."""
    wake_only = ka.AppResult(url=APP, reachable=True, awake=True, health="ok")
    assert wake_only.tools_requested == 0
    assert wake_only.ok
    assert ka.verdict([wake_only])[0] == 0


# ---------------------------------------------------------------------------
# Two questions, two exit codes: the warning mode for the schedule
# ---------------------------------------------------------------------------
#
# One job answered "is the app awake" and "is the deploy current" with one
# exit code, and the result was a failure email every three hours for a
# stale deploy that was already known and had not changed. These pin the
# split. Strict mode keeps its exit codes and its pass marker exactly; the
# wording of one failure bucket changed on purpose, and a status line was
# added, both pinned below. Warning mode passes on an awake app while still
# saying, loudly and parseably, that the deploy is stale.


def test_strict_mode_is_the_default_and_keeps_its_exit_code():
    stale = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=33,
                         missing_tools=["irish-wine-distribution-console "
                                        "(the live app fell back to the landing page)"])
    code, lines = ka.verdict([stale])
    assert code == 1
    assert lines[0].strip().startswith("FAIL")
    assert lines[-1].startswith("KEEPALIVE RESULT: FAIL 1 of 1 apps")


def test_a_stale_deploy_is_a_warning_not_a_failure_in_warning_mode():
    stale = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=33,
                         missing_tools=["irish-wine-distribution-console "
                                        "(the live app fell back to the landing page)"])
    code, lines = ka.verdict([stale], stale_is_failure=False)
    assert code == 0, "an awake app with a stale deploy must not fail the wake"
    assert lines[0].strip().startswith("WARN "), lines[0]
    assert "irish-wine-distribution-console" in lines[0]
    assert lines[-1] == "KEEPALIVE RESULT: PASS 1/1 apps awake"


def test_the_stale_verdict_no_longer_says_not_awake_about_a_running_app():
    """Run 33 printed "1 not awake" for an app that was awake and serving 32
    of 33 tools. That sent the reader to the wrong place."""
    stale = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=33, missing_tools=["x (landing page)"])
    _, lines = ka.verdict([stale])
    assert "awake but stale" in lines[-1]
    assert "not awake" not in lines[-1]


def test_a_down_app_still_fails_in_warning_mode():
    """Warning mode relaxes the deploy question only. The app being down is
    the whole reason the schedule exists."""
    down = ka.AppResult(url=APP, reachable=True, awake=False,
                        error="the app never rendered: TimeoutError")
    code, lines = ka.verdict([down], stale_is_failure=False)
    assert code == 1
    assert "not awake" in lines[-1]


def test_an_unproven_deploy_is_also_a_warning_in_warning_mode():
    unproven = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                            tools_requested=33,
                            unchecked_tools=["a (TimeoutError)"])
    code, lines = ka.verdict([unproven], stale_is_failure=False)
    assert code == 0
    assert lines[0].strip().startswith("WARN ")
    assert "UNPROVEN" in "\n".join(lines)


def test_a_positively_stale_deploy_past_the_grace_fails_in_warning_mode():
    """The one deploy condition worth an email: stuck. Community Cloud has
    applied every push here within about two hours on its own, so a change
    still unapplied past three is not going to fix itself."""
    stuck = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=33, missing_tools=["x (landing page)"],
                         stale_for=4 * 3600)
    assert ka.past_grace(stuck, 3 * 3600)
    code, lines = ka.verdict([stuck], stale_is_failure=False,
                             stale_grace_seconds=3 * 3600)
    assert code == 1
    assert "past the 3 hour grace" in lines[0]
    assert "4.0 hours" in lines[0]
    assert "Reboot" in lines[0]


def test_a_build_mismatch_past_the_grace_also_counts_as_stuck():
    stuck = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=33, live_build="c06bbb6",
                         expected_build="8ef505e" + "0" * 33, stale_for=4 * 3600)
    assert stuck.deploy_status == "STALE"
    assert ka.past_grace(stuck, 3 * 3600)


def test_an_unproven_deploy_never_escalates_however_old():
    """The first draft escalated on this, and one flaky read on a quiet
    branch brought the every three hours email straight back."""
    flaky = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=33, unchecked_tools=["a (TimeoutError)"],
                         stale_for=30 * 24 * 3600)
    assert flaky.deploy_status == "UNPROVEN"
    assert not ka.past_grace(flaky, 3 * 3600)
    code, lines = ka.verdict([flaky], stale_is_failure=False,
                             stale_grace_seconds=3 * 3600)
    assert code == 0
    assert lines[0].strip().startswith("WARN ")


def test_a_stale_deploy_within_the_grace_stays_a_warning():
    young = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=33, missing_tools=["x (landing page)"],
                         stale_for=90 * 60)
    assert not ka.past_grace(young, 3 * 3600)
    code, lines = ka.verdict([young], stale_is_failure=False,
                             stale_grace_seconds=3 * 3600)
    assert code == 0
    assert lines[0].strip().startswith("WARN ")


def test_the_grace_needs_an_age_and_a_grace_to_act_on():
    stale = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=33, missing_tools=["x (landing page)"])
    assert not ka.past_grace(stale, 1)                      # no age known
    stale.stale_for = 10 ** 6
    assert not ka.past_grace(stale, None)                   # no grace set
    assert ka.verdict([stale], stale_is_failure=False)[0] == 0


def test_the_grace_never_touches_strict_mode_or_a_current_deploy():
    clean = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=33, stale_for=10 ** 6)
    assert not ka.past_grace(clean, 1)
    assert ka.verdict([clean], stale_grace_seconds=1)[0] == 0


def test_a_current_deploy_passes_identically_in_both_modes():
    clean = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=33)
    strict = ka.verdict([clean])
    lenient = ka.verdict([clean], stale_is_failure=False)
    assert strict == lenient
    assert strict[0] == 0


# ---------------------------------------------------------------------------
# Two states that are not "stale"
# ---------------------------------------------------------------------------


def test_every_tool_missing_is_down_not_stale():
    """A stale deploy misses the newest tool or two. An app missing every one
    of them is serving no tools: a broken build, and down in both modes. The
    strict schedule used to fail this; the warning mode must not let it
    through as a warning."""
    broken = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                          tools_requested=3, error=ka.BROKEN_APP,
                          missing_tools=[f"t{i} (landing page)" for i in range(3)])
    assert not broken.up
    assert broken.deploy_status == "UNCHECKED"
    for mode in (True, False):
        code, lines = ka.verdict([broken], stale_is_failure=mode)
        assert code == 1
        assert "not serving tools" in lines[0]


def test_a_build_mismatch_is_stale_even_when_every_tool_resolves():
    """A push that fixes an existing tool adds no registry key, so the tool
    check alone certifies it deployed while the app runs the old code. The
    sidebar label is the one signal that catches it."""
    stale = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=33, live_build="d611e14",
                         expected_build="c06bbb632333dc35abb65fe8c3d4e014f2426708")
    assert stale.build_mismatch
    assert stale.deploy_status == "STALE"
    assert not stale.ok
    code, lines = ka.verdict([stale])
    assert code == 1
    assert "reports build d611e14" in lines[0]
    assert "deploy branch is at c06bbb6" in lines[0]
    code, lines = ka.verdict([stale], stale_is_failure=False)
    assert code == 0
    assert lines[0].strip().startswith("WARN ")


def test_a_matching_build_is_stated_but_never_claimed_as_proof():
    """The label is read from the checkout at render time, and a git pull
    updates the checkout before the process reloads, so a match proves
    nothing. Only the mismatch is acted on."""
    clean = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=33, live_build="c06bbb6",
                         expected_build="c06bbb632333dc35abb65fe8c3d4e014f2426708")
    assert not clean.build_mismatch
    assert clean.ok
    _, lines = ka.verdict([clean])
    status = next(l for l in lines if l.startswith("DEPLOY STATUS:"))
    assert "live app reports build c06bbb6" in status
    assert "matches" not in status


def test_an_unknown_or_unread_build_is_never_a_mismatch():
    for label in ("", "unknown"):
        result = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                              tools_requested=33, live_build=label,
                              expected_build="c06bbb6")
        assert not result.build_mismatch
        assert result.ok


def test_no_expected_build_means_no_mismatch():
    result = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                          tools_requested=33, live_build="d611e14")
    assert not result.build_mismatch


# ---------------------------------------------------------------------------
# The deploy status line, parseable on its own
# ---------------------------------------------------------------------------


def test_the_deploy_status_line_is_printed_whenever_tools_were_asked_about():
    clean = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=33)
    _, lines = ka.verdict([clean])
    status = [l for l in lines if l.startswith("DEPLOY STATUS:")]
    assert status == ["DEPLOY STATUS: CURRENT, all 33 tools confirmed"]


def test_the_deploy_status_line_is_absent_on_a_wake_only_run():
    wake_only = ka.AppResult(url=APP, reachable=True, awake=True, health="ok")
    _, lines = ka.verdict([wake_only])
    assert not any(l.startswith("DEPLOY STATUS:") for l in lines)


def test_the_deploy_status_line_names_stale_and_the_missing_tool():
    stale = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=33,
                         missing_tools=["irish-wine-distribution-console (landing page)"])
    _, lines = ka.verdict([stale], stale_is_failure=False)
    status = next(l for l in lines if l.startswith("DEPLOY STATUS:"))
    assert status.startswith("DEPLOY STATUS: STALE,")
    assert "irish-wine-distribution-console" in status


def test_the_deploy_status_line_never_says_current_for_an_app_that_never_rendered():
    """It did. run() records tools_requested before looking, so an app that
    never rendered had empty lists and read as CURRENT in the line and the
    JSON. Nothing was checked, and the line now says so."""
    down = ka.AppResult(url=APP, reachable=True, awake=False, tools_requested=33,
                        error="the app never rendered: TimeoutError")
    assert down.deploy_status == "UNCHECKED"
    _, lines = ka.verdict([down])
    status = next(l for l in lines if l.startswith("DEPLOY STATUS:"))
    assert status.startswith("DEPLOY STATUS: UNCHECKED,")
    assert "CURRENT" not in status


def test_the_deploy_status_line_comes_before_the_result_marker():
    """lines[-1] is the KEEPALIVE RESULT marker and stays so. The deploy line
    sits above the blank line that precedes it."""
    stale = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=33, missing_tools=["x (landing page)"])
    _, lines = ka.verdict([stale])
    assert lines[-1].startswith("KEEPALIVE RESULT:")
    assert lines[-2] == ""
    assert lines[-3].startswith("DEPLOY STATUS:")


def test_the_deploy_status_line_counts_the_retries():
    late = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                        tools_requested=33, retries=4)
    _, lines = ka.verdict([late])
    status = next(l for l in lines if l.startswith("DEPLOY STATUS:"))
    assert "(after 4 retries)" in status


def test_unchecked_tools_do_not_vanish_from_a_stale_report():
    """Both lists non empty used to print only the missing one, so the
    reader could not tell how much of the deploy was actually examined."""
    both = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                        tools_requested=33, missing_tools=["x (landing page)"],
                        unchecked_tools=["a (TimeoutError)", "b (TimeoutError)"])
    _, lines = ka.verdict([both])
    assert "2 tool page(s) could not be read: a (TimeoutError), b (TimeoutError)" in lines[0]


def test_deploy_status_property_has_four_values_and_no_boolean():
    assert ka.AppResult(url=APP).deploy_status == "UNCHECKED"
    assert ka.AppResult(url=APP, awake=True, tools_requested=3).deploy_status == "CURRENT"
    assert ka.AppResult(url=APP, awake=True, tools_requested=3,
                        unchecked_tools=["a (x)"]).deploy_status == "UNPROVEN"
    assert ka.AppResult(url=APP, awake=True, tools_requested=3,
                        missing_tools=["a (x)"],
                        unchecked_tools=["b (x)"]).deploy_status == "STALE"


# ---------------------------------------------------------------------------
# The live build label
# ---------------------------------------------------------------------------


def test_the_build_label_is_read_from_the_sidebar_text():
    text = "Toolbench\nDiagnostic tools for client work.\nbuild d611e14\n"
    assert ka.parse_build_label(text) == "d611e14"


def test_an_app_that_does_not_know_its_build_says_unknown():
    assert ka.parse_build_label("build unknown") == "unknown"


def test_no_label_reads_as_empty_not_as_a_guess():
    assert ka.parse_build_label("How to use\n1. Pick a tool") == ""
    assert ka.parse_build_label("") == ""


def test_a_full_sha_is_accepted_too():
    assert ka.parse_build_label("build " + "a" * 40) == "a" * 40


# ---------------------------------------------------------------------------
# Retrying only what is not yet confirmed
# ---------------------------------------------------------------------------


class _FakePage:
    """Enough of a Playwright page for run() to drive.

    The live app "gains" tools over successive looks, which is exactly what
    Community Cloud applying a push late looks like from outside. A key in
    timeouts_by_look makes that tool's read raise on that look.
    """

    def __init__(self, live_by_look, build="abc1234", timeouts_by_look=None,
                 build_by_look=None, empty=(), read_cost=0.0):
        self.live_by_look = live_by_look
        self.timeouts_by_look = timeouts_by_look or {}
        self.build_by_look = build_by_look
        self.look = 0
        self.build = build
        self.visited: list[str] = []
        self.url = ""
        self.frames: list = []
        # Tool keys whose main column paints nothing.
        self.empty = set(empty)
        # Seconds every tool read costs on the fake clock, set by _drive.
        self.read_cost = read_cost
        self.tick = lambda seconds: None

    def _live(self):
        return self.live_by_look[min(self.look, len(self.live_by_look) - 1)]

    def goto(self, url, **_):
        self.url = url
        self.visited.append(url)

    def wait_for_selector(self, selector, **_):
        if "wakeup-button" in selector:
            raise RuntimeError("no sleep screen")
        key = self.url.rsplit("/", 1)[-1]
        if "stMain" in selector and key in self.timeouts_by_look.get(self.look, set()):
            raise TimeoutError(f"{key} timed out on look {self.look}")
        return object()

    def query_selector(self, selector):
        return object()

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        raise AssertionError("never clicked")

    def screenshot(self, **_):
        pass

    def inner_text(self, selector, **_):
        if "stSidebar" in selector:
            build = self.build
            if self.build_by_look:
                build = self.build_by_look[min(self.look, len(self.build_by_look) - 1)]
            return f"Toolbench\nbuild {build}"
        key = self.url.rsplit("/", 1)[-1]
        self.tick(self.read_cost)
        if key in self.empty:
            return "   \n  "
        if key in self._live():
            return f"{key} page content that differs from home"
        return "Toolbench home landing page"


class _FakeDriver:
    def __init__(self, page):
        self.page = page

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    @property
    def chromium(self):
        page = self.page

        class _Browser:
            def new_context(self, **_):
                class _Ctx:
                    def new_page(self_inner):
                        return page

                    def close(self_inner):
                        pass
                return _Ctx()

            def close(self):
                pass

        class _Chromium:
            def launch(self, **_):
                return _Browser()
        return _Chromium()


def _drive(monkeypatch, page, tools=(("a", "A"), ("b", "B"), ("c", "C")), **kwargs):
    import types
    fake_pw = types.SimpleNamespace(sync_playwright=lambda: _FakeDriver(page))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_pw)
    monkeypatch.setitem(sys.modules, "playwright", types.SimpleNamespace(sync_api=fake_pw))
    monkeypatch.setattr(ka, "read_health", lambda *_: "ok")
    monkeypatch.setattr(ka, "DWELL_MS", 0)
    ticks = {"t": 0.0}

    def clock():
        return ticks["t"]

    def sleep(seconds):
        ticks["t"] += seconds
        page.look += 1

    def tick(seconds):
        ticks["t"] += seconds

    page.tick = tick
    return ka.run([APP], list(tools), None, "", sleep=sleep, clock=clock, **kwargs)


def test_a_pass_stops_at_its_budget_and_leaves_the_rest_unchecked(monkeypatch):
    page = _FakePage([{"a", "b", "c"}], read_cost=100)
    results = _drive(monkeypatch, page, pass_budget=150)
    result = results[0]
    # a at t=0, b at t=100, then the budget is spent before c.
    assert result.missing_tools == []
    assert result.unchecked_tools == ["c (pass budget of 150s spent)"]
    assert result.deploy_status == "UNPROVEN"
    result.stale_for = 96 * 3600
    assert not ka.past_grace(result, 3 * 3600)


def test_no_budget_means_every_tool_is_read(monkeypatch):
    page = _FakePage([{"a", "b", "c"}], read_cost=100)
    results = _drive(monkeypatch, page)
    assert results[0].unchecked_tools == []
    assert results[0].deploy_status == "CURRENT"


def test_a_label_on_no_known_commit_is_unproven_not_stale(monkeypatch):
    """Community Cloud can be running a merge it made on its own side, or a
    detached checkout. That label is on no branch and proves nothing about
    whether the code is old. Counting it as proof failed every scheduled run
    until a Reboot."""
    page = _FakePage([{"a", "b", "c"}], build="abcdef0")
    results = _drive(monkeypatch, page, expect_build="1" * 40,
                     known_builds=("1" * 40, "2" * 40, "0ad0000" + "0" * 33))
    result = results[0]
    assert not result.build_mismatch
    assert result.build_unrecognised
    assert result.deploy_status == "UNPROVEN"
    result.stale_for = 96 * 3600
    assert not ka.past_grace(result, 3 * 3600)
    code, lines = ka.verdict(results, stale_is_failure=False,
                             stale_grace_seconds=3 * 3600)
    assert code == 0
    assert "neither the deploy branch head" in lines[0]


def test_a_label_on_a_known_older_commit_is_still_stale(monkeypatch):
    page = _FakePage([{"a", "b", "c"}], build="0ad0000")
    results = _drive(monkeypatch, page, expect_build="1" * 40,
                     known_builds=("1" * 40, "0ad0000" + "0" * 33))
    result = results[0]
    assert result.build_mismatch
    assert not result.build_unrecognised
    assert result.deploy_status == "STALE"
    result.stale_for = 4 * 3600
    assert ka.past_grace(result, 3 * 3600)


def test_without_a_history_any_other_label_is_a_mismatch():
    """The deploy-current job has no history and needs none: a minute after
    a push any label that is not the head means not applied yet, and that
    job never escalates."""
    result = ka.AppResult(url=APP, awake=True, tools_requested=3,
                          live_build="abcdef0", expected_build="1" * 40)
    assert result.build_mismatch
    assert not result.build_unrecognised
    assert result.deploy_status == "STALE"


def test_known_builds_are_read_from_a_git_log_file(tmp_path):
    path = tmp_path / "known.txt"
    path.write_text("1" * 40 + "\n" + "0AD0000" + "0" * 33 + "\nnot a sha\n\n")
    assert ka.load_known_builds(path) == ("1" * 40, "0ad0000" + "0" * 33)
    assert ka.load_known_builds(tmp_path / "absent.txt") == ()


def test_a_blank_age_is_unknown_not_a_usage_error():
    assert ka._optional_float("") is None
    assert ka._optional_float("  ") is None
    assert ka._optional_float("1700000000") == 1700000000.0


def _main_with(monkeypatch, tmp_path, results, argv):
    monkeypatch.chdir(tmp_path)
    apps = tmp_path / "apps.txt"
    apps.write_text(APP + "\n")
    registry = tmp_path / "registry.py"
    registry.write_text('Tool(key="a", title="A")\n')
    monkeypatch.setattr(ka, "run", lambda *a, **k: results)
    code = ka.main(["--apps", str(apps), "--registry", str(registry),
                    "--verify-tools", "--tools-as-warnings"] + argv)
    summary = json.loads((tmp_path / "keepalive-summary.json").read_text())
    return code, summary


def test_the_schedule_escalates_a_stuck_deploy_end_to_end(monkeypatch, tmp_path):
    """Through main(), not verdict(): the age must reach the results and the
    grace must reach the verdict, or the design's whole point is untested."""
    stale = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=1, missing_tools=["a (landing page)"])
    four_hours_ago = str(int(time.time()) - 4 * 3600)
    code, summary = _main_with(monkeypatch, tmp_path, [stale],
                               ["--deployed-at", four_hours_ago,
                                "--stale-grace-seconds", "10800"])
    assert code == 1
    app = summary["apps"][0]
    assert app["deploy_status"] == "STALE"
    assert app["stale_for_seconds"] >= 4 * 3600 - 5


def test_the_schedule_warns_inside_the_grace_end_to_end(monkeypatch, tmp_path):
    stale = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=1, missing_tools=["a (landing page)"])
    one_hour_ago = str(int(time.time()) - 3600)
    code, summary = _main_with(monkeypatch, tmp_path, [stale],
                               ["--deployed-at", one_hour_ago,
                                "--stale-grace-seconds", "10800"])
    assert code == 0
    assert summary["apps"][0]["deploy_status"] == "STALE"


def test_a_blank_age_on_the_command_line_is_not_exit_2(monkeypatch, tmp_path):
    clean = ka.AppResult(url=APP, reachable=True, awake=True, health="ok",
                         tools_requested=1)
    code, summary = _main_with(monkeypatch, tmp_path, [clean],
                               ["--deployed-at", "", "--stale-grace-seconds", "10800"])
    assert code == 0
    assert summary["apps"][0]["stale_for_seconds"] is None


def test_a_late_deploy_is_confirmed_by_retrying(monkeypatch):
    # Look 0: only a live. Look 1: a and b. Look 2: all three.
    page = _FakePage([{"a"}, {"a", "b"}, {"a", "b", "c"}])
    results = _drive(monkeypatch, page, retry_until_current=300, retry_every=60)
    result = results[0]
    assert result.missing_tools == []
    assert result.retries == 2
    assert result.ok


def test_retries_only_look_at_the_tools_not_yet_confirmed(monkeypatch):
    page = _FakePage([{"a"}, {"a", "b", "c"}])
    _drive(monkeypatch, page, retry_until_current=300, retry_every=60)
    tool_visits = [u.rsplit("/", 1)[-1] for u in page.visited if u != APP]
    # First pass reads a, b, c. The retry reads only b and c.
    assert tool_visits == ["a", "b", "c", "b", "c"]


def test_the_retry_budget_is_honoured_and_the_last_wait_is_clamped(monkeypatch):
    # One tool live throughout, two never appear. Not all three: an app
    # missing every tool is classed as broken rather than stale, on purpose.
    page = _FakePage([{"a"}])
    results = _drive(monkeypatch, page, retry_until_current=150, retry_every=60)
    result = results[0]
    # Waits of 60, 60 and then 30, so the last look lands exactly at the
    # deadline rather than a full interval past it.
    assert result.retries == 3
    assert result.deploy_status == "STALE"
    assert len(result.missing_tools) == 2


def test_a_single_look_when_no_retry_budget_is_given(monkeypatch):
    page = _FakePage([{"a"}, {"a", "b", "c"}])
    results = _drive(monkeypatch, page)
    assert results[0].retries == 0
    assert results[0].deploy_status == "STALE"


def test_the_live_build_is_read_and_carried(monkeypatch):
    page = _FakePage([{"a", "b", "c"}], build="c06bbb6")
    results = _drive(monkeypatch, page)
    assert results[0].live_build == "c06bbb6"


def test_a_tool_proved_missing_stays_missing_when_its_re_read_times_out(monkeypatch):
    """A timeout is not a confirmation. Letting it downgrade a proved STALE
    into an UNPROVEN was a real bug."""
    page = _FakePage([set(), set()], timeouts_by_look={1: {"c"}})
    results = _drive(monkeypatch, page, retry_until_current=60, retry_every=60)
    result = results[0]
    assert result.retries == 1
    assert "c (the live app fell back to the landing page)" in result.missing_tools
    assert not any(e.startswith("c ") for e in result.unchecked_tools)


def test_merge_passes_keeps_proof_and_drops_nothing_silently():
    prev = ["a (landing page)", "b (landing page)"]
    missing, unchecked = ka._merge_passes(prev, ["a (landing page)"],
                                          ["b (TimeoutError)", "c (TimeoutError)"])
    assert missing == ["a (landing page)", "b (landing page)"]
    assert unchecked == ["c (TimeoutError)"]


def test_the_retry_loop_waits_for_the_build_label_when_tools_resolve(monkeypatch):
    """Every key resolves but the label is behind: that is stale, and the
    loop keeps looking until the label catches up."""
    page = _FakePage([{"a", "b", "c"}], build_by_look=["0ad0000", "1ce1111"])
    results = _drive(monkeypatch, page, retry_until_current=120, retry_every=60,
                     expect_build="1ce1111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    result = results[0]
    assert result.retries == 1
    assert result.live_build == "1ce1111"
    assert not result.build_mismatch
    assert result.ok


def test_the_build_label_read_on_a_retry_is_the_fresh_one(monkeypatch):
    """It was read before the navigation, so the pass that confirmed the tools
    reported the build from the pass before."""
    page = _FakePage([set(), {"a", "b", "c"}], build_by_look=["0ad0000", "1ce1111"])
    results = _drive(monkeypatch, page, retry_until_current=60, retry_every=60,
                     expect_build="1ce1111aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert results[0].live_build == "1ce1111"
    assert results[0].ok


def test_an_app_missing_every_tool_is_reported_down(monkeypatch):
    page = _FakePage([set()])
    results = _drive(monkeypatch, page)
    result = results[0]
    assert result.error == ka.BROKEN_APP
    assert not result.up
    assert ka.verdict(results, stale_is_failure=False)[0] == 1


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------


def test_fail_lines_are_error_annotations_and_warn_lines_are_warnings(capsys):
    ka.emit_annotations(["  FAIL x stale", "  WARN y slow"])
    out = capsys.readouterr().out.splitlines()
    assert out == ["::error::x stale", "::warning::y slow"]


def test_the_soft_annotation_flag_went_with_its_only_caller():
    source = (ROOT / "scripts" / "keep_streamlit_awake.py").read_text()
    assert "soft-annotations" not in source
    assert "soft=" not in source


def test_nothing_is_annotated_on_a_clean_run(capsys):
    _, lines = ka.verdict([ka.AppResult(url=APP, reachable=True, awake=True, health="ok")])
    ka.emit_annotations(lines)
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# The two job workflow
# ---------------------------------------------------------------------------


def _workflow():
    import yaml
    return yaml.safe_load(
        (ROOT / ".github" / "workflows" / "keep-awake.yml").read_text())


def _step(job, step_id):
    return next(s for s in _workflow()["jobs"][job]["steps"] if s.get("id") == step_id)


def _wake_run() -> str:
    steps = _workflow()["jobs"]["wake"]["steps"]
    return next(s for s in steps
                if "keep_streamlit_awake.py" in str(s.get("run", "")))["run"]


def _enclosing_if(script: str, needle: str) -> str:
    """The `if [ ... ]; then` line that guards the line holding needle, or ""."""
    lines = script.splitlines()
    index = next(i for i, line in enumerate(lines) if needle in line)
    depth = 0
    for line in reversed(lines[:index]):
        text = line.strip()
        if text == "fi":
            depth += 1
        elif text.startswith("if ") and text.endswith("then"):
            if depth == 0:
                return text
            depth -= 1
    return ""


def test_the_wake_job_reports_a_stale_deploy_as_a_warning_with_a_grace():
    run = _wake_run()
    assert "--tools-as-warnings" in run
    assert "--expect-build" in run
    assert "--stale-grace-seconds" in run
    assert "--deployed-at" in run
    assert "set -euo pipefail" in run


def test_the_grace_rides_only_with_the_schedule():
    """A push run is inside any sensible grace by definition, and the commit
    time of a pushed head is hours old when an old commit is pushed back on
    purpose. Passing the grace there failed that push's own run at once."""
    run = _wake_run()
    guard = _enclosing_if(run, "--stale-grace-seconds")
    assert "github.event_name }}\" = \"schedule\"" in guard, guard
    assert _enclosing_if(run, "--deployed-at") == guard


def test_the_wake_job_never_measures_against_a_session_branch_head():
    """When the deploy branch cannot be fetched the fallback used to pass this
    checkout's sha and commit time, so a session branch push with a fetch
    blip measured the live app against the wrong commit."""
    registry = _step("wake", "registry")["run"]
    assert "$GITHUB_SHA" not in registry
    assert "--format=%ct HEAD" not in registry
    assert 'echo "sha=" >>' in registry
    assert 'echo "committed_at=" >>' in registry
    run = _wake_run()
    guard = _enclosing_if(run, "--expect-build")
    assert "steps.registry.outputs.sha" in guard, guard


def test_the_age_is_the_committer_time_of_the_deploy_branch_head():
    registry = _step("wake", "registry")["run"]
    assert "git log -1 --format=%ct FETCH_HEAD" in registry, (
        "%ct of FETCH_HEAD: the author time can be far older on a rebased "
        "commit, and HEAD is whatever branch this run happens to be on")


def test_the_wake_job_knows_the_deploy_branch_history():
    registry = _step("wake", "registry")["run"]
    assert "--depth=200" in registry
    assert "git log --format=%H FETCH_HEAD > known-builds.txt" in registry
    assert "--known-builds known-builds.txt" in _wake_run()


def test_both_jobs_bound_a_pass_and_the_timeouts_are_sized_to_it():
    """A pass in which every page times out ran over an hour and killed the
    job with a generic timeout, a failure email for an app that is up. Each
    pass is now bounded and the job timeouts are computed from the bound."""
    data = _workflow()
    budget = float(data["env"]["PASS_BUDGET_SECONDS"])
    assert 300 <= budget <= 900, budget
    worst_pass_min = (budget + ka.WORST_TOOL_READ_S) / 60
    for job in ("wake", "deploy-current"):
        scripts = " ".join(str(s.get("run", "")) for s in data["jobs"][job]["steps"])
        assert '--pass-budget "$PASS_BUDGET_SECONDS"' in scripts, job
    install_and_wake_min = 8
    assert data["jobs"]["wake"]["timeout-minutes"] >= worst_pass_min + install_and_wake_min
    deploy = data["jobs"]["deploy-current"]
    assert "--retry-until-current 1200" in " ".join(
        str(s.get("run", "")) for s in deploy["steps"])
    assert deploy["timeout-minutes"] >= 2 * worst_pass_min + 20 + install_and_wake_min


def test_the_grace_is_longer_than_community_cloud_has_ever_taken():
    """Two hours was the worst self applied lag measured. Three is the grace.
    Shorter would email about a deploy that is about to fix itself; much
    longer would sit on a stuck one."""
    grace = int(_workflow()["env"]["STALE_GRACE_SECONDS"])
    assert 2.5 * 3600 <= grace <= 6 * 3600, grace


def test_the_deploy_job_warns_and_waits_rather_than_failing():
    """A change still unapplied after twenty minutes is normal here. The job
    confirms the prompt case quickly and never emails on its own."""
    steps = _workflow()["jobs"]["deploy-current"]["steps"]
    probes = [s for s in steps if "keep_streamlit_awake.py" in str(s.get("run", ""))]
    assert len(probes) == 1
    run = probes[0]["run"]
    assert "--tools-as-warnings" in run
    assert "--retry-until-current" in run
    assert "--stale-grace-seconds" not in run, (
        "an age is meaningless a minute after a push")


def test_the_deploy_job_never_runs_on_the_schedule():
    condition = _workflow()["jobs"]["deploy-current"]["if"]
    assert "github.event_name != 'schedule'" in condition


def test_the_deploy_job_runs_only_for_the_deploy_branch():
    data = _workflow()
    condition = data["jobs"]["deploy-current"]["if"]
    branch = data["env"]["DEPLOY_BRANCH"]
    assert f"github.ref_name == '{branch}'" in condition, (
        "the job level if spells the branch out and it has drifted from env")


def test_the_deploy_job_does_not_wait_for_the_wake_job():
    assert "needs" not in _workflow()["jobs"]["deploy-current"]


def test_deploy_branch_runs_have_their_own_queue():
    """A single group held every event with room for one pending run, so a
    deploy branch push queued behind a long deploy check was cancelled by the
    next schedule tick or session branch push and never got its verdict."""
    data = _workflow()
    group = data["concurrency"]["group"]
    branch = data["env"]["DEPLOY_BRANCH"]
    assert "github.event_name != 'schedule'" in group
    assert f"github.ref_name == '{branch}'" in group
    assert "'deploy'" in group and "'wake'" in group
    assert data["concurrency"]["cancel-in-progress"] is False


def test_nothing_in_the_workflow_writes_to_the_repository():
    """The nudge that wrote a stamp commit is gone: a dependency file change
    did not make Community Cloud redeploy any faster than an ordinary push,
    measured twice, and it left bot commits on the branch for nothing."""
    data = _workflow()
    assert data["permissions"] == {"contents": "read"}
    for job in data["jobs"].values():
        assert "permissions" not in job
        scripts = " ".join(str(s.get("run", "")) for s in job["steps"])
        assert "git push" not in scripts
        assert "git commit" not in scripts
        assert "deploy_stamp" not in scripts
    assert not (ROOT / "scripts" / "deploy_stamp.py").exists()


def test_the_requirements_file_carries_no_stamp():
    text = (ROOT / "requirements.txt").read_text()
    assert "deploy stamp" not in text
    assert text.splitlines()[0].startswith("#") or text.splitlines()[0].strip()


def test_the_workflow_states_the_measured_platform_behaviour():
    """The comment is the record of why the nudge is gone. If it is removed
    someone will add the nudge back."""
    raw = (ROOT / ".github" / "workflows" / "keep-awake.yml").read_text()
    assert "tried, twice" in raw
    assert "Reboot" in raw
