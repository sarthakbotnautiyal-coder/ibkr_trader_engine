"""
tradingview_reader.py — read pre-computed indicators from tradingview.db.

Uses tradingview.db (from tradingView_signal_generator) for live technical data
instead of computing BB/ADX/MACD from scan history.
Scanner.db still provides: call_strike_003, put_strike_003, atm_strike,
atm_call_mid, atm_put_mid, expected_move (EM).

Regime: primarily read from spx_standardized.regime column (pre-computed by
tradingView_signal_generator). Falls back to local classify_regime() if the
column is NULL or absent — this is logged as a warning so it's visible in the
engine log during transition.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, TYPE_CHECKING
import logging

import sqlite3
from pathlib import Path
import sys

if TYPE_CHECKING:
    from gex_reader import GexSnapshot

# Import config for data source paths
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import CONFIG

# Read paths from config
TV_DB_PATH = CONFIG.get("data_sources", {}).get("tradingview_db", "../tradingView_signal_generator/data/tradingview.db")
TV_DB = Path(TV_DB_PATH).resolve() if Path(TV_DB_PATH).exists() or Path(TV_DB_PATH).is_absolute() else Path(__file__).parent.parent.parent / TV_DB_PATH

_LOG = logging.getLogger("tradingview_reader")


# ---------------------------------------------------------------------------
# Lightweight SPX spot fallback (for the scanner when the IBKR index feed freezes)
# ---------------------------------------------------------------------------

def get_tv_spot() -> tuple[Optional[float], Optional[float]]:
    """Return ``(spx_price, age_seconds)`` from the most recent TradingView
    ``fundamentals`` row, or ``(None, None)`` if unavailable.

    Lightweight, read-only, and never raises — intended as a fallback SPX spot
    source for the scanner when the IBKR index feed freezes. ``age_seconds`` is
    how old that row is (TV writes ~1 row/min), so callers can reject it if the
    upstream tradingView_signal_generator process has itself stalled.
    """
    try:
        conn = sqlite3.connect(f"file:{TV_DB}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(
                """
                SELECT price, received_at
                FROM spx_standardized
                WHERE alert_type = 'fundamentals' AND price IS NOT NULL
                ORDER BY received_at DESC
                LIMIT 1
                """
            ).fetchone()
        finally:
            conn.close()
    except Exception as e:
        _LOG.warning("get_tv_spot: DB read failed: %s", e)
        return None, None

    if not row or row[0] is None or row[1] is None:
        return None, None

    price, received_at = row
    try:
        ts = datetime.fromisoformat(received_at)
    except (ValueError, TypeError):
        return float(price), None
    now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
    age = (now - ts).total_seconds()
    return float(price), age


# ---------------------------------------------------------------------------
# TradingView snapshot
# ---------------------------------------------------------------------------

@dataclass
class TradingViewSnapshot:
    # Price
    price: float

    # RSI
    rsi: float

    # MACD
    macd: float
    macd_signal: float
    macd_hist: float

    # ADX
    adx: float

    # Bollinger Bands
    bb_upper: float
    bb_middle: float
    bb_lower: float

    # EMAs
    ema9: float
    ema21: float
    ema50: float

    # Derived / flags
    bb_position: float       # (price - bb_lower) / (bb_upper - bb_lower)
    bb_expanding: bool      # BB bandwidth grew vs prior row
    adx_rising: bool        # ADX > prior ADX
    macd_expanding: bool    # MACD histogram > prior histogram

    # Regime (pre-computed by tradingView_signal_generator, or fallback)
    regime: str = "unknown"  # regime name or "unknown"

    # Market context (passed in from scanner)
    expected_move: float = 0.0


def get_latest_fundamentals(gex_expected_move: float = 0.0) -> TradingViewSnapshot:
    """
    Get latest 'fundamentals' alert row from tradingview.db.
    Computes bb_position and expanding/rising flags by comparing with the prior row.

    Regime is read from spx_standardized.regime if available and non-NULL.
    Falls back to local classify_regime() with a warning if regime is absent
    or NULL — this keeps the engine functional during the transition window
    while tradingView_signal_generator starts writing the regime column.

    Args:
        gex_expected_move: EM value from scanner.db to carry through.
    """
    conn = sqlite3.connect(str(TV_DB))
    conn.execute("PRAGMA journal_mode = WAL;")

    # Check if regime column exists
    all_cols = {r[1] for r in conn.execute("PRAGMA table_info(spx_standardized)").fetchall()}
    has_regime_col = "regime" in all_cols

    if has_regime_col:
        rows = conn.execute("""
            SELECT
                price, rsi,
                macd, macd_signal, macd_hist,
                adx,
                bb_upper, bb_middle, bb_lower,
                ema9, ema21, ema50,
                regime
            FROM spx_standardized
            WHERE alert_type = 'fundamentals'
              AND price      IS NOT NULL
              AND bb_upper   IS NOT NULL
              AND bb_lower   IS NOT NULL
              AND bb_upper   != bb_lower
            ORDER BY received_at ASC
        """).fetchall()
    else:
        rows = conn.execute("""
            SELECT
                price, rsi,
                macd, macd_signal, macd_hist,
                adx,
                bb_upper, bb_middle, bb_lower,
                ema9, ema21, ema50
            FROM spx_standardized
            WHERE alert_type = 'fundamentals'
              AND price      IS NOT NULL
              AND bb_upper   IS NOT NULL
              AND bb_lower   IS NOT NULL
              AND bb_upper   != bb_lower
            ORDER BY received_at ASC
        """).fetchall()

    conn.close()

    if not rows:
        raise RuntimeError(
            "No valid fundamentals data in tradingview.db "
            "(last 30 min, or rows with NULL price/BB)"
        )

    latest = rows[-1]

    price     = latest[0]
    rsi       = latest[1]
    macd      = latest[2]
    macd_sig  = latest[3]
    macd_hist = latest[4]
    adx       = latest[5]
    bb_upper  = latest[6]
    bb_middle = latest[7]
    bb_lower  = latest[8]
    ema9      = latest[9]
    ema21     = latest[10]
    ema50     = latest[11]

    # Regime: read from DB or compute via fallback
    precomputed_regime: Optional[str] = None
    if has_regime_col and len(latest) > 12:
        precomputed_regime = latest[12]

    if precomputed_regime and precomputed_regime.strip():
        regime = precomputed_regime.strip()
    else:
        # Fallback: compute regime from indicators
        _LOG.warning(
            "spx_standardized.regime is absent or NULL — "
            "falling back to local classify_regime() computation. "
            "This is expected during the transition window. "
            "Once tradingView_signal_generator starts writing the regime column, "
            "this warning will stop."
        )
        regime = _classify_regime_fallback(
            rsi=rsi, adx=adx, bb_pos=(price - bb_lower) / (bb_upper - bb_lower),
            bb_exp=False, ema9=ema9, ema21=ema21,
        )

    # BB position: where is price within the BB band?
    bb_position = (price - bb_lower) / (bb_upper - bb_lower)

    # Expanding/rising flags — compare to prior fundamentals row
    bb_exp = adx_rise = macd_exp = False
    if len(rows) >= 2:
        prev = rows[-2]
        bb_width_prev = prev[6] - prev[8]
        bb_width_curr = bb_upper - bb_lower
        bb_exp = bb_width_curr > bb_width_prev
        adx_rise = adx > prev[5]
        macd_exp = macd_hist > prev[4]

    return TradingViewSnapshot(
        price=price,
        rsi=rsi,
        macd=macd,
        macd_signal=macd_sig,
        macd_hist=macd_hist,
        adx=adx,
        bb_upper=bb_upper,
        bb_middle=bb_middle,
        bb_lower=bb_lower,
        ema9=ema9,
        ema21=ema21,
        ema50=ema50,
        bb_position=bb_position,
        bb_expanding=bb_exp,
        adx_rising=adx_rise,
        macd_expanding=macd_exp,
        regime=regime,
        expected_move=gex_expected_move,
    )


# ---------------------------------------------------------------------------
# Regime classification — fallback only (pre-computed regime preferred)
# ---------------------------------------------------------------------------

def _classify_regime_fallback(
    rsi: float,
    adx: float,
    bb_pos: float,
    bb_exp: bool,
    ema9: float,
    ema21: float,
) -> str:
    """
    Compute regime from raw indicators. Used as fallback when the DB column
    is absent or NULL. Do not call this when tv.regime is available.

    Logic:
      - strong_trend:  ADX > 50 AND (RSI > 70 or RSI < 30)
      - ranging:       ADX < 25
      - volatile:      BB position > 1.1 (price outside upper band)
      - momentum_up:   ADX > 25 AND 60 < RSI < 70 AND ema9 > ema21
      - momentum_down: ADX > 25 AND 30 < RSI < 40 AND ema9 < ema21
      - reversal:      (RSI > 80 or RSI < 20) AND BB expanding
      - neutral:       default
    """
    if adx > 50 and (rsi > 70 or rsi < 30):
        return "strong_trend"
    elif adx < 25:
        return "ranging"
    elif bb_pos > 1.1:
        return "volatile"
    elif adx > 25 and 60 < rsi < 70 and ema9 > ema21:
        return "momentum_up"
    elif adx > 25 and 30 < rsi < 40 and ema9 < ema21:
        return "momentum_down"
    elif (rsi > 80 or rsi < 20) and bb_exp:
        return "reversal"
    else:
        return "neutral"


def classify_regime(
    tv: TradingViewSnapshot,
    gex: Optional["GexSnapshot"],
    em: float,
    spx: float,
) -> str:
    """
    Return tv.regime (pre-computed).  Provided for backwards compatibility
    and for callers that pass (tv, gex, em, spx) — the gex/spx args are
    accepted but ignored since regime is now pre-computed.
    """
    if tv.regime and tv.regime != "unknown":
        return tv.regime
    # Fallback only if pre-computed regime is missing
    return _classify_regime_fallback(
        rsi=tv.rsi, adx=tv.adx,
        bb_pos=tv.bb_position, bb_exp=tv.bb_expanding,
        ema9=tv.ema9, ema21=tv.ema21,
    )


def get_dealer_regime(gex: Optional["GexSnapshot"]) -> str:
    """Returns 'dealer_long' if gex_by_oi >= 0, else 'dealer_short'."""
    if gex is None:
        return "unknown"
    return "dealer_long" if gex.gex_by_oi >= 0 else "dealer_short"


if __name__ == "__main__":
    snap = get_latest_fundamentals(gex_expected_move=15.0)
    print(f"price={snap.price}  rsi={snap.rsi:.2f}  adx={snap.adx:.1f}")
    print(f"BB: {snap.bb_lower:.2f}–{snap.bb_middle:.2f}–{snap.bb_upper:.2f}  "
          f"pos={snap.bb_position:.4f}  exp={snap.bb_expanding}")
    print(f"MACD hist={snap.macd_hist:.4f}  exp={snap.macd_expanding}")
    print(f"ADX rising={snap.adx_rising}  ema9={snap.ema9:.2f}  ema21={snap.ema21:.2f}")
    print(f"Regime (pre-computed): {snap.regime}")
    print(f"Regime (via classify_regime): {classify_regime(snap, None, 15.0, snap.price)}")