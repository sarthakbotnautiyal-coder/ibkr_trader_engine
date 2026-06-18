"""
executor.py — IBKR order placement.

DRY_RUN=True by default — logs orders instead of sending them.
Set DRY_RUN=False only after manual confirmation that live trading works.
Log path comes from config/config.yaml.

TASK-2026-179: Pending-state protocol.
  - reqAllOpenOrders() and query_order_status() added for polling sync
  - get_available_cash() added for cash checking
  - DRY_RUN: returns safe defaults (no IBKR API calls)
  - LIVE: delegates to BlockingIBKRClient (background-thread, queue-based)

TASK-2026-185: estimate_required_margin() added for cash-before-entry check.
"""
import logging
from pathlib import Path
from typing import Optional

# Config must load — startup fails loudly if missing
from config import CONFIG
from log_setup import get_engine_logger

# ---------------------------------------------------------------------------
# Safety flag — never accidentally set to False without manual confirmation
# ---------------------------------------------------------------------------

DRY_RUN: bool = False   # Flipped to False for paper trading (2026-05-19)

_LOGS_DIR = Path(__file__).parent.parent / CONFIG["paths"]["logs"]
TASK_ID = "TASK-2026-173"

# ---------------------------------------------------------------------------
# Logger setup
# ---------------------------------------------------------------------------

def _setup_logger(name: str = __name__) -> logging.Logger:
    return get_engine_logger(name, _LOGS_DIR)


# ---------------------------------------------------------------------------
# Order models
# ---------------------------------------------------------------------------

from dataclasses import dataclass


@dataclass
class OrderParams:
    symbol:         str
    side:           str        # 'BUY' or 'SELL' (net combo direction)
    contract_type:  str        # 'CALL' or 'PUT' (option right)
    strike:         float      # short strike of the spread
    long_strike:    float      # long strike of the spread
    expiry:         str        # 'YYYYMMDD'
    quantity:       int        # spread lots
    action:         str        # 'OPEN' or 'CLOSE'
    credit_debit:   Optional[float] = None


@dataclass
class FillResult:
    order_id:   Optional[int]
    filled:     bool
    avg_price:  Optional[float]
    contracts:  int
    status:     str     # 'filled' | 'dry_run' | 'rejected' | 'pending'
    message:    str


# ---------------------------------------------------------------------------
# Margin estimation — TASK-2026-185
# ---------------------------------------------------------------------------

def estimate_required_margin(
    side: str,
    short_strike: float,
    long_strike: float,
    num_contracts: int,
    credit: float,
) -> float:
    """
    Return approximate margin (buying power) required for a credit spread.

    For SPX credit spreads, margin is the worst-case loss per spread:
      margin = spread_width_dollars * $100 * num_contracts

    The net credit received reduces the margin requirement slightly.

    TASK-2026-185: Used by engine._on_enter_approved() to verify sufficient
    cash is available BEFORE sending an entry order to IBKR in LIVE mode.
    """
    spread_width = abs(short_strike - long_strike)
    margin_per_contract = spread_width * 100  # $100 per point
    total_margin = margin_per_contract * num_contracts
    net_credit_apply = credit * 100 * num_contracts
    required = total_margin - net_credit_apply
    return max(required, 0.0)


# ---------------------------------------------------------------------------
# IBKR Client (thread-safe — ib_async runs in a background daemon thread)
# ---------------------------------------------------------------------------

