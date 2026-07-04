"""
Tests for src/vix_buckets.py — config-driven VIX buckets.

Pins the boundary semantics of the original hardcoded ladders
(tick_processor if/elif, risk_manager VIX_BUCKET_BOUNDARIES) so the
config-driven refactor cannot silently drift:
  - buckets half-open [lo, hi), top bucket closed [lo, hi]
  - VIX outside all buckets → None (no-trade zone)
"""
import pytest

import vix_buckets
from vix_buckets import parse_buckets, classify


# Mirrors the production config.yaml entry.vix_buckets structure exactly.
CURRENT_FORMAT = {
    "13-16": {"min_premium": 0.20, "rsi_upper_threshold": 50.0, "rsi_lower_threshold": 49.0,
              "distance": {"from_spot": 3, "from_gex_level": 1}},
    "16-20": {"min_premium": 0.25, "rsi_upper_threshold": 55.0, "rsi_lower_threshold": 45.0,
              "distance": {"from_spot": 3, "from_gex_level": 1.5}},
    "20-25": {"min_premium": 0.30, "rsi_upper_threshold": 55.0, "rsi_lower_threshold": 45.0,
              "distance": {"from_spot": 3, "from_gex_level": 1.5}},
    "25-30": {"min_premium": 0.35, "rsi_upper_threshold": 55.0, "rsi_lower_threshold": 45.0,
              "distance": {"from_spot": 3.0, "from_gex_level": 1.5}},
}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_current_config_format():
    buckets = parse_buckets(CURRENT_FORMAT)
    assert [b.name for b in buckets] == ["13-16", "16-20", "20-25", "25-30"]
    assert buckets[0].lo == 13.0 and buckets[0].hi == 16.0
    assert [b.inclusive_hi for b in buckets] == [False, False, False, True]
    assert buckets[1].params["min_premium"] == 0.25


def test_parse_sorts_by_lower_bound():
    buckets = parse_buckets({"25-30": {}, "13-16": {}, "20-25": {}, "16-20": {}})
    assert [b.name for b in buckets] == ["13-16", "16-20", "20-25", "25-30"]


def test_parse_empty_and_missing():
    assert parse_buckets({}) == []
    assert parse_buckets(None) == []


def test_parse_float_bounds():
    buckets = parse_buckets({"13.5-16": {}, "16-20.5": {}})
    assert buckets[0].lo == 13.5
    assert buckets[1].hi == 20.5


def test_parse_none_params_tolerated():
    # YAML `13-16:` with no mapping loads as None
    buckets = parse_buckets({"13-16": None})
    assert buckets[0].params == {}


# ---------------------------------------------------------------------------
# Validation — fail loudly on malformed config
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad,match", [
    ({"low_vol": {}},               "range"),
    ({"13": {}},                    "range"),
    ({"a-b": {}},                   "numeric"),
    ({"20-16": {}},                 "lower bound"),
    ({"16-16": {}},                 "lower bound"),
    ({"13-16": {}, "17-20": {}},    "gap"),
    ({"13-17": {}, "16-20": {}},    "overlap"),
])
def test_parse_rejects_malformed(bad, match):
    with pytest.raises(ValueError, match=match):
        parse_buckets(bad)


# ---------------------------------------------------------------------------
# Boundary pins — must match the original hardcoded ladder exactly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("vix,expected", [
    (0.0,   None),
    (12.99, None),
    (13.0,  "13-16"),
    (15.99, "13-16"),
    (16.0,  "16-20"),
    (19.99, "16-20"),
    (20.0,  "20-25"),
    (24.99, "20-25"),
    (25.0,  "25-30"),
    (30.0,  "25-30"),   # top bucket closed
    (30.01, None),
])
def test_classify_boundary_pins(vix, expected):
    bucket = classify(vix, parse_buckets(CURRENT_FORMAT))
    assert (bucket.name if bucket else None) == expected


def test_adding_bucket_is_config_only():
    """Adding '30-35' shifts the closed top: 30.0 now belongs to the new bucket."""
    extended = dict(CURRENT_FORMAT)
    extended["30-35"] = {"rsi_upper_threshold": 60.0, "rsi_lower_threshold": 40.0}
    buckets = parse_buckets(extended)
    assert classify(29.99, buckets).name == "25-30"
    assert classify(30.0, buckets).name == "30-35"
    assert classify(35.0, buckets).name == "30-35"
    assert classify(35.01, buckets) is None


# ---------------------------------------------------------------------------
# Live-config reads (call time, patchable)
# ---------------------------------------------------------------------------

def test_get_buckets_reads_config_at_call_time(monkeypatch):
    monkeypatch.setattr(
        vix_buckets, "CONFIG",
        {"entry": {"vix_buckets": {"10-40": {"min_premium": 0.1}}}},
    )
    buckets = vix_buckets.get_buckets()
    assert len(buckets) == 1
    assert buckets[0].name == "10-40"
    assert classify(30.0).name == "10-40"


def test_get_buckets_tolerates_stripped_config(monkeypatch):
    # Test fixtures patch in configs with vix_buckets: {} — must not raise.
    monkeypatch.setattr(vix_buckets, "CONFIG", {"entry": {"vix_buckets": {}}})
    assert vix_buckets.get_buckets() == []
    monkeypatch.setattr(vix_buckets, "CONFIG", {})
    assert vix_buckets.get_buckets() == []


def test_real_config_parses():
    """The committed config.yaml must always yield a valid, non-empty ladder."""
    assert vix_buckets.get_buckets()


# ---------------------------------------------------------------------------
# Call-site integration — behavior preserved
# ---------------------------------------------------------------------------

def test_risk_manager_bucket_lookup_unchanged(monkeypatch):
    import risk_manager
    monkeypatch.setattr(
        vix_buckets, "CONFIG", {"entry": {"vix_buckets": CURRENT_FORMAT}}
    )
    assert risk_manager._get_vix_bucket(12.0) is None
    assert risk_manager._get_vix_bucket(18.0) == "16-20"
    assert risk_manager._get_vix_bucket(30.0) == "25-30"
    assert risk_manager._get_vix_bucket(31.0) is None


def test_rsi_gates_unchanged(monkeypatch):
    from tick_processor import _get_rsi_gates
    monkeypatch.setattr(
        vix_buckets, "CONFIG", {"entry": {"vix_buckets": CURRENT_FORMAT}}
    )
    assert _get_rsi_gates(None) == (70.0, 30.0)          # no VIX → wide defaults
    assert _get_rsi_gates(14.0) == (50.0, 49.0)          # in bucket
    assert _get_rsi_gates(18.0) == (55.0, 45.0)
    # Out-of-range → top bucket's gates (most restrictive), per original ladder
    assert _get_rsi_gates(12.0) == (55.0, 45.0)
    assert _get_rsi_gates(40.0) == (55.0, 45.0)


def test_rsi_gates_no_buckets_configured(monkeypatch):
    from tick_processor import _get_rsi_gates
    monkeypatch.setattr(vix_buckets, "CONFIG", {"entry": {"vix_buckets": {}}})
    assert _get_rsi_gates(18.0) == (70.0, 30.0)
