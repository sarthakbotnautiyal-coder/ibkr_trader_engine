# Cron Schedule — ibkr_trader_engine

This document is the **canonical reference** for the host crontab entries that
manage the `ibkr_trader_engine` lifecycle. The crontab itself lives on the host
(`crontab -e`) and is **not** checked into this repo. When this doc changes,
update the host crontab to match.

Last updated: 2026-06-29 (TASK-2026-268 follow-up — single-instruction design)

---

## Lifecycle pattern (single-instruction)

The engine lifecycle is managed by **one** cron instruction: the watchdog.
It is responsible for both **cold-start** (engine down at market open) and
**post-crash recovery** (engine dies mid-session). No separate START line.

This mirrors `premium_extractor` / `gex_extractor` exactly — they have always
used a single `*/5 * * * *` watchdog because there is no need for two
instructions when one is sufficient.

Helper scripts live in `/Users/ubexbot/.openclaw/scripts/` (host-managed, out of
repo scope).

---

## Current crontab (host)

```cron
# -- ibkr_trader_engine ----------------------------------------------------
# SPX 0DTE auto-trader -- single-instruction lifecycle (watchdog-only).
# The watchdog fires every 5 minutes during the trading day and is responsible
# for BOTH cold-start (engine down at market open) AND post-crash recovery.
# No separate START line. See docs/CRON.md for the design rationale.
# .env loaded by run-ibkr-engine.sh (gitignored secrets: TELEGRAM_BOT_TOKEN, etc.)
# STOP at 4:05 PM ET (Mon-Fri, market-aware) -- gives 5-min buffer for exit orders
5 16 * * 1-5 [ "$(/opt/homebrew/bin/python3 /Users/ubexbot/.openclaw/vault/vault/SharedResources/Scripts/is_market_open.py)" = "OPEN" ] && /Users/ubexbot/.openclaw/scripts/stop-ibkr-engine.sh >> /Users/ubexbot/logs/ibkr-engine-cron.log 2>&1
# WATCHDOG -- every 5 min, restarts crashed engine during market hours.
# This is the ONLY instruction that starts the engine.
*/5 * * * * /Users/ubexbot/.openclaw/scripts/ibkr-engine-watchdog.sh >> /Users/ubexbot/logs/ibkr-engine-watchdog.log 2>&1
```

---

## Cold-start timing

The watchdog fires every 5 minutes (`*/5 * * * *`) during the trading day. Its
behaviour at the start of a session:

| Time (ET)   | What happens                                                              |
|-------------|---------------------------------------------------------------------------|
| 09:30 open  | First watchdog tick at `:30` (or `:35` if `:30` already passed).           |
| ≤ 09:35     | Engine is DOWN; watchdog sees `MARKET_STATUS=OPEN && !is_alive` → starts. |
| ~09:30–09:35| Engine warm-up: connect to IBKR Gateway (~5s), load data sources (~10s), |
|             | first tick processed at ~30s. **30+ seconds of warm-up is normal.**      |
| 09:35+      | Engine is UP; watchdog sees `is_alive` → exits cleanly without action.   |

**Worst-case cold-start latency: 5 minutes** (the gap between two watchdog
ticks). This is acceptable because:

1. The engine's own connect-and-warm-up is 30+ seconds (IBKR Gateway handshake
   + data-source reads + first-tick evaluation).
2. At 09:30 sharp, the pre-market auction is still settling — the first minute
   of trading is often wide spreads and unreliable prints, so a 5-min delay
   to first-tick loses ~3 minutes of low-quality signals.
3. The watchdog's `acquire_lock` / `flock` / `lockdir` mutex
   (TASK-2026-269) makes it impossible for two watchdog ticks in the same
   5-minute window to fork duplicate processes even if they fire close together.

If the engine crashes mid-session (e.g. IBKR disconnect → engine exits), the
watchdog's next tick (≤ 5 minutes later) restarts it. The watchdog also has
duplicate-instance backoff (TASK-2026-276) to avoid restart-loops on exit
code 2.

---

## Why single-instruction, not two

On **2026-06-29 at 09:30:00 ET**, two cron jobs fired in the same second:

- `30 9 * * 1-5 ...run-ibkr-engine.sh` (START)
- `*/5 * * * * ...ibkr-engine-watchdog.sh` (WATCHDOG — `:00`, `:05`, ...,
  `:30`, `:35`, ...)

