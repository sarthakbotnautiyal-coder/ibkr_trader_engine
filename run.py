#!/usr/bin/env python3
"""IBKR Trader Engine - SPX 0DTE auto-trader."""

import os
import signal
import sys
from pathlib import Path

# Load .env BEFORE importing engine, so os.environ is populated for telegram_notifier
def _load_env_file(env_path: Path) -> None:
    """Load key=value pairs from env_path into os.environ."""
    if not env_path.exists():
        return
    try:
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    v = v.strip().strip("'\"")
                    os.environ[k.strip()] = v
    except Exception as e:
        print(f"Warning: Failed to load {env_path}: {e}", file=sys.stderr)

_load_env_file(Path(__file__).parent / ".env")
_load_env_file(Path.home() / ".openclaw" / ".env")

sys.path.insert(0, str(Path(__file__).parent / "src"))

from src.engine import AutoTraderEngine, DRY_RUN

def _handle_sigterm(signum, frame):
    raise KeyboardInterrupt

signal.signal(signal.SIGTERM, _handle_sigterm)

def main():
    import argparse
    parser = argparse.ArgumentParser(description="SPX 0DTE Auto-Trader Engine")
    parser.add_argument("--test", action="store_true", help="Run smoke test then exit")
    parser.add_argument("--backtest", metavar="YYYY-MM-DD", help="Run backtest for specific date (LOCAL mode only)")
    args = parser.parse_args()

    if args.test:
        _smoke_test()
    elif args.backtest:
        _run_backtest(args.backtest)
    else:
        engine = AutoTraderEngine()
        engine.run()

def _smoke_test():
    """Quick smoke test — run imports + DB init + scan/gex/TV read."""
    print("=== Smoke Test ===")
    from src.data_sources import verify_data_source
    from src.trades_db import init_db, get_conn
    from src.scanner_reader import get_latest_scan
    from src.gex_reader import get_latest_gex
    from src.tradingview_reader import get_latest_fundamentals, classify_regime
    from src.position_store import PositionStore
    from src.risk_manager import is_market_open, is_force_close_time

    print("✓ All imports OK")

    try:
        verify_data_source()
        print("✓ Data source verified")
    except Exception as e:
        print(f"❌ Data source: {e}")
        sys.exit(1)

    init_db()
    print("✓ DB init OK")

    try:
        scan = get_latest_scan()
        print(f"✓ Latest scan OK: {scan is not None}")
    except Exception as e:
        print(f"⚠ Scan read: {e}")

    try:
        gex = get_latest_gex()
        print(f"✓ Latest GEX OK: {gex is not None}")
    except Exception as e:
        print(f"⚠ GEX read: {e}")

    print("=== Smoke Test PASSED ===")


def _run_backtest(backtest_date: str):
    """Run a backtest for a specific date via the REAL engine (LOCAL mode only).

    The engine runs in backtest mode: same tick()/decision logic as live, fills
    at scan mid (DRY_RUN path), writing to an isolated per-run positions DB.
    """
    print(f"\n=== Backtesting {backtest_date} ===\n")

    from src.data_sources import verify_data_source, check_backtesting_compatibility
    from src.backtests_db import init_registry, create_run, finalize_run

    try:
        verify_data_source()
        check_backtesting_compatibility()
    except Exception as e:
        print(f"❌ Configuration error: {e}")
        sys.exit(1)

    init_registry()
    run_id, db_path = create_run(backtest_date)
    print(f"Backtest run #{run_id} → {db_path}\n")

    try:
        from src.engine import AutoTraderEngine

        engine = AutoTraderEngine(
            mode="backtest",
            backtest_date=backtest_date,
            store_db_path=str(db_path),
        )
        ticks = engine.run_backtest(backtest_date)
        stats = finalize_run(run_id, db_path, ticks)

        print(f"\n=== Backtest #{run_id} Results ({backtest_date}) ===")
        print(f"Ticks replayed:    {stats.get('ticks', 0)}")
        print(f"Signals recorded:  {stats.get('total_signals', 0)}")
        print(f"Positions opened:  {stats.get('total_positions', 0)}")
        print(f"  - still open:    {stats.get('open_positions', 0)}")
        print(f"  - closed:        {stats.get('closed_positions', 0)}")
        print(f"Winning / Losing:  {stats.get('winning', 0)} / {stats.get('losing', 0)}")
        print(f"Total P&L:         {stats.get('total_pnl', 0):.2f}")
        print(f"\nIsolated DB: {db_path}")
        print()

    except Exception as e:
        print(f"❌ Backtest error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
