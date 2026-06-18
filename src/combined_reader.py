"""
combined_reader.py — scanner-driven as-of join across all three data sources.

scanner.db is the driving table.  For each scan row at timestamp T we carry
forward the latest GEX row and latest TradingView fundamentals row, both
constrained to a 10-minute freshness window:
  (T - 10min) <= timestamp <= T

If either GEX or TV has no row within that window, StaleDataError is raised
and the engine tick is skipped.
"""
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCANNER_DB = Path(__file__).parent.parent / "data" / "scanner.db"
GEX_DB     = Path(__file__).parent.parent / "data" / "gex.db"
TV_DB      = Path(
    "/Users/ubexbot/.openclaw/workspace-venkat/"
    "tradingView_signal_generator/data/tradingview.db"
)

# Freshness window in minutes
FRESNESS_WINDOW_MIN = 10


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class StaleDataError(Exception):
    """
    Raised when GEX or TV has no row within the 10-minute freshness window
    of the scan timestamp.  The engine tick should be skipped when this is raised.
    """
    pass


# ---------------------------------------------------------------------------
# Combined snapshot
# ---------------------------------------------------------------------------

@dataclass
class CombinedSnapshot:
    # --- From scanner.db (driving table) ----------------------------------
    scan_timestamp:   str
    spx_spot:          float
    expected_move:     float          # EM

    atm_strike:        float
    atm_call_mid:      float
    atm_put_mid:       float

    call_strike_003:   float
    call_delta:        float
    call_mid:          float
    call_10_long_strike: float
    call_10_long_mid:   float
    call_10_premium:    float
    call_20_long_strike: float
    call_20_long_mid:   float
    call_20_premium:    float

    put_strike_003:    float
    put_delta:         float
    put_mid:           float
    put_10_long_strike: float
    put_10_long_mid:    float
    put_10_premium:     float
    put_20_long_strike: float
    put_20_long_mid:    float
    put_20_premium:     float

    # --- From gex.db (as-of join, within 10-min window) -------------------
    gex_by_oi:                float
    gex_by_volume:            float
    major_positive_by_volume:  float
    major_negative_by_volume:  float
    zero_gamma:               float

    # --- From tradingview.db (as-of join, within 10-min window) -----------
    rsi:           float
    bb_upper:      float
    bb_middle:     float
    bb_lower:      float
    bb_position:   float
    bb_expanding:  bool
    adx:           float
    macd_hist:     float
    macd_expanding: bool
    adx_rising:    bool
    regime:        str

    # --- VIX (from spx_standardized, column index 31) --------------------
    vix:           Optional[float] = None  # None → fall back to expected_move * 16


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

def _parse_ts(ts: str) -> str:
    """
    Return a SQLite datetime()-compatible string from an ISO-8601 timestamp.

    Handles:
      - '2026-05-15T15:59:02-04:00'   (ISO with timezone offset)
      - '2026-05-15T15:58:03.497097-04:00'
      - '2026-05-15 15:57:59'         (space-separated, no offset)

    Returns: 'YYYY-MM-DD HH:MM:SS[.fff]' suitable for SQLite datetime().
    The timezone offset (-04:00 etc.) is stripped.
    """
    # Strip trailing timezone offset like -04:00 or +05:30
    ts = re.sub(r'[-+]\d{2}:?\d{2}$', '', ts.rstrip())
    # Replace T separator with space
    return ts.replace('T', ' ')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_scan(row: tuple) -> dict:
    """Convert a scanner row tuple into a dict."""
    return dict(
        scan_timestamp      = row[1],
        spx_spot           = row[2],
        expected_move      = row[3],
        atm_strike         = row[4],
        atm_call_mid       = row[5],
        atm_put_mid        = row[6],
        call_strike_003    = row[7],
        call_delta         = row[8],
        call_mid           = row[9],
        call_10_long_strike = row[10],
        call_10_long_mid   = row[11],
        call_10_premium    = row[12],
        call_20_long_strike = row[13],
        call_20_long_mid   = row[14],
        call_20_premium    = row[15],
        put_strike_003     = row[16],
        put_delta          = row[17],
        put_mid            = row[18],
        put_10_long_strike = row[19],
        put_10_long_mid    = row[20],
        put_10_premium     = row[21],
        put_20_long_strike = row[22],
        put_20_long_mid    = row[23],
        put_20_premium    = row[24],
    )


