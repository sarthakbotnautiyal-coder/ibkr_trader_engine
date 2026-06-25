"""
blocking_ib_client.py — Thread-safe IBKR client using ib_async.

Architecture:
  - A single daemon thread (_ib_thread) owns the ib_async.IB instance and its
    asyncio event loop. The loop is pumped via ib.loopUntil() in that thread.
  - All IB operations (place_order, reqContractDetails, etc.) run on the IB thread.
  - The main thread (engine tick loop) never touches ib_async directly.
  - Communication is via a threading.Queue of (fn, args, kwargs, result_holder).
  - On disconnect, the IB thread auto-reconnects and resumes processing requests.
    The main thread continues ticking without interruption.

TASK-2026-191: Initial connect failure now triggers retry loop in the background
  thread instead of exiting. The thread keeps retrying until connection is
  established or explicitly shut down. Reconnect uses a FRESH IB instance each
  time to avoid "event loop already running" state corruption from failed attempts.

TASK-2026-179: Polling-only state sync (no callbacks as primary mechanism).
  - reqAllOpenOrders(): returns list of all open Trade objects
  - query_order_status(): returns Trade for specific order_id or None
  - Callbacks still fire for logging, but engine uses polling results
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

from ib_async import IB, Contract, ComboLeg, LimitOrder, MarketOrder, Option

from config import CONFIG
from src.executor import DRY_RUN, FillResult, OrderParams
from src.log_setup import get_engine_logger

_LOGS_DIR = Path(__file__).parent.parent / CONFIG["paths"]["logs"]

T = TypeVar("T")


def _setup_logger(name: str = __name__) -> logging.Logger:
    return get_engine_logger(name, _LOGS_DIR)

# -----------------------------------------------------------------------------
# Tick rounding helper — round price to minimum increment for SPX options
# -----------------------------------------------------------------------------
# SPX/SPXW options tick size = $0.05. Prices not on a tick boundary cause
# Error 110 "price does not conform to minimum price variation" rejections.
#
# For SELL limit orders (credit spreads) we floor to the nearest tick.
# Rounding UP would demand MORE credit than calculated, making fills harder.
# Rounding DOWN accepts slightly less — better fill probability.
# Floor is also safe: the minimum valid credit is TICK_SIZE (0.05), so any
# raw credit below 0.025 is already too small to trade and will be filtered
# by the engine's premium check before we get here.
TICK_SIZE = 0.05

def _round_to_tick(price: float) -> float:
    """Floor price to nearest SPX tick increment (0.05).

    We floor (not round) because this is used for SELL limit prices:
    rounding up would ask for more credit than the market offers and hurt fills.

    A small epsilon (1e-9) is added before flooring to guard against floating
    point imprecision — e.g. 0.30/0.05 = 5.9999... without it, which would
    incorrectly floor to 0.25 instead of 0.30.
    """
    import math
    return math.floor((price + 1e-9) / TICK_SIZE) * TICK_SIZE




# -----------------------------------------------------------------------------
# Result holder — shared between threads so the caller can wait for a result
# -----------------------------------------------------------------------------

class ResultHolder:
    __slots__ = ("value", "exc", "_event")

    def __init__(self):
        self.value: Any = None
        self.exc: Optional[Exception] = None
        self._event = threading.Event()

    def set_result(self, value: Any) -> None:
        self.value = value
        self._event.set()

    def set_exception(self, exc: Exception) -> None:
        self.exc = exc
        self._event.set()

    def get(self, timeout: float = 30.0) -> Any:
        if not self._event.wait(timeout=timeout):
            raise TimeoutError(f"IBKR call timed out after {timeout}s")
        if self.exc:
            raise self.exc
        return self.value


# -----------------------------------------------------------------------------
# Shared request queue + state
# -----------------------------------------------------------------------------

class _IBThreadState:
    def __init__(self, host: str, port: int, client_id: int, log: logging.Logger):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.log = log

        # Queue: main → IB thread. Items are (fn, args, kwargs, ResultHolder)
        self.request_queue: queue.Queue[Optional[tuple]] = queue.Queue()

        # Set True by IB thread when connection is established
        self._connected = threading.Event()
        self._connected.clear()

        # Reference to the current IB instance (only valid when _connected is set)
        # TASK-2026-191: Reconnect always uses a fresh IB instance to avoid
        # "event loop already running" state corruption from failed attempts.
        self._ib = None

        # Pending orders (maintained by IB thread; accessed only in callbacks)
        self._pending_orders: Dict[int, OrderParams] = {}

        # Set True to signal the IB thread to shut down
        self._shutdown = threading.Event()

        # Heartbeat tracking for health check
        self._last_heartbeat = time.monotonic()

        # Callback for fill confirmation (set by engine)
        self._fill_callback: Optional[Callable[..., None]] = None

        # Set True by main thread to request an explicit reconnect
        self._reconnect_requested = threading.Event()

        # Live combo (BAG) market-data subscriptions for open positions, keyed by
        # a caller-supplied position key. Holds streaming ib_async Ticker objects
        # that the IB thread's event loop keeps fresh. Used for the L2 premium
        # (debit-to-close) exit vote. One BAG line per open position; cancelled on
        # close. Touched only on the IB thread.
        self._combo_tickers: Dict[str, Any] = {}


# -----------------------------------------------------------------------------
# IB thread worker
# -----------------------------------------------------------------------------

def _ib_thread_worker(state: _IBThreadState) -> None:
    log = state.log
    host, port, client_id = state.host, state.port, state.client_id

    # Track consecutive failed connect attempts for backoff
    _consecutive_failures = 0

    def _wire_callbacks(ib: IB) -> None:
        ib.openOrderEvent += _on_open_order
        ib.orderStatusEvent += _on_order_status
        ib.execDetailsEvent += _on_exec_details
        ib.commissionReportEvent += _on_commission_report
        ib.positionEvent += _on_position
        ib.disconnectedEvent += _on_disconnected

    def _safe_call(fn: Callable, *args, **kwargs) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            state.log.critical(f"Callback exception in {fn.__name__}: {e}")
            state._connected.clear()
            return None

    def _do_connect() -> bool:
        """
        Attempt to connect once using a fresh IB instance. Returns True on
        success, False on failure. Never raises — all errors are caught and
        logged. Creates a new IB() each attempt to avoid stale event-loop state.
        """
        nonlocal _consecutive_failures
        ib = IB()
        try:
            ib.connect(host, port, clientId=client_id, timeout=15, readonly=False)
            # Give ib_async time to complete post-connect handshake
            ib.sleep(0.5)
            state._ib = ib
            # Fresh IB instance → any prior combo tickers are dead. Clear so the
            # engine re-subscribes cleanly (its per-tick subscribe is idempotent).
            state._combo_tickers.clear()
            _wire_callbacks(ib)
            state._connected.set()
            _consecutive_failures = 0
            state.log.info(
                f"IBKR connected {host}:{port} client_id={client_id}"
            )
            return True
        except Exception as e:
            state.log.warning(f"Connect failed: {e}")
            try:
                ib.disconnect()
            except Exception:
                pass
            _consecutive_failures += 1
            return False

    def _on_disconnected():
        try:
            log.warning("IBKR connection lost (background thread)")
            connected_was_set = state._connected.is_set()
            state._connected.clear()
            if connected_was_set:
                _do_connect()
        except Exception as e:
            log.critical(f"_on_disconnected: {e}")

    def _on_open_order(orderId, contract, order, state_):
        try:
            state.log.info(
                f"OPEN_ORDER id={orderId} {order.action} {order.totalQuantity} "
                f"{contract.symbol} {contract.strike} {contract.right} "
                f"status={state_.status}"
            )
        except Exception as e:
            state.log.critical(f"_on_open_order exception: {e}")
            state._connected.clear()

    def _on_order_status(
        orderId, status, filled, remaining, avgFillPrice, permId, parentId,
        lastFillPrice, clientId, whyHeld, mktCapPrice,
    ):
        try:
            state.log.info(
                f"ORDER_STATUS id={orderId} status={status} "
                f"filled={filled} avgPrice={avgFillPrice} whyHeld={whyHeld}"
            )
            if status == "Filled":
                state.log.info(f"  └─ FILLED id={orderId}: {filled} @ {avgFillPrice}")
                _safe_call(_mark_filled, orderId, avgFillPrice, filled)
            elif status == "Rejected":
                state.log.error(f"ORDER REJECTED id={orderId}: {whyHeld}")
                state._pending_orders.pop(orderId, None)
        except Exception as e:
            state.log.critical(f"_on_order_status exception: {e}")
            state._connected.clear()

    def _on_exec_details(reqId, contract, execution):
        try:
            state.log.info(
                f"EXEC_DETAILS: execId={execution.execId} | "
                f"{contract.symbol} {contract.strike} {contract.right} | "
                f"{execution.shares} @{execution.price} | "
                f"comm=${execution.commission} | time={execution.time}"
            )
        except Exception as e:
            state.log.critical(f"_on_exec_details exception: {e}")
            state._connected.clear()

    def _on_commission_report(report):
        try:
            state.log.info(
                f"COMMISSION: ${report.commission} {report.currency} | "
                f"realizedPnl={report.realizedPNL}"
            )
        except Exception as e:
            state.log.critical(f"_on_commission_report exception: {e}")
            state._connected.clear()

    def _on_position(account, contract, position, avgCost):
        try:
            state.log.debug(
                f"Position: {contract.symbol} {contract.strike} "
                f"{contract.right} | {position} @ {avgCost}"
            )
        except Exception as e:
            state.log.critical(f"_on_position exception: {e}")
            state._connected.clear()

    def _mark_filled(order_id: int, avg_price: float, filled: float):
        params = state._pending_orders.pop(order_id, None)
        if params is None:
            state.log.warning(f"No pending order found for filled order_id={order_id}")
            return
        state.log.info(
            f"FILL CONFIRMED: order_id={order_id} {params.action} "
            f"{params.contract_type} {params.strike} avg_price=${avg_price}"
        )
        if state._fill_callback is not None:
            try:
                state._fill_callback(order_id, params, avg_price, filled)
            except Exception as e:
                state.log.critical(f"_fill_callback exception: {e}")

    # ---------------------------------------------------------------------
    # TASK-2026-191: Initial connect with retry loop — never exit thread
    # on initial failure. Keeps retrying until explicitly shut down.
    # Uses exponential-ish backoff: 5s, 10s, 15s, cap 30s.
    # ---------------------------------------------------------------------
    connected = _do_connect()
    if not connected:
        backoff = 5.0
        cycle = 0
        log.warning(
            f"Initial connect failed — background thread entering "
            f"retry loop (backoff={backoff}s)"
        )
        while not connected and not state._shutdown.is_set():
            cycle += 1
            log.warning(
                f"Background retry cycle {cycle} (consecutive_failures="
                f"{_consecutive_failures}, backoff={backoff}s)"
            )
            # Block here for backoff — same-thread sleep, IB instance ref is None
            for step in range(int(backoff)):
                if state._shutdown.is_set():
                    log.info("Background thread shutting down during retry wait")
                    return
                time.sleep(1.0)
            connected = _do_connect()
            # Increase backoff up to 30s after every 2 failed cycles
            if not connected and cycle % 2 == 0:
                backoff = min(backoff + 5.0, 30.0)
                log.warning(
                    f"Increasing retry backoff to {backoff}s after "
                    f"{cycle} failed cycles"
                )

    # ---------------------------------------------------------------------
    # Main request-serving loop
    # ---------------------------------------------------------------------
    while True:
        if state._shutdown.is_set():
            log.info("Background thread shutting down")
            if state._ib is not None:
                try:
                    state._ib.disconnect()
                except Exception:
                    pass
            return

        # Handle explicit reconnect request from main thread
        if state._reconnect_requested.is_set():
            state._reconnect_requested.clear()
            log.info("Explicit reconnect requested — will reconnect at next cycle")
            state._connected.clear()
            # Drop the stale IB instance so a fresh one is created
            if state._ib is not None:
                try:
                    state._ib.disconnect()
                except Exception:
                    pass
                state._ib = None
            # Proceed to connect attempt below

        # If not currently connected, attempt reconnect now
        if not state._connected.is_set():
            connected = _do_connect()
            if not connected:
                backoff = min(5.0 + _consecutive_failures * 5.0, 30.0)
                log.warning(
                    f"Background reconnect failed — waiting {backoff}s before "
                    f"next attempt ({_consecutive_failures} consecutive failures)"
                )
                for step in range(int(backoff)):
                    if state._shutdown.is_set() or state._reconnect_requested.is_set():
                        break
                    time.sleep(1.0)
                if state._reconnect_requested.is_set():
                    state._reconnect_requested.clear()
                    log.info("Explicit reconnect during wait — cancelling backoff sleep")
                continue

        ib = state._ib
        try:
            item = state.request_queue.get(timeout=0.5)
        except queue.Empty:
            try:
                state._last_heartbeat = time.monotonic()
                ib.loopUntil(
                    condition=lambda: state.request_queue.qsize() > 0,
                    timeout=0.5,
                )
            except Exception as e:
                log.warning(f"IB loopUntil exception: {e}")
            continue

        if item is None:
            log.info("IB thread received shutdown signal")
            try:
                ib.disconnect()
            except Exception:
                pass
            return

        fn, args, kwargs, holder = item
        state._last_heartbeat = time.monotonic()
        try:
            result = fn(ib, *args, **kwargs)
            holder.set_result(result)
        except Exception as e:
            holder.set_exception(e)


# -----------------------------------------------------------------------------
# Public IBKR Client
# -----------------------------------------------------------------------------


class BlockingIBKRClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = None,
    ):
        import os as _os
        self.host = host
        self.port = port
        self.client_id = client_id if client_id is not None else (_os.getpid() % 100) + 1
        self._logger = _setup_logger("executor.ibkr")
        self._state = _IBThreadState(host, port, self.client_id, self._logger)

        self._ib_thread = threading.Thread(
            target=_ib_thread_worker,
            args=(self._state,),
            daemon=True,
            name="ibkr-thread",
        )
        self._ib_thread.start()

    # -------------------------------------------------------------------------
    # Synchronous non-blocking interface
    # -------------------------------------------------------------------------

    def is_connected(self) -> bool:
        if DRY_RUN:
            return True
        return self._state._connected.is_set()

    def _health_check(self) -> bool:
        try:
            if self._state.request_queue.qsize() > 5:
                self._logger.warning(
                    f"Health check FAILED: queue backlogged "
                    f"(qsize={self._state.request_queue.qsize()})"
                )
                return False
            elapsed = time.monotonic() - self._state._last_heartbeat
            if elapsed > 30.0:
                self._logger.warning(
                    f"Health check FAILED: IB thread stalled "
                    f"(no heartbeat in {elapsed:.1f}s)"
                )
                return False
            return True
        except Exception as e:
            self._logger.warning(f"Health check exception: {e}")
            return False

    def register_fill_callback(self, callback: Callable[..., None]) -> None:
        self._state._fill_callback = callback

    def _enqueue(self, fn: Callable[..., T], *args, timeout: float = 30.0, **kwargs) -> T:
        holder = ResultHolder()
        self._state.request_queue.put((fn, args, kwargs, holder), block=True)
        return holder.get(timeout=timeout)

    # -------------------------------------------------------------------------
    # Connection / lifecycle
    # -------------------------------------------------------------------------

    def connect(self) -> bool:
        """
        Signal the background thread to reconnect (only if already connected,
        to force a fresh connection). Returns current connection state.
        The background thread manages its own retry loop for initial connection
        — calling connect() before that loop is done would trigger an unnecessary
        disconnect/reconnect cycle.
        """
        if DRY_RUN:
            self._logger.info("DRY_RUN: connect() — skipped")
            return True
        if self.is_connected():
            self._logger.info("connect() called while connected — requesting reconnect")
            self._state._reconnect_requested.set()
        else:
            self._logger.info("connect() called but not yet connected — background thread is retrying")
        return self.is_connected()

    def disconnect(self) -> None:
        self._state._shutdown.set()
        self._state.request_queue.put(None, block=True)
        self._ib_thread.join(timeout=10.0)
        self._logger.info("IBKR client disconnected")

    # -------------------------------------------------------------------------
    # Order placement
    # -------------------------------------------------------------------------

    def place_order(self, params: OrderParams) -> FillResult:
        if DRY_RUN:
            return self._dry_run_place_order(params)

        if not self.is_connected():
            msg = "Not connected to IBKR"
            self._logger.error(f"ORDER_REJECTED: {msg}")
            return FillResult(
                order_id=None, filled=False, avg_price=None,
                contracts=params.quantity, status="rejected", message=msg,
            )

        try:
            order_id, initial_status = self._enqueue(_place_order_impl, params, self._logger, timeout=20.0)
        except TimeoutError:
            msg = "IBKR order placement timed out after 20s"
            self._logger.warning(f"ORDER_REJECTED: {msg}")
            return FillResult(
                order_id=None, filled=False, avg_price=None,
                contracts=params.quantity, status="rejected", message=msg,
            )
        except Exception as e:
            msg = f"placeOrder call failed: {e}"
            self._logger.error(f"ORDER_REJECTED: {msg}")
            return FillResult(
                order_id=None, filled=False, avg_price=None,
                contracts=params.quantity, status="rejected", message=msg,
            )

        # Immediate rejection: IBKR signalled Inactive/Cancelled at ACK time.
        # Return rejected status now so the engine never adds this to pending.
        if initial_status in ('Inactive', 'Cancelled', 'ApiCancelled'):
            msg = f"Order {order_id} immediately {initial_status} at ACK — rejected by IBKR"
            self._logger.warning(f"ORDER_REJECTED_IMMEDIATE | id={order_id} status={initial_status!r} | {msg}")
            return FillResult(
                order_id=order_id, filled=False, avg_price=None,
                contracts=params.quantity, status="rejected", message=msg,
            )

        if order_id is not None:
            self._state._pending_orders[order_id] = params

        self._logger.info(
            f"ORDER_PLACED id={order_id} | {params.action} "
            f"{params.quantity}x SPX {params.contract_type} "
            f"{params.strike}/{params.long_strike} exp {params.expiry}"
        )

        return FillResult(
            order_id=order_id,
            filled=False,
            avg_price=None,
            contracts=params.quantity,
            status="pending",
            message=f"Order {order_id} submitted, awaiting fill callback",
        )

    def cancel_order(self, order_id: int) -> bool:
        if DRY_RUN:
            self._logger.info(f"DRY_RUN: cancel_order({order_id})")
            return True
        if not self.is_connected():
            return False
        try:
            self._enqueue(_cancel_order_impl, order_id, self._logger, timeout=6.0)
        except Exception as e:
            self._logger.warning(f"cancel_order({order_id}) failed: {e}")
            return False
        self._logger.info(f"ORDER_CANCELLED id={order_id}")
        self._state._pending_orders.pop(order_id, None)
        return True

    def get_buying_power(self) -> float:
        if DRY_RUN:
            return 999999.0
        return self._enqueue(_get_buying_power_impl, timeout=10.0)

    def get_available_cash(self) -> float:
        if DRY_RUN:
            return 999999.0
        return self._enqueue(_get_available_cash_impl, timeout=10.0)

    def get_margin_limit(self) -> float:
        """Return NetLiquidation as the margin limit for entry checks."""
        if DRY_RUN:
            return 999999.0
        return self._enqueue(_get_margin_limit_impl, timeout=10.0)

    def get_open_positions_ibkr(self) -> List:
        if DRY_RUN:
            return []
        return self._enqueue(_get_positions_impl, timeout=10.0)

    def reconcile(self, store) -> None:
        if DRY_RUN:
            return
        self._enqueue(_reconcile_impl, store, timeout=20.0)

    # -------------------------------------------------------------------------
    # TASK-2026-179: Polling methods (primary sync mechanism — no callbacks)
    # -------------------------------------------------------------------------

    def reqAllOpenOrders(self) -> List:
        """
        Return all open orders from IBKR as a list of Trade objects.
        Used by engine._poll_pending_orders() every tick as the authoritative
        sync mechanism (polling-only, not callbacks).
        Returns [] in DRY_RUN mode.
        """
        if DRY_RUN:
            return []
        try:
            return self._enqueue(_get_open_orders_impl, timeout=10.0)
        except Exception as e:
            self._logger.warning(f"reqAllOpenOrders failed: {e}")
            return []

    def query_order_status(self, order_id: int):
        """
        Return the Trade object for a specific order_id, or None if not found.
        Used when an order_id is no longer in the open orders list — check
        if it was filled, cancelled, or just not in the current snapshot.

        Returns None in DRY_RUN mode.
        """
        if DRY_RUN:
            return None
        try:
            return self._enqueue(_query_order_impl, order_id, timeout=6.0)
        except Exception as e:
            self._logger.warning(f"query_order_status({order_id}) failed: {e}")
            return None

    # -------------------------------------------------------------------------
    # Live combo (BAG) marks — for the L2 premium (debit-to-close) exit vote
    # -------------------------------------------------------------------------

    def subscribe_combo_mark(
        self, key, expiry: str, short_strike: float, long_strike: float, right: str,
    ) -> bool:
        """Open one streaming BAG line for an open position. No-op in DRY_RUN /
        when disconnected. Idempotent (re-subscribing an existing key is a no-op).
        """
        if DRY_RUN or not self.is_connected():
            return False
        try:
            return bool(self._enqueue(
                _subscribe_combo_impl, self._state, str(key), expiry,
                short_strike, long_strike, right, self._logger, timeout=10.0,
            ))
        except Exception as e:
            self._logger.warning(f"subscribe_combo_mark({key}) failed: {e}")
            return False

    def get_combo_debit(self, key) -> Optional[float]:
        """Current debit-to-close for a subscribed position, or None if no live
        quote yet / DRY_RUN / disconnected. None simply skips the premium vote."""
        if DRY_RUN or not self.is_connected():
            return None
        try:
            return self._enqueue(_read_combo_debit_impl, self._state, str(key), timeout=5.0)
        except Exception as e:
            self._logger.warning(f"get_combo_debit({key}) failed: {e}")
            return None

    def unsubscribe_combo_mark(self, key) -> None:
        """Cancel a position's BAG line (call on close). No-op in DRY_RUN."""
        if DRY_RUN or not self.is_connected():
            return
        try:
            self._enqueue(_unsubscribe_combo_impl, self._state, str(key), self._logger, timeout=5.0)
        except Exception as e:
            self._logger.warning(f"unsubscribe_combo_mark({key}) failed: {e}")

    # -------------------------------------------------------------------------
    # DRY_RUN helpers
    # -------------------------------------------------------------------------

    def _dry_run_place_order(self, params: OrderParams) -> FillResult:
        self._logger.info(
            f"DRY_RUN | {params.action} {params.side} {params.quantity} "
            f"{params.symbol} {params.contract_type} {params.strike} "
            f"exp {params.expiry} — credit/debit: {params.credit_debit}"
        )
        self._order_id = getattr(self, "_order_id", 1000) + 1
        return FillResult(
            order_id=self._order_id,
            filled=False,
            avg_price=None,
            contracts=params.quantity,
            status="dry_run",
            message=f"DRY_RUN: would {params.side} {params.quantity} "
                    f"{params.contract_type} {params.strike} "
                    f"for {params.symbol}",
        )


