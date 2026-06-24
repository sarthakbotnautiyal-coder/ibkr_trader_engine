# GEX Timestamp Join Fix for Cloud Mode

## Issue Summary

When running the engine in **cloud mode**, you were seeing repeated warnings:

```
[WARNING] No GEX row in 10-minute window (scan_ts=2026-06-24T10:10:20-0400) — skipping entry for this tick
```

This happened even though GEX data was present in Supabase. The root cause was a **timezone mismatch** in the as-of join logic.

---

## Root Cause

### The Problem

1. **Scan timestamps** are stored in ET (Eastern Time) with explicit timezone offset:
   ```
   2026-06-24T10:10:20-0400  (EDT, UTC-4)
   ```

2. **GEX snapshot timestamps** in Supabase are stored as `TIMESTAMPTZ` (with timezone info), often in UTC:
   ```
   2026-06-24T14:10:20Z  (UTC, same moment as above but different representation)
   ```

3. **The bug**: The `CloudSource._in_window()` method was stripping timezone information using `_parse_ts()`:
   - Scan: `2026-06-24T10:10:20-0400` → parsed as `2026-06-24 10:10:20` (naive, no tz)
   - GEX: `2026-06-24T14:10:20Z` → parsed as `2026-06-24 14:10:20` (naive, no tz)

4. **The comparison** would then check if GEX time (14:10:20) falls within the 10-min window:
   ```
   Window: 10:00:20 — 10:10:20 ET
   GEX:    14:10:20 (naive)
   Result: ❌ Outside window (14:10:20 > 10:10:20)
   ```

   But they actually represent the **same moment in time**!

### Why It Happened

- **GEX writer** (`supabase_gex_writer.py`) correctly normalizes timestamps to ISO 8601 with timezone:
  ```python
  dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_ET)
  return dt.isoformat()  # "2026-06-24T10:10:20-04:00"
  ```

- **Scanner writer** (`supabase_scanner_writer.py`) was NOT normalizing `timestamp_est`, just passing it through:
  ```python
  "timestamp_est": row.get("timestamp_est"),  # ❌ Not normalized
  ```

- **Reader logic** (`combined_reader.py`) was naively stripping all timezone info before comparison, losing the ability to convert between timezones.

---

## The Fix

### 1. Enhanced Timestamp Parsing in `combined_reader.py`

**File**: `src/combined_reader.py`, method `CloudSource._in_window()`

**Change**: Parse timestamps WITH timezone info, then convert to naive ET for comparison.

```python
def _in_window(self, rows: list[dict], scan_ts: str, ts_key: str) -> list[dict]:
    """Filter cloud rows to the 10-min as-of window, newest-first.

    Handles both timezone-aware and naive timestamps by normalizing to naive
    ET for comparison. Timestamps with timezone info are converted to ET before
    stripping the zone; naive timestamps are assumed to already be ET.
    """
    win_start = _window_start_dt(scan_ts)
    upper = datetime.strptime(_parse_ts(scan_ts).split('.')[0], "%Y-%m-%d %H:%M:%S")
    out = []
    for r in rows:
        raw = r.get(ts_key)
        if not raw:
            continue
        try:
            raw_str = str(raw).strip()
            # Try to parse as ISO 8601 with timezone info
            try:
                from datetime import timezone as tz_module
                iso_str = raw_str.split('.')[0]
                if 'T' in iso_str:
                    if iso_str.endswith('Z'):
                        dt_tz = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
                    else:
                        dt_tz = datetime.fromisoformat(iso_str)
                    # Convert to ET (naive) for comparison
                    if dt_tz.tzinfo is not None:
                        from zoneinfo import ZoneInfo
                        et = ZoneInfo("America/New_York")
                        dt = dt_tz.astimezone(et).replace(tzinfo=None)
                    else:
                        dt = dt_tz
                else:
                    # Naive timestamp — assume already ET
                    dt = datetime.strptime(iso_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, AttributeError):
                # Fall back to naive parsing
                dt = datetime.strptime(_parse_ts(raw_str).split('.')[0], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if win_start <= dt <= upper:
            out.append((dt, r))
    out.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in out]
```

**How it works**:
- Tries to parse as ISO 8601 with timezone (handles both `Z` for UTC and `±HH:MM` offsets)
- If timezone-aware, converts to ET, then strips timezone for comparison
- If naive, assumes already ET
- Compares apples-to-apples in the same timezone (naive ET)

### 2. Normalized Timestamp Writing in `supabase_scanner_writer.py`

**File**: `src/supabase_scanner_writer.py`