def _window_clause(scan_ts: str) -> tuple[str, str, str]:
    """
    Return (window_start_sql, upper_bound_sqlite, upper_bound_tv) for the 10-min freshness window.

    window_start_sql : SQL expression for SQLite datetime() — interpolate into query.
    upper_bound_sqlite : space-format string, for GEX timestamp comparison.
    upper_bound_tv     : raw ISO-format string, for TV received_at comparison.

    Rationale:
      - GEX timestamps are stored as 'YYYY-MM-DD HH:MM:SS' (space format), so the
        space-format upper bound gives correct lexicographic comparison.
      - TV received_at is stored as ISO with timezone offset
        ('2026-05-15T15:59:03.095472-04:00'), so we pass the raw scan_ts for
        correct lexicographic comparison in the TV query.
    """
    parsed_ts = _parse_ts(scan_ts)
    window_start_sql = f"datetime('{parsed_ts}', '-{FRESNESS_WINDOW_MIN} minutes')"
    # Space-format upper bound for GEX (GEX timestamps are space-separated)
    upper_bound_sqlite = parsed_ts
    # Raw scan_ts for TV (TV received_at uses ISO format with timezone)
    upper_bound_tv = scan_ts
    return window_start_sql, upper_bound_sqlite, upper_bound_tv


def _fetch_gex_in_window(scan_ts: str) -> tuple:
    """
    As-of join GEX: latest row where (scan_ts - 10min) <= timestamp <= scan_ts.

    Raises StaleDataError if no row exists in the window.
    """
    conn = sqlite3.connect(GEX_DB)
    conn.execute("PRAGMA journal_mode = WAL;")

    window_start_sql, upper_bound, _ = _window_clause(scan_ts)

    # window_start_sql is interpolated; upper_bound is a bound parameter (space format)
    sql = f"""
        SELECT * FROM gex_snapshots
        WHERE timestamp >= {window_start_sql}
          AND timestamp <= ?
        ORDER BY timestamp DESC
        LIMIT 1
    """
    row = conn.execute(sql, (upper_bound,)).fetchone()
    conn.close()

    if row is None:
        ws_sql, ub_sqlite, _ = _window_clause(scan_ts)
        raise StaleDataError(
            f"No GEX row in {FRESNESS_WINDOW_MIN}-minute window "
            f"(window_start={ws_sql}, scan_ts={scan_ts})"
        )

    return row


def _fetch_tv_in_window(scan_ts: str) -> tuple:
    """
    As-of join TradingView: latest fundamentals row where
    (scan_ts - 10min) <= received_at <= scan_ts.

    Raises StaleDataError if no row exists in the window.
    """
    conn = sqlite3.connect(str(TV_DB))
    conn.execute("PRAGMA journal_mode = WAL;")

    window_start_sql, _, upper_bound_tv = _window_clause(scan_ts)

    # window_start_sql is interpolated; upper_bound_tv is a bound parameter (raw ISO format)
    sql = f"""
        SELECT * FROM spx_standardized
        WHERE received_at     >= {window_start_sql}
          AND received_at     <= ?
          AND alert_category   = 'indicator_snapshot'
          AND alert_type       = 'fundamentals'
          AND price            IS NOT NULL
          AND bb_upper         IS NOT NULL
          AND bb_lower         IS NOT NULL
          AND bb_upper         != bb_lower
        ORDER BY received_at DESC
        LIMIT 1
    """
    row = conn.execute(sql, (upper_bound_tv,)).fetchone()
    conn.close()

    if row is None:
        ws_sql, _, ub_tv = _window_clause(scan_ts)
        raise StaleDataError(
            f"No TV row in {FRESNESS_WINDOW_MIN}-minute window "
            f"(window_start={ws_sql}, scan_ts={scan_ts})"
        )

    return row


# ---------------------------------------------------------------------------
# spx_standardized actual column indices (verified against live schema)
# ---------------------------------------------------------------------------
#  0=id  1=raw_id  2=alert_category  3=alert_type  4=symbol  5=price
#  6=received_at  7=rsi  8=macd  9=macd_signal  10=macd_hist
# 11=adx  12=vwap  13=bb_upper  14=bb_middle  15=bb_lower
# 16=pattern_description  17=signal_direction  18=metadata  19=processed
# 20=created_at  21=current_otm  22=avg_otm  23=delta_abs  24=delta_pct
# 25=vix_bucket  26=ema9  27=ema21  28=ema50  29=expected_move  30=regime
# 31=vix
# ---------------------------------------------------------------------------

