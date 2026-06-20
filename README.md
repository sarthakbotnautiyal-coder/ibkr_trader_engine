# IBKR Trader Engine — SPX 0DTE Auto-Trading System

A sophisticated Python-based auto-trading engine for SPX 0DTE (zero days to expiration) options strategies with real-time risk management, technical analysis integration, and Telegram alerts.

## 🎯 Features

- ✅ **Real-time Entry/Exit Logic** — VIX-adaptive parameters, RSI gates, premium thresholds
- ✅ **Risk Management** — Day gate volatility protection, margin limits, position tracking
- ✅ **Multi-Source Data** — GEX snapshots, option premiums, technical indicators
- ✅ **Live Trading** — IBKR order execution with fill confirmation and polling
- ✅ **Dry-Run Mode** — Test strategies without placing real orders
- ✅ **Telegram Alerts** — Real-time entry/exit/rejection notifications
- ✅ **Comprehensive Logging** — Daily rotating logs by Eastern Time
- ✅ **Backtesting** — Historical simulation with clock override
- ✅ **Cloud Sync** — Optional Supabase dual-write for analytics

---

## 📋 Prerequisites

### System Requirements
- **Python**: 3.9 or higher
- **OS**: macOS, Linux (tested on macOS 14.x)
- **Network**: Stable internet connection for IBKR, Discord, Telegram

### Required Services
1. **Interactive Brokers (IBKR)**
   - TWS (Trader Workstation) or IB Gateway running
   - Paper trading account (recommended for testing) or live account
   - Default: `localhost:7497` (paper trading)

2. **GEX Extractor** (sibling project)
   - Polling Discord for gamma exposure data
   - Writing to `../gex_extractor/data/gex.db`
   - Must be running for engine to function

3. **Premium Extractor** (sibling project)
   - Scanning IBKR for option premiums
   - Writing to `../premium_extractor/data/scanner.db`
   - Must be running for engine to function

4. **TradingView Signal Generator** (sibling project)
   - Computing technical indicators (RSI, Bollinger Bands, MACD, ADX)
   - Writing to `../tradingView_signal_generator/data/tradingview.db`
   - Must be running for full feature set

5. **Telegram** (for notifications)
   - Your own Telegram bot (created via `@BotFather`)
   - A Telegram group to receive alerts

### Optional
- **Supabase** — For cloud data archival and analytics

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
pip install -r requirements.txt
```

### Step 4: Verify Data Sources

Ensure the required data source projects are accessible:

```bash
ls ../gex_extractor/data/gex.db              # Should exist
ls ../premium_extractor/data/scanner.db      # Should exist
ls ../tradingView_signal_generator/data/tradingview.db  # Should exist
```

If any are missing, start those extractors first (they create the DBs on first run).

---

## 🔧 Configuration

### Step 1: Environment Variables (`.env`)

Copy the example and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env`:

```bash
# ========================
# TELEGRAM NOTIFICATIONS
# ========================

# Your personal Telegram bot token (see "Telegram Bot Setup" below)
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11

# Shared group chat ID (all friends post to the same group)
# Format: -100 prefix for group, e.g., -1001234567890
TELEGRAM_SIGNALS_CHAT_ID=-1001234567890

# ========================
# SUPABASE (Optional)
# ========================

# Uncomment if using cloud sync for dual-write to Supabase
# SUPABASE_URL=https://your-project.supabase.co
# SUPABASE_SECRET_KEY=your-secret-key
```

### Step 2: Telegram Bot Setup

**This step is required for notifications. Each friend creates their own bot.**

#### Create Your Bot (via @BotFather in Telegram)

1. Open Telegram and search for `@BotFather`
2. Send `/newbot`
3. Follow prompts:
   - **Bot name**: E.g., "My SPX Trading Bot"
   - **Username**: E.g., "my_spx_trading_bot" (must be unique, end with `_bot`)
4. Copy the token: `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`
5. Paste it into `.env` as `TELEGRAM_BOT_TOKEN`

#### Add Bot to Shared Group

1. In Telegram, create or open the shared group (e.g., "SPX Trading Signals")
2. Go to group info → **Add Members** → search and add your bot
3. Go to group info → **Permissions** → make the bot an **Admin**
   - This allows the bot to post messages to the group
