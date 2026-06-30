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
#   If the lockdir exists we first verify the holder PID (stored in
#   $LOCKDIR/pid) is alive. A dead holder PID means a previous wrapper
#   crashed without cleaning up — log it, remove the lockdir, retry.
#   Only if the PID file is missing do we fall back to age-based
#   detection (LOCK_STALE_SECS = 60s). 5 min was too long for a crashed
#   wrapper to be considered stale; 60s is short enough that an
#   orphaned lockdir does not block the watchdog for a full tick.
#   treat it as a leftover from a previous SIGKILL'd wrapper and try to clean
#   it before bailing. Cleanup is best-effort — the worst case is the next
#   wrapper also fails and the watchdog continues to retry.
#
# ─────────────────────────────────────────────────────────────────────────────
# Last-exit sentinel for watchdog backoff (TASK-2026-276)
# ─────────────────────────────────────────────────────────────────────────────
# After the engine is forked, a tiny reaper subshell waits on the child PID in
# the background and writes /tmp/ibkr-engine.last_exit = "<exit_code> <epoch>"
# once the engine actually exits. The watchdog reads this sentinel to detect
# exit code 2 (duplicate clientId — added in PR #17 / TASK-2026-275) and
# back off 15 min instead of entering a 5-min restart loop.
#
# The reaper is a separate process so the wrapper's main flow stays
# fire-and-forget — cron START at 09:35 returns 0 promptly. The reaper's
# SIGKILL'd-tail caveat is benign: at worst the sentinel file is missing and
# the watchdog falls through to the default restart path.

set -u

REPO_DIR="/Users/ubexbot/.openclaw/workspace-venkat/ibkr_trader_engine"
PYTHON_PATH="$REPO_DIR/venv/bin/python"
LOG_FILE="ibkr_engine.log"

PID_FILE="$REPO_DIR/run.pid"
LOG_DIR="$REPO_DIR/logs"
LOG_PATH="$LOG_DIR/$LOG_FILE"

LOCK_FILE="/tmp/ibkr-engine-launch.lock"
LOCKDIR="/tmp/ibkr-engine-launch.lockdir"
LOCK_STALE_SECS=60

