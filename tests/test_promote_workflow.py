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

# The gate runs every suite through one runner rather than looping over the
# files itself. That is not a tidiness preference: the loop it replaced ran
# `python3 tests/<file>.py` on every suite, which executes nothing at all for a
# pytest style file, and twenty eight of the forty seven suites here are that
# style. The gate was reporting them as passed without running a check.
RUNNER = "scripts/run_suites.py"

# The suites that drive a real browser against a live hub. A runner has
# neither, so including them would fail every promotion for a reason that has
# nothing to do with the code being promoted. The runner owns this list now,
# which is why the workflow names none of them.
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
    assert "promote" in jobs, f"no promote job, found {sorted(jobs)}"
    return jobs["promote"]


def sync_job(data: dict) -> dict:
    jobs = data["jobs"]
    assert "sync-main" in jobs, f"no sync-main job, found {sorted(jobs)}"
    return jobs["sync-main"]


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

def test_permissions_are_exactly_contents_and_actions_write():
    # Write to the repository is the merge. Write to actions is the one
    # workflow_dispatch that hands the deploy verdict to keep-awake.yml, since
    # a push made with the token starts no workflow and a dispatch does.
    # Anything broader hands a token that can do more than that to every push
    # on every session branch.
    data = load()
    assert data["permissions"] == {"contents": "write", "actions": "write"}, (
        f"unexpected permissions: {data['permissions']}")


# ---------------------------------------------------------------------------
# The gate, which is the reason the automation is safe at all
# ---------------------------------------------------------------------------

def test_the_suites_run_before_the_merge():
    data = load()
    gate = index_of(data, RUNNER)
    merge = index_of(data, "--no-ff")
    assert gate < merge, (
        f"the merge step is at {merge} and the gate at {gate}: "
        "an untested branch could reach the deploy branch")


def test_each_suite_is_run_the_repository_s_own_way():
    # This test used to assert that the gate ran `python3 "${suite}"` on every
    # file, with a comment explaining that every suite carries a __main__
    # runner. That belief was false and this test was holding it in place. The
    # property that actually matters is that each suite is executed the way its
    # own style runs, which is what the runner does and what the next test
    # proves the old loop did not.
    data = load()
    gate = runs(data)[index_of(data, RUNNER)]
    assert RUNNER in gate, "the gate does not call the suite runner"
    assert (ROOT / RUNNER).exists(), f"{RUNNER} does not exist"


def test_the_gate_cannot_go_back_to_running_each_file_with_python3():
    """The old loop, run against this repository's real suites, proves nothing.

    Held as a test rather than a comment so the mechanism cannot be reinstated
    on the belief that it works. If this ever fails because every suite has
    gained a __main__ runner, the loop would still be the weaker check: it
    would count a suite that prints nothing as passed.
    """
    import re as _re
    vacuous = []
    for suite in sorted((ROOT / "tests").glob("test_*.py")):
        source = suite.read_text(encoding="utf-8", errors="ignore")
        if _re.search(r"^def test_", source, _re.M) and "__main__" not in source:
            vacuous.append(suite.name)
    assert vacuous, (
        "expected at least one pytest style suite with no __main__ runner, "
        "which is what makes `python3 <file>` an unsound gate")
    gate = runs(load())[index_of(load(), RUNNER)]
    assert 'python3 "${suite}"' not in gate, (
        f"the gate runs each file with python3 again, which executes nothing "
        f"for {len(vacuous)} suite(s) including {vacuous[0]}")


def test_the_browser_suite_is_skipped():
    data = load()
    gate = runs(data)[index_of(data, RUNNER)]
    assert "--no-browser" in gate, (
        "the gate does not tell the runner to skip the browser suites, so a "
        "runner with no hub would fail every promotion")
    # The runner, not the workflow, is where the list lives now. It has to
    # actually contain the suite this test is named for.
    runner_source = (ROOT / RUNNER).read_text(encoding="utf-8")
    assert Path(SKIPPED_SUITE).name in runner_source, (
        f"{SKIPPED_SUITE} is not in the runner's browser suite list, so it "
        f"would be run on a machine with no hub")


def test_a_failing_suite_stops_the_promotion():
    data = load()
    gate = runs(data)[index_of(data, RUNNER)]
    assert "status=$?" in gate, "the gate never reads the runner's exit code"
    assert 'exit "${status}"' in gate, (
        "a failing suite does not fail the job, so a red branch could be "
        "merged into the deploy branch")
    # And the runner it calls has to be able to say no.
    runner_source = (ROOT / RUNNER).read_text(encoding="utf-8")
    assert "SUITES RESULT: FAIL" in runner_source, (
        "the runner has no failure verdict to report")


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

