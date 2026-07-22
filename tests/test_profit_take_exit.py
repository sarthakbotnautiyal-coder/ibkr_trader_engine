"""
Tests for the profit-take exit (layer 3) — risk_manager._profit_take_decision
via evaluate_exit(), plus the engine-side reconnect resubscribe regression.

Run with: PYTHONPATH=".:src" venv/bin/python -m pytest tests/test_profit_take_exit.py -v

Design (see evaluate_exit docstring): the profit-take fires only when ALL of
  config    — exit.profit_take_debit > 0
  cutoff    — now (ET, clock-override aware) is before exit.profit_take_cutoff
  quote     — a live TWO-SIDED ComboQuote with a bounded close_debit
  freshness — quote age <= profit_take_max_quote_age
  price     — close_debit <= profit_take_debit
  debounce  — held for profit_take_confirm_ticks consecutive ticks
hold. Every gate is suppress-only: any failure → no exit (ride to expiry).
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import risk_manager
from src.risk_manager import ET, evaluate_exit, reset_profit_take_state, set_clock_override
from src.position_store import PositionSide


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A config with the profit-take enabled at 0.05 (plus the standard L2 keys).
_PT_EXIT_CFG = {
    "votes_to_exit": 3,
    "profit_take_debit": 0.05,
    "profit_take_cutoff": "15:00",
    "profit_take_confirm_ticks": 2,
    "profit_take_max_quote_age": 90,
}

# A config written BEFORE the profit-take keys existed (backward compat).
_LEGACY_EXIT_CFG = {"votes_to_exit": 3}


def _pos(db_id=7, side=PositionSide.CALL, short=4560.0, long_=4570.0, credit=0.50):
    """Open position mock, far from its short strike, with no L2 baselines
    firing (entry_em set, indicators quiet)."""
    pos = Mock()
    pos.db_id = db_id
    pos.side = side
    pos.short_strike = short
    pos.long_strike = long_
    pos.credit = credit
    pos.num_contracts = 1
    pos.entry_em = 15.0
    pos.entry_spx_spot = 4500.0
    pos.entry_adx = None
    pos.entry_rsi = None
    pos.entry_vix1d = None
    return pos


def _combined(spx=4500.0, em=15.0):
    """Snapshot mock: price far from the strike, no L2 votes possible."""
    c = Mock()
    c.spx_spot = spx
    c.expected_move = em
    c.regime = "neutral"
    c.adx = None
    c.rsi = None
    c.vix1d = None
    c.major_positive_by_volume = None
    c.major_negative_by_volume = None
    return c


def _quote(close_debit=0.04, two_sided=True, age_sec=10.0, bid=-0.06, ask=-0.02):
    """Duck-typed stand-in for blocking_ib_client.ComboQuote."""
    return SimpleNamespace(
        bid=bid, ask=ask, two_sided=two_sided,
        close_debit=close_debit, age_sec=age_sec,
    )


@pytest.fixture(autouse=True)
def _clean_state():
    """Fresh debounce state + a 13:00 ET clock (well before cutoff) per test."""
    reset_profit_take_state()
    set_clock_override(lambda: datetime(2026, 7, 22, 13, 0, tzinfo=ET))
    yield
    set_clock_override(None)
    reset_profit_take_state()


def _evaluate_n(pos, quote, n, cfg=_PT_EXIT_CFG):
    """Run evaluate_exit n times (n ticks) with the same quote; return last decision."""
    decision = None
    with patch.object(risk_manager, "CONFIG", {"exit": cfg}):
        for _ in range(n):
            decision = evaluate_exit(pos, _combined(), combo_quote=quote)
    return decision


# ---------------------------------------------------------------------------
# Fires when everything lines up
# ---------------------------------------------------------------------------

class TestProfitTakeFires:
    def test_fires_after_confirm_ticks(self):
        d = _evaluate_n(_pos(), _quote(close_debit=0.04), n=2)
        assert d.should_exit is True
        assert d.exit_layer == 3
        assert "PROFIT_TAKE" in d.reason
        assert "% of credit locked" in d.reason

    def test_single_tick_does_not_fire(self):
        """Debounce: one tick below threshold is not enough (confirm_ticks=2)."""
        d = _evaluate_n(_pos(), _quote(close_debit=0.04), n=1)
        assert d.should_exit is False

    def test_streak_resets_when_condition_breaks(self):
        """below → above → below → below: the interruption restarts the count,
        so the exit fires on the 2nd consecutive good tick, not the flicker."""
        pos = _pos()
        with patch.object(risk_manager, "CONFIG", {"exit": _PT_EXIT_CFG}):
            assert evaluate_exit(pos, _combined(), combo_quote=_quote(0.04)).should_exit is False
            assert evaluate_exit(pos, _combined(), combo_quote=_quote(0.20)).should_exit is False
            assert evaluate_exit(pos, _combined(), combo_quote=_quote(0.04)).should_exit is False
            assert evaluate_exit(pos, _combined(), combo_quote=_quote(0.04)).should_exit is True

    def test_exactly_at_threshold_fires(self):
        d = _evaluate_n(_pos(), _quote(close_debit=0.05), n=2)
        assert d.should_exit is True

    def test_free_close_fires(self):
        """close_debit clamped to 0 (positive bid = paid to close) still fires."""
        d = _evaluate_n(_pos(), _quote(close_debit=0.0), n=2)
        assert d.should_exit is True

    def test_works_without_entry_em(self):
        """Legacy positions (entry_em=None → L2 inactive) still profit-take."""
        pos = _pos()
        pos.entry_em = None
        d = _evaluate_n(pos, _quote(close_debit=0.04), n=2)
        assert d.should_exit is True
        assert d.exit_layer == 3

    def test_put_side_fires_too(self):
        pos = _pos(side=PositionSide.PUT, short=4440.0, long_=4430.0)
        d = _evaluate_n(pos, _quote(close_debit=0.04), n=2)
        assert d.should_exit is True


# ---------------------------------------------------------------------------
# Backward compatibility — old configs stay untouched
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_inert_when_keys_absent(self):
        """A config written before the profit-take keys existed → feature off."""
        d = _evaluate_n(_pos(), _quote(close_debit=0.01), n=5, cfg=_LEGACY_EXIT_CFG)
        assert d.should_exit is False

    def test_inert_when_zero(self):
        cfg = dict(_PT_EXIT_CFG, profit_take_debit=0.0)
        d = _evaluate_n(_pos(), _quote(close_debit=0.01), n=5, cfg=cfg)
        assert d.should_exit is False

    def test_inert_when_exit_section_missing_entirely(self):
        with patch.object(risk_manager, "CONFIG", {}):
            pos = _pos()
            for _ in range(5):
                d = evaluate_exit(pos, _combined(), combo_quote=_quote(0.01))
            assert d.should_exit is False


# ---------------------------------------------------------------------------
# Last-hour cutoff
# ---------------------------------------------------------------------------

class TestCutoff:
    def test_inert_at_cutoff(self):
        set_clock_override(lambda: datetime(2026, 7, 22, 15, 0, tzinfo=ET))
        d = _evaluate_n(_pos(), _quote(close_debit=0.01), n=5)
        assert d.should_exit is False

    def test_inert_after_cutoff(self):
        set_clock_override(lambda: datetime(2026, 7, 22, 15, 30, tzinfo=ET))
        d = _evaluate_n(_pos(), _quote(close_debit=0.01), n=5)
        assert d.should_exit is False

    def test_fires_just_before_cutoff(self):
        set_clock_override(lambda: datetime(2026, 7, 22, 14, 59, tzinfo=ET))
        d = _evaluate_n(_pos(), _quote(close_debit=0.04), n=2)
        assert d.should_exit is True

    def test_crossing_cutoff_resets_streak(self):
        """Tick 1 before cutoff builds a streak; the cutoff then suppresses and
        clears it, so a later tick can't complete a stale pre-cutoff streak."""
        pos = _pos()
        with patch.object(risk_manager, "CONFIG", {"exit": _PT_EXIT_CFG}):
            set_clock_override(lambda: datetime(2026, 7, 22, 14, 59, tzinfo=ET))
            assert evaluate_exit(pos, _combined(), combo_quote=_quote(0.04)).should_exit is False
            set_clock_override(lambda: datetime(2026, 7, 22, 15, 0, tzinfo=ET))
            assert evaluate_exit(pos, _combined(), combo_quote=_quote(0.04)).should_exit is False
        assert risk_manager._PT_STREAKS == {}

    def test_bad_cutoff_string_falls_back_to_1500(self):
        cfg = dict(_PT_EXIT_CFG, profit_take_cutoff="garbage")
        set_clock_override(lambda: datetime(2026, 7, 22, 15, 30, tzinfo=ET))
        d = _evaluate_n(_pos(), _quote(close_debit=0.01), n=5, cfg=cfg)
        assert d.should_exit is False


