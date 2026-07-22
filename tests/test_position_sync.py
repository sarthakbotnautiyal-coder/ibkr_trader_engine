"""
Tests for IBKR ⇄ positions.db reconciliation (Engine._sync_positions_with_ibkr).

IBKR is the source of truth. positions.db stores SPREADS (short+long combo,
num_contracts lots); IBKR reqPositions() returns INDIVIDUAL legs (short leg
position=-N, long leg position=+N, right 'C'/'P'). These tests drive the
reconcile with crafted leg lists via a fake client and assert the DB outcome.

Covered:
  - qty match             → no change
  - qty mismatch (5 → 3)  → num_contracts updated to IBKR
  - DB open, leg absent   → status 'expired', full-credit P&L
  - orphan legs (no DB)   → auto-created (PUT −10 and CALL +20)
  - unpairable orphan leg → warning only, no DB write
  - dry_run               → no-op
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_root = Path(__file__).parent.parent
sys.path.insert(0, str(_root / "src"))

from trades_db import get_conn, init_db
from executor import today_expiry


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------

def _leg(right: str, strike: float, position: int, expiry: str | None = None):
    """Build a fake IBKR position (leg) object shaped like ib_async's Position."""
    contract = SimpleNamespace(
        symbol="SPX",
        right=right,
        strike=float(strike),
        lastTradeDateOrContractMonth=expiry or today_expiry(),
    )
    return SimpleNamespace(contract=contract, position=position)


def _make_engine(tmp_path: Path, ibkr_legs, dry_run: bool = False):
    """
    Build an AutoTraderEngine wired to a real PositionStore on a temp DB and a
    fake client returning `ibkr_legs` from get_open_positions_ibkr().
    """
    _cfg = {
        "dry_run": False,
        "ibkr": {"host": "127.0.0.1", "port": 7497, "engine_client_id": 1,
                 "account_id": None, "scanner_client_id": 15},
        "market": {"entry_start": "09:30", "entry_end": "15:00"},
        "engine": {"check_interval_seconds": 30},
        "entry": {
            "spread_width_primary": 10, "spread_width_fallback": 20,
            "short_delta_target": 0.03, "contracts_per_trade": 1,
            "vix_buckets": {},
        },
        "paths": {"logs": "logs"},
        "telegram": {"dry_run": True},
    }

    with patch("engine.CONFIG", _cfg), \
         patch("position_store.CONFIG", _cfg), \
         patch("engine.get_engine_logger", return_value=MagicMock()):

        from engine import AutoTraderEngine
        from position_store import PositionStore

        db = tmp_path / "positions.db"
        with get_conn(db) as conn:
            init_db(db)

        eng = AutoTraderEngine.__new__(AutoTraderEngine)
        eng.dry_run = dry_run
        eng.mode = "local"
        eng._clock = None
        eng._current_combined = None
        eng._TASK_ID = "TEST"
        eng.logger = MagicMock()
        eng._pending_exits = {}
        eng._pending_exit_times = {}

        eng.store = PositionStore(db_path=db)
        eng.store.init()
        eng.store.load_open()

        client = MagicMock()
        client.is_connected.return_value = True
        client.get_open_positions_ibkr.return_value = ibkr_legs
        client.query_order_status.return_value = None  # force best-guess fill path
        eng._client = client

    return eng, db, _cfg


def _add_open(eng, cfg, side: str, short: float, long: float,
              credit: float, num_contracts: int, status: str = "open") -> int:
    """Insert a spread into the store's DB (status 'open' or 'pending_open')."""
    from position_store import TradePosition, PositionSide
    with patch("position_store.CONFIG", cfg), \
         patch("position_store.build_market_snapshot",
               return_value=_empty_snap()):
        pos = TradePosition(
            task_id=f"T-{short}-{status}", ticker="SPX",
            side=PositionSide(side), short_strike=short, long_strike=long,
            credit=credit, num_contracts=num_contracts,
        )
        return eng.store.add_position(pos, status=status)


def _empty_snap():
    from position_store import MarketSnapshot
    return MarketSnapshot(
        spx_spot=0.0, vix=0.0, em=0.0, gex=0.0, bb_position=0.5,
        bb_expanding=0, adx=0.0, macd_hist=0.0, rsi=50.0,
        atm_call_mid=0.0, atm_put_mid=0.0, atm_strike=0.0,
    )


def _run_sync(eng, cfg, reason="test"):
    _tg = MagicMock(return_value=True)
    with patch("engine.CONFIG", cfg), \
         patch("position_store.CONFIG", cfg), \
         patch("position_store.build_market_snapshot", return_value=_empty_snap()), \
         patch("telegram_notifier.send_telegram_message", _tg):
        eng._sync_positions_with_ibkr(reason)
    return _tg


