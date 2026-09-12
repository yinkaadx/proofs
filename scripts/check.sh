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
set -uo pipefail
cd "$(dirname "$0")/.."

PORT="${TOOLBENCH_PORT:-8600}"
BASE="http://127.0.0.1:${PORT}"
MODE="${1:-full}"
LOG="${TMPDIR:-/tmp}/toolbench-hub-${PORT}.log"

UNIT_SUITES=(
  tests/test_hub.py
  tests/test_wp_form_debugger_core.py
  tests/test_app_smoke.py
  tests/test_php_syntax.py
  tests/test_netsuite_hubspot_sync.py
  tests/test_netsuite_hubspot_sync_page.py
  tests/test_multi_channel_inventory_sync.py
  tests/test_multi_channel_inventory_sync_page.py
)
BROWSER_SUITES=(
  tests/test_theme.py
  tests/test_browser_inventory_flow.py
)

healthy() { [ "$(curl -s -o /dev/null -w '%{http_code}' --noproxy 127.0.0.1 "${BASE}/_stcore/health" 2>/dev/null)" = "200" ]; }

ensure_hub() {
  healthy && { echo "hub already running on ${PORT}"; return 0; }
  echo "starting hub on ${PORT}"
  setsid nohup streamlit run streamlit_app.py --server.port "${PORT}" \
    --server.headless true --server.address 127.0.0.1 > "${LOG}" 2>&1 < /dev/null &
  disown 2>/dev/null || true
  for _ in $(seq 1 30); do healthy && { echo "hub ready"; return 0; }; sleep 1; done
  echo "hub did not come up, see ${LOG}"
  return 1
}

total=0; failed=0; started=$(date +%s%N)
run_suite() {
  local suite="$1"; shift
  local s e out secs
  s=$(date +%s%N)
  out=$(python3 "$suite" "$@" 2>&1 | grep -vE "ScriptRunContext" | tail -1)
  e=$(date +%s%N)
  secs=$(awk "BEGIN{printf \"%.1f\", ($e-$s)/1000000000}")
  printf '  %-50s %6ss  %s\n' "$suite" "$secs" "$out"
  if echo "$out" | grep -qE "PASS|SKIP"; then
    n=$(echo "$out" | grep -oE '[0-9]+/[0-9]+' | cut -d/ -f1)
    total=$((total + ${n:-0}))
  else
    failed=$((failed + 1))
  fi
}

echo "== Suites =="
for suite in "${UNIT_SUITES[@]}"; do run_suite "$suite"; done

if [ "$MODE" != "fast" ]; then
  ensure_hub || exit 1
  for suite in "${BROWSER_SUITES[@]}"; do run_suite "$suite" "$BASE"; done
else
  echo "  (browser suites skipped: fast mode)"
fi

ended=$(date +%s%N)
echo "-------------------------------------------------------------------------"
printf 'TOTAL %s checks passed, %s suite(s) failed, wall clock %ss\n' \
  "$total" "$failed" "$(awk "BEGIN{printf \"%.1f\", ($ended-$started)/1000000000}")"
[ "$failed" -eq 0 ] || exit 1
