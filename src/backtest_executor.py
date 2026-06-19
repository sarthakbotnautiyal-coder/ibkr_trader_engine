"""
backtest_executor.py — Backtesting engine that replays historical data for a given date.

Reads historical signals (GEX, scan) for a date and simulates the engine's decisions
without connecting to IBKR. Records results in backtests.db.
"""
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

from config import CONFIG
from data_sources import get_gex_snapshots_table, get_scan_results_table, get_tradingview_fundamentals_table
from backtests_db import (
    record_signal, record_position, close_position, finalize_backtest
)
from log_setup import get_engine_logger

_LOGS_DIR = CONFIG.get("paths", {}).get("logs", "logs")
_LOG = get_engine_logger("backtest_executor", _LOGS_DIR)


class BacktestSignal:
    """Represents a signal from historical data."""

    def __init__(self, side: str, strike: float, premium: float, time: str, **kwargs):
        self.side = side  # 'call' or 'put'
        self.strike = strike
        self.premium = premium
        self.time = time
        self.rsi = kwargs.get("rsi")
        self.vix = kwargs.get("vix")
        self.reason = kwargs.get("reason", "")

    def __repr__(self):
        return f"Signal({self.side} {self.strike} @{self.premium} {self.time})"


class BacktestPosition:
    """Represents a position during backtest."""

    def __init__(self, position_id: str, side: str, strike: float, entry_time: str, entry_price: float):
        self.position_id = position_id
        self.side = side
        self.strike = strike
        self.entry_time = entry_time
        self.entry_price = entry_price
        self.exit_time: Optional[str] = None
        self.exit_price: Optional[float] = None
        self.pnl: Optional[float] = None
        self.closed = False

    def close(self, exit_time: str, exit_price: float) -> None:
        """Close position and calculate P&L."""
        self.exit_time = exit_time
        self.exit_price = exit_price
        self.pnl = exit_price - self.entry_price
        self.closed = True


