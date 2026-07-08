#!/bin/bash
# ibkr-engine-watchdog.sh — Cron-driven watchdog for ibkr_trader_engine
#
# Runs every 5 minutes (set in crontab). Behaviour:
#   - During market hours (9:30 AM - 4:00 PM ET, AND is_market_open.py = OPEN):
#       * If process is DOWN -> start it
#       * If process is UP   -> leave it alone
#   - Outside market hours:
#       * If process is UP   -> stop it (handles overnight crash recovery)
#       * If process is DOWN -> leave it alone (next watchdog tick in market
#                                  hours will start it; no separate cron
#                                  START line — single-instruction design,
#                                  see docs/CRON.md and TASK-2026-268
#                                  follow-up).
#
# Engine manages its own pidfile via run-ibkr-engine.sh / stop-ibkr-engine.sh.
#
# Mirrors extractor-watchdog.sh. The 9:30-16:00 ET window matches market hours
# (engine stops at 16:05 via the STOP cron, giving 5 minutes of overlap to
# flush any open position updates before SIGTERM).
#
# Launch critical section (the call to run-ibkr-engine.sh when the engine is
# DOWN during market hours) is guarded by the SAME flock-or-lockdir mutex that
# run-ibkr-engine.sh uses. That way two watchdog ticks fired close together
# (e.g. on a host wake-from-sleep race) cannot both fork duplicate processes.
# See TASK-2026-269 / TASK-2026-274.
#
# ─────────────────────────────────────────────────────────────────────────────
# Backoff on duplicate-instance exit (TASK-2026-276)
# ─────────────────────────────────────────────────────────────────────────────
# Engine (PR #17 / TASK-2026-275) now calls sys.exit(2) on the FIRST
# ClientIdInUse collision instead of entering a clientId-rotation retry-storm.
# That exit-2 is meaningful: another ibkr_trader_engine is already connected
# and we are the duplicate. Restarting immediately would just re-collide.
#
# To stop the 5-min restart loop from hammering the duplicate, this watchdog
# reads /tmp/ibkr-engine.last_exit (sentinel written by run-ibkr-engine.sh's
# child-reaper after the engine process exits) and:
#   - If exit_code == 2 AND age < 15 min  -> skip the restart, log backoff.
#   - If exit_code == 2 AND age > 30 min  -> safety valve: assume duplicate
#                                            has been killed/moved, restart.
#   - Otherwise (any other exit, missing file, malformed) -> restart normally.
#
# See TASK-2026-276. Pairs with PR #17 (engine failfast) and PR #18 (read-retry).

set -u

IS_OPEN_SCRIPT="/opt/homebrew/bin/python3 /Users/ubexbot/.openclaw/vault/vault/SharedResources/Scripts/is_market_open.py"
RUN_SCRIPT="/Users/ubexbot/.openclaw/scripts/run-ibkr-engine.sh"
STOP_SCRIPT="/Users/ubexbot/.openclaw/scripts/stop-ibkr-engine.sh"

ENGINE_REPO="/Users/ubexbot/.openclaw/workspace-venkat/ibkr_trader_engine"

WATCHDOG_LOG="/Users/ubexbot/logs/ibkr-engine-watchdog.log"
mkdir -p /Users/ubexbot/logs

# Launch mutex — MUST match run-ibkr-engine.sh so two watchdog ticks (or a
# watchdog tick concurrent with any future launch path) can't both pass the
# no-pidfile check.
LOCK_FILE="/tmp/ibkr-engine-watchdog.lock"
LOCKDIR="/tmp/ibkr-engine-watchdog.lockdir"
LOCK_STALE_SECS=60

# Duplicate-instance backoff (TASK-2026-276)
STATE_FILE="/tmp/ibkr-engine.last_exit"
DUPLICATE_BACKOFF_SECONDS=900   # 15 min — skip restart within this window
RECOVERY_SECONDS=1800           # 30 min — safety valve; older than this, ignore exit code

acquire_lock() {
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$LOCK_FILE"
        if ! flock -n 9; then
            echo "[$(date '+%F %T')] watchdog: another launch in progress ($LOCK_FILE held) — skipping this tick" >> "$WATCHDOG_LOG"
            return 1
        fi
        LOCK_FD=9
        return 0
    fi
    # PID-liveness stale-detection (TASK-2026-stale-lockdir):
    # Mirror of the run-ibkr-engine.sh fix. If the lockdir exists, verify
    # the holder PID (in $LOCKDIR/pid) is alive. A dead PID means the
    # previous launcher crashed without running its EXIT trap; the
    # lockdir is stale regardless of mtime, so clean it up and retry.
    if [ -d "$LOCKDIR" ]; then
        STALE_HOLDER=""
        if [ -f "$LOCKDIR/pid" ]; then
            HOLDER_PID=$(cat "$LOCKDIR/pid" 2>/dev/null || echo "")
            if [ -n "$HOLDER_PID" ] && ! kill -0 "$HOLDER_PID" 2>/dev/null; then
                STALE_HOLDER="$HOLDER_PID"
            fi
        else
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
            echo "[$(date '+%F %T')] watchdog: stale lockdir — holder PID $STALE_HOLDER is dead, removing $LOCKDIR" >> "$WATCHDOG_LOG"
            rm -f "$LOCKDIR/pid" 2>/dev/null || true
            rmdir "$LOCKDIR" 2>/dev/null || rm -rf "$LOCKDIR" 2>/dev/null || true
        fi
    fi
    if ! mkdir "$LOCKDIR" 2>/dev/null; then
        echo "[$(date '+%F %T')] watchdog: another launch in progress ($LOCKDIR held) — skipping this tick" >> "$WATCHDOG_LOG"
        return 1
    fi
    echo "$$" > "$LOCKDIR/pid" 2>/dev/null || true
    trap 'rm -f "$LOCKDIR/pid" 2>/dev/null; rmdir "$LOCKDIR" 2>/dev/null || rm -rf "$LOCKDIR" 2>/dev/null || true' EXIT
    return 0
}

