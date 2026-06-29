#!/bin/bash
# ibkr-engine-watchdog.sh — Cron-driven watchdog for ibkr_trader_engine
#
# Runs every 5 minutes (set in crontab). Behaviour:
#   - During market hours (9:30 AM - 4:00 PM ET, AND is_market_open.py = OPEN):
#       * If process is DOWN -> start it
#       * If process is UP   -> leave it alone
#   - Outside market hours:
#       * If process is UP   -> stop it (handles overnight crash recovery)
#       * If process is DOWN -> leave it alone (cron START will bring it up at 9:30 AM)
#
# Engine manages its own pidfile via run-ibkr-engine.sh / stop-ibkr-engine.sh.
#
# Mirrors extractor-watchdog.sh. The 9:30-16:00 ET window matches the crontab
# schedule (9:30 START, 16:05 STOP), giving the engine 5 minutes of overlap on
# both ends to flush any open position updates before SIGTERM.
#
# Launch critical section (the call to run-ibkr-engine.sh when the engine is
# DOWN during market hours) is guarded by the SAME flock-or-lockdir mutex that
# run-ibkr-engine.sh uses. That way a cron START at 09:35 and a watchdog tick
# at 09:35 (after the timer stagger lands) are mutually exclusive. See
# TASK-2026-269 / TASK-2026-274.

set -u

IS_OPEN_SCRIPT="/opt/homebrew/bin/python3 /Users/ubexbot/.openclaw/vault/vault/SharedResources/Scripts/is_market_open.py"
RUN_SCRIPT="/Users/ubexbot/.openclaw/scripts/run-ibkr-engine.sh"
STOP_SCRIPT="/Users/ubexbot/.openclaw/scripts/stop-ibkr-engine.sh"

ENGINE_REPO="/Users/ubexbot/.openclaw/workspace-venkat/ibkr_trader_engine"

WATCHDOG_LOG="/Users/ubexbot/logs/ibkr-engine-watchdog.log"
mkdir -p /Users/ubexbot/logs

# Launch mutex — MUST match run-ibkr-engine.sh so cron START and watchdog tick
# can't both pass the no-pidfile check.
LOCK_FILE="/tmp/ibkr-engine-launch.lock"
LOCKDIR="/tmp/ibkr-engine-launch.lockdir"
LOCK_STALE_SECS=300

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
    if [ -d "$LOCKDIR" ]; then
        if command -v stat >/dev/null 2>&1; then
            LOCKDIR_AGE=$(( $(date +%s) - $(stat -f %m "$LOCKDIR" 2>/dev/null || stat -c %Y "$LOCKDIR" 2>/dev/null || echo 0) ))
        else
            LOCKDIR_AGE=0
        fi
        if [ "$LOCKDIR_AGE" -gt "$LOCK_STALE_SECS" ]; then
            echo "[$(date '+%F %T')] watchdog: removing stale $LOCKDIR (age ${LOCKDIR_AGE}s)" >> "$WATCHDOG_LOG"
            rmdir "$LOCKDIR" 2>/dev/null || true
        fi
    fi
    if ! mkdir "$LOCKDIR" 2>/dev/null; then
        echo "[$(date '+%F %T')] watchdog: another launch in progress ($LOCKDIR held) — skipping this tick" >> "$WATCHDOG_LOG"
        return 1
    fi
    trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT
    return 0
}

release_lock() {
    if [ -n "${LOCK_FD:-}" ]; then
        flock -u "$LOCK_FD" 2>/dev/null || true
        exec 9>&- 2>/dev/null || true
    fi
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
    # Market open + process down -> restart (mutex-shared with run-ibkr-engine.sh)
    echo "[$TS] watchdog: ibkr_trader_engine DOWN during market hours -- restarting (after acquiring launch mutex)" >> "$WATCHDOG_LOG"
    if ! acquire_lock; then
        exit 0
    fi
    "$RUN_SCRIPT" >> "$WATCHDOG_LOG" 2>&1
    release_lock
else
    # Market closed + process down -> leave alone
    echo "[$TS] watchdog: ibkr_trader_engine DOWN outside market hours -- leaving (cron will start at 9:35 AM)" >> "$WATCHDOG_LOG"
fi

exit 0
