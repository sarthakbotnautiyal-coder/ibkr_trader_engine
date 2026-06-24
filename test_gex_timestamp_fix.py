#!/usr/bin/env python3
"""Test to verify the GEX timestamp join fix for cloud mode.

This test validates that the CloudSource._in_window() method correctly
handles timezone-aware timestamps when filtering GEX snapshots within
the 10-minute freshness window.
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

# Add src to path
_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from combined_reader import CloudSource, _window_start_dt, _parse_ts


def test_in_window_with_utc_timestamps():
    """Test that GEX rows with UTC timestamps are correctly matched."""
    source = CloudSource()

    # Scan timestamp in ET with EDT offset: 2026-06-24T10:10:20-04:00
    scan_ts = "2026-06-24T10:10:20-0400"

    # Create mock GEX rows with UTC timestamps (same moment in time)
    # 2026-06-24T14:10:20Z is the same as 2026-06-24T10:10:20-04:00
    mock_rows = [
        {
            "id": 1,
            "snapshot_timestamp": "2026-06-24T14:10:20Z",  # UTC
            "gex_by_oi": 0.5,
        },
        {
            "id": 2,
            "snapshot_timestamp": "2026-06-24T14:05:00Z",  # 5 min before, still in window
            "gex_by_oi": 0.4,
        },
        {
            "id": 3,
            "snapshot_timestamp": "2026-06-24T13:59:00Z",  # ~11 min before, outside window
            "gex_by_oi": 0.3,
        },
    ]

    # Filter to the window
    windowed = source._in_window(mock_rows, scan_ts, "snapshot_timestamp")

    # Should have 2 rows (ids 1 and 2)
    assert len(windowed) == 2, f"Expected 2 rows in window, got {len(windowed)}"
    assert windowed[0]["id"] == 1, "First row should be id 1 (most recent)"
    assert windowed[1]["id"] == 2, "Second row should be id 2"
    print("✓ UTC timestamps correctly matched to ET scan time")


def test_in_window_with_et_timestamps():
    """Test that GEX rows with ET timestamps are correctly matched."""
    source = CloudSource()

    scan_ts = "2026-06-24T10:10:20-0400"  # EDT

    # Create mock GEX rows with ET timestamps (with EDT offset)
    mock_rows = [
        {
            "id": 1,
            "snapshot_timestamp": "2026-06-24T10:10:20-04:00",  # EDT (exact match)
            "gex_by_oi": 0.5,
        },
        {
            "id": 2,
            "snapshot_timestamp": "2026-06-24T10:05:00-04:00",  # 5 min before
            "gex_by_oi": 0.4,
        },
    ]

    windowed = source._in_window(mock_rows, scan_ts, "snapshot_timestamp")

    assert len(windowed) == 2, f"Expected 2 rows in window, got {len(windowed)}"
    assert windowed[0]["id"] == 1
    print("✓ ET timestamps with offset correctly matched")


def test_in_window_with_naive_timestamps():
    """Test that naive (no timezone) timestamps are handled."""
    source = CloudSource()

    scan_ts = "2026-06-24T10:10:20-0400"

    # Create mock GEX rows with naive timestamps (assumed to be ET)
    mock_rows = [
        {
            "id": 1,
            "snapshot_timestamp": "2026-06-24 10:10:20",  # Naive, assumed ET
            "gex_by_oi": 0.5,
        },
        {
            "id": 2,
            "snapshot_timestamp": "2026-06-24 10:05:00",  # 5 min before
            "gex_by_oi": 0.4,
        },
    ]

    windowed = source._in_window(mock_rows, scan_ts, "snapshot_timestamp")

    assert len(windowed) == 2, f"Expected 2 rows in window, got {len(windowed)}"
    print("✓ Naive timestamps correctly handled (assumed ET)")


def test_scanner_timestamp_normalization():
    """Test that scanner writer normalizes timestamps correctly."""
    from supabase_scanner_writer import _normalize_timestamp

    # Naive ET timestamp
    result = _normalize_timestamp("2026-06-24 10:10:20")
    assert "T" in result, "Should have T separator"
    assert "-04:" in result or "-05:" in result, f"Should have ET offset, got {result}"
    print(f"✓ Naive timestamp normalized: {result}")

    # Already ISO with UTC
    result = _normalize_timestamp("2026-06-24T14:10:20Z")
    assert result == "2026-06-24T14:10:20Z", "Should pass through UTC unchanged"
    print("✓ UTC timestamp passed through unchanged")

    # Already ISO with EDT offset
    result = _normalize_timestamp("2026-06-24T10:10:20-04:00")
    assert result == "2026-06-24T10:10:20-04:00", "Should pass through EDT unchanged"
    print("✓ EDT timestamp passed through unchanged")

    # None
    result = _normalize_timestamp(None)
    assert result is None, "Should handle None"
    print("✓ None handled correctly")


if __name__ == "__main__":
    print("=" * 70)
    print("Testing GEX timestamp join fix for cloud mode")
    print("=" * 70)
    print()

    try:
        test_in_window_with_utc_timestamps()
        test_in_window_with_et_timestamps()
        test_in_window_with_naive_timestamps()
        test_scanner_timestamp_normalization()

        print()
        print("=" * 70)
        print("✅ All tests passed! Timezone handling is working correctly.")
        print("=" * 70)
        print()
        print("Summary of fixes:")
        print("  1. CloudSource._in_window() now properly parses timezone-aware timestamps")
        print("  2. Timestamps are converted to naive ET for comparison")
        print("  3. Scanner writer normalizes timestamp_est to ISO with ET offset")
        print("  4. Both UTC and ET timestamps are correctly handled")

    except AssertionError as e:
        print(f"❌ Test failed: {e}")
        sys.exit(1)