# -----------------------------------------------------------------------------
# Per-call implementations (all run in the IB thread, receive the IB instance)
# -----------------------------------------------------------------------------


def _combo_bag(ib: IB, expiry: str, short_strike: float, long_strike: float, right: str) -> Contract:
    """Qualify the two legs and build the BAG used to mark an SPX credit spread.

    Uses the same leg layout as an OPEN order (SELL short / BUY long) because only
    that direction returns a real market from IBKR (bid/ask negative = credit).
    """
    short_opt = Option("SPX", expiry, short_strike, right, "SMART", tradingClass="SPXW")
    long_opt  = Option("SPX", expiry, long_strike,  right, "SMART", tradingClass="SPXW")
    ib.qualifyContracts(short_opt, long_opt)
    if not (short_opt.conId and long_opt.conId):
        raise RuntimeError(
            f"combo qualify failed short={short_opt.conId} long={long_opt.conId}"
        )
    bag = Contract()
    bag.symbol = "SPX"
    bag.secType = "BAG"
    bag.currency = "USD"
    bag.exchange = "SMART"
    bag.comboLegs = [
        ComboLeg(conId=short_opt.conId, ratio=1, action="SELL", exchange="SMART"),
        ComboLeg(conId=long_opt.conId,  ratio=1, action="BUY",  exchange="SMART"),
    ]
    return bag


