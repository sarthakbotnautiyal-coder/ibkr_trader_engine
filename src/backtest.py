"""
backtest.py — Replay historical scanner data through the live entry/exit engine.

Usage:
    python3 -m src.backtest --date 2026-05-15
    python3 -m src.backtest --date 2026-05-15 --verbose    # every tick
    python3 -m src.backtest --date 2026-05-15 --summary-only
    python3 -m src.backtest --date 2026-05-15 --run-id 2026-05-15_custom  # override run ID

DRY_RUN=True enforced — no real trades.

Reuses evaluate_entry() and evaluate_exit() from risk_manager.py (no modifications).
Uses combined_reader.get_combined_snapshot() for timestamp-aligned reads, same as live engine.

Results are written to data/backtest.db (shadow tables):
  - backtest_signals   — every signal (entry/skip/exit_check/exited/expired)
  - backtest_positions — open/close/expired records per backtest run

0DTE Expiry: At end of run (last scan row processed), any still-open positions
are marked as status='expired' with exit_reason='expired_0dte'.  Full credit is
kept — expired options are worthless so total_pnl=0 (credit already collected).

TickProcessor: all entry/exit evaluation is delegated to TickProcessor.process_tick(),
identical to the live engine. Only the snapshot source and signal-recording callbacks differ.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths (same as live engine)
# ---------------------------------------------------------------------------

_REPO_DIR = Path(__file__).parent.parent          # ibkr_auto_trader/
_SCAN_DB  = _REPO_DIR / "data" / "scanner.db"
_TV_DB    = Path(
    "/Users/ubexbot/.openclaw/workspace-venkat/"
    "tradingview_signal_generator/data/tradingview.db"
)

_SRC_DIR = _REPO_DIR / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# ---------------------------------------------------------------------------
# Monkey-patch is_market_open in risk_manager before importing evaluate_entry.
#
# evaluate_entry() calls is_market_open() at its entry point.  During backtest
# replay the wall clock is irrelevant — the scan timestamp determines whether
# we are inside the entry window.  We patch is_market_open to always return
# True so that evaluate_entry proceeds to run its filter logic.  The caller
# (_is_market_open_at_ts) enforces the per-tick window check separately.
# ---------------------------------------------------------------------------

import risk_manager
_original_is_market_open = risk_manager.is_market_open


def _patched_is_market_open() -> bool:
    """Always return True in backtest context — caller enforces entry window."""
    return True


risk_manager.is_market_open = _patched_is_market_open

# ---------------------------------------------------------------------------
# Now import the rest of the live engine components (patch already applied)
# ---------------------------------------------------------------------------

from combined_reader import (
    get_combined_snapshot,
    StaleDataError,
    CombinedSnapshot,
)
from risk_manager import (
    evaluate_entry,
    evaluate_exit,
    FilterResult,
    EntryDecision,
)
from config import CONFIG
from gex_reader import GEX_DB
from tick_processor import TickProcessor
from day_gate import DayGate, DayGateResult

# ---------------------------------------------------------------------------
# Import backtest DB layer
# ---------------------------------------------------------------------------

import backtest_db

# Contracts per trade — read from config (single source of truth)
from config import CONFIG
CONTRACTS_PER_TRADE = CONFIG["entry"]["contracts_per_trade"]


# ---------------------------------------------------------------------------
# In-memory position tracking (mirrors live engine's TradePosition)
# ---------------------------------------------------------------------------

class PositionSide(Enum):
    CALL = "CALL"
    PUT  = "PUT"


@dataclass
class BacktestPosition:
    """Lightweight in-memory position mirroring live engine's TradePosition."""
    id:           int = 0
    db_id:        int = 0
    side:         Optional[PositionSide] = None
    short_strike: float = 0.0
    long_strike:  Optional[float] = None
    credit:       float = 0.0
    layer:        int = 1
    open_ts:      str = ""
    close_ts:     Optional[str] = None
    pnl:          Optional[float] = None
    status:       str = "open"
    reason:       Optional[str] = None
    contracts:    int = CONTRACTS_PER_TRADE

    # Entry market indicators (captured at position open)
    entry_spx_spot:                  Optional[float] = None
    entry_em:                        Optional[float] = None
    entry_gex_by_volume:             Optional[float] = None
    entry_bb_position:               Optional[float] = None
    entry_bb_expanding:              Optional[bool]  = None
    entry_adx:                       Optional[float] = None
    entry_macd_hist:                 Optional[float] = None
    entry_rsi:                       Optional[float] = None
    entry_atm_call_mid:              Optional[float] = None
    entry_atm_put_mid:               Optional[float] = None
    entry_atm_strike:               Optional[float] = None
    entry_regime:                    Optional[str]  = None
    entry_major_positive_by_volume:   Optional[float] = None
    entry_zero_gamma:                Optional[float] = None

    # Exit market indicators (captured at position close)
    exit_spx_spot:      Optional[float] = None
    exit_em:            Optional[float] = None
    exit_gex_by_volume: Optional[float] = None
    exit_bb_position:   Optional[float] = None
    exit_bb_expanding:  Optional[bool]  = None
    exit_adx:           Optional[float] = None
    exit_macd_hist:     Optional[float] = None
    exit_rsi:           Optional[float] = None
    exit_regime:        Optional[str]  = None

    @property
    def spread_width(self) -> float:
        if self.long_strike is not None:
            return abs(self.long_strike - self.short_strike)
        return 0.0


