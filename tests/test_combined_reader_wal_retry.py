"""Tests for the shared WAL-resilient read-only connect helper
(TASK-2026-235, generalized in db_utils) and the read-path retry wrapper
(TASK-2026-285).

The engine reads scanner.db / gex.db / tradingview.db while separate writer
processes append to them in WAL mode.

``db_utils.connect_ro_with_retry()``:

  * returns a connection on first success
  * opens the DB read-only (``mode=ro`` URI) — never a writable handle
  * does NOT issue ``PRAGMA journal_mode = WAL`` (that needs a write lock and
    races the writer; journal mode is persisted by the writer anyway)
  * retries up to 3 times on sqlite3.OperationalError
  * raises RuntimeError after exhausting retries
  * applies 0.2s / 0.4s / 0.6s backoff between attempts
  * does NOT retry on non-OperationalError exceptions (e.g. FileNotFoundError)

``db_utils.execute_ro_with_retry()`` (TASK-2026-285) closes the read-path gap:

  * wraps every ``conn.execute(...).fetchone()`` in combined_reader
  * retries recoverable ``OperationalError`` (locked / busy / disk I/O)
  * linear backoff: 0.5s, 1.0s, 1.5s (mirrors PR #13 writer-path retry)
  * raises immediately on non-recoverable errors

Today's incident (2026-06-29): 559 ``OperationalError`` occurrences between
11:04:49 and 14:28:48 ET, engine blind to scanner data for 3h 28m.

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
from src.combined_reader import (
    SCANNER_DB,
    GEX_DB,
    TV_DB,
    _connect_ro_with_retry,
    LocalSource,
)


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


# ---------------------------------------------------------------------------
# Read-path retry integration (TASK-2026-285)
# ---------------------------------------------------------------------------
# combined_reader wraps every fetchone() in LocalSource with
# db_utils.execute_ro_with_retry() so transient SQLite contention doesn't
# propagate raw. These tests verify the wrapper is actually called by the
# engine's read paths — guards against accidental unwrapping in a future
# refactor that would silently re-introduce today's 3h 28m blindness.


def _fake_scan_row() -> tuple:
    """Return a tuple shaped like ``SELECT * FROM scan_results``.

    The schema must satisfy ``_scan_tuple_to_dict(row)`` which indexes
    positions 1 (timestamp_est), 2 (spx_spot), 3 (expected_move), 4 (atm),
    etc. The full row tuple has 25+ columns; see ``_scan_tuple_to_dict``.
    """
    cols = [None] * 25
    cols[1] = "2026-06-29T12:00:00-0400"
    cols[2] = 5800.0
    cols[3] = 25.0
    cols[4] = 5800
    cols[5] = 12.5
    cols[6] = 13.0
    cols[7] = 5800
    cols[8] = 0.5
    cols[9] = 12.5
    cols[10] = 5790
    cols[11] = 11.0
    cols[12] = 1.10
    cols[13] = 5780
    cols[14] = 9.5
    cols[15] = 0.95
    cols[16] = 5800
    cols[17] = -0.5
    cols[18] = 13.0
    cols[19] = 5810
    cols[20] = 14.0
    cols[21] = 1.40
    cols[22] = 5820
    cols[23] = 16.0
    cols[24] = 1.60
    return tuple(cols)


def _fake_gex_row() -> tuple:
    """Return a tuple shaped like ``SELECT * FROM gex_snapshots``.

    ``_gex_tuple_to_dict`` reads positions 3 (gex_by_oi), 4 (gex_by_volume),
    6 (major_negative_by_volume), 7 (major_positive_by_volume),
    10 (zero_gamma). The row needs to be at least 11 elements long.
    """
    cols = [None] * 12
    cols[3] = 1.5     # gex_by_oi
    cols[4] = 2.5     # gex_by_volume
    cols[6] = 0.5     # major_negative_by_volume
    cols[7] = 0.7     # major_positive_by_volume
    cols[10] = 1234.5 # zero_gamma
    return tuple(cols)


def test_local_source_latest_scan_uses_execute_ro_with_retry():
    """LocalSource.latest_scan must call db_utils.execute_ro_with_retry.

    We patch both ``_connect_ro_with_retry`` and ``execute_ro_with_retry`` on
    the combined_reader module so we can verify the wrapper is invoked with
    the expected SELECT.
    """
    fake_row = _fake_scan_row()
    fake_conn = MagicMock(spec=sqlite3.Connection)
    fake_conn.execute.return_value.fetchone.return_value = fake_row

    with patch("src.combined_reader._connect_ro_with_retry",
               return_value=fake_conn), \
         patch("src.combined_reader.execute_ro_with_retry",
               return_value=fake_row) as wrapper_mock:
        result = LocalSource().latest_scan()

    assert wrapper_mock.call_count == 1
    args, kwargs = wrapper_mock.call_args
    # args: (conn, query, params)
    assert args[0] is fake_conn
    assert "SELECT * FROM scan_results" in args[1]
    assert "ORDER BY timestamp_est DESC LIMIT 1" in args[1]
    assert result is not None
    assert result["scan_timestamp"] == "2026-06-29T12:00:00-0400"
    assert result["spx_spot"] == 5800.0


def test_local_source_latest_scan_returns_none_when_no_row():
    """No scan row in scanner.db → latest_scan returns None (not an error)."""
    fake_conn = MagicMock(spec=sqlite3.Connection)

    with patch("src.combined_reader._connect_ro_with_retry",
               return_value=fake_conn), \
         patch("src.combined_reader.execute_ro_with_retry",
               return_value=None) as wrapper_mock:
        result = LocalSource().latest_scan()

    assert result is None
    assert wrapper_mock.call_count == 1


def test_local_source_scan_at_uses_execute_ro_with_retry():
    """scan_at's first fetchone is wrapped; fallback is skipped when row found.

    The wrapper mock returns a row, so the fallback ``fetchone()`` inside
    ``scan_at`` is not reached — we assert the wrapper was called exactly
    once with the parameterized WHERE clause.
    """
    fake_row = _fake_scan_row()
    fake_conn = MagicMock(spec=sqlite3.Connection)

    with patch("src.combined_reader._connect_ro_with_retry",
               return_value=fake_conn), \
         patch("src.combined_reader.execute_ro_with_retry",
               return_value=fake_row) as wrapper_mock:
        result = LocalSource().scan_at("2026-06-29T12:00:00-0400")

    assert result is not None
    assert wrapper_mock.call_count == 1
    args, kwargs = wrapper_mock.call_args
    assert args[2] == ("2026-06-29T12:00:00-0400",)
    assert "WHERE timestamp_est = ?" in args[1]


def test_local_source_gex_in_window_uses_execute_ro_with_retry():
    """LocalSource.gex_in_window must also call execute_ro_with_retry."""
    gex_row = _fake_gex_row()
    fake_conn = MagicMock(spec=sqlite3.Connection)

    with patch("src.combined_reader._connect_ro_with_retry",
               return_value=fake_conn), \
         patch("src.combined_reader.execute_ro_with_retry",
               return_value=gex_row) as wrapper_mock:
        result = LocalSource().gex_in_window("2026-06-29T12:00:00-0400")

    assert wrapper_mock.call_count == 1
    args, kwargs = wrapper_mock.call_args
    assert args[0] is fake_conn
    assert "SELECT * FROM gex_snapshots" in args[1]
    assert result is not None
    assert result["gex_by_oi"] == 1.5


def test_combined_reader_re_exports_execute_ro_with_retry():
    """The combined_reader module imports execute_ro_with_retry from db_utils.

    Guards against an accidental import-removal in a future refactor that
    would break the read-path retry silently.
    """
    from src import combined_reader
    assert hasattr(combined_reader, "execute_ro_with_retry")
    # Identity is not guaranteed because the engine imports db_utils as a bare
    # module while tests import it as src.db_utils (two distinct module objects).
    # Assert by name instead — same pattern as
    # test_combined_reader_reexports_shared_helper above.
    from src.db_utils import execute_ro_with_retry as _expected
    assert combined_reader.execute_ro_with_retry.__name__ == _expected.__name__
    assert combined_reader.execute_ro_with_retry.__name__ == "execute_ro_with_retry"