def _subscribe_combo_impl(
    ib: IB, state: "_IBThreadState", key: str, expiry: str,
    short_strike: float, long_strike: float, right: str, log: logging.Logger,
) -> bool:
    """Open a streaming BAG market-data line for an open position (IB thread)."""
    if key in state._combo_tickers:
        return True
    bag = _combo_bag(ib, expiry, short_strike, long_strike, right)
    ticker = ib.reqMktData(bag, "", False, False)  # streaming (one line)
    state._combo_tickers[key] = ticker
    log.info(
        f"COMBO_MKTDATA_SUB | key={key} {right} {short_strike:.0f}/{long_strike:.0f} "
        f"exp={expiry} (1 BAG line)"
    )
    return True


def _read_combo_debit_impl(ib: IB, state: "_IBThreadState", key: str) -> Optional[float]:
    """Return current debit-to-close (abs of the BAG mid) for a subscribed position."""
    import math
    ticker = state._combo_tickers.get(key)
    if ticker is None:
        return None

    def _ok(x) -> bool:
        return x is not None and not (isinstance(x, float) and math.isnan(x))

    bid, ask = getattr(ticker, "bid", None), getattr(ticker, "ask", None)
    if _ok(bid) and _ok(ask):
        mid = (bid + ask) / 2.0
    elif _ok(getattr(ticker, "close", None)):
        mid = ticker.close
    else:
        return None
    # BAG bid/ask are negative (you receive credit to open); debit to close = |mid|.
    return abs(mid)


