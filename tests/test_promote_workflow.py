"""Ground truth tests for the session branch promotion workflow.

The workflow merges a session branch into the branch Streamlit Community Cloud
actually deploys from, so its failure modes are expensive in a way a normal CI
job's are not: a bad guard merges the deploy branch into itself, a missing gate
ships a broken hub to every visitor, and a history rewrite under a running
deploy makes a rollback unreasonable. None of that is visible by reading the
file once. These tests parse the YAML and hold each guarantee in place.

Run either way:
    python3 -m pytest tests/test_promote_workflow.py -q
    python3 tests/test_promote_workflow.py

Pass marker under pytest: every test passes, zero errors, exit code 0.
Pass marker under python3: final line is exactly
"PROMOTE RESULT: PASS <n>/<n>" and exit code 0.
Fail marker: any line starting "FAIL", plus exit code 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "promote-to-deploy.yml"

# Recorded in README.md and in keep-awake.yml. If the deploy branch ever moves,
# it moves in both workflows and here together, and this test is what says so.
DEPLOY_BRANCH = "claude/kind-feynman-489x72"

# The suite that drives a real browser against the live hub. A runner has
# neither, so including it would fail every promotion for a reason that has
# nothing to do with the code being promoted.
SKIPPED_SUITE = "tests/test_theme.py"


def load() -> dict:
    raw = WORKFLOW.read_text()
    data = yaml.safe_load(raw)
    assert isinstance(data, dict), "the workflow did not parse to a mapping"
    return data


def triggers(data: dict):
    # PyYAML reads a bare `on:` key as the boolean True, so accept either.
    found = data.get("on", data.get(True))
    assert found is not None, "the workflow declares no triggers"
    return found


def job(data: dict) -> dict:
    jobs = data["jobs"]
    assert len(jobs) == 1, f"expected exactly one job, found {sorted(jobs)}"
    return next(iter(jobs.values()))


def steps(data: dict) -> list:
    return job(data)["steps"]


def runs(data: dict) -> list:
    """Every shell block in the workflow, in the order they execute."""
    return [str(step.get("run", "")) for step in steps(data)]


def index_of(data: dict, needle: str) -> int:
    for position, script in enumerate(runs(data)):
        if needle in script:
            return position
    raise AssertionError(f"no step runs anything containing {needle!r}")


# ---------------------------------------------------------------------------
# Triggers, and the branches that must never start a run
# ---------------------------------------------------------------------------

def test_the_workflow_parses():
    data = load()
    assert data.get("name"), "the workflow has no name"


def test_it_triggers_on_session_branches_and_by_hand():
    data = load()
    found = triggers(data)
    assert "workflow_dispatch" in found, "no manual trigger, so a stuck branch cannot be pushed through"
    branches = found["push"]["branches"]
    assert "claude/**" in branches, f"session branches are not watched: {branches}"


def test_the_deploy_branch_cannot_trigger_a_merge_into_itself():
    # The deploy branch matches "claude/**", so the branch filter alone lets a
    # push to it start a run. Only the job level condition stops that run from
    # merging the deploy branch into the deploy branch.
    data = load()
    condition = str(job(data).get("if", ""))
    assert condition, "the job has no if, so a push to the deploy branch would run it"
    assert "github.ref_name" in condition, "the guard does not read the branch name"
    assert f"!= '{DEPLOY_BRANCH}'" in condition, (
        f"the guard does not exclude the deploy branch {DEPLOY_BRANCH}")


def test_main_cannot_trigger_a_merge():
    data = load()
    condition = str(job(data).get("if", ""))
    assert "!= 'main'" in condition, "the guard does not exclude main"


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------

def test_permissions_are_exactly_contents_write():
    # Write to the repository is the whole job. Anything broader hands a token
    # that can do more than merge to every push on every session branch.
    data = load()
    assert data["permissions"] == {"contents": "write"}, (
        f"unexpected permissions: {data['permissions']}")


# ---------------------------------------------------------------------------
# The gate, which is the reason the automation is safe at all
# ---------------------------------------------------------------------------

def test_the_suites_run_before_the_merge():
    data = load()
    gate = index_of(data, "for suite in tests/test_*.py")
    merge = index_of(data, "--no-ff")
    assert gate < merge, (
        f"the merge step is at {merge} and the gate at {gate}: "
        "an untested branch could reach the deploy branch")


def test_each_suite_is_run_the_repository_s_own_way():
    # A bare "pytest tests/" errors during collection, because most suites here
    # are script style with a module level sys.exit. Running each file uses the
    # __main__ runner every suite carries, which works for both styles.
    data = load()
    gate = runs(data)[index_of(data, "for suite in tests/test_*.py")]
    assert 'python3 "${suite}"' in gate, "the gate does not run each suite as a file"


def test_the_browser_suite_is_skipped():
    data = load()
    text = WORKFLOW.read_text()
    assert SKIPPED_SUITE in text, f"{SKIPPED_SUITE} is not named anywhere"
    gate = runs(data)[index_of(data, "for suite in tests/test_*.py")]
    assert "SKIP_SUITE" in gate, "the gate does not skip anything"
    assert load()["env"]["SKIP_SUITE"] == SKIPPED_SUITE, (
        f"the skipped suite is not {SKIPPED_SUITE}")


def test_a_failing_suite_stops_the_promotion():
    data = load()
    gate = runs(data)[index_of(data, "for suite in tests/test_*.py")]
    assert "exit 1" in gate, "a failing suite does not fail the job"
    assert "skipped" in gate and "SUITE SUMMARY" in gate, (
        "the gate does not report which suites ran and which were skipped")


# ---------------------------------------------------------------------------
# The merge itself
# ---------------------------------------------------------------------------

def test_the_merge_keeps_the_merge_commit():
    # A fast forward would erase the fact that a promotion happened, which is
    # the one marker that makes a bad deploy findable afterwards.
    data = load()
    merge = runs(data)[index_of(data, "--no-ff")]
    assert "git merge --no-ff" in merge, "the merge is not an explicit no fast forward merge"
    assert "git push origin" in merge, "the merge step never pushes"


def test_history_is_never_rewritten():
    # The live app is built from this branch. Rewriting history underneath a
    # running deploy is how a rollback stops being possible to reason about.
    banned = ("--force", " -f ", "push -f", "rebase", "--amend", "--hard")
    for script in runs(load()):
        for token in banned:
            assert token not in script, f"a history rewrite reached a run block: {token!r}"


def test_a_conflict_aborts_and_pushes_nothing():
    data = load()
    merge_at = index_of(data, "--no-ff")
    merge = runs(data)[merge_at]
    assert "git merge --abort" in merge, "a conflicting merge is left half done"
    assert "--diff-filter=U" in merge, "the conflicting files are never named"
    # The abort and the failure both have to happen before the push line, or a
    # half merged tree reaches the branch the live app is built from.
    assert merge.index("git merge --abort") < merge.index("git push origin"), (
        "the abort comes after the push")


def test_an_already_merged_branch_does_nothing():
    data = load()
    plan = runs(data)[index_of(data, "merge-base --is-ancestor")]
    assert "needed=false" in plan and "needed=true" in plan, (
        "the containment check produces no decision the later steps can read")
    conditions = [str(step.get("if", "")) for step in steps(data)]
    assert any("needed == 'false'" in c for c in conditions), (
        "nothing says so when the branch is already contained")
    assert any("needed == 'true'" in c for c in conditions), (
        "the merge is not held behind the containment check")


# ---------------------------------------------------------------------------
# Waking the app, because a push with GITHUB_TOKEN triggers nothing
# ---------------------------------------------------------------------------

def test_the_prober_runs_after_the_merge():
    # A push made with GITHUB_TOKEN does not trigger other workflows, so
    # keep-awake.yml will not fire for this push and the merged tool would sit
    # undeployed until something else woke the app.
    data = load()
    merge = index_of(data, "--no-ff")
    wake = index_of(data, "keep_streamlit_awake.py")
    assert wake > merge, f"the prober is at {wake}, before the merge at {merge}"
    prober = runs(data)[wake]
    assert "--verify-tools" in prober, "the prober does not check the tools are live"
    assert "playwright" in prober, "the prober has no browser to drive"


def test_an_unreachable_app_does_not_fail_the_promotion():
    data = load()
    wake = index_of(data, "keep_streamlit_awake.py")
    step = steps(data)[wake]
    assert step.get("continue-on-error") is True, (
        "an unreachable app would paint a successful merge red")


# ---------------------------------------------------------------------------
# Concurrency and the clock
# ---------------------------------------------------------------------------

def test_two_session_branches_cannot_race_a_merge():
    data = load()
    group = data["concurrency"]
    assert group.get("group"), "no concurrency group, so two promotions can race"
    assert group.get("cancel-in-progress") is False, (
        "cancelling a run half way through a merge is worse than queueing it")


def test_the_job_has_an_explicit_timeout():
    data = load()
    timeout = job(data).get("timeout-minutes")
    assert isinstance(timeout, int) and timeout > 0, (
        "no timeout, so a hung browser holds the concurrency group")


# ---------------------------------------------------------------------------
# House rules
# ---------------------------------------------------------------------------

def test_this_suite_can_run_from_a_plain_checkout():
    # The docstring above declares a marker line this suite prints on failure.
    # That contract is only honoured if the module imports at all, and it
    # imports yaml at the top. Declaring pyyaml only inside the workflow that
    # happens to install it leaves every other environment, a local checkout
    # included, producing an ImportError traceback and no marker line.
    requirements = (ROOT / "requirements.txt").read_text()
    declared = [line.strip().lower() for line in requirements.splitlines()
                if line.strip() and not line.strip().startswith("#")]
    assert any(line.startswith("pyyaml") for line in declared), (
        "pyyaml is imported by this suite and is not in requirements.txt")


def test_the_merge_names_the_green_gate_as_its_own_precondition():
    # A step condition without a status check normally has success() applied
    # implicitly, so a red gate would skip this step anyway. "Normally" is the
    # wrong standard for the only thing standing between a red branch and every
    # visitor, so the gate's verdict is written down and read back explicitly.
    merge = steps(load())[index_of(load(), "git merge --no-ff")]
    condition = str(merge.get("if", ""))
    assert "steps.gate.outputs.green == 'true'" in condition, (
        f"the merge step does not require the gate's green output: {condition!r}")
    gate_script = runs(load())[index_of(load(), "SUITE SUMMARY")]
    assert 'echo "green=true" >> "$GITHUB_OUTPUT"' in gate_script, (
        "the gate never publishes the green output the merge step requires")
    # And it is published only after the failure branch has exited.
    assert gate_script.index("exit 1") < gate_script.index("green=true"), (
        "the gate publishes green before it has ruled out a failing suite")


def test_every_git_command_in_the_merge_step_is_checked():
    # The merge script deliberately does not use set -e, so the conflict path
    # can abort cleanly before exiting. That makes an unchecked command a
    # silent failure: the step would run on to its final echo and report a
    # success that never happened. A rejected push is the dangerous one,
    # because the job goes green while the live app is unchanged.
    script = runs(load())[index_of(load(), "git merge --no-ff")]
    for command in ("git checkout -B", "git push origin"):
        assert f"if ! {command}" in script, (
            f"{command!r} is not checked, so a failure would be reported as success")
    assert script.count("exit 1") >= 3, (
        "each of checkout, merge and push needs its own failure exit")


def test_a_branch_name_is_never_pasted_into_the_shell():
    # A git branch name may legally contain ; & | $ and backticks. A ${{ }}
    # substitution pastes it into the script before the shell runs, which turns
    # a branch name into code. Passed through env: it stays data.
    merge = steps(load())[index_of(load(), "git merge --no-ff")]
    script = str(merge.get("run", ""))
    assert "${{" not in script, (
        "the merge script interpolates a GitHub expression directly into the shell")
    env = merge.get("env", {})
    assert env.get("SESSION_BRANCH") == "${{ steps.plan.outputs.session_branch }}"
    assert env.get("SESSION_SHA") == "${{ steps.plan.outputs.session_sha }}"


def test_no_em_or_en_dashes_in_the_new_files():
    # Spelled by code point so the detector itself carries no dash.
    banned = (chr(8212), chr(8211))
    for name in (".github/workflows/promote-to-deploy.yml",
                 "tests/test_promote_workflow.py"):
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
        print(f"PROMOTE RESULT: FAIL {len(failures)} of {len(tests)} checks failed")
        for name in failures:
            print(f"  - {name}")
        sys.exit(1)
    print(f"PROMOTE RESULT: PASS {len(tests)}/{len(tests)}")
    sys.exit(0)
