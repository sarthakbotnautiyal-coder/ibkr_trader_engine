"""Regression tests for _read_combo_debit_impl — IBKR "no quote" sentinel handling.

IBKR returns -1 (with size 0) as the "no quote available" marker, common for
illiquid deep-OTM 0DTE combos. The old reader averaged those sentinels into a
fabricated debit-to-close, which inverted unrealized P&L and falsely tripped the
L2 premium exit vote (a worthless spread mis-marked at $1.50).

The data baked into these tests is the real market snapshot captured live on
2026-06-25 for two open positions that the engine reported as premium=1.50 /
premium=1.00 while the spreads were actually worth ~$0.05 / ~$0.10.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from types import SimpleNamespace

_root = Path(__file__).parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))

from src.blocking_ib_client import _read_combo_debit_impl


class _FakeState:
    def __init__(self):
        self._combo_tickers = {}
        self._combo_widths = {}


def _ticker(**kw):
    base = dict(bid=None, bidSize=None, ask=None, askSize=None, last=None, close=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _state_with(key, ticker, width):
    st = _FakeState()
    st._combo_tickers[key] = ticker
    st._combo_widths[key] = width
    return st


# ---------------------------------------------------------------------------
# The two real-world sentinel cases that motivated the fix
# ---------------------------------------------------------------------------

def test_put_7235_7215_sentinel_ask_falls_back_to_last():
    """BAG bid=-2.0(sz1) ask=-1(sz0): ask is a sentinel → use |last|=0.05."""
    t = _ticker(bid=-2.0, bidSize=1.0, ask=-1.0, askSize=0.0, last=-0.05, close=0.0)
    st = _state_with("9", t, width=20.0)
    debit = _read_combo_debit_impl(None, st, "9")
    assert debit is not None
    assert abs(debit - 0.05) < 1e-9   # NOT the old fabricated 1.50


def test_put_7205_7195_double_sentinel_falls_back_to_last():
    """BAG bid=-1(sz0) ask=-1(sz0): both sentinels → use |last|=0.10."""
    t = _ticker(bid=-1.0, bidSize=0.0, ask=-1.0, askSize=0.0, last=-0.10, close=0.0)
    st = _state_with("8", t, width=10.0)
    debit = _read_combo_debit_impl(None, st, "8")
    assert debit is not None
    assert abs(debit - 0.10) < 1e-9   # NOT the old fabricated 1.00


# ---------------------------------------------------------------------------
# Valid two-sided quotes still work (including a legit $1.00 credit mark)
# ---------------------------------------------------------------------------

def test_valid_two_sided_quote_uses_mid():
    """Real resting quotes on both sides → mid, abs()'d to a debit."""
    t = _ticker(bid=-1.6, bidSize=12.0, ask=-1.2, askSize=8.0, last=-1.5, close=-1.4)
    st = _state_with("k", t, width=20.0)
    assert abs(_read_combo_debit_impl(None, st, "k") - 1.4) < 1e-9


def test_legit_minus_one_mark_is_not_rejected():
    """A -1.0 mark with real size is a genuine $1.00 credit-to-close, not a sentinel."""
    t = _ticker(bid=-1.05, bidSize=5.0, ask=-0.95, askSize=5.0, last=-1.0, close=-1.0)
    st = _state_with("k", t, width=20.0)
    assert abs(_read_combo_debit_impl(None, st, "k") - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------

def test_no_data_at_all_returns_none():
    """All sentinels, no last/close → None (skip premium vote, don't lie)."""
    t = _ticker(bid=-1.0, bidSize=0.0, ask=-1.0, askSize=0.0, last=None, close=None)
    st = _state_with("k", t, width=10.0)
    assert _read_combo_debit_impl(None, st, "k") is None


def test_debit_above_width_is_rejected():
    """A mid implying debit > spread width is impossible → bad data → None."""
    t = _ticker(bid=-26.0, bidSize=3.0, ask=-24.0, askSize=3.0, last=None, close=None)
    st = _state_with("k", t, width=20.0)  # mid=-25 → debit 25 > 20
    assert _read_combo_debit_impl(None, st, "k") is None


def test_debit_at_width_boundary_is_accepted():
    """Debit exactly == width (deep ITM at expiry) is valid."""
    t = _ticker(bid=-20.0, bidSize=3.0, ask=-20.0, askSize=3.0)
    st = _state_with("k", t, width=20.0)
    assert abs(_read_combo_debit_impl(None, st, "k") - 20.0) < 1e-9


def test_last_sentinel_minus_one_not_used():
    """A -1 sentinel in last (no two-sided quote, sizeless) is not trusted."""
    t = _ticker(bid=None, bidSize=None, ask=None, askSize=None, last=-1.0, close=0.07)
    st = _state_with("k", t, width=10.0)
    # last=-1 is the sentinel → skip to close=0.07
    assert abs(_read_combo_debit_impl(None, st, "k") - 0.07) < 1e-9


def test_unknown_key_returns_none():
    assert _read_combo_debit_impl(None, _FakeState(), "missing") is None


def test_missing_width_skips_bound_check():
    """If width is unknown, still return the mark (best effort, no bound)."""
    t = _ticker(bid=-1.6, bidSize=12.0, ask=-1.2, askSize=8.0)
    st = _FakeState()
    st._combo_tickers["k"] = t  # no width recorded
    assert abs(_read_combo_debit_impl(None, st, "k") - 1.4) < 1e-9
