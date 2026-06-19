"""
backtests_db.py — SQLite database for backtest runs and results.

Similar to trades_db.py but separate schema for historical backtesting.
Each backtest run gets a unique ID and tracks signals + positions for that date.

Schema:
  - backtests: Metadata for each backtest run (id, date, start_time, end_time, stats)
  - backtest_signals: Entry signals generated during backtest (backtest_id, strike, premium, etc.)
  - backtest_positions: Positions taken during backtest (backtest_id, entry, exit, P&L)
"""
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any
import logging

from config import CONFIG

_LOG = logging.getLogger(__name__)

# Get backtests DB path from config
_BACKTESTS_DB_PATH = Path(CONFIG.get("backtesting", {}).get("db_path", "data/backtests.db"))


def get_backtests_db_path() -> Path:
    """Return backtests.db path, creating directory if needed."""
    db_dir = _BACKTESTS_DB_PATH.parent
    db_dir.mkdir(parents=True, exist_ok=True)
    return _BACKTESTS_DB_PATH


def init_backtests_db() -> None:
    """Create backtests schema if not exists."""
    db_path = get_backtests_db_path()

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Backtests metadata table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backtests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            backtest_date TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            total_signals INTEGER DEFAULT 0,
            total_entries INTEGER DEFAULT 0,
            total_exits INTEGER DEFAULT 0,
            total_trades INTEGER DEFAULT 0,
            winning_trades INTEGER DEFAULT 0,
            losing_trades INTEGER DEFAULT 0,
            total_pnl REAL DEFAULT 0.0,
            max_drawdown REAL DEFAULT 0.0,
            notes TEXT,
            status TEXT DEFAULT 'running'
        )
    """)

    # Backtest signals table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backtest_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            backtest_id INTEGER NOT NULL,
            signal_time TEXT NOT NULL,
            side TEXT NOT NULL,
            strike REAL NOT NULL,
            premium REAL NOT NULL,
            call_spread TEXT,
            put_spread TEXT,
            rsi REAL,
            vix REAL,
            reason TEXT,
            FOREIGN KEY (backtest_id) REFERENCES backtests(id)
        )
    """)

    # Backtest positions table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backtest_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            backtest_id INTEGER NOT NULL,
            position_id TEXT UNIQUE NOT NULL,
            side TEXT NOT NULL,
            strike REAL NOT NULL,
            entry_time TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_time TEXT,
            exit_price REAL,
            pnl REAL,
            pnl_pct REAL,
            status TEXT DEFAULT 'open',
            FOREIGN KEY (backtest_id) REFERENCES backtests(id)
        )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_backtest_date ON backtests(backtest_date)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_backtest_signals_id ON backtest_signals(backtest_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_backtest_positions_id ON backtest_positions(backtest_id)")

    conn.commit()
    conn.close()

    _LOG.info(f"Backtests DB initialized: {db_path}")


def get_conn() -> sqlite3.Connection:
    """Get connection to backtests DB."""
    db_path = get_backtests_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def create_backtest(backtest_date: str) -> int:
    """
    Create a new backtest run record.

    Args:
        backtest_date: Date string (YYYY-MM-DD)

    Returns:
        Backtest ID
    """
    conn = get_conn()
    cursor = conn.cursor()

    created_at = datetime.now().isoformat()

    cursor.execute("""
        INSERT INTO backtests (backtest_date, created_at, status)
        VALUES (?, ?, 'running')
    """, (backtest_date, created_at))

    conn.commit()
    backtest_id = cursor.lastrowid
    conn.close()

    _LOG.info(f"Created backtest #{backtest_id} for {backtest_date}")
    return backtest_id


def record_signal(backtest_id: int, signal_time: str, side: str, strike: float,
                  premium: float, rsi: Optional[float] = None, vix: Optional[float] = None,
                  reason: Optional[str] = None) -> int:
    """Record a signal generated during backtest."""
    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO backtest_signals
        (backtest_id, signal_time, side, strike, premium, rsi, vix, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (backtest_id, signal_time, side, strike, premium, rsi, vix, reason))

    conn.commit()
    signal_id = cursor.lastrowid
    conn.close()

    return signal_id


def record_position(backtest_id: int, position_id: str, side: str, strike: float,
                    entry_time: str, entry_price: float) -> int:
    """Record a position opened during backtest."""
    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO backtest_positions
        (backtest_id, position_id, side, strike, entry_time, entry_price, status)
        VALUES (?, ?, ?, ?, ?, ?, 'open')
    """, (backtest_id, position_id, side, strike, entry_time, entry_price))

    conn.commit()
    pos_id = cursor.lastrowid
    conn.close()

    return pos_id


def close_position(backtest_id: int, position_id: str, exit_time: str,
                   exit_price: float, pnl: float) -> None:
    """Mark a position as closed."""
    conn = get_conn()
    cursor = conn.cursor()

    # Get entry price for P&L calculation
    cursor.execute("""
        SELECT entry_price FROM backtest_positions
        WHERE backtest_id = ? AND position_id = ?
    """, (backtest_id, position_id))

    row = cursor.fetchone()
    if not row:
        _LOG.warning(f"Position {position_id} not found in backtest {backtest_id}")
        conn.close()
        return

    entry_price = row["entry_price"]
    pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price != 0 else 0

    cursor.execute("""
        UPDATE backtest_positions
        SET exit_time = ?, exit_price = ?, pnl = ?, pnl_pct = ?, status = 'closed'
        WHERE backtest_id = ? AND position_id = ?
    """, (exit_time, exit_price, pnl, pnl_pct, backtest_id, position_id))

    conn.commit()
    conn.close()


def finalize_backtest(backtest_id: int) -> Dict[str, Any]:
    """
    Finalize backtest run: calculate stats and mark complete.

    Returns:
        Dict with summary statistics
    """
    conn = get_conn()
    cursor = conn.cursor()

    # Get all closed positions
    cursor.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winners,
               SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losers,
               SUM(pnl) as total_pnl,
               MIN(pnl) as worst_trade
        FROM backtest_positions
        WHERE backtest_id = ? AND status = 'closed'
    """, (backtest_id,))

    stats_row = cursor.fetchone()
    total_trades = stats_row["total"] or 0
    winning_trades = stats_row["winners"] or 0
    losing_trades = stats_row["losers"] or 0
    total_pnl = stats_row["total_pnl"] or 0.0

    # Get signal counts
    cursor.execute("""
        SELECT COUNT(*) as total_signals FROM backtest_signals
        WHERE backtest_id = ?
    """, (backtest_id,))

    signal_count = cursor.fetchone()["total_signals"] or 0

    # Count entries/exits
    cursor.execute("""
        SELECT COUNT(*) as entries FROM backtest_positions
        WHERE backtest_id = ? AND status = 'open'
    """, (backtest_id,))

    open_positions = cursor.fetchone()["entries"] or 0

    completed_at = datetime.now().isoformat()

    cursor.execute("""
        UPDATE backtests
        SET completed_at = ?,
            total_signals = ?,
            total_entries = ?,
            total_exits = ?,
            total_trades = ?,
            winning_trades = ?,
            losing_trades = ?,
            total_pnl = ?,
            status = 'completed'
        WHERE id = ?
    """, (completed_at, signal_count, open_positions + total_trades, total_trades,
          total_trades, winning_trades, losing_trades, total_pnl, backtest_id))

    conn.commit()
    conn.close()

    stats = {
        "backtest_id": backtest_id,
        "total_signals": signal_count,
        "total_entries": open_positions + total_trades,
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate": (winning_trades / total_trades * 100) if total_trades > 0 else 0,
        "total_pnl": total_pnl,
        "avg_pnl_per_trade": (total_pnl / total_trades) if total_trades > 0 else 0,
    }

    _LOG.info(f"Backtest #{backtest_id} completed: {stats}")
    return stats


def get_backtest(backtest_id: int) -> Optional[Dict[str, Any]]:
    """Get backtest metadata."""
    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM backtests WHERE id = ?", (backtest_id,))
    row = cursor.fetchone()
    conn.close()

    return dict(row) if row else None


def list_backtests(limit: int = 10) -> List[Dict[str, Any]]:
    """List recent backtests."""
    conn = get_conn()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM backtests
        ORDER BY created_at DESC
        LIMIT ?
    """, (limit,))

    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return rows