4. Get the group's chat ID:
   - Send a test message in the group
   - Open browser console and run:
     ```javascript
     // In Telegram's web app, open DevTools and run:
     // The group ID is shown in the chat info
     ```
   - Or use this Python script to extract it:
     ```bash
     python3 -c "
     import requests
     token = '123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11'
     url = f'https://api.telegram.org/bot{token}/getUpdates'
     r = requests.get(url)
     print(r.json())  # Look for chat_id in the response
     "
     ```
   - Or retrieve from an existing message:
     ```bash
     curl -s "https://api.telegram.org/bot123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11/getUpdates" | python3 -m json.tool
     ```

5. Paste the group's chat ID into `.env` as `TELEGRAM_SIGNALS_CHAT_ID`

#### Testing Telegram Connection

```bash
python3 -c "
from src.telegram_notifier import send_telegram_message
result = send_telegram_message('🧪 Test message from IBKR Engine')
print(f'Message sent: {result}')
"
```

Expected output: `Message sent: True` and a message appears in your Telegram group.

### Step 3: Trading Configuration (`config/config.yaml`)

The engine is pre-configured for SPX 0DTE trading. Key parameters:

```yaml
# ========================
# RUNNING MODE
# ========================
dry_run: true          # Set to false for LIVE TRADING with real orders

# ========================
# IBKR GATEWAY CONNECTION
# ========================
ibkr:
  host: "127.0.0.1"
  port: 7497           # 7497 = paper trading, 4001 = live trading
  scanner_client_id: 15
  spot_client_id: 16
  engine_client_id: 31
  account_id: "U13498586"  # Replace with your account ID
  use_margin_limit: true

# ========================
# MARKET HOURS (ET)
# ========================
market:
  entry_start: "09:00"   # Stop taking new entries at 4 PM
  entry_end: "16:00"

# ========================
# ENTRY PARAMETERS
# ========================
entry:
  spread_width_primary: 10    # 10-point spreads (4500/4510, 4510/4520)
  spread_width_fallback: 20   # Fallback to 20-point if 10 unavailable
  short_delta_target: 0.03    # Target 0.03 delta (~3% OTM)
  contracts_per_trade: 1      # Number of contracts per trade
  
  vix_buckets:
    13-16:
      min_premium: 0.20       # Minimum credit required
      rsi_upper_threshold: 50.0   # Don't enter if RSI above 50
      rsi_lower_threshold: 49.0
      distance:
        from_spot: 3          # How far OTM from SPX spot
        from_gex_level: 1     # Distance from GEX zero gamma level
    16-20:
      min_premium: 0.25
      rsi_upper_threshold: 55.0
      rsi_lower_threshold: 45.0
      distance:
        from_spot: 3
        from_gex_level: 1.5
    # ... more VIX buckets ...

# ========================
# DAY GATE (Volatility Protection)
# ========================
day_gate:
  enabled: true
  window_minutes: 15        # Rolling 15-min average
  gex_by_oi_threshold: 0.0  # Block if GEX/OI below 0
  spot_zero_gamma_threshold: -15.0  # Block if ZG below -15
  rsi_extreme_low: 15.0     # Block if RSI below 15
  rsi_extreme_high: 85.0    # Block if RSI above 85

# ========================
# ENGINE LOOP
# ========================
engine:
  check_interval_seconds: 30  # Tick every 30 seconds

# ========================
# TELEGRAM NOTIFICATIONS
# ========================
telegram:
  dry_run: false            # Set to true to log only (don't send Telegram)

# ========================
# DATA SOURCES
# ========================
data_sources:
  gex_db: "../gex_extractor/data/gex.db"
  scanner_db: "../premium_extractor/data/scanner.db"
  tradingview_db: "../tradingView_signal_generator/data/tradingview.db"
```

**Important**: Before setting `dry_run: false`, thoroughly test in dry-run mode first.

---

## ▶️ Running the Engine

### Dry-Run Mode (Recommended for First Test)

```bash
# Ensure .env is populated and config has dry_run: true
python3 run.py

# Example output:
# 2026-06-20 09:32:15 - engine - INFO - Engine starting (DRY_RUN mode)
# 2026-06-20 09:32:16 - engine - INFO - Data sources connected
# 2026-06-20 09:32:17 - engine - INFO - Listening for ticks... (tick interval: 30 sec)
```

In dry-run mode:
- ✅ Positions are tracked in `data/positions.db`
- ✅ Entry/exit decisions logged to console and logs
- ✅ Telegram messages logged (not sent) if `telegram.dry_run: true`
- ✅ **NO REAL ORDERS** placed with IBKR

### Live Mode (Real Trading)

