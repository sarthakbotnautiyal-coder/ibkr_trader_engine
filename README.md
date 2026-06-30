# IBKR Trader Engine — SPX 0DTE Auto-Trading System

A Python-based auto-trading engine for SPX 0DTE (zero days to expiration) options strategies with real-time risk management, multi-source data integration, Telegram alerts, and optional Supabase cloud sync.

## ⚡ Quick Start (5 Steps)

```bash
# 1. Clone + venv + deps
cd /Users/ubexbot/.openclaw/workspace-venkat
git clone <repo> ibkr_trader_engine && cd ibkr_trader_engine
python3 -m venv venv && source venv/bin/activate
pip install --only-binary=:all: -r requirements.txt   # IMPORTANT: see Install Dependencies section

# 2. Configure credentials
cp .env.example .env       # then edit .env with TELEGRAM_BOT_TOKEN, TELEGRAM_SIGNALS_CHAT_ID
# Also update config/config.yaml: set ibkr.account_id, choose data_source_mode (local | cloud)

# 3. Start required data sources (separate terminals)
../gex_extractor/venv/bin/python3 ../gex_extractor/run.py
../premium_extractor/venv/bin/python3 ../premium_extractor/run.py
../tradingView_signal_generator/venv/bin/python3 ../tradingView_signal_generator/run.py

# 4. Start TWS or IB Gateway on port 7497 (paper) or 4001 (live), with API enabled

# 5. Run the engine (NOTE: must source .env manually — see "Running" section below)
bash -c 'set -a && source ./.env && set +a && exec python3 run.py'
```

For dry-run mode (`dry_run: true` in `config/config.yaml`), no real money is risked. Start there.

---

## 🎯 Features

- ✅ **Real-time Entry/Exit Logic** — VIX-adaptive parameters, RSI gates, premium thresholds
- ✅ **Risk Management** — Day gate volatility protection, margin limits, position tracking
- ✅ **Multi-Source Data** — GEX snapshots, option premiums, technical indicators
- ✅ **Live Trading** — IBKR order execution with fill confirmation and polling
- ✅ **Dry-Run Mode** — Test strategies without placing real orders
- ✅ **Telegram Alerts** — Real-time entry/exit/rejection notifications
- ✅ **Comprehensive Logging** — Daily rotating logs by Eastern Time
- ✅ **Backtesting** — Historical simulation with clock override
- ✅ **Cloud Sync** — Optional Supabase dual-write for analytics (CLOUD mode)
- ✅ **Auto-restart** — Watchdog pattern via cron (see "Running 24/7" below)

---

## 📋 Prerequisites

### System Requirements
- **Python**: **3.10 or higher** (code uses PEP 604 union syntax: `str | None`)
- **OS**: macOS, Linux (tested on macOS 14.x)
- **Network**: Stable internet connection for IBKR, Telegram

### Required Services

1. **Interactive Brokers (IBKR)** — TWS or IB Gateway, see "TWS API Setup" below
2. **GEX Extractor** (sibling project) — writes GEX data to either `gex.db` (LOCAL) or Supabase `trading.gex_snapshots` (CLOUD)
3. **Premium Extractor** (sibling project) — writes scanner data to either `scanner.db` (LOCAL) or Supabase `trading.scan_results` (CLOUD)
4. **TradingView Signal Generator** (sibling project) — writes TV indicators to either `tradingview.db` (LOCAL) or Supabase `trading.trading_view_indicators` (CLOUD)
5. **Telegram** — for entry/exit notifications (see "Telegram Bot Setup" below)

### Optional
- **Supabase** — for cloud data archival and cross-machine engine runs (see "Cloud Mode" below)

---

## 🚀 Installation

### Step 1: Clone the Repository

```bash
cd /Users/ubexbot/.openclaw/workspace-venkat
git clone <repository_url> ibkr_trader_engine
cd ibkr_trader_engine
```

### Step 2: Create Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### Step 3: Install Dependencies

```bash
pip install --only-binary=:all: -r requirements.txt
```

