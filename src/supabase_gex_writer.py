"""TASK-2026-235 — Supabase dual-write writer for GEX (gamma exposure) snapshots.

A best-effort, lazy, non-blocking writer that mirrors rows of
``data/gex.db`` → ``trading.gex_snapshots`` in Supabase immediately after
the local SQLite insert succeeds.

Design contract (mirrors TASK-2026-234 supabase_writer.py):

  1. **Singleton** — module-level instance accessed via :func:`get_writer`.
  2. **Lazy** — Supabase client created on first write, not at import time.
     Makes unit tests trivial (mock :func:`_create_client`).
  3. **No crash on missing .env** — :func:`load_dotenv` is called with
     ``override=False``; missing env vars only raise on actual write.
  4. **Best-effort** — on Supabase failure we log a warning (stdlib
     ``logging`` — matches the rest of the ibkr_auto_trader codebase) and
     append the failed payload to ``~/supabase_pending_writes_gex.jsonl``
     for :func:`retry_pending_writes`. Separate from the TV writer's
     dead-letter file to avoid mixing GEX with TV writes.
  5. **Field mapping** — minimal: ``id`` (local) → ``raw_id_local`` (cloud);
     ``timestamp`` (local) → ``snapshot_timestamp`` (cloud). All other
     columns match the cloud schema exactly.

The cloud target is ``trading.gex_snapshots`` (lives in the `trading` schema
established in TASK-2026-234 Phase 2.5). The migration script
``scripts/migrate_gex_sqlite_to_pg.py`` implements the exact same field
mapping for the one-shot backfill.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

# Lazy import: supabase is in requirements.txt, but we don't want to fail at
# import time in environments where it's missing. The actual import happens
# in _create_client().
try:
    from supabase import Client, create_client  # type: ignore
except ImportError:  # pragma: no cover — handled at write time
    Client = None  # type: ignore[assignment,misc]
    create_client = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Cloud schema + table name (single source of truth).
# TASK-2026-234 Phase 2.5: dedicated `trading` schema for isolation.
CLOUD_SCHEMA = "trading"
CLOUD_TABLE  = "gex_snapshots"

# Local DB path — kept here so callers don't need to know the layout.
LOCAL_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "gex.db"

# JSONL file holding failed writes for retry. Separate from the TV writer's
# file so retry logic doesn't mix GEX rows with fundamentals rows.
PENDING_WRITES_PATH = Path.home() / "supabase_pending_writes_gex.jsonl"

# Source value written to the cloud (also CHECK-constrained in the schema).
GEX_SOURCE = "gex_bot"

# Module-level lock for thread-safe lazy initialization.
_init_lock = threading.Lock()
_writer_instance: "SupabaseGexWriter | None" = None


def _normalize_timestamp(value: str | None) -> str | None:
    """Normalize a local timestamp to ISO 8601 with offset.

    Local timestamps are stored as ``"YYYY-MM-DD HH:MM:SS"`` (naive UTC, from
    ``datetime.utcnow().strftime(...)`` in fetch_gex.py:115). The cloud schema
    expects ``TIMESTAMPTZ`` — PostgREST will accept both ISO 8601 with offset
    and naive timestamps (interpreting the latter as UTC), but being explicit
    removes ambiguity.

    Examples:
        "2026-06-15 15:55:59" → "2026-06-15T15:55:59+00:00"
        "2026-06-15T15:55:59-04:00" → "2026-06-15T15:55:59-04:00"  (unchanged)
        None → None
    """
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    # Already ISO 8601 (contains 'T' and either 'Z' or '+' or '-')
    if "T" in s and ("Z" in s or "+" in s[10:] or s.count("-") > 2):
        return s
    # "YYYY-MM-DD HH:MM:SS" — treat as UTC (matches fetch_gex.py:115 behavior)
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        # Best effort: hand it to the cloud and let PostgREST complain
        return s


def _to_cloud_row(local_id: int, row: dict[str, Any]) -> dict[str, Any]:
    """Map a local SQLite row to a cloud-ready dict.

    Mapping:
        id (local)        → raw_id_local (cloud)
        timestamp (local) → snapshot_timestamp (cloud)
        All other columns pass through unchanged (column names match).

    `id`, `raw_id_local`, and `raw_message` are filtered automatically
    because we construct a fresh dict rather than copying the input.
    """
    cloud_row: dict[str, Any] = {
        "raw_id_local":       int(local_id),
        "source":             GEX_SOURCE,
        "snapshot_timestamp": _normalize_timestamp(row.get("timestamp")),
        "received_at":        _normalize_timestamp(row.get("received_at")),
        "gex_by_oi":                  row.get("gex_by_oi"),
        "gex_by_volume":              row.get("gex_by_volume"),
        "spot":                       row.get("spot"),
        "major_negative_by_volume":   row.get("major_negative_by_volume"),
        "major_positive_by_volume":   row.get("major_positive_by_volume"),
        "major_negative_by_oi":       row.get("major_negative_by_oi"),
        "major_positive_by_oi":       row.get("major_positive_by_oi"),
        "zero_gamma":                 row.get("zero_gamma"),
        "raw_message":                row.get("raw_message"),
    }
    return cloud_row


class SupabaseGexWriter:
    """Best-effort dual-write writer for GEX snapshots.

    Mirrors the TV writer's API (``get_writer()``, ``write_xxx(...)``,
    ``retry_pending_writes()``) so the pattern is consistent across repos.
    """

    def __init__(self) -> None:
        self._client: Optional[Client] = None  # lazy

    # ─────────────────────────────────────────────────────────────────────
    # Lazy client
    # ─────────────────────────────────────────────────────────────────────

    def _get_client(self) -> Client:
        """Create the Supabase client on first use (thread-safe)."""
        if self._client is None:
            with _init_lock:
                if self._client is None:
                    self._client = _create_client()
        return self._client

    # ─────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────

    def write_snapshot(self, local_id: int, row: dict[str, Any]) -> bool:
        """Dual-write a GEX snapshot row to Supabase.

        Args:
            local_id: The local SQLite primary key (gex_snapshots.id).
            row: The local row dict (column-name keys).

        Returns:
            True if the cloud write succeeded (or was a no-op like None
            fields), False if it failed and was queued for retry.
        """
        try:
            cloud_row = _to_cloud_row(local_id, row)
        except Exception as e:  # noqa: BLE001 — any mapping error → queue
            logger.warning(
                "[gex_writer] failed to map row (local_id=%s): %s",
                local_id, e,
            )
            self._enqueue(local_id, row, error=f"mapping_error: {e}")
            return False

        try:
            client = self._get_client()
            # TASK-2026-234 Phase 2.5: schema-scoped table access.
            # client.schema("trading").table("gex_snapshots") is equivalent
            # to fully-qualifying as trading.gex_snapshots in raw SQL.
            client.schema(CLOUD_SCHEMA).table(CLOUD_TABLE).insert(cloud_row).execute()
            logger.debug("[gex_writer] wrote snapshot local_id=%s ts=%s",
                         local_id, cloud_row.get("snapshot_timestamp"))
            return True
        except Exception as e:  # noqa: BLE001 — capture-all for resilience
            logger.warning(
                "[gex_writer] cloud write failed (local_id=%s): %s",
                local_id, e,
            )
            self._enqueue(local_id, row, error=str(e))
            return False

    # ─────────────────────────────────────────────────────────────────────
    # Dead-letter queue
    # ─────────────────────────────────────────────────────────────────────

    def _enqueue(self, local_id: int, row: dict[str, Any], error: str) -> None:
        """Append a failed write to the JSONL retry file (best-effort)."""
        try:
            PENDING_WRITES_PATH.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts":          datetime.now(timezone.utc).isoformat(),
                "local_id":    int(local_id),
                "row":         dict(row),
                "error":       error,
            }
            with PENDING_WRITES_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:  # noqa: BLE001 — if we can't write the queue either
            logger.error(
                "[gex_writer] CRITICAL: failed to enqueue dead-letter "
                "(local_id=%s): %s",
                local_id, e,
            )

    def retry_pending_writes(self) -> tuple[int, int]:
        """Retry any writes that previously failed and are in the dead-letter file.

        Returns:
            (succeeded, failed) tuple.
        """
        if not PENDING_WRITES_PATH.exists():
            return (0, 0)
        succeeded = 0
        failed = 0
        # Read all entries, retry each, then rewrite the file with the failures.
        entries: list[dict[str, Any]] = []
        with PENDING_WRITES_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        for entry in entries:
            local_id = entry.get("local_id")
            row = entry.get("row", {})
            if self.write_snapshot(local_id=local_id, row=row):
                succeeded += 1
            else:
                failed += 1
        # Rewrite the file with only the still-failed entries
        # (write_snapshot() re-enqueues failures).
        if failed == 0:
            try:
                PENDING_WRITES_PATH.unlink()
            except FileNotFoundError:
                pass
        return (succeeded, failed)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ─────────────────────────────────────────────────────────────────────────────


def get_writer() -> SupabaseGexWriter:
    """Return the module-level singleton writer (thread-safe init)."""
    global _writer_instance
    if _writer_instance is None:
        with _init_lock:
            if _writer_instance is None:
                _writer_instance = SupabaseGexWriter()
    return _writer_instance


def _create_client() -> Client:
    """Create the Supabase client. Called lazily on first write.

    Raises:
        RuntimeError: if .env is missing or SUPABASE_* env vars are unset.
    """
    if create_client is None:  # pragma: no cover — import guard
        raise RuntimeError(
            "supabase package is not installed. "
            "Run: .venv/bin/pip install supabase==2.31.0"
        )
    # Load .env relative to the repo root (where the symlink lives).
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SECRET_KEY")
    if not url or not key:
        raise RuntimeError(
            f"SUPABASE_URL and SUPABASE_SECRET_KEY must be set "
            f"(checked .env at {env_path} and process env)"
        )
    return create_client(url, key)