def _unsubscribe_combo_impl(
    ib: IB, state: "_IBThreadState", key: str, log: logging.Logger,
) -> bool:
    """Cancel a position's streaming BAG line on close (IB thread)."""
    ticker = state._combo_tickers.pop(key, None)
    if ticker is not None:
        try:
            ib.cancelMktData(ticker.contract)
        except Exception:
            pass
        log.info(f"COMBO_MKTDATA_CANCEL | key={key}")
    return True


def _place_order_impl(ib: IB, params: OrderParams, log: logging.Logger) -> int:
    """
    Qualify individual option legs, build a BAG contract, and place the order.
    Runs entirely within the IB thread.

    IB BAG/combo convention for credit spreads:
      OPEN  → combo_action="BUY",  lmtPrice=-credit (negative)
      CLOSE → combo_action="SELL", MarketOrder (buying back the spread)
    """
    right = "C" if params.contract_type in ("CALL", "C") else "P"

    short_opt = Option("SPX", params.expiry, params.strike,      right, "SMART", tradingClass="SPXW")
    long_opt  = Option("SPX", params.expiry, params.long_strike, right, "SMART", tradingClass="SPXW")

    log.info(
        f"ORDER_QUALIFY | {params.action} {params.contract_type} "
        f"{params.strike}/{params.long_strike} exp={params.expiry} "
        f"qty={params.quantity} credit={params.credit_debit}"
    )
    ib.qualifyContracts(short_opt, long_opt)
    ib.sleep(1)

    if not (short_opt.conId and long_opt.conId):
        log.error(
            f"ORDER_QUALIFY_FAILED | short conId={short_opt.conId} "
            f"long conId={long_opt.conId} — aborting order"
        )
        raise RuntimeError(
            f"qualifyContracts failed: short conId={short_opt.conId}, "
            f"long conId={long_opt.conId}"
        )

    log.info(
        f"ORDER_QUALIFY_OK | short conId={short_opt.conId} "
        f"long conId={long_opt.conId}"
    )

    # Both OPEN and CLOSE use the same physical legs:
    #   short leg (higher strike): defined first
    #   long leg  (lower strike):  defined second
    # The combo_action flips the direction:
    #   BUY  the combo → execute legs as defined   → SELL higher + BUY lower (open credit spread)
    #   SELL the combo → execute legs in reverse   → BUY  higher + SELL lower (close credit spread)
    #
    # Verified empirically: only "BUY combo [SELL higher, BUY lower]" returns
    # real market data from IBKR (bid/ask are negative = you receive credit).
    # "SELL combo [SELL higher, BUY lower]" shows bid/ask=-1 (no market = wrong direction).
    short_action, long_action = "SELL", "BUY"   # same for both OPEN and CLOSE

    if params.action == "OPEN":
        # BUY the combo to sell the credit spread and receive premium.
        # lmtPrice is NEGATIVE: you receive money, so the cost is negative.
        # e.g. lmtPrice=-0.25 means "fill me if I receive at least $0.25 credit".
        combo_action = "BUY"
        lmt_price = -_round_to_tick(abs(params.credit_debit)) if params.credit_debit else None
    else:
        # SELL the combo to buy back the spread (execute BUY higher + SELL lower).
        # Market order — pay whatever debit the market asks to exit.
        combo_action = "SELL"
        lmt_price = None

    bag = Contract()
    bag.symbol   = "SPX"
    bag.secType  = "BAG"
    bag.currency = "USD"
    bag.exchange = "SMART"
    bag.comboLegs = [
        ComboLeg(conId=short_opt.conId, ratio=1, action=short_action, exchange="SMART"),
        ComboLeg(conId=long_opt.conId,  ratio=1, action=long_action,  exchange="SMART"),
    ]

    if params.action == "OPEN" and lmt_price is not None:
        order = LimitOrder(action=combo_action, totalQuantity=params.quantity, lmtPrice=lmt_price)
        order_desc = f"LMT {combo_action} qty={params.quantity} lmtPrice={lmt_price:+.4f}"
    else:
        order = MarketOrder(action=combo_action, totalQuantity=params.quantity)
        order_desc = f"MKT {combo_action} qty={params.quantity}"
    order.tif = "DAY"
    order.outsideRth = False
    account_id = CONFIG["ibkr"].get("account_id")
    if account_id:
        order.account = account_id

    log.info(
        f"ORDER_SUBMIT | {order_desc} | "
        f"legs=[{short_action} {short_opt.conId}, {long_action} {long_opt.conId}] | "
        f"account={account_id or 'default'}"
    )

    trade = ib.placeOrder(bag, order)
    # Give TWS time to acknowledge and fire initial status callbacks.
    ib.sleep(2)

    if isinstance(trade, int):
        # Unexpected: ib.placeOrder returned raw orderId directly.
        log.warning(f"ORDER_SUBMIT_RAW_ID | orderId={trade} (ib_async returned int, not Trade)")
        return trade, 'unknown'

    if hasattr(trade, 'order') and trade.order:
        order_id = trade.order.orderId
        initial_status = trade.orderStatus.status if hasattr(trade, 'orderStatus') else 'unknown'
        is_bad = initial_status in ('Inactive', 'Cancelled', 'ApiCancelled', '')

        # Capture IBKR rejection reason from trade log entries when Inactive
        reject_detail = ""
        if is_bad and hasattr(trade, 'log') and trade.log:
            messages = [e.message for e in trade.log if e.message]
            if messages:
                reject_detail = f" | ibkr_reason={messages[-1]!r}"

        log_fn = log.warning if is_bad else log.info
        log_fn(
            f"ORDER_ACK | id={order_id} tws_status={initial_status!r} "
            f"{order_desc}{reject_detail} | "
            f"{'REJECTED' if is_bad else 'OK'}"
        )
        return order_id, initial_status

    log.error(f"ORDER_SUBMIT_FAILED | placeOrder returned unexpected value: {trade!r}")
    return None, 'unknown'


