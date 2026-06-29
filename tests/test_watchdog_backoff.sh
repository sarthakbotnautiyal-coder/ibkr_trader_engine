#!/bin/bash
# test_watchdog_backoff.sh — Unit tests for the exit-2 backoff helper in
# scripts/ibkr-engine-watchdog.sh (TASK-2026-276).
#
# NOT executed by pytest — standalone bash harness, run from the repo root:
#
#   bash tests/test_watchdog_backoff.sh
#
# What it verifies:
#   1. State file absent -> should_backoff_duplicate returns 1 (restart).
#   2. State file shows "2 <5 min ago>" -> backoff triggered (0).
#   3. State file shows "2 <45 min ago>" -> past recovery window (1, safety
#      valve: the duplicate is presumed dead; restart normally).
#   4. State file shows "0 <5 min ago>" -> clean exit, no backoff (restart).
#   5. State file malformed -> no backoff (fall-through, restart).
#   6. log_backoff_decision writes the expected log line and includes both
#      the last exit time and the next-attempt time.
#
# How it isolates the helpers:
#   - The watchdog sets STATE_FILE, DUPLICATE_BACKOFF_SECONDS, RECOVERY_SECONDS,
#     and WATCHDOG_LOG at module-load time. The test sources the script with
#     the file pre-processed by sed so the constants point at a tmp scratch
#     path. That gives us a sub-shell with the helpers in scope without
#     executing the watchdog's main-loop logic (which would otherwise try to
#     query is_market_open.py and acquire the launch mutex).
#
# Implementation note: each test runs in its own subshell so the helper
# state cannot leak. PASS/FAIL counters accumulate via the OUTCOME_FILE
# (one line per assertion outcome: "P" or "F\t<label>\t<expected>\t<actual>").
# Files beat subshell-scoped variables because the helpers themselves run in
# nested subshells around every `should_backoff_duplicate` call.
#
# See TASK-2026-276. Pairs with PR #17 (engine exit-2) and PR #18 (read-retry).

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRATCH="$(mktemp -d -t ibkr-backoff-test-XXXXXX)"
STATE_FILE="$SCRATCH/ibkr-engine.last_exit"
WATCHDOG_LOG="$SCRATCH/ibkr-engine-watchdog.log"
OUTCOME_FILE="$SCRATCH/outcomes.tsv"
: > "$OUTCOME_FILE"

# ---------------------------------------------------------------------------
# Isolate the watchdog helpers into a separate file by stripping the main
# logic and re-pointing STATE_FILE / WATCHDOG_LOG at scratch paths.
# ---------------------------------------------------------------------------
WATCHDOG_SCRIPT="$REPO_ROOT/scripts/ibkr-engine-watchdog.sh"

sed -e "s|^STATE_FILE=.*|STATE_FILE=\"$STATE_FILE\"|" \
    -e "s|^WATCHDOG_LOG=.*|WATCHDOG_LOG=\"$WATCHDOG_LOG\"|" \
    "$WATCHDOG_SCRIPT" > "$SCRATCH/wdog-isolated.sh"

# Strip everything from the date-check onward so sourcing the file does not
# run the watchdog's main flow (which would otherwise try to talk to
# is_market_open.py and acquire the mutex).
awk '
    /^# Date check: is today a trading day\?/ { exit }
    { print }
' "$SCRATCH/wdog-isolated.sh" > "$SCRATCH/wdog-helpers.sh"

grep -q "should_backoff_duplicate()" "$SCRATCH/wdog-helpers.sh" || { echo "FATAL: helpers missing"; exit 2; }
grep -q "Date check: is today a trading day" "$SCRATCH/wdog-isolated.sh" || { echo "FATAL: main loop not detected in full script"; exit 2; }

cleanup() {
    rm -rf "$SCRATCH" /tmp/ibkr-engine.last_exit
    rm -f /tmp/ibkr-engine.last_exit
}
trap cleanup EXIT

# runs the given BASH source-code snippet in a fresh subshell with the helper
# functions loaded. Print PASS/FAIL counters as standard assertions do.
check() {
    local label="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        echo "  ✓ $label"
        printf 'P\n' >> "$OUTCOME_FILE"
    else
        echo "  ✗ $label  expected='$expected' actual='$actual'"
        printf 'F\t%s\t%s\t%s\n' "$label" "$expected" "$actual" >> "$OUTCOME_FILE"
    fi
}

