"""
engine.py — main evaluation loop with structured decision logging.

TASK-2026-179: Pending-state protocol for live trading.
  - pending_open written to DB BEFORE order sent to IBKR
  - _poll_pending_orders() every tick — primary sync mechanism (no callbacks)
  - _check_pending_timeouts() every tick — 10-min timeout, rollback stale entries
  - Telegram only fires after confirmed fill via polling in LIVE mode
  - get_open_strikes() includes pending_open for collision checking
  - DRY_RUN unchanged (simulate fill immediately, same as current)

TASK-2026-185: Cash check BEFORE entry in LIVE mode.
  - estimate_required_margin() called from executor to compute required margin
  - get_available_cash() checked before any DB write or order send
  - Insufficient cash: reject silently — NO Telegram, NO DB write, log only
  - DRY_RUN: always passes (get_available_cash returns 999999.0)

TASK-2026-070: USE_MARGIN_LIMIT flag in config.yaml.
  - When ibkr.use_margin_limit: true, engine uses get_margin_limit()
    (NetLiquidation) instead of get_available_cash() (AvailableFunds)
  - Sarthak's request: available cash was too restrictive; margin line
    lets engine take trades that would otherwise be incorrectly rejected

TASK-2026-XXX: VIX-bucket RSI gate lookup.
  - RSI gates are now looked up per-VIX-bucket from CONFIG["entry"]["vix_buckets"]
  - tick_processor._get_rsi_gates(vix) maps VIX → (rsi_upper, rsi_lower)
  - Bucket mapping: 13-16, 16-20, 20-25, 25-30
  - VIX < 13 or VIX > 30 → no-trade zone (widest gate defaults, caller enforces)
"""
import logging
import time
import sys
from pathlib import Path
from typing import Optional
from datetime import datetime

_SELF_DIR = Path(__file__).parent
_SRC_DIR  = _SELF_DIR.parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))

from config import CONFIG
from log_setup import get_engine_logger
from tick_processor import TickProcessor, _get_rsi_gates
from telegram_notifier import (
    notify_entry, notify_exit,
    notify_rejection, notify_timeout,
    notify_day_gate,
)
from executor import estimate_required_margin
from day_gate import DayGate

# ---------------------------------------------------------------------------
# DRY_RUN — driven from config.yaml (safe default: True)
# ---------------------------------------------------------------------------
DRY_RUN: bool = CONFIG.get("dry_run", True)

# ---------------------------------------------------------------------------
# Pending order timeout (10 minutes)
# ---------------------------------------------------------------------------
PENDING_TIMEOUT_SECONDS: int = 600

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LOGS_DIR = _SRC_DIR / CONFIG["paths"]["logs"]