def test_the_deploy_verdict_is_handed_to_keep_awake_after_the_merge():
    # A push made with GITHUB_TOKEN does not trigger other workflows, so
    # keep-awake.yml will not fire for the merge push. A workflow_dispatch
    # made with the same token does, and keep-awake.yml's deploy-current job
    # runs on a dispatch from the deploy branch, waits, and reports.
    data = load()
    merge = index_of(data, "--no-ff")
    handover = index_of(data, "gh workflow run keep-awake.yml")
    assert handover > merge, (
        f"the handover is at {handover}, before the merge at {merge}")
    script = runs(data)[handover]
    assert f'--ref "${{DEPLOY_BRANCH}}"' in script, (
        "the dispatch must run keep-awake.yml on the deploy branch, where its "
        "deploy-current job is allowed to run")
    assert "set -euo pipefail" in script, "a failed dispatch would go unnoticed"
    step = steps(data)[handover]
    assert step.get("continue-on-error") is not True
    assert "GH_TOKEN" in step.get("env", {}), "gh has no token to dispatch with"


def test_promotion_no_longer_probes_the_app_itself():
    # One place decides whether a deploy landed, and it is keep-awake.yml.
    # A second prober here was one failure email per promotion that
    # Community Cloud was slow on, telling the owner to reboot.
    data = load()
    for script in runs(data):
        assert "keep_streamlit_awake.py" not in script, (
            "the promotion probes the live app itself again")
        assert "Reboot the app" not in script


def test_the_receiving_job_runs_on_a_dispatch_from_the_deploy_branch():
    # The handover only works if the other side accepts it: keep-awake.yml's
    # deploy-current job must run on workflow_dispatch, on the deploy branch,
    # and carry the failure the old probe used to carry.
    import yaml
    keep_awake = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "keep-awake.yml").read_text())
    triggers = keep_awake.get("on", keep_awake.get(True))
    assert "workflow_dispatch" in triggers
    job = keep_awake["jobs"]["deploy-current"]
    condition = job["if"]
    assert "github.event_name != 'schedule'" in condition
    assert f"github.ref_name == '{DEPLOY_BRANCH}'" in condition
    scripts = " ".join(str(step.get("run", "")) for step in job["steps"])
    assert "keep_streamlit_awake.py" in scripts
    assert "--verify-tools" in scripts
    # The prober's own exit code is the step's: an app that is down fails
    # the job, and a change not yet applied is a warning, on purpose. See the
    # workflow's header comment for the measurement behind that.
    assert "--tools-as-warnings" in scripts


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
    gate_script = runs(load())[index_of(load(), RUNNER)]
    assert 'echo "green=true" >> "$GITHUB_OUTPUT"' in gate_script, (
        "the gate never publishes the green output the merge step requires")
    # And it is published only after the failure branch has exited.
    assert gate_script.index('exit "${status}"') < gate_script.index("green=true"), (
        "the gate publishes green before it has ruled out a failing suite")


def test_every_git_command_in_the_merge_step_is_checked():
    # The merge script deliberately does not use set -e, so the conflict path
    # can abort cleanly before exiting. That makes an unchecked command a
    # silent failure: the step would run on to its final echo and report a
    # success that never happened. A rejected push is the dangerous one,
    # because the job goes green while the live app is unchanged.
    script = runs(load())[index_of(load(), "git merge --no-ff")]

    # Checked in either direction. `if ! git push` and `if git push; then
    # ...; fi` are both examinations of the result; only a bare `git push` on
    # its own line is not. Asserting one spelling made this test refuse a
    # retry loop that is strictly safer than what it was guarding.
    allowed_unchecked = (
        "git config",          # cannot meaningfully fail on a fresh runner
        "git merge --abort",   # already on the failure path, and || true
        "git diff",            # read only, inside a substitution
    )
    unchecked = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped.startswith("git "):
            continue
        if stripped.startswith(allowed_unchecked):
            continue
        unchecked.append(stripped)
    assert not unchecked, (
        "these git commands are neither inside a conditional nor allowed to "
        f"fail, so a failure would be reported as success: {unchecked}")

    for command in ("git checkout -B", "git push origin", "git merge --no-ff"):
        assert (f"if ! {command}" in script) or (f"if {command}" in script), (
            f"{command!r} is not checked in either direction")
    assert script.count("exit 1") >= 3, (
        "each of checkout, merge and push needs its own failure exit")


def test_a_moving_deploy_branch_is_retried_rather_than_handed_to_a_person():
    """A rejected push means the base moved, not that something is wrong.

    Per branch concurrency queues let two promotions run at once on purpose,
    so the merge step has to expect the branch to move under it. Fetching once
    at the start and pushing once at the end turned a safe, green promotion
    into a red job waiting for a human, with the finished tool merged nowhere.
    """
    script = runs(load())[index_of(load(), "git merge --no-ff")]
    assert "attempt" in script, "the merge never retries"
    assert script.count("git fetch origin") >= 1, (
        "the merge never refreshes the deploy branch, so a retry would push "
        "the same rejected commit again")
    # The refresh has to be inside the loop, not before it.
    loop_start = script.index("while ")
    assert script.index("git fetch origin", loop_start) > loop_start, (
        "the fetch is outside the retry loop, so every attempt uses the same "
        "stale base")
    # A conflict must not be retried: it would conflict identically.
    assert "A retry would" in script, (
        "the conflict path does not say why it is not retried")


