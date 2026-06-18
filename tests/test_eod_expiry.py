"""
Tests for EOD expiry sweep:
  - get_open_positions_for_date() returns only open positions for the given date
  - Engine._close_expired_positions() marks them expired, sets pnl, inserts EXPIRE signal
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_root = Path(__file__).parent.parent
sys.path.insert(0, str(_root / "src"))

from trades_db import (
    get_conn,
    init_db,
    insert_position,
    get_open_positions_for_date,
    Position,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> Path:
    db = tmp_path / "test_positions.db"
    with get_conn(db) as conn:
        init_db(db)
    return db


def _insert_pos(db: Path, order_time: str, status: str = "open", side: str = "PUT",
                short_strike: float = 5500.0, long_strike: float = 5480.0,
                credit: float = 0.30, num_contracts: int = 1) -> int:
    # Use order_time as open_time to satisfy UNIQUE(task_id, ticker, open_time)
    pos = Position(
        task_id="TEST", ticker="SPX", side=side,
        short_strike=short_strike, long_strike=long_strike,
        open_time=order_time, credit=credit, num_contracts=num_contracts,
        status=status, order_time=order_time,
    )
    with get_conn(db) as conn:
        pid = insert_position(conn, pos)
        conn.commit()
    return pid


# ---------------------------------------------------------------------------
# get_open_positions_for_date
# ---------------------------------------------------------------------------

def test_returns_only_matching_date(tmp_path):
    db = _make_db(tmp_path)
    _insert_pos(db, order_time="2026-06-02T09:37:00-04:00")
    _insert_pos(db, order_time="2026-06-03T09:45:00-04:00")

    with get_conn(db) as conn:
        june2 = get_open_positions_for_date(conn, "2026-06-02")
        june3 = get_open_positions_for_date(conn, "2026-06-03")
        other = get_open_positions_for_date(conn, "2026-06-01")

    assert len(june2) == 1
    assert len(june3) == 1
    assert len(other) == 0


def test_excludes_already_closed(tmp_path):
    db = _make_db(tmp_path)
    _insert_pos(db, order_time="2026-06-02T09:37:00-04:00", status="open")
    # Manually close one
    with get_conn(db) as conn:
        pid = _insert_pos(db, order_time="2026-06-02T10:00:00-04:00", status="open")
        conn.execute("UPDATE positions SET status = 'closed' WHERE id = ?", (pid,))
        conn.commit()

    with get_conn(db) as conn:
        rows = get_open_positions_for_date(conn, "2026-06-02")

    assert len(rows) == 1


# ---------------------------------------------------------------------------
# _close_expired_positions integration (uses in-memory DB via monkeypatch)
# ---------------------------------------------------------------------------

def test_close_expired_marks_expired_and_inserts_signal(tmp_path):
    db = _make_db(tmp_path)
    pid = _insert_pos(
        db,
        order_time="2026-06-02T09:37:00-04:00",
        credit=0.25,
        num_contracts=1,
    )

    # Build a minimal engine stub — no IBKR, no config deps
    with patch("engine.CONFIG", {
        "dry_run": True,
        "ibkr": {"host": "127.0.0.1", "port": 7497, "engine_client_id": 1, "account_id": None, "scanner_client_id": 15},
        "market": {"entry_start": "09:30", "entry_end": "15:00"},
        "engine": {"check_interval_seconds": 30},
        "entry": {
            "spread_width_primary": 10, "spread_width_fallback": 20,
            "short_delta_target": 0.03, "contracts_per_trade": 1,
            "vix_buckets": {},
        },
        "paths": {"logs": "logs"},
        "telegram": {"dry_run": True},
    }), patch("engine.DRY_RUN", True), \
       patch("engine.get_engine_logger", return_value=MagicMock()):

        from engine import AutoTraderEngine
        eng = AutoTraderEngine.__new__(AutoTraderEngine)
        eng.dry_run = True
        eng.logger = MagicMock()
        eng._TASK_ID = "TEST"
        eng._eod_expiry_done = set()
        eng.store = MagicMock()

        # Pass _db_path so the engine writes to the test DB, not production
        eng._close_expired_positions(
            trade_date="2026-06-02",
            spx=5600.0, em=10.0, gex_val=5.0, vix=18.0, rsi=55.0,
            _db_path=db,
        )

    with get_conn(db) as conn:
        pos_row = conn.execute(
            "SELECT status, pnl, close_time, notes, debit, max_profit FROM positions WHERE id = ?", (pid,)
        ).fetchone()
        sig_row = conn.execute(
            "SELECT action, signal_reason, short_strike, credit FROM signals ORDER BY id DESC LIMIT 1"
        ).fetchone()

    status, pnl, close_time, notes, debit, max_profit = pos_row

    assert status == "expired"
    assert pnl == pytest.approx(25.0)        # 0.25 credit × 100 × 1 contract = max profit
    assert close_time is not None
    assert "expired worthless" in (notes or "")
    assert "100% max profit" in (notes or "")
    assert debit == pytest.approx(0.0)        # expired worthless → no closing cost
    assert max_profit == pytest.approx(25.0)  # backfilled at expiry if not set at entry

    assert sig_row[0] == "EXPIRE"
    assert sig_row[1] == "eod_expiry"
    assert sig_row[2] == pytest.approx(5500.0)
    assert sig_row[3] == pytest.approx(0.25)