def _snapshot_entry_indicators(combined: CombinedSnapshot) -> dict:
    """Capture all entry indicators from a CombinedSnapshot."""
    return dict(
        entry_spx_spot                 = combined.spx_spot,
        entry_em                       = combined.expected_move,
        entry_gex_by_volume            = combined.gex_by_volume,
        entry_bb_position             = combined.bb_position,
        entry_bb_expanding            = combined.bb_expanding,
        entry_adx                     = combined.adx,
        entry_macd_hist               = combined.macd_hist,
        entry_rsi                     = combined.rsi,
        entry_atm_call_mid            = combined.atm_call_mid,
        entry_atm_put_mid             = combined.atm_put_mid,
        entry_atm_strike             = combined.atm_strike,
        entry_regime                  = combined.regime,
        entry_major_positive_by_volume = combined.major_positive_by_volume,
        entry_zero_gamma              = combined.zero_gamma,
    )


def _snapshot_exit_indicators(combined: CombinedSnapshot) -> dict:
    """Capture all exit indicators from a CombinedSnapshot."""
    return dict(
        exit_spx_spot      = combined.spx_spot,
        exit_em            = combined.expected_move,
        exit_gex_by_volume = combined.gex_by_volume,
        exit_bb_position   = combined.bb_position,
        exit_bb_expanding  = combined.bb_expanding,
        exit_adx           = combined.adx,
        exit_macd_hist     = combined.macd_hist,
        exit_rsi           = combined.rsi,
        exit_regime        = combined.regime,
    )