def _cancel_order_impl(ib: IB, order_id: int, log: logging.Logger) -> None:
    # ib.cancelOrder() requires a Trade object, not a raw int.
    # Find the matching trade from the current open trades.
    for trade in ib.trades():
        if trade.order.orderId == order_id:
            log.info(f"ORDER_CANCEL | id={order_id} found in ib.trades() — cancelling")
            ib.cancelOrder(trade.order)
            return
    # Fall back to reqAllOpenOrders if not in local trades list.
    for trade in ib.reqAllOpenOrders():
        if trade.order.orderId == order_id:
            log.info(f"ORDER_CANCEL | id={order_id} found via reqAllOpenOrders — cancelling")
            ib.cancelOrder(trade.order)
            return
    log.warning(f"ORDER_CANCEL_NOT_FOUND | id={order_id} not in ib.trades() or open orders — may already be filled/cancelled")


def _get_buying_power_impl(ib: IB) -> float:
    account_id = CONFIG["ibkr"].get("account_id")
    ib.reqAccountSummary()
    ib.sleep(0.5)
    for av in ib.accountSummary():
        if av.tag == "BuyingPower" and (not account_id or av.account == account_id):
            return float(av.value)
    return 0.0


def _get_available_cash_impl(ib: IB) -> float:
    account_id = CONFIG["ibkr"].get("account_id")
    ib.reqAccountSummary()
    ib.sleep(0.5)
    for av in ib.accountSummary():
        if av.tag == "AvailableFunds" and (not account_id or av.account == account_id):
            return float(av.value)
    return 0.0


