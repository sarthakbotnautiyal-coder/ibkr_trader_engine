"""Supabase dual-write writer for SPX 0DTE option scanner rows.

Best-effort, lazy, non-blocking writer that mirrors rows from
``data/scanner.db → scan_results`` to ``trading.scan_results`` in Supabase
immediately after the local SQLite insert succeeds.

Design contract (mirrors supabase_gex_writer.py):

  1. **Singleton** — module-level instance via :func:`get_writer`.
  2. **Lazy** — Supabase client created on first write, not at import time.
  3. **No crash on missing .env** — missing env vars only raise on actual write.
  4. **Best-effort** — on failure, logs a warning and appends to
     ``~/supabase_pending_writes_scanner.jsonl`` for later retry.
  5. **Field mapping** — ``id`` (local) → ``raw_id_local`` (cloud).
     All other column names match exactly.

Cloud target: ``trading.scan_results`` (same ``trading`` schema as GEX).
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

try:
    from supabase import Client, create_client  # type: ignore
except ImportError:  # pragma: no cover
    Client = None  # type: ignore[assignment,misc]
    create_client = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

CLOUD_SCHEMA = "trading"
CLOUD_TABLE  = "scan_results"

PENDING_WRITES_PATH = Path.home() / "supabase_pending_writes_scanner.jsonl"

_init_lock = threading.Lock()
_writer_instance: "SupabaseScannerWriter | None" = None

# Columns written to the cloud — matches trading.scan_results schema exactly.
_CLOUD_COLUMNS = [
    "raw_id_local",
    "source",
    "timestamp_est",
    "received_at",
    "spx_spot",
    "expected_move",
    "atm_strike",
    "atm_call_mid",
    "atm_put_mid",
    "call_strike_003",
    "call_delta",
    "call_mid",
    "call_10_long_strike",
    "call_10_long_mid",
    "call_10_premium",
    "call_20_long_strike",
    "call_20_long_mid",
    "call_20_premium",
    "put_strike_003",
    "put_delta",
    "put_mid",
    "put_10_long_strike",
    "put_10_long_mid",
    "put_10_premium",
    "put_20_long_strike",
    "put_20_long_mid",
    "put_20_premium",
]


def _to_cloud_row(local_id: int, row: dict[str, Any]) -> dict[str, Any]:
    """Map a local scan_results row to a cloud-ready dict.

    ``id`` (local) → ``raw_id_local`` (cloud). All other columns pass through
    unchanged. ``received_at`` is stamped now (UTC) to record when the row
    arrived in our system.
    """
    return {
        "raw_id_local":       int(local_id),
        "source":             "scanner",
        "timestamp_est":      row.get("timestamp_est"),
        "received_at":        datetime.now(timezone.utc).isoformat(),
        "spx_spot":           row.get("spx_spot"),
        "expected_move":      row.get("expected_move"),
        "atm_strike":         row.get("atm_strike"),
        "atm_call_mid":       row.get("atm_call_mid"),
        "atm_put_mid":        row.get("atm_put_mid"),
        "call_strike_003":    row.get("call_strike_003"),
        "call_delta":         row.get("call_delta"),
        "call_mid":           row.get("call_mid"),
        "call_10_long_strike":row.get("call_10_long_strike"),
        "call_10_long_mid":   row.get("call_10_long_mid"),
        "call_10_premium":    row.get("call_10_premium"),
        "call_20_long_strike":row.get("call_20_long_strike"),
        "call_20_long_mid":   row.get("call_20_long_mid"),
        "call_20_premium":    row.get("call_20_premium"),
        "put_strike_003":     row.get("put_strike_003"),
        "put_delta":          row.get("put_delta"),
        "put_mid":            row.get("put_mid"),
        "put_10_long_strike": row.get("put_10_long_strike"),
        "put_10_long_mid":    row.get("put_10_long_mid"),
        "put_10_premium":     row.get("put_10_premium"),
        "put_20_long_strike": row.get("put_20_long_strike"),
        "put_20_long_mid":    row.get("put_20_long_mid"),
        "put_20_premium":     row.get("put_20_premium"),
    }


class SupabaseScannerWriter:
    """Best-effort dual-write writer for scanner scan_results rows."""

    def __init__(self) -> None:
        self._client: Optional[Any] = None  # lazy

    def _get_client(self) -> Any:
        if self._client is None:
            with _init_lock:
                if self._client is None:
                    self._client = _create_client()
        return self._client

    def write_scan(self, local_id: int, row: dict[str, Any]) -> bool:
        """Dual-write one scan_results row to Supabase.

        Returns True on success (or filtered out), False on failure (queued
        to dead-letter file). Never raises.
        """
        try:
            cloud_row = _to_cloud_row(local_id, row)
        except Exception as e:
            logger.warning("[scanner_writer] row mapping failed (local_id=%s): %s", local_id, e)
            self._enqueue(local_id, row, error=f"mapping_error: {e}")
            return False

        try:
            client = self._get_client()
            client.schema(CLOUD_SCHEMA).table(CLOUD_TABLE).insert(cloud_row).execute()
            logger.debug("[scanner_writer] wrote scan local_id=%s ts=%s",
                         local_id, cloud_row.get("timestamp_est"))
            return True
        except Exception as e:
            logger.warning("[scanner_writer] cloud write failed (local_id=%s): %s", local_id, e)
            self._enqueue(local_id, row, error=str(e))
            return False

    def _enqueue(self, local_id: int, row: dict[str, Any], error: str) -> None:
        try:
            PENDING_WRITES_PATH.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts":       datetime.now(timezone.utc).isoformat(),
                "local_id": int(local_id),
                "row":      dict(row),
                "error":    error,
            }
            with PENDING_WRITES_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception as e:
            logger.error("[scanner_writer] CRITICAL: dead-letter write failed (local_id=%s): %s",
                         local_id, e)

    def retry_pending_writes(self) -> tuple[int, int]:
        """Retry all queued failed writes. Returns (succeeded, failed)."""
        if not PENDING_WRITES_PATH.exists():
            return (0, 0)
        succeeded = 0
        failed = 0
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
            if self.write_scan(local_id=entry.get("local_id"), row=entry.get("row", {})):
                succeeded += 1
            else:
                failed += 1
        if failed == 0:
            try:
                PENDING_WRITES_PATH.unlink()
            except FileNotFoundError:
                pass
        return (succeeded, failed)


def get_writer() -> SupabaseScannerWriter:
    """Return the module-level singleton writer (thread-safe init)."""
    global _writer_instance
    if _writer_instance is None:
        with _init_lock:
            if _writer_instance is None:
                _writer_instance = SupabaseScannerWriter()
    return _writer_instance


def _create_client() -> Any:
    """Create the Supabase client. Called lazily on first write."""
    if create_client is None:
        raise RuntimeError(
            "supabase package is not installed. "
            "Run: .venv/bin/pip install supabase>=2.31.0"
        )
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