class BacktestExecutor:
    """
    Replays historical data for a date and simulates trading decisions.
    """

    def __init__(self, backtest_id: int, backtest_date: str):
        """
        Initialize backtest executor.

        Args:
            backtest_id: Database ID for this backtest run
            backtest_date: Date to backtest (YYYY-MM-DD)
        """
        self.backtest_id = backtest_id
        self.backtest_date = backtest_date
        self.signals: List[BacktestSignal] = []
        self.positions: Dict[str, BacktestPosition] = {}
        self.position_counter = 0

    def run(self) -> Dict[str, Any]:
        """
        Run backtest for the date.

        Returns:
            Dictionary with summary statistics
        """
        _LOG.info(f"Starting backtest for {self.backtest_date}")

        try:
            # Load historical data
            self._load_historical_data()

            # Process signals and simulate trading
            self._process_signals()

            # Finalize and get statistics
            stats = finalize_backtest(self.backtest_id)

            _LOG.info(f"Backtest complete: {stats}")
            return stats

        except Exception as e:
            _LOG.error(f"Backtest failed: {e}", exc_info=True)
            raise

    def _load_historical_data(self) -> None:
        """Load all historical data for the backtest date."""
        _LOG.info(f"Loading historical data for {self.backtest_date}")

        # Load scan results (options data)
        scan_results = get_scan_results_table(as_of_date=self.backtest_date, limit=1000)
        _LOG.info(f"Loaded {len(scan_results)} scan records")

        # Load GEX snapshots
        gex_snapshots = get_gex_snapshots_table(as_of_date=self.backtest_date, limit=1000)
        _LOG.info(f"Loaded {len(gex_snapshots)} GEX records")

        # Load TradingView fundamentals
        tv_data = get_tradingview_fundamentals_table(as_of_date=self.backtest_date, limit=1000)
        _LOG.info(f"Loaded {len(tv_data)} TradingView records")

    def _process_signals(self) -> None:
        """
        Process historical signals and simulate trading.

        This is a simplified simulation:
        - For each scan result (option price), check if it meets entry criteria
        - If yes, record a signal and open a position
        - Use next scan record as exit point
        """
        _LOG.info("Processing signals and simulating trades")

        scan_results = get_scan_results_table(as_of_date=self.backtest_date, limit=1000)

        if not scan_results:
            _LOG.warning("No scan results found for backtest date")
            return

        # Simple strategy: Process each scan result as a potential entry point
        for i, scan in enumerate(scan_results):
            try:
                spx_spot = scan.get("spx_spot")
                timestamp = scan.get("timestamp_est")

                if not spx_spot or not timestamp:
                    continue

                # Extract call and put data
                call_strike = scan.get("call_strike_003")
                call_mid = scan.get("call_mid")
                put_strike = scan.get("put_strike_003")
                put_mid = scan.get("put_mid")

                # Record signals
                if call_strike and call_mid:
                    signal_id = record_signal(
                        self.backtest_id,
                        signal_time=timestamp,
                        side="call",
                        strike=call_strike,
                        premium=call_mid,
                        reason="0.03-delta call"
                    )
                    _LOG.debug(f"Signal #{signal_id}: Call {call_strike} @{call_mid}")

                    # Open position if we haven't reached position limit (simplified: just open one)
                    if len(self.positions) < 2:
                        position_id = f"backtest_{self.backtest_id}_call_{self.position_counter}"
                        self.position_counter += 1

                        record_position(
                            self.backtest_id,
                            position_id=position_id,
                            side="call",
                            strike=call_strike,
                            entry_time=timestamp,
                            entry_price=call_mid
                        )

                        self.positions[position_id] = BacktestPosition(
                            position_id=position_id,
                            side="call",
                            strike=call_strike,
                            entry_time=timestamp,
                            entry_price=call_mid
                        )

                        _LOG.debug(f"Opened position: {position_id}")

                if put_strike and put_mid:
                    signal_id = record_signal(
                        self.backtest_id,
                        signal_time=timestamp,
                        side="put",
                        strike=put_strike,
                        premium=put_mid,
                        reason="0.03-delta put"
                    )
                    _LOG.debug(f"Signal #{signal_id}: Put {put_strike} @{put_mid}")

                    # Open position if we haven't reached position limit
                    if len(self.positions) < 2:
                        position_id = f"backtest_{self.backtest_id}_put_{self.position_counter}"
                        self.position_counter += 1

                        record_position(
                            self.backtest_id,
                            position_id=position_id,
                            side="put",
                            strike=put_strike,
                            entry_time=timestamp,
                            entry_price=put_mid
                        )

                        self.positions[position_id] = BacktestPosition(
                            position_id=position_id,
                            side="put",
                            strike=put_strike,
                            entry_time=timestamp,
                            entry_price=put_mid
                        )

                        _LOG.debug(f"Opened position: {position_id}")

                # Close positions every 5 scans (simplified exit logic)
                if (i + 1) % 5 == 0:
                    self._close_positions(timestamp, scan)

            except Exception as e:
                _LOG.warning(f"Error processing scan at {i}: {e}")
                continue

        # Close any remaining open positions
        if scan_results:
            last_scan = scan_results[0]
            self._close_positions(last_scan.get("timestamp_est"), last_scan)

        _LOG.info(f"Processing complete: {len(self.positions)} positions traded")

    def _close_positions(self, exit_time: str, scan: Dict[str, Any]) -> None:
        """
        Close open positions at current market price.

        Simplified: Use ATM strike price as exit price.
        """
        atm_call_mid = scan.get("atm_call_mid")
        atm_put_mid = scan.get("atm_put_mid")

        for position_id, position in list(self.positions.items()):
            if position.closed:
                continue

            # Simplified exit: Use ATM price as exit reference
            exit_price = atm_call_mid if position.side == "call" else atm_put_mid

            if exit_price:
                position.close(exit_time, exit_price)

                close_position(
                    self.backtest_id,
                    position_id=position_id,
                    exit_time=exit_time,
                    exit_price=exit_price,
                    pnl=position.pnl
                )

                _LOG.debug(f"Closed position {position_id}: P&L={position.pnl:.2f}")