def test_two_session_branches_do_not_evict_each_other_from_the_queue():
    """GitHub holds one pending run per concurrency group.

    A single shared group meant a third push cancelled whatever was queued, so
    a finished tool never reached the deploy branch and the only trace was a
    grey cancelled run.
    """
    group = str(load()["concurrency"]["group"])
    assert "github.ref" in group, (
        f"the concurrency group is shared across branches: {group!r}")
    assert load()["concurrency"]["cancel-in-progress"] is False, (
        "a promotion in flight can be cancelled part way through")


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


# ---------------------------------------------------------------------------
# Keeping main current, which is what makes the schedule run at all
# ---------------------------------------------------------------------------

def test_the_deploy_branch_name_is_the_same_everywhere_it_is_written():
    """The suite claimed to hold these in step and never actually checked.

    If the deploy branch moves and one copy is missed, promotions land on a
    branch nobody serves and the tools are invisible with every check green.
    """
    promote_env = load()["env"]["DEPLOY_BRANCH"]
    assert promote_env == DEPLOY_BRANCH, (
        f"promote-to-deploy.yml deploys to {promote_env!r}, not {DEPLOY_BRANCH!r}")

    keep_awake = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "keep-awake.yml").read_text())
    assert keep_awake["env"]["DEPLOY_BRANCH"] == DEPLOY_BRANCH, (
        f"keep-awake.yml measures {keep_awake['env']['DEPLOY_BRANCH']!r}")

    assert DEPLOY_BRANCH in (ROOT / "README.md").read_text(), (
        "README.md does not record the deploy branch")

    # The job guard repeats the name as a literal, because the env context is
    # not available in a job level condition. That literal has to match too.
    guard = str(job(load()).get("if", ""))
    assert DEPLOY_BRANCH in guard, (
        f"the job guard does not name {DEPLOY_BRANCH}: {guard!r}")


def test_main_is_fast_forwarded_after_a_promotion():
    """Without this, the three hourly wake never runs.

    GitHub triggers a schedule only from the copy of the workflow on the
    default branch. main is the default branch, and it did not carry
    keep-awake.yml, so that schedule produced zero runs from the day it was
    written.
    """
    data = load()
    names = [str(step.get("name", "")) for step in steps(data)]
    scripts_run = runs(data)
    syncs = [i for i, script in enumerate(scripts_run) if "HEAD:main" in script]
    assert syncs, f"no step pushes to main; steps are {names}"
    merge = index_of(data, "git merge --no-ff")
    assert min(syncs) > merge, (
        "main is advanced before the merge, so it could run ahead of the "
        "deploy branch")


def test_a_direct_push_to_the_deploy_branch_still_advances_main():
    """The promote job excludes the deploy branch, so something else must."""
    data = load()
    sync = sync_job(data)
    guard = str(sync.get("if", ""))
    assert DEPLOY_BRANCH in guard, (
        f"the sync job does not run for the deploy branch: {guard!r}")
    body = "\n".join(str(step.get("run", "")) for step in sync["steps"])
    assert ":main" in body, "the sync job never pushes to main"


def executable_lines(data: dict) -> str:
    """Every shell line the workflow actually runs, comments stripped.

    Scanning the raw file instead would flag a comment that explains why a flag
    is not used, which is prose about the rule rather than a break of it.
    """
    bodies = [str(step.get("run", "")) for step in steps(data)]
    bodies += [str(step.get("run", "")) for step in sync_job(data)["steps"]]
    kept = []
    for body in bodies:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                kept.append(stripped)
    return "\n".join(kept)


def test_main_is_never_force_pushed():
    """main is the default branch. A fast forward is the only safe push.

    A rejected fast forward means main has diverged, which is a thing to read
    and reconcile, never a thing to overwrite.
    """
    commands = executable_lines(load())
    for forbidden in ("--force", "-f origin", "+refs/heads/main",
                      "--force-with-lease", "push -f"):
        assert forbidden not in commands, (
            f"a command in the workflow uses {forbidden!r}, which could "
            f"rewrite a branch rather than fast forward it")


def test_a_failed_main_sync_is_reported_rather_than_swallowed():
    data = load()
    scripts_run = runs(data)
    syncs = [script for script in scripts_run if "HEAD:main" in script]
    syncs += [str(step.get("run", "")) for step in sync_job(data)["steps"]
              if ":main" in str(step.get("run", ""))]
    assert syncs, "no sync step found"
    for script in syncs:
        assert "::error::" in script, (
            "a sync step fails without an error annotation, so a main branch "
            "that stopped tracking would be silent")
        assert "exit 1" in script, (
            "a sync step reports an error without failing the job")