> **IMPORTANT — Always use `--only-binary=:all:`.**
>
> Without it, `pip` tries to build PyYAML 6.0 and pydantic 2.5.0 from source on Python 3.14+, which fails (Cython `cython_sources` compat issue + no pydantic-core 2.14.1 wheel for 3.14). The build error is non-obvious and leaves the venv in a half-installed state — engine then fails at startup with `ModuleNotFoundError: No module named 'yaml'` (or similar). The `--only-binary=:all:` flag forces pip to use prebuilt wheels (PyYAML 6.0.3, pydantic 2.13.x) and sidesteps the build issue entirely.
>
> **Note:** `requirements.txt` pins all required packages including `supabase==2.31.0`. PyYAML, pydantic, and python-dotenv use minimum-version pins (`>=...`) because their exact pins lack Python 3.14 wheels for transitive deps. The codebase works fine with the newer versions — verified by `run.py --test`.

**Verify the install before starting the engine:**

```bash
./venv/bin/python run.py --test
```

This runs a quick smoke test (imports + DB init + read latest scan/GEX) and exits. If any required dep is missing you'll see `ModuleNotFoundError` here instead of after launching the engine and waiting for the first tick.

### Step 4: Verify Data Sources

Choose your data source mode (LOCAL or CLOUD) in `config/config.yaml`:

```yaml
data_source_mode: "local"   # or "cloud" for Supabase
```

**For LOCAL mode** (default, simplest):

```bash
ls ../gex_extractor/data/gex.db                      # should exist
ls ../premium_extractor/data/scanner.db              # should exist
ls ../tradingView_signal_generator/data/tradingview.db  # should exist
```

If any are missing, start those extractors first (they create the DBs on first run). See "Starting the Data Extractors" below.

**For CLOUD mode**: see "Cloud Mode (Supabase)" below.

---

## 🔧 Configuration

### Step 1: Environment Variables (`.env`)

```bash
cp .env.example .env
chmod 600 .env   # owner-only access
```

Edit `.env`:

```bash
# ========================
# TELEGRAM NOTIFICATIONS (Required)
# ========================
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_SIGNALS_CHAT_ID=-1001234567890

# ========================
# SUPABASE (Optional, only for CLOUD mode)
# ========================
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_APP_ID=sb_publishable_xxxx   # or sb_secret_xxxx for write access
```

### Step 2: Telegram Bot Setup

1. Open Telegram, search `@BotFather`, send `/newbot`
2. Follow prompts (bot name, unique username ending in `_bot`)
3. Copy the token into `.env` as `TELEGRAM_BOT_TOKEN`
4. Add the bot to your signal group and make it an **Admin** (required to post)
5. Get the chat ID:
   ```bash
   # Send a test message in the group first, then:
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" | python3 -m json.tool
   # Look for "chat":{"id":-100XXXXXXXXX,...}
   ```
6. Paste the chat ID into `.env` as `TELEGRAM_SIGNALS_CHAT_ID`
7. Test:
   ```bash
   bash -c 'set -a && source ./.env && set +a && python3 -c "from src.telegram_notifier import send_telegram_message; print(send_telegram_message(\"test\"))"'
   ```

### Step 3: TWS API Setup

For TWS (Trader Workstation) or IB Gateway:

1. Open TWS, log in
2. Go to **Edit → Global Configuration → API → Settings**
3. Check **"Enable ActiveX and Socket Clients"**
4. Set **Socket port** to match `config/config.yaml`:
   - `7497` = paper trading
   - `4001` = live trading
5. **Uncheck** "Read-Only API" (engine needs to place orders)
6. Add `127.0.0.1` to **Trusted IPs** (or `0.0.0.0/0` for any local client)
7. Click **OK** and restart TWS for changes to take effect

### Step 4: Trading Configuration (`config/config.yaml`)

Key parameters to review:

```yaml
# Run mode
data_source_mode: "local"  # "local" or "cloud" (Supabase)
dry_run: true              # CRITICAL: set to false ONLY after thorough testing

# IBKR connection
ibkr:
  host: "127.0.0.1"
  port: 7497               # 7497=paper, 4001=live
  account_id: "U13498586"  # YOUR account ID

# Market hours (ET)
market:
  entry_start: "09:00"
  entry_end: "16:00"       # stop taking new entries

# Data source paths (LOCAL mode only)
data_sources:
  gex_db: "../gex_extractor/data/gex.db"
  scanner_db: "../premium_extractor/data/scanner.db"
  tradingview_db: "../tradingView_signal_generator/data/tradingview.db"
```