TV_IDX = dict(
    price=5, rsi=7, macd=8, macd_signal=9, macd_hist=10,
    adx=11, bb_upper=13, bb_middle=14, bb_lower=15,
    ema9=26, ema21=27, ema50=28, regime=30, vix=31,
)


def _tv_row_to_snapshot(row: tuple, prior_row: Optional[tuple]) -> dict:
    """
    Build TV-derived fields dict from a spx_standardized row tuple.
    Uses known column indices (verified against live schema).
    """
    price     = float(row[TV_IDX["price"]])
    rsi       = float(row[TV_IDX["rsi"]])
    macd_hist = float(row[TV_IDX["macd_hist"]])
    adx       = float(row[TV_IDX["adx"]])
    bb_upper  = float(row[TV_IDX["bb_upper"]])
    bb_middle = float(row[TV_IDX["bb_middle"]])
    bb_lower  = float(row[TV_IDX["bb_lower"]])

    bb_position = (price - bb_lower) / (bb_upper - bb_lower)

    bb_exp = adx_rise = macd_exp = False
    if prior_row is not None:
        bb_width_prev = (
            float(prior_row[TV_IDX["bb_upper"]]) - float(prior_row[TV_IDX["bb_lower"]])
        )
        bb_width_curr = bb_upper - bb_lower
        bb_exp   = bb_width_curr > bb_width_prev
        adx_rise = adx > float(prior_row[TV_IDX["adx"]])
        macd_exp = macd_hist > float(prior_row[TV_IDX["macd_hist"]])

    # Regime (column 30) — may be NULL on older rows
    regime = "neutral"
    raw_regime = row[TV_IDX["regime"]]
    if raw_regime not in (None, ""):
        regime = str(raw_regime).strip()
        if not regime:
            regime = "neutral"

    # VIX (column 31) — may be NULL; None means fall back to expected_move * 16
    vix_raw = row[TV_IDX["vix"]]
    vix = float(vix_raw) if vix_raw is not None else None

    return dict(
        rsi            = rsi,
        bb_upper       = bb_upper,
        bb_middle      = bb_middle,
        bb_lower       = bb_lower,
        bb_position    = bb_position,
        bb_expanding   = bb_exp,
        adx            = adx,
        macd_hist      = macd_hist,
        macd_expanding = macd_exp,
        adx_rising     = adx_rise,
        regime         = regime,
        vix            = vix,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_combined_snapshot(scan_timestamp: str) -> CombinedSnapshot:
    """
    For a given scanner timestamp, return a CombinedSnapshot with:
      - scanner.dk fields (from the row at scan_timestamp, or latest available)
      - gex.db fields  (as-of join: latest row within 10-min window of scan_timestamp)
      - tradingview.db fields (as-of join: latest fundamentals within 10-min window)

    Raises StaleDataError if GEX or TV has no row in the 10-minute window.
    """
    # --- Scanner (driving table) -------------------------------------------
    conn_scan = sqlite3.connect(SCANNER_DB)
    conn_scan.execute("PRAGMA journal_mode = WAL;")
    scan_row = conn_scan.execute("""
        SELECT * FROM scan_results
        WHERE timestamp_est = ?
        LIMIT 1
    """, (scan_timestamp,)).fetchone()
    if scan_row is None:
        scan_row = conn_scan.execute("""
            SELECT * FROM scan_results
            ORDER BY timestamp_est DESC
            LIMIT 1
        """).fetchone()
    conn_scan.close()

    if scan_row is None:
        raise RuntimeError("No scan data found in scanner.db")

    scan = _row_to_scan(scan_row)

    # --- GEX (as-of join, 10-min freshness window) -------------------------
    # Raises StaleDataError if no row in window
    gex_row = _fetch_gex_in_window(scan_timestamp)
    gex = dict(
        gex_by_oi                = float(gex_row[3]),
        gex_by_volume            = float(gex_row[4]),
        major_positive_by_volume = float(gex_row[7]),
        major_negative_by_volume = float(gex_row[6]),
        zero_gamma              = float(gex_row[10]),
    )

    # --- TradingView (as-of join, 10-min freshness window) ------------------
    # Raises StaleDataError if no row in window
    conn_tv = sqlite3.connect(str(TV_DB))
    conn_tv.execute("PRAGMA journal_mode = WAL;")

    window_start_sql, _, upper_bound_tv = _window_clause(scan_timestamp)

    # Fetch prior row for expanding/rising comparison (within same window)
    prior_rows = conn_tv.execute(f"""
        SELECT * FROM spx_standardized
        WHERE received_at    >= {window_start_sql}
          AND received_at    <= ?
          AND alert_category  = 'indicator_snapshot'
          AND alert_type      = 'fundamentals'
          AND price           IS NOT NULL
          AND bb_upper        IS NOT NULL
          AND bb_lower        IS NOT NULL
          AND bb_upper        != bb_lower
        ORDER BY received_at DESC
        LIMIT 2
    """, (upper_bound_tv,)).fetchall()

    prior_row = None
    if prior_rows and len(prior_rows) >= 2:
        prior_row = prior_rows[1]   # second-most-recent = prior to latest
    elif prior_rows:
        prior_row = prior_rows[0]   # only one row available

    tv_row = _fetch_tv_in_window(scan_timestamp)
    tv_fields = _tv_row_to_snapshot(tv_row, prior_row)

    conn_tv.close()

    return CombinedSnapshot(
        # Scanner
        scan_timestamp      = scan["scan_timestamp"],
        spx_spot            = scan["spx_spot"],
        expected_move       = scan["expected_move"],
        atm_strike         = scan["atm_strike"],
        atm_call_mid       = scan["atm_call_mid"],
        atm_put_mid        = scan["atm_put_mid"],
        call_strike_003    = scan["call_strike_003"],
        call_delta         = scan["call_delta"],
        call_mid           = scan["call_mid"],
        call_10_long_strike = scan["call_10_long_strike"],
        call_10_long_mid   = scan["call_10_long_mid"],
        call_10_premium    = scan["call_10_premium"],
        call_20_long_strike = scan["call_20_long_strike"],
        call_20_long_mid   = scan["call_20_long_mid"],
        call_20_premium    = scan["call_20_premium"],
        put_strike_003     = scan["put_strike_003"],
        put_delta          = scan["put_delta"],
        put_mid            = scan["put_mid"],
        put_10_long_strike = scan["put_10_long_strike"],
        put_10_long_mid    = scan["put_10_long_mid"],
        put_10_premium     = scan["put_10_premium"],
        put_20_long_strike = scan["put_20_long_strike"],
        put_20_long_mid    = scan["put_20_long_mid"],
        put_20_premium    = scan["put_20_premium"],
        # GEX
        gex_by_oi                = gex["gex_by_oi"],
        gex_by_volume            = gex["gex_by_volume"],
        major_positive_by_volume = gex["major_positive_by_volume"],
        major_negative_by_volume = gex["major_negative_by_volume"],
        zero_gamma              = gex["zero_gamma"],
        # TradingView
        **tv_fields,
    )


def get_combined_for_latest_scan() -> CombinedSnapshot:
    """
    Get the latest scan row and as-of-join GEX + TV to its timestamp.

    Raises StaleDataError if GEX or TV has no row within the 10-minute
    freshness window of the latest scan.
    """
    conn = sqlite3.connect(SCANNER_DB)
    conn.execute("PRAGMA journal_mode = WAL;")
    latest_scan = conn.execute(
        "SELECT * FROM scan_results ORDER BY timestamp_est DESC LIMIT 1"
    ).fetchone()
    conn.close()

    if latest_scan is None:
        raise RuntimeError("No scan data found in scanner.db")

    scan_ts = latest_scan[1]
    return get_combined_snapshot(scan_ts)


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        c = get_combined_for_latest_scan()
        print(
            f"scan={c.scan_timestamp}  spx={c.spx_spot}  EM={c.expected_move}  "
            f"gex_major_pos={c.major_positive_by_volume:.2f}  "
            f"gex_major_neg={c.major_negative_by_volume:.2f}  "
            f"tv_rsi={c.rsi:.2f}  tv_bb_pos={c.bb_position:.4f}  "
            f"tv_regime={c.regime}"
        )
        print(
            f"bb_exp={c.bb_expanding}  adx={c.adx:.1f}  macd_hist={c.macd_hist:.4f}  "
            f"macd_exp={c.macd_expanding}  adx_rising={c.adx_rising}  "
            f"vix={c.vix}"
        )
    except StaleDataError as e:
        print(f"STALE: {e}")