**Changes**:
1. Added `_normalize_timestamp()` function (parallel to GEX writer):
```python
def _normalize_timestamp(value: str | None) -> str | None:
    """Normalize a local timestamp to ISO 8601 with ET offset."""
    # ... converts naive ET → ISO with timezone offset
    # ... passes through timestamps that already have timezone info
```

2. Updated `_to_cloud_row()` to use it:
```python
return {
    "raw_id_local":  int(local_id),
    "source":        "scanner",
    "timestamp_est": _normalize_timestamp(row.get("timestamp_est")),  # ✅ Now normalized
    "received_at":   datetime.now(_ET).isoformat(),
    # ... rest of fields
}
```

**Why**: Ensures both scanner and GEX timestamps are written to the cloud in the same format (ISO 8601 with ET offset), eliminating the possibility of timezone mismatches.

---

## Testing the Fix

Run the included test to verify correct timestamp handling:

```bash
python3 test_gex_timestamp_fix.py
```

Expected output:
```
✅ All tests passed! Timezone handling is working correctly.

Summary of fixes:
  1. CloudSource._in_window() now properly parses timezone-aware timestamps
  2. Timestamps are converted to naive ET for comparison
  3. Scanner writer normalizes timestamp_est to ISO with ET offset
  4. Both UTC and ET timestamps are correctly handled
```

---

## What You Should See Now

After this fix:

1. **Before**: `[WARNING] No GEX row in 10-minute window...` every tick
2. **After**: GEX data is found correctly, engine processes ticks normally

Example log:
```
2026-06-24 10:11:08 ET [ENTRY] 🚀 CALL | 4500/4510 | $2.80 credit | SPX=4500 | GEX(OI)=+0.45
```

---

## Technical Details

### Timestamp Flow in Cloud Mode

```
Premium Extractor (scanner.db)
  └─→ timestamp_est: "2026-06-24 10:10:20" (naive ET)
        └─→ Scanner Writer (NORMALIZED to ISO with TZ)
            └─→ Supabase trading.scan_results: "2026-06-24T10:10:20-04:00"

GEX Extractor (gex.db)
  └─→ timestamp: "2026-06-24 10:10:20" (naive ET)
        └─→ GEX Writer (NORMALIZED to ISO with TZ)
            └─→ Supabase trading.gex_snapshots: "2026-06-24T10:10:20-04:00"

Engine (Cloud Mode)
  └─→ Read scan from Supabase: "2026-06-24T10:10:20-04:00"
  └─→ Read GEX from Supabase: "2026-06-24T10:10:20-04:00" (or UTC equivalent)
        └─→ _in_window() now correctly converts both to naive ET
            └─→ Compares: 10:10:20 == 10:10:20 ✅ MATCH
```

### Timezone Offset Examples

EDT (Eastern Daylight Time, summer):
- `-0400` or `-04:00`
- UTC-4

EST (Eastern Standard Time, winter):
- `-0500` or `-05:00`
- UTC-5

UTC:
- `Z` or `+00:00`
- UTC+0

The fix handles DST correctly by using `zoneinfo.ZoneInfo("America/New_York")`, which is DST-aware.

---

## Summary

| Aspect | Before | After |
|--------|--------|-------|
| **Scan timestamp** | ET with offset | ET with offset ✓ |
| **GEX timestamp read** | Timezone stripped → naive | Parsed with tz, converted to naive ET ✓ |
| **Comparison** | Naive UTC vs naive ET → mismatch | Both naive ET → correct match ✓ |
| **Scanner write** | Naive string | Normalized ISO with ET offset ✓ |
| **"No GEX row" errors** | Every few seconds | None (fixed) ✓ |

---

## References

- **Issue**: Cloud mode GEX join fails due to timezone mismatch in timestamp comparison
- **Files changed**: 
  - `src/combined_reader.py` (CloudSource._in_window)
  - `src/supabase_scanner_writer.py` (_normalize_timestamp, _to_cloud_row)
- **Test**: `test_gex_timestamp_fix.py` (validates all scenarios)

---

## Questions?

If you still see `[WARNING] No GEX row...` messages after applying this fix:

1. **Verify scanner writer is running**: Check if `supabase_pending_writes_scanner.jsonl` exists in your home directory
2. **Check Supabase schema**: Ensure `trading.scan_results.timestamp_est` and `trading.gex_snapshots.snapshot_timestamp` are `TIMESTAMPTZ` columns
3. **Check data freshness**: Run a quick query to see if fresh data is being written
4. **Review logs**: Check `logs/engine.*.log` for any other error messages

For immediate help, see `SUPABASE_QUICK_START.md` or `SUPABASE_SETUP.md`.
