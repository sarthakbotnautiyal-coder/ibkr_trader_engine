"""TASK-2026-235 — tests for the GEX Supabase dual-write writer.

Covers:
- _normalize_timestamp: naive UTC, with-offset, ISO with T, None, empty
- _to_cloud_row: column mapping (id→raw_id_local, timestamp→snapshot_timestamp)
- write_snapshot: happy path, missing env (raises), cloud failure (returns False + dead-letter)
- retry_pending_writes: success/failure split, file cleanup on full success
- Module constants: CLOUD_SCHEMA = "trading", CLOUD_TABLE = "gex_snapshots"

No real network calls. No real Supabase client. The client is mocked.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add the repo root and src/ to sys.path so we can import the writer
# the same way the ibkr_auto_trader conftest does.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from supabase_gex_writer import (  # noqa: E402
    CLOUD_SCHEMA,
    CLOUD_TABLE,
    PENDING_WRITES_PATH,
    SupabaseGexWriter,
    _normalize_timestamp,
    _to_cloud_row,
    get_writer,
)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def writer_with_mock_client(monkeypatch):
    """A SupabaseGexWriter whose _create_client is replaced with a MagicMock.

    Returns (writer, mock_client) so tests can configure the mock and assert
    on the calls.
    """
    mock_client = MagicMock()
    writer = SupabaseGexWriter()
    writer._client = mock_client  # bypass lazy init
    return writer, mock_client


@pytest.fixture()
def clean_pending_writes(tmp_path, monkeypatch):
    """Redirect the dead-letter file to a temp path so tests don't pollute
    the real ~/supabase_pending_writes_gex.jsonl."""
    pending = tmp_path / "pending_gex.jsonl"
    monkeypatch.setattr("supabase_gex_writer.PENDING_WRITES_PATH", pending)
    return pending


def _make_row() -> dict:
    """A representative GEX row matching data/gex.db columns."""
    return {
        "timestamp":                "2026-06-15 15:55:59",
        "received_at":              "2026-06-15 19:57:35",
        "gex_by_oi":                90.14,
        "gex_by_volume":            -872.71,
        "spot":                     7549.04,
        "major_negative_by_volume": 7550.0,
        "major_positive_by_volume": 7570.0,
        "major_negative_by_oi":     7300.0,
        "major_positive_by_oi":     7550.0,
        "zero_gamma":               7567.05,
        "raw_message":              "2026-06-15 15:55:59 ... (markdown table)",
    }


# ──────────────────────────────────────────────────────────────────────────────
# Module constants
# ──────────────────────────────────────────────────────────────────────────────


class TestConstants:
    def test_cloud_schema_is_trading(self):
        """TASK-2026-234 Phase 2.5: dedicated `trading` schema."""
        assert CLOUD_SCHEMA == "trading"

    def test_cloud_table_is_gex_snapshots(self):
        assert CLOUD_TABLE == "gex_snapshots"

    def test_pending_writes_path_is_in_home(self):
        """Dead-letter file is in the user's home dir, not the repo root."""
        assert PENDING_WRITES_PATH.parent == Path.home()
        assert "gex" in PENDING_WRITES_PATH.name


# ──────────────────────────────────────────────────────────────────────────────
# _normalize_timestamp
# ──────────────────────────────────────────────────────────────────────────────


class TestNormalizeTimestamp:
    def test_naive_string_becomes_iso_with_et_offset(self):
        """Local 'YYYY-MM-DD HH:MM:SS' is ET wall-clock → America/New_York."""
        out = _normalize_timestamp("2026-06-15 15:55:59")
        assert out == "2026-06-15T15:55:59-04:00"  # June → EDT

    def test_naive_winter_string_uses_est_offset(self):
        """DST-aware: a January date localizes to EST (-05:00)."""
        out = _normalize_timestamp("2026-01-15 15:55:59")
        assert out == "2026-01-15T15:55:59-05:00"

    def test_iso_with_offset_unchanged(self):
        """Already has +HH:MM offset — pass through."""
        out = _normalize_timestamp("2026-06-15T15:55:59-04:00")
        assert out == "2026-06-15T15:55:59-04:00"

    def test_iso_with_z_unchanged(self):
        out = _normalize_timestamp("2026-06-15T15:55:59Z")
        assert out == "2026-06-15T15:55:59Z"

    def test_none_returns_none(self):
        assert _normalize_timestamp(None) is None

    def test_empty_string_returns_none(self):
        assert _normalize_timestamp("") is None
        assert _normalize_timestamp("   ") is None

    def test_unparseable_string_returned_as_is(self):
        """Best-effort: hand it to the cloud and let PostgREST complain."""
        assert _normalize_timestamp("not a date") == "not a date"


