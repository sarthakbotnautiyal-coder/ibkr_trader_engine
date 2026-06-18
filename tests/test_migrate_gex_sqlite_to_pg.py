"""TASK-2026-235 — tests for the GEX SQLite -> Supabase migration row mapping.

The migration's job is to transform local SQLite rows into cloud-ready dicts
without making any network calls. These tests cover the mapping logic,
column-set integrity, and the iteration/count helpers.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock, patch

import pytest

# Reuse the import convention from conftest.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from scripts.migrate_gex_sqlite_to_pg import (  # noqa: E402
    CLOUD_COLUMNS,
    DB_PATH,
    LOCAL_TABLE,
    _get_client,
    count_local_snapshots,
    iter_local_snapshots,
    map_local_row_to_cloud,
    migrate,
)


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def fake_local_db(tmp_path) -> Path:
    """Create a temp SQLite DB with a representative set of GEX rows."""
    db_path = tmp_path / "fake_gex.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        f"""
        CREATE TABLE {LOCAL_TABLE} (
            id                       INTEGER PRIMARY KEY,
            timestamp                TEXT    NOT NULL UNIQUE,
            received_at              TEXT    NOT NULL,
            gex_by_oi                REAL,
            gex_by_volume            REAL,
            spot                     REAL,
            major_negative_by_volume REAL,
            major_positive_by_volume REAL,
            major_negative_by_oi     REAL,
            major_positive_by_oi     REAL,
            zero_gamma               REAL,
            raw_message              TEXT
        )
        """
    )
    rows = [
        (1, "2026-06-15 15:55:59", "2026-06-15 19:57:35",
         90.14, -872.71, 7549.04,
         7550.0, 7570.0, 7300.0, 7550.0, 7567.05, "msg 1"),
        (2, "2026-06-15 15:50:00", "2026-06-15 19:51:00",
         85.5, -800.0, 7540.0,
         7545.0, 7565.0, 7290.0, 7545.0, 7560.0, "msg 2"),
        (3, "2026-06-15 15:45:00", "2026-06-15 19:46:00",
         None, None, 7530.0,
         None, None, None, None, None, None),  # nulls allowed
    ]
    conn.executemany(
        f"INSERT INTO {LOCAL_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db_path


# ──────────────────────────────────────────────────────────────────────────────
# map_local_row_to_cloud
# ──────────────────────────────────────────────────────────────────────────────


class TestMapLocalRowToCloud:
    def test_renames_id_to_raw_id_local(self, fake_local_db):
        conn = sqlite3.connect(str(fake_local_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT * FROM {LOCAL_TABLE} WHERE id=1").fetchone()
        cloud = map_local_row_to_cloud(row)
        assert cloud["raw_id_local"] == 1
        assert "id" not in cloud

    def test_renames_timestamp_to_snapshot_timestamp(self, fake_local_db):
        conn = sqlite3.connect(str(fake_local_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT * FROM {LOCAL_TABLE} WHERE id=1").fetchone()
        cloud = map_local_row_to_cloud(row)
        assert cloud["snapshot_timestamp"] == "2026-06-15T15:55:59+00:00"
        assert "timestamp" not in cloud

    def test_source_is_hardcoded_to_gex_bot(self, fake_local_db):
        """DB CHECK constraint: source = 'gex_bot'."""
        conn = sqlite3.connect(str(fake_local_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT * FROM {LOCAL_TABLE} WHERE id=1").fetchone()
        cloud = map_local_row_to_cloud(row)
        assert cloud["source"] == "gex_bot"

    def test_passes_through_gex_values(self, fake_local_db):
        conn = sqlite3.connect(str(fake_local_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT * FROM {LOCAL_TABLE} WHERE id=1").fetchone()
        cloud = map_local_row_to_cloud(row)
        assert cloud["gex_by_oi"] == 90.14
        assert cloud["gex_by_volume"] == -872.71
        assert cloud["spot"] == 7549.04
        assert cloud["zero_gamma"] == 7567.05

    def test_nulls_preserved(self, fake_local_db):
        conn = sqlite3.connect(str(fake_local_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT * FROM {LOCAL_TABLE} WHERE id=3").fetchone()
        cloud = map_local_row_to_cloud(row)
        assert cloud["gex_by_oi"] is None
        assert cloud["gex_by_volume"] is None
        assert cloud["spot"] == 7530.0  # spot is non-null
        assert cloud["zero_gamma"] is None
        assert cloud["raw_message"] is None

    def test_column_set_integrity(self, fake_local_db):
        """Mapping must produce exactly CLOUD_COLUMNS keys (no drift)."""
        conn = sqlite3.connect(str(fake_local_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT * FROM {LOCAL_TABLE} WHERE id=1").fetchone()
        cloud = map_local_row_to_cloud(row)
        assert set(cloud.keys()) == set(CLOUD_COLUMNS)
        assert len(cloud) == 13  # 13 cloud columns


# ──────────────────────────────────────────────────────────────────────────────
# iter_local_snapshots / count_local_snapshots
# ──────────────────────────────────────────────────────────────────────────────


class TestLocalIteration:
    def test_count_returns_total(self, fake_local_db):
        assert count_local_snapshots(fake_local_db) == 3

    def test_iter_yields_batches(self, fake_local_db):
        batches = list(iter_local_snapshots(fake_local_db, batch_size=2))
        assert len(batches) == 2  # 3 rows / 2 per batch = 2 batches
        assert len(batches[0]) == 2
        assert len(batches[1]) == 1

    def test_iter_orders_by_id_ascending(self, fake_local_db):
        batches = list(iter_local_snapshots(fake_local_db, batch_size=10))
        ids = [row["id"] for batch in batches for row in batch]
        assert ids == [1, 2, 3]

    def test_missing_db_raises(self, tmp_path):
        missing = tmp_path / "no_such.db"
        with pytest.raises(FileNotFoundError):
            count_local_snapshots(missing)


# ──────────────────────────────────────────────────────────────────────────────
# Migration end-to-end (mocked supabase)
# ──────────────────────────────────────────────────────────────────────────────


class TestMigrationWithMockedClient:
    def _patched_client(self):
        client = MagicMock()
        builder = MagicMock()
        # Phase 2.5: client.schema("trading").table("gex_snapshots")
        client.schema.return_value.table.return_value = builder
        builder.upsert.return_value.execute.return_value = MagicMock()
        return client, builder

    def test_dry_run_does_not_write(self, monkeypatch, fake_local_db, caplog):
        import logging
        monkeypatch.setattr("scripts.migrate_gex_sqlite_to_pg.DB_PATH", fake_local_db)
        client, builder = self._patched_client()
        monkeypatch.setattr(
            "scripts.migrate_gex_sqlite_to_pg._get_client", lambda: client
        )

        with caplog.at_level(logging.INFO, logger="migrate_gex"):
            rc = migrate(dry_run=True)
        assert rc == 0
        # No upserts happened
        builder.upsert.assert_not_called()
        # But the count was reported
        assert any("Local GEX rows: 3" in r.message for r in caplog.records)

    def test_real_run_upserts_in_batches(self, monkeypatch, fake_local_db, capsys):
        monkeypatch.setattr("scripts.migrate_gex_sqlite_to_pg.DB_PATH", fake_local_db)
        client, builder = self._patched_client()
        monkeypatch.setattr(
            "scripts.migrate_gex_sqlite_to_pg._get_client", lambda: client
        )

        rc = migrate(dry_run=False)
        assert rc == 0

        # Phase 2.5: schema-scoped mock chain
        client.schema.assert_called_with("trading")
        client.schema.return_value.table.assert_called_with("gex_snapshots")
        builder = client.schema.return_value.table.return_value
        # 3 rows in 1 batch of 500
        assert builder.upsert.call_count == 1
        rows_sent = builder.upsert.call_args.args[0]
        assert len(rows_sent) == 3
        # upsert was called with ignore_duplicates=True (idempotent)
        assert builder.upsert.call_args.kwargs.get("ignore_duplicates") is True

    def test_uses_500_batch_size(self, monkeypatch, tmp_path):
        # 1,203 rows in 3 batches: 500 + 500 + 203
        db_path = tmp_path / "big_gex.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            f"""
            CREATE TABLE {LOCAL_TABLE} (
                id INTEGER PRIMARY KEY, timestamp TEXT UNIQUE NOT NULL,
                received_at TEXT NOT NULL, gex_by_oi REAL, gex_by_volume REAL,
                spot REAL, major_negative_by_volume REAL, major_positive_by_volume REAL,
                major_negative_by_oi REAL, major_positive_by_oi REAL,
                zero_gamma REAL, raw_message TEXT
            )
            """
        )
        # Generate 1,203 unique timestamps (i.e. each minute for ~20 hours, 4x dupes stripped)
        # Simplest: use a counter that scales — day * 86400 + second offset
        for i in range(1, 1204):
            ts = f"2026-06-15 {i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}"
            conn.execute(
                f"INSERT INTO {LOCAL_TABLE} VALUES (?, ?, ?, 0, 0, 0, 0, 0, 0, 0, 0, '')",
                (i, ts, "2026-06-15 19:00:00"),
            )
        conn.commit()
        conn.close()

        monkeypatch.setattr("scripts.migrate_gex_sqlite_to_pg.DB_PATH", db_path)
        client, _ = self._patched_client()
        monkeypatch.setattr(
            "scripts.migrate_gex_sqlite_to_pg._get_client", lambda: client
        )

        rc = migrate(dry_run=False)
        assert rc == 0
        # Phase 2.5: schema-scoped mock chain
        builder = client.schema.return_value.table.return_value
        assert builder.upsert.call_count == 3
        sizes = [len(c.args[0]) for c in builder.upsert.call_args_list]
        assert sizes == [500, 500, 203]


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────


class TestMigrationConstants:
    def test_cloud_schema_is_trading(self):
        from scripts.migrate_gex_sqlite_to_pg import CLOUD_SCHEMA
        assert CLOUD_SCHEMA == "trading"

    def test_cloud_table_is_gex_snapshots(self):
        from scripts.migrate_gex_sqlite_to_pg import CLOUD_TABLE
        assert CLOUD_TABLE == "gex_snapshots"
