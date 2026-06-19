#!/usr/bin/env python3
"""IBKR Trader Engine - SPX 0DTE auto-trader."""

import signal
import sys
from pathlib import Path

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
    """Run backtest for a specific date (LOCAL mode only)."""
    print(f"\n=== Backtesting {backtest_date} ===\n")

    from src.data_sources import verify_data_source, check_backtesting_compatibility
    from src.backtests_db import init_backtests_db, create_backtest, finalize_backtest

    try:
        verify_data_source()
        check_backtesting_compatibility()
    except Exception as e:
        print(f"❌ Configuration error: {e}")
        sys.exit(1)

    init_backtests_db()
    backtest_id = create_backtest(backtest_date)

    try:
        # Import backtest executor
        from src.backtest_executor import BacktestExecutor

        executor = BacktestExecutor(backtest_id=backtest_id, backtest_date=backtest_date)
        stats = executor.run()

        print(f"\n=== Backtest #{backtest_id} Results ===")
        print(f"Date: {backtest_date}")
        print(f"Total Signals: {stats.get('total_signals', 0)}")
        print(f"Total Trades: {stats.get('total_trades', 0)}")
        print(f"Winning Trades: {stats.get('winning_trades', 0)}")
        print(f"Losing Trades: {stats.get('losing_trades', 0)}")
        print(f"Win Rate: {stats.get('win_rate', 0):.1f}%")
        print(f"Total P&L: {stats.get('total_pnl', 0):.2f}")
        print(f"Avg P&L/Trade: {stats.get('avg_pnl_per_trade', 0):.2f}")
        print()

    except Exception as e:
        print(f"❌ Backtest error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
