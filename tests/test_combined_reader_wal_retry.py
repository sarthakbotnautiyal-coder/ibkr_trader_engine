"""Tests for the shared WAL-resilient read-only connect helper
(TASK-2026-235, generalized in db_utils).

The engine reads scanner.db / gex.db / tradingview.db while separate writer
processes append to them in WAL mode. ``db_utils.connect_ro_with_retry()``:

  * returns a connection on first success
  * opens the DB read-only (``mode=ro`` URI) — never a writable handle
  * does NOT issue ``PRAGMA journal_mode = WAL`` (that needs a write lock and
    races the writer; journal mode is persisted by the writer anyway)
  * retries up to 3 times on sqlite3.OperationalError
  * raises RuntimeError after exhausting retries
  * applies 0.2s / 0.4s / 0.6s backoff between attempts
  * does NOT retry on non-OperationalError exceptions (e.g. FileNotFoundError)

These tests use unittest.mock to avoid touching real databases and to control
timing precisely (no real sleep).
"""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from src import db_utils
from src.db_utils import (
    _DB_CONNECT_BACKOFFS,
    _DB_CONNECT_MAX_ATTEMPTS,
    _DB_CONNECT_TIMEOUT,
    connect_ro_with_retry,
)
from src.combined_reader import SCANNER_DB, GEX_DB, TV_DB, _connect_ro_with_retry


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------

def test_retry_constants_match_spec():
    """Spec: 3 attempts, 0.2/0.4/0.6 backoff, 2.0s timeout."""
    assert _DB_CONNECT_MAX_ATTEMPTS == 3
    assert _DB_CONNECT_BACKOFFS == (0.2, 0.4, 0.6)
    assert _DB_CONNECT_TIMEOUT == 2.0


def test_combined_reader_reexports_shared_helper():
    """combined_reader keeps a backward-compatible alias to the shared helper.

    Identity isn't guaranteed because the engine imports modules by bare name
    (``db_utils``) while the test suite imports them as a package (``src.db_utils``),
    yielding two distinct module objects. Assert by name instead.
    """
    assert callable(_connect_ro_with_retry)
    assert _connect_ro_with_retry.__name__ == connect_ro_with_retry.__name__ == "connect_ro_with_retry"


def test_db_paths_are_set():
    """The three source DB paths point at the expected files."""
    assert str(SCANNER_DB).endswith("scanner.db")
    assert str(GEX_DB).endswith("gex.db")
    assert "tradingView_signal_generator" in str(TV_DB)
    assert str(TV_DB).endswith("tradingview.db")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_connect_returns_connection_on_first_success():
    """When sqlite3.connect succeeds on the first call, return that conn."""
    fake_conn = MagicMock(spec=sqlite3.Connection)
    with patch("src.db_utils.sqlite3.connect", return_value=fake_conn) as mc:
        result = connect_ro_with_retry("/tmp/scanner.db", "scanner.db")
    assert result is fake_conn
    assert mc.call_count == 1
    # Read-only URI + uri=True + timeout
    args, kwargs = mc.call_args
    assert args[0] == "file:/tmp/scanner.db?mode=ro"
    assert kwargs.get("uri") is True
    assert kwargs.get("timeout") == _DB_CONNECT_TIMEOUT


def test_connect_never_sets_wal_pragma():
    """The reader must not issue PRAGMA journal_mode = WAL (write-lock race)."""
    fake_conn = MagicMock(spec=sqlite3.Connection)
    with patch("src.db_utils.sqlite3.connect", return_value=fake_conn):
        connect_ro_with_retry("/tmp/scanner.db", "scanner.db")
    for call in fake_conn.execute.call_args_list:
        assert "journal_mode" not in str(call).lower()


# ---------------------------------------------------------------------------
# Retry path
# ---------------------------------------------------------------------------

def test_connect_retries_on_operational_error_and_succeeds():
    """First two attempts raise OperationalError, third succeeds."""
    fake_conn = MagicMock(spec=sqlite3.Connection)
    op_err = sqlite3.OperationalError("unable to open database file")

    with patch("src.db_utils.sqlite3.connect",
               side_effect=[op_err, op_err, fake_conn]) as mc, \
         patch("src.db_utils.time.sleep") as sleep_mock:
        result = connect_ro_with_retry("/tmp/gex.db", "gex.db")

    assert result is fake_conn
    assert mc.call_count == 3
    assert sleep_mock.call_count == 2
    sleep_mock.assert_any_call(0.2)
    sleep_mock.assert_any_call(0.4)


def test_connect_raises_runtime_error_after_three_failures():
    """Three OperationalErrors → RuntimeError with last error message + label."""
    op_err = sqlite3.OperationalError("database is locked")

    with patch("src.db_utils.sqlite3.connect",
               side_effect=[op_err, op_err, op_err]) as mc, \
         patch("src.db_utils.time.sleep") as sleep_mock:
        with pytest.raises(RuntimeError) as exc_info:
            connect_ro_with_retry("/tmp/scanner.db", "scanner.db")

    assert mc.call_count == 3
    assert sleep_mock.call_count == 2
    sleep_mock.assert_any_call(0.2)
    sleep_mock.assert_any_call(0.4)
    msg = str(exc_info.value)
    assert "3 attempts" in msg
    assert "database is locked" in msg
    assert "scanner.db" in msg


def test_connect_does_not_retry_non_operational_error():
    """A non-OperationalError must NOT trigger retry — raise immediately."""
    other_err = sqlite3.IntegrityError("constraint failed")

    with patch("src.db_utils.sqlite3.connect",
               side_effect=other_err) as mc, \
         patch("src.db_utils.time.sleep") as sleep_mock:
        with pytest.raises(sqlite3.IntegrityError):
            connect_ro_with_retry("/tmp/scanner.db", "scanner.db")

    assert mc.call_count == 1
    sleep_mock.assert_not_called()


def test_connect_retries_exactly_max_attempts_on_persistent_error():
    """Persistent OperationalError → attempts == _DB_CONNECT_MAX_ATTEMPTS."""
    op_err = sqlite3.OperationalError("disk I/O error")

    with patch("src.db_utils.sqlite3.connect",
               side_effect=op_err) as mc, \
         patch("src.db_utils.time.sleep") as sleep_mock:
        with pytest.raises(RuntimeError):
            connect_ro_with_retry("/tmp/tv.db", "tradingview.db")

    assert mc.call_count == _DB_CONNECT_MAX_ATTEMPTS
    assert sleep_mock.call_count == _DB_CONNECT_MAX_ATTEMPTS - 1


# ---------------------------------------------------------------------------
# Integration sanity check — actually open a temp db read-only
# ---------------------------------------------------------------------------

def test_connect_against_real_temp_db(tmp_path):
    """Smoke test: helper opens a real SQLite file read-only and can read it."""
    real_db = tmp_path / "scanner.db"
    pre = sqlite3.connect(str(real_db))
    pre.execute("CREATE TABLE t (x INTEGER)")
    pre.execute("INSERT INTO t VALUES (42)")
    pre.commit()
    pre.close()

    conn = connect_ro_with_retry(real_db, "scanner.db")
    try:
        assert conn.execute("SELECT x FROM t").fetchone()[0] == 42
        # Read-only: writes must be rejected
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES (1)")
    finally:
        conn.close()
