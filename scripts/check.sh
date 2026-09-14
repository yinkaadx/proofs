#!/usr/bin/env bash
# One command gate for Toolbench.
#
# Starts a hub only if one is not already running, then runs every suite and
# prints a single summary. Reusing a long lived hub matters: starting Streamlit
# and waiting for it to answer costs several seconds, and doing that once per
# check was a large part of the wall clock on earlier builds.
#
# Usage:  scripts/check.sh            run everything
#         scripts/check.sh fast       skip the browser suites
#
# In full mode the browser suites are mandatory: TOOLBENCH_REQUIRE_BROWSER=1 is
# exported so a missing browser fails the gate instead of skipping quietly.
set -uo pipefail
cd "$(dirname "$0")/.."

PORT="${TOOLBENCH_PORT:-8600}"
BASE="http://127.0.0.1:${PORT}"
MODE="${1:-full}"
LOG="${TMPDIR:-/tmp}/toolbench-hub-${PORT}.log"

# There is no list of suites here any more. There were three, they were
# maintained by hand, and eight suites had already been forgotten by the time
# anyone looked, including both suites for the newest tool. scripts/run_suites.py
# discovers every tests/test_*.py, runs each one the way its own style actually
# executes, and fails any suite that cannot show that checks ran. The promotion
# workflow calls the same script, so the gate that guards the live app and the
# gate a person runs locally cannot drift apart.
RUNNER=scripts/run_suites.py

STAMP="${TMPDIR:-/tmp}/toolbench-hub-${PORT}.started"

healthy() { [ "$(curl -s -o /dev/null -w '%{http_code}' --noproxy 127.0.0.1 "${BASE}/_stcore/health" 2>/dev/null)" = "200" ]; }

# Newest modification time across everything the app actually serves.
newest_source() {
  find streamlit_app.py shared tools .streamlit -type f \
       \( -name '*.py' -o -name '*.toml' \) -printf '%T@\n' 2>/dev/null \
    | sort -n | tail -1 | cut -d. -f1
}

# Reusing a running hub is what makes this fast, but a hub started before the
# last edit serves stale code and reports a false pass. That already happened:
# a tool URL was reported missing because the hub predated the registry entry
# by twelve minutes. So reuse is allowed only when the hub is newer than every
# source file it serves.
hub_is_fresh() {
  [ -f "${STAMP}" ] || return 1
  local started newest
  started=$(cat "${STAMP}" 2>/dev/null || echo 0)
  newest=$(newest_source)
  [ -n "$newest" ] || return 0
  [ "$started" -ge "$newest" ]
}

stop_hub() {
  local pid
  pid=$(pgrep -f "server.port ${PORT}" | head -1)
  [ -n "$pid" ] && kill "$pid" 2>/dev/null && sleep 2
  return 0
}

ensure_hub() {
  if healthy; then
    if hub_is_fresh; then
      echo "hub already running on ${PORT} and newer than every source file"
      return 0
    fi
    echo "hub on ${PORT} predates the latest edit, restarting so it serves current code"
    stop_hub
  fi
  echo "starting hub on ${PORT}"
  setsid nohup streamlit run streamlit_app.py --server.port "${PORT}" \
    --server.headless true --server.address 127.0.0.1 > "${LOG}" 2>&1 < /dev/null &
  disown 2>/dev/null || true
  for _ in $(seq 1 30); do
    if healthy; then date +%s > "${STAMP}"; echo "hub ready"; return 0; fi
    sleep 1
  done
  echo "hub did not come up, see ${LOG}"
  return 1
}

started=$(date +%s%N)

if [ "$MODE" != "fast" ]; then
  # A guard that skips itself is not a guard. The browser suites exit 0 with a
  # SKIP line when Playwright or a hub is missing, which is right for a
  # developer running one by hand and wrong for the gate: it would report a
  # clean pass on a machine where the layout was never measured at all. Setting
  # this turns every skip in those suites into a failure. Fast mode remains the
  # way to deliberately not run them.
  export TOOLBENCH_REQUIRE_BROWSER=1
  ensure_hub || exit 1
  python3 "$RUNNER" --base "$BASE"
  failed=$?
else
  python3 "$RUNNER" --no-browser
  failed=$?
fi

ended=$(date +%s%N)
echo "-------------------------------------------------------------------------"
# The runner has already printed how many suites ran and how many checks
# actually executed. Counting that again here would be a second tally to keep
# in step with the first, which is the habit that produced a list of suites
# nobody had updated in eight tools.
printf 'wall clock %ss\n' \
  "$(awk "BEGIN{printf \"%.1f\", ($ended-$started)/1000000000}")"
exit "$failed"