class BacktestPositionStore:
    """
    In-memory position store backed by backtest_positions table.
    """

    def __init__(self, conn: sqlite3.Connection, backtest_run_id: str) -> None:
        self._conn: sqlite3.Connection = conn
        self._run_id: str = backtest_run_id
        self._positions: list[BacktestPosition] = []
        self._next_id: int = 1

    def open(self, pos: BacktestPosition) -> BacktestPosition:
        """Insert position into backtest_positions, assign db_id."""
        pos.id = self._next_id
        self._next_id += 1
        pos.db_id = backtest_db.insert_backtest_position(self._conn, self._run_id, pos)
        self._positions.append(pos)
        return pos

    def close(
        self,
        pos_id: int,
        exit_ts: str,
        pnl: Optional[float],
        reason: str,
        exit_indicators: dict,
    ) -> None:
        for p in self._positions:
            if p.id == pos_id:
                p.status   = "closed"
                p.pnl      = pnl
                p.reason   = reason
                p.close_ts = exit_ts
                for k, v in exit_indicators.items():
                    setattr(p, k, v)
                backtest_db.close_backtest_position(
                    self._conn, self._run_id, p.db_id, exit_ts, pnl, reason,
                    **exit_indicators,
                )
                break

    def expire_all(
        self,
        exit_ts: str,
        exit_indicators: dict,
    ) -> list[BacktestPosition]:
        """Mark all open positions as expired (0DTE end-of-run forced expiry)."""
        expired = []
        for p in self._positions:
            if p.status == "open":
                p.status   = "expired"
                p.close_ts = exit_ts
                for k, v in exit_indicators.items():
                    setattr(p, k, v)
                backtest_db.expire_backtest_position(
                    self._conn, self._run_id, p.db_id, exit_ts,
                    **exit_indicators,
                )
                expired.append(p)
        return expired

    def get_open(self) -> list[BacktestPosition]:
        return [p for p in self._positions if p.status == "open"]

    def open_count(self) -> int:
        return len(self.get_open())

    def get_open_strikes(self) -> list[tuple[float, Optional[float]]]:
        return [(p.short_strike, p.long_strike) for p in self.get_open()]


# ---------------------------------------------------------------------------
# Backtest decisions
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    ts:      str
    kind:    str
    side:    Optional[str] = None
    spx:     float = 0.0
    em:      float = 0.0
    credit:  Optional[float] = None
    pnl:     Optional[float] = None
    detail:  str = ""
    reason:  str = ""


# ---------------------------------------------------------------------------
# Core replay — uses TickProcessor for identical eval logic to live engine
# ---------------------------------------------------------------------------

def _load_scan_rows(date: str) -> list[tuple]:
    """Load all scan rows for date in ascending timestamp order."""
    conn = sqlite3.connect(_SCAN_DB)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 10000;")
    rows = conn.execute(
        """
        SELECT * FROM scan_results
        WHERE timestamp_est LIKE ?
        ORDER BY timestamp_est ASC
        """,
        (f"{date}%",),
    ).fetchall()
    conn.close()
    return rows


def _is_market_open_at_ts(scan_ts: str) -> bool:
    """
    Check if scan_ts falls within the entry window (09:30–16:00 ET).
    scan_ts format: "2026-05-15T10:07:01-0400"
    """
    time_part = scan_ts.split("T")[1][:5]   # "10:07"
    h, m = int(time_part[:2]), int(time_part[3:])
    es_h, es_m = int(CONFIG["market"]["entry_start"][:2]), int(CONFIG["market"]["entry_start"][3:])
    ee_h, ee_m = int(CONFIG["market"]["entry_end"][:2]),   int(CONFIG["market"]["entry_end"][3:])
    if h < es_h or (h == es_h and m < es_m):
        return False
    if h > ee_h or (h == ee_h and m >= ee_m):
        return False
    return True


def _backtest_skip_handler(
    processor: TickProcessor,
    decisions: list[Decision],
    conn: sqlite3.Connection,
    backtest_run_id: str,
    store: BacktestPositionStore,
) -> callable:
    """
    Return the on_skip callback for backtest.
    Handles anti-spam reason-change tracking + writes backtest signal rows.
    """
    _last_skip_reason = {"CALL": None, "PUT": None}

    def on_skip(
        ts: str,
        target_side: str,
        decision,
        combined,
        spx: float,
        em: float,
        gex_val: float,
        rsi: float,
        regime: str,
    ):
        fr = decision.filter_result
        if fr is None:
            return

        premium_ok = fr.premium_passed
        dist_ok    = fr.distance_passed_any()

        # TASK-2026-127: only write signal when any meaningful condition passes
        if premium_ok or dist_ok:
            reason = fr.first_failure_reason
            backtest_db.insert_backtest_signal(
                conn, backtest_run_id, ts,
                target_side, "skip",
                decision,
                spx, em,
                gex=combined.gex_by_volume,
                vix=combined.vix,
                rsi=rsi,
                signalled=0,
                signal_reason=target_side,
                premium_passed=1 if premium_ok else 0,
                distance_passed=1 if dist_ok else 0,
                collision_passed=1 if fr.overlap_passed else 0,
                blocked_reason=reason,
                credit=decision.credit if hasattr(decision, "credit") else None,
                short_strike=decision.short_strike if hasattr(decision, "short_strike") else None,
                long_strike=decision.long_strike if hasattr(decision, "long_strike") else None,
                action="entry",
            )

        # Anti-spam: log SKIP only on reason CHANGE
        reason = fr.first_failure_reason if fr else None
        if reason != _last_skip_reason.get(target_side):
            _last_skip_reason[target_side] = reason
            decisions.append(Decision(
                ts=ts, kind="SKIP",
                side=target_side,
                spx=spx, em=em,
                credit=decision.credit,
                detail=f"reason={reason}",
                reason=reason,
            ))

    return on_skip


