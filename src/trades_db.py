"""
trades_db.py — positions.db schema + read/write helpers.

TASK-2026-174: Unified schema with backtest.db.
  - positions table now has total_credit (net = credit - debit)
  - positions table now has exit_gex_regime in exit snapshot

TASK-2026-179: Pending-state protocol for live trading.
  - New status values: pending_open, pending_close, rejected, cancelled, timeout
  - New columns: order_id, order_action, fill_price, fill_time, order_time
  - update_position_fill() helper to record fill confirmation
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent.parent / "data" / "positions.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT    NOT NULL,
    ticker              TEXT    NOT NULL,
    side                TEXT    NOT NULL,
    short_strike        REAL    NOT NULL,
    long_strike         REAL,
    open_time           TEXT    NOT NULL,
    close_time          TEXT,
    credit              REAL    NOT NULL,
    debit               REAL,
    total_credit        REAL,
    status              TEXT    NOT NULL DEFAULT 'open',
    pnl                 REAL,
    max_profit          REAL,
    max_loss            REAL,
    layer               INTEGER,
    notes               TEXT,

    -- Entry market snapshot
    entry_spx_spot      REAL,
    entry_vix           REAL,
    entry_em            REAL,
    entry_gex           REAL,
    entry_bb_position   REAL,
    entry_bb_expanding  INTEGER,
    entry_adx           REAL,
    entry_macd_hist     REAL,
    entry_rsi           REAL,
    entry_atm_call_mid  REAL,
    entry_atm_put_mid   REAL,
    entry_atm_strike    REAL,

    -- Exit market snapshot
    exit_spx_spot       REAL,
    exit_vix            REAL,
    exit_em             REAL,
    exit_bb_position    REAL,
    exit_rsi            REAL,
    exit_adx            REAL,
    exit_macd_hist      REAL,

    -- Exit decision metadata
    exit_layer          INTEGER,
    exit_conditions_met INTEGER,

    -- Regime metadata
    entry_regime        TEXT,
    entry_gex_regime    TEXT,
    entry_zero_gamma_dist REAL,
    exit_regime         TEXT,
    exit_gex_regime     TEXT,

    num_contracts       INTEGER NOT NULL DEFAULT 1,

    -- TASK-2026-179: IBKR order tracking
    order_id            INTEGER,
    order_action        TEXT,
    fill_price          REAL,
    fill_time           TEXT,
    order_time          TEXT,

    UNIQUE(task_id, ticker, open_time)
);

CREATE TABLE IF NOT EXISTS signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT    NOT NULL,
    layer               INTEGER NOT NULL,
    spx_spot            REAL    NOT NULL,
    em                  REAL,
    gex                 REAL,

    -- Decision: was a signal generated?
    signalled           INTEGER DEFAULT 0,
    signal_reason       TEXT,

    -- Per-condition flags (all INTEGER, 0=fail, 1=pass)
    premium_passed      INTEGER DEFAULT 0,
    distance_passed     INTEGER DEFAULT 0,
    collision_passed    INTEGER DEFAULT 0,

    -- Market context at signal time
    vix                 REAL,
    rsi                 REAL,

    -- Execution results
    filled              INTEGER DEFAULT 0,
    short_strike        REAL,
    long_strike         REAL,
    credit              REAL,

    -- Why NOT filled (if signalled but not filled)
    blocked_reason      TEXT,

    action              TEXT,
    task_id             TEXT,
    UNIQUE(timestamp, layer, signal_reason)
);
"""


