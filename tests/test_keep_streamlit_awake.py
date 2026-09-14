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

import sys
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
