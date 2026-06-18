# IBKR Trader Engine

SPX 0DTE auto-trading engine with risk management and order execution.

## Features

- Real-time trade entry/exit logic based on technical indicators
- VIX-adaptive entry parameters and RSI gates
- Day gate volatility protection
- Position tracking and risk management
- Telegram notifications for entry/exit signals
- Dry-run mode for backtesting and validation

## Architecture

- `src/engine.py`: Main trading engine
- `src/executor.py`: Order placement and fill tracking
- `src/risk_manager.py`: Entry/exit decision logic
- `src/day_gate.py`: Volatility protection
- `src/position_store.py`: Position state management
- `src/trades_db.py`: Trade history database
- `src/gex_reader.py`: Read GEX data from gex_extractor
- `src/scanner_reader.py`: Read scan data from premium_extractor
- `src/tradingview_reader.py`: Read technical indicators
- `src/combined_reader.py`: Orchestrate multi-source data

## Configuration

See `config/config.yaml` for all trading parameters.

## Data Sources

The engine reads data from 3 external sources:
1. `../gex_extractor/data/gex.db` - Gamma exposure snapshots
2. `../premium_extractor/data/scanner.db` - Option premiums and spreads
3. `../tradingView_signal_generator/data/tradingview.db` - Technical indicators

## Running

Requires TWS/IB Gateway on localhost:7497 (paper) or 4001 (live).

```bash
python run.py
```

Set `dry_run: true` in config.yaml to test without placing real orders.
