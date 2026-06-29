#!/bin/bash
# test_wrapper_flock.sh — Smoke test for the flock-or-lockdir mutex in
# scripts/run-ibkr-engine.sh and scripts/ibkr-engine-watchdog.sh.
#
# This script is NOT executed by pytest — it's a standalone Bash harness because
# the lock-target behaviour (two concurrent forks, one blocked) is awkward to
# express in Python. Run manually from the repo root:
#
#   bash tests/test_wrapper_flock.sh
#
# Exit code: 0 on success, 1 on any assertion failure.
#
# What it verifies:
#   1. Two concurrent invocations of the launch critical section: only ONE
#      fork happens, the other exits 0 idempotent with the "another invocation
#      holds" log line.
#   2. Stale lockdir (mtime > 5 min) is automatically cleared by the next
#      invocation so a SIGKILL'ed previous wrapper doesn't deadlock the engine.
#
# See TASK-2026-269 and TASK-2026-274 for context.

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRATCH="$(mktemp -d -t ibkr-flock-test-XXXXXX)"
FAKE_REPO="$SCRATCH/repo"
FAKE_LOG="$FAKE_REPO/logs"
FAKE_PY="$SCRATCH/fakepython"
RUN_SCRIPT="$REPO_ROOT/scripts/run-ibkr-engine.sh"

PASS=0
FAIL=0

assert_eq() {
    local label="$1"
    local expected="$2"
    local actual="$3"
    if [ "$expected" = "$actual" ]; then
        echo "  ✓ $label"
        PASS=$((PASS + 1))
    else
        echo "  ✗ $label  expected='$expected' actual='$actual'"
        FAIL=$((FAIL + 1))
    fi
}

cleanup() {
    pkill -9 -f "$FAKE_PY" 2>/dev/null || true
    rm -rf "$SCRATCH" /tmp/ibkr-engine-launch.lockdir /tmp/ibkr-engine-launch.lock
}
trap cleanup EXIT

mkdir -p "$FAKE_LOG"

# Fake engine — sleeps long enough for the wrapper's 2s health check to pass.
# Does NOT `exec sleep` so the script's absolute path stays visible in argv
# (which pgrep -fl needs to count children).
cat > "$FAKE_PY" <<'PYEOF'
#!/bin/bash
sleep 30
PYEOF
chmod +x "$FAKE_PY"

# Run-script copy with REPO_DIR + PYTHON_PATH patched to scratch paths.
cp "$RUN_SCRIPT" "$SCRATCH/run-test.sh"
perl -pi -e "s|REPO_DIR=\"/Users/ubexbot/.openclaw/workspace-venkat/ibkr_trader_engine\"|REPO_DIR=\"$FAKE_REPO\"|g" "$SCRATCH/run-test.sh"
perl -pi -e "s|PYTHON_PATH=\"\\\$REPO_DIR/venv/bin/python\"|PYTHON_PATH=\"$FAKE_PY\"|g" "$SCRATCH/run-test.sh"

# Sanity: confirm the patches landed
grep -E '^REPO_DIR=|^PYTHON_PATH=' "$SCRATCH/run-test.sh"

# Clean any prior state
rm -f /tmp/ibkr-engine-launch.lockdir /tmp/ibkr-engine-launch.lock "$FAKE_REPO/run.pid" "$FAKE_LOG/ibkr_engine.log"

echo "Test 1: Two concurrent wrappers — only ONE fork should happen"
bash "$SCRATCH/run-test.sh" > /tmp/wrap1.out 2>&1 &
PID1=$!
bash "$SCRATCH/run-test.sh" > /tmp/wrap2.out 2>&1 &
PID2=$!
sleep 4
wait $PID1 2>/dev/null; W1=$?
wait $PID2 2>/dev/null; W2=$?

assert_eq "wrapper1 exit code is 0"  "0" "$W1"
assert_eq "wrapper2 exit code is 0"  "0" "$W2"

# Exactly one pidfile must exist (the first wrapper wrote it; second bailed before forking)
PIDFILE_CONTENT=$(cat "$FAKE_REPO/run.pid" 2>/dev/null || echo "")
if [ -n "$PIDFILE_CONTENT" ]; then
    assert_eq "pidfile is non-empty (one fork happened)" "1" "1"
else
    assert_eq "pidfile is non-empty (one fork happened)" "1" "0"
fi

# The pidfile content is the surviving child PID. Verify exactly ONE child
# process is alive (the file is the source of truth for "did we fork").
# The duplicate-fork check is implicit: if wrapper2 had also forked, the
# pidfile would have been overwritten by a different PID AND a second child
# process would be alive — instead wrapper2 exited idempotent BEFORE writing
# run.pid, so the file holds exactly the original child's PID.
PID_FROM_FILE="$PIDFILE_CONTENT"
if [ -n "$PID_FROM_FILE" ] && kill -0 "$PID_FROM_FILE" 2>/dev/null; then
    assert_eq "pidfile PID is alive (one fork happened)" "1" "1"
else
    assert_eq "pidfile PID is alive (one fork happened)" "1" "0"
fi
# Cross-check: count processes whose argv matches our fake python path.
FAKE_PIDS=$(pgrep -fl "$FAKE_PY" 2>/dev/null | wc -l | tr -d ' ')
assert_eq "exactly one fake-child process is alive" "1" "$FAKE_PIDS"

# Both wrappers must have logged — wrapper1 "started successfully" and
# wrapper2 "exiting 0 idempotent"
assert_eq "wrapper1 logged 'started successfully'" "1" "$(grep -c 'started successfully' "$FAKE_LOG/ibkr_engine.log" 2>/dev/null | head -1)"
assert_eq "wrapper2 logged 'exiting 0 idempotent'" "1" "$(grep -c 'exiting 0 idempotent' "$FAKE_LOG/ibkr_engine.log" 2>/dev/null | head -1)"

# Release the fake engine so the lockdir cleanup trap on wrapper1 can fire cleanly
pkill -9 -f "$FAKE_PY" 2>/dev/null || true
wait $PID1 2>/dev/null
wait $PID2 2>/dev/null
sleep 1

echo ""
echo "Test 2: Stale lockdir (age > 5 min) is auto-cleared by next invocation"
mkdir -p /tmp/ibkr-engine-launch.lockdir
# Use BSD touch -t (macOS first); fall back to GNU date for Linux.
if date -v -10M '+%Y%m%d%H%M' >/dev/null 2>&1; then
    STAMP=$(date -v -10M '+%Y%m%d%H%M')
else
    STAMP=$(date -d '-10 minutes' '+%Y%m%d%H%M')
fi
touch -t "$STAMP" /tmp/ibkr-engine-launch.lockdir

# Run a single wrapper — it should detect the stale lockdir, clear it, and proceed
bash "$SCRATCH/run-test.sh" > /tmp/wrap-stale.out 2>&1
STALE_EXIT=$?
sleep 3
pkill -9 -f "$FAKE_PY" 2>/dev/null || true

assert_eq "stale-recovery wrapper exit code" "0" "$STALE_EXIT"
assert_eq "stale lockdir removal was logged" "1" "$(grep -c 'removing stale /tmp/ibkr-engine-launch.lockdir' "$FAKE_LOG/ibkr_engine.log" 2>/dev/null | head -1)"

echo ""
if [ "$FAIL" -eq 0 ]; then
    echo "ALL TESTS PASSED ($PASS assertions)"
    exit 0
else
    echo "TESTS FAILED ($FAIL failures, $PASS passed)"
    exit 1
fi
