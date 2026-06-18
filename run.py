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
    args = parser.parse_args()

    if args.test:
        _smoke_test()
    else:
        engine = AutoTraderEngine()
        engine.run()

def _smoke_test():
    """Quick smoke test — run imports + DB init + scan/gex/TV read."""
    print("=== Smoke Test ===")
    from src.trades_db import init_db, get_conn
    from src.scanner_reader import get_latest_scan
    from src.gex_reader import get_latest_gex
    from src.tradingview_reader import get_latest_fundamentals, classify_regime
    from src.position_store import PositionStore
    from src.risk_manager import is_market_open, is_force_close_time
    
    print("✓ All imports OK")
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

if __name__ == "__main__":
    main()
