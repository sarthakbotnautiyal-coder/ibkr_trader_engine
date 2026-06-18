"""
gex_reader.py — read from data/gex.db + regime judgment.
"""
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Import config for data source paths
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import CONFIG

# Read paths from config
GEX_DB_PATH = CONFIG.get("data_sources", {}).get("gex_db", "../gex_extractor/data/gex.db")
TV_DB_PATH = CONFIG.get("data_sources", {}).get("tradingview_db", "../tradingView_signal_generator/data/tradingview.db")

# Resolve to absolute paths
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


def get_latest_gex(db_path: Path = GEX_DB) -> Optional[GexSnapshot]:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    row = conn.execute(
        "SELECT * FROM gex_snapshots ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return _row_to_gex(row)


def get_latest_regime() -> str:
    """
    Read the most recent non-NULL regime from tradingview.db spx_standardized.

    Only 'indicator_snapshot'/'fundamentals' rows have regime values.
    Rows from other alert_category/alert_type pairs (e.g., market_comparison/
    OTM_value) have NULL regime and must be excluded.

    Returns regime string (e.g., 'neutral', 'momentum_up') or 'unknown' if
    no valid row exists.
    """
    conn = sqlite3.connect(str(TV_DB))
    conn.execute("PRAGMA journal_mode = WAL;")
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
    conn.close()
    if row and row[0]:
        return row[0].strip()
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