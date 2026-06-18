"""
scanner_reader.py — read latest rows from data/scanner.db.
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from dateutil import tz

# Import config for data source paths
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import CONFIG

# Read paths from config
SCANNER_DB_PATH = CONFIG.get("data_sources", {}).get("scanner_db", "../premium_extractor/data/scanner.db")
SCANNER_DB = Path(SCANNER_DB_PATH).resolve() if Path(SCANNER_DB_PATH).exists() or Path(SCANNER_DB_PATH).is_absolute() else Path(__file__).parent.parent.parent / SCANNER_DB_PATH


@dataclass
class ScanRow:
    id:              int
    timestamp_est:   str
    spx_spot:        float
    expected_move:   float

    atm_strike:      float
    atm_call_mid:    float
    atm_put_mid:     float

    call_strike_003: float   # +0.03-delta call strike
    call_delta:      float
    call_mid:        float
    call_10_long_strike:  float
    call_10_long_mid:    float
    call_10_premium:     float
    call_20_long_strike: float
    call_20_long_mid:    float
    call_20_premium:     float

    put_strike_003:  float   # -0.03-delta put strike
    put_delta:       float
    put_mid:         float
    put_10_long_strike:   float
    put_10_long_mid:      float
    put_10_premium:       float
    put_20_long_strike:   float
    put_20_long_mid:      float
    put_20_premium:       float

    @property
    def atm_call_spread_credit(self) -> float:
        """Net credit for ATM call spread (short ATM call, long call_10_long)."""
        return self.atm_call_mid - self.call_10_long_mid

    @property
    def atm_put_spread_credit(self) -> float:
        """Net credit for ATM put spread (short ATM put, long put_10_long)."""
        return self.atm_put_mid - self.put_10_long_mid

    @property
    def call_spread_width(self) -> float:
        """Width of +0.03-delta call spread."""
        return abs(self.call_strike_003 - self.call_10_long_strike)

    @property
    def put_spread_width(self) -> float:
        """Width of -0.03-delta put spread."""
        return abs(self.put_10_long_strike - self.put_strike_003)


def _row_to_scan(row: tuple) -> ScanRow:
    return ScanRow(
        id=row[0], timestamp_est=row[1], spx_spot=row[2], expected_move=row[3],
        atm_strike=row[4], atm_call_mid=row[5], atm_put_mid=row[6],
        call_strike_003=row[7], call_delta=row[8], call_mid=row[9],
        call_10_long_strike=row[10], call_10_long_mid=row[11], call_10_premium=row[12],
        call_20_long_strike=row[13], call_20_long_mid=row[14], call_20_premium=row[15],
        put_strike_003=row[16], put_delta=row[17], put_mid=row[18],
        put_10_long_strike=row[19], put_10_long_mid=row[20], put_10_premium=row[21],
        put_20_long_strike=row[22], put_20_long_mid=row[23], put_20_premium=row[24],
    )


def get_latest_scan(db_path: Path = SCANNER_DB) -> Optional[ScanRow]:
    """Return the most recent scan row."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    row = conn.execute(
        "SELECT * FROM scan_results ORDER BY timestamp_est DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return _row_to_scan(row)


def _cutoff_time(minutes: int) -> str:
    """
    Return an ET cutoff timestamp for the last N minutes.
    Properly handles EST/EDT with automatic DST adjustment.
    """
    eastern = tz.gettz('America/New_York')
    et_now = datetime.now(eastern)
    cutoff = et_now - timedelta(minutes=minutes)
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


def get_scan_history(
    minutes: int = 60,
    db_path: Path = SCANNER_DB,
) -> list[ScanRow]:
    """Return scan rows from the last N minutes."""
    cutoff = _cutoff_time(minutes)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode = WAL;")
    rows = conn.execute(
        "SELECT * FROM scan_results WHERE timestamp_est >= ? ORDER BY timestamp_est ASC",
        (cutoff,),
    ).fetchall()
    conn.close()
    return [_row_to_scan(r) for r in rows]


if __name__ == "__main__":
    latest = get_latest_scan()
    if latest:
        print(f"SPX: {latest.spx_spot}  EM: ±{latest.expected_move}  ATM: {latest.atm_strike}")
    else:
        print("No scan data found.")