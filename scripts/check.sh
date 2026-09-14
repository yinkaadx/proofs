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

UNIT_SUITES=(
  tests/test_hub.py
  tests/test_canonical_url.py
  tests/test_wp_form_debugger_core.py
  tests/test_app_smoke.py
  tests/test_php_syntax.py
  tests/test_netsuite_hubspot_sync.py
  tests/test_netsuite_hubspot_sync_page.py
  tests/test_multi_channel_inventory_sync.py
  tests/test_multi_channel_inventory_sync_page.py
)
BROWSER_SUITES=(
  tests/test_tool_urls.py
  tests/test_theme.py
  tests/test_browser_inventory_flow.py
)

# Browser suites that start their own servers, so they take no base URL. The
# sidebar invariant needs two hubs at once, one of them with a padded tool
# list, which is not something the shared hub on ${PORT} can be.
SELF_HOSTED_BROWSER_SUITES=(
  tests/test_sidebar_invariant.py
)

# Suites written for pytest rather than the standalone script style.
PYTEST_SUITES=(
  tests/test_zero_trust_rmm_console.py
  tests/test_zero_trust_rmm_console_page.py
  tests/test_pod_automation_router.py
  tests/test_pod_automation_router_page.py
  tests/test_tv_mt5_bridge_diagnostic.py
  tests/test_tv_mt5_bridge_diagnostic_page.py
  tests/test_sharepoint_zero_trust_simulator.py
  tests/test_sharepoint_zero_trust_simulator_page.py
  tests/test_retreat_funnel_redundancy_guard.py
  tests/test_retreat_funnel_redundancy_guard_page.py
  tests/test_web3_smart_escrow_console.py
  tests/test_web3_smart_escrow_console_page.py
  tests/test_ten_dlc_compliance_validator.py
  tests/test_ten_dlc_compliance_validator_page.py
  tests/test_reventure_conversion_engine.py
  tests/test_reventure_conversion_engine_page.py
  tests/test_airtable_whatsapp_automation_guard.py
  tests/test_airtable_whatsapp_automation_guard_page.py
  tests/test_m365_intranet_architecture_console.py
  tests/test_m365_intranet_architecture_console_page.py
  tests/test_wastetab_dispatch_engine.py
  tests/test_wastetab_dispatch_engine_page.py
  tests/test_aerial_insights_qa_console.py
  tests/test_aerial_insights_qa_console_page.py
  tests/test_askew_suit_engine.py
  tests/test_askew_suit_engine_page.py
)

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

run_pytest() {
  local s e out secs
  s=$(date +%s%N)
  out=$(python3 -m pytest "$@" -q 2>&1 | tail -1)
  e=$(date +%s%N)
  secs=$(awk "BEGIN{printf \"%.1f\", ($e-$s)/1000000000}")
  printf '  %-50s %6ss  %s\n' "pytest (${#} file(s))" "$secs" "$out"
  if echo "$out" | grep -qE "[0-9]+ passed"; then
    n=$(echo "$out" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+")
    total=$((total + ${n:-0}))
  else
    failed=$((failed + 1))
  fi
}

echo "== Suites =="
for suite in "${UNIT_SUITES[@]}"; do run_suite "$suite"; done
run_pytest "${PYTEST_SUITES[@]}"

if [ "$MODE" != "fast" ]; then
  # A guard that skips itself is not a guard. Both browser suites exit 0 with a
  # SKIP line when Playwright or a hub is missing, which is right for a
  # developer running one by hand and wrong for the gate: it would report a
  # clean pass on a machine where the layout was never measured at all. Setting
  # this turns every skip in those suites into a failure. Fast mode remains the
  # way to deliberately not run them.
  export TOOLBENCH_REQUIRE_BROWSER=1
  ensure_hub || exit 1
  for suite in "${BROWSER_SUITES[@]}"; do run_suite "$suite" "$BASE"; done
  for suite in "${SELF_HOSTED_BROWSER_SUITES[@]}"; do run_suite "$suite"; done
else
  echo "  (browser suites skipped: fast mode)"
fi

ended=$(date +%s%N)
echo "-------------------------------------------------------------------------"
printf 'TOTAL %s checks passed, %s suite(s) failed, wall clock %ss\n' \
  "$total" "$failed" "$(awk "BEGIN{printf \"%.1f\", ($ended-$started)/1000000000}")"
[ "$failed" -eq 0 ] || exit 1