> ⚠️ **Before setting `dry_run: false`:** test in dry-run for at least 1 full trading day, verify all data sources are populated, and verify Telegram works.

---

## ▶️ Running the Engine

### ⚠️ Important: `.env` is NOT auto-loaded by `run.py`

`run.py` reads env vars directly from `os.environ`. Only the Supabase writers (`supabase_gex_writer.py`, `supabase_scanner_writer.py`) call `load_dotenv()`. You must source `.env` before running:

```bash
bash -c 'set -a && source ./.env && set +a && exec python3 run.py'
```

This exports all `.env` vars to the shell environment, then `exec` replaces the shell with Python (so the engine inherits them).

### Run Modes

#### Smoke test (no IBKR connection, no trading)
```bash
bash -c 'set -a && source ./.env && set +a && python3 run.py --test'
```
Verifies config loads, all data sources are reachable, and Supabase (if CLOUD) credentials work. No orders, no IBKR connection.

#### Dry-run mode (real data, no real orders)
```bash
# config/config.yaml: dry_run: true
bash -c 'set -a && source ./.env && set +a && python3 run.py'
```
- ✅ All signals generated, positions tracked in `data/positions.db`
- ✅ Entry/exit decisions logged to console and `logs/engine.YYYY-MM-DD.log`
- ✅ Telegram messages sent
- ✅ **No real orders** placed with IBKR

#### Live mode (real trading)
```bash
# config/config.yaml: dry_run: false
bash -c 'set -a && source ./.env && set +a && python3 run.py'
```
⚠️ **Trades real positions with real money.** Start with `contracts_per_trade: 1`.

#### Backtest mode (replay historical data)
```bash
bash -c 'set -a && source ./.env && set +a && python3 -m src.backtest --date 2026-05-15'
bash -c 'set -a && source ./.env && set +a && python3 -m src.backtest --date 2026-05-15 --verbose'
bash -c 'set -a && source ./.env && set +a && python3 -m src.backtest --date 2026-05-15 --summary-only'
```
- **LOCAL mode only** — backtest reads from `gex.db`, `scanner.db`, `tradingview.db`
- **DRY_RUN forced** — no real trades even in live config
- Results written to `data/backtest.db` (tables: `backtest_signals`, `backtest_positions`)
- `--run-id YYYY-MM-DD_custom` lets you save multiple runs per date
- 0DTE expiry: any still-open positions at end of run are marked `status='expired'` with `exit_reason='expired_0dte'`

### Starting the Data Extractors

Before running the engine, start the three sibling extractors (each in its own terminal or via the watchdog pattern):

```bash
# Terminal 1: GEX extractor
cd /Users/ubexbot/.openclaw/workspace-venkat/gex_extractor
source venv/bin/activate
python3 run.py

# Terminal 2: Premium extractor
cd /Users/ubexbot/.openclaw/workspace-venkat/premium_extractor
source venv/bin/activate
python3 run.py

# Terminal 3: TradingView signal generator
cd /Users/ubexbot/.openclaw/workspace-venkat/tradingView_signal_generator
source venv/bin/activate
python3 run.py
```

> The `run-extractor.sh` watchdog (in `../scripts/`) auto-restarts these via cron every 5 minutes. See "Running 24/7" below.

### Running 24/7 (Auto-Restart)

For a production setup, use the watchdog pattern (already used for the extractors):

```bash
# Create a watchdog script: /Users/ubexbot/.openclaw/scripts/ibkr-engine-watchdog.sh
#!/bin/bash
# Watchdog for ibkr_trader_engine. Cron entry: */5 * * * * /path/to/this/script.sh
# IMPORTANT: use ./venv/bin/python3 — the system /opt/homebrew/bin/python3 does
# NOT have the engine's deps (PyYAML, ib-async, etc.) and will fail at startup
# with ModuleNotFoundError. See "Install Dependencies" section above.
PIDFILE=/Users/ubexbot/.openclaw/workspace-venkat/ibkr_trader_engine/run.pid
if [ -f "$PIDFILE" ] && kill -0 $(cat "$PIDFILE") 2>/dev/null; then
  exit 0
fi
cd /Users/ubexbot/.openclaw/workspace-venkat/ibkr_trader_engine
nohup bash -c 'set -a && source ./.env && set +a && exec ./venv/bin/python3 run.py' \
  >> logs/engine_$(date +%Y-%m-%d).log 2>&1 < /dev/null &
echo $! > "$PIDFILE"
```

