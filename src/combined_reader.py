"""
combined_reader.py — scanner-driven as-of join across all three data sources.

This is THE data seam for the engine. Everything downstream (TickProcessor,
risk_manager, day_gate, entry/exit rules) consumes the CombinedSnapshot this
module produces. All three run modes converge here:

  - LOCAL    : scanner/gex/tv rows sourced from local SQLite (config paths)
  - CLOUD    : same rows sourced from Supabase (trading.* tables)
  - BACKTEST : historical rows for a single date, iterated chronologically
               (LOCAL SQLite only — backtesting is not supported in CLOUD mode)

The as-of join (10-minute freshness window), the TV indicator math, the regime
and VIX extraction are SHARED across all modes — only the row-fetch primitives
differ per source. This guarantees that any change to the join/indicator logic
is reflected identically in local, cloud, and backtest.

scanner.db is the driving table. For each scan row at timestamp T we carry
forward the latest GEX row and latest TradingView fundamentals row, both
constrained to a 10-minute freshness window:
  (T - 10min) <= timestamp <= T

If either GEX or TV has no row within that window, StaleDataError is raised
and the engine tick is skipped.
"""
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Iterator

from config import CONFIG

# ---------------------------------------------------------------------------
# Paths (config-driven — resolved relative to the engine root)
# ---------------------------------------------------------------------------

_ENGINE_ROOT = Path(__file__).parent.parent


def _resolve(path_str: str) -> Path:
    """Resolve a config path: absolute as-is, else relative to engine root."""
    p = Path(path_str)
    if p.is_absolute():
        return p
    return (_ENGINE_ROOT / p).resolve()


_DS = CONFIG.get("data_sources", {})
SCANNER_DB = _resolve(_DS.get("scanner_db", "../premium_extractor/data/scanner.db"))
GEX_DB     = _resolve(_DS.get("gex_db", "../gex_extractor/data/gex.db"))
TV_DB      = _resolve(_DS.get("tradingview_db", "../tradingView_signal_generator/data/tradingview.db"))

# Freshness window in minutes
FRESNESS_WINDOW_MIN = 10


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class StaleDataError(Exception):
    """
    Raised when GEX or TV has no row within the 10-minute freshness window
    of the scan timestamp. The engine tick should be skipped when this is raised.
    """
    pass


# ---------------------------------------------------------------------------
# WAL resilience (TASK-2026-235)
# ---------------------------------------------------------------------------

_TV_CONNECT_MAX_ATTEMPTS = 3
_TV_CONNECT_BACKOFFS = (0.2, 0.4, 0.6)
_TV_CONNECT_TIMEOUT = 2.0


