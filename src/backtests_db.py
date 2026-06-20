"""
backtests_db.py — registry of backtest runs.

Each backtest run gets a unique id and its OWN isolated positions database that
uses the EXACT same schema as live trading (trades_db). This is deliberate:
"similar to what we have now" means the real positions/signals tables, not a
parallel schema. The engine runs in backtest mode against that isolated DB, so
backtest results are recorded by the same code paths that record live trades.

Layout:
  data/backtests/index.db                 — this registry (backtest_runs table)
  data/backtests/run_<id>_<date>.db        — one run's positions/signals (trades_db schema)
"""
import sqlite3
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict, Any
import logging

from config import CONFIG

_LOG = logging.getLogger(__name__)

# Registry audit timestamps are stamped in Eastern time for readability.
_ET = ZoneInfo("America/New_York")


def _now_et_iso() -> str:
    return datetime.now(_ET).isoformat(timespec="seconds")

# Backtests live under the engine's data dir (config: backtesting.db_path's parent)
_ENGINE_ROOT = Path(__file__).parent.parent
_BACKTESTS_DIR = _ENGINE_ROOT / "data" / "backtests"
_INDEX_DB = _BACKTESTS_DIR / "index.db"


def _ensure_dir() -> None:
    _BACKTESTS_DIR.mkdir(parents=True, exist_ok=True)


def init_registry() -> None:
    """Create the backtest_runs registry table if absent."""
    _ensure_dir()
    conn = sqlite3.connect(_INDEX_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtest_runs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            backtest_date TEXT NOT NULL,
            db_path       TEXT,
            created_at    TEXT NOT NULL,
            completed_at  TEXT,
            status        TEXT DEFAULT 'running',
            ticks         INTEGER DEFAULT 0,
            total_signals INTEGER DEFAULT 0,
            total_positions INTEGER DEFAULT 0,
            open_positions  INTEGER DEFAULT 0,
            closed_positions INTEGER DEFAULT 0,
            total_pnl     REAL DEFAULT 0.0
        )
    """)
    conn.commit()
    conn.close()
    _LOG.info(f"Backtest registry ready: {_INDEX_DB}")


def create_run(backtest_date: str) -> tuple[int, Path]:
    """
    Register a new backtest run and return (run_id, isolated_db_path).

    The isolated DB uses the trades_db schema and is created fresh per run so
    runs never mix. A second backtest of the same date gets a distinct id+file.
    """
    _ensure_dir()
    created_at = _now_et_iso()
    conn = sqlite3.connect(_INDEX_DB)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO backtest_runs (backtest_date, created_at, status) VALUES (?, ?, 'running')",
        (backtest_date, created_at),
    )
    run_id = cur.lastrowid
    db_path = _BACKTESTS_DIR / f"run_{run_id}_{backtest_date}.db"
    cur.execute("UPDATE backtest_runs SET db_path = ? WHERE id = ?", (str(db_path), run_id))
    conn.commit()
    conn.close()
    _LOG.info(f"Backtest run #{run_id} registered for {backtest_date} → {db_path}")
    return run_id, db_path


def finalize_run(run_id: int, db_path: Path, ticks: int) -> Dict[str, Any]:
    """
    Compute summary stats from the run's isolated positions DB and update the
    registry. Stats come from the real positions/signals tables.
    """
    stats = _summarize_positions(db_path)
    stats["ticks"] = ticks
    stats["run_id"] = run_id

    conn = sqlite3.connect(_INDEX_DB)
    conn.execute(
        """
        UPDATE backtest_runs
        SET completed_at = ?, status = 'completed', ticks = ?,
            total_signals = ?, total_positions = ?, open_positions = ?,
            closed_positions = ?, total_pnl = ?
        WHERE id = ?
        """,
        (
            _now_et_iso(), ticks,
            stats["total_signals"], stats["total_positions"],
            stats["open_positions"], stats["closed_positions"],
            stats["total_pnl"], run_id,
        ),
    )
    conn.commit()
    conn.close()
    _LOG.info(f"Backtest run #{run_id} finalized: {stats}")
    return stats


def _summarize_positions(db_path: Path) -> Dict[str, Any]:
    """Read counts and P&L from a run's positions/signals tables."""
    out = dict(total_signals=0, total_positions=0, open_positions=0,
               closed_positions=0, total_pnl=0.0, winning=0, losing=0)
    if not Path(db_path).exists():
        return out
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    def _count(sql: str) -> int:
        try:
            return cur.execute(sql).fetchone()[0] or 0
        except sqlite3.OperationalError:
            return 0

    out["total_positions"] = _count("SELECT COUNT(*) FROM positions")
    out["open_positions"] = _count(
        "SELECT COUNT(*) FROM positions WHERE status IN ('open','pending_open')"
    )
    # Any terminal status (closed, expired, ...) counts as closed for the summary.
    out["closed_positions"] = _count(
        "SELECT COUNT(*) FROM positions WHERE status NOT IN ('open','pending_open')"
    )
    out["total_signals"] = _count("SELECT COUNT(*) FROM signals")
    out["winning"] = _count("SELECT COUNT(*) FROM positions WHERE pnl > 0")
    out["losing"] = _count("SELECT COUNT(*) FROM positions WHERE pnl < 0")
    try:
        out["total_pnl"] = round(
            cur.execute("SELECT COALESCE(SUM(pnl),0) FROM positions").fetchone()[0] or 0.0, 2
        )
    except sqlite3.OperationalError:
        pass

    conn.close()
    return out


def list_runs(limit: int = 20) -> List[Dict[str, Any]]:
    """List recent backtest runs from the registry."""
    if not _INDEX_DB.exists():
        return []
    conn = sqlite3.connect(_INDEX_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