class IBKRClient:
    """
    Interface wrapper for the engine.

    Delegates to blocking_ib_client.BlockingIBKRClient (non-blocking) when
    DRY_RUN=False. The BlockingIBKRClient keeps all ib_async I/O on a
    dedicated daemon thread so the engine tick loop never blocks on IBKR
    connection events or reconnect attempts.

    TASK-2026-179: Adds reqAllOpenOrders() and query_order_status() for
    polling-only state sync (primary mechanism, no callbacks).
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = None,
        client_id: int = None,  # Default None = auto-assign (passed to BlockingIBKRClient)
    ):
        self.host = host
        port = port if port is not None else CONFIG["ibkr"]["port"]
        self.port = port
        self.client_id = client_id
        self._logger = _setup_logger("executor.ibkr")

        if not DRY_RUN:
            from src.blocking_ib_client import BlockingIBKRClient as RealClient
            self._real = RealClient(host, port, client_id)
        else:
            self._real = None

    def connect(self) -> bool:
        if DRY_RUN:
            self._logger.info("DRY_RUN: connect() — skipped")
            return True
        return self._real.connect() if self._real else False

    def disconnect(self):
        if not DRY_RUN and self._real:
            self._real.disconnect()

    def is_connected(self) -> bool:
        if DRY_RUN:
            return True
        return self._real.is_connected() if self._real else False

    def register_fill_callback(self, callback) -> None:
        """
        Register a callback for fill confirmations.
        Signature: callback(order_id: int, params: OrderParams, avg_price: float, filled: float)
        The callback fires when IBKR confirms a fill via orderStatus callback.
        Delegates to BlockingIBKRClient.register_fill_callback().

        TASK-2026-179: Callbacks still fire for logging, but the engine uses
        polling results (reqAllOpenOrders) as the authoritative sync mechanism.
        """
        if DRY_RUN or self._real is None:
            return
        self._real.register_fill_callback(callback)

    def _health_check(self) -> bool:
        """
        Health check: returns False if IBKR thread is stalled or backlogged.
        Delegates to BlockingIBKRClient._health_check().
        """
        if DRY_RUN or self._real is None:
            return True
        return self._real._health_check()

    def place_order(self, params: OrderParams) -> FillResult:
        if DRY_RUN:
            self._order_id = getattr(self, "_order_id", 1000) + 1
            self._order_id += 1
            self._logger.info(
                f"DRY_RUN | {params.action} {params.side} {params.quantity} "
                f"{params.symbol} {params.contract_type} "
                f"{params.strike}/{params.long_strike} exp {params.expiry} — "
                f"credit/debit: {params.credit_debit}"
            )
            return FillResult(
                order_id=self._order_id,
                filled=False,
                avg_price=None,
                contracts=params.quantity,
                status="dry_run",
                message=f"DRY_RUN: would {params.side} {params.quantity} "
                        f"{params.contract_type} {params.strike} for {params.symbol}",
            )
        return self._real.place_order(params)

    def cancel_order(self, order_id: int) -> bool:
        if DRY_RUN:
            self._logger.info(f"DRY_RUN: cancel_order({order_id})")
            return True
        return self._real.cancel_order(order_id) if self._real else False

    def get_buying_power(self) -> float:
        if DRY_RUN:
            return 999999.0
        return self._real.get_buying_power() if self._real else 0.0

    def get_available_cash(self) -> float:
        """Return available cash. DRY_RUN returns 999999.0."""
        if DRY_RUN:
            return 999999.0
        return self._real.get_available_cash() if self._real else 0.0

    def get_margin_limit(self) -> float:
        """Return net liquidation (margin limit). DRY_RUN returns 999999.0."""
        if DRY_RUN:
            return 999999.0
        return self._real.get_margin_limit() if self._real else 0.0


    def reconcile(self, store):
        if DRY_RUN:
            return
        if self._real:
            self._real.reconcile(store)

    # -------------------------------------------------------------------------
    # TASK-2026-179: Polling-only state sync methods
    # -------------------------------------------------------------------------

    def reqAllOpenOrders(self):
        """
        Return all open orders from IBKR as a list of Trade objects.
        Used by engine._poll_pending_orders() every tick as the authoritative
        sync mechanism (polling-only, not callbacks as primary mechanism).

        DRY_RUN: returns [] (no IBKR API calls).
        LIVE: delegates to BlockingIBKRClient.reqAllOpenOrders().
        """
        if DRY_RUN:
            return []
        return self._real.reqAllOpenOrders() if self._real else []

    def query_order_status(self, order_id: int):
        """
        Return the Trade for a specific order_id, or None if not found.
        Used when an order_id is no longer in the open orders list to check
        if it was filled, cancelled, or just not in the current snapshot.

        DRY_RUN: returns None.
        LIVE: delegates to BlockingIBKRClient.query_order_status().
        """
        if DRY_RUN:
            return None
        return self._real.query_order_status(order_id) if self._real else None

    def get_open_positions_ibkr(self):
        """Return current positions from IBKR. DRY_RUN returns []. LIVE delegates."""
        if DRY_RUN:
            return []
        return self._real.get_open_positions_ibkr() if self._real else []


# ---------------------------------------------------------------------------
# Order helpers
# ---------------------------------------------------------------------------

def today_expiry() -> str:
    """Return today's date as YYYYMMDD — SPXW options expire daily (Mon–Fri)."""
    from datetime import date
    return date.today().strftime("%Y%m%d")


