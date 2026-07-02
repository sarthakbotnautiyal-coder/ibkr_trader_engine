"""
Tests for TASK-2026-308: defensive _fmt() helper + _log_tick_heartbeat() NoneType crash.

The 2026-07-02 ops report (TASK-2026-307) flagged a recurring
``TypeError: unsupported format string passed to NoneType.__format__``
at engine.py:914 (_log_tick_heartbeat) when EM feed returned None.
This test pins the fix: _fmt() handles None, _log_tick_heartbeat()
never raises on None values, and non-None values format exactly as
before (backward compat).
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helper-level tests
# ---------------------------------------------------------------------------

class TestFmtHelper:
    """Unit tests for the module-level _fmt() helper."""

    def test_fmt_non_none_default_spec(self):
        from src.engine import _fmt
        assert _fmt(14.32) == "14.32"

    def test_fmt_non_none_zero_is_preserved(self):
        """Zero must NOT be replaced by the fallback — it's a valid value."""
        from src.engine import _fmt
        assert _fmt(0) == "0.00"
        assert _fmt(0.0) == "0.00"
        assert _fmt(-1.5) == "-1.50"

    def test_fmt_none_returns_default_fallback(self):
        from src.engine import _fmt
        assert _fmt(None) == "n/a"

    def test_fmt_none_returns_custom_fallback(self):
        from src.engine import _fmt
        assert _fmt(None, ".2f", "—") == "—"
        assert _fmt(None, ".2f", "") == ""

    def test_fmt_alternate_specs(self):
        """The spec param lets callers reuse _fmt() for non-'.2f' format strings."""
        from src.engine import _fmt
        assert _fmt(37, ".0f") == "37"
        assert _fmt(36.9, ".1f") == "36.9"
        assert _fmt(None, ".0f") == "n/a"
        assert _fmt(None, ".1f", "warmup") == "warmup"

    def test_fmt_custom_fallback_for_non_none_specs(self):
        """If caller supplies a spec+fallback that conflict, fallback is None-only."""
        from src.engine import _fmt
        # Non-None always uses the spec
        assert _fmt(99.9, ".0f", "missing") == "100"
        # None uses the fallback
        assert _fmt(None, ".0f", "missing") == "missing"

    def test_fmt_does_not_raise_on_none(self):
        """The whole point: no TypeError on NoneType.__format__."""
        from src.engine import _fmt
        # This previously raised: TypeError: unsupported format string
        # passed to NoneType.__format__
        result = _fmt(None, ".2f")
        assert result == "n/a"
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _log_tick_heartbeat end-to-end (None safety + backward compat)
# ---------------------------------------------------------------------------

def _make_engine():
    """Bypass AutoTraderEngine.__init__ — we only need .logger."""
    from src.engine import AutoTraderEngine
    eng = AutoTraderEngine.__new__(AutoTraderEngine)
    eng.logger = MagicMock(spec=logging.Logger)
    return eng


class TestLogTickHeartbeatNoneSafe:
    """Pin TASK-2026-308 fix: None values must NOT crash the tick logger."""

    def test_all_none_values_does_not_raise(self):
        eng = _make_engine()
        # Before the fix this raised TypeError; after the fix it logs
        # one INFO line with 'n/a' placeholders.
        eng._log_tick_heartbeat(
            ts="2026-07-02T16:10:00-04:00",
            spx=None,
            em=None,
            gex_val=None,
            regime="momentum_down",
            rsi=None,
            gex_regime="dealer_long",
        )
        eng.logger.info.assert_called_once()
        msg = eng.logger.info.call_args[0][0]
        assert isinstance(msg, str)
        # All four vulnerable fields rendered as 'n/a'
        assert "SPX=n/a" in msg
        assert "EM=n/a" in msg
        assert "GEX=n/a" in msg
        assert "RSI=n/a" in msg
        # Non-vulnerable fields still appear
        assert "[TICK]" in msg
        assert "regime=momentum_down" in msg
        assert "GEX_regime=dealer_long" in msg

    def test_em_none_rsi_atr_present(self):
        """Reproduces the exact 2026-07-02 10:47 ET condition from the ops report."""
        eng = _make_engine()
        eng._log_tick_heartbeat(
            ts="2026-07-02T16:10:00-04:00",
            spx=7491.12,
            em=None,        # <-- the exact bug condition
            gex_val=37,
            regime="momentum_down",
            rsi=36.9,
            gex_regime="dealer_long",
        )
        eng.logger.info.assert_called_once()
        msg = eng.logger.info.call_args[0][0]
        assert "EM=n/a" in msg
        assert "SPX=7491.12" in msg
        assert "RSI=36.9" in msg

    def test_all_non_none_backward_compat(self):
        """Non-None values must format exactly as before — no behavior change."""
        eng = _make_engine()
        eng._log_tick_heartbeat(
            ts="2026-07-02T16:10:30-04:00",
            spx=7491.12,
            em=14.32,
            gex_val=37,
            regime="momentum_down",
            rsi=36.9,
            gex_regime="dealer_long",
        )
        msg = eng.logger.info.call_args[0][0]
        # Exact-match against pre-fix output format
        assert msg.endswith(
            " ET [TICK] SPX=7491.12 | EM=14.32 | GEX=37 | "
            "regime=momentum_down | RSI=36.9 | GEX_regime=dealer_long"
        )

    def test_mixed_none_and_values(self):
        """One None field doesn't poison the others — partial-resilience."""
        eng = _make_engine()
        eng._log_tick_heartbeat(
            ts="2026-07-02T16:11:00-04:00",
            spx=7492.50,
            em=14.32,
            gex_val=None,        # scanner outage mid-tick
            regime="momentum_down",
            rsi=42.1,
            gex_regime="dealer_long",
        )
        msg = eng.logger.info.call_args[0][0]
        assert "EM=14.32" in msg
        assert "SPX=7492.50" in msg
        assert "GEX=n/a" in msg
        assert "RSI=42.1" in msg

    def test_zero_values_formatted_as_zero_not_n_a(self):
        """Zero is a legitimate value; must not be coerced to fallback."""
        eng = _make_engine()
        eng._log_tick_heartbeat(
            ts="2026-07-02T16:12:00-04:00",
            spx=0.0,
            em=0.0,
            gex_val=0,
            regime="neutral",
            rsi=0.0,
            gex_regime="dealer_long",
        )
        msg = eng.logger.info.call_args[0][0]
        assert "SPX=0.00" in msg
        assert "EM=0.00" in msg
        assert "GEX=0" in msg
        assert "RSI=0.0" in msg
        assert "n/a" not in msg
