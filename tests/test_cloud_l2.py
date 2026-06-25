"""
Cloud-mode verification for the momentum-aware L2 exit.

Simulates Supabase rows (trading.scan_results / gex_snapshots /
trading_view_indicators) flowing through combined_reader.CloudSource and
confirms:
  1. vix1d / adx / rsi / major-GEX levels are carried into CombinedSnapshot
     via the CLOUD as-of join (same join logic as LOCAL).
  2. The new L2 fires on the cloud-built snapshot exactly as it does locally.

No live Supabase needed — the data_sources fetch primitives are patched.
"""
from unittest.mock import Mock, patch

from src.risk_manager import evaluate_exit
from src.position_store import PositionSide


SCAN_TS = "2026-06-24 13:16:00"

_SCAN_ROW = {
    "timestamp_est": SCAN_TS,
    "spx_spot": 7368.0, "expected_move": 22.0,
    "atm_strike": 7370.0, "atm_call_mid": 9.0, "atm_put_mid": 11.0,
    "call_strike_003": 7430.0, "call_delta": 0.03, "call_mid": 0.3,
    "call_10_long_strike": 7440.0, "call_10_long_mid": 0.2, "call_10_premium": 0.1,
    "call_20_long_strike": 7450.0, "call_20_long_mid": 0.15, "call_20_premium": 0.15,
    "put_strike_003": 7300.0, "put_delta": -0.03, "put_mid": 0.4,
    "put_10_long_strike": 7290.0, "put_10_long_mid": 0.25, "put_10_premium": 0.15,
    "put_20_long_strike": 7280.0, "put_20_long_mid": 0.2, "put_20_premium": 0.2,
}

_GEX_ROW = {
    "snapshot_timestamp": "2026-06-24 13:15:00",
    "gex_by_oi": -27.0, "gex_by_volume": -1e6,
    "major_negative_by_volume": 7340.0, "major_positive_by_volume": 7400.0,
    "major_negative_by_oi": 7345.0, "major_positive_by_oi": 7405.0,
    "zero_gamma": 7360.0,
}

_TV_ROWS = [
    {  # latest (13:15:30) — trend + momentum against a short put
        "received_at": "2026-06-24 13:15:30",
        "price": 7368.0, "rsi": 33.0, "macd_hist": -4.3, "adx": 20.1,
        "bb_upper": 7380.0, "bb_middle": 7360.0, "bb_lower": 7340.0,
        "regime": "neutral", "vix": 19.3, "vix1d": 16.0,
    },
    {  # prior (13:14:30) — for expanding/rising flags
        "received_at": "2026-06-24 13:14:30",
        "price": 7372.0, "rsi": 35.0, "macd_hist": -4.0, "adx": 19.0,
        "bb_upper": 7382.0, "bb_middle": 7362.0, "bb_lower": 7344.0,
        "regime": "neutral", "vix": 19.1, "vix1d": 15.5,
    },
]


def _cloud_combined():
    """Build a CombinedSnapshot via the CLOUD source with patched fetches."""
    with patch("data_sources.get_data_source_mode", return_value="cloud"), \
         patch("data_sources.get_scan_results_table", return_value=[_SCAN_ROW]), \
         patch("data_sources.get_gex_snapshots_table", return_value=[_GEX_ROW]), \
         patch("data_sources.get_tradingview_fundamentals_table", return_value=_TV_ROWS):
        from combined_reader import get_combined_for_latest_scan
        return get_combined_for_latest_scan()


def test_cloud_join_carries_vix1d_and_indicators():
    c = _cloud_combined()
    assert c.scan_timestamp == SCAN_TS
    assert c.spx_spot == 7368.0
    assert c.vix1d == 16.0          # NEW: carried through cloud as-of join
    assert c.vix == 19.3           # real VIX (not EM*16)
    assert c.adx == 20.1
    assert c.rsi == 33.0
    assert c.major_negative_by_volume == 7340.0


def test_cloud_build_market_snapshot_uses_seam():
    """In CLOUD mode, build_market_snapshot() (no combined passed) fetches via the
    mode-aware seam and carries real VIX + VIX1D — not the EM*16 proxy."""
    with patch("data_sources.get_data_source_mode", return_value="cloud"), \
         patch("data_sources.get_scan_results_table", return_value=[_SCAN_ROW]), \
         patch("data_sources.get_gex_snapshots_table", return_value=[_GEX_ROW]), \
         patch("data_sources.get_tradingview_fundamentals_table", return_value=_TV_ROWS):
        from position_store import build_market_snapshot
        snap = build_market_snapshot(em=22.0, gex_val=-27.0)

    assert snap.spx_spot == 7368.0
    assert snap.vix == 19.3          # real VIX, not 22*16=352
    assert snap.vix1d == 16.0
    assert snap.adx == 20.1
    assert snap.rsi == 33.0


def test_cloud_l2_fires_on_trend_plus_momentum():
    c = _cloud_combined()

    pos = Mock()
    pos.side = PositionSide.PUT
    pos.short_strike = 7335.0
    pos.entry_em = 19.6
    pos.credit = 0.30
    pos.num_contracts = 1
    pos.entry_spx_spot = 7400.0
    pos.entry_adx = 10.4
    pos.entry_rsi = 43.5
    pos.entry_vix1d = 13.2

    r = evaluate_exit(pos, c)
    assert r.should_exit is True
    assert r.exit_layer == 2
    assert r.exit_conditions_met >= 2
    assert "trend" in r.reason and "momentum" in r.reason