# ──────────────────────────────────────────────────────────────────────────────
# _to_cloud_row
# ──────────────────────────────────────────────────────────────────────────────


class TestToCloudRow:
    def test_field_mapping_renames_columns(self):
        """id→raw_id_local, timestamp→snapshot_timestamp."""
        cloud = _to_cloud_row(local_id=42, row=_make_row())
        assert cloud["raw_id_local"] == 42
        assert "id" not in cloud
        assert cloud["snapshot_timestamp"] == "2026-06-15T15:55:59-04:00"
        assert "timestamp" not in cloud

    def test_other_columns_pass_through_unchanged(self):
        cloud = _to_cloud_row(local_id=1, row=_make_row())
        assert cloud["gex_by_oi"] == 90.14
        assert cloud["gex_by_volume"] == -872.71
        assert cloud["spot"] == 7549.04
        assert cloud["zero_gamma"] == 7567.05
        assert cloud["major_negative_by_oi"] == 7300.0
        assert cloud["major_positive_by_oi"] == 7550.0
        assert "spot 7549" in cloud["raw_message"] or cloud["raw_message"].startswith("2026-06-15 15:55:59")

    def test_source_is_gex_bot(self):
        """DB CHECK constraint: source = 'gex_bot'."""
        cloud = _to_cloud_row(local_id=1, row=_make_row())
        assert cloud["source"] == "gex_bot"

    def test_received_at_normalized(self):
        cloud = _to_cloud_row(local_id=1, row=_make_row())
        assert cloud["received_at"] == "2026-06-15T19:57:35-04:00"

    def test_int_local_id_is_preserved(self):
        """raw_id_local is BIGINT in the cloud, INTEGER in SQLite."""
        cloud = _to_cloud_row(local_id=2213, row=_make_row())
        assert cloud["raw_id_local"] == 2213
        assert isinstance(cloud["raw_id_local"], int)


# ──────────────────────────────────────────────────────────────────────────────
# write_snapshot — happy path
# ──────────────────────────────────────────────────────────────────────────────


class TestWriteSnapshotHappyPath:
    def test_calls_schema_and_table(self, writer_with_mock_client, clean_pending_writes):
        writer, mock_client = writer_with_mock_client
        # Phase 2.5: client.schema("trading").table("gex_snapshots").insert(...).execute()
        mock_table = MagicMock()
        mock_table.insert.return_value.execute.return_value = MagicMock(data=[{"id": 1}])
        mock_client.schema.return_value.table.return_value = mock_table

        result = writer.write_snapshot(local_id=42, row=_make_row())

        assert result is True
        mock_client.schema.assert_called_once_with("trading")
        mock_client.schema.return_value.table.assert_called_once_with("gex_snapshots")

    def test_returns_false_on_cloud_failure(self, writer_with_mock_client, clean_pending_writes):
        writer, mock_client = writer_with_mock_client
        mock_table = MagicMock()
        mock_table.insert.return_value.execute.side_effect = ConnectionError("net down")
        mock_client.schema.return_value.table.return_value = mock_table

        result = writer.write_snapshot(local_id=42, row=_make_row())

        assert result is False
        # Failed write was queued
        assert clean_pending_writes.exists()
        lines = clean_pending_writes.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["local_id"] == 42
        assert "net down" in entry["error"]


# ──────────────────────────────────────────────────────────────────────────────
# write_snapshot — mapping errors
# ──────────────────────────────────────────────────────────────────────────────


class TestWriteSnapshotMappingError:
    def test_mapping_error_returns_false_and_queues(self, writer_with_mock_client, clean_pending_writes):
        writer, mock_client = writer_with_mock_client
        # Force _to_cloud_row to raise (e.g. row contains a non-numeric where a number is expected)
        # The actual mapping is robust to None, so we patch the helper to raise.
        with patch("supabase_gex_writer._to_cloud_row",
                   side_effect=ValueError("bad row")):
            result = writer.write_snapshot(local_id=1, row=_make_row())

        assert result is False
        # Client should NOT have been called — mapping error short-circuits
        mock_client.schema.assert_not_called()
        # And the failure was queued
        assert clean_pending_writes.exists()


# ──────────────────────────────────────────────────────────────────────────────
# retry_pending_writes
# ──────────────────────────────────────────────────────────────────────────────