def build_order_params(
    ticker: str,
    side: str,
    short_strike: float,
    long_strike: float,
    expiry: str,
    quantity: int,
    action: str,
    credit_debit: Optional[float] = None,
) -> OrderParams:
    """
    Build an OrderParams for a combo spread.

    side:         'PUT' or 'CALL' (the option right — determines CCS vs PCS)
    short_strike: the strike of the leg being sold
    long_strike:  the strike of the long leg being bought
    action:       'OPEN' (sell the spread) or 'CLOSE' (buy back the spread)
    """
    return OrderParams(
        symbol=ticker,
        side=side,
        contract_type=side,
        strike=short_strike,
        long_strike=long_strike,
        expiry=expiry,
        quantity=quantity,
        action=action,
        credit_debit=credit_debit,
    )


# ---------------------------------------------------------------------------
# Entry / Exit execution
# ---------------------------------------------------------------------------

def execute_entry(
    client: IBKRClient,
    ticker: str,
    side: str,
    short_strike: float,
    long_strike: float,
    credit: float,
    num_contracts: int = 1,
) -> FillResult:
    """
    Open a calendar spread position.

    side:         'PUT' or 'CALL' (the option right — CCS vs PCS)
    short_strike: the strike of the short leg being sold
    long_strike:  the strike of the long leg being bought
    credit:       credit received for the spread
    """
    logger = _setup_logger("executor.entry")
    expiry = today_expiry()
    params = build_order_params(
        ticker=ticker,
        side=side,
        short_strike=short_strike,
        long_strike=long_strike,
        expiry=expiry,
        quantity=num_contracts,
        action="OPEN",
        credit_debit=credit,
    )
    result = client.place_order(params)
    logger.info(f"ENTRY result: {result}")
    return result


def execute_exit(
    client: IBKRClient,
    ticker: str,
    side: str,
    short_strike: float,
    long_strike: float,
    quantity: int = 1,
) -> FillResult:
    """
    Close a calendar spread position.

    side:         'PUT' or 'CALL' (same as the entry side)
    short_strike: same as entry short strike
    long_strike:  same as entry long strike
    quantity:     number of spread lots to close
    """
    logger = _setup_logger("executor.exit")
    expiry = today_expiry()
    params = build_order_params(
        ticker=ticker,
        side=side,
        short_strike=short_strike,
        long_strike=long_strike,
        expiry=expiry,
        quantity=quantity,
        action="CLOSE",
        credit_debit=None,
    )
    result = client.place_order(params)
    logger.info(f"EXIT result: {result}")
    return result


if __name__ == "__main__":
    client = IBKRClient()
    client.connect()
    print(f"DRY_RUN={DRY_RUN}")
    r = execute_entry(client, "SPX", "PUT", 7395.0, 7385.0, 0.50)
    print(r)