Both saw no pidfile. Both forked `run.py`. Both tried to claim IBKR `clientId
31`. Result: **two engine instances fought for the same clientId for ~3h 40m**
before the duplicate was detected.

The lessons:

1. **One instruction is structurally safer than two.** With one instruction
   there is no possible collision by construction — only one instruction can
   start the engine. Two instructions require non-trivial coordination
   (mutex, stagger, failfast) to avoid the same bug recurring.
2. **Defence-in-depth still matters.** Even with one instruction, the
   watchdog's launch mutex (`acquire_lock`) keeps two simultaneous ticks
   (e.g. on a host wake-from-sleep race) from forking duplicates. The engine
   itself fails fast on duplicate `clientId` (TASK-2026-275, exit code 2) so
   any collision is detected within one tick.
3. **The watchdog already does cold-start.** `ibkr-engine-watchdog.sh`
   branches on `MARKET_STATUS=OPEN && !is_alive` and calls
   `run-ibkr-engine.sh` after acquiring the launch mutex. No separate START
   line is needed.

PR #14 (TASK-2026-268) attempted to stagger the START line from `:30` to `:35`
to avoid the `:30` watchdog tick. That was a band-aid — the proper fix is
to remove the duplicate START line entirely, which is what this doc and the
matching host crontab edit do.

---

## Applying the change

The crontab is host-managed. After this PR is merged, run this **one-time**
command to remove the redundant START line from the host crontab:

```bash
# Backup first
crontab -l > /tmp/crontab.backup-$(date +%Y-%m-%d-%H%M)

# Edit and remove the line starting with "35 9 * * 1-5" that references
# run-ibkr-engine.sh. Keep the */5 watchdog and the 5 16 STOP lines.
crontab -e
```

The line to **remove**:

```
35 9 * * 1-5 [ "$(/opt/homebrew/bin/python3 ...is_market_open.py)" = "OPEN" ] && /Users/ubexbot/.openclaw/scripts/run-ibkr-engine.sh ...
```

The lines to **keep**:

```
5 16 * * 1-5 [ ... ] && /Users/ubexbot/.openclaw/scripts/stop-ibkr-engine.sh ...
*/5 * * * * /Users/ubexbot/.openclaw/scripts/ibkr-engine-watchdog.sh ...
```

Verify after the edit:

```bash
crontab -l | grep -E "ibkr-engine\.sh|ibkr_trader_engine|ibkr-engine-watchdog"
# Expected: 2 lines (STOP + WATCHDOG). The START line is gone.
```

---

## Disabling for a day

To skip the engine for a single trading day (e.g. a known-bad data day), set
`dry_run: true` in `config/config.yaml` and `git pull` on the host. The
engine will still start at the next watchdog tick, but place no orders. To
also prevent the engine from starting entirely, comment out the watchdog
line in the crontab for that day, then restore it before the next session.

To pause for an extended period (vacation, IBKR maintenance window), comment
out both the STOP and the WATCHDOG lines. The engine will stay down until
you re-enable them.

---

## Inspecting the state

```bash
# Is the engine running?
pgrep -fl "ibkr_trader_engine.*run\.py"

# When was the last tick?
tail -n 5 /Users/ubexbot/logs/ibkr_engine.log    # adjust path to actual log

# What has the watchdog done today?
tail -n 30 /Users/ubexbot/logs/ibkr-engine-watchdog.log

# What did the STOP script do at 16:05?
tail -n 20 /Users/ubexbot/logs/ibkr-engine-cron.log

# What does the crontab look like right now?
crontab -l | grep -E "ibkr"
```

---

## Related

- **TASK-2026-268 follow-up (this doc)** — drop the redundant START line;
  watchdog-only design.
- **TASK-2026-268 (PR #14)** — original stagger attempt (`30 9` → `35 9`).
  Superseded by this PR.
- **TASK-2026-269 (PR #16)** — `flock` / `lockdir` mutex on the wrapper so
  concurrent `run-ibkr-engine.sh` invocations are idempotent.
- **TASK-2026-274** — same mutex on the watchdog launch path.
- **TASK-2026-275 (PR #17)** — engine fail-fast on duplicate `clientId`
  (exit code 2).
- **TASK-2026-276 (PR #19)** — watchdog backoff on exit code 2 so the
  restart-loop doesn't hammer the duplicate.
- **TASK-2026-278** — parent incident (two engine instances, ~3h 40m
  downtime) that motivated all of the above.