def _get_margin_limit_impl(ib: IB) -> float:
    """Return margin limit with fallback chain for paper and live accounts.

    Tries in order: NetLiquidation -> AvailableFunds -> BuyingPower.
    If account_id is set but not found in summary, falls back to any account.
    Paper accounts often have NetLiquidation=0; this ensures they still get a limit.
    """
    import logging as _log_module
    _logger = _log_module.getLogger(__name__)

    account_id = CONFIG["ibkr"].get("account_id")
    ib.reqAccountSummary()
    ib.sleep(0.5)

    summary = list(ib.accountSummary())
    _logger.debug(f"Account summary tags: {[(av.tag, av.value, av.account) for av in summary]}")

    # Tags to try in order of preference
    tags_to_try = ["NetLiquidation", "AvailableFunds", "BuyingPower"]

    for tag in tags_to_try:
        # First: exact account match
        for av in summary:
            if av.tag == tag and (not account_id or av.account == account_id):
                value = float(av.value)
                _logger.debug(f"Margin limit via {tag} (account={av.account}): ${value:,.2f}")
                return value

        # Fallback for paper accounts: no exact account match -> accept any account
        if account_id:
            for av in summary:
                if av.tag == tag:
                    value = float(av.value)
                    _logger.debug(f"Margin limit via {tag} (any account={av.account}): ${value:,.2f}")
                    return value

    _logger.warning("Could not determine margin limit — no valid tag found in account summary")
    return 0.0