def _row(db, pid, cols="status, num_contracts, pnl"):
    with get_conn(db) as conn:
        return conn.execute(
            f"SELECT {cols} FROM positions WHERE id = ?", (pid,)
        ).fetchone()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_qty_match_no_change(tmp_path):
    # PUT spread 5500/5490, 2 lots → short leg -2, long leg +2.
    legs = [_leg("P", 5500, -2), _leg("P", 5490, +2)]
    eng, db, cfg = _make_engine(tmp_path, legs)
    pid = _add_open(eng, cfg, "PUT", 5500, 5490, credit=0.30, num_contracts=2)

    _run_sync(eng, cfg)

    status, n, pnl = _row(db, pid)
    assert status == "open"
    assert n == 2


def test_qty_mismatch_updates_to_ibkr(tmp_path):
    # DB says 5, IBKR holds 3 → DB corrected down to 3.
    legs = [_leg("P", 5500, -3), _leg("P", 5490, +3)]
    eng, db, cfg = _make_engine(tmp_path, legs)
    pid = _add_open(eng, cfg, "PUT", 5500, 5490, credit=0.30, num_contracts=5)

    _run_sync(eng, cfg)

    status, n, _ = _row(db, pid)
    assert status == "open"
    assert n == 3
    # total_credit kept consistent with the new lot count.
    tc = _row(db, pid, cols="total_credit")[0]
    assert tc == pytest.approx(0.30 * 100 * 3)


def test_missing_in_ibkr_marked_expired_full_credit(tmp_path):
    # No legs in IBKR → DB position booked expired with full-credit profit.
    eng, db, cfg = _make_engine(tmp_path, ibkr_legs=[])
    pid = _add_open(eng, cfg, "PUT", 5500, 5490, credit=0.40, num_contracts=2)

    _run_sync(eng, cfg)

    status, n, pnl = _row(db, pid)
    assert status == "expired"
    assert pnl == pytest.approx(0.40 * 100 * 2)   # full credit collected
    notes = _row(db, pid, cols="notes")[0]
    assert "not in IBKR" in (notes or "")


def test_orphan_put_autocreated(tmp_path):
    # PUT legs with no DB row → auto-create 5500/5490 (long = short - 10).
    legs = [_leg("P", 5500, -1), _leg("P", 5490, +1)]
    eng, db, cfg = _make_engine(tmp_path, legs)

    _run_sync(eng, cfg)

    with get_conn(db) as conn:
        rows = conn.execute(
            "SELECT side, short_strike, long_strike, num_contracts, status, credit "
            "FROM positions"
        ).fetchall()
    assert len(rows) == 1
    side, short, long, n, status, credit = rows[0]
    assert side == "PUT"
    assert short == pytest.approx(5500)
    assert long == pytest.approx(5490)
    assert n == 1
    assert status == "open"
    assert credit == pytest.approx(0.0)


def test_orphan_call_autocreated_fallback_width(tmp_path):
    # CALL legs, fallback width 20 → auto-create 5500/5520 (long = short + 20).
    legs = [_leg("C", 5500, -4), _leg("C", 5520, +4)]
    eng, db, cfg = _make_engine(tmp_path, legs)

    _run_sync(eng, cfg)

    with get_conn(db) as conn:
        row = conn.execute(
            "SELECT side, short_strike, long_strike, num_contracts FROM positions"
        ).fetchone()
    assert row == ("CALL", pytest.approx(5500), pytest.approx(5520), 4)