Add to crontab (`crontab -e`):
```cron
@reboot /Users/ubexbot/.openclaw/scripts/ibkr-engine-watchdog.sh
*/5 * * * * /Users/ubexbot/.openclaw/scripts/ibkr-engine-watchdog.sh
```

> **Production cadence (WATCHDOG + STOP, single-instruction design) is
> documented in [`docs/CRON.md`](docs/CRON.md).** The simple watchdog-only
> pattern above works for development. Production uses the same single
> watchdog (`*/5 * * * *`) which handles **both** cold-start at market open
> and post-crash recovery, plus a market-aware STOP at 16:05 ET. There is no
> separate START line — the watchdog fires within 5 minutes of market open
> and starts the engine. This is structurally race-free: only one cron
> instruction can ever start the engine. See TASK-2026-268 (PR #14 stagger
> attempt) and its follow-up PR which removed the redundant START line
> entirely. The crontab itself lives on the host, not in this repo.

---

## ☁️ Cloud Mode (Supabase)

CLOUD mode reads from Supabase instead of local SQLite. Useful when:
- Running the engine on a different machine from the extractors
- Wanting shared state across multiple engine instances
- Backing up data for analytics

### Setup

1. **Create a Supabase project** at https://supabase.com (free tier is enough for a few months)

2. **Create the schema and tables** in the Supabase SQL editor:
   ```sql
   CREATE SCHEMA IF NOT EXISTS trading;
   
   CREATE TABLE trading.gex_snapshots (
     id BIGSERIAL PRIMARY KEY,
     ticker TEXT NOT NULL,
     captured_at TIMESTAMPTZ NOT NULL,
     -- ... other columns matching gex_extractor schema
   );
   
   CREATE TABLE trading.scan_results (
     id BIGSERIAL PRIMARY KEY,
     ticker TEXT NOT NULL,
     captured_at TIMESTAMPTZ NOT NULL,
     -- ... other columns matching premium_extractor schema
   );
   
   CREATE TABLE trading.trading_view_indicators (
     id BIGSERIAL PRIMARY KEY,
     ticker TEXT NOT NULL,
     captured_at TIMESTAMPTZ NOT NULL,
     -- ... other columns matching TradingView signal generator schema
   );
   ```
   (Full schema: see sibling extractor `supabase_*_writer.py` files for the exact column list.)

3. **Get your credentials** from Supabase dashboard → Settings → API:
   - **Project URL** → `SUPABASE_URL`
   - **Publishable key** (`sb_publishable_...`) → `SUPABASE_APP_ID`
   - **Secret key** (`sb_secret_...`) → for extractors writing to Supabase

4. **Configure extractors** to write to Supabase: add the same `SUPABASE_URL` and `SUPABASE_SECRET_KEY` to each extractor's `.env` file. Each extractor's `supabase_*_writer.py` will auto-detect these and dual-write.

5. **Configure engine** to read from Supabase:
   ```yaml
   # config/config.yaml
   data_source_mode: "cloud"
   ```
   And in `.env`:
   ```bash
   SUPABASE_URL=https://your-project.supabase.co
   SUPABASE_APP_ID=sb_publishable_xxxx
   ```

6. **Test** the connection:
   ```bash
   bash -c 'set -a && source ./.env && set +a && python3 run.py --test'
   ```
   If credentials are wrong, the test will fail with `DataSourceError: CLOUD mode requires SUPABASE_APP_ID environment variable`.

### Local ↔ Cloud Switching

You can switch modes by changing `data_source_mode` and restarting. Local data is not lost — CLOUD mode just ignores local `.db` files.

---

## 📊 Monitoring & Logs

### Real-Time Console

```bash
tail -f logs/engine.$(date +%Y-%m-%d).log
tail -f logs/engine.$(date +%Y-%m-%d).log | grep -E "ENTRY|EXIT|DAY_GATE"
tail -f logs/engine.$(date +%Y-%m-%d).log | grep -i error
```