⚠️ **Before going live:**
1. ✅ Tested dry-run mode for at least 1 full trading day
2. ✅ Verified all data sources are running and populated
3. ✅ Verified Telegram notifications work
4. ✅ Confirmed IBKR account is funded and connected
5. ✅ Reviewed all trading parameters in `config/config.yaml`

```bash
# Set in config/config.yaml:
# dry_run: false

python3 run.py

# Example output:
# 2026-06-20 09:30:00 - engine - INFO - Engine starting (LIVE mode)
# 2026-06-20 09:32:15 - engine - INFO - [LIVE] 🚀 ENTRY | CALL | 4500/4510 | $2.80 credit
# 2026-06-20 14:45:32 - engine - INFO - [LIVE] ✅ EXIT | CALL | 4500/4510 | P&L: +$280 | L1 crossed
```

Live mode trades real positions with real money. **Start small** and monitor carefully.

---

## 📊 Monitoring & Logs

### Real-Time Console Output

```bash
tail -f logs/engine*.log

# Filter for entries/exits only
tail -f logs/engine*.log | grep -E "ENTRY|EXIT|DAY_GATE"

# Filter for errors
tail -f logs/engine*.log | grep -i error
```

### Log File Locations

Logs are written daily by Eastern Time:

```bash
logs/
├── engine.2026-06-20.log       # Today's engine log
├── engine.2026-06-19.log       # Yesterday's engine log
└── ...
```

### Key Log Patterns

**Entry Signal:**
```
[LIVE] 🚀 ENTRY | CALL | 4500/4510 | $2.80 credit | 1 contract | SPX=4500 | EM=15.0 | fill=$2.80
```

**Exit Signal:**
```
[LIVE] ✅ EXIT | CALL | 4500/4510 | 1 contract | P&L: +$280 | L1 crossed | SPX=4510 | L1
```

**Day Gate Alert:**
```
🚨 DAY GATE BLOCKED | 2026-06-20 13:45:00 ET
New entries suspended — danger signals fired:
  GEX-OI=-2.5 ❌  |  Dist=0.8 ❌  |  RSI=88.0 ❌  (n=3 ticks)
```

**Dry-Run Telegram Log:**
```
[TELEGRAM DRY] [LIVE] 🚀 ENTRY | CALL | 4500/4510 | $2.80 credit | 1 contract
```

### Position Tracking

View current positions in the SQLite database:

```bash
sqlite3 data/positions.db "SELECT * FROM positions WHERE status='open';"
sqlite3 data/positions.db "SELECT * FROM trades ORDER BY entry_ts DESC LIMIT 10;"
```

---

## 🤝 Sharing with Friends

All friends can run the same engine code and trade into a **shared Telegram group**.

### Setup for Each Friend

1. **Get your own Telegram bot** (create via `@BotFather`)
   - Each friend creates their own unique bot
   - Get your `TELEGRAM_BOT_TOKEN`

2. **Add your bot to the shared group**
   - E.g., group "SPX Trading Signals"
   - Make your bot an admin