# ---------------------------------------------------------------------------
# Quote-quality gates (all suppress-only)
# ---------------------------------------------------------------------------

class TestQuoteGates:
    def test_inert_without_quote(self):
        """combo_quote=None (dry-run / backtest / cloud) → nothing happens."""
        d = _evaluate_n(_pos(), None, n=5)
        assert d.should_exit is False

    def test_inert_on_one_sided_quote(self):
        """No two-sided book → never trust the mark for a buy-back."""
        d = _evaluate_n(_pos(), _quote(close_debit=0.01, two_sided=False), n=5)
        assert d.should_exit is False

    def test_inert_without_close_debit(self):
        """Two-sided but close_debit failed the [0, width] bound → skipped."""
        d = _evaluate_n(_pos(), _quote(close_debit=None), n=5)
        assert d.should_exit is False

    def test_inert_on_stale_quote(self):
        d = _evaluate_n(_pos(), _quote(close_debit=0.01, age_sec=120.0), n=5)
        assert d.should_exit is False

    def test_inert_on_unknown_age(self):
        d = _evaluate_n(_pos(), _quote(close_debit=0.01, age_sec=None), n=5)
        assert d.should_exit is False

    def test_inert_above_threshold(self):
        d = _evaluate_n(_pos(), _quote(close_debit=0.06), n=5)
        assert d.should_exit is False