def get_conn(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db(path: Path = DB_PATH) -> None:
    """
    Create tables if they don't exist.
    On existing databases, run ALTER TABLE migrations to add new columns
    added in schema updates without dropping existing data.
    """
    # Positions new columns
    POSITION_NEW_COLS = [
        ("num_contracts",        "INTEGER NOT NULL DEFAULT 1"),
        ("entry_regime",         "TEXT"),
        ("entry_gex_regime",     "TEXT"),
        ("entry_zero_gamma_dist","REAL"),
        ("exit_regime",          "TEXT"),
        ("exit_rsi",             "REAL"),
        ("total_credit",         "REAL"),
        ("exit_gex_regime",      "TEXT"),
        # TASK-2026-179: IBKR order tracking
        ("order_id",            "INTEGER"),
        ("order_action",        "TEXT"),
        ("fill_price",          "REAL"),
        ("fill_time",           "TEXT"),
        ("order_time",          "TEXT"),
    ]
    SIGNAL_NEW_COLS = [
        ("spx_spot",             "REAL    NOT NULL"),
        ("em",                   "REAL"),
        ("gex",                  "REAL"),
        ("vix",                  "REAL"),
        ("rsi",                  "REAL"),
        ("signalled",            "INTEGER DEFAULT 0"),
        ("signal_reason",        "TEXT"),
        ("premium_passed",       "INTEGER DEFAULT 0"),
        ("distance_passed",      "INTEGER DEFAULT 0"),
        ("collision_passed",     "INTEGER DEFAULT 0"),
        ("filled",               "INTEGER DEFAULT 0"),
        ("short_strike",         "REAL"),
        ("long_strike",          "REAL"),
        ("credit",               "REAL"),
        ("blocked_reason",       "TEXT"),
        ("action",               "TEXT"),
        ("task_id",              "TEXT"),
    ]

    with get_conn(path) as conn:
        conn.executescript(SCHEMA)

        # Migrate positions: add any new columns that don't exist yet
        existing_pos = {r[1] for r in conn.execute("PRAGMA table_info(positions)")}
        for col_name, col_type in POSITION_NEW_COLS:
            if col_name not in existing_pos:
                conn.execute(
                    f"ALTER TABLE positions ADD COLUMN {col_name} {col_type}"
                )
                if col_name == "num_contracts":
                    conn.execute(
                        "UPDATE positions SET num_contracts = 1 WHERE num_contracts IS NULL"
                    )
                elif col_name == "total_credit":
                    conn.execute("""
                        UPDATE positions
                        SET total_credit = COALESCE(credit, 0) - COALESCE(debit, 0)
                        WHERE total_credit IS NULL
                    """)

        # Migrate signals: add any new columns that don't exist yet
        existing_sig = {r[1] for r in conn.execute("PRAGMA table_info(signals)")}
        for col_name, col_type in SIGNAL_NEW_COLS:
            if col_name not in existing_sig:
                conn.execute(
                    f"ALTER TABLE signals ADD COLUMN {col_name} {col_type}"
                )

        conn.commit()


# ---------------------------------------------------------------------------
# Position dataclass
# ---------------------------------------------------------------------------

@dataclass
class Position:
    task_id:             str
    ticker:              str
    side:                str
    short_strike:        float
    long_strike:         Optional[float] = None
    open_time:           str = ""
    close_time:          Optional[str] = None
    credit:              float = 0.0
    debit:               Optional[float] = None
    total_credit:        Optional[float] = None
    status:              str = "open"
    pnl:                 Optional[float] = None
    max_profit:          Optional[float] = None
    max_loss:            Optional[float] = None
    layer:               Optional[int] = None
    notes:               Optional[str] = None
    id:                  Optional[int] = None
    num_contracts:       int = 1

    # Entry market snapshot
    entry_spx_spot:      Optional[float] = None
    entry_vix:           Optional[float] = None
    entry_em:            Optional[float] = None
    entry_gex:           Optional[float] = None
    entry_bb_position:   Optional[float] = None
    entry_bb_expanding:  Optional[int] = None
    entry_adx:           Optional[float] = None
    entry_macd_hist:     Optional[float] = None
    entry_rsi:           Optional[float] = None
    entry_atm_call_mid:  Optional[float] = None
    entry_atm_put_mid:   Optional[float] = None
    entry_atm_strike:    Optional[float] = None

    # Exit market snapshot
    exit_spx_spot:       Optional[float] = None
    exit_vix:            Optional[float] = None
    exit_em:             Optional[float] = None
    exit_bb_position:    Optional[float] = None
    exit_rsi:            Optional[float] = None
    exit_adx:            Optional[float] = None
    exit_macd_hist:      Optional[float] = None

    # Exit decision metadata
    exit_layer:          Optional[int] = None
    exit_conditions_met: Optional[int] = None

    # Regime metadata
    entry_regime:         Optional[str] = None
    entry_gex_regime:     Optional[str] = None
    entry_zero_gamma_dist: Optional[float] = None
    exit_regime:          Optional[str] = None
    exit_gex_regime:      Optional[str] = None

    # TASK-2026-179: IBKR order tracking
    order_id:             Optional[int] = None
    order_action:        Optional[str] = None
    fill_price:          Optional[float] = None
    fill_time:           Optional[str] = None
    order_time:          Optional[str] = None


def _row_to_position(row: tuple) -> Position:
    """Map a DB row to a Position dataclass."""
    conn_tmp = sqlite3.connect(str(DB_PATH))
    try:
        pragma_rows = conn_tmp.execute("PRAGMA table_info(positions)").fetchall()
    finally:
        conn_tmp.close()
    real_cols = {}
    for i, r in enumerate(pragma_rows):
        real_cols[r[1]] = i

    def g(idx_or_name, default=None):
        if isinstance(idx_or_name, int):
            idx = idx_or_name
            return row[idx] if len(row) > idx else default
        else:
            idx = real_cols.get(idx_or_name, -1)
            return row[idx] if idx >= 0 and len(row) > idx else default

    return Position(
        id=g(0),
        task_id=g(1), ticker=g(2), side=g(3),
        short_strike=g(4), long_strike=g(5),
        open_time=g(6), close_time=g(7),
        credit=g(8), debit=g(9),
        total_credit=g("total_credit", None),
        status=g("status"), pnl=g("pnl"),
        max_profit=g("max_profit"), max_loss=g("max_loss"),
        layer=g("layer"), notes=g("notes"),

        # Entry snapshot
        entry_spx_spot=g("entry_spx_spot"),
        entry_vix=g("entry_vix"),
        entry_em=g("entry_em"),
        entry_gex=g("entry_gex"),
        entry_bb_position=g("entry_bb_position"),
        entry_bb_expanding=g("entry_bb_expanding"),
        entry_adx=g("entry_adx"),
        entry_macd_hist=g("entry_macd_hist"),
        entry_rsi=g("entry_rsi"),
        entry_atm_call_mid=g("entry_atm_call_mid"),
        entry_atm_put_mid=g("entry_atm_put_mid"),
        entry_atm_strike=g("entry_atm_strike"),

        # Exit snapshot
        exit_spx_spot=g("exit_spx_spot"),
        exit_vix=g("exit_vix"),
        exit_em=g("exit_em"),
        exit_bb_position=g("exit_bb_position"),
        exit_rsi=g("exit_rsi"),
        exit_adx=g("exit_adx"),
        exit_macd_hist=g("exit_macd_hist"),

        # Exit metadata
        exit_layer=g("exit_layer"),
        exit_conditions_met=g("exit_conditions_met"),

        # Regime metadata
        entry_regime=g("entry_regime"),
        entry_gex_regime=g("entry_gex_regime"),
        entry_zero_gamma_dist=g("entry_zero_gamma_dist"),
        exit_regime=g("exit_regime"),
        exit_gex_regime=g("exit_gex_regime"),

        # TASK-2026-179: IBKR order tracking
        order_id=g("order_id"),
        order_action=g("order_action"),
        fill_price=g("fill_price"),
        fill_time=g("fill_time"),
        order_time=g("order_time"),
    )


def insert_position(conn: sqlite3.Connection, pos: Position) -> int:
    """
    Insert a position row.
    TASK-2026-174: total_credit is written as net (credit - debit).
    TASK-2026-179: order_id, order_action, order_time written at insert time.
    """
    total_credit = pos.total_credit
    if total_credit is None:
        total_credit = (pos.credit or 0.0) - (pos.debit or 0.0)

    return conn.execute(
        """
        INSERT OR IGNORE INTO positions
            (task_id, ticker, side, short_strike, long_strike, open_time,
             close_time, credit, debit, total_credit, status, pnl, max_profit,
             max_loss, layer, notes, num_contracts,
             entry_spx_spot, entry_vix, entry_em, entry_gex,
             entry_bb_position, entry_bb_expanding, entry_adx,
             entry_macd_hist, entry_rsi, entry_atm_call_mid,
             entry_atm_put_mid, entry_atm_strike,
             exit_spx_spot, exit_vix, exit_em, exit_bb_position,
             exit_rsi, exit_adx, exit_macd_hist,
             exit_layer, exit_conditions_met,
             entry_regime, entry_gex_regime, entry_zero_gamma_dist,
             exit_regime, exit_gex_regime,
             order_id, order_action, fill_price, fill_time, order_time)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
             ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
             ?, ?, ?, ?, ?, ?, ?, ?,
             ?, ?, ?, ?, ?, ?,
             ?, ?, ?, ?, ?)
        """,
        (
            pos.task_id, pos.ticker, pos.side,
            pos.short_strike, pos.long_strike, pos.open_time,
            pos.close_time, pos.credit, pos.debit, total_credit, pos.status,
            pos.pnl, pos.max_profit, pos.max_loss,
            pos.layer, pos.notes, pos.num_contracts,

            pos.entry_spx_spot, pos.entry_vix, pos.entry_em, pos.entry_gex,
            pos.entry_bb_position, pos.entry_bb_expanding, pos.entry_adx,
            pos.entry_macd_hist, pos.entry_rsi, pos.entry_atm_call_mid,
            pos.entry_atm_put_mid, pos.entry_atm_strike,

            pos.exit_spx_spot, pos.exit_vix, pos.exit_em,
            pos.exit_bb_position, pos.exit_rsi,
            pos.exit_adx, pos.exit_macd_hist,
            pos.exit_layer, pos.exit_conditions_met,

            pos.entry_regime, pos.entry_gex_regime,
            pos.entry_zero_gamma_dist, pos.exit_regime,
            pos.exit_gex_regime,

            # TASK-2026-179: IBKR order tracking
            pos.order_id, pos.order_action,
            pos.fill_price, pos.fill_time, pos.order_time,
        ),
    ).lastrowid


def update_position_status(
    conn: sqlite3.Connection,
    pos_id: int,
    status: str,
    close_time: Optional[str] = None,
    pnl: Optional[float] = None,
    notes: Optional[str] = None,
) -> None:
    """Update position status (and optionally close_time, pnl, notes)."""
    conn.execute(
        """
        UPDATE positions
        SET status = ?,
            close_time = COALESCE(?, close_time),
            pnl = COALESCE(?, pnl),
            notes = COALESCE(?, notes)
        WHERE id = ?
        """,
        (status, close_time, pnl, notes, pos_id),
    )


def update_position_fill(
    conn: sqlite3.Connection,
    pos_id: int,
    fill_price: float,
    fill_time: str,
) -> None:
    """
    Record fill confirmation on a pending position.
    TASK-2026-179: called by engine when polling detects fill.
    """
    conn.execute(
        """
        UPDATE positions
        SET fill_price = ?, fill_time = ?
        WHERE id = ?
        """,
        (fill_price, fill_time, pos_id),
    )


def update_position_order_id(
    conn: sqlite3.Connection,
    pos_id: int,
    order_id: int,
    order_action: str,
    order_time: str,
) -> None:
    """
    Record the IBKR order_id after order is placed.
    TASK-2026-179: called in _on_enter_approved after execute_entry() returns.
    """
    conn.execute(
        """
        UPDATE positions
        SET order_id = ?, order_action = ?, order_time = ?
        WHERE id = ?
        """,
        (order_id, order_action, order_time, pos_id),
    )


def update_position_exit_snapshot(
    conn: sqlite3.Connection,
    pos_id: int,
    exit_spx_spot: Optional[float],
    exit_vix: Optional[float],
    exit_em: Optional[float],
    exit_bb_position: Optional[float],
    exit_rsi: Optional[float],
    exit_adx: Optional[float],
    exit_macd_hist: Optional[float],
    exit_layer: Optional[int],
    exit_conditions_met: Optional[int],
    exit_regime: Optional[str] = None,
    exit_gex_regime: Optional[str] = None,
) -> None:
    conn.execute(
        """
        UPDATE positions
        SET exit_spx_spot = ?, exit_vix = ?, exit_em = ?,
            exit_bb_position = ?, exit_rsi = ?, exit_adx = ?,
            exit_macd_hist = ?, exit_layer = ?,
            exit_conditions_met = ?, exit_regime = ?,
            exit_gex_regime = ?
        WHERE id = ?
        """,
        (
            exit_spx_spot, exit_vix, exit_em,
            exit_bb_position, exit_rsi, exit_adx, exit_macd_hist,
            exit_layer, exit_conditions_met, exit_regime,
            exit_gex_regime,
            pos_id,
        ),
    )


def get_open_positions(conn: sqlite3.Connection) -> list[Position]:
    """Load all active positions: both 'open' (confirmed fills) and 'pending_open' (in-flight entries).

    TASK-2026-179: Must include pending_open so that after restart, in-flight orders
    are tracked for collision checking and exit signal processing.
    """
    rows = conn.execute(
        "SELECT * FROM positions WHERE status IN ('open', 'pending_open') ORDER BY open_time DESC"
    ).fetchall()
    return [_row_to_position(r) for r in rows]


def get_position_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM positions WHERE status = 'open'"
    ).fetchone()[0]