### Key Log Patterns

**Entry signal:**
```
[LIVE] 🚀 ENTRY | CALL | 4500/4510 | $2.80 credit | 1 contract | SPX=4500 | EM=15.0 | fill=$2.80
```

**Exit signal:**
```
[LIVE] ✅ EXIT | CALL | 4500/4510 | 1 contract | P&L: +$280 | L1 crossed
```

**Day gate:**
```
🚨 DAY GATE BLOCKED | 2026-06-20 13:45:00 ET
New entries suspended — danger signals fired:
  GEX-OI=-2.5 ❌  |  Dist=0.8 ❌  |  RSI=88.0 ❌  (n=3 ticks)
```

### Position Tracking

```bash
# Open positions
sqlite3 data/positions.db "SELECT * FROM positions WHERE status='open';"

# Recent trades
sqlite3 data/positions.db "SELECT * FROM trades ORDER BY entry_ts DESC LIMIT 10;"

# Backtest results
sqlite3 data/backtest.db "SELECT * FROM backtest_positions WHERE run_id='2026-05-15';"
```

---

## 🏗️ Architecture Overview

### Components

```
src/
├── engine.py                   # Main event loop, orchestration
├── risk_manager.py             # Entry/exit logic, VIX-adaptive parameters
├── day_gate.py                 # Volatility protection (GEX, RSI, distance)
├── position_store.py           # Open position tracking (SQLite)
├── executor.py                 # Order execution, fill confirmation
├── trades_db.py                # Trade history database
├── blocking_ib_client.py       # IBKR Gateway communication (blocking API)
├── telegram_notifier.py        # Telegram message sending
│
├── combined_reader.py          # Multi-source data orchestrator
│   ├── LocalSource             # reads from local SQLite DBs
│   └── CloudSource             # reads from Supabase
├── data_sources.py             # Supabase client, CLOUD mode helpers
├── gex_reader.py               # Read GEX snapshots from gex.db
├── scanner_reader.py           # Read option premiums from scanner.db
├── tradingview_reader.py       # Read technical indicators from tradingview.db
│
├── supabase_gex_writer.py      # Dual-write gex.db → Supabase (used by gex_extractor)
├── supabase_scanner_writer.py  # Dual-write scanner.db → Supabase (used by premium_extractor)
│
├── backtest.py                 # Historical replay (--date 2026-05-15)
├── backtest_db.py              # Backtest result storage
├── backtests_db.py             # Backtest metadata
│
├── tick_processor.py           # Per-tick entry/exit evaluation
├── contracts.py                # SPX options contract definitions
│
├── log_setup.py                # Logging config (daily ET rotation)
└── config/                     # config package (loads config.yaml)
    ├── __init__.py
    └── config.yaml
```

### Data Flow (LOCAL mode)

```
Market Open (9:30 AM ET)
  ↓
Engine starts: run.py → AutoTraderEngine.run()
  ↓
Load config (config/config.yaml) + .env (must be sourced manually)
  ↓
Connect to IBKR Gateway (blocking_ib_client.py on 127.0.0.1:7497)
  ↓
Every 30 seconds (tick loop):
  ├─ LocalSource.get_combined_for_latest_scan() → gex.db + scanner.db + tradingview.db
  ├─ Day gate check (day_gate.py)
  ├─ Entry decision (risk_manager.py)
  ├─ Place order (executor.py) → IBKR
  ├─ Poll for fill (executor.py)
  ├─ Send Telegram alert (telegram_notifier.py)
  └─ Update position tracking (position_store.py, trades_db.py)
  ↓
Market Close (4:00 PM ET)
  ↓
Log final positions, close connections, exit
```

### Data Flow (CLOUD mode)

Identical to LOCAL, except `combined_reader` returns `CloudSource` which reads from `trading.gex_snapshots`, `trading.scan_results`, `trading.trading_view_indicators` in Supabase.

### Key Decisions per Tick