# ---------------------------------------------------------------------------
# Layer priority
# ---------------------------------------------------------------------------

class TestLayerPriority:
    def test_l1_wins_over_profit_take(self):
        """SPX through the short strike is a hard stop — even with a streak
        primed and a winning quote, L1 must fire, not the profit-take."""
        pos = _pos(short=4560.0)
        with patch.object(risk_manager, "CONFIG", {"exit": _PT_EXIT_CFG}):
            evaluate_exit(pos, _combined(spx=4500.0), combo_quote=_quote(0.04))  # streak=1
            d = evaluate_exit(pos, _combined(spx=4561.0), combo_quote=_quote(0.04))
        assert d.should_exit is True
        assert d.exit_layer == 1
        assert "L1 crossed" in d.reason

    def test_profit_take_beats_l2_evaluation(self):
        """When the profit-take fires, L2 is not consulted (reason says PROFIT_TAKE)."""
        d = _evaluate_n(_pos(), _quote(close_debit=0.04), n=2)
        assert d.exit_layer == 3
        assert "L2" not in d.reason


# ---------------------------------------------------------------------------
# Engine reconnect regression — resubscribe must be unconditional
# ---------------------------------------------------------------------------

class TestReconnectResubscribe:
    """After an IBKR reconnect the client clears its ticker map, so the engine
    must re-issue subscribe_combo_mark every tick (client-side idempotent).
    Regression for the bug where an engine-side 'already subscribed' cache
    left pre-disconnect positions with premium=None forever."""

    def _engine(self):
        from engine import AutoTraderEngine
        eng = AutoTraderEngine.__new__(AutoTraderEngine)  # skip heavyweight __init__
        eng.dry_run = False
        eng._combo_sub_keys = set()
        eng.logger = Mock()
        eng._client = Mock()
        eng._client.subscribe_combo_mark = Mock(return_value=True)
        eng._client.get_combo_debit = Mock(return_value=0.04)
        eng._client.get_combo_quote = Mock(return_value=_quote())
        return eng

    def _make_pos(self):
        pos = Mock()
        pos.db_id = 7
        pos.side = PositionSide.CALL
        pos.short_strike = 4560.0
        pos.long_strike = 4570.0
        return pos

    def test_subscribe_called_every_tick_even_when_cached(self):
        eng = self._engine()
        # Simulate pre-reconnect state: engine already thinks key "7" is live.
        eng._combo_sub_keys.add("7")
        pos = self._make_pos()
        with patch("executor.today_expiry", return_value="20260722"):
            eng._position_debit(pos)
            eng._position_debit(pos)
        # The fix: subscribe is re-issued on every call, not gated by the cache.
        assert eng._client.subscribe_combo_mark.call_count == 2

    def test_quote_provider_shares_subscription_path(self):
        eng = self._engine()
        pos = self._make_pos()
        with patch("executor.today_expiry", return_value="20260722"):
            q = eng._position_quote(pos)
        assert q is not None
        assert eng._client.subscribe_combo_mark.called
        eng._client.get_combo_quote.assert_called_once_with("7")

    def test_provider_failures_return_none(self):
        eng = self._engine()
        eng._client.get_combo_quote = Mock(side_effect=RuntimeError("boom"))
        pos = self._make_pos()
        with patch("executor.today_expiry", return_value="20260722"):
            assert eng._position_quote(pos) is None
