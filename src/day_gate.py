"""
day_gate.py — Rolling 30-min volatility protection gate.

Blocks new entries when a rolling window of CombinedData snapshots shows
sustained danger across three signals:

  Signal 1 (primary):  avg GEX-by-OI < threshold (default 0)
  Signal 2:            avg(SPX spot − zero_gamma) < threshold (default -15)
  Signal 3:            avg RSI < rsi_extreme_low OR > rsi_extreme_high

Rule: Signal 1 fires AND at least one of Signal 2/3 fires → blocked.

Gate is dynamic: re-evaluated every tick. Unblocks if rolling averages recover.
Exits on open positions always run regardless of gate state.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time as _time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from config import CONFIG


# ---------------------------------------------------------------------------
# Internal helpers (avoid importing from combined_reader to prevent cycles)
# ---------------------------------------------------------------------------

def _parse_ts(ts: str) -> str:
    """
    Return a SQLite datetime()-compatible string from an ISO-8601 timestamp.

    Handles:
      - '2026-05-15T15:59:02-04:00'   (ISO with timezone offset)
      - '2026-05-15T15:58:03.497097-04:00'
      - '2026-05-15 15:57:59'         (space-separated, no offset)

    Returns: 'YYYY-MM-DD HH:MM:SS[.fff]' suitable for SQLite datetime().
    The timezone offset (-04:00 etc.) is stripped.
    """
    ts = re.sub(r'[-+]\d{2}:?\d{2}$', '', ts.rstrip())
    return ts.replace('T', ' ')


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class DayGateResult:
    blocked: bool
    avg_gex: float
    avg_dist: float       # avg(spot - zero_gamma)
    avg_rsi: float
    signal_1: bool        # GEX-by-OI danger
    signal_2: bool        # spot-zero_gamma danger
    signal_3: bool        # RSI extreme danger
    n_samples: int


class DayGate:
    """
    Maintains a rolling in-memory buffer of CombinedData snapshots and
    evaluates the 2-of-3 danger rule on every update() call.

    Parameters
    ----------
    logger : logging.Logger | None
        If provided, state-transition logs are written here. Caller is
        responsible for injecting the engine logger.
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self._logger = logger or logging.getLogger("day_gate")
        cfg = CONFIG.get("day_gate", {})
        self._enabled: bool = cfg.get("enabled", True)
        self._window_minutes: int = int(cfg.get("window_minutes", 30))
        self._gex_threshold: float = float(cfg.get("gex_by_oi_threshold", 0.0))
        self._dist_threshold: float = float(cfg.get("spot_zero_gamma_threshold", -15.0))
        self._rsi_low: float = float(cfg.get("rsi_extreme_low", 15.0))
        self._rsi_high: float = float(cfg.get("rsi_extreme_high", 85.0))

        # Each entry: (epoch_seconds, gex_by_oi, spot_minus_zero_gamma, rsi)
        self._buffer: deque[tuple[float, float, float, Optional[float]]] = deque()

        self._was_blocked: Optional[bool] = None  # None = never evaluated yet

        # TASK-2026-227: Track last seen GEX scan_timestamp to avoid duplicate
        # buffer entries when the same GEX reading is returned across multiple
        # scanner ticks (GEX saves every 2-6 min; scanner ticks every 30 s).
        self._last_gex_timestamp: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def prefill_from_db(self, scan_timestamp: str) -> None:
        """
        Pre-fill the rolling buffer from historical gex_snapshots data so the
        gate is operative immediately on engine start (no 30-min warm-up).

        Queries gex_snapshots for all rows in the last 30 minutes of
        `scan_timestamp`, ordered ascending, and populates the buffer.
        Sets _last_gex_timestamp to the most recent row's timestamp.

        RSI is set to None for pre-filled entries (not available in gex.db);
        it will be populated with real values on subsequent update() calls.

        Parameters
        ----------
        scan_timestamp : str
            Current scan timestamp (ISO or space format). The 30-minute window
            is computed as (scan_timestamp - 30 min) .. scan_timestamp.
        """
        GEX_DB = Path(__file__).parent.parent / "data" / "gex.db"

        parsed_ts = _parse_ts(scan_timestamp)
        window_start_sql = f"datetime('{parsed_ts}', '-{self._window_minutes} minutes')"

        conn = sqlite3.connect(GEX_DB)
        conn.execute("PRAGMA journal_mode = WAL;")
        rows = conn.execute(f"""
            SELECT timestamp, gex_by_oi, spot, zero_gamma
            FROM gex_snapshots
            WHERE timestamp >= {window_start_sql}
              AND timestamp <= ?
            ORDER BY timestamp ASC
        """, (parsed_ts,)).fetchall()
        conn.close()

        # Use current time as epoch so prefill entries are "fresh" in the
        # rolling window and not immediately pruned on the first tick.
        now_epoch = _time.time()

        for row in rows:
            ts_str, gex_by_oi, spot, zero_gamma = row
            dist = spot - zero_gamma
            # All prefill entries share the same "arrival" time (now); they will
            # be pruned naturally as the 30-min rolling window advances.
            self._buffer.append((now_epoch, gex_by_oi, dist, None))
            now_epoch += 0.001  # microsecond apart so oldest is first in deque

        if rows:
            self._last_gex_timestamp = rows[-1][0]

    def update(self, combined) -> DayGateResult:
        """
        Append the latest CombinedData snapshot (if genuinely new), prune old
        entries, and return the current gate evaluation.
        """
        if not self._enabled:
            return DayGateResult(
                blocked=False, avg_gex=0.0, avg_dist=0.0, avg_rsi=0.0,
                signal_1=False, signal_2=False, signal_3=False, n_samples=0,
            )

        now = _time.time()
        cutoff = now - self._window_minutes * 60

        gex = combined.gex_by_oi
        dist = combined.spx_spot - combined.zero_gamma
        rsi = combined.rsi

        # TASK-2026-227: Deduplication — only append if this is a genuinely new
        # GEX reading (scan_timestamp changed means a different GEX row was
        # returned by the as-of join).  Skip buffer append on duplicate GEX but
        # still evaluate so gate state is current.
        gex_ts = combined.scan_timestamp
        is_new_gex = gex_ts != self._last_gex_timestamp

        if is_new_gex:
            self._buffer.append((now, gex, dist, rsi))
            self._last_gex_timestamp = gex_ts

        # Prune entries outside the rolling window
        while self._buffer and self._buffer[0][0] < cutoff:
            self._buffer.popleft()

        result = self._evaluate()
        self._log_transition(result, combined.scan_timestamp)
        return result

    @property
    def is_blocked(self) -> bool:
        """Current gate state without updating the buffer."""
        if not self._enabled:
            return False
        if not self._buffer:
            return False
        return self._evaluate().blocked

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _evaluate(self) -> DayGateResult:
        if not self._buffer:
            return DayGateResult(
                blocked=False, avg_gex=0.0, avg_dist=0.0, avg_rsi=0.0,
                signal_1=False, signal_2=False, signal_3=False, n_samples=0,
            )

        n = len(self._buffer)
        avg_gex  = sum(e[1] for e in self._buffer) / n
        avg_dist = sum(e[2] for e in self._buffer) / n

        # RSI may be None for pre-filled entries — only average valid (non-None) RSI
        valid_rsi = [e[3] for e in self._buffer if e[3] is not None]
        avg_rsi = (sum(valid_rsi) / len(valid_rsi)) if valid_rsi else 0.0

        s1 = avg_gex < self._gex_threshold
        s2 = avg_dist < self._dist_threshold
        s3 = avg_rsi < self._rsi_low or avg_rsi > self._rsi_high

        blocked = s1 and (s2 or s3)

        return DayGateResult(
            blocked=blocked,
            avg_gex=avg_gex,
            avg_dist=avg_dist,
            avg_rsi=avg_rsi,
            signal_1=s1,
            signal_2=s2,
            signal_3=s3,
            n_samples=n,
        )

    def _log_transition(self, result: DayGateResult, ts: str) -> None:
        signals = (
            f"avg_gex={result.avg_gex:.1f}[{'DANGER' if result.signal_1 else 'ok'}]  "
            f"avg_dist={result.avg_dist:.1f}[{'DANGER' if result.signal_2 else 'ok'}]  "
            f"avg_rsi={result.avg_rsi:.1f}[{'DANGER' if result.signal_3 else 'ok'}]  "
            f"n={result.n_samples} window={self._window_minutes}m"
        )

        if result.blocked:
            # Always log at INFO when blocked — actionable state
            self._logger.info(f"{ts} ET [DAY_GATE] BLOCKED | {signals}")
        else:
            # DEBUG when open — visible in verbose mode, silent at INFO
            self._logger.debug(f"{ts} ET [DAY_GATE] clear | {signals}")

        # Log transition marker on state change
        if result.blocked != self._was_blocked:
            self._was_blocked = result.blocked
            if result.blocked:
                self._logger.warning(f"{ts} ET [DAY_GATE] TRANSITION → BLOCKED")
            else:
                self._logger.info(f"{ts} ET [DAY_GATE] TRANSITION → CLEARED")