release_lock() {
    if [ -n "${LOCK_FD:-}" ]; then
        flock -u "$LOCK_FD" 2>/dev/null || true
        exec 9>&- 2>/dev/null || true
    fi
}

# ---------------------------------------------------------------------------
# Backoff helpers (TASK-2026-276)
# ---------------------------------------------------------------------------
# Sentinel file format: "<exit_code> <epoch_timestamp>" — written by the child
# reaper in run-ibkr-engine.sh after the engine process exits. Returns 0 if
# the watchdog should back off (skip restart); 1 otherwise. The watchdog logs
# the rationale either way.
should_backoff_duplicate() {
    if [ ! -f "$STATE_FILE" ]; then
        return 1
    fi
    # Two whitespace-separated fields. Tolerate trailing whitespace.
    local code ts now age next_attempt_line
    read -r code ts < "$STATE_FILE" 2>/dev/null || return 1
    if [ -z "${code:-}" ] || [ -z "${ts:-}" ]; then
        # Malformed sentinel — treat as "no backoff" so we don't get stuck.
        return 1
    fi
    if [ "$code" != "2" ]; then
        # Any other exit code (0 = clean exit, 1 = wrapper-side error) — restart normally.
        return 1
    fi
    now=$(date +%s)
    age=$((now - ts))
    if [ "$age" -gt "$RECOVERY_SECONDS" ]; then
        # Safety valve: sentinel is older than the recovery window. Assume the
        # duplicate is gone and restart normally.
        return 1
    fi
    if [ "$age" -lt "$DUPLICATE_BACKOFF_SECONDS" ]; then
        # Within the backoff window — caller should skip the restart.
        return 0
    fi
    # Between DUPLICATE_BACKOFF_SECONDS (15 min) and RECOVERY_SECONDS (30 min):
    # allow the restart. The next run will write a fresh sentinel; if it
    # exits 2 again, the backoff kicks in for another 15 min.
    return 1
}

# Human-readable summary helper for the log line.
_human_time() {
    local epoch="$1"
    if command -v python3 >/dev/null 2>&1; then
        /opt/homebrew/bin/python3 -c "import datetime,sys; print(datetime.datetime.fromtimestamp(int(sys.argv[1])).strftime('%Y-%m-%d %H:%M:%S'))" "$epoch" 2>/dev/null && return 0
    fi
    date -r "$epoch" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "$epoch"
}

log_backoff_decision() {
    # Called when should_backoff_duplicate returns 0.
    local code ts age next_human
    read -r code ts < "$STATE_FILE" 2>/dev/null || return 0
    age=$(( $(date +%s) - ts ))
    next_human=$(_human_time $((ts + DUPLICATE_BACKOFF_SECONDS)))
    echo "[$(date '+%F %T')] watchdog: ibkr_trader_engine exit code $code detected (duplicate instance) — backing off. Last exit $(_human_time "$ts") (${age}s ago). Next attempt allowed at $next_human." >> "$WATCHDOG_LOG"
}

# Date check: is today a trading day?
DATE_STATUS=$($IS_OPEN_SCRIPT 2>/dev/null || echo "CLOSED")

# Time check: are we between 09:30 and 16:00 ET?
CURRENT_ET=$(TZ=America/New_York date '+%H%M')
TS=$(date '+%Y-%m-%d %H:%M:%S')

# Treat as market hours only when:
#   - is_market_open.py returns OPEN (date is a trading day, not a holiday)
#   - AND current ET time is between 0930 and 1559 inclusive
if [ "$DATE_STATUS" = "OPEN" ] && [ "$CURRENT_ET" -ge 930 ] && [ "$CURRENT_ET" -lt 1600 ]; then
    MARKET_STATUS="OPEN"
else
    MARKET_STATUS="CLOSED"
fi

is_alive() {
    local pid_file="$ENGINE_REPO/run.pid"
    if [ ! -f "$pid_file" ]; then
        return 1
    fi
    local pid
    pid=$(cat "$pid_file" 2>/dev/null || echo "")
    if [ -z "$pid" ]; then
        return 1
    fi
    kill -0 "$pid" 2>/dev/null
}

if is_alive; then
    # Process is up -- leave it alone
    exit 0
fi

if [ "$MARKET_STATUS" = "OPEN" ]; then
    # Market open + process down -> consider restart
    # TASK-2026-276: skip if the engine most recently exited with code 2 (a
    # duplicate instance is still holding the clientId). The state file
    # /tmp/ibkr-engine.last_exit is written by run-ibkr-engine.sh after the
    # engine process exits.
    if should_backoff_duplicate; then
        log_backoff_decision
        exit 0
    fi
    echo "[$TS] watchdog: ibkr_trader_engine DOWN during market hours -- restarting (after acquiring launch mutex)" >> "$WATCHDOG_LOG"
    if ! acquire_lock; then
        exit 0
    fi
    "$RUN_SCRIPT" >> "$WATCHDOG_LOG" 2>&1
    release_lock
else
    # Market closed + process down -> leave alone
    echo "[$TS] watchdog: ibkr_trader_engine DOWN outside market hours -- leaving (next market-hours watchdog tick will start it; single-instruction design per docs/CRON.md)" >> "$WATCHDOG_LOG"
fi

exit 0
