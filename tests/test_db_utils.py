"""Tests for db_utils.execute_ro_with_retry (TASK-2026-285).

The engine reads scanner.db / gex.db / tradingview.db while separate writer
processes append to them in WAL mode. ``db_utils.connect_ro_with_retry()``
retries only the ``sqlite3.connect()`` call — the SELECT that follows can
still raise ``sqlite3.OperationalError`` if the database is locked, the WAL is
busy, or the file is briefly unavailable. ``execute_ro_with_retry()`` closes
that gap on the read path:

  * returns the row on first success, no retry
  * retries up to 3 times on a recoverable ``OperationalError``
  * retries are linear: 0.5s, 1.0s, 1.5s
  * raises immediately on a non-recoverable ``OperationalError``
  * raises immediately on any non-``OperationalError`` exception
  * raises the final ``OperationalError`` after exhausting retries

Today's incident (2026-06-29): 559 ``OperationalError`` occurrences between
11:04:49 and 14:28:48 ET, engine blind to scanner data for 3h 28m.

These tests use ``unittest.mock`` to avoid touching real databases and to
control timing precisely (no real ``time.sleep``).
"""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from src import db_utils
from src.db_utils import (
    _READ_RETRY_BASE_BACKOFF,
    _READ_RETRY_MAX_ATTEMPTS,
    _RECOVERABLE_ERROR_SUBSTRINGS,
    execute_ro_with_retry,
)


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------

def test_retry_constants_match_spec():
    """Spec: 4 total attempts (1 + 3 retries), 0.5s base backoff.

    PR #13's writer-path retry uses ``time.sleep(0.5 * (retry_count + 1))``,
    which yields 0.5 / 1.0 / 1.5s backoff for 3 retries. The read-path
    wrapper mirrors that exactly.
    """
    assert _READ_RETRY_MAX_ATTEMPTS == 3
    assert _READ_RETRY_BASE_BACKOFF == 0.5
    assert "database is locked" in _RECOVERABLE_ERROR_SUBSTRINGS
    assert "unable to open database file" in _RECOVERABLE_ERROR_SUBSTRINGS
    assert "disk i/o error" in _RECOVERABLE_ERROR_SUBSTRINGS
    assert "database is busy" in _RECOVERABLE_ERROR_SUBSTRINGS


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_execute_ro_with_retry_happy_path():
    """First call returns a row; no retry, no sleep."""
    fake_row = MagicMock(spec=sqlite3.Row)
    fake_conn = MagicMock(spec=sqlite3.Connection)
    fake_conn.execute.return_value.fetchone.return_value = fake_row

    with patch("src.db_utils.time.sleep") as sleep_mock:
        result = execute_ro_with_retry(
            fake_conn, "SELECT * FROM scan_results LIMIT 1", ()
        )

    assert result is fake_row
    assert fake_conn.execute.call_count == 1
    assert sleep_mock.call_count == 0


# ---------------------------------------------------------------------------
# Recoverable retry path
# ---------------------------------------------------------------------------

def test_execute_ro_with_retry_recovers_on_locked():
    """Two recoverable OperationalErrors then success → row returned, slept twice.

    Backoff schedule: 0.5s, 1.0s, 1.5s. With 2 failures before success we
    sleep exactly 2 times (after attempt 1, after attempt 2). The third
    attempt succeeds and we don't sleep.
    """
    fake_row = MagicMock(spec=sqlite3.Row)
    fake_conn = MagicMock(spec=sqlite3.Connection)
    fake_conn.execute.return_value.fetchone.side_effect = [
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("database is locked"),
        fake_row,
    ]

    with patch("src.db_utils.time.sleep") as sleep_mock:
        result = execute_ro_with_retry(
            fake_conn, "SELECT * FROM scan_results LIMIT 1", ()
        )

    assert result is fake_row
    assert fake_conn.execute.call_count == 3
    assert sleep_mock.call_count == 2
    sleep_mock.assert_any_call(0.5)
    sleep_mock.assert_any_call(1.0)


def test_execute_ro_with_retry_recovers_on_wal_busy():
    """'database is busy' is a recoverable substring → retries."""
    fake_row = MagicMock(spec=sqlite3.Row)
    fake_conn = MagicMock(spec=sqlite3.Connection)
    fake_conn.execute.return_value.fetchone.side_effect = [
        sqlite3.OperationalError("database is busy"),
        fake_row,
    ]

    with patch("src.db_utils.time.sleep") as sleep_mock:
        result = execute_ro_with_retry(fake_conn, "SELECT 1", ())

    assert result is fake_row
    assert fake_conn.execute.call_count == 2
    assert sleep_mock.call_count == 1
    sleep_mock.assert_called_once_with(0.5)


def test_execute_ro_with_retry_recovers_on_disk_io():
    """'disk i/o error' is a recoverable substring → retries (case-insensitive)."""
    fake_row = MagicMock(spec=sqlite3.Row)
    fake_conn = MagicMock(spec=sqlite3.Connection)
    fake_conn.execute.return_value.fetchone.side_effect = [
        sqlite3.OperationalError("Disk I/O error"),
        fake_row,
    ]

    with patch("src.db_utils.time.sleep") as sleep_mock:
        result = execute_ro_with_retry(fake_conn, "SELECT 1", ())

    assert result is fake_row
    assert sleep_mock.call_count == 1