1. **Day Gate**: Are market conditions safe? (GEX, RSI, distance thresholds)
2. **VIX Bucket**: Which parameters to use? (VIX 13-16, 16-20, 20-25, 25-30)
3. **Premium Scan**: Are available spreads above minimum credit?
4. **RSI Gate**: Is RSI in safe range for this VIX bucket?
5. **Distance Check**: Is strike far enough from spot / GEX zero-gamma level?
6. **Entry Decision**: Place order or skip?
7. **Exit Decision**: For open positions, should we close?
8. **Risk Limits**: Margin limit, max position count

---

## 🔍 Troubleshooting

### IBKR Connection

**Error: `Connection refused: 127.0.0.1:7497`**
- ✅ Start TWS or IB Gateway
- ✅ Verify port: paper=7497, live=4001
- ✅ TWS: Edit → Global Configuration → API → Settings → "Enable ActiveX and Socket Clients" + correct port
- ✅ Add 127.0.0.1 to Trusted IPs
- ✅ UNCHECK "Read-Only API" (engine needs to place orders)

### ModuleNotFoundError at Startup

**Error: `ModuleNotFoundError: No module named 'yaml'` (or 'dateutil', 'ib_async', 'requests')**
- ✅ The venv is missing required dependencies. Reinstall with the wheels-only flag:
  ```bash
  ./venv/bin/pip install --only-binary=:all: -r requirements.txt
  ```
- ✅ Verify after install: `./venv/bin/python run.py --test`
- ✅ See "Step 3: Install Dependencies" above for why `--only-binary=:all:` is mandatory on Python 3.14+.

**Error: `ModuleNotFoundError: No module named 'src'` or `config`**
- ✅ You're running `python run.py` from outside the project dir. Always `cd ibkr_trader_engine` first, or invoke `./venv/bin/python run.py` from inside.

**Error: Engine crashes silently, log shows nothing**
- ✅ The engine buffers stdout when not connected to a TTY. Always redirect to a log file or run in foreground during debugging:
  ```bash
  ./venv/bin/python run.py 2>&1 | tee logs/debug.log
  ```

### Data Source Missing (LOCAL mode)

**Error: `FileNotFoundError: ../gex_extractor/data/gex.db`**
- ✅ Start that extractor: `../gex_extractor/venv/bin/python3 ../gex_extractor/run.py`
- ✅ Wait 1-2 minutes for first DB write
- ✅ Verify: `ls -lh ../gex_extractor/data/gex.db`
- ✅ Repeat for `premium_extractor` and `tradingView_signal_generator`

### Cloud Mode Errors

**Error: `DataSourceError: CLOUD mode requires SUPABASE_APP_ID`**
- ✅ Check `.env` has `SUPABASE_APP_ID=sb_publishable_...` (or `sb_secret_...`)
- ✅ Source `.env` before running: `bash -c 'set -a && source ./.env && set +a && exec python3 run.py'`

**Error: Supabase returns 401 / 404 / 403**
- ✅ Verify the project URL and key are correct (no typos)
- ✅ If the key is `sb_publishable_...`, the publishable key only works for anon read. For CLOUD mode reads of the engine, that's enough. If extractors are writing, they need `sb_secret_...`.
- ✅ Verify the tables exist: in Supabase SQL editor, run `SELECT COUNT(*) FROM trading.gex_snapshots;`

### Telegram Not Working

**Error: `[TELEGRAM] TELEGRAM_BOT_TOKEN not set`**
- ✅ Check `.env` has `TELEGRAM_BOT_TOKEN=...`
- ✅ **Source `.env` manually** — `run.py` does NOT auto-load it
- ✅ Use: `bash -c 'set -a && source ./.env && set +a && exec python3 run.py'`

**Error: `400 Bad Request: chat_id invalid`**
- ✅ Verify `TELEGRAM_SIGNALS_CHAT_ID` is correct (format: `-100XXXXXXXXXX`)
- ✅ Verify bot is added to the group AND is an Admin

### Stale Data (Engine Says "Skipping entry — no GEX in 10min window")

- ✅ Check the extractors are running: `pgrep -fl gex_extractor.*run.py`
- ✅ Check the extractors are writing: `ls -la ../gex_extractor/data/gex.db`
- ✅ For CLOUD mode: `SELECT MAX(captured_at) FROM trading.gex_snapshots;` should be within last 10 minutes during market hours

### Database Lock