def get_open_positions_for_date(
    conn: sqlite3.Connection, trade_date: str
) -> list:
    """
    Return all open positions whose order_time falls on trade_date (YYYY-MM-DD).
    Used by the EOD expiry sweep to find positions that expired today.
    """
    rows = conn.execute(
        """
        SELECT * FROM positions
        WHERE status IN ('open', 'pending_open')
          AND date(order_time) = ?
        ORDER BY open_time DESC
        """,
        (trade_date,),
    ).fetchall()
    return [_row_to_position(r) for r in rows]


# ---------------------------------------------------------------------------
# Signal helpers (TASK-2026-126: redesigned with per-condition flags)
# ---------------------------------------------------------------------------

def insert_signal(
    conn: sqlite3.Connection,
    timestamp: str,
    layer: int,
    spx_spot: float,
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
    em: Optional[float] = None,
    gex: Optional[float] = None,
    vix: Optional[float] = None,
    rsi: Optional[float] = None,
    task_id: Optional[str] = None,
) -> int:
    """
    Insert a signal row with per-condition pass/fail flags.
    """
    return conn.execute(
        """
        INSERT OR IGNORE INTO signals
            (timestamp, layer, spx_spot, signalled, signal_reason,
             premium_passed, distance_passed, collision_passed,
             filled, short_strike, long_strike, credit,
             blocked_reason, action, em, gex, vix, rsi, task_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            timestamp, layer, spx_spot, signalled, signal_reason,
            premium_passed, distance_passed, collision_passed,
            filled, short_strike, long_strike, credit,
            blocked_reason, action, em, gex, vix, rsi, task_id,
        ),
    ).lastrowid


def mark_signal_filled(
    conn: sqlite3.Connection,
    signal_id: int,
) -> None:
    """Mark a signal as filled."""
    conn.execute("UPDATE signals SET filled = 1 WHERE id = ?", (signal_id,))


def mark_signal_blocked(
    conn: sqlite3.Connection,
    signal_id: int,
    blocked_reason: str,
) -> None:
    """Mark a signal as blocked (signalled but not filled)."""
    conn.execute(
        "UPDATE signals SET blocked_reason = ? WHERE id = ?",
        (blocked_reason, signal_id),
    )


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")