def _connect_tv_with_retry() -> "sqlite3.Connection":
    """Open tradingview.db with WAL-aware retry.

    Reader-writer contention on the shared tradingview.db can surface as
    ``sqlite3.OperationalError: unable to open database file`` even though
    the file exists. Retry with short backoff and a 2s connect timeout.
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, _TV_CONNECT_MAX_ATTEMPTS + 1):
        try:
            conn = sqlite3.connect(str(TV_DB), timeout=_TV_CONNECT_TIMEOUT)
            conn.execute("PRAGMA journal_mode = WAL;")
            return conn
        except sqlite3.OperationalError as e:
            last_err = e
            if attempt < _TV_CONNECT_MAX_ATTEMPTS:
                time.sleep(_TV_CONNECT_BACKOFFS[attempt - 1])
                continue
            break
    raise RuntimeError(
        f"Failed to open tradingview.db after {_TV_CONNECT_MAX_ATTEMPTS} attempts: {last_err}"
    )


# ---------------------------------------------------------------------------
# Combined snapshot
# ---------------------------------------------------------------------------

@dataclass
class CombinedSnapshot:
    # --- From scanner (driving table) -------------------------------------
    scan_timestamp:   str
    spx_spot:          float
    expected_move:     float

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

    # --- From GEX (as-of join, within 10-min window) ----------------------
    gex_by_oi:                float
    gex_by_volume:            float
    major_positive_by_volume:  float
    major_negative_by_volume:  float
    zero_gamma:               float

    # --- From TradingView (as-of join, within 10-min window) --------------
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

    vix:           Optional[float] = None  # None → fall back to expected_move * 16
    vix1d:         Optional[float] = None  # 1-day VIX (intraday-move expansion signal)


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

def _parse_ts(ts: str) -> str:
    """
    Return a SQLite datetime()-compatible string from an ISO-8601 timestamp.
    Strips trailing timezone offset and replaces 'T' with a space.
    """
    ts = re.sub(r'[-+]\d{2}:?\d{2}$', '', ts.rstrip())
    return ts.replace('T', ' ')


def _window_start_dt(scan_ts: str) -> datetime:
    """Return the window-start datetime (scan_ts - FRESHNESS minutes)."""
    parsed = _parse_ts(scan_ts)
    # Drop fractional seconds for robust parsing
    parsed_main = parsed.split('.')[0]
    dt = datetime.strptime(parsed_main, "%Y-%m-%d %H:%M:%S")
    return dt - timedelta(minutes=FRESNESS_WINDOW_MIN)


def _window_clause(scan_ts: str) -> tuple[str, str, str]:
    """
    Return (window_start_sql, upper_bound_sqlite, upper_bound_tv) for the
    10-min freshness window (used by the LOCAL SQLite source).
    """
    parsed_ts = _parse_ts(scan_ts)
    window_start_sql = f"datetime('{parsed_ts}', '-{FRESNESS_WINDOW_MIN} minutes')"
    upper_bound_sqlite = parsed_ts
    upper_bound_tv = scan_ts
    return window_start_sql, upper_bound_sqlite, upper_bound_tv


# ---------------------------------------------------------------------------
# Canonical row normalization (shared by all sources)
# ---------------------------------------------------------------------------

# spx_standardized column indices (verified against live schema)
#  0=id 1=raw_id 2=alert_category 3=alert_type 4=symbol 5=price 6=received_at
#  7=rsi 8=macd 9=macd_signal 10=macd_hist 11=adx 12=vwap 13=bb_upper
# 14=bb_middle 15=bb_lower ... 30=regime 31=vix
TV_IDX = dict(
    price=5, rsi=7, macd=8, macd_signal=9, macd_hist=10,
    adx=11, bb_upper=13, bb_middle=14, bb_lower=15,
    ema9=26, ema21=27, ema50=28, regime=30, vix=31, vix1d=32,
)


def _scan_tuple_to_dict(row: tuple) -> dict:
    """Convert a scanner.db `SELECT *` row tuple into a canonical scan dict."""
    return dict(
        scan_timestamp=row[1], spx_spot=row[2], expected_move=row[3],
        atm_strike=row[4], atm_call_mid=row[5], atm_put_mid=row[6],
        call_strike_003=row[7], call_delta=row[8], call_mid=row[9],
        call_10_long_strike=row[10], call_10_long_mid=row[11], call_10_premium=row[12],
        call_20_long_strike=row[13], call_20_long_mid=row[14], call_20_premium=row[15],
        put_strike_003=row[16], put_delta=row[17], put_mid=row[18],
        put_10_long_strike=row[19], put_10_long_mid=row[20], put_10_premium=row[21],
        put_20_long_strike=row[22], put_20_long_mid=row[23], put_20_premium=row[24],
    )


def _scan_cloud_to_dict(row: dict) -> dict:
    """Convert a Supabase scan_results row (dict) into a canonical scan dict."""
    return dict(
        scan_timestamp=row.get("timestamp_est"),
        spx_spot=row.get("spx_spot"), expected_move=row.get("expected_move"),
        atm_strike=row.get("atm_strike"), atm_call_mid=row.get("atm_call_mid"),
        atm_put_mid=row.get("atm_put_mid"),
        call_strike_003=row.get("call_strike_003"), call_delta=row.get("call_delta"),
        call_mid=row.get("call_mid"),
        call_10_long_strike=row.get("call_10_long_strike"),
        call_10_long_mid=row.get("call_10_long_mid"),
        call_10_premium=row.get("call_10_premium"),
        call_20_long_strike=row.get("call_20_long_strike"),
        call_20_long_mid=row.get("call_20_long_mid"),
        call_20_premium=row.get("call_20_premium"),
        put_strike_003=row.get("put_strike_003"), put_delta=row.get("put_delta"),
        put_mid=row.get("put_mid"),
        put_10_long_strike=row.get("put_10_long_strike"),
        put_10_long_mid=row.get("put_10_long_mid"),
        put_10_premium=row.get("put_10_premium"),
        put_20_long_strike=row.get("put_20_long_strike"),
        put_20_long_mid=row.get("put_20_long_mid"),
        put_20_premium=row.get("put_20_premium"),
    )


def _gex_tuple_to_dict(row: tuple) -> dict:
    """Convert a gex.db `SELECT *` row tuple into a canonical gex dict."""
    return dict(
        gex_by_oi=float(row[3]),
        gex_by_volume=float(row[4]),
        major_negative_by_volume=float(row[6]),
        major_positive_by_volume=float(row[7]),
        zero_gamma=float(row[10]),
    )


def _gex_cloud_to_dict(row: dict) -> dict:
    """Convert a Supabase gex_snapshots row (dict) into a canonical gex dict."""
    return dict(
        gex_by_oi=float(row.get("gex_by_oi") or 0.0),
        gex_by_volume=float(row.get("gex_by_volume") or 0.0),
        major_negative_by_volume=float(row.get("major_negative_by_volume") or 0.0),
        major_positive_by_volume=float(row.get("major_positive_by_volume") or 0.0),
        zero_gamma=float(row.get("zero_gamma") or 0.0),
    )


def _tv_tuple_to_dict(row: tuple) -> dict:
    """Convert a tradingview.db `SELECT *` row tuple into a canonical TV dict."""
    return dict(
        price=float(row[TV_IDX["price"]]),
        rsi=float(row[TV_IDX["rsi"]]),
        macd_hist=float(row[TV_IDX["macd_hist"]]),
        adx=float(row[TV_IDX["adx"]]),
        bb_upper=float(row[TV_IDX["bb_upper"]]),
        bb_middle=float(row[TV_IDX["bb_middle"]]),
        bb_lower=float(row[TV_IDX["bb_lower"]]),
        regime=row[TV_IDX["regime"]],
        vix=row[TV_IDX["vix"]],
        vix1d=row[TV_IDX["vix1d"]] if len(row) > TV_IDX["vix1d"] else None,
    )


def _tv_cloud_to_dict(row: dict) -> dict:
    """Convert a Supabase spx_standardized row (dict) into a canonical TV dict."""
    return dict(
        price=float(row.get("price")),
        rsi=float(row.get("rsi")),
        macd_hist=float(row.get("macd_hist")),
        adx=float(row.get("adx")),
        bb_upper=float(row.get("bb_upper")),
        bb_middle=float(row.get("bb_middle")),
        bb_lower=float(row.get("bb_lower")),
        regime=row.get("regime"),
        vix=row.get("vix"),
        vix1d=row.get("vix1d"),
    )


def _build_tv_fields(tv: dict, prior: Optional[dict]) -> dict:
    """
    Build TV-derived snapshot fields from a canonical TV dict and an optional
    prior TV dict (for expanding/rising comparisons). SHARED across all modes.
    """
    price = tv["price"]
    rsi = tv["rsi"]
    macd_hist = tv["macd_hist"]
    adx = tv["adx"]
    bb_upper = tv["bb_upper"]
    bb_middle = tv["bb_middle"]
    bb_lower = tv["bb_lower"]

    bb_position = (price - bb_lower) / (bb_upper - bb_lower)

    bb_exp = adx_rise = macd_exp = False
    if prior is not None:
        bb_width_prev = prior["bb_upper"] - prior["bb_lower"]
        bb_width_curr = bb_upper - bb_lower
        bb_exp = bb_width_curr > bb_width_prev
        adx_rise = adx > prior["adx"]
        macd_exp = macd_hist > prior["macd_hist"]

    regime = "neutral"
    raw_regime = tv["regime"]
    if raw_regime not in (None, ""):
        regime = str(raw_regime).strip() or "neutral"

    vix_raw = tv["vix"]
    vix = float(vix_raw) if vix_raw is not None else None

    vix1d_raw = tv.get("vix1d")
    vix1d = float(vix1d_raw) if vix1d_raw is not None else None

    return dict(
        rsi=rsi, bb_upper=bb_upper, bb_middle=bb_middle, bb_lower=bb_lower,
        bb_position=bb_position, bb_expanding=bb_exp, adx=adx,
        macd_hist=macd_hist, macd_expanding=macd_exp, adx_rising=adx_rise,
        regime=regime, vix=vix, vix1d=vix1d,
    )


def _assemble(scan: dict, gex: dict, tv_fields: dict) -> CombinedSnapshot:
    """Assemble the final CombinedSnapshot from canonical parts. SHARED."""
    return CombinedSnapshot(
        scan_timestamp=scan["scan_timestamp"], spx_spot=scan["spx_spot"],
        expected_move=scan["expected_move"], atm_strike=scan["atm_strike"],
        atm_call_mid=scan["atm_call_mid"], atm_put_mid=scan["atm_put_mid"],
        call_strike_003=scan["call_strike_003"], call_delta=scan["call_delta"],
        call_mid=scan["call_mid"], call_10_long_strike=scan["call_10_long_strike"],
        call_10_long_mid=scan["call_10_long_mid"], call_10_premium=scan["call_10_premium"],
        call_20_long_strike=scan["call_20_long_strike"], call_20_long_mid=scan["call_20_long_mid"],
        call_20_premium=scan["call_20_premium"], put_strike_003=scan["put_strike_003"],
        put_delta=scan["put_delta"], put_mid=scan["put_mid"],
        put_10_long_strike=scan["put_10_long_strike"], put_10_long_mid=scan["put_10_long_mid"],
        put_10_premium=scan["put_10_premium"], put_20_long_strike=scan["put_20_long_strike"],
        put_20_long_mid=scan["put_20_long_mid"], put_20_premium=scan["put_20_premium"],
        gex_by_oi=gex["gex_by_oi"], gex_by_volume=gex["gex_by_volume"],
        major_positive_by_volume=gex["major_positive_by_volume"],
        major_negative_by_volume=gex["major_negative_by_volume"],
        zero_gamma=gex["zero_gamma"], **tv_fields,
    )


# ---------------------------------------------------------------------------
# Sources — only the row-fetch primitives differ per mode
# ---------------------------------------------------------------------------

class BaseSource:
    """Interface for a data source. Returns canonical dicts (never raw rows)."""

    def latest_scan(self) -> Optional[dict]:
        raise NotImplementedError

    def scan_at(self, scan_ts: str) -> Optional[dict]:
        raise NotImplementedError

    def gex_in_window(self, scan_ts: str) -> Optional[dict]:
        raise NotImplementedError

    def tv_in_window(self, scan_ts: str) -> tuple[Optional[dict], Optional[dict]]:
        """Return (latest_tv, prior_tv) canonical dicts within the window."""
        raise NotImplementedError

    def scan_timestamps_for_date(self, date_str: str) -> list[str]:
        """Backtest: chronological scan timestamps for a single date."""
        raise NotImplementedError


class LocalSource(BaseSource):
    """LOCAL mode — read directly from local SQLite (config-driven paths)."""

    def latest_scan(self) -> Optional[dict]:
        conn = sqlite3.connect(SCANNER_DB)
        conn.execute("PRAGMA journal_mode = WAL;")
        row = conn.execute(
            "SELECT * FROM scan_results ORDER BY timestamp_est DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return _scan_tuple_to_dict(row) if row else None

    def scan_at(self, scan_ts: str) -> Optional[dict]:
        conn = sqlite3.connect(SCANNER_DB)
        conn.execute("PRAGMA journal_mode = WAL;")
        row = conn.execute(
            "SELECT * FROM scan_results WHERE timestamp_est = ? LIMIT 1", (scan_ts,)
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM scan_results ORDER BY timestamp_est DESC LIMIT 1"
            ).fetchone()
        conn.close()
        return _scan_tuple_to_dict(row) if row else None

    def gex_in_window(self, scan_ts: str) -> Optional[dict]:
        conn = sqlite3.connect(GEX_DB)
        conn.execute("PRAGMA journal_mode = WAL;")
        window_start_sql, upper_bound, _ = _window_clause(scan_ts)
        row = conn.execute(f"""
            SELECT * FROM gex_snapshots
            WHERE timestamp >= {window_start_sql} AND timestamp <= ?
            ORDER BY timestamp DESC LIMIT 1
        """, (upper_bound,)).fetchone()
        conn.close()
        return _gex_tuple_to_dict(row) if row else None

    def tv_in_window(self, scan_ts: str) -> tuple[Optional[dict], Optional[dict]]:
        conn = _connect_tv_with_retry()
        window_start_sql, _, upper_bound_tv = _window_clause(scan_ts)
        rows = conn.execute(f"""
            SELECT * FROM spx_standardized
            WHERE received_at >= {window_start_sql} AND received_at <= ?
              AND alert_category = 'indicator_snapshot'
              AND alert_type = 'fundamentals'
              AND price IS NOT NULL
              AND bb_upper IS NOT NULL AND bb_lower IS NOT NULL
              AND bb_upper != bb_lower
            ORDER BY received_at DESC LIMIT 2
        """, (upper_bound_tv,)).fetchall()
        conn.close()
        if not rows:
            return None, None
        latest = _tv_tuple_to_dict(rows[0])
        prior = _tv_tuple_to_dict(rows[1]) if len(rows) >= 2 else _tv_tuple_to_dict(rows[0])
        return latest, prior

    def scan_timestamps_for_date(self, date_str: str) -> list[str]:
        # Use substr() rather than date(): scanner timestamps are stored ISO-8601
        # with a timezone offset ('2026-06-18T16:00:20-0400') that SQLite's
        # date() cannot parse. The leading 10 chars are always 'YYYY-MM-DD'.
        conn = sqlite3.connect(SCANNER_DB)
        conn.execute("PRAGMA journal_mode = WAL;")
        rows = conn.execute("""
            SELECT timestamp_est FROM scan_results
            WHERE substr(timestamp_est, 1, 10) = ?
            ORDER BY timestamp_est ASC
        """, (date_str,)).fetchall()
        conn.close()
        return [r[0] for r in rows]


class CloudSource(BaseSource):
    """CLOUD mode — read from Supabase via the data_sources abstraction."""

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
                # Examples: "2026-06-24T14:10:20Z", "2026-06-24T14:10:20+00:00", "2026-06-24T10:10:20-04:00"
                try:
                    from datetime import timezone as tz_module
                    # Remove fractional seconds for consistent parsing
                    iso_str = raw_str.split('.')[0]
                    # Try full ISO with timezone
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
                    # Fall back to naive parsing (strip timezone)
                    dt = datetime.strptime(_parse_ts(raw_str).split('.')[0], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if win_start <= dt <= upper:
                out.append((dt, r))
        out.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in out]

    def latest_scan(self) -> Optional[dict]:
        from data_sources import get_scan_results_table
        rows = get_scan_results_table(limit=1)
        return _scan_cloud_to_dict(rows[0]) if rows else None

    def scan_at(self, scan_ts: str) -> Optional[dict]:
        from data_sources import get_scan_results_table
        rows = get_scan_results_table(limit=1)  # cloud: drive off latest
        return _scan_cloud_to_dict(rows[0]) if rows else None

    def gex_in_window(self, scan_ts: str) -> Optional[dict]:
        from data_sources import get_gex_snapshots_table
        rows = get_gex_snapshots_table(limit=50)
        windowed = self._in_window(rows, scan_ts, "snapshot_timestamp")
        return _gex_cloud_to_dict(windowed[0]) if windowed else None

    def tv_in_window(self, scan_ts: str) -> tuple[Optional[dict], Optional[dict]]:
        from data_sources import get_tradingview_fundamentals_table
        rows = get_tradingview_fundamentals_table(limit=50)
        windowed = self._in_window(rows, scan_ts, "received_at")
        if not windowed:
            return None, None
        latest = _tv_cloud_to_dict(windowed[0])
        prior = _tv_cloud_to_dict(windowed[1]) if len(windowed) >= 2 else latest
        return latest, prior

    def scan_timestamps_for_date(self, date_str: str) -> list[str]:
        raise RuntimeError("Backtesting is not supported in CLOUD mode (LOCAL only).")


def _get_source() -> BaseSource:
    """Return the source for the configured mode. Backtest uses LocalSource."""
    from data_sources import get_data_source_mode
    mode = get_data_source_mode()
    if mode == "cloud":
        return CloudSource()
    return LocalSource()


# ---------------------------------------------------------------------------
# Public API — assembly is SHARED, only the source differs
# ---------------------------------------------------------------------------

def get_combined_snapshot(scan_timestamp: str, source: Optional[BaseSource] = None) -> CombinedSnapshot:
    """
    Build a CombinedSnapshot for a given scan timestamp via the active source.
    Raises StaleDataError if GEX or TV has no row in the 10-min window.
    """
    src = source or _get_source()

    scan = src.scan_at(scan_timestamp)
    if scan is None:
        raise RuntimeError("No scan data found")

    gex = src.gex_in_window(scan_timestamp)
    if gex is None:
        raise StaleDataError(
            f"No GEX row in {FRESNESS_WINDOW_MIN}-minute window (scan_ts={scan_timestamp})"
        )

    tv_latest, tv_prior = src.tv_in_window(scan_timestamp)
    if tv_latest is None:
        raise StaleDataError(
            f"No TV row in {FRESNESS_WINDOW_MIN}-minute window (scan_ts={scan_timestamp})"
        )

    tv_fields = _build_tv_fields(tv_latest, tv_prior)
    return _assemble(scan, gex, tv_fields)


def get_combined_for_latest_scan(source: Optional[BaseSource] = None) -> CombinedSnapshot:
    """
    Get the latest scan row and as-of-join GEX + TV to its timestamp.
    Used by the live engine tick (LOCAL and CLOUD modes).
    """
    src = source or _get_source()
    latest = src.latest_scan()
    if latest is None:
        raise RuntimeError("No scan data found")
    return get_combined_snapshot(latest["scan_timestamp"], source=src)


def iter_combined_for_date(date_str: str) -> Iterator[CombinedSnapshot]:
    """
    BACKTEST: iterate CombinedSnapshots for every scan timestamp on `date_str`,
    in chronological order. Stale ticks (no GEX/TV in window) are skipped, just
    as the live engine skips them. LOCAL SQLite only.
    """
    src = LocalSource()
    for ts in src.scan_timestamps_for_date(date_str):
        try:
            yield get_combined_snapshot(ts, source=src)
        except StaleDataError:
            continue


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        c = get_combined_for_latest_scan()
        print(
            f"scan={c.scan_timestamp}  spx={c.spx_spot}  EM={c.expected_move}  "
            f"gex_oi={c.gex_by_oi:.2f}  tv_rsi={c.rsi:.2f}  regime={c.regime}  vix={c.vix}"
        )
    except StaleDataError as e:
        print(f"STALE: {e}")