def _get_positions_impl(ib: IB) -> List:
    return ib.reqPositions()


def _reconcile_impl(ib: IB, store) -> None:
    """Compare IBKR positions against positions.db. Runs in IB thread."""
    ibkr_raw = ib.reqPositions()

    ibkr_map: Dict[tuple, int] = {}
    for pos in ibkr_raw:
        c = pos.contract
        if c.symbol != "SPX":
            continue
        key = (c.symbol, c.right, c.strike)
        ibkr_map[key] = pos.position

    for db_pos in store.get_open():
        key = ("SPX", db_pos.side.value, db_pos.short_strike)
        if key not in ibkr_map:
            store.close_position(
                db_pos.db_id,
                status="closed_manual",
                notes="Closed manually in TWS, detected on reconciliation",
            )


# TASK-2026-179: Polling-only methods
def _get_open_orders_impl(ib: IB) -> List:
    """Return all open orders. Used for polling sync every tick."""
    return ib.reqAllOpenOrders()


def _query_order_impl(ib: IB, order_id: int):
    """
    Return Trade for a specific order_id, or None if not found anywhere.

    reqAllOpenOrders() only returns orders still pending — filled and inactive
    orders are removed from that list immediately by IBKR. Fall back to
    ib.trades() which holds all orders placed in the current session
    (including filled and rejected ones) so fast fills aren't missed.
    """
    for trade in ib.reqAllOpenOrders():
        if trade.order.orderId == order_id:
            return trade
    # Not in open orders — check session trade history (filled/inactive/cancelled)
    for trade in ib.trades():
        if trade.order.orderId == order_id:
            return trade
    return None