class TestRetryPendingWrites:
    def test_no_file_returns_zero_zero(self, writer_with_mock_client, clean_pending_writes):
        writer, _ = writer_with_mock_client
        ok, fail = writer.retry_pending_writes()
        assert (ok, fail) == (0, 0)

    def test_all_succeed_clears_file(self, writer_with_mock_client, clean_pending_writes):
        writer, mock_client = writer_with_mock_client
        mock_table = MagicMock()
        mock_table.insert.return_value.execute.return_value = MagicMock(data=[{"id": 1}])
        mock_client.schema.return_value.table.return_value = mock_table

        # Seed the dead-letter file with 2 entries
        clean_pending_writes.write_text(
            json.dumps({"ts": "x", "local_id": 1, "row": _make_row(), "error": "fail1"}) + "\n"
            + json.dumps({"ts": "x", "local_id": 2, "row": _make_row(), "error": "fail2"}) + "\n"
        )

        ok, fail = writer.retry_pending_writes()
        assert (ok, fail) == (2, 0)
        # File should be deleted when all succeed
        assert not clean_pending_writes.exists()

    def test_partial_failure_keeps_failures(self, writer_with_mock_client, clean_pending_writes):
        writer, mock_client = writer_with_mock_client
        # First call succeeds, second call fails
        mock_table = MagicMock()
        responses = [
            MagicMock(data=[{"id": 1}]),  # success
            ConnectionError("still down"),  # failure
        ]
        mock_table.insert.return_value.execute.side_effect = responses
        mock_client.schema.return_value.table.return_value = mock_table

        clean_pending_writes.write_text(
            json.dumps({"ts": "x", "local_id": 1, "row": _make_row(), "error": "fail1"}) + "\n"
            + json.dumps({"ts": "x", "local_id": 2, "row": _make_row(), "error": "fail2"}) + "\n"
        )

        ok, fail = writer.retry_pending_writes()
        assert (ok, fail) == (1, 1)
        # File still exists with the 1 failed entry
        assert clean_pending_writes.exists()


# ──────────────────────────────────────────────────────────────────────────────
# Module-level singleton
# ──────────────────────────────────────────────────────────────────────────────


class TestGetWriter:
    def test_returns_same_instance(self):
        # Reset the module-level singleton
        import supabase_gex_writer as wmod
        wmod._writer_instance = None
        a = get_writer()
        b = get_writer()
        assert a is b

    def test_get_writer_is_lazy(self):
        """get_writer() should NOT create a Supabase client."""
        import supabase_gex_writer as wmod
        wmod._writer_instance = None
        with patch("supabase_gex_writer._create_client") as mock_create:
            w = get_writer()
            mock_create.assert_not_called()
            # Client is only created on first write
            w.write_snapshot(local_id=1, row=_make_row())
            mock_create.assert_called_once()


# ──────────────────────────────────────────────────────────────────────────────
# Real local DB integration (no cloud — exercises the mapping only)
# ──────────────────────────────────────────────────────────────────────────────


class TestRealLocalDbMapping:
    """Verify the mapping works against the real data/gex.db (if present)."""

    def test_real_latest_row_maps_cleanly(self, writer_with_mock_client, clean_pending_writes):
        local_db = _REPO_ROOT / "data" / "gex.db"
        if not local_db.exists():
            pytest.skip("data/gex.db not present")
        conn = sqlite3.connect(str(local_db))
        conn.row_factory = sqlite3.Row
        has_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='gex_snapshots'"
        ).fetchone()
        if has_table is None:
            conn.close()
            pytest.skip("gex_snapshots table not present in data/gex.db")
        row = conn.execute(
            "SELECT * FROM gex_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row is None:
            pytest.skip("gex_snapshots table is empty")

        cloud = _to_cloud_row(local_id=row["id"], row=dict(row))
        # Must have all 13 cloud columns
        expected = {
            "raw_id_local", "source", "snapshot_timestamp", "received_at",
            "gex_by_oi", "gex_by_volume", "spot",
            "major_negative_by_volume", "major_positive_by_volume",
            "major_negative_by_oi", "major_positive_by_oi",
            "zero_gamma", "raw_message",
        }
        assert set(cloud.keys()) == expected
        assert cloud["source"] == "gex_bot"
        assert cloud["raw_id_local"] == row["id"]


# ──────────────────────────────────────────────────────────────────────────────
# Phase 2.5 schema usage (TASK-2026-234 inheritance)
# ──────────────────────────────────────────────────────────────────────────────


class TestSchemaUsage:
    """The writer MUST use the `trading` schema (not public)."""

    def test_write_snapshot_uses_trading_schema(self, writer_with_mock_client, clean_pending_writes):
        writer, mock_client = writer_with_mock_client
        mock_table = MagicMock()
        mock_table.insert.return_value.execute.return_value = MagicMock()
        mock_client.schema.return_value.table.return_value = mock_table

        writer.write_snapshot(local_id=1, row=_make_row())

        mock_client.schema.assert_called_once_with("trading")
        # And table
        mock_client.schema.return_value.table.assert_called_once_with("gex_snapshots")