3. **Use the shared group chat ID**
   - All friends use the **same** `TELEGRAM_SIGNALS_CHAT_ID` (the group's ID)
   - This is the only value you share

4. **Copy `.env.example` → `.env`** and fill in:
   ```bash
   TELEGRAM_BOT_TOKEN=<your_personal_bot_token>
   TELEGRAM_SIGNALS_CHAT_ID=<shared_group_chat_id>
   ```

### Shared Telegram Group Example

When all friends are running:

```
SPX Trading Signals

[LIVE] [Your Bot] 🚀 ENTRY | CALL | 4500/4510 | $2.80 credit
[LIVE] [Friend1 Bot] ✅ EXIT | CALL | 4505/4515 | P&L: +$250
[LIVE] [Friend2 Bot] 🚀 ENTRY | PUT | 4490/4480 | $1.50 credit
🚨 [Your Bot] DAY GATE BLOCKED | GEX signals fired
```

Each bot is visually distinct (different names, avatars), so you can see who's trading.

### Distributing the Project

Create a package for your friends:

```bash
# Create distribution folder
mkdir -p ~/ibkr_trader_engine_distribution

# Copy project files (excluding sensitive data)
cp -r . ~/ibkr_trader_engine_distribution/
cd ~/ibkr_trader_engine_distribution

# Remove sensitive files
rm .env                  # Don't share your credentials
rm -rf .git/config      # Don't share git config
rm -rf data/*.db        # Don't share databases
rm -rf logs/*.log       # Don't share logs

# Keep example files for friends to fill in
ls -la .env.example     # ✅ Friends will copy this
```

**Instructions to send friends:**

```markdown
## Setup Instructions

1. Clone or extract the project
2. Create virtual environment: `python3 -m venv venv && source venv/bin/activate`
3. Install deps: `pip install -r requirements.txt`
4. Create your Telegram bot (see README.md "Telegram Bot Setup")
5. Copy `.env.example` → `.env` and fill in your bot token + shared group chat ID
6. Update `config/config.yaml` with your IBKR account ID
7. Run in dry-run mode first: `python3 run.py` (with dry_run: true in config)
8. Once comfortable, set dry_run: false and run again
9. Watch logs: `tail -f logs/engine*.log`

Questions? See "Troubleshooting" section in README.md
```

---

## 🔍 Troubleshooting

### IBKR Connection Issues

**Error**: `Connection refused: 127.0.0.1:7497`

- ✅ Start TWS (Trader Workstation) or IB Gateway
- ✅ Check port: Paper trading = 7497, Live = 4001
- ✅ Verify in config: `ibkr.port: 7497`
- ✅ Check IBKR settings: Enable API connections

### Data Source Missing

**Error**: `FileNotFoundError: ../gex_extractor/data/gex.db`

- ✅ Start `gex_extractor/run.py` in a separate terminal
- ✅ Wait 1-2 minutes for it to populate data
- ✅ Verify: `ls -lh ../gex_extractor/data/gex.db`
- ✅ Repeat for `premium_extractor` and `tradingView_signal_generator`

### Telegram Notifications Not Working

**Error**: `[TELEGRAM] TELEGRAM_BOT_TOKEN not set`

- ✅ Check `.env` has `TELEGRAM_BOT_TOKEN=...` (not empty)
- ✅ Source `.env` before running: It's auto-loaded by `run.py`
- ✅ Test connection: `python3 -c "from src.telegram_notifier import send_telegram_message; send_telegram_message('test')"`
- ✅ Check bot is admin in group: Go to group info → Members → Bot permissions

**Error**: `[TELEGRAM] API error: {'ok': False, 'error_code': 400, 'description': 'Bad Request: chat_id invalid'}`

- ✅ Verify `TELEGRAM_SIGNALS_CHAT_ID` is correct (format: `-1001234567890`)
- ✅ Verify bot was added to group and is admin
- ✅ Try manually sending message via bot: `curl -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<ID>&text=test"`

### Database Lock Issues

**Error**: `sqlite3.OperationalError: database is locked`

- ✅ Close other instances reading the same DB
- ✅ Ensure WAL mode is enabled (it should be by default)
- ✅ Restart the engine

### High CPU/Memory Usage

- ✅ Check tick interval in config: `engine.check_interval_seconds: 30`
- ✅ Reduce if needed (higher = more responsive, more CPU)
- ✅ Monitor: `top -p $(pgrep -f "python3 run.py")`

### Trades Not Executing

**In dry-run mode** (expected):
- ✅ Check logs for `[DRY_RUN]` tag on entry/exit messages
- ✅ Verify `dry_run: true` in config

**In live mode** (troubleshoot):
- ✅ Verify order placement: Check TWS for pending orders
- ✅ Check risk manager gates: `entry_start` and `entry_end` market hours
- ✅ Check day gate status: Look for `DAY GATE BLOCKED` in logs
- ✅ Verify IBKR account margin: Account must have available buying power
- ✅ Check for order rejections: Search logs for `REJECTED`

---

## 📚 Architecture Overview

### Core Components

```
src/
├── engine.py                 # Main event loop, decision orchestration
├── risk_manager.py           # Entry/exit logic, VIX-adaptive parameters
├── day_gate.py               # Volatility protection (GEX, RSI, distance)
├── position_store.py         # Open position tracking
├── executor.py               # Order execution, fill confirmation
├── trades_db.py              # Trade history database
├── blocking_ib_client.py     # IBKR Gateway communication (blocking API)
├── telegram_notifier.py      # Telegram message sending
│
├── gex_reader.py             # Read GEX snapshots from gex.db
├── scanner_reader.py         # Read option premiums from scanner.db
├── tradingview_reader.py     # Read technical indicators from tradingview.db
├── combined_reader.py        # Orchestrate multi-source data reads
│
├── log_setup.py              # Logging configuration (daily ET rotation)
├── config.py                 # Config loading from config.yaml
└── schema.py                 # Position/trade data structures
```

### Data Flow

```
Market Open (9:30 AM ET)
    ↓
Engine starts: engine.py __main__
    ↓
Load config (config.yaml) + environment (.env)
    ↓
Connect to IBKR Gateway (blocking_ib_client.py)
    ↓
Every 30 seconds (tick loop):
    ├─ Read GEX snapshot (gex_reader.py ← gex.db)
    ├─ Read scan results (scanner_reader.py ← scanner.db)
    ├─ Read TV indicators (tradingview_reader.py ← tradingview.db)
    ├─ Combine data (combined_reader.py)
    ├─ Check day gate (day_gate.py)
    ├─ Make entry decision (risk_manager.py)
    ├─ Place order (executor.py) → IBKR
    ├─ Poll for fill (executor.py)
    ├─ Send Telegram alert (telegram_notifier.py)
    └─ Update position tracking (position_store.py, trades_db.py)
    ↓
Market Close (4:00 PM ET)
    ↓
Log final positions, close connections, exit
```

### Key Decisions per Tick

1. **Day Gate Check**: Are market conditions safe to enter?
2. **VIX Bucket**: Which parameters to use (VIX 13-16, 16-20, etc.)?
3. **Premium Scan**: Are available spreads above minimum credit?
4. **RSI Gate**: Is RSI in safe range?
5. **Distance Check**: Is strike far enough from spot/GEX level?
6. **Entry Decision**: Place order or skip?
7. **Exit Decision**: For open positions, should we close?
8. **Risk Limits**: Are we below margin limit and max position count?

---

## 🔐 Security & Best Practices

### Environment Variables

- ✅ Store in `.env` (gitignored)
- ✅ Never commit `.env` to git
- ✅ Never share `.env` with others
- ✅ Use separate bot tokens per person (one token per friend)
- ✅ Rotate tokens periodically if compromised

### Paper vs. Live

- ✅ Start in **paper trading** (port 7497, `ibkr.port`)
- ✅ Test for at least 5 trading days
- ✅ Only move to **live trading** (port 4001) when confident
- ✅ Even in live, start with `contracts_per_trade: 1`
- ✅ Scale up position size gradually after profitability confirmed

### Risk Management

- ✅ Day gate enabled (blocks entries in volatile markets)
- ✅ Margin limits enforced (check account buying power)
- ✅ Position limits (e.g., max 3 concurrent positions)
- ✅ Daily P&L stop-loss (optional: stop trading if drawdown threshold hit)
- ✅ Manual kill switch: Set `dry_run: true` to halt real trading

### Monitoring

- ✅ Watch logs during market hours
- ✅ Monitor Telegram alerts in real-time
- ✅ Check TWS for pending/filled orders
- ✅ Verify positions at market close
- ✅ Review trade log daily: `sqlite3 data/positions.db "SELECT * FROM trades WHERE DATE(entry_ts)='2026-06-20';"`

---

## 📖 Additional Resources

- **IBKR API**: https://ibkr.com/api
- **Telegram Bot API**: https://core.telegram.org/bots/api
- **Config Reference**: See `config/config.yaml` for all parameters
- **Data Schema**: See `src/schema.py` for position/trade structures
- **Logging**: Logs rotate daily by Eastern Time in `logs/`

---

## 📞 Support

### Before Asking for Help

1. ✅ Check logs: `tail -f logs/engine*.log | grep -i error`
2. ✅ Verify all data sources running: `ls -lh ../*/data/*.db`
3. ✅ Verify IBKR connection: TWS/IB Gateway running on correct port
4. ✅ Verify Telegram setup: Bot in group + admin permissions
5. ✅ Test dry-run mode first: `dry_run: true` in config

### Common Questions

**Q: Can I run multiple instances of the engine?**
- A: Not recommended — they'll compete for orders on same strikes. Use separate accounts/groups if needed.

**Q: What's the minimum VIX for trading?**
- A: Configured per VIX bucket. Default: 13-16, 16-20, 20-25, 25-30. Adjust in `config/config.yaml`.

**Q: How do I backtest?**
- A: Use: `python3 run.py --backtest 2026-06-15`. Requires historical DB data.

**Q: What if IBKR connection drops during market hours?**
- A: Engine will auto-reconnect (logging attempts). Open positions remain in database.

**Q: How do I calculate P&L?**
- A: Database stores entry price, exit price, contracts. P&L = (exit - entry) × contracts × 100.

---

## 📝 Version History

- **v1.0** (2026-06-20): Initial release with EST timezone awareness, multi-source data readers, and Telegram integration
- **v0.9** (2026-06-15): Beta release with core trading logic and IBKR integration

---

**Happy trading! 🚀**
