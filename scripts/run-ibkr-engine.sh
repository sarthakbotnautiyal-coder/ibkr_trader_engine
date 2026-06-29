#!/bin/bash
# run-ibkr-engine.sh — Start the IBKR trader engine with pidfile management + .env loading
#
# Usage: run-ibkr-engine.sh
#
# Mirrors run-extractor.sh but with two engine-specific behaviours:
#   - Sources .env (gitignored secrets: TELEGRAM_BOT_TOKEN, etc.) before launch.
#   - Logs to logs/ibkr_engine.log inside the repo.
#
# Behaviour (same as run-extractor.sh):
#   - If a previous instance is still running, log it and exit 0 (idempotent).
#   - Otherwise: cd into repo, source .env, write PID to run.pid,
#     launch in background with stdin redirected from /dev/null,
#     disown so the cron-launched shell can exit cleanly.
#
# Launch critical section (pidfile-check + fork + write-pidfile + 2s health check)
# is guarded by a flock-or-lockdir mutex shared with ibkr-engine-watchdog.sh so two
# concurrent invocations (cron START vs. watchdog tick) cannot both pass the
# "no pidfile" TOCTOU check. See TASK-2026-269 / TASK-2026-274.
#
# Lock paths:
#   LOCK_FILE  = /tmp/ibkr-engine-launch.lock        (used when flock(1) is available)
#   LOCKDIR    = /tmp/ibkr-engine-launch.lockdir     (POSIX mkdir-atomic fallback)
#
# Lock STALENESS:
#   If the lockdir exists and is older than LOCK_STALE_SECS (300s = 5 min) we
#   treat it as a leftover from a previous SIGKILL'd wrapper and try to clean
#   it before bailing. Cleanup is best-effort — the worst case is the next
#   wrapper also fails and the watchdog continues to retry.

set -u

REPO_DIR="/Users/ubexbot/.openclaw/workspace-venkat/ibkr_trader_engine"
PYTHON_PATH="$REPO_DIR/venv/bin/python"
LOG_FILE="ibkr_engine.log"

PID_FILE="$REPO_DIR/run.pid"
LOG_DIR="$REPO_DIR/logs"
LOG_PATH="$LOG_DIR/$LOG_FILE"

LOCK_FILE="/tmp/ibkr-engine-launch.lock"
LOCKDIR="/tmp/ibkr-engine-launch.lockdir"
LOCK_STALE_SECS=300

mkdir -p "$LOG_DIR"

# ---------------------------------------------------------------------------
# flock-or-lockdir mutex (TASK-2026-269, TASK-2026-274)
# ---------------------------------------------------------------------------
LOCK_FD=""
acquire_lock() {
    # Prefer flock(1) if present on PATH; otherwise fall back to the POSIX
    # mkdir-atomic lockdir pattern that works on stock macOS.
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$LOCK_FILE"
        if ! flock -n 9; then
            echo "[$(date '+%F %T')] run-ibkr-engine.sh: another invocation holds $LOCK_FILE — exiting 0 idempotent" >> "$LOG_PATH"
            return 1
        fi
        LOCK_FD=9
        return 0
    fi
    # mkdir-based mutex (POSIX-atomic: mkdir is one syscall, succeeds once).
    if [ -d "$LOCKDIR" ]; then
        # Stale-detection: if the lockdir is older than LOCK_STALE_SECS, treat
        # it as a SIGKILL leftover and try to remove it.
        if command -v stat >/dev/null 2>&1; then
            LOCKDIR_AGE=$(( $(date +%s) - $(stat -f %m "$LOCKDIR" 2>/dev/null || stat -c %Y "$LOCKDIR" 2>/dev/null || echo 0) ))
        else
            LOCKDIR_AGE=0
        fi
        if [ "$LOCKDIR_AGE" -gt "$LOCK_STALE_SECS" ]; then
            echo "[$(date '+%F %T')] run-ibkr-engine.sh: removing stale $LOCKDIR (age ${LOCKDIR_AGE}s)" >> "$LOG_PATH"
            rmdir "$LOCKDIR" 2>/dev/null || true
        fi
    fi
    if ! mkdir "$LOCKDIR" 2>/dev/null; then
        echo "[$(date '+%F %T')] run-ibkr-engine.sh: another invocation holds $LOCKDIR — exiting 0 idempotent" >> "$LOG_PATH"
        return 1
    fi
    # Best-effort cleanup on graceful exit (SIGNAL won't run the trap — that's
    # the known SIGKILL limitation; stale-detection above mitigates it).
    trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT
    return 0
}

release_lock() {
    if [ -n "$LOCK_FD" ]; then
        flock -u "$LOCK_FD" 2>/dev/null || true
        exec 9>&- 2>/dev/null || true
    fi
    # lockdir cleanup is via the EXIT trap registered in acquire_lock().
}

# ---------------------------------------------------------------------------
# Launch critical section — guarded by the mutex
# ---------------------------------------------------------------------------
if ! acquire_lock; then
    exit 0
fi

# Idempotent: if a live process already owns the pidfile, do nothing.
if [ -f "$PID_FILE" ]; then
    EXISTING_PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [ -n "$EXISTING_PID" ] && kill -0 "$EXISTING_PID" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] run-ibkr-engine.sh: $REPO_DIR already running (PID $EXISTING_PID)" >> "$LOG_PATH"
        release_lock
        exit 0
    else
        # Stale pidfile — process died but file lingered. Clean up.
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] run-ibkr-engine.sh: removing stale pidfile (PID $EXISTING_PID)" >> "$LOG_PATH"
        rm -f "$PID_FILE"
    fi
fi

cd "$REPO_DIR" || { echo "[$(date '+%Y-%m-%d %H:%M:%S')] run-ibkr-engine.sh: failed to cd $REPO_DIR" >> "$LOG_PATH"; release_lock; exit 1; }

# Load gitignored .env into the environment. set -a auto-exports every var
# defined here, so the child Python process inherits them via os.environ.
if [ -f "$REPO_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$REPO_DIR/.env"
    set +a
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] run-ibkr-engine.sh: WARNING no .env at $REPO_DIR/.env — Telegram notifs will be skipped" >> "$LOG_PATH"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] run-ibkr-engine.sh: starting $REPO_DIR" >> "$LOG_PATH"

# Launch: redirect stdin from /dev/null, stdout+stderr to log file,
# run in background (&), disown so cron shell can exit cleanly.
nohup "$PYTHON_PATH" run.py >> "$LOG_PATH" 2>&1 < /dev/null &
NEW_PID=$!
disown

# Write pidfile atomically
echo "$NEW_PID" > "$PID_FILE"

# Give the process 2 seconds to either crash or settle, then verify.
sleep 2
if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] run-ibkr-engine.sh: started successfully (PID $NEW_PID)" >> "$LOG_PATH"
    release_lock
    exit 0
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] run-ibkr-engine.sh: process died within 2s of launch (PID $NEW_PID)" >> "$LOG_PATH"
    rm -f "$PID_FILE"
    release_lock
    exit 1
fi
