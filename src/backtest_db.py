"""
backtest_db.py — Backtest shadow tables (same schema as live signals/positions).

Shadow tables live in data/backtest.db (separate from live positions.db).
Schema mirrors live pipeline's signals + positions tables exactly.

Tables:
  - backtest_signals   — every tick logged: entry / skip / exit_check / exited / expired
  - backtest_positions — open/closed/expired position records for each backtest run

backtest_run_id format: {date}_{HHMMSS} e.g. 2026-05-15_135300
All rows for a given run share this ID so the run is queryable as a unit.

Usage:
    init_backtest_db()          # create tables on startup
    insert_backtest_signal(...)  # every tick with per-condition flags
    insert_backtest_position(..)  # on entry
    close_backtest_position(..)   # on natural exit
    expire_backtest_position(..)  # 0DTE end-of-run forced expiry

TASK-2026-174: Unified schema with live positions.db.
  - backtest_positions now has all exit-tracking columns (exit_layer,
    exit_conditions_met, exit_gex_regime) matching live positions table
  - backtest_signals has decision column like live signals
  - total_credit written as net figure (credit × 100 × contracts)
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_DIR   = Path(__file__).parent.parent          # ibkr_auto_trader/
BACKTEST_DB = _REPO_DIR / "data" / "backtest.db"

# Contracts per trade — read from config (single source of truth)
import sys
from pathlib import Path
_BACKTEST_DB_DIR = Path(__file__).parent          # src/
_CONFIG_DIR      = _BACKTEST_DB_DIR.parent / "config"  # config/
_SRC_DIR         = _BACKTEST_DB_DIR                  # src/
sys.path.insert(0, str(_CONFIG_DIR))   # for: from config import CONFIG
sys.path.insert(0, str(_SRC_DIR))     # for: from schema import ...
from config import CONFIG
CONTRACTS_PER_TRADE = CONFIG["entry"]["contracts_per_trade"]

# Shared column lists (single source of truth)
from schema import (
    SIGNAL_COMMON_COLS,
    SIGNAL_BACKTEST_UNIQUE,
    POSITION_COMMON_COLS,
    POSITION_BACKTEST_UNIQUE,
)

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _serialize_decision(decision: Any) -> Optional[str]:
    """
    Serialize an EntryDecision or ExitDecision to JSON.

    Skips non-JSON-serializable objects (e.g. FilterResult dataclass attached
    to EntryDecision.filter_result) by replacing them with a summary string.
    """
    if decision is None:
        return None

    # Build a JSON-safe copy of __dict__, replacing non-serializable objects
    safe: dict = {}
    for k, v in decision.__dict__.items():
        if isinstance(v, (str, int, float, bool, type(None))):
            safe[k] = v
        elif isinstance(v, list):
            safe[k] = [
                _serialize_decision(item) if hasattr(item, "__dict__") else item
                for item in v
            ]
        elif hasattr(v, "__dict__"):
            # Nested dataclass (e.g. FilterResult) — store as string summary
            safe[k] = str(v)
        else:
            safe[k] = str(v)

    return json.dumps(safe)


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------

def _col_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    """Return True if `col` already exists in `table`."""
    existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    return col in existing


def _add_col(conn: sqlite3.Connection, table: str, col: str, col_type: str) -> None:
    """Add a column to a table if it doesn't already exist (safe for warm DB)."""
    if not _col_exists(conn, table, col):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def init_backtest_db() -> None:
    """
    Create shadow tables with the same column structure as the live
    signals and positions tables (TASK-2026-126: per-condition flags).

    Existing tables are kept (CREATE TABLE IF NOT EXISTS), so calling this
    on a warm backtest.db is safe.
    New columns are added via ALTER TABLE after the initial CREATE so
    existing DBs migrate cleanly.

    TASK-2026-174: backtest_positions now includes all exit-tracking columns
    to match live positions table schema.

    Includes automatic WAL recovery if the database is corrupted.
    """
    Path(BACKTEST_DB).parent.mkdir(parents=True, exist_ok=True)

    wal_path = Path(BACKTEST_DB).with_suffix(Path(BACKTEST_DB).suffix + "-wal")
    shm_path = Path(BACKTEST_DB).with_suffix(Path(BACKTEST_DB).suffix + "-shm")

    retry_count = 0
    max_retries = 2
    while retry_count < max_retries:
        try:
            conn = sqlite3.connect(str(BACKTEST_DB), timeout=5.0)
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA busy_timeout = 10000;")
            break
        except sqlite3.OperationalError as e:
            if "unable to open database file" in str(e) and retry_count < max_retries - 1:
                print(f"[Backtest DB] Recovering from WAL corruption...")
                if wal_path.exists():
                    try:
                        wal_path.unlink()
                    except OSError:
                        pass
                if shm_path.exists():
                    try:
                        shm_path.unlink()
                    except OSError:
                        pass
                time.sleep(0.5)
                retry_count += 1
                continue
            raise

    # --- backtest_signals (TASK-2026-126: mirrors live signals schema) ---
    # TASK-2026-174: decision column included to match live signals
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS backtest_signals (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            backtest_run_id     TEXT    NOT NULL,
            ts                  TEXT    NOT NULL,
            side                TEXT,
            layer               INTEGER NOT NULL DEFAULT 1,
            spx_spot            REAL,
            em                  REAL,
            gex                 REAL,

            -- Decision: was a signal generated?
            signalled           INTEGER DEFAULT 0,
            signal_reason       TEXT,

            -- Per-condition flags (TASK-2026-126)
            premium_passed      INTEGER DEFAULT 0,
            distance_passed     INTEGER DEFAULT 0,
            collision_passed    INTEGER DEFAULT 0,

            -- Market context at signal time
            vix                REAL,
            rsi                REAL,

            -- Execution results
            filled              INTEGER DEFAULT 0,
            short_strike        REAL,
            long_strike         REAL,
            credit              REAL,

            -- Why NOT filled (if signalled but not filled)
            blocked_reason     TEXT,

            action             TEXT,
            decision           TEXT,
            created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
        );
    """)

    # --- backtest_positions (TASK-2026-174: unified schema with live positions) ---
    # Includes all exit-tracking columns matching live positions:
    # exit_layer, exit_conditions_met, exit_gex_regime, exit_bb_expanding, exit_macd_hist
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS backtest_positions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            backtest_run_id TEXT    NOT NULL,
            side            TEXT    NOT NULL,
            entry_ts        TEXT    NOT NULL,
            short_strike    REAL    NOT NULL,
            long_strike     REAL    NOT NULL,
            credit          REAL    NOT NULL,
            spread_width    REAL    NOT NULL,
            layer           INTEGER NOT NULL DEFAULT 1,
            status          TEXT    NOT NULL DEFAULT 'open',
            contracts       INTEGER NOT NULL DEFAULT 4,
            total_credit    REAL    NOT NULL DEFAULT 0,

            -- Entry indicators (matching live positions)
            entry_spx_spot                  REAL,
            entry_em                        REAL,
            entry_gex_by_volume             REAL,
            entry_bb_position               REAL,
            entry_bb_expanding             INTEGER,
            entry_adx                       REAL,
            entry_macd_hist                 REAL,
            entry_rsi                       REAL,
            entry_atm_call_mid             REAL,
            entry_atm_put_mid               REAL,
            entry_atm_strike               REAL,
            entry_regime                    TEXT,
            entry_major_positive_by_volume  REAL,
            entry_zero_gamma                REAL,

            -- Exit indicators (TASK-2026-174: all exit-tracking columns matching live positions)
            exit_spx_spot      REAL,
            exit_em            REAL,
            exit_gex_by_volume REAL,
            exit_bb_position   REAL,
            exit_bb_expanding  INTEGER,
            exit_adx           REAL,
            exit_macd_hist     REAL,
            exit_rsi           REAL,
            exit_regime        TEXT,

            -- Exit decision metadata (TASK-2026-174: added exit_layer, exit_conditions_met, exit_gex_regime)
            exit_layer         INTEGER,
            exit_conditions_met INTEGER,
            exit_gex_regime    TEXT,

            -- Position outcome
            exit_ts            TEXT,
            pnl                REAL,
            exit_reason        TEXT,

            created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
        );
    """)

    # -----------------------------------------------------------------------
    # Migrate warm DBs: add any missing columns (safe ALTER TABLE)
    # -----------------------------------------------------------------------

    # Entry indicators (may already exist from earlier migrations)
    _add_col(conn, "backtest_positions", "entry_spx_spot",                  "REAL")
    _add_col(conn, "backtest_positions", "entry_em",                        "REAL")
    _add_col(conn, "backtest_positions", "entry_gex_by_volume",             "REAL")
    _add_col(conn, "backtest_positions", "entry_bb_position",               "REAL")
    _add_col(conn, "backtest_positions", "entry_bb_expanding",             "INTEGER")
    _add_col(conn, "backtest_positions", "entry_adx",                        "REAL")
    _add_col(conn, "backtest_positions", "entry_macd_hist",                  "REAL")
    _add_col(conn, "backtest_positions", "entry_rsi",                       "REAL")
    _add_col(conn, "backtest_positions", "entry_atm_call_mid",              "REAL")
    _add_col(conn, "backtest_positions", "entry_atm_put_mid",               "REAL")
    _add_col(conn, "backtest_positions", "entry_atm_strike",                "REAL")
    _add_col(conn, "backtest_positions", "entry_regime",                     "TEXT")
    _add_col(conn, "backtest_positions", "entry_major_positive_by_volume",  "REAL")
    _add_col(conn, "backtest_positions", "entry_zero_gamma",                "REAL")

    # Exit indicators (TASK-2026-174: ensure all exit-tracking columns present)
    _add_col(conn, "backtest_positions", "exit_spx_spot",                  "REAL")
    _add_col(conn, "backtest_positions", "exit_em",                         "REAL")
    _add_col(conn, "backtest_positions", "exit_gex_by_volume",              "REAL")
    _add_col(conn, "backtest_positions", "exit_bb_position",               "REAL")
    _add_col(conn, "backtest_positions", "exit_bb_expanding",              "INTEGER")
    _add_col(conn, "backtest_positions", "exit_adx",                       "REAL")
    _add_col(conn, "backtest_positions", "exit_macd_hist",                  "REAL")
    _add_col(conn, "backtest_positions", "exit_rsi",                        "REAL")
    _add_col(conn, "backtest_positions", "exit_regime",                     "TEXT")

    # Exit decision metadata (TASK-2026-174: new columns to match live positions)
    _add_col(conn, "backtest_positions", "exit_layer",         "INTEGER")
    _add_col(conn, "backtest_positions", "exit_conditions_met","INTEGER")
    _add_col(conn, "backtest_positions", "exit_gex_regime",    "TEXT")

    # Backfill existing rows: total_credit = credit × 100 × contracts
    # (previously may have been stored as credit × contracts, so fix the math)
    if _col_exists(conn, "backtest_positions", "total_credit"):
        conn.execute("""
            UPDATE backtest_positions
            SET contracts    = COALESCE(contracts, 4),
                total_credit = credit * 100 * COALESCE(contracts, 4)
            WHERE total_credit = 0
               OR ABS(total_credit - credit * COALESCE(contracts, 4)) < 0.01
        """)

    # Migrate backtest_signals with per-condition columns (TASK-2026-126)
    _add_col(conn, "backtest_signals", "layer",               "INTEGER NOT NULL DEFAULT 1")
    _add_col(conn, "backtest_signals", "spx_spot",           "REAL")
    _add_col(conn, "backtest_signals", "em",                  "REAL")
    _add_col(conn, "backtest_signals", "gex",                 "REAL")
    _add_col(conn, "backtest_signals", "signalled",           "INTEGER DEFAULT 0")
    _add_col(conn, "backtest_signals", "signal_reason",       "TEXT")
    _add_col(conn, "backtest_signals", "premium_passed",      "INTEGER DEFAULT 0")
    _add_col(conn, "backtest_signals", "distance_passed",     "INTEGER DEFAULT 0")
    _add_col(conn, "backtest_signals", "collision_passed",    "INTEGER DEFAULT 0")
    _add_col(conn, "backtest_signals", "vix",                 "REAL")
    _add_col(conn, "backtest_signals", "rsi",                 "REAL")
    _add_col(conn, "backtest_signals", "filled",              "INTEGER DEFAULT 0")
    _add_col(conn, "backtest_signals", "short_strike",        "REAL")
    _add_col(conn, "backtest_signals", "long_strike",         "REAL")
    _add_col(conn, "backtest_signals", "credit",              "REAL")
    _add_col(conn, "backtest_signals", "blocked_reason",      "TEXT")
    _add_col(conn, "backtest_signals", "action",              "TEXT")
    _add_col(conn, "backtest_signals", "decision",            "TEXT")

    # TASK-2026-199: exit diagnostic fields
    _add_col(conn, "backtest_signals", "displacement",     "REAL")
    _add_col(conn, "backtest_signals", "entry_em",         "REAL")
    _add_col(conn, "backtest_signals", "near_major",       "INTEGER")
    _add_col(conn, "backtest_signals", "major_level",      "REAL")

    conn.commit()
    conn.close()