def _log(name: str = __name__) -> logging.Logger:
    return get_engine_logger(name, _LOGS_DIR)

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class AutoTraderEngine:
    """
    Main SPX 0DTE evaluation loop with distance-based entry + RSI gate.

    TASK-2026-179: Pending-state protocol for live trading.
      - Pending entries stored in _pending_entries keyed by order_id
      - Pending exits stored in _pending_exits keyed by order_id
      - Polling every tick reconciles IBKR state (no callbacks as primary mechanism)
      - Telegram fires only after confirmed fill in LIVE mode

    TASK-2026-185: Cash check before entry in LIVE mode.
      - estimate_required_margin() computes worst-case margin for the spread
      - get_available_cash() checked BEFORE any DB write or IBKR order
      - Insufficient cash: silent reject, no Telegram, no DB write, log only

    RSI Gate: Each tick, RSI determines which side to evaluate.
      - RSI > rsi_upper_threshold → evaluate/sell CALLS only
      - RSI < rsi_lower_threshold → evaluate/sell PUTS only
      - RSI in neutral band → no entry for either side this tick
      - Gates are VIX-adaptive: looked up from CONFIG["entry"]["vix_buckets"]
    """

    def __init__(
        self,
        check_interval: Optional[int] = None,
        mode: str = "local",
        backtest_date: Optional[str] = None,
        store_db_path: Optional[str] = None,
    ):
        from position_store import PositionStore

        self.check_interval = (
            check_interval
            if check_interval is not None
            else CONFIG["engine"]["check_interval_seconds"]
        )

        # Run mode: "local" | "cloud" | "backtest".
        #   - local/cloud share the live path (real IBKR client, real-time loop)
        #     and differ ONLY in data source, which is resolved inside
        #     combined_reader via config data_source_mode. The decision logic
        #     (tick/process_tick/gates/entry/exit) is identical for both.
        #   - backtest forces the DRY_RUN fill path (fills at scan mid =
        #     decision.credit), replays a single historical date, and writes to
        #     an isolated positions DB. It runs the SAME tick()/process_tick().
        self.mode = mode
        self._backtest_date = backtest_date
        self.dry_run: bool = True if mode == "backtest" else DRY_RUN
        self.logger = _log("engine")

        # Backtest seams (unused in live/cloud):
        #   _clock           — parsed timestamp of the snapshot being replayed,
        #                       so EOD/time logic uses backtest time not wall-clock
        #   _current_combined — the snapshot tick() consumes this iteration
        self._clock: Optional[datetime] = None
        self._current_combined = None

        self.store = PositionStore(db_path=store_db_path)
        self.store.init()
        self.store.load_open()

        # TASK-2026-???: Load pending orders from DB on restart for recovery
        # If engine restarts before pending orders are confirmed, reload them so
        # _poll_pending_orders() can track them through to fill.
        self._recover_pending_orders()

        self._client = None
        self._TASK_ID = "TASK-2026-140"

        # Per-side skip tracking
        self._last_skip_reason = {"CALL": None, "PUT": None}
        self._last_rsi_gate_skipped = {"CALL": None, "PUT": None}

        # EOD expiry sweep — tracks dates already processed so it fires once/day
        self._eod_expiry_done: set[str] = set()

        # Rolling 30-min volatility gate
        self._day_gate = DayGate(logger=self.logger)

        # TASK-2026-227: Pre-fill gate buffer from historical GEX data so gate is
        # operative immediately on engine start (no 30-min warm-up).  If no scan data
        # is available yet (e.g. engine started before market open) the gate starts
        # empty and will pre-fill on the first successful tick.
        try:
            from combined_reader import get_combined_for_latest_scan
            combined = get_combined_for_latest_scan()
            self._day_gate.prefill_from_db(combined.scan_timestamp)
        except Exception:
            pass  # No data yet — gate starts with empty buffer

        # TASK-2026-179: Pending entries awaiting fill confirmation
        # Key: order_id, Value: (pos, ts, decision, em, gex_val, spx, db_id)
        self._pending_entries: dict[int, tuple] = {}
        self._pending_entry_times: dict[int, float] = {}  # order_id → epoch

        # TASK-2026-179: Pending exits awaiting fill confirmation
        # Key: order_id, Value: (pos_db_id, ts, decision, em, gex_val, spx, exit_layer)
        self._pending_exits: dict[int, tuple] = {}
        self._pending_exit_times: dict[int, float] = {}  # order_id → epoch

        self._processor = TickProcessor(
            on_enter_approved=self._on_enter_approved,
            on_skip          =self._on_skip,
            on_exit_checked  =self._on_exit_checked,
            on_heartbeat     =self._on_heartbeat,
            is_live          =not self.dry_run,
        )

    @property
    def client(self):
        if self._client is None:
            from executor import IBKRClient
            from config import CONFIG
            self._client = IBKRClient(
                port=CONFIG["ibkr"]["port"],
                client_id=CONFIG["ibkr"]["engine_client_id"]
            )
        return self._client

    # -------------------------------------------------------------------------
    # Mode seams — the ONLY behavioral differences between live and backtest.
    # Everything downstream (process_tick, gates, entry/exit rules) is shared.
    # -------------------------------------------------------------------------

    def _now(self) -> datetime:
        """Current time. Live/cloud: wall clock. Backtest: the snapshot's timestamp."""
        if self.mode == "backtest" and self._clock is not None:
            return self._clock
        return datetime.now()

    def _fetch_combined(self):
        """Fetch the combined snapshot for this tick.

        Live/cloud read the latest scan (as-of joined). Backtest replays the
        snapshot the run loop set for this iteration. Either way the engine sees
        an identical CombinedSnapshot and runs identical logic.
        """
        if self.mode == "backtest":
            return self._current_combined
        from combined_reader import get_combined_for_latest_scan
        return get_combined_for_latest_scan()

    def _parse_clock(self, ts: str) -> datetime:
        """Parse a scan timestamp (ISO with offset) into a naive datetime."""
        from combined_reader import _parse_ts
        main = _parse_ts(ts).split('.')[0]
        return datetime.strptime(main, "%Y-%m-%d %H:%M:%S")

    def _recover_pending_orders(self) -> None:
        """
        On engine restart, reload pending_open positions from DB and sync with IBKR.

        For each pending_open position:
        1. Query IBKR to check actual order status
        2. If FILLED: immediately call _on_fill_confirmed to update DB status → 'open'
        3. If still PENDING: add to _pending_entries for polling to track
        4. If CANCELLED/INACTIVE: log and mark as such

        This ensures the engine never loses track of pending orders across restarts.
        """
        from trades_db import get_conn
        import time as time_module

        if self.dry_run:
            return

        with get_conn(self.store.db_path) as conn:
            # Load pending entries (status='pending_open') from today
            pending_entries = conn.execute(
                "SELECT * FROM positions WHERE status = 'pending_open' "
                "AND date(open_time) = date('now') ORDER BY open_time DESC"
            ).fetchall()

            if pending_entries:
                from trades_db import _row_to_position
                self.logger.info(
                    f"[RECOVERY] Found {len(pending_entries)} pending_open position(s) from prior session"
                )
                for row in pending_entries:
                    pos = _row_to_position(row)
                    order_id = row[44]  # order_id column index in positions table
                    if not order_id:
                        self.logger.warning(f"[RECOVERY] Skipping position {row[0]} — no order_id")
                        continue

                    ts = row[6]  # open_time
                    db_id = row[0]  # id

                    # Query IBKR immediately to check actual order status
                    trade = None
                    status = None
                    try:
                        # Try open orders first (fastest)
                        all_open_orders = self.client.reqAllOpenOrders()
                        ibkr_open_by_id = {t.order.orderId: t for t in all_open_orders}

                        if order_id in ibkr_open_by_id:
                            trade = ibkr_open_by_id[order_id]
                            status = trade.orderStatus.status
                        else:
                            # Not in open orders — query session trade history
                            trade = self.client.query_order_status(order_id)
                            if trade is not None:
                                status = trade.orderStatus.status
                    except Exception as e:
                        self.logger.warning(f"[RECOVERY] Failed to query IBKR for order_id={order_id}: {e}")
                        status = None

                    # Handle based on actual IBKR status
                    if status == "Filled":
                        # Order was filled while app was down — confirm immediately
                        avg_price = trade.orderStatus.avgFillPrice if trade else 0.0
                        filled = self._get_filled_qty(trade) if trade else pos.num_contracts if hasattr(pos, 'num_contracts') else 1
                        self.logger.info(
                            f"[RECOVERY] Order FILLED while offline: order_id={order_id} | "
                            f"pos_id={db_id} | avg_price={avg_price:.4f} | confirming fill..."
                        )
                        self._on_fill_confirmed(order_id, trade.order if trade else None, avg_price, filled)

                    elif status in ("Submitted", "PendingSubmission", "PreSubmitted", "Accepted"):
                        # Still pending — add to _pending_entries to resume normal polling
                        self._pending_entries[order_id] = (
                            pos, ts, None, 0.0, 0.0, 0.0, db_id
                        )
                        self._pending_entry_times[order_id] = time_module.time()
                        self.logger.info(
                            f"[RECOVERY] Order still PENDING: order_id={order_id} | "
                            f"pos_id={db_id} | {pos.side.value} {pos.ticker} "
                            f"{pos.short_strike}/{pos.long_strike} | resuming poll..."
                        )

                    elif status in ("Cancelled", "ApiCancelled"):
                        # Order was cancelled — rollback the position
                        self.logger.warning(
                            f"[RECOVERY] Order CANCELLED: order_id={order_id} | pos_id={db_id} | rolling back..."
                        )
                        self._on_order_cancelled(order_id, reason="cancelled_offline")

                    elif status == "Inactive":
                        # Order rejected
                        self.logger.warning(
                            f"[RECOVERY] Order INACTIVE/REJECTED: order_id={order_id} | pos_id={db_id}"
                        )
                        self._on_entry_rejected(
                            order_id,
                            reason="inactive",
                            error_code=str(trade.orderStatus.errorCode) if trade else "0",
                            error_message=trade.orderStatus.errorMessage if trade else "Unknown",
                        )

                    else:
                        # Unknown status — log and add to pending for polling
                        self.logger.warning(
                            f"[RECOVERY] Unknown status for order_id={order_id}: {status} | "
                            f"pos_id={db_id} | adding to pending for polling"
                        )
                        self._pending_entries[order_id] = (
                            pos, ts, None, 0.0, 0.0, 0.0, db_id
                        )
                        self._pending_entry_times[order_id] = time_module.time()

    # -------------------------------------------------------------------------
    # TASK-2026-179: Polling — primary sync mechanism (every tick)
    # -------------------------------------------------------------------------

    def _poll_pending_orders(self) -> None:
        """
        Primary mechanism for IBKR state reconciliation.
        Called every tick to check status of all pending orders via polling.
        No callbacks — polling is authoritative.

        Uses client.reqAllOpenOrders() to get current open orders from IBKR,
        then checks each pending entry/exit against IBKR's reported state.
        """
        if self.dry_run:
            return

        try:
            all_open_orders = self.client.reqAllOpenOrders()
        except Exception as e:
            self.logger.warning(f"_poll_pending_orders: reqAllOpenOrders failed: {e}")
            return

        # Build lookup: orderId → Trade
        ibkr_open_by_id = {t.order.orderId: t for t in all_open_orders}

        # Check each pending entry
        for order_id in list(self._pending_entries.keys()):
            trade = None
            status = None

            if order_id in ibkr_open_by_id:
                trade = ibkr_open_by_id[order_id]
                status = trade.orderStatus.status
            else:
                # Not in current open orders — query session trade history
                trade = self.client.query_order_status(order_id)
                if trade is not None:
                    status = trade.orderStatus.status
                else:
                    # Not in open orders or session trade history — order is a ghost.
                    # This should not happen if _place_order_impl returns correct status.
                    # Log clearly and let the timeout handler clean it up.
                    self.logger.warning(
                        f"ORDER_GHOST | entry order_id={order_id} not found in IBKR "
                        f"open orders or session trades — likely submitted but never "
                        f"acknowledged; will be rolled back at timeout"
                    )
                    continue

            if status == "Filled":
                params = trade.order  # Trade object has .order with params
                avg_price = trade.orderStatus.avgFillPrice
                filled = self._get_filled_qty(trade)
                self._on_fill_confirmed(order_id, params, avg_price, filled)
            elif status in ("Cancelled", "ApiCancelled"):
                self._on_order_cancelled(order_id, reason="cancelled_by_ibkr")
            elif status == "Inactive":
                self._on_entry_rejected(
                    order_id,
                    reason="inactive",
                    error_code=trade.orderStatus.errorCode,
                    error_message=trade.orderStatus.errorMessage,
                )

        # Check each pending exit
        for order_id in list(self._pending_exits.keys()):
            trade = None
            status = None

            if order_id in ibkr_open_by_id:
                trade = ibkr_open_by_id[order_id]
                status = trade.orderStatus.status
            else:
                trade = self.client.query_order_status(order_id)
                if trade is not None:
                    status = trade.orderStatus.status
                else:
                    self.logger.warning(
                        f"ORDER_GHOST | exit order_id={order_id} not found in IBKR "
                        f"open orders or session trades — will timeout check"
                    )
                    continue

            if status == "Filled":
                params = trade.order
                avg_price = trade.orderStatus.avgFillPrice
                filled = self._get_filled_qty(trade)
                self._on_close_confirmed(order_id, params, avg_price, filled)
            elif status in ("Cancelled", "ApiCancelled"):
                self._on_order_cancelled(order_id, reason="cancelled_by_ibkr")
            elif status == "Inactive":
                self._on_close_rejected(
                    order_id,
                    reason="inactive",
                    error_code=trade.orderStatus.errorCode,
                    error_message=trade.orderStatus.errorMessage,
                )

    @staticmethod
    def _get_filled_qty(trade) -> float:
        """Extract actual filled quantity from a Trade object."""
        return float(trade.orderStatus.filled)

    # -------------------------------------------------------------------------
    # TASK-2026-179: 10-minute pending timeout check
    # -------------------------------------------------------------------------

    def _check_pending_timeouts(self) -> None:
        """
        Check all pending orders for age > PENDING_TIMEOUT_SECONDS.
        Entry timeout: rollback pending_open row, send Telegram.
        Exit timeout: keep position open, allow retry on next tick.
        """
        if self.dry_run:
            return

        now_ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-04:00")
        now_epoch = time.time()

        # ---- Entry timeouts ----
        stale_entry_ids = []
        for order_id, entry_time in self._pending_entry_times.items():
            age = now_epoch - entry_time
            if age > PENDING_TIMEOUT_SECONDS:
                stale_entry_ids.append(order_id)

        for order_id in stale_entry_ids:
            if order_id not in self._pending_entries:
                self._pending_entry_times.pop(order_id, None)
                continue

            pos, ts, decision, em, gex_val, spx, db_id = \
                self._pending_entries.pop(order_id)
            self._pending_entry_times.pop(order_id, None)

            # Cancel the IBKR order before rolling back DB row
            self.client.cancel_order(order_id)

            # Rollback pending_open row
            self.store.rollback_position(db_id)
            self.store._positions = [
                p for p in self.store._positions if p.db_id != db_id
            ]

            side_str = pos.side.value if hasattr(pos.side, 'value') else pos.side
            self.logger.warning(
                f"{now_ts} ET [ENTRY_TIMEOUT] order_id={order_id} | "
                f"{side_str} | strike={pos.short_strike:.0f}/{pos.long_strike:.0f} | "
                f"No fill after 10 min — rolling back pending_open row"
            )

            notify_timeout(
                msg_type="entry",
                side=side_str,
                short_strike=pos.short_strike,
                long_strike=pos.long_strike,
                note="No fill after 10 min | DB row rolled back",
            )

        # ---- Exit timeouts ----
        stale_exit_ids = []
        for order_id, exit_time in self._pending_exit_times.items():
            age = now_epoch - exit_time
            if age > PENDING_TIMEOUT_SECONDS:
                stale_exit_ids.append(order_id)

        for order_id in stale_exit_ids:
            if order_id not in self._pending_exits:
                self._pending_exit_times.pop(order_id, None)
                continue

            pos_db_id, ts, decision, em, gex_val, spx, exit_layer = \
                self._pending_exits.pop(order_id)
            self._pending_exit_times.pop(order_id, None)

            # Cancel the stale exit order at IBKR before updating DB
            self.client.cancel_order(order_id)

            # Update status to timeout (position stays open)
            with get_conn() as conn:
                update_position_status(conn, pos_db_id, status="timeout")
                conn.commit()

            self.logger.warning(
                f"{now_ts} ET [EXIT_TIMEOUT] order_id={order_id} | "
                f"pos_db_id={pos_db_id} | "
                f"Position remains open — exit decision reverted"
            )

            notify_timeout(
                msg_type="exit",
                pos_db_id=pos_db_id,
                note="No fill after 10 min | Position kept open",
            )

    # -------------------------------------------------------------------------
    # TASK-2026-179: Polling callbacks — DB update + Telegram after confirmed fill
    # -------------------------------------------------------------------------

    def _on_fill_confirmed(
        self,
        order_id: int,
        params,
        avg_price: float,
        filled: float,
    ) -> None:
        """
        Called by _poll_pending_orders() when IBKR reports fill.
        Updates pending_open → open in DB, sends Telegram notification.

        TASK-2026-179: Previously called via IBKR callback.
        Now triggered by polling every tick — no callback reliance.
        """
        if order_id not in self._pending_entries:
            self.logger.warning(
                f"_on_fill_confirmed: no pending entry for order_id={order_id}"
            )
            return

        pos, ts, decision, em, gex_val, spx, db_id = \
            self._pending_entries.pop(order_id)
        self._pending_entry_times.pop(order_id, None)

        # Update DB: pending_open → open, record fill info
        from trades_db import get_conn, update_position_status, update_position_fill, mark_signal_filled

        fill_ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-04:00")
        with get_conn() as conn:
            update_position_fill(conn, db_id, fill_price=avg_price, fill_time=fill_ts)
            update_position_status(conn, db_id, status="open")
            conn.commit()

        # TASK-2026-208: mark the entry signal as filled now that IBKR has confirmed
        with get_conn() as conn:
            cur = conn.execute(
                """SELECT id FROM signals
                   WHERE timestamp = ?
                     AND short_strike = ?
                     AND long_strike = ?
                     AND action = 'entry'
                   ORDER BY id DESC LIMIT 1""",
                (ts, pos.short_strike, pos.long_strike),
            )
            row = cur.fetchone()
            if row is not None:
                mark_signal_filled(conn, row[0])
                conn.commit()
            else:
                self.logger.warning(
                    f"_on_fill_confirmed: no entry signal found for "
                    f"ts={ts} short_strike={pos.short_strike:.0f} "
                    f"long_strike={pos.long_strike:.0f}"
                )

        # Update in-memory position status
        for p in self.store._positions:
            if p.db_id == db_id:
                p.status = "open"
                break

        self.logger.info(
            f"{ts} ET [FILLED_CONFIRMED] order_id={order_id} | "
            f"{pos.side.value if hasattr(pos.side, 'value') else pos.side} | "
            f"strike={pos.short_strike:.0f}/{pos.long_strike:.0f} | "
            f"filled={filled} @ ${avg_price:.2f} | db_id={db_id}"
        )

        # TASK-2026-179: Telegram fires ONLY after confirmed fill (via polling)
        side_val = decision.side
        notify_entry(
            side=side_val,
            short_strike=pos.short_strike,
            long_strike=pos.long_strike,
            credit=abs(avg_price),
            num_contracts=int(filled),
            spx=spx,
            entry_em=em,
            fill_price=avg_price,
            is_live=True,
        )

    def _on_close_confirmed(
        self,
        order_id: int,
        params,
        avg_price: float,
        filled: float,
    ) -> None:
        """
        Called by _poll_pending_orders() when close order is confirmed filled.
        Calls store.close_position(), sends Telegram notification.

        TASK-2026-179: Previously called via IBKR callback.
        Now triggered by polling every tick — no callback reliance.
        """
        if order_id not in self._pending_exits:
            self.logger.warning(
                f"_on_close_confirmed: no pending exit for order_id={order_id}"
            )
            return

        pos_db_id, ts, decision, em, gex_val, spx, exit_layer = \
            self._pending_exits.pop(order_id)
        self._pending_exit_times.pop(order_id, None)

        fill_ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-04:00")

        # Compute actual P&L using original credit and actual close debit
        # P&L = (original_credit * 100 * contracts) - (close_debit * 100 * contracts)
        # Original credit was collected at entry; close debit is what we paid to close
        original_credit = decision.credit  # credit received when spread was opened
        close_debit = abs(avg_price)  # what we paid to buy back the spread
        contracts = int(filled)

        from position_store import TradePosition, PositionSide
        pos_for_pnl = None
        for p in self.store._positions:
            if p.db_id == pos_db_id:
                pos_for_pnl = p
                break

        if pos_for_pnl is not None:
            pnl = (original_credit - close_debit) * 100 * contracts
        else:
            pnl = 0.0

        # Close position in DB (uses close_position which handles exit snapshot)
        self.store.close_position(
            pos_db_id,
            status="closed",
            pnl=pnl,
            notes=decision.reason,
            exit_layer=exit_layer,
            exit_conditions_met=decision.exit_conditions_met,
            em=em,
            gex_val=gex_val,
            exit_regime=decision.exit_regime,
            gex_snapshot=None,
            spx=spx,
            fill_price=avg_price,
            fill_time=fill_ts,
        )

        self.logger.info(
            f"{ts} ET [CLOSED_CONFIRMED] order_id={order_id} | "
            f"pos_db_id={pos_db_id} | filled={filled} @ ${avg_price:.2f} | P&L=${pnl:.2f}"
        )

        # TASK-2026-179: Telegram fires ONLY after confirmed fill (via polling)
        side_val = params.contract_type if hasattr(params, 'contract_type') else decision.side
        notify_exit(
            side=side_val,
            short_strike=pos_for_pnl.short_strike if pos_for_pnl else params.strike,
            long_strike=pos_for_pnl.long_strike if pos_for_pnl else params.long_strike,
            num_contracts=contracts,
            pnl=pnl,
            reason=decision.reason,
            spx=spx,
            exit_layer=exit_layer,
            is_live=True,
        )

    def _on_entry_rejected(
        self,
        order_id: int,
        reason: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        """
        Called by _poll_pending_orders() when IBKR reports order inactive/rejected.
        Rolls back pending_open row; logs to file only (no Telegram).
        """
        if order_id not in self._pending_entries:
            self.logger.warning(
                f"_on_entry_rejected: no pending entry for order_id={order_id}"
            )
            return

        pos, ts, decision, em, gex_val, spx, db_id = \
            self._pending_entries.pop(order_id)
        self._pending_entry_times.pop(order_id, None)

        # Rollback pending_open row
        self.store.rollback_position(db_id)
        self.store._positions = [p for p in self.store._positions if p.db_id != db_id]

        side_str = pos.side.value if hasattr(pos.side, 'value') else pos.side
        error_detail = ""
        if error_code or error_message:
            error_detail = f" | errorCode={error_code} | errorMessage={error_message}"
        self.logger.warning(
            f"{ts} ET [ENTRY_REJECTED] order_id={order_id} | "
            f"{side_str} | strike={pos.short_strike:.0f}/{pos.long_strike:.0f} | "
            f"reason={reason}{error_detail} | DB row rolled back"
        )

    def _on_close_rejected(
        self,
        order_id: int,
        reason: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        """
        Called by _poll_pending_orders() when close order is rejected/inactive.
        Position may have been closed manually in TWS — reconcile and log (no Telegram).
        """
        if order_id not in self._pending_exits:
            self.logger.warning(
                f"_on_close_rejected: no pending exit for order_id={order_id}"
            )
            return

        pos_db_id, ts, decision, em, gex_val, spx, exit_layer = \
            self._pending_exits.pop(order_id)
        self._pending_exit_times.pop(order_id, None)

        error_detail = ""
        if error_code or error_message:
            error_detail = f" | errorCode={error_code} | errorMessage={error_message}"
        self.logger.warning(
            f"{ts} ET [CLOSE_REJECTED] order_id={order_id} | "
            f"pos_db_id={pos_db_id} | reason={reason}{error_detail} | "
            f"Position may have been closed manually in TWS"
        )

        # Reconcile with IBKR to find if position is actually closed
        try:
            self.client.reconcile(self.store)
        except Exception as e:
            self.logger.warning(f"Reconcile failed after close rejection: {e}")

    def _on_order_cancelled(self, order_id: int, reason: str = "") -> None:
        """
        Handle order cancellation (not a fill — order was cancelled by user or IBKR).
        Entry: rollback pending_open row.
        Exit: position stays open (user may have manually closed in TWS).
        """
        # Try entry first, then exit
        if order_id in self._pending_entries:
            pos, ts, decision, em, gex_val, spx, db_id = \
                self._pending_entries.pop(order_id)
            self._pending_entry_times.pop(order_id, None)

            self.store.rollback_position(db_id)
            self.store._positions = [p for p in self.store._positions if p.db_id != db_id]

            side_str = pos.side.value if hasattr(pos.side, 'value') else pos.side
            self.logger.info(
                f"{ts} ET [ORDER_CANCELLED] order_id={order_id} | "
                f"ENTRY | {side_str} | DB row rolled back"
            )
        elif order_id in self._pending_exits:
            pos_db_id, ts, decision, em, gex_val, spx, exit_layer = \
                self._pending_exits.pop(order_id)
            self._pending_exit_times.pop(order_id, None)

            self.logger.info(
                f"{ts} ET [ORDER_CANCELLED] order_id={order_id} | "
                f"EXIT | pos_db_id={pos_db_id} | Position may be closed in TWS"
            )

    # -------------------------------------------------------------------------
    # Structured log helpers
    # -------------------------------------------------------------------------

    def _log_tick_heartbeat(
        self,
        ts: str,
        spx: float,
        em: float,
        gex_val: float,
        regime: str,
        rsi: float,
        gex_regime: str,
    ):
        self.logger.info(
            f"{ts} ET [TICK] SPX={spx:.2f} | EM={em:.2f} | "
            f"GEX={gex_val:.0f} | regime={regime} | RSI={rsi:.1f} | GEX_regime={gex_regime}"
        )

    def _log_skip_if_changed(self, ts: str, side: str, decision):
        if not decision.filter_result:
            return
        fr = decision.filter_result
        current_reason = fr.first_failure_reason

        if current_reason == self._last_skip_reason[side]:
            return

        self._last_skip_reason[side] = current_reason

        self.logger.info(
            f"{ts} ET [SKIP] {side} | reason={current_reason} | "
            f"filters_passed={','.join(fr.filters_passed)} | "
            f"filters_failed={','.join(fr.filters_failed)}"
        )

    def _log_rsi_gate_skip(
        self,
        ts: str,
        side: str,
        rsi: float,
        upper_threshold: float,
        lower_threshold: float,
    ):
        # Determine current gate bucket so we only log on state change, not every tick
        if rsi > upper_threshold:
            bucket = "above"
        elif rsi < lower_threshold:
            bucket = "below"
        else:
            bucket = "neutral"

        if self._last_rsi_gate_skipped.get(side) == bucket:
            return
        self._last_rsi_gate_skipped[side] = bucket
        self.logger.info(
            f"{ts} ET [SKIP] {side} | reason=rsi_gate | "
            f"RSI={rsi:.1f} | upper={upper_threshold:.1f} | lower={lower_threshold:.1f}"
        )

    def _log_entry(self, ts: str, decision, spx: float):
        self.logger.info(
            f"{ts} ET [ENTRY] {decision.side} | "
            f"strike={decision.short_strike:.0f}/{decision.long_strike:.0f} | "
            f"credit=${decision.credit:.2f} | layer={decision.layer} | SPX={spx:.2f}"
        )

    def _log_opened(self, ts: str, decision, spx: float, em: float, is_pending: bool = False):
        pending_str = " (pending — awaiting IBKR fill)" if is_pending else ""
        self.logger.info(
            f"{ts} ET [OPENED] {decision.side} | "
            f"strike={decision.short_strike:.0f}/{decision.long_strike:.0f} | "
            f"credit=${decision.credit:.2f} | layer={decision.layer} | "
            f"SPX={spx:.2f} | DRY_RUN={self.dry_run}{pending_str}"
        )
        if not is_pending:
            notify_entry(
                side=decision.side,
                short_strike=decision.short_strike,
                long_strike=decision.long_strike,
                credit=decision.credit,
                num_contracts=CONFIG.get('entry', {}).get('contracts_per_trade', 1),
                spx=spx,
                entry_em=em,
            )

    def _log_exit_check(self, ts: str, pos, decision, spx: float):
        self.logger.info(
            f"{ts} ET [EXIT CHECK] pos_id={pos.db_id} | "
            f"{pos.side.value if hasattr(pos.side, 'value') else pos.side} | "
            f"short={pos.short_strike} | PnL=unknown | "
            f"reason={decision.reason} | SPX={spx:.2f}"
        )

    def _log_exited(self, ts: str, pos, pnl: float, reason: str, spx: float, exit_layer: int = 1):
        self.logger.info(
            f"{ts} ET [EXITED] pos_id={pos.db_id} | "
            f"{pos.side.value if hasattr(pos.side, 'value') else pos.side} | "
            f"short={pos.short_strike} | PnL=${pnl:.2f} | "
            f"reason={reason} | SPX={spx:.2f} | DRY_RUN={self.dry_run}"
        )
        side_val = pos.side.value if hasattr(pos.side, 'value') else pos.side
        notify_exit(
            side=side_val,
            short_strike=pos.short_strike,
            long_strike=pos.long_strike,
            num_contracts=pos.num_contracts if hasattr(pos, 'num_contracts') else 1,
            pnl=pnl,
            reason=reason,
            spx=spx,
            exit_layer=exit_layer,
        )

    def _log_stale(self, ts: str, source: str):
        self.logger.warning(f"{ts} ET [STALE] {source} — skipping entry for this tick")

    # -------------------------------------------------------------------------
    # TickProcessor callbacks
    # -------------------------------------------------------------------------

    def _on_enter_approved(
        self,
        combined,
        decision,
        store,
        ts: str,
        spx: float,
        em: float,
        gex_val: float,
    ) -> None:
        """
        Called by TickProcessor when entry is approved.

        DRY_RUN: simulate fill immediately — no IBKR API call, no pending state.
        LIVE: check cash BEFORE writing pending_open to DB or sending order to IBKR.

        TASK-2026-185: Cash check in LIVE mode — reject silently if insufficient cash.
          - estimate_required_margin() called to compute required buying power
          - get_available_cash() checked BEFORE any DB write or order send
          - Insufficient cash: log only, NO Telegram, NO DB write
        """
        from position_store import TradePosition, PositionSide

        fr = decision.filter_result

        self._log_entry(ts, decision, spx)

        num_contracts = CONFIG['entry']['contracts_per_trade']

        # DRY_RUN: always pass — no cash check needed (returns 999999.0)
        if self.dry_run:
            # Record signal first
            self._record_signal(
                ts=ts, layer=decision.layer,
                signalled=1,
                signal_reason=decision.reason,
                premium_passed=1 if (fr and fr.premium_passed) else 0,
                distance_passed=1 if (fr and fr.distance_passed_any()) else 0,
                collision_passed=1 if (fr and fr.overlap_passed) else 0,
                spx_spot=spx, em=em, gex_val=gex_val,
                action="entry",
                short_strike=decision.short_strike,
                long_strike=decision.long_strike,
                credit=decision.credit,
                vix=combined.vix,
                rsi=combined.rsi,
            )

            pos = TradePosition(
                task_id=self._TASK_ID,
                ticker="SPX",
                side=PositionSide(decision.side),
                short_strike=decision.short_strike,
                long_strike=decision.long_strike,
                credit=decision.credit,
                layer=decision.layer,
                num_contracts=num_contracts,
            )
            store._positions.append(pos)
            # In backtest we set order_time from the backtest clock so the EOD
            # expiry sweep (which filters on date(order_time)) fires for these
            # positions and assigns full-credit P&L on worthless 0DTE expiry —
            # matching LIVE behavior. Live DRY_RUN keeps order_time=None (unchanged).
            bt_order_time = (
                self._now().strftime("%Y-%m-%dT%H:%M:%S-04:00")
                if self.mode == "backtest" else None
            )
            db_id = store.add_position(
                pos,
                em=em,
                gex_val=gex_val,
                entry_regime=None,
                entry_gex_regime=None,
                entry_zero_gamma_dist=None,
                gex_snapshot=None,
                spx=spx,
                order_time=bt_order_time,
            )
            self._log_opened(ts, decision, spx, em)
            self.logger.info(
                f"{ts} ET [DRY_RUN_ENTRY] {decision.side} | "
                f"strike={decision.short_strike:.0f}/{decision.long_strike:.0f} | "
                f"credit=${decision.credit:.2f} | SPX={spx:.2f} | "
                f"No IBKR API call made — position written to DB as OPEN"
            )
            return

        # -----------------------------------------------------------------
        # LIVE mode: TASK-2026-185 — cash check BEFORE any DB write or order
        # TASK-2026-070: USE_MARGIN_LIMIT uses NetLiquidation instead of
        # AvailableFunds so trades are checked against the margin line rather
        # than available cash (Sarthak's request to avoid false rejections).
        # -----------------------------------------------------------------
        use_margin_limit = CONFIG["ibkr"].get("use_margin_limit", False)
        if use_margin_limit:
            available_cash = self.client.get_margin_limit()
            limit_label = "margin_limit"
        else:
            available_cash = self.client.get_available_cash()
            limit_label = "available_cash"

        required_margin = estimate_required_margin(
            side=decision.side,
            short_strike=decision.short_strike,
            long_strike=decision.long_strike,
            num_contracts=num_contracts,
            credit=decision.credit,
        )

        if available_cash < required_margin:
            # Reject silently — NO Telegram, NO DB write, log only
            self.logger.warning(
                f"{ts} ET [ENTRY_REJECTED] {decision.side} | "
                f"strike={decision.short_strike:.0f}/{decision.long_strike:.0f} | "
                f"reason=insufficient_cash | "
                f"{limit_label}=${available_cash:.2f} required=${required_margin:.2f} | "
                f"No Telegram notification per Sarthak's instruction"
            )
            # Record signal with cash details — signalled=0, blocked_reason
            self._record_signal(
                ts=ts, layer=decision.layer,
                signalled=0,
                signal_reason=decision.reason,
                premium_passed=1 if (fr and fr.premium_passed) else 0,
                distance_passed=1 if (fr and fr.distance_passed_any()) else 0,
                collision_passed=1 if (fr and fr.overlap_passed) else 0,
                spx_spot=spx, em=em, gex_val=gex_val,
                action="entry",
                blocked_reason="insufficient_cash",
                short_strike=decision.short_strike,
                long_strike=decision.long_strike,
                credit=decision.credit,
                vix=combined.vix,
                rsi=combined.rsi,
            )
            return

        # Cash sufficient — write pending_open to DB BEFORE sending order to IBKR
        pos = TradePosition(
            task_id=self._TASK_ID,
            ticker="SPX",
            side=PositionSide(decision.side),
            short_strike=decision.short_strike,
            long_strike=decision.long_strike,
            credit=decision.credit,
            layer=decision.layer,
            num_contracts=num_contracts,
        )
        order_time = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-04:00")
        store._positions.append(pos)  # track in-memory for overlap check

        db_id = store.add_position(
            pos,
            em=em,
            gex_val=gex_val,
            entry_regime=None,
            entry_gex_regime=None,
            entry_zero_gamma_dist=None,
            gex_snapshot=None,
            spx=spx,
            status="pending_open",
            order_action="OPEN",
            order_time=order_time,
        )

        # Record signal before sending order
        self._record_signal(
            ts=ts, layer=decision.layer,
            signalled=1,
            signal_reason=decision.reason,
            premium_passed=1 if (fr and fr.premium_passed) else 0,
            distance_passed=1 if (fr and fr.distance_passed_any()) else 0,
            collision_passed=1 if (fr and fr.overlap_passed) else 0,
            spx_spot=spx, em=em, gex_val=gex_val,
            action="entry",
            short_strike=decision.short_strike,
            long_strike=decision.long_strike,
            credit=decision.credit,
            vix=combined.vix,
            rsi=combined.rsi,
        )

        # Send order to IBKR
        from executor import execute_entry
        result = execute_entry(
            client=self.client,
            ticker="SPX",
            side=decision.side,
            short_strike=decision.short_strike,
            long_strike=decision.long_strike,
            credit=decision.credit,
            num_contracts=num_contracts,
        )

        if result.status == "rejected":
            self.logger.warning(
                f"{ts} ET [ENTRY_REJECTED] {decision.side} | "
                f"strike={decision.short_strike:.0f}/{decision.long_strike:.0f} | "
                f"reason={result.message} | SPX={spx:.2f}"
            )
            # Rollback the pending_open row
            store.rollback_position(db_id)
            store._positions = [p for p in store._positions if p.db_id != db_id]
            notify_rejection(
                msg_type="entry",
                side=decision.side,
                short_strike=decision.short_strike,
                long_strike=decision.long_strike,
                reason=result.message,
            )
            return

        if result.filled or result.status == "dry_run":
            # Edge case: order filled synchronously before place_order returned
            # (should not happen in LIVE but handle it gracefully)
            from trades_db import get_conn, update_position_status
            with get_conn() as conn:
                update_position_status(conn, db_id, status="open")
                conn.commit()
            self._log_opened(ts, decision, spx, em)
            return

        # Real mode (status=pending): store pending entry keyed by order_id
        # Includes db_id for DB update when fill is confirmed via polling
        self._pending_entries[result.order_id] = (
            pos, ts, decision, em, gex_val, spx, db_id,
        )
        self._pending_entry_times[result.order_id] = time.time()

        # Log as pending (not yet confirmed)
        self._log_opened(ts, decision, spx, em, is_pending=True)
        self.logger.info(
            f"{ts} ET [ENTRY_PENDING] {decision.side} | "
            f"order_id={result.order_id} | db_id={db_id} | "
            f"strike={decision.short_strike:.0f}/{decision.long_strike:.0f} | "
            f"available_cash=${available_cash:.2f} | required_margin=${required_margin:.2f} | "
            f"pending_open written to DB — awaiting IBKR fill confirmation via polling"
        )

    def _on_skip(
        self,
        ts: str,
        target_side: str,
        decision,
        combined,
        spx: float,
        em: float,
        gex_val: float,
        rsi: float,
        regime: str,
    ) -> None:
        fr = decision.filter_result

        premium_ok = fr and fr.premium_passed
        dist_ok    = fr and fr.distance_passed_any()

        if premium_ok or dist_ok:
            blocked = fr.first_failure_reason if fr else "unknown"
            self._record_signal(
                ts=ts, layer=1,
                signalled=0,
                signal_reason=target_side,
                premium_passed=1 if premium_ok else 0,
                distance_passed=1 if dist_ok else 0,
                collision_passed=1 if (fr and fr.overlap_passed) else 0,
                blocked_reason=blocked,
                spx_spot=spx, em=em, gex_val=gex_val,
                action="entry",
                short_strike=decision.short_strike if hasattr(decision, "short_strike") else None,
                long_strike=decision.long_strike if hasattr(decision, "long_strike") else None,
                credit=decision.credit if hasattr(decision, "credit") else None,
                vix=combined.vix,
                rsi=rsi,
            )

        self._log_skip_if_changed(ts, target_side, decision)

    def _on_exit_checked(
        self,
        ts: str,
        pos,
        decision,
        combined,
        store,
        spx: float,
        em: float,
        gex_val: float,
    ) -> None:
        """
        Called by TickProcessor for every open position every tick.

        DRY_RUN: close immediately — no IBKR API call, no pending state.
        LIVE: send close order, track in _pending_exits. Only call
        store.close_position() when _on_close_confirmed fires via polling.
        """
        from executor import execute_exit

        self._log_exit_check(ts, pos, decision, spx)

        if decision.should_exit:
            pnl = 0.0

            # DRY_RUN: unchanged
            if self.dry_run:
                self._log_exited(ts, pos, pnl, decision.reason, spx, exit_layer=decision.exit_layer)
                store.close_position(
                    pos.db_id,
                    status="closed",
                    notes=decision.reason,
                    exit_layer=decision.exit_layer,
                    exit_conditions_met=decision.exit_conditions_met,
                    em=em,
                    gex_val=gex_val,
                    exit_regime=decision.exit_regime,
                    gex_snapshot=None,
                    spx=spx,
                )
                self._record_signal(
                    ts=ts, layer=decision.exit_layer,
                    signalled=1,
                    signal_reason=decision.reason,
                    premium_passed=0,
                    distance_passed=0,
                    collision_passed=0,
                    spx_spot=spx, em=em, gex_val=gex_val,
                    action="exit",
                    short_strike=pos.short_strike,
                    long_strike=pos.long_strike,
                    vix=combined.vix,
                    rsi=combined.rsi,
                )
                self.logger.info(
                    f"{ts} ET [DRY_RUN_EXIT] pos_id={pos.db_id} | "
                    f"reason={decision.reason} | SPX={spx:.2f} | "
                    f"No IBKR close order sent — position closed in DB"
                )
                return

            # LIVE mode: send close order, track pending
            result = execute_exit(
                client=self.client,
                ticker=pos.ticker,
                side=pos.side.value,
                short_strike=pos.short_strike,
                long_strike=pos.long_strike,
            )

            if result.status == "dry_run":
                self._log_exited(ts, pos, pnl, decision.reason, spx, exit_layer=decision.exit_layer)
                store.close_position(
                    pos.db_id,
                    status="closed",
                    notes=decision.reason,
                    exit_layer=decision.exit_layer,
                    exit_conditions_met=decision.exit_conditions_met,
                    em=em,
                    gex_val=gex_val,
                    exit_regime=decision.exit_regime,
                    gex_snapshot=None,
                    spx=spx,
                )
                self._record_signal(
                    ts=ts, layer=decision.exit_layer,
                    signalled=1,
                    signal_reason=decision.reason,
                    premium_passed=0,
                    distance_passed=0,
                    collision_passed=0,
                    spx_spot=spx, em=em, gex_val=gex_val,
                    action="exit",
                    short_strike=pos.short_strike,
                    long_strike=pos.long_strike,
                    vix=combined.vix,
                    rsi=combined.rsi,
                )
            elif result.filled:
                # Synchronous fill
                self._log_exited(ts, pos, pnl, decision.reason, spx, exit_layer=decision.exit_layer)
                store.close_position(
                    pos.db_id,
                    status="closed",
                    notes=decision.reason,
                    exit_layer=decision.exit_layer,
                    exit_conditions_met=decision.exit_conditions_met,
                    em=em,
                    gex_val=gex_val,
                    exit_regime=decision.exit_regime,
                    gex_snapshot=None,
                    spx=spx,
                )
                self._record_signal(
                    ts=ts, layer=decision.exit_layer,
                    signalled=1,
                    signal_reason=decision.reason,
                    premium_passed=0,
                    distance_passed=0,
                    collision_passed=0,
                    spx_spot=spx, em=em, gex_val=gex_val,
                    action="exit",
                    short_strike=pos.short_strike,
                    long_strike=pos.long_strike,
                    vix=combined.vix,
                    rsi=combined.rsi,
                )
            else:
                # Real mode (pending): store pending exit keyed by order_id
                self._pending_exits[result.order_id] = (
                    pos.db_id, ts, decision, em, gex_val, spx, decision.exit_layer,
                )
                self._pending_exit_times[result.order_id] = time.time()
                self.logger.info(
                    f"{ts} ET [EXIT_PENDING] pos_id={pos.db_id} | "
                    f"order_id={result.order_id} | reason={decision.reason} | "
                    f"pending close — awaiting IBKR fill confirmation via polling"
                )

    def _on_heartbeat(
        self,
        ts: str,
        spx: float,
        em: float,
        gex_val: float,
        regime: str,
        rsi: float,
        gex_regime: str,
    ) -> None:
        self._log_tick_heartbeat(ts, spx, em, gex_val, regime, rsi, gex_regime)

    # -------------------------------------------------------------------------
    # EOD expiry sweep — closes positions that expired at 4 PM ET
    # -------------------------------------------------------------------------

    def _close_expired_positions(self, trade_date: str, spx: float, em: float, gex_val: float, vix: float, rsi: float, _db_path=None) -> None:
        """
        Called once per trading day after 16:00 ET.

        Reaching expiry means neither L1 nor L2 triggered — the spread expired
        worthless, which is 100% profit (full credit collected, debit = 0).
        """
        from trades_db import get_conn, get_open_positions_for_date, insert_signal

        # Use the engine clock so backtest stamps the close at the backtest
        # date's 16:00 (not wall-clock). Live: self._now() == datetime.now().
        close_ts = self._now().strftime("%Y-%m-%dT16:00:00-0400")
        _conn_kwargs = {"path": _db_path} if _db_path else {}

        try:
            with get_conn(**_conn_kwargs) as conn:
                positions = get_open_positions_for_date(conn, trade_date)
                if not positions:
                    return

                for pos in positions:
                    # Spread expired worthless → 100% profit = full credit collected
                    pnl = round((pos.credit or 0) * 100 * (pos.num_contracts or 1), 2)

                    conn.execute(
                        """
                        UPDATE positions
                        SET status = 'expired',
                            close_time = ?,
                            debit = 0.0,
                            pnl = ?,
                            max_profit = COALESCE(max_profit, ?),
                            notes = COALESCE(notes || ' | ', '') || 'expired worthless — 100% max profit'
                        WHERE id = ?
                        """,
                        (close_ts, pnl, pnl, pos.id),
                    )

                    # Exit snapshot — use current tick values as best available proxy
                    conn.execute(
                        """
                        UPDATE positions
                        SET exit_spx_spot = ?,
                            exit_vix = ?,
                            exit_em = ?,
                            exit_layer = 0,
                            exit_conditions_met = 1,
                            exit_regime = 'expired'
                        WHERE id = ?
                        """,
                        (spx, vix, em, pos.id),
                    )

                    # Insert EXPIRE signal
                    insert_signal(
                        conn,
                        timestamp=close_ts,
                        layer=pos.layer or 0,
                        spx_spot=spx,
                        signalled=1,
                        signal_reason="eod_expiry",
                        premium_passed=1,
                        distance_passed=1,
                        collision_passed=1,
                        filled=1,
                        short_strike=pos.short_strike,
                        long_strike=pos.long_strike,
                        credit=pos.credit,
                        action="EXPIRE",
                        em=em,
                        gex=gex_val,
                        vix=vix,
                        rsi=rsi,
                        task_id=self._TASK_ID,
                    )

                    self.logger.info(
                        f"{close_ts} ET [EOD_EXPIRE] pos_id={pos.id} | "
                        f"{pos.side} {pos.short_strike}/{pos.long_strike} | "
                        f"pnl=${pnl:.2f} (100% max profit — expired worthless) | status=expired"
                    )

                conn.commit()

            # Reload store so in-memory state stays consistent
            self.store.load_open()

        except Exception as exc:
            self.logger.error(f"EOD expiry sweep failed: {exc}", exc_info=True)

    def _record_signal(
        self,
        ts: str,
        layer: int,
        signalled: int,
        signal_reason: Optional[str],
        premium_passed: int,
        distance_passed: int,
        collision_passed: int,
        spx_spot: float,
        em: float,
        gex_val: float,
        action: str,
        blocked_reason: Optional[str] = None,
        short_strike: Optional[float] = None,
        long_strike: Optional[float] = None,
        credit: Optional[float] = None,
        vix: Optional[float] = None,
        rsi: Optional[float] = None,
    ):
        from trades_db import get_conn, insert_signal
        try:
            with get_conn() as conn:
                insert_signal(
                    conn,
                    timestamp=ts,
                    layer=layer,
                    spx_spot=spx_spot,
                    signalled=signalled,
                    signal_reason=signal_reason,
                    premium_passed=premium_passed,
                    distance_passed=distance_passed,
                    collision_passed=collision_passed,
                    blocked_reason=blocked_reason,
                    action=action,
                    em=em,
                    gex=gex_val,
                    vix=vix,
                    rsi=rsi,
                    short_strike=short_strike,
                    long_strike=long_strike,
                    credit=credit,
                    task_id=self._TASK_ID,
                )
                conn.commit()
        except Exception as e:
            self.logger.warning(f"Failed to record signal: {e}")

    # -------------------------------------------------------------------------
    # Core tick
    # -------------------------------------------------------------------------

    def tick(self) -> Optional[str]:
        from combined_reader import StaleDataError

        try:
            combined = self._fetch_combined()
        except StaleDataError as e:
            ts = "unknown"
            from scanner_reader import get_latest_scan
            scan = get_latest_scan()
            if scan:
                ts = scan.timestamp_est
            self._log_stale(ts, str(e))
            self._run_exit_checks_on_stale()
            return None

        ts            = combined.scan_timestamp
        spx           = combined.spx_spot
        em            = combined.expected_move
        gex_val       = combined.gex_by_oi
        regime        = combined.regime or "neutral"
        rsi           = combined.rsi
        gex_regime    = "dealer_long" if gex_val >= 0 else "dealer_short"
        vix           = combined.vix  # VIX for adaptive RSI gate lookup

        # TASK-2026-179: Poll pending orders every tick (primary sync mechanism)
        self._poll_pending_orders()

        # TASK-2026-179: Check for stale pending orders (10-min timeout)
        self._check_pending_timeouts()

        # EOD expiry sweep — once per day after 16:00 ET.
        # Uses self._now() so backtest fires the sweep at the backtest date's
        # 16:00 (not wall-clock), and writes to the engine's own store DB so
        # backtest expiries land in the isolated backtest DB.
        now_et = self._now()
        trade_date = now_et.strftime("%Y-%m-%d")
        if now_et.hour >= 16 and trade_date not in self._eod_expiry_done:
            self._eod_expiry_done.add(trade_date)
            self._close_expired_positions(
                trade_date=trade_date,
                spx=spx, em=em, gex_val=gex_val,
                vix=vix, rsi=rsi,
                _db_path=self.store.db_path,
            )

        # Rolling 30-min volatility gate — blocks new entries, exits always run
        prev_blocked = self._day_gate._was_blocked
        gate = self._day_gate.update(combined)
        if gate.blocked != prev_blocked:
            notify_day_gate(
                blocked=gate.blocked,
                ts=ts,
                avg_gex=gate.avg_gex,
                avg_dist=gate.avg_dist,
                avg_rsi=gate.avg_rsi,
                signal_1=gate.signal_1,
                signal_2=gate.signal_2,
                signal_3=gate.signal_3,
                n_samples=gate.n_samples,
            )
        if gate.blocked:
            self.logger.info(
                f"{ts} ET [DAY_GATE ACTIVE] entries suppressed | "
                f"avg_gex={gate.avg_gex:.1f} avg_dist={gate.avg_dist:.1f} avg_rsi={gate.avg_rsi:.1f} | "
                f"s1={'Y' if gate.signal_1 else 'N'} s2={'Y' if gate.signal_2 else 'N'} s3={'Y' if gate.signal_3 else 'N'} | "
                f"n={gate.n_samples}"
            )
            # Exits on open positions always run even when gate is blocking entries
            from risk_manager import is_market_open
            if is_market_open():
                self._processor._run_exit_checks(
                    combined=combined,
                    ts=ts, spx=spx, em=em,
                    gex_val=gex_val, regime=regime, rsi=rsi,
                    store=self.store,
                )
                if self._processor._on_heartbeat:
                    self._processor._on_heartbeat(
                        ts, spx, em, gex_val, regime, rsi, gex_regime
                    )
            return None

        self._processor.process_tick(
            combined=combined,
            ts=ts,
            spx=spx,
            em=em,
            gex_val=gex_val,
            regime=regime,
            rsi=rsi,
            gex_regime=gex_regime,
            store=self.store,
            open_strikes=self.store.get_open_strikes(),
            vix=vix,
        )
        return None

    def _run_exit_checks_on_stale(self):
        from combined_reader import get_combined_for_latest_scan, StaleDataError

        # Still poll pending orders on stale tick (important for pending order state)
        self._poll_pending_orders()
        self._check_pending_timeouts()

        try:
            combined = get_combined_for_latest_scan()
        except StaleDataError:
            return

        ts      = combined.scan_timestamp
        spx     = combined.spx_spot
        em      = combined.expected_move
        gex_val = combined.gex_by_oi
        regime  = combined.regime or "neutral"
        rsi     = combined.rsi
        gex_regime = "dealer_long" if gex_val >= 0 else "dealer_short"
        vix     = combined.vix  # VIX for adaptive RSI gate lookup

        self._processor.process_tick(
            combined=combined,
            ts=ts,
            spx=spx,
            em=em,
            gex_val=gex_val,
            regime=regime,
            rsi=rsi,
            gex_regime=gex_regime,
            store=self.store,
            open_strikes=self.store.get_open_strikes(),
            vix=vix,
        )

    # -------------------------------------------------------------------------
    # Backtest loop — replays a single historical date through the SAME tick()
    # -------------------------------------------------------------------------

    def run_backtest(self, date_str: str) -> int:
        """
        Replay every scan timestamp on `date_str` through the real engine tick().

        Runs in DRY_RUN fill mode (fills at scan mid = decision.credit), writes
        to this engine's isolated store DB, and uses the backtest clock so the
        EOD expiry sweep fires at the historical 16:00. Returns the number of
        ticks replayed.
        """
        from combined_reader import iter_combined_for_date
        import risk_manager

        self.logger.info("=" * 60)
        self.logger.info(f"BACKTEST START — date={date_str}")
        self.logger.info(f"Fills: DRY_RUN @ scan mid (decision.credit)")
        self.logger.info(f"State DB: {self.store.db_path}")
        self.logger.info("=" * 60)

        # Make market-hours / timestamp gating in the shared decision logic use
        # the backtest clock instead of wall-clock. self._clock is updated each
        # tick below, and this lambda reads it lazily.
        risk_manager.set_clock_override(lambda: self._clock)
        try:
            n = 0
            for combined in iter_combined_for_date(date_str):
                self._current_combined = combined
                self._clock = self._parse_clock(combined.scan_timestamp)
                try:
                    self.tick()
                except Exception as e:
                    self.logger.exception(
                        f"Backtest tick failed at {combined.scan_timestamp}: {e}"
                    )
                n += 1
        finally:
            risk_manager.set_clock_override(None)

        self.logger.info(f"BACKTEST COMPLETE — {n} ticks replayed for {date_str}")
        return n

    # -------------------------------------------------------------------------
    # Run loop
    # -------------------------------------------------------------------------

    def run(self):
        from datetime import datetime as dt

        # VIX-adaptive RSI gates: pass None at startup (no combined data yet).
        # Actual per-tick gates come from _get_rsi_gates(combined.vix) inside
        # TickProcessor.process_tick() using the live VIX from scanner data.
        rsi_upper, rsi_lower = _get_rsi_gates(None)
        self.logger.info("=" * 60)
        self.logger.info("SPX 0DTE Auto-Trader Engine STARTING")
        self.logger.info(f"DRY_RUN={self.dry_run}  "
                         f"check_interval={self.check_interval}s")
        self.logger.info(f"Entry window: {CONFIG['market']['entry_start']}–"
                         f"{CONFIG['market']['entry_end']} ET")
        self.logger.info(f"RSI Gate ENABLED (VIX-adaptive per bucket): upper={rsi_upper} lower={rsi_lower} | "
                         f"RSI>{rsi_upper}→CALL, RSI<{rsi_lower}→PUT, in-band→skip")
        self.logger.info(f"VIX buckets: 13-16→55/54, 16-20→60/40, 20-25→70/30, 25-30→75/25")
        _dg_cfg = CONFIG.get("day_gate", {})
        self.logger.info(
            f"Day Gate: enabled={_dg_cfg.get('enabled', True)} | "
            f"window={_dg_cfg.get('window_minutes', 30)}m | "
            f"gex_threshold={_dg_cfg.get('gex_by_oi_threshold', 0.0)} | "
            f"dist_threshold={_dg_cfg.get('spot_zero_gamma_threshold', -15.0)} | "
            f"rsi_band=[{_dg_cfg.get('rsi_extreme_low', 15.0)}, {_dg_cfg.get('rsi_extreme_high', 85.0)}]"
        )
        self.logger.info("No force-close — existing positions run until natural exit")
        self.logger.info("Data: Scanner-driven as-of join (scanner + GEX + TV, 10-min freshness window)")
        self.logger.info("Signal recording: entry only when meaningful condition passes; exit only when should_exit=True")
        if not self.dry_run:
            self.logger.info(
                f"TASK-2026-185: Cash check ENABLED | "
                f"estimate_required_margin() called before any LIVE entry order"
            )
        self.logger.info("=" * 60)

        try:
            if not self.dry_run:
                while not self.client.is_connected():
                    self.logger.info("Waiting for IBKR connection...")
                    time.sleep(2)
            # TASK-2026-235: outer loop resilient to transient failures (e.g. WAL contention).
            # KeyboardInterrupt is re-raised so SIGTERM/Ctrl+C still hits the outer except below.
            while True:
                try:
                    self.tick()
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    self.logger.exception(
                        f"Engine tick failed (will retry in {self.check_interval}s): {e}"
                    )
                time.sleep(self.check_interval)
        except KeyboardInterrupt:
            self.logger.info("Engine stopped by user (Ctrl+C)")
        finally:
            self._shutdown()

    def _shutdown(self):
        self.logger.info("Engine shutdown complete")
        if self._client:
            self._client.disconnect()

if __name__ == "__main__":
    engine = AutoTraderEngine()
    engine.run()