# Each test sources the helper-script in its own subshell so the helper state
# (PATH-scoped vars, function defs) cannot leak across tests. Assertions
# inside the subshell call check() (which writes a line to OUTCOME_FILE).

echo "Test 1: state file absent -> no backoff"
(
    rm -f "$STATE_FILE"
    set +u
    # shellcheck disable=SC1091
    . "$SCRATCH/wdog-helpers.sh"
    set +e
    should_backoff_duplicate
    rc=$?
    set -e
    check "should_backoff_duplicate returns 1 (no backoff)" "1" "$rc"
)

# --------------------------------------------------
echo ""
echo "Test 2: exit=2, 5 min old -> backoff (within 15 min window)"
(
    rm -f "$STATE_FILE" "$WATCHDOG_LOG"
    NOW=$(date +%s)
    echo "2 $((NOW - 300))" > "$STATE_FILE"
    set +u
    # shellcheck disable=SC1091
    . "$SCRATCH/wdog-helpers.sh"
    set +e
    should_backoff_duplicate
    rc=$?
    log_backoff_decision
    set -e
    check "should_backoff_duplicate returns 0 (backoff)" "0" "$rc"
    if grep -q "ibkr_trader_engine exit code 2 detected (duplicate instance)" "$WATCHDOG_LOG" 2>/dev/null; then
        check "log line mentions duplicate instance" "1" "1"
    else
        check "log line mentions duplicate instance" "1" "0"
    fi
    if grep -q "Next attempt allowed at" "$WATCHDOG_LOG" 2>/dev/null; then
        check "log line includes next-attempt time" "1" "1"
    else
        check "log line includes next-attempt time" "1" "0"
    fi
    if grep -q "s ago" "$WATCHDOG_LOG" 2>/dev/null; then
        check "log line includes age in seconds" "1" "1"
    else
        check "log line includes age in seconds" "1" "0"
    fi
)

# --------------------------------------------------
echo ""
echo "Test 3: exit=2, 45 min old -> past recovery window -> no backoff (safety valve)"
(
    rm -f "$STATE_FILE" "$WATCHDOG_LOG"
    NOW=$(date +%s)
    echo "2 $((NOW - 2700))" > "$STATE_FILE"
    set +u
    # shellcheck disable=SC1091
    . "$SCRATCH/wdog-helpers.sh"
    set +e
    should_backoff_duplicate
    rc=$?
    set -e
    check "should_backoff_duplicate returns 1 (past recovery)" "1" "$rc"
    if [ ! -s "$WATCHDOG_LOG" ]; then
        check "no backoff log was written" "1" "1"
    else
        check "no backoff log was written" "1" "0"
    fi
)

# --------------------------------------------------
echo ""
echo "Test 4: exit=0 (clean), 5 min old -> no backoff"
(
    rm -f "$STATE_FILE" "$WATCHDOG_LOG"
    NOW=$(date +%s)
    echo "0 $((NOW - 300))" > "$STATE_FILE"
    set +u
    # shellcheck disable=SC1091
    . "$SCRATCH/wdog-helpers.sh"
    set +e
    should_backoff_duplicate
    rc=$?
    set -e
    check "should_backoff_duplicate returns 1 (clean exit)" "1" "$rc"
)

# --------------------------------------------------
echo ""
echo "Test 5: state file malformed -> no backoff (fall-through)"
(
    rm -f "$STATE_FILE" "$WATCHDOG_LOG"
    echo "garbage_no_fields" > "$STATE_FILE"
    set +u
    # shellcheck disable=SC1091
    . "$SCRATCH/wdog-helpers.sh"
    set +e
    should_backoff_duplicate
    rc=$?
    set -e
    check "should_backoff_duplicate returns 1 (malformed)" "1" "$rc"
)

# --------------------------------------------------
echo ""
PASS=$(grep -c '^P$' "$OUTCOME_FILE" || true)
FAIL=$(grep -c -E '^F\t' "$OUTCOME_FILE" || true)
PASS=${PASS:-0}
FAIL=${FAIL:-0}
if [ "$FAIL" -eq 0 ]; then
    echo "ALL TESTS PASSED ($PASS assertions)"
    exit 0
else
    echo "TESTS FAILED ($FAIL failures, $PASS passed)"
    echo "--- failures ---"
    awk -F'\t' '$1=="F" { printf "  - %s  expected=%s actual=%s\n", $2, $3, $4 }' "$OUTCOME_FILE"
    exit 1
fi
