#!/usr/bin/env python3
"""
run_engine.py — SPX 0DTE Auto-Trader entry point.

Usage:
    python3 run_engine.py          # normal run
    python3 run_engine.py --test   # smoke-test the DB / reader modules

Config:
    DRY_RUN is driven from config/config.yaml (dry_run: true by default).
    To enable live trading: set dry_run: false in config.yaml.
    This is intentional — safe by default, no accidental live orders.
"""
import os
import signal
import sys

from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from src.engine import AutoTraderEngine, DRY_RUN


def _handle_sigterm(signum, frame):
    # Redirect SIGTERM into the existing KeyboardInterrupt path so engine._shutdown()
    # calls ib.disconnect() and releases the IB Gateway client ID cleanly.
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
    from src.tradingview_reader import get_latest_fundamentals, classify_regime, get_dealer_regime
    from src.position_store import PositionStore
    from src.risk_manager import is_market_open, is_force_close_time
    from src.engine import DRY_RUN

    print(f"DRY_RUN={DRY_RUN}")

    # DB
    init_db()
    with get_conn() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        print(f"DB tables: {[r[0] for r in rows]}")

    # Scanner
    scan = get_latest_scan()
    print(f"Scan: SPX={scan.spx_spot}  EM=±{scan.expected_move}  ATM={scan.atm_strike}"
          if scan else "No scan data")

    # GEX
    gex = get_latest_gex()
    print(f"GEX: regime={gex.regime}  ZeroGamma={gex.zero_gamma}  GEX(OI)={gex.gex_by_oi:.2f}"
          if gex else "No GEX data")

    # TradingView pre-computed indicators
    try:
        tv = get_latest_fundamentals(gex_expected_move=scan.expected_move if scan else 0.0)
        print(f"TradingView: price={tv.price:.2f}  rsi={tv.rsi:.2f}  "
              f"adx={tv.adx:.1f}  BB_pos={tv.bb_position:.4f}  exp={tv.bb_expanding}")
        print(f"  MACD hist={tv.macd_hist:.4f}  exp={tv.macd_expanding}  "
              f"adx_rising={tv.adx_rising}")

        # Regime classification
        regime = classify_regime(tv, gex, em=scan.expected_move if scan else 0.0,
                                 spx=scan.spx_spot if scan else 0.0)
        dealer_regime = get_dealer_regime(gex)
        print(f"Regime: {regime}  GEX regime: {dealer_regime}")
    except RuntimeError as e:
        print(f"TradingView: {e}")

    # Risk manager
    em = scan.expected_move if scan else 0.0
    print(f"Market open: {is_market_open()}  Force close: {is_force_close_time()}")

    # Position store
    store = PositionStore()
    store.init()
    store.load_open()
    print(f"Open positions: {store.open_count()}")

    print("=== Smoke Test PASSED ===")


if __name__ == "__main__":
    main()