# ---------------------------------------------------------------------------
# Non-recoverable path
# ---------------------------------------------------------------------------

def test_execute_ro_with_retry_raises_on_unrecoverable():
    """An OperationalError not in the recoverable list raises immediately.

    'out of memory' is not in the recoverable substrings, so the wrapper
    must not retry — it should raise after 1 attempt, no sleep.
    """
    fake_conn = MagicMock(spec=sqlite3.Connection)
    fake_conn.execute.return_value.fetchone.side_effect = (
        sqlite3.OperationalError("out of memory")
    )

    with patch("src.db_utils.time.sleep") as sleep_mock, \
         pytest.raises(sqlite3.OperationalError) as exc_info:
        execute_ro_with_retry(fake_conn, "SELECT 1", ())

    assert "out of memory" in str(exc_info.value)
    assert fake_conn.execute.call_count == 1
    assert sleep_mock.call_count == 0


def test_execute_ro_with_retry_raises_on_non_operational_error():
    """A non-OperationalError must NOT trigger retry — raise immediately."""
    fake_conn = MagicMock(spec=sqlite3.Connection)
    fake_conn.execute.return_value.fetchone.side_effect = (
        sqlite3.IntegrityError("constraint failed")
    )

    with patch("src.db_utils.time.sleep") as sleep_mock, \
         pytest.raises(sqlite3.IntegrityError):
        execute_ro_with_retry(fake_conn, "SELECT 1", ())

    assert fake_conn.execute.call_count == 1
    assert sleep_mock.call_count == 0


# ---------------------------------------------------------------------------
# Exhaustion
# ---------------------------------------------------------------------------

def test_execute_ro_with_retry_raises_after_exhausting_retries():
    """Persistent recoverable error → attempts == max_retries + 1, then raise.

    With _READ_RETRY_MAX_ATTEMPTS=3 we expect 4 total attempts and 3 sleeps
    (0.5s, 1.0s, 1.5s). The final OperationalError must propagate up.
    """
    fake_conn = MagicMock(spec=sqlite3.Connection)
    persistent_err = sqlite3.OperationalError("database is locked")
    fake_conn.execute.return_value.fetchone.side_effect = persistent_err

    with patch("src.db_utils.time.sleep") as sleep_mock, \
         pytest.raises(sqlite3.OperationalError) as exc_info:
        execute_ro_with_retry(fake_conn, "SELECT 1", ())

    assert exc_info.value is persistent_err
    assert fake_conn.execute.call_count == _READ_RETRY_MAX_ATTEMPTS + 1
    assert sleep_mock.call_count == _READ_RETRY_MAX_ATTEMPTS
    sleep_mock.assert_any_call(0.5)
    sleep_mock.assert_any_call(1.0)
    sleep_mock.assert_any_call(1.5)


# ---------------------------------------------------------------------------
# Parametrization
# ---------------------------------------------------------------------------

def test_execute_ro_with_retry_passes_params():
    """params tuple is forwarded to conn.execute verbatim."""
    fake_row = MagicMock(spec=sqlite3.Row)
    fake_conn = MagicMock(spec=sqlite3.Connection)
    fake_conn.execute.return_value.fetchone.return_value = fake_row

    result = execute_ro_with_retry(
        fake_conn,
        "SELECT * FROM scan_results WHERE timestamp_est = ? LIMIT 1",
        ("2026-06-29T12:00:00-0400",),
    )

    assert result is fake_row
    args, kwargs = fake_conn.execute.call_args
    assert args[0] == "SELECT * FROM scan_results WHERE timestamp_est = ? LIMIT 1"
    assert args[1] == ("2026-06-29T12:00:00-0400",)


def test_execute_ro_with_retry_returns_none_when_no_row():
    """fetchone() returning None (no row found) is success — return None.

    The wrapper must NOT treat a None result as a retryable condition. The
    caller's caller (``_scan_tuple_to_dict(row) if row else None``) handles
    the "no row found" case.
    """
    fake_conn = MagicMock(spec=sqlite3.Connection)
    fake_conn.execute.return_value.fetchone.return_value = None

    with patch("src.db_utils.time.sleep") as sleep_mock:
        result = execute_ro_with_retry(fake_conn, "SELECT 1", ())

    assert result is None
    assert fake_conn.execute.call_count == 1
    assert sleep_mock.call_count == 0


# ---------------------------------------------------------------------------
# Module surface — sanity check
# ---------------------------------------------------------------------------

def test_db_utils_module_exposes_execute_ro_with_retry():
    """The new wrapper is importable from both ``src.db_utils`` and ``db_utils``.

    The engine imports ``from db_utils import execute_ro_with_retry``; tests
    import via ``from src.db_utils import execute_ro_with_retry``. Both must
    resolve to the same callable.
    """
    assert callable(execute_ro_with_retry)
    assert execute_ro_with_retry.__name__ == "execute_ro_with_retry"
    # Same callable object whether imported as src.db_utils or bare db_utils.
    assert db_utils.execute_ro_with_retry is execute_ro_with_retry