def run_backtest(
    date: str,
    backtest_run_id: str,
    verbose: bool = False,
    summary_only: bool = False,
) -> list[Decision]:
    """
    Replay all scan rows for `date` through the live entry/exit logic.

    Uses the same evaluate_entry() and evaluate_exit() as the live engine,
    called with the same signatures.  The entry window check uses scan_ts
    (via _is_market_open_at_ts), not wall-clock time, so pre-market and
    post-market hours in the replay data are handled correctly.

    All ticks are written to backtest.db:
      - every signal (entry/skip/exit_check/exited/expired) → backtest_signals
      - position open/close → backtest_positions
      - entry/exit indicator snapshots → backtest_positions columns

    0DTE Expiry: After all scan rows are processed, any positions still open
    are marked as status='expired' with exit_reason='expired_0dte'.  Full
    credit is kept (expired options are worthless).  This is done using the
    last available CombinedSnapshot for exit indicator values.

    Returns list of decisions for logging / summary.
    """
    backtest_db.init_backtest_db()

    conn = sqlite3.connect(backtest_db.BACKTEST_DB)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 10000;")
    conn.execute("PRAGMA busy_timeout = 10000;")

    rows = _load_scan_rows(date)
    if not rows:
        conn.close()
        print(f"No scan rows found for {date}")
        return []

    decisions: list[Decision] = []
    store = BacktestPositionStore(conn, backtest_run_id)

    # Per-side skip tracking (anti-spam: only log on reason CHANGE)
    _last_skip_reason: dict[str, Optional[str]] = {"CALL": None, "PUT": None}

    # -----------------------------------------------------------------------
    # Day gate — rolling30-min volatility gate, same params as live engine
    # -----------------------------------------------------------------------
    day_gate = DayGate()

    # -----------------------------------------------------------------------
    # TickProcessor callbacks
    # -----------------------------------------------------------------------

    def on_enter_approved(
        combined,
        decision,
        store: BacktestPositionStore,
        ts: str,
        spx: float,
        em: float,
        gex_val: float,
    ) -> None:
        """Called by TickProcessor when entry is approved."""
        fr = decision.filter_result
        premium_flag = 1 if (fr and fr.premium_passed) else 0
        distance_flag = 1 if (fr and fr.distance_passed_any()) else 0
        collision_flag = 1 if (fr and fr.overlap_passed) else 0

        backtest_db.insert_backtest_signal(
            conn, backtest_run_id, ts,
            decision.side, "entry",
            decision,
            spx, em,
            gex=combined.gex_by_volume,
            vix=combined.vix,
            rsi=combined.rsi,
            signalled=1,
            signal_reason=decision.reason,
            premium_passed=premium_flag,
            distance_passed=distance_flag,
            collision_passed=collision_flag,
            filled=1,
            short_strike=decision.short_strike,
            long_strike=decision.long_strike,
            credit=decision.credit,
            action="entry",
        )

        entry_indicators = _snapshot_entry_indicators(combined)
        new_pos = BacktestPosition(
            id=0,
            db_id=0,
            side=PositionSide(decision.side),
            short_strike=decision.short_strike,
            long_strike=decision.long_strike,
            credit=decision.credit,
            layer=decision.layer,
            open_ts=ts,
            **entry_indicators,
        )
        store.open(new_pos)

        decisions.append(Decision(
            ts=ts, kind="ENTRY",
            side=entry_decision.side if False else decision.side,
            spx=spx, em=em,
            credit=decision.credit,
            detail=(
                f"{decision.side} | "
                f"strike={decision.short_strike:.0f}/{decision.long_strike:.0f} | "
                f"total_credit=${decision.credit * CONTRACTS_PER_TRADE:.2f} (×{CONTRACTS_PER_TRADE}) | "
                f"layer={decision.layer} | SPX={spx:.2f}"
            ),
            reason=decision.reason,
        ))

    def on_skip(
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
        """Called by TickProcessor when entry is rejected."""
        fr = decision.filter_result
        if fr is None:
            return

        premium_ok = fr.premium_passed
        dist_ok    = fr.distance_passed_any()

        # TASK-2026-127: only write signal when any meaningful condition passes
        if premium_ok or dist_ok:
            reason = fr.first_failure_reason
            backtest_db.insert_backtest_signal(
                conn, backtest_run_id, ts,
                target_side, "skip",
                decision,
                spx, em,
                gex=combined.gex_by_volume,
                vix=combined.vix,
                rsi=rsi,
                signalled=0,
                signal_reason=target_side,
                premium_passed=1 if premium_ok else 0,
                distance_passed=1 if dist_ok else 0,
                collision_passed=1 if fr.overlap_passed else 0,
                blocked_reason=reason,
                credit=decision.credit if hasattr(decision, "credit") else None,
                short_strike=decision.short_strike if hasattr(decision, "short_strike") else None,
                long_strike=decision.long_strike if hasattr(decision, "long_strike") else None,
                action="entry",
            )

        # Anti-spam: log SKIP only on reason CHANGE
        reason = fr.first_failure_reason
        if reason != _last_skip_reason.get(target_side):
            _last_skip_reason[target_side] = reason
            decisions.append(Decision(
                ts=ts, kind="SKIP",
                side=target_side,
                spx=spx, em=em,
                credit=decision.credit,
                detail=f"reason={reason}",
                reason=reason,
            ))

    def on_exit_checked(
        ts: str,
        pos: BacktestPosition,
        decision,
        combined,
        store: BacktestPositionStore,
        spx: float,
        em: float,
        gex_val: float,
    ) -> None:
        """Called by TickProcessor for every open position every tick."""

        # TASK-2026-199: skip entirely when no exit conditions are met
        if decision.exit_conditions_met < 1:
            return

        if decision.should_exit:
            pnl = 0.0
            exit_indicators = _snapshot_exit_indicators(combined)
            exit_indicators["exit_layer"] = decision.exit_layer
            exit_indicators["exit_conditions_met"] = decision.exit_conditions_met
            store.close(pos.id, ts, pnl, decision.reason, exit_indicators)

            decisions.append(Decision(
                ts=ts, kind="EXITED",
                side=pos.side.value if pos.side else None,
                spx=spx, em=em,
                pnl=pnl,
                detail=(
                    f"pos_id={pos.id} | "
                    f"{pos.side.value if pos.side else '?'} | "
                    f"short={pos.short_strike} | total_credit=${pos.credit * CONTRACTS_PER_TRADE:.2f} (×{CONTRACTS_PER_TRADE}) | "
                    f"reason={decision.reason}"
                ),
                reason=decision.reason,
            ))
            backtest_db.insert_backtest_signal(
                conn, backtest_run_id, ts,
                pos.side.value if pos.side else None,
                "exited",
                decision,
                spx, em,
                gex=combined.gex_by_volume,
                vix=combined.vix,
                rsi=combined.rsi,
                signalled=1,
                signal_reason=decision.reason,
                premium_passed=0,
                distance_passed=0,
                collision_passed=0,
                filled=1,
                short_strike=pos.short_strike,
                long_strike=pos.long_strike,
                action="exit",
                layer=decision.exit_layer,
                displacement=decision.displacement,
                entry_em=pos.entry_em,
                near_major=1 if decision.near_major else 0,
                major_level=decision.major_level,
            )
        else:
            decisions.append(Decision(
                ts=ts, kind="STAY",
                side=pos.side.value if pos.side else None,
                spx=spx, em=em,
                pnl=None,
                detail=(
                    f"pos_id={pos.id} | "
                    f"{pos.side.value if pos.side else '?'} | "
                    f"short={pos.short_strike} | reason={decision.reason}"
                ),
                reason=decision.reason,
            ))
            backtest_db.insert_backtest_signal(
                conn, backtest_run_id, ts,
                pos.side.value if pos.side else None,
                "exit_check",
                decision,
                spx, em,
                gex=combined.gex_by_volume,
                vix=combined.vix,
                rsi=combined.rsi,
                signalled=0,
                signal_reason=decision.reason,
                premium_passed=0,
                distance_passed=0,
                collision_passed=0,
                filled=0,
                short_strike=pos.short_strike,
                long_strike=pos.long_strike,
                action="exit_check",
                layer=decision.exit_layer,
                displacement=decision.displacement,
                entry_em=pos.entry_em,
                near_major=1 if decision.near_major else 0,
                major_level=decision.major_level,
            )

    # -----------------------------------------------------------------------
    # TickProcessor instance (is_live=False for backtest)
    # -----------------------------------------------------------------------
    processor = TickProcessor(
        on_enter_approved=on_enter_approved,
        on_skip=on_skip,
        on_exit_checked=on_exit_checked,
        on_heartbeat=None,         # backtest uses Decision list, not heartbeat logger
        is_live=False,
    )

    # -----------------------------------------------------------------------
    # Main replay loop
    # -----------------------------------------------------------------------
    rsi_threshold = CONFIG["entry"].get("rsi_gate_threshold", 50.0)

    for row in rows:
        scan_ts = row[1]
        spx     = float(row[2])
        em      = float(row[3])

        if verbose:
            decisions.append(Decision(
                ts=scan_ts, kind="TICK",
                spx=spx, em=em,
                detail=f"SPX={spx:.2f} EM={em:.2f}",
            ))

        try:
            combined = get_combined_snapshot(scan_ts)
        except StaleDataError as e:
            decisions.append(Decision(
                ts=scan_ts, kind="STALE",
                spx=spx, em=em,
                detail=str(e),
            ))
            continue

        if combined.call_strike_003 is None or combined.put_strike_003 is None:
            continue

        # Enforce entry window via scan_ts (not wall-clock)
        if not _is_market_open_at_ts(scan_ts):
            continue

        # -----------------------------------------------------------------
        # TASK-2026-226: Rolling30-min day gate — same logic as live engine.
        # Gate blocks new entries when rolling averages show sustained danger
        # (signal_1 active AND at least one of signal_2/3 active).
        # Exits on open positions always run regardless of gate state.
        # -----------------------------------------------------------------
        gate_result = day_gate.update(combined)
        if gate_result.blocked:
            decisions.append(Decision(
                ts=scan_ts,
                kind="DAY_GATE_BLOCKED",
                spx=spx,
                em=em,
                detail=(
                    f"gex={gate_result.avg_gex:.1f} dist={gate_result.avg_dist:.1f} "
                    f"rsi={gate_result.avg_rsi:.1f} "
                    f"s1={'Y' if gate_result.signal_1 else 'N'} "
                    f"s2={'Y' if gate_result.signal_2 else 'N'} "
                    f"s3={'Y' if gate_result.signal_3 else 'N'} "
                    f"n={gate_result.n_samples}"
                ),
            ))
            # Exits always run even when gate is blocking entries.
            # Call _run_exit_checks directly — process_tick would also run
            # entry evaluation, which must be suppressed when gate blocks.
            processor._run_exit_checks(
                combined=combined,
                ts=scan_ts,
                spx=spx,
                em=em,
                gex_val=combined.gex_by_volume,
                regime=combined.regime or "neutral",
                rsi=combined.rsi,
                store=store,
            )
            continue

        gex_val    = combined.gex_by_volume
        regime     = combined.regime or "neutral"
        rsi        = combined.rsi
        gex_regime = "dealer_long" if gex_val >= 0 else "dealer_short"

        processor.process_tick(
            combined=combined,
            ts=scan_ts,
            spx=spx,
            em=em,
            gex_val=gex_val,
            regime=regime,
            rsi=rsi,
            gex_regime=gex_regime,
            vix=combined.vix,
            store=store,
            open_strikes=store.get_open_strikes(),
        )

    # -----------------------------------------------------------------------
    # 0DTE End-of-Run Expiry: mark all still-open positions as expired.
    # The scan data ends at 15:59 ET; the last combined snapshot gives us
    # the end-of-day indicator values for the exit snapshot.
    # -----------------------------------------------------------------------
    if store.open_count() > 0:
        last_ts = rows[-1][1]
        last_spx = float(rows[-1][2])
        last_em  = float(rows[-1][3])

        try:
            last_combined = get_combined_snapshot(last_ts)
        except StaleDataError:
            last_combined = None

        exit_indicators: dict
        if last_combined is not None:
            exit_indicators = _snapshot_exit_indicators(last_combined)
            last_gex = last_combined.gex_by_volume
        else:
            exit_indicators = {
                "exit_spx_spot":      last_spx,
                "exit_em":            last_em,
                "exit_gex_by_volume": None,
                "exit_bb_position":   None,
                "exit_bb_expanding":  None,
                "exit_adx":           None,
                "exit_macd_hist":     None,
                "exit_rsi":           None,
                "exit_regime":        None,
            }
            last_gex = None

        expired_positions = store.expire_all(last_ts, exit_indicators)

        for pos in expired_positions:
            decisions.append(Decision(
                ts=last_ts,
                kind="EXPIRED",
                side=pos.side.value if pos.side else None,
                spx=last_spx,
                em=last_em,
                pnl=0.0,
                detail=(
                    f"pos_id={pos.id} | "
                    f"{pos.side.value if pos.side else '?'} | "
                    f"short={pos.short_strike} | "
                    f"total_credit=${pos.credit * CONTRACTS_PER_TRADE:.2f} (×{CONTRACTS_PER_TRADE}) | "
                    f"status=expired | reason=expired_0dte"
                ),
                reason="expired_0dte",
            ))
            backtest_db.insert_backtest_signal(
                conn, backtest_run_id, last_ts,
                pos.side.value if pos.side else None,
                "expired",
                None,
                last_spx, last_em,
                gex=last_gex,
                vix=last_combined.vix if last_combined else None,
                rsi=last_combined.rsi if last_combined else None,
                signalled=1,
                signal_reason="expired_0dte",
                premium_passed=0,
                distance_passed=0,
                collision_passed=0,
                filled=1,
                short_strike=pos.short_strike,
                long_strike=pos.long_strike,
                action="exit",
                layer=pos.layer,
            )

    conn.commit()
    conn.close()
    return decisions


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _ts_display(ts: str) -> str:
    """Show just the HH:MM:SS part of a timestamp."""
    return ts.split("T")[1][:8] if "T" in ts else ts


def print_decisions(decisions: list[Decision]) -> None:
    for d in decisions:
        ts = _ts_display(d.ts)
        if d.kind == "TICK":
            print(f"{ts} ET [TICK] {d.detail}")
        elif d.kind == "STALE":
            print(f"{ts} ET [STALE] {d.detail}")
        elif d.kind == "DAY_GATE_BLOCKED":
            print(f"{ts} ET [DAY_GATE_BLOCKED] {d.detail}")
        elif d.kind == "ENTRY":
            print(f"{ts} ET [ENTRY] {d.detail}")
        elif d.kind == "SKIP":
            print(f"{ts} ET [SKIP] {d.side} | {d.detail}")
        elif d.kind == "EXITED":
            print(f"{ts} ET [EXITED] {d.detail}")
        elif d.kind == "EXPIRED":
            print(f"{ts} ET [EXPIRED] {d.detail}")


def print_summary(decisions: list[Decision], date: str) -> None:
    entries   = [d for d in decisions if d.kind == "ENTRY"]
    call_ents = [d for d in entries   if d.side == "CALL"]
    put_ents  = [d for d in entries   if d.side == "PUT"]
    exits     = [d for d in decisions if d.kind == "EXITED"]
    expired   = [d for d in decisions if d.kind == "EXPIRED"]
    stale_cnt = len([d for d in decisions if d.kind == "STALE"])
    gate_blocked_cnt = len([d for d in decisions if d.kind == "DAY_GATE_BLOCKED"])
    total_credit = sum((d.credit or 0.0) * 100 * CONTRACTS_PER_TRADE for d in entries)

    max_open = 0
    current_open = 0
    for d in decisions:
        if d.kind == "ENTRY":
            current_open += 1
            max_open = max(max_open, current_open)
        elif d.kind == "EXITED":
            current_open -= 1
        elif d.kind == "EXPIRED":
            current_open -= 1
    still_open = current_open

    print()
    print(f"=== Backtest Results: {date} ===")
    print(f"Entries triggered:   {len(entries)}")
    print(f"  CALL entries:      {len(call_ents)}")
    print(f"  PUT entries:       {len(put_ents)}")
    print(f"Exits triggered:     {len(exits)}")
    print(f"Expired (0DTE EOD):  {len(expired)}")
    print(f"Total credit (×100×4):  ${total_credit:.2f}")
    print(f"Max open positions:  {max_open}")
    if still_open == 0:
        print(f"Positions at EOD:    0 (all closed or expired)")
    else:
        print(f"Positions at EOD:    {still_open}  (would run overnight)")
    print(f"Skipped ticks (StaleDataError): {stale_cnt}")
    print(f"Day gate blocked ticks: {gate_blocked_cnt}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Replay historical scanner data through the engine entry/exit logic"
    )
    p.add_argument("--date", required=True, help="Date to replay (YYYY-MM-DD)")
    p.add_argument(
        "--run-id",
        default=None,
        help="Override auto-generated backtest_run_id (format: YYYY-MM-DD_HHMMSS). "
             "Useful for re-running a specific date deterministically.",
    )
    p.add_argument(
        "--verbose", action="store_true",
        help="Print every tick heartbeat (not just decisions)",
    )
    p.add_argument(
        "--summary-only", action="store_true",
        help="Print summary only (skip per-tick log)",
    )
    return p


if __name__ == "__main__":
    parser = _build_argparser()
    args = parser.parse_args()

    backtest_run_id = args.run_id or backtest_db.get_backtest_run_id(args.date)

    print(f"Backtest replay for {args.date}")
    print(f"backtest_run_id: {backtest_run_id}")
    print(f"DRY_RUN=True | Entry window: {CONFIG['market']['entry_start']}–{CONFIG['market']['entry_end']} ET")
    print(f"0DTE expiry: positions open at EOD are marked expired (exit_reason=expired_0dte)")
    print(f"TickProcessor: shared eval logic with live engine")
    print(f"Day gate: rolling 30-min window (params from CONFIG['day_gate'])")
    print()

    decisions = run_backtest(
        date=args.date,
        backtest_run_id=backtest_run_id,
        verbose=args.verbose,
        summary_only=args.summary_only,
    )

    if args.summary_only:
        print_summary(decisions, args.date)
    else:
        print_decisions(decisions)
        print()
        print_summary(decisions, args.date)
