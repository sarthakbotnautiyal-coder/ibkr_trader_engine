# Cron Schedule — ibkr_trader_engine

This document is the **canonical reference** for the host crontab entries that
manage the `ibkr_trader_engine` lifecycle. The crontab itself lives on the host
(`crontab -e`) and is **not** checked into this repo. When this doc changes,
update the host crontab to match.

Last updated: 2026-06-29 (see TASK-2026-268)

---

## Lifecycle pattern

Same as `premium_extractor` / `gex_extractor`:

1. **START** at market open (9:35 AM ET, Mon–Fri, market-aware via
   `is_market_open.py`)
2. **STOP** at market close (4:05 PM ET, Mon–Fri, market-aware)
3. **WATCHDOG** fires every 5 minutes during the day to recover from crashes

Helper scripts live in `/Users/ubexbot/.openclaw/scripts/` (host-managed, out of
repo scope).

---

## Current crontab (host)

```cron
# -- ibkr_trader_engine ----------------------------------------------------
# SPX 0DTE auto-trader -- same lifecycle pattern as premium_extractor / gex_extractor
# .env loaded by run-ibkr-engine.sh (gitignored secrets: TELEGRAM_BOT_TOKEN, etc.)
# START at 9:35 AM ET (Mon-Fri, market-aware via is_market_open.py)
#   Staggered off :30 (was 9:30) to avoid collision with the */5 watchdog tick.
#   On 2026-06-29 the START and watchdog fired simultaneously at 09:30:00, both
#   forked run.py, two instances fought for client_id 31 for ~3h40m. Five
#   minutes gives the watchdog a clean window to settle and the pidfile to be
#   authoritative. See TASK-2026-268 and TASK-2026-278 (parent incident).
35 9 * * 1-5 [ "$(/opt/homebrew/bin/python3 /Users/ubexbot/.openclaw/vault/vault/SharedResources/Scripts/is_market_open.py)" = "OPEN" ] && /Users/ubexbot/.openclaw/scripts/run-ibkr-engine.sh >> /Users/ubexbot/logs/ibkr-engine-cron.log 2>&1
# STOP at 4:05 PM ET (Mon-Fri, market-aware) -- gives 5-min buffer for exit orders
5 16 * * 1-5 [ "$(/opt/homebrew/bin/python3 /Users/ubexbot/.openclaw/vault/vault/SharedResources/Scripts/is_market_open.py)" = "OPEN" ] && /Users/ubexbot/.openclaw/scripts/stop-ibkr-engine.sh >> /Users/ubexbot/logs/ibkr-engine-cron.log 2>&1
# WATCHDOG -- every 5 min, restarts crashed engine during market hours
*/5 * * * * /Users/ubexbot/.openclaw/scripts/ibkr-engine-watchdog.sh >> /Users/ubexbot/logs/ibkr-engine-watchdog.log 2>&1
```

---

## Why the 5-minute buffer matters

On **2026-06-29 at 09:30:00 ET**, two cron jobs fired in the same second:

- `30 9 * * 1-5 ...run-ibkr-engine.sh` (START)
- `*/5 * * * * ...ibkr-engine-watchdog.sh` (WATCHDOG — `:00`, `:05`, ...,
  `:30`, `:35`, ...)

Both saw no pidfile. Both forked `run.py`. Both tried to claim IBKR `clientId
31`. Result: **two engine instances fought for the same clientId for ~3h 40m**
before the duplicate was detected.

By moving START to `35 9 * * 1-5`:

- The 09:30 watchdog tick fires 5 minutes **before** the START cron.
- Any pre-existing (stale) process has its pidfile cleared by then.
- When START fires at 09:35, the pidfile is authoritative and the launch path
  is race-free.
- The watchdog tick at 09:35 still fires — if the engine crashed in the first
  5 minutes, the watchdog will restart it.

**Net effect:** no two `run-ibkr-engine.sh: starting` lines in the same second
on a clean open.

---

## Applying the change

The crontab is host-managed. After this PR is merged, run this **one-time**
command to apply the cadence change on the host:

```bash
# Show the current crontab, find the line starting with "30 9 * * 1-5" that
# references run-ibkr-engine.sh, and change it to start with "35 9 * * 1-5".
crontab -e
```

Find this line:

```
30 9 * * 1-5 [ "$(/opt/homebrew/bin/python3 ...is_market_open.py)" = "OPEN" ] && /Users/ubexbot/.openclaw/scripts/run-ibkr-engine.sh ...
```

Change the leading `30 9` to `35 9`. Save and exit. Verify with `crontab -l |
grep run-ibkr-engine`.

---

## Related

- **TASK-2026-268** — this task. Stagger the START cron.
- **TASK-2026-278** — parent incident (two engine instances, ~3h 40m downtime).
- **TASK-2026-269** — add `flock`/`lockdir` to the wrapper so concurrent
  `run-ibkr-engine.sh` invocations become idempotent even without the cron
  stagger.
- **TASK-2026-275** — engine fail-fast on duplicate `clientId` (exit code 2).
- **TASK-2026-277** — derive `clientId` from PID hash to preempt the collision
  class entirely.