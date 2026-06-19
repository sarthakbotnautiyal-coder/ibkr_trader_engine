"""Tests for combined_reader WAL-resilience helpers (TASK-2026-235).

Verifies that _connect_tv_with_retry():

  * returns a connection on first success
  * retries up to 3 times on sqlite3.OperationalError
  * raises RuntimeError after exhausting retries
  * applies 0.2s / 0.4s / 0.6s backoff between attempts
  * sets PRAGMA journal_mode = WAL on the returned connection
  * does NOT retry on non-OperationalError exceptions (e.g. FileNotFoundError)

These tests use unittest.mock to avoid actually opening tradingview.db
and to control timing precisely (no real sleep).
"""
from __future__ import annotations

import sqlite3
import time
from unittest.mock import MagicMock, patch

import pytest

from src.combined_reader import (
    TV_DB,
    _TV_CONNECT_BACKOFFS,
    _TV_CONNECT_MAX_ATTEMPTS,
    _TV_CONNECT_TIMEOUT,
    _connect_tv_with_retry,
)


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------

def test_retry_constants_match_spec():
    """Spec: 3 attempts, 0.2/0.4/0.6 backoff, 2.0s timeout."""
    assert _TV_CONNECT_MAX_ATTEMPTS == 3
    assert _TV_CONNECT_BACKOFFS == (0.2, 0.4, 0.6)
    assert _TV_CONNECT_TIMEOUT == 2.0


def test_tv_db_path_is_set():
    """TV_DB must point to the tradingview_signal_generator data dir."""
    assert "tradingView_signal_generator" in str(TV_DB)
    assert str(TV_DB).endswith("tradingview.db")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_connect_returns_connection_on_first_success():
    """When sqlite3.connect succeeds on the first call, return that conn."""
    fake_conn = MagicMock(spec=sqlite3.Connection)
    with patch("src.combined_reader.sqlite3.connect", return_value=fake_conn) as mc:
        result = _connect_tv_with_retry()
    assert result is fake_conn
    # Must set WAL journal mode
    fake_conn.execute.assert_any_call("PRAGMA journal_mode = WAL;")
    # Must call connect exactly once on happy path
    assert mc.call_count == 1
    # Verify timeout argument
    _, kwargs = mc.call_args
    assert kwargs.get("timeout") == _TV_CONNECT_TIMEOUT


# ---------------------------------------------------------------------------
# Retry path
# ---------------------------------------------------------------------------

def test_connect_retries_on_operational_error_and_succeeds():
    """First two attempts raise OperationalError, third succeeds."""
    fake_conn = MagicMock(spec=sqlite3.Connection)
    fake_conn.execute.return_value = None
    op_err = sqlite3.OperationalError("unable to open database file")

    with patch("src.combined_reader.sqlite3.connect",
               side_effect=[op_err, op_err, fake_conn]) as mc, \
         patch("src.combined_reader.time.sleep") as sleep_mock:
        result = _connect_tv_with_retry()

    assert result is fake_conn
    assert mc.call_count == 3
    # Backoff sleep called twice (after attempt 1 and attempt 2)
    assert sleep_mock.call_count == 2
    sleep_mock.assert_any_call(0.2)
    sleep_mock.assert_any_call(0.4)


def test_connect_raises_runtime_error_after_three_failures():
    """Three OperationalErrors → RuntimeError with last error message."""
    op_err = sqlite3.OperationalError("database is locked")

    with patch("src.combined_reader.sqlite3.connect",
               side_effect=[op_err, op_err, op_err]) as mc, \
         patch("src.combined_reader.time.sleep") as sleep_mock:
        with pytest.raises(RuntimeError) as exc_info:
            _connect_tv_with_retry()

    assert mc.call_count == 3
    # Backoff between 3 attempts is 2 sleeps (after attempts 1 and 2) — 0.2s, 0.4s
    # No sleep after the final failed attempt; we break out of the retry loop.
    assert sleep_mock.call_count == 2
    sleep_mock.assert_any_call(0.2)
    sleep_mock.assert_any_call(0.4)
    # RuntimeError should include the last error and the attempt count
    msg = str(exc_info.value)
    assert "3 attempts" in msg
    assert "database is locked" in msg


def test_connect_does_not_retry_non_operational_error():
    """FileNotFoundError must NOT trigger retry — raise immediately."""
    fnf_err = FileNotFoundError("tradingview.db does not exist")

    with patch("src.combined_reader.sqlite3.connect",
               side_effect=fnf_err) as mc, \
         patch("src.combined_reader.time.sleep") as sleep_mock:
        with pytest.raises(FileNotFoundError):
            _connect_tv_with_retry()

    # Only one call — no retry for non-OperationalError
    assert mc.call_count == 1
    sleep_mock.assert_not_called()


def test_connect_retries_exactly_max_attempts_on_persistent_error():
    """Even with a persistent OperationalError, attempts == _TV_CONNECT_MAX_ATTEMPTS."""
    op_err = sqlite3.OperationalError("disk I/O error")

    with patch("src.combined_reader.sqlite3.connect",
               side_effect=op_err) as mc, \
         patch("src.combined_reader.time.sleep") as sleep_mock:
        with pytest.raises(RuntimeError):
            _connect_tv_with_retry()

    assert mc.call_count == _TV_CONNECT_MAX_ATTEMPTS
    # Two sleeps for three attempts
    assert sleep_mock.call_count == _TV_CONNECT_MAX_ATTEMPTS - 1


# ---------------------------------------------------------------------------
# Integration sanity check — actually open a temp db
# ---------------------------------------------------------------------------

def test_connect_against_real_temp_db(tmp_path):
    """Smoke test: helper works against a real SQLite file with WAL."""
    fake_db = tmp_path / "tradingview.db"
    # Pre-create the db so journal_mode = WAL can be set on open
    pre = sqlite3.connect(str(fake_db))
    pre.close()

    with patch("src.combined_reader.TV_DB", fake_db):
        conn = _connect_tv_with_retry()

    try:
        # Verify WAL is enabled
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()