**Error: `sqlite3.OperationalError: database is locked`**
- ✅ Close other instances reading the same DB
- ✅ Engine uses WAL mode (should not conflict with extractors, but check)

### High CPU / Memory

- ✅ Adjust `engine.check_interval_seconds` in config (default 30s)
- ✅ Monitor: `top -p $(pgrep -f "python3 run.py")`

### Trades Not Executing

**In dry-run mode** (expected):
- Check for `[DRY_RUN]` tag in entry/exit messages
- Confirm `dry_run: true` in config

**In live mode** (troubleshoot):
- Check TWS for pending orders
- Check risk manager: `entry_start` / `entry_end` market hours
- Look for `DAY GATE BLOCKED` in logs
- Verify IBKR buying power
- Search logs for `REJECTED`

---

## 🔐 Security & Best Practices

### Environment Variables
- ✅ Store in `.env` (gitignored, permissions 600)
- ✅ Never commit `.env` to git
- ✅ Never share `.env` with others
- ✅ Use unique bot tokens per machine

### Paper vs. Live
- ✅ Start in **paper** (port 7497) for at least 5 trading days
- ✅ Only move to **live** (port 4001) when confident
- ✅ Even in live, start with `contracts_per_trade: 1`
- ✅ Scale position size gradually after profitability confirmed

### Risk Management
- ✅ Day gate enabled (blocks entries in volatile markets)
- ✅ Margin limits enforced (check buying power)
- ✅ Position limits (max concurrent positions)
- ✅ Manual kill switch: set `dry_run: true` to halt real trading

### Monitoring
- ✅ Watch logs during market hours: `tail -f logs/engine.$(date +%Y-%m-%d).log`
- ✅ Monitor Telegram alerts
- ✅ Check TWS for pending/filled orders
- ✅ Review trade log daily

---

## 📖 References

- **IBKR API docs**: https://ibkr.com/api
- **Telegram Bot API**: https://core.telegram.org/bots/api
- **Supabase docs**: https://supabase.com/docs
- **Config reference**: `config/config.yaml` (all parameters)
- **Position/trade schema**: `src/schema.py`
- **Sibling extractors**:
  - `../gex_extractor/`
  - `../premium_extractor/`
  - `../tradingView_signal_generator/`

---

## 📞 Support

Before asking:
1. ✅ Check logs: `tail -f logs/engine.$(date +%Y-%m-%d).log | grep -i error`
2. ✅ Verify data sources running: `pgrep -fl "../gex_extractor\|../premium_extractor\|../tradingView"`
3. ✅ Verify IBKR: TWS running on correct port, API enabled
4. ✅ Verify Telegram: bot in group + admin
5. ✅ Test dry-run first: `dry_run: true` in config

### Common Questions

**Q: Can I run multiple engines?**
A: Not recommended — they'll compete for orders on same strikes. Use separate accounts.

**Q: What's the minimum VIX for trading?**
A: Configured per VIX bucket. Default: 13-16, 16-20, 20-25, 25-30. Adjust in `config/config.yaml`.

**Q: How do I backtest?**
A: `python3 -m src.backtest --date 2026-05-15`. LOCAL mode only. Results in `data/backtest.db`.

**Q: What if IBKR connection drops during market hours?**
A: Engine will auto-reconnect. Open positions remain in database. Watchdog will restart if process dies.

**Q: P&L calculation?**
A: `(exit_price - entry_price) × contracts × 100` for credit spreads.

**Q: Can I run the engine on a different machine than the extractors?**
A: Yes — use CLOUD mode. Extractors write to Supabase, engine reads from Supabase.

**Q: How do I add a new VIX bucket?**
A: Edit `config/config.yaml` `entry.vix_buckets` and add a new entry. Restart the engine.

---

## 📝 Version History

- **v1.2** (2026-06-24): PyYAML/pydantic/pydantic-core pins relaxed to support Python 3.14 wheels; install command requires `--only-binary=:all:`; watchdog script updated to use venv python; added ModuleNotFoundError troubleshooting section.
- **v1.1** (2026-06-23): CLOUD mode (Supabase), backtest improvements, watchdog support, updated README
- **v1.0** (2026-06-20): Initial release with EST timezone awareness, multi-source data readers, Telegram integration

---

**Happy trading! 🚀**
