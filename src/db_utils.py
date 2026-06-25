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
