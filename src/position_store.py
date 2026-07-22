"""
position_store.py — Position class + state management + market snapshots.

TASK-2026-179: Pending-state protocol for live trading.
  - get_open_strikes() now includes 'pending_open' rows for collision checking
  - rollback_position() deletes a pending row from DB and in-memory list
  - add_position() accepts explicit status parameter
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import logging
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from config import CONFIG
from trades_db import (
    DB_PATH, init_db, open_conn,
    insert_position, update_position_status,
    update_position_exit_snapshot,
    get_open_positions, get_position_count,
    Position,
)

if TYPE_CHECKING:
    from gex_reader import GexSnapshot

_LOG = logging.getLogger("position_store")
_LOGS_DIR = Path(CONFIG["paths"]["logs"])


class PositionSide(Enum):
    CALL = "CALL"
    PUT  = "PUT"


# ---------------------------------------------------------------------------
# Market snapshot
# ---------------------------------------------------------------------------

@dataclass
class MarketSnapshot:
    spx_spot:       float
    vix:            float
    em:             float
    gex:            float
    bb_position:    float
    bb_expanding:   int
    adx:            float
    macd_hist:      float
    rsi:            float
    atm_call_mid:   float
    atm_put_mid:    float
    atm_strike:     float
    vix1d:          float = 0.0

    @property
    def bb_position_pct(self) -> float:
        return self.bb_position * 100 if self.bb_position is not None else 50.0


def _snapshot_from_combined(combined, em: float = 0.0, gex_val: float = 0.0) -> MarketSnapshot:
    """Build a MarketSnapshot from a CombinedSnapshot (the combined_reader seam).

    CombinedSnapshot is produced identically in LOCAL, CLOUD and BACKTEST, so a
    snapshot derived from it is mode-agnostic — no direct SQLite/Supabase reads.
    """
    em_v  = em if em else (getattr(combined, "expected_move", 0.0) or 0.0)
    gex_v = gex_val if gex_val else (getattr(combined, "gex_by_oi", 0.0) or 0.0)

    # Real VIX from the feed; fall back to the EM*16 proxy only if absent.
    vix_raw = getattr(combined, "vix", None)
    vix = float(vix_raw) if vix_raw else ((em_v * 16) if em_v > 0 else 0.0)
    vix1d = float(getattr(combined, "vix1d", 0.0) or 0.0)

    return MarketSnapshot(
        spx_spot     = combined.spx_spot,
        vix          = vix,
        vix1d        = vix1d,
        em           = em_v,
        gex          = gex_v,
        bb_position  = combined.bb_position,
        bb_expanding = int(bool(combined.bb_expanding)),
        adx          = combined.adx,
        macd_hist    = combined.macd_hist,
        rsi          = combined.rsi,
        atm_call_mid = combined.atm_call_mid,
        atm_put_mid  = combined.atm_put_mid,
        atm_strike   = combined.atm_strike,
    )


def build_market_snapshot(
    em: float = 0.0,
    gex_val: float = 0.0,
    combined=None,
) -> MarketSnapshot:
    """
    Capture the current market state via the combined_reader seam, so entry/exit
    snapshots are identical in LOCAL and CLOUD mode.

    If ``combined`` is provided (the exact decision snapshot), it is used directly
    — correct in every mode including BACKTEST. Otherwise the latest combined
    snapshot is fetched via the mode-aware seam (LOCAL SQLite or CLOUD Supabase).
    """
    try:
        if combined is None:
            from combined_reader import get_combined_for_latest_scan
            combined = get_combined_for_latest_scan()
        return _snapshot_from_combined(combined, em=em, gex_val=gex_val)
    except Exception as e:
        _LOG.warning("build_market_snapshot: combined read failed: %s", e)
        return _empty_snapshot()


def _empty_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        spx_spot=0.0, vix=0.0, em=0.0, gex=0.0,
        bb_position=0.5, bb_expanding=0, adx=0.0,
        macd_hist=0.0, rsi=50.0, atm_call_mid=0.0,
        atm_put_mid=0.0, atm_strike=0.0,
    )


# ---------------------------------------------------------------------------
# Trade position
# ---------------------------------------------------------------------------

@dataclass
class TradePosition:
    task_id:       str
    ticker:        str
    side:          PositionSide
    short_strike:  float
    long_strike:   Optional[float] = None
    open_time:     str = ""
    close_time:    Optional[str] = None
    credit:        float = 0.0
    debit:         Optional[float] = None
    status:        str = "open"
    pnl:           Optional[float] = None
    max_profit:    Optional[float] = None
    max_loss:      Optional[float] = None
    layer:         Optional[int] = None
    notes:         Optional[str] = None
    db_id:         Optional[int] = None
    entry_snapshot: Optional[MarketSnapshot] = None
    entry_em:      Optional[float] = None
    num_contracts: int = 1

    # Entry indicator baselines for momentum-aware L2 exit. Populated at open
    # (add_position) and on restart (load_open). Used by evaluate_exit to compare
    # current indicators against entry to detect the market turning against us.
    entry_spx_spot:  Optional[float] = None
    entry_adx:       Optional[float] = None
    entry_rsi:       Optional[float] = None
    entry_macd_hist: Optional[float] = None
    entry_vix1d:     Optional[float] = None

    def __post_init__(self):
        if not self.open_time:
            self.open_time = _now_et()

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def spread_width(self) -> float:
        if self.long_strike is not None:
            return abs(self.long_strike - self.short_strike)
        return 0.0

    @property
    def is_spread_winning(self) -> bool:
        return self.spread_width >= 3 * self.credit


def _clock_now() -> datetime:
    """Current time, honoring the backtest clock override if one is installed.

    Routes through risk_manager so position open/close timestamps reflect the
    backtest moment in backtest mode. Live/cloud → real wall clock (unchanged).
    """
    try:
        from risk_manager import _now_dt
        return _now_dt()
    except Exception:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))


def _now_et() -> str:
    return _clock_now().strftime("%H:%M:%S")


def _timestamp_et() -> str:
    # _clock_now() is ET-aware (routes through risk_manager._now_dt). isoformat()
    # emits the correct EST/EDT offset for the date (DST-aware).
    return _clock_now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Overlap detection
# ---------------------------------------------------------------------------

def any_overlap(
    new_short: float,
    new_long: Optional[float],
    open_strikes: list[tuple[float, Optional[float]]],
) -> bool:
    """
    Reject a candidate spread if it overlaps any open position.
    Strict < comparison — touching at a boundary is NOT overlap.
    """
    new_lo = min(new_short, new_long) if new_long is not None else new_short
    new_hi = max(new_short, new_long) if new_long is not None else new_short

    for ex_short, ex_long in open_strikes:
        ex_lo = min(ex_short, ex_long) if ex_long is not None else ex_short
        ex_hi = max(ex_short, ex_long) if ex_long is not None else ex_short

        if new_short == ex_short and new_long == ex_long:
            return True
        if new_short == ex_short:
            return True
        if new_long is not None and ex_long is not None and new_long == ex_long:
            return True
        if ex_lo < new_short < ex_hi:
            return True
        if new_long is not None and ex_lo < new_long < ex_hi:
            return True
        if new_lo < ex_short < new_hi:
            return True
        if ex_long is not None and new_lo < ex_long < new_hi:
            return True

    return False


def check_strike_collision(
    new_short: float,
    new_long: float,
    open_strikes: list[tuple[float, Optional[float]]],
) -> tuple[bool, str]:
    """
    Check two strike collision conditions (HARD BLOCKS) before allowing a new entry.

    Returns (can_proceed: bool, reason: str):
      - (True, "") if no collision — entry is safe
      - (False, collision_type) if any condition triggers

    Two collision conditions (both HARD BLOCKS):
      1. same_short_strike          — new_short == existing.short_strike
      2. long_closes_existing_short — new_long == existing.short_strike
         (adding this long leg would close the existing short leg)
    """
    for ex_short, ex_long in open_strikes:
        if new_short == ex_short:
            return (False, "same_short_strike")
        if new_long == ex_short:
            return (False, "long_closes_existing_short")
        if new_short == ex_long:
            return (False, "same_short_strike")

    return True, ""


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class PositionStore:
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self._positions: list[TradePosition] = []
        self._open_count = 0

    def init(self):
        init_db(self.db_path)

    def load_open(self) -> None:
        """Load all open positions from DB, including pending_open entries.

        TASK-2026-179: Must load both 'open' and 'pending_open' rows so that
        in-flight entry orders are tracked for collision checking and exit signals.
        """
        with open_conn(self.db_path) as conn:
            rows = get_open_positions(conn)
            self._positions = [
                TradePosition(
                    task_id=p.task_id, ticker=p.ticker,
                    side=PositionSide(p.side),
                    short_strike=p.short_strike, long_strike=p.long_strike,
                    open_time=p.open_time, credit=p.credit,
                    status=p.status, layer=p.layer,
                    notes=p.notes, db_id=p.id, entry_em=p.entry_em,
                    num_contracts=p.num_contracts,
                    entry_spx_spot=p.entry_spx_spot,
                    entry_adx=p.entry_adx,
                    entry_rsi=p.entry_rsi,
                    entry_macd_hist=p.entry_macd_hist,
                    entry_vix1d=p.entry_vix1d,
                )
                for p in rows
            ]
            self._open_count = len(self._positions)

            # Log loaded positions for debugging restart issues
            if self._open_count > 0:
                from log_setup import get_engine_logger
                log = get_engine_logger("position_store", _LOGS_DIR)
                log.info(f"[STARTUP] Loaded {self._open_count} open position(s) from database")
                for pos in self._positions:
                    log.info(
                        f"  - {pos.side.value} {pos.ticker} | "
                        f"{pos.short_strike}/{pos.long_strike} | "
                        f"credit=${pos.credit:.2f} | opened {pos.open_time}"
                    )

    def add_position(
        self,
        pos: TradePosition,
        em: float = 0.0,
        gex_val: float = 0.0,
        entry_regime: Optional[str] = None,
        entry_gex_regime: Optional[str] = None,
        entry_zero_gamma_dist: Optional[float] = None,
        gex_snapshot: Optional["GexSnapshot"] = None,
        spx: float = 0.0,
        status: str = "open",
        order_id: Optional[int] = None,
        order_action: Optional[str] = None,
        order_time: Optional[str] = None,
        combined=None,
    ) -> int:
        """
        Persist a new position and capture the entry market snapshot.
        Also records regime metadata.

        TASK-2026-179:
          - status='open' in DRY_RUN (unchanged behavior)
          - status='pending_open' in LIVE mode (written BEFORE IBKR confirms fill)
          - order_id/order_action/order_time recorded for LIVE pending orders
        """
        # Snapshot is built from `combined` (the exact decision tick) when given —
        # correct in LOCAL, CLOUD and BACKTEST. Falls back to the mode-aware seam.
        snapshot = build_market_snapshot(em=em, gex_val=gex_val, combined=combined)

        # Entry indicator baselines for momentum-aware L2 exit (all from snapshot).
        pos.entry_em        = snapshot.em
        pos.entry_spx_spot  = snapshot.spx_spot
        pos.entry_adx       = snapshot.adx
        pos.entry_rsi       = snapshot.rsi
        pos.entry_macd_hist = snapshot.macd_hist
        pos.entry_vix1d     = snapshot.vix1d

        with open_conn(self.db_path) as conn:
            db_row = Position(
                task_id=pos.task_id, ticker=pos.ticker,
                side=pos.side.value, short_strike=pos.short_strike,
                long_strike=pos.long_strike, open_time=pos.open_time,
                credit=pos.credit, debit=pos.debit,
                total_credit=pos.credit * 100 * pos.num_contracts,
                status=status,
                pnl=pos.pnl, max_profit=pos.max_profit,
                max_loss=pos.max_loss, layer=pos.layer, notes=pos.notes,
                num_contracts=pos.num_contracts,

                # Entry snapshot (all fields from the combined-sourced snapshot)
                entry_spx_spot     = snapshot.spx_spot,
                entry_vix          = snapshot.vix,
                entry_em           = snapshot.em,
                entry_gex          = snapshot.gex,
                entry_bb_position  = snapshot.bb_position,
                entry_bb_expanding = snapshot.bb_expanding,
                entry_adx          = snapshot.adx,
                entry_macd_hist    = snapshot.macd_hist,
                entry_rsi          = snapshot.rsi,
                entry_atm_call_mid = snapshot.atm_call_mid,
                entry_atm_put_mid  = snapshot.atm_put_mid,
                entry_atm_strike   = snapshot.atm_strike,
                entry_vix1d        = snapshot.vix1d,

                # Regime metadata
                entry_regime         = entry_regime,
                entry_gex_regime     = entry_gex_regime,
                entry_zero_gamma_dist = entry_zero_gamma_dist,

                # TASK-2026-179: IBKR order tracking
                order_id     = order_id,
                order_action = order_action,
                order_time   = order_time,
            )
            db_id = insert_position(conn, db_row)
            conn.commit()

        pos.db_id = db_id
        pos.status = status
        pos.entry_snapshot = snapshot
        self._positions.append(pos)
        self._open_count += 1
        return db_id

    def rollback_position(self, db_id: int) -> None:
        """
        Delete a pending position row from DB (TASK-2026-179).
        Called when an entry order is rejected, cancelled, or timed out.
        Also removes from in-memory list.
        """
        with open_conn(self.db_path) as conn:
            conn.execute("DELETE FROM positions WHERE id = ?", (db_id,))
            conn.commit()
        self._positions = [p for p in self._positions if p.db_id != db_id]
        self._open_count = max(0, self._open_count - 1)

    def close_position(
        self,
        db_id: int,
        status: str = "closed",
        pnl: Optional[float] = None,
        notes: Optional[str] = None,
        exit_layer: Optional[int] = None,
        exit_conditions_met: Optional[int] = None,
        em: float = 0.0,
        gex_val: float = 0.0,
        exit_regime: Optional[str] = None,
        gex_snapshot: Optional["GexSnapshot"] = None,
        spx: float = 0.0,
        fill_price: Optional[float] = None,
        fill_time: Optional[str] = None,
        combined=None,
    ) -> None:
        """
        Close a position and capture the exit market snapshot.
        Also records exit_regime and exit_rsi.

        `combined` (the exact exit-decision tick) is used for the exit snapshot
        when provided — correct in every mode. When None (e.g. a pending exit
        confirmed a few ticks later), the latest combined snapshot is fetched via
        the mode-aware seam, which is the correct close-time state in LOCAL/CLOUD.
        """
        close_ts = _timestamp_et()
        snapshot = build_market_snapshot(em=em, gex_val=gex_val, combined=combined)

        with open_conn(self.db_path) as conn:
            update_position_status(conn, db_id, status, close_ts, pnl, notes)
            update_position_exit_snapshot(
                conn, db_id,
                exit_spx_spot    = snapshot.spx_spot,
                exit_vix         = snapshot.vix,
                exit_em          = snapshot.em,
                exit_bb_position = snapshot.bb_position,
                exit_rsi         = snapshot.rsi,
                exit_adx         = snapshot.adx,
                exit_macd_hist   = snapshot.macd_hist,
                exit_layer       = exit_layer,
                exit_conditions_met = exit_conditions_met,
                exit_regime      = exit_regime,
                exit_vix1d       = snapshot.vix1d,
            )
            conn.commit()

        self._positions = [p for p in self._positions if p.db_id != db_id]
        self._open_count -= 1

    def open_count(self) -> int:
        with open_conn(self.db_path) as conn:
            return get_position_count(conn)

    def get_open(self) -> list[TradePosition]:
        """Return confirmed open positions only (status='open')."""
        return [p for p in self._positions if p.is_open]

    def get_open_strikes(self) -> list[tuple[float, Optional[float]]]:
        """
        Return all strikes (confirmed + pending) for collision checking.

        TASK-2026-179: Includes both 'open' and 'pending_open' rows so that
        in-flight entry orders block new entries at the same strike.

        DRY_RUN: Only 'open' rows exist (pending rows are never written),
        so this returns the same result as before.

        pending_close rows are excluded — they represent the same position
        being closed, not a new position.
        """
        return [
            (p.short_strike, p.long_strike)
            for p in self._positions
            if p.status in ("open", "pending_open")
        ]


if __name__ == "__main__":
    store = PositionStore()
    store.init()
    print("PositionStore ready")
