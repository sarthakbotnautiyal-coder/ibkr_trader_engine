"""
db_utils.py — WAL-safe read-only SQLite access for the engine's readers.

The engine is a *reader* of scanner.db / gex.db / tradingview.db; each file is
written continuously by a separate extractor process in WAL mode. Two things
previously made reads fragile and surfaced as
``sqlite3.OperationalError: unable to open database file``:

  1. Issuing ``PRAGMA journal_mode = WAL`` on every read. That pragma needs a
     write lock and momentarily touches the -wal/-shm sidecars, so it races the
     writer. It is also redundant — journal mode is persisted in the DB header
     by the writer, so a reader never needs to (re)set it.
  2. No connect timeout / retry, so any transient contention failed the read.

This module centralizes the fix: open every local DB read-only via a URI
("mode=ro"), with a busy timeout and short-backoff retry, and never set the
journal mode from the reader. A read-only connection coexists cleanly with the
WAL writer.
"""
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional, Union

_DB_CONNECT_MAX_ATTEMPTS = 3
_DB_CONNECT_BACKOFFS = (0.2, 0.4, 0.6)
_DB_CONNECT_TIMEOUT = 2.0


def connect_ro_with_retry(
    db_path: Union[str, Path], label: Optional[str] = None
) -> sqlite3.Connection:
    """Open a local SQLite DB read-only, WAL-safe, with short-backoff retry.

    Reader-writer contention on a shared WAL database can surface as
    ``sqlite3.OperationalError: unable to open database file`` even though the
    file exists and is readable. We open read-only (no write lock, no journal
    pragma) with a connect/busy timeout and retry a few times before giving up.

    Args:
        db_path: Path to the SQLite database file.
        label:   Optional human-readable name used in the error message.
    """
    db_path = str(db_path)
    label = label or db_path
    uri = f"file:{db_path}?mode=ro"
    last_err: Optional[Exception] = None
    for attempt in range(1, _DB_CONNECT_MAX_ATTEMPTS + 1):
        try:
            return sqlite3.connect(uri, uri=True, timeout=_DB_CONNECT_TIMEOUT)
        except sqlite3.OperationalError as e:
            last_err = e
            if attempt < _DB_CONNECT_MAX_ATTEMPTS:
                time.sleep(_DB_CONNECT_BACKOFFS[attempt - 1])
                continue
            break
    raise RuntimeError(
        f"Failed to open {label} ({db_path}) read-only after "
        f"{_DB_CONNECT_MAX_ATTEMPTS} attempts: {last_err}"
    )


# ---------------------------------------------------------------------------
# Read-path retry (TASK-2026-285)
# ---------------------------------------------------------------------------
# connect_ro_with_retry() above only retries the sqlite3.connect() call. The
# SELECT that follows can still raise sqlite3.OperationalError if the database
# is locked, the WAL is busy, or the file is briefly unavailable — and that
# error propagates raw to the engine. Today's incident (2026-06-29): 559 such
# errors between 11:04:49 and 14:28:48 ET, engine blind to scanner data for
# 3h 28m. This wrapper closes that gap on the read path, mirroring the
# writer-path retry added to trades_db.get_conn in PR #13 (commit 03d0af3).

_READ_RETRY_MAX_ATTEMPTS = 3
_READ_RETRY_BASE_BACKOFF = 0.5

_RECOVERABLE_ERROR_SUBSTRINGS = (
    "database is locked",
    "unable to open database file",
    "disk i/o error",
    "database is busy",
)


def execute_ro_with_retry(
    conn: sqlite3.Connection,
    query: str,
    params: tuple = (),
    *,
    max_retries: int = _READ_RETRY_MAX_ATTEMPTS,
    base_backoff: float = _READ_RETRY_BASE_BACKOFF,
) -> sqlite3.Row:
    """Run a read query with retry on OperationalError.

    Mirrors the writer-path retry in trades_db.get_conn (PR #13). The
    underlying ``sqlite3.connect()`` may succeed but the SELECT can still
    raise ``OperationalError`` if the database is locked, the WAL is busy,
    or the file is briefly unavailable.

    Today's incident: 559 such errors between 11:04:49 and 14:28:48 ET,
    engine blind for 3h 28m.

    Args:
        conn:         A read-only SQLite connection (typically from
                      ``connect_ro_with_retry``).
        query:        SQL SELECT statement (parameterized).
        params:       Positional parameters for the query.
        max_retries:  Number of retries after the first attempt (so total
                      attempts = ``max_retries + 1``). Default 3 → 4 total.
        base_backoff: Initial backoff in seconds; each subsequent retry
                      sleeps ``base_backoff * (attempt + 1)``.

    Returns:
        A single ``sqlite3.Row`` (the result of ``fetchone()``).

    Raises:
        sqlite3.OperationalError: If the error is non-recoverable, or after
            all retries are exhausted.
    """
    last_err: Optional[sqlite3.OperationalError] = None
    total_attempts = max_retries + 1
    for attempt in range(total_attempts):
        try:
            return conn.execute(query, params).fetchone()
        except sqlite3.OperationalError as e:
            last_err = e
            msg = str(e).lower()
            recoverable = any(s in msg for s in _RECOVERABLE_ERROR_SUBSTRINGS)
            if not recoverable or attempt == max_retries:
                raise
            backoff = base_backoff * (attempt + 1)
            logging.warning(
                "execute_ro_with_retry: %s on attempt %d/%d, sleeping %.2fs",
                e, attempt + 1, total_attempts, backoff,
            )
            time.sleep(backoff)
    # Unreachable, but keeps type-checkers happy (last_err is always set if
    # we exit the loop without returning).
    assert last_err is not None  # pragma: no cover
    raise last_err
