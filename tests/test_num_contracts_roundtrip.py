"""
tests/test_num_contracts_roundtrip.py

Regression tests for the cloud-mode exit-quantity bug.

Symptom: opening N contracts but exiting only 1 after the engine reloads
positions from the DB (which happens on every restart — the norm in cloud
mode). The exit path uses ``pos.num_contracts`` (engine._on_exit ->
execute_exit(quantity=pos.num_contracts)), so if a reloaded position loses its
contract count it silently under-closes the spread, leaving a naked residual.

Root cause was two layers that both dropped the column:
  1. trades_db._row_to_position() never mapped ``num_contracts`` from the row,
     so the Position dataclass fell back to its default of 1.
  2. position_store.PositionStore.load_open() never forwarded num_contracts to
     the reconstructed TradePosition, so it also defaulted to 1.

Local mode masked the bug because positions stay in memory for the life of the
process and are never reloaded from the DB within a single run.
"""
import tempfile
from pathlib import Path

import pytest

import trades_db
from trades_db import Position, get_conn, get_open_positions, init_db, insert_position
from position_store import PositionStore


@pytest.fixture
def temp_db(monkeypatch):
    """Isolated positions.db. _row_to_position reads PRAGMA off the module-level
    DB_PATH, so we must repoint it as well as pass the path explicitly."""
    tmp = Path(tempfile.mkdtemp()) / "positions.db"
    monkeypatch.setattr(trades_db, "DB_PATH", tmp)
    init_db(tmp)
    return tmp


def _make_position(num_contracts: int) -> Position:
    return Position(
        id=None, task_id="T", ticker="SPX", side="PUT",
        short_strike=5000, long_strike=4990,
        open_time="2026-07-21T10:00:00", close_time=None,
        credit=1.50, debit=None, status="open",
        layer=1, notes="regression", num_contracts=num_contracts,
    )


def test_row_to_position_preserves_num_contracts(temp_db):
    """DB layer: a 10-contract position must read back as 10, not the default 1."""
    with get_conn(temp_db) as conn:
        insert_position(conn, _make_position(10))
        conn.commit()

    with get_conn(temp_db) as conn:
        rows = get_open_positions(conn)

    assert len(rows) == 1
    assert rows[0].num_contracts == 10


def test_load_open_preserves_num_contracts(temp_db):
    """Store layer: PositionStore.load_open() must carry num_contracts through so
    the exit path closes the same quantity it opened."""
    with get_conn(temp_db) as conn:
        insert_position(conn, _make_position(10))
        conn.commit()

    store = PositionStore(db_path=temp_db)
    store.load_open()

    assert len(store._positions) == 1
    # This is the value execute_exit() sends as the close quantity.
    assert store._positions[0].num_contracts == 10


def test_single_contract_still_roundtrips(temp_db):
    """Sanity: the default/1-contract case is unaffected by the fix."""
    with get_conn(temp_db) as conn:
        insert_position(conn, _make_position(1))
        conn.commit()

    store = PositionStore(db_path=temp_db)
    store.load_open()

    assert store._positions[0].num_contracts == 1
