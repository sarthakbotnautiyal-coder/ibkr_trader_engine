"""Tests for engine.run() resilience (TASK-2026-235, Fix 1).

Verifies that the inner ``while True: tick()`` loop in ``AutoTraderEngine.run()``
swallows arbitrary exceptions, logs them, and continues — but still propagates
``KeyboardInterrupt`` to the outer handler.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _build_engine_with_mocks():
    """Build an AutoTraderEngine with all heavy deps stubbed.

    The engine constructor is heavy (loads config, sets up clients, etc.).
    We bypass it by allocating the object directly and patching everything
    run() touches.
    """
    from src.engine import AutoTraderEngine

    eng = AutoTraderEngine.__new__(AutoTraderEngine)
    eng.dry_run = True
    eng.check_interval = 0          # no real sleep
    # ``client`` is a property backed by ``_client`` — set the underlying attr.
    eng._client = MagicMock()
    eng._client.is_connected.return_value = True
    eng._shutdown = MagicMock()
    eng.logger = MagicMock()
    return eng


def test_run_retries_on_tick_exception():
    """If tick() raises an arbitrary Exception, run() must log and continue."""
    eng = _build_engine_with_mocks()

    call_count = {"n": 0}

    def fake_tick():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise sqlite3_OperationalError()
        if call_count["n"] >= 3:
            raise KeyboardInterrupt()

    eng.tick = fake_tick

    # Patch time.sleep so the loop runs through quickly
    with patch("src.engine.time.sleep"):
        eng.run()

    # tick() was called multiple times — exception was caught
    assert call_count["n"] >= 3
    # The exception was logged via logger.exception
    eng.logger.exception.assert_called()
    # The shutdown path was still called
    eng._shutdown.assert_called()


def test_run_propagates_keyboard_interrupt():
    """KeyboardInterrupt must NOT be swallowed — it must reach the outer except."""
    eng = _build_engine_with_mocks()
    eng.tick = MagicMock(side_effect=KeyboardInterrupt())

    with patch("src.engine.time.sleep"):
        eng.run()

    # Outer except KeyboardInterrupt logged the "stopped by user" message
    info_calls = [str(c) for c in eng.logger.info.call_args_list]
    assert any("Ctrl+C" in msg or "stopped by user" in msg for msg in info_calls)
    # Shutdown still runs in finally
    eng._shutdown.assert_called()


def test_run_logs_retry_interval_in_error_message():
    """Error log must mention the retry interval so operators know it will retry."""
    eng = _build_engine_with_mocks()
    eng.check_interval = 30

    call_count = {"n": 0}

    def fake_tick():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise sqlite3_OperationalError("WAL contention simulated")
        raise KeyboardInterrupt()

    eng.tick = fake_tick

    with patch("src.engine.time.sleep"):
        eng.run()

    # Find the exception log
    assert eng.logger.exception.call_count >= 1
    exc_msg = str(eng.logger.exception.call_args_list[0])
    assert "30" in exc_msg or "retry" in exc_msg.lower()


def test_run_shutdown_runs_even_on_repeated_exceptions():
    """finally block must always run, even when tick() keeps raising."""
    eng = _build_engine_with_mocks()

    call_count = {"n": 0}

    def fake_tick():
        call_count["n"] += 1
        if call_count["n"] < 5:
            raise RuntimeError("transient failure #%d" % call_count["n"])
        raise KeyboardInterrupt()

    eng.tick = fake_tick

    with patch("src.engine.time.sleep"):
        eng.run()

    assert call_count["n"] == 5  # 4 failures + 1 KeyboardInterrupt
    eng._shutdown.assert_called_once()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sqlite3_OperationalError(msg: str = "unable to open database file"):
    """Create a sqlite3.OperationalError matching the WAL contention symptom."""
    import sqlite3
    return sqlite3.OperationalError(msg)
