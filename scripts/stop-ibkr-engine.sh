#!/bin/bash
# stop-ibkr-engine.sh — Gracefully stop the IBKR trader engine via pidfile + pgrep fallback
#
# Usage: stop-ibkr-engine.sh
#
# Behaviour:
#   - Primary path: Reads PID from run.pid, sends SIGTERM, waits 10s, SIGKILL.
#   - Fallback path: pgrep -f "run.py" filtered by cwd == REPO_DIR (catches orphans).
#   - After confirmed stop: cleans up launch-mutex state files left by
#     run-ibkr-engine.sh (lockdir + last_exit sentinel). This is the canonical
#     cleanup point — the wrapper's EXIT trap is best-effort and fails on
#     SIGKILL/crash; if it didn't run, an orphaned lockdir blocks the next
#     morning's 9:30 launch (see 2026-06-30 incident, TASK-2026-277).
#
# Logs to logs/ibkr_engine.log so fallback kills and cleanup are auditable.

set -u

REPO_DIR="/Users/ubexbot/.openclaw/workspace-venkat/ibkr_trader_engine"
PID_FILE="$REPO_DIR/run.pid"
LOG_DIR="$REPO_DIR/logs"
LOG_PATH="$LOG_DIR/ibkr_engine.log"

# Launch-mutex state files (TASK-2026-278).
# Normally cleaned by run-ibkr-engine.sh's EXIT trap, but if the wrapper was
# SIGKILL'd, OOM'd, or the system rebooted, they're left behind and block the
# next morning's 9:30 launch.
LOCKDIR="/tmp/ibkr-engine-launch.lockdir"
LOCK_FILE="/tmp/ibkr-engine-launch.lock"
EXIT_STATE_FILE="/tmp/ibkr-engine.last_exit"

mkdir -p "$LOG_DIR"

stop_pid() {
    local pid="$1"
    local source="$2"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] stop-ibkr-engine.sh: stopping PID $pid (source=$source)" >> "$LOG_PATH"

    kill -TERM "$pid" 2>/dev/null
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] stop-ibkr-engine.sh: PID $pid exited gracefully" >> "$LOG_PATH"
            return 0
        fi
        sleep 1
    done

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] stop-ibkr-engine.sh: PID $pid did not exit in 10s -- sending SIGKILL" >> "$LOG_PATH"
    kill -KILL "$pid" 2>/dev/null
    return 0
}

# Cleanup launch-mutex state files. Called after the engine has been confirmed
# stopped (or even if no engine was running — leftover state from a prior day
# is exactly what we want to evict). The liveness check on the lockdir holder
# PID prevents racing with a *live* wrapper (e.g. a stale lockdir from a
# wrapper that happens to still be running for some reason).
cleanup_launch_state() {
    # 1. Lockdir (POSIX mkdir-atomic mutex). Verify holder PID is dead before
    #    removing — a live holder means a wrapper is mid-launch and we MUST
    #    not delete its mutex out from under it.
    if [ -d "$LOCKDIR" ]; then
        local HOLDER_PID=""
        if [ -f "$LOCKDIR/pid" ]; then
            HOLDER_PID=$(cat "$LOCKDIR/pid" 2>/dev/null || echo "")
        fi
        if [ -n "$HOLDER_PID" ] && kill -0 "$HOLDER_PID" 2>/dev/null; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] stop-ibkr-engine.sh: WARNING lockdir held by live PID $HOLDER_PID, NOT removing" >> "$LOG_PATH"
        else
            if [ -n "$HOLDER_PID" ]; then
                echo "[$(date '+%Y-%m-%d %H:%M:%S')] stop-ibkr-engine.sh: removing orphaned $LOCKDIR (holder PID $HOLDER_PID dead)" >> "$LOG_PATH"
            else
                echo "[$(date '+%Y-%m-%d %H:%M:%S')] stop-ibkr-engine.sh: removing orphaned $LOCKDIR (no holder PID recorded)" >> "$LOG_PATH"
            fi
            rm -rf "$LOCKDIR"
        fi
    fi

    # 2. flock-style lock file (used when flock(1) is on PATH — currently
    #    not installed on this host, but cheap to handle defensively).
    #    lsof guards against removing a lock held by a live process.
    if [ -f "$LOCK_FILE" ] && ! lsof "$LOCK_FILE" >/dev/null 2>&1; then
        rm -f "$LOCK_FILE"
    fi

    # 3. Last-exit sentinel — written by the reaper subshell in
    #    run-ibkr-engine.sh. Always safe to remove; it's a pure
    #    state file, not a mutex.
    rm -f "$EXIT_STATE_FILE"
}

# Primary path: pidfile
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        stop_pid "$PID" "pidfile"
    fi
    rm -f "$PID_FILE"
fi

# Fallback path: pgrep for run.py in our repo dir
FALLBACK_PIDS=$(pgrep -f "run\.py" 2>/dev/null | while read -r PID; do
    CWD=$(lsof -p "$PID" 2>/dev/null | awk '/cwd/ {print $NF; exit}')
    if [ -n "$CWD" ] && [ "$CWD" = "$REPO_DIR" ]; then
        echo "$PID"
    fi
done)

if [ -n "$FALLBACK_PIDS" ]; then
    for PID in $FALLBACK_PIDS; do
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] stop-ibkr-engine.sh: no pidfile, found orphan PID $PID via pgrep (cwd=$REPO_DIR)" >> "$LOG_PATH"
        stop_pid "$PID" "pgrep"
    done
fi

# Always run launch-state cleanup — even if no engine was running today,
# leftover state from a prior SIGKILL'd wrapper is exactly what blocks
# tomorrow's 9:30 launch. (TASK-2026-278)
cleanup_launch_state

exit 0