def test_unpairable_orphan_leg_no_write(tmp_path):
    # A lone short put with no long leg at ±10/±20 → warning only, no DB row.
    legs = [_leg("P", 5500, -1)]
    eng, db, cfg = _make_engine(tmp_path, legs)

    _run_sync(eng, cfg)

    with get_conn(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    assert count == 0
    assert eng.logger.warning.called


def test_exit_clamps_close_qty_to_ibkr_held(tmp_path):
    """
    Exit path must close exactly what IBKR holds. DB says 5, IBKR holds 3 →
    the close order is sent for 3 and num_contracts is synced to 3 (so the
    exit notification, which derives from the actual fill, is correct).
    """
    from executor import FillResult

    legs = [_leg("P", 5500, -3), _leg("P", 5490, +3)]
    eng, db, cfg = _make_engine(tmp_path, legs)
    pid = _add_open(eng, cfg, "PUT", 5500, 5490, credit=0.30, num_contracts=5)
    pos = eng.store.get_open()[0]

    decision = SimpleNamespace(
        should_exit=True, reason="L2 exit", exit_layer=2,
        exit_conditions_met=2, exit_regime="neutral",
    )
    combined = SimpleNamespace(vix=18.0, rsi=40.0)

    captured = {}

    def _fake_exit(*, client, ticker, side, short_strike, long_strike, quantity):
        captured["quantity"] = quantity
        return FillResult(order_id=999, filled=False, avg_price=None,
                          contracts=quantity, status="pending", message="")

    with patch("engine.CONFIG", cfg), \
         patch("position_store.CONFIG", cfg), \
         patch("executor.execute_exit", _fake_exit), \
         patch.object(eng, "_log_exit_check"), \
         patch.object(eng, "_record_signal"):
        eng._on_exit_checked(
            ts="10:00:00", pos=pos, decision=decision, combined=combined,
            store=eng.store, spx=5495.0, em=10.0, gex_val=5.0,
        )

    # Close order sent for IBKR-held qty (3), not the stale DB qty (5).
    assert captured["quantity"] == 3
    # DB + in-memory synced to 3 so the notification count is correct.
    assert pos.num_contracts == 3
    assert _row(db, pid, cols="num_contracts")[0] == 3
    # Tracked as a pending exit awaiting fill confirmation.
    assert 999 in eng._pending_exits


def test_row_to_position_loads_num_contracts(tmp_path):
    """
    Regression: _row_to_position() must read num_contracts from the DB, not
    silently default it to 1. A 5-lot position stored in the DB must load as 5.
    """
    from trades_db import init_db, insert_position, get_open_positions, Position

    db = tmp_path / "p.db"
    with get_conn(db) as conn:
        init_db(db)
        insert_position(conn, Position(
            task_id="T", ticker="SPX", side="PUT",
            short_strike=7355.0, long_strike=7335.0,
            open_time="2026-07-08T10:00:00-04:00", credit=0.31,
            num_contracts=5, status="open",
        ))
        conn.commit()
        loaded = get_open_positions(conn)

    assert len(loaded) == 1
    assert loaded[0].num_contracts == 5


def test_load_open_preserves_num_contracts(tmp_path):
    """
    Regression: PositionStore.load_open() must carry num_contracts through to
    the in-memory TradePosition (was defaulting to 1 on every restart).
    """
    legs = []
    eng, db, cfg = _make_engine(tmp_path, legs)
    _add_open(eng, cfg, "PUT", 7355, 7335, credit=0.31, num_contracts=5)

    eng.store.load_open()  # simulate restart reload

    pos = eng.store.get_open()[0]
    assert pos.num_contracts == 5


def test_pending_open_with_legs_promoted_to_open(tmp_path):
    """
    A pending_open order whose legs are present at IBKR has filled → it must be
    promoted to 'open' (not left pending, and NOT duplicated as an orphan), with
    a best-guess fill price/time populated.
    """
    legs = [
        _leg("P", 7355, -5), _leg("P", 7335, +5),   # open 7355/7335
        _leg("P", 7345, -5), _leg("P", 7325, +5),   # pending_open 7345/7325 (filled)
    ]
    eng, db, cfg = _make_engine(tmp_path, legs)
    _add_open(eng, cfg, "PUT", 7355, 7335, credit=0.31, num_contracts=5)
    _add_open(eng, cfg, "PUT", 7345, 7325, credit=0.27, num_contracts=5,
              status="pending_open")

    _run_sync(eng, cfg)

    with get_conn(db) as conn:
        rows = conn.execute(
            "SELECT status, num_contracts, fill_price, fill_time, notes "
            "FROM positions WHERE short_strike = 7345 ORDER BY id"
        ).fetchall()
    # Exactly one 7345 row (no duplicate), now open with fill data.
    assert len(rows) == 1
    status, n, fill_price, fill_time, notes = rows[0]
    assert status == "open"
    assert n == 5
    assert fill_price == pytest.approx(-0.27)   # best guess: -credit
    assert fill_time is not None
    assert "promoted pending_open" in (notes or "")


def test_pending_open_without_legs_stays_pending(tmp_path):
    """
    A pending_open order with NO legs at IBKR is still in-flight (or never
    filled) → reconcile must leave it pending for the poller/timeout to own,
    and must not expire or orphan it.
    """
    legs = []  # nothing at IBKR yet
    eng, db, cfg = _make_engine(tmp_path, legs)
    _add_open(eng, cfg, "PUT", 7345, 7325, credit=0.27, num_contracts=5,
              status="pending_open")

    _run_sync(eng, cfg)

    with get_conn(db) as conn:
        rows = conn.execute(
            "SELECT status FROM positions WHERE short_strike = 7345"
        ).fetchall()
    assert rows == [("pending_open",)]


def test_dry_run_is_noop(tmp_path):
    legs = [_leg("P", 5500, -3), _leg("P", 5490, +3)]
    eng, db, cfg = _make_engine(tmp_path, legs, dry_run=True)
    pid = _add_open(eng, cfg, "PUT", 5500, 5490, credit=0.30, num_contracts=5)

    _run_sync(eng, cfg)

    # Unchanged — dry_run short-circuits before any IBKR call.
    status, n, _ = _row(db, pid)
    assert status == "open"
    assert n == 5
    eng._client.get_open_positions_ibkr.assert_not_called()