def insert_backtest_signal(
    conn: sqlite3.Connection,
    backtest_run_id: str,
    ts: str,
    side: Optional[str],
    signal_type: str,
    decision,        # EntryDecision | ExitDecision | None
    spx_spot: float,
    em: float,
    gex: Optional[float] = None,
    vix: Optional[float] = None,
    rsi: Optional[float] = None,
    signalled: int = 0,
    signal_reason: Optional[str] = None,
    premium_passed: int = 0,
    distance_passed: int = 0,
    collision_passed: int = 0,
    filled: int = 0,
    short_strike: Optional[float] = None,
    long_strike: Optional[float] = None,
    credit: Optional[float] = None,
    blocked_reason: Optional[str] = None,
    action: Optional[str] = None,
    layer: int = 1,
    # TASK-2026-199: exit diagnostic fields
    displacement: Optional[float] = None,
    entry_em: Optional[float] = None,
    near_major: Optional[int] = None,
    major_level: Optional[float] = None,
) -> None:
    """
    Write one signal row to backtest_signals.

    signal_type values:
      - "entry"   — entry approved and position opened
      - "skip"    — entry rejected (condition failure or RSI gate)
      - "exited"  — position exited (natural exit)
      - "expired" — position expired (0DTE forced expiry at EOD)
    """
    decision_json = _serialize_decision(decision)
    conn.execute(
        """
        INSERT INTO backtest_signals
            (backtest_run_id, ts, side, layer, spx_spot, em, gex, vix, rsi,
             signalled, signal_reason,
             premium_passed, distance_passed, collision_passed,
             filled, short_strike, long_strike, credit,
             blocked_reason, action, decision,
             displacement, entry_em, near_major, major_level)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            backtest_run_id, ts, side, layer, spx_spot, em, gex, vix, rsi,
            signalled, signal_reason,
            premium_passed, distance_passed, collision_passed,
            filled, short_strike, long_strike, credit,
            blocked_reason, action, decision_json,
            displacement, entry_em, near_major, major_level,
        ),
    )


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def insert_backtest_position(
    conn: sqlite3.Connection,
    backtest_run_id: str,
    pos,  # BacktestPosition
) -> int:
    """
    Insert a new open position into backtest_positions.
    Returns the inserted row id (db_id).

    Credit is stored per SHARE (e.g. $0.25/share).
    total_credit = credit × 100 shares/contract × contracts (net figure).
    """
    contracts    = getattr(pos, "contracts", CONTRACTS_PER_TRADE)
    total_credit = pos.credit * 100 * contracts  # net credit × 100 shares × contracts

    cursor = conn.execute(
        """
        INSERT INTO backtest_positions
            (backtest_run_id, side, entry_ts, short_strike, long_strike,
             credit, spread_width, layer, status,
             contracts, total_credit,
             entry_spx_spot, entry_em, entry_gex_by_volume,
             entry_bb_position, entry_bb_expanding,
             entry_adx, entry_macd_hist, entry_rsi,
             entry_atm_call_mid, entry_atm_put_mid, entry_atm_strike,
             entry_regime, entry_major_positive_by_volume, entry_zero_gamma)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open',
                ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?)
        """,
        (
            backtest_run_id,
            pos.side.value if hasattr(pos.side, "value") else str(pos.side),
            pos.open_ts,
            pos.short_strike,
            pos.long_strike or pos.short_strike,
            pos.credit,
            pos.spread_width,
            pos.layer,
            contracts,
            total_credit,
            # Entry indicators
            pos.entry_spx_spot,
            pos.entry_em,
            pos.entry_gex_by_volume,
            pos.entry_bb_position,
            int(pos.entry_bb_expanding) if pos.entry_bb_expanding is not None else None,
            pos.entry_adx,
            pos.entry_macd_hist,
            pos.entry_rsi,
            pos.entry_atm_call_mid,
            pos.entry_atm_put_mid,
            pos.entry_atm_strike,
            pos.entry_regime,
            pos.entry_major_positive_by_volume,
            pos.entry_zero_gamma,
        ),
    )
    return cursor.lastrowid


def close_backtest_position(
    conn: sqlite3.Connection,
    backtest_run_id: str,
    db_id: int,
    exit_ts: str,
    pnl: Optional[float],
    reason: str,
    **exit_indicators: Any,
) -> None:
    """
    Mark a backtest position as closed.
    Updates status + exit_ts + pnl + exit_reason in backtest_positions.
    Also stores exit indicator snapshots via **exit_indicators.

    TASK-2026-174: exit_indicators now includes exit_layer, exit_conditions_met,
    and exit_gex_regime to match live positions schema.
    """
    exit_cols = ", ".join(f"{k} = ?" for k in exit_indicators)
    exit_vals = list(exit_indicators.values())
    conn.execute(
        f"""
        UPDATE backtest_positions
        SET status='closed', exit_ts=?, pnl=?, exit_reason=?
            {', ' + exit_cols if exit_cols else ''}
        WHERE id=? AND backtest_run_id=?
        """,
        (exit_ts, pnl, reason, *exit_vals, db_id, backtest_run_id),
    )


def expire_backtest_position(
    conn: sqlite3.Connection,
    backtest_run_id: str,
    db_id: int,
    exit_ts: str,
    **exit_indicators: Any,
) -> None:
    """
    Mark a backtest position as expired (0DTE forced expiry at end of run).
    Updates status + exit_ts + exit_reason='expired_0dte' in backtest_positions.
    Full credit is retained (total_credit already collected at entry).
    Also stores exit indicator snapshots via **exit_indicators.
    """
    exit_cols = ", ".join(f"{k} = ?" for k in exit_indicators)
    exit_vals = list(exit_indicators.values())
    conn.execute(
        f"""
        UPDATE backtest_positions
        SET status='expired', exit_ts=?, exit_reason='expired_0dte'
            {', ' + exit_cols if exit_cols else ''}
        WHERE id=? AND backtest_run_id=?
        """,
        (exit_ts, *exit_vals, db_id, backtest_run_id),
    )


# ---------------------------------------------------------------------------
# Run ID helpers
# ---------------------------------------------------------------------------

def get_backtest_run_id(date: str) -> str:
    """
    Generate a unique run ID for this backtest session.

    Format: {date}_{HHMMSS}  e.g. 2026-05-15_135300
    """
    return f"{date}_{datetime.now().strftime('%H%M%S')}"