# Last-exit sentinel read by ibkr-engine-watchdog.sh (TASK-2026-276).
EXIT_STATE_FILE="/tmp/ibkr-engine.last_exit"

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
    #
    # PID-liveness stale-detection (TASK-2026-stale-lockdir):
    # The lockdir holds a pid file ($LOCKDIR/pid) with the WRAPPER's PID
    # (written immediately after mkdir succeeds, below). If the lockdir
    # exists when we enter, we verify the recorded PID is still alive
    # before bailing idempotent. A dead PID means a previous wrapper
    # crashed (SIGKILL or OOM) without running the EXIT trap — the
    # lockdir is stale regardless of its mtime, and we remove it and
    # retry. This fixes the 2026-06-30 incident where a 2-second-old
    # orphaned lockdir slipped past the 300s mtime check and the
    # watchdog bailed 5 consecutive times (09:30, 09:35, 09:40, 09:45,
    # 09:50) without starting the engine.
    if [ -d "$LOCKDIR" ]; then
        STALE_HOLDER=""
        if [ -f "$LOCKDIR/pid" ]; then
            HOLDER_PID=$(cat "$LOCKDIR/pid" 2>/dev/null || echo "")
            if [ -n "$HOLDER_PID" ] && ! kill -0 "$HOLDER_PID" 2>/dev/null; then
                STALE_HOLDER="$HOLDER_PID"
            fi
        else
            # No pid file — fall back to age-based detection.
            if command -v stat >/dev/null 2>&1; then
                LOCKDIR_AGE=$(( $(date +%s) - $(stat -f %m "$LOCKDIR" 2>/dev/null || stat -c %Y "$LOCKDIR" 2>/dev/null || echo 0) ))
            else
                LOCKDIR_AGE=0
            fi
            if [ "$LOCKDIR_AGE" -gt "$LOCK_STALE_SECS" ]; then
                STALE_HOLDER="unknown-age-${LOCKDIR_AGE}s"
            fi
        fi
        if [ -n "$STALE_HOLDER" ]; then
            echo "[$(date '+%F %T')] run-ibkr-engine.sh: stale lockdir — holder PID $STALE_HOLDER is dead, removing $LOCKDIR" >> "$LOG_PATH"
            # rm -f first so rmdir doesn't fail on a non-empty lockdir.
            rm -f "$LOCKDIR/pid" 2>/dev/null || true
            rmdir "$LOCKDIR" 2>/dev/null || rm -rf "$LOCKDIR" 2>/dev/null || true
        fi
    fi
    if ! mkdir "$LOCKDIR" 2>/dev/null; then
        # Lost the race AFTER cleaning a stale holder — someone else got it
        # between our rmdir and mkdir. Treat as held; bail idempotent.
        echo "[$(date '+%F %T')] run-ibkr-engine.sh: another invocation holds $LOCKDIR — exiting 0 idempotent" >> "$LOG_PATH"
        return 1
    fi
    # Record this wrapper's PID inside the lockdir so a future invocation
    # can verify liveness. $$ is the bash process, which is what actually
    # holds the mutex (the engine runs as a disowned grandchild).
    echo "$$" > "$LOCKDIR/pid" 2>/dev/null || true
    # Best-effort cleanup on graceful exit. SIGNAL won't run the trap —
    # that's the known SIGKILL limitation; PID-liveness check above
    # mitigates it. Remove the pid file BEFORE rmdir since rmdir refuses
    # to remove a non-empty directory.
    trap 'rm -f "$LOCKDIR/pid" 2>/dev/null; rmdir "$LOCKDIR" 2>/dev/null || rm -rf "$LOCKDIR" 2>/dev/null || true' EXIT
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
# Last-exit sentinel helpers (TASK-2026-276)
# ---------------------------------------------------------------------------
# Spawn an asynchronous reaper that waits on $1 (the engine PID) and writes
# the sentinel file when it dies. The reaper outlives the wrapper — cron
# START at 09:35 returns 0 immediately, the reaper persists, and writes the
# state file when the engine later exits (minutes or hours later).
spawn_exit_reaper() {
    local engine_pid="$1"
    if [ -z "$engine_pid" ]; then
        return 0
    fi
    # Use a subshell with explicit fd redirections to avoid leaking the
    # wrapper's log_path / state-file handles. The reaper is disowned so it
    # survives the wrapper's exit; if the wrapper is SIGKILL'd the reaper
    # inherits the engine as init-adopted and still completes its wait.
    (
        wait "$engine_pid"
        local rc=$?
        echo "$rc $(date +%s)" > "$EXIT_STATE_FILE"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] run-ibkr-engine.sh: reaper wrote $EXIT_STATE_FILE = $rc for PID $engine_pid" >> "$LOG_PATH"
    ) &
    disown 2>/dev/null || true
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

# TASK-2026-276: spawn the exit-code reaper BEFORE the 2s health check so the
# reaper attaches to the engine PID regardless of whether the engine survives
# past the 2s window. The reaper is fire-and-forget — see spawn_exit_reaper().
spawn_exit_reaper "$NEW_PID"

# Give the process 2 seconds to either crash or settle, then verify.
sleep 2
if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] run-ibkr-engine.sh: started successfully (PID $NEW_PID)" >> "$LOG_PATH"
    release_lock
    exit 0
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] run-ibkr-engine.sh: process died within 2s of launch (PID $NEW_PID)" >> "$LOG_PATH"
    rm -f "$PID_FILE"
    # The reaper will write the sentinel with exit code 143/137 (SIGTERM/SIGKILL)
    # OR the engine's own sys.exit code if the python side raced ahead — the
    # watchdog treats any non-2 exit as "no backoff", so this is safe.
    release_lock
    exit 1
fi
