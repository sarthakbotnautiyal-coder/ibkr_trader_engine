"""
gex_reader.py — read GEX data from LOCAL or CLOUD sources.

Uses data_sources abstraction layer to support both LOCAL (SQLite) and CLOUD (Supabase) modes.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import logging

from config import CONFIG
from data_sources import get_gex_snapshots_table, get_tradingview_fundamentals_table, is_local_mode
from db_utils import connect_ro_with_retry

_LOG = logging.getLogger(__name__)

# For LOCAL mode only
GEX_DB_PATH = CONFIG.get("data_sources", {}).get("gex_db", "../gex_extractor/data/gex.db")
TV_DB_PATH = CONFIG.get("data_sources", {}).get("tradingview_db", "../tradingView_signal_generator/data/tradingview.db")

GEX_DB = Path(GEX_DB_PATH).resolve() if Path(GEX_DB_PATH).exists() or Path(GEX_DB_PATH).is_absolute() else Path(__file__).parent.parent.parent / GEX_DB_PATH
TV_DB = Path(TV_DB_PATH).resolve() if Path(TV_DB_PATH).exists() or Path(TV_DB_PATH).is_absolute() else Path(__file__).parent.parent.parent / TV_DB_PATH


@dataclass
class GexSnapshot:
    id:                       int
    timestamp:                str
    received_at:              str
    gex_by_oi:                float
    gex_by_volume:            float
    spot:                     float
    major_negative_by_volume: float
    major_positive_by_volume: float
    major_negative_by_oi:     float
    major_positive_by_oi:    float
    zero_gamma:               float
    raw_message:              str

    @property
    def regime(self) -> str:
        """Gamma regime: 'positive' | 'negative' | 'neutral'."""
        if self.gex_by_oi > 20:
            return "positive"
        elif self.gex_by_oi < -20:
            return "negative"
        return "neutral"

    @property
    def gamma_flip_zone(self) -> str:
        """Zone in which a gamma flip is likely."""
        return f"{self.zero_gamma:.0f}"

    @property
    def in_positive_gamma(self) -> bool:
        return self.regime == "positive"


def _row_to_gex(row: tuple) -> GexSnapshot:
    return GexSnapshot(
        id=row[0], timestamp=row[1], received_at=row[2],
        gex_by_oi=row[3], gex_by_volume=row[4], spot=row[5],
        major_negative_by_volume=row[6], major_positive_by_volume=row[7],
        major_negative_by_oi=row[8], major_positive_by_oi=row[9],
        zero_gamma=row[10], raw_message=row[11],
    )


def get_latest_gex(db_path: Path = None) -> Optional[GexSnapshot]:
    """Get latest GEX snapshot (supports LOCAL and CLOUD modes)."""
    try:
        if is_local_mode():
            # LOCAL mode: Read directly from SQLite
            if db_path is None:
                db_path = GEX_DB
            conn = connect_ro_with_retry(db_path, "gex.db")
            try:
                row = conn.execute(
                    "SELECT * FROM gex_snapshots ORDER BY timestamp DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
            if row is None:
                return None
            return _row_to_gex(row)
        else:
            # CLOUD mode: Use abstraction layer
            rows = get_gex_snapshots_table(limit=1)
            if not rows:
                return None
            row = rows[0]
            # Convert dict to tuple for compatibility
            return GexSnapshot(
                id=row.get("id"),
                timestamp=row.get("snapshot_timestamp"),
                received_at=row.get("received_at"),
                gex_by_oi=row.get("gex_by_oi"),
                gex_by_volume=row.get("gex_by_volume"),
                spot=row.get("spot"),
                major_negative_by_volume=row.get("major_negative_by_volume"),
                major_positive_by_volume=row.get("major_positive_by_volume"),
                major_negative_by_oi=row.get("major_negative_by_oi"),
                major_positive_by_oi=row.get("major_positive_by_oi"),
                zero_gamma=row.get("zero_gamma"),
                raw_message=row.get("raw_message"),
            )
    except Exception as e:
        _LOG.warning(f"Error reading GEX data: {e}")
        return None


def get_latest_regime() -> str:
    """
    Read the most recent regime from TradingView data (supports LOCAL and CLOUD modes).

    Returns regime string (e.g., 'neutral', 'momentum_up') or 'unknown' if
    no valid row exists.
    """
    try:
        if is_local_mode():
            # LOCAL mode: Direct SQLite query
            conn = connect_ro_with_retry(TV_DB, "tradingview.db")
            try:
                row = conn.execute("""
                    SELECT regime
                    FROM spx_standardized
                    WHERE alert_category  = 'indicator_snapshot'
                      AND alert_type      = 'fundamentals'
                      AND regime          IS NOT NULL
                      AND regime          != ''
                    ORDER BY id DESC
                    LIMIT 1
                """).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                return row[0].strip()
        else:
            # CLOUD mode: Use abstraction layer
            rows = get_tradingview_fundamentals_table(limit=1)
            if rows:
                regime = rows[0].get("regime")
                if regime:
                    return regime.strip()
    except Exception as e:
        _LOG.warning(f"Error reading regime: {e}")

    return "unknown"


if __name__ == "__main__":
    gex = get_latest_gex()
    if gex:
        print(f"GEX(OI)={gex.gex_by_oi:.2f}  GEX(Vol)={gex.gex_by_volume:.2f}  "
              f"Regime={gex.regime}  ZeroGamma={gex.zero_gamma}")
    else:
        print("No GEX data found.")
    regime = get_latest_regime()
    print(f"TV regime: {regime}")