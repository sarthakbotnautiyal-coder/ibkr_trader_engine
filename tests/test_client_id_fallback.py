"""Tests for the IBKR client-id fallback ladder (_compute_client_id).

Background: the engine connects with a fixed engine client id. When a stale
session still holds that id, TWS rejects the new connection with Error 326
("client id already in use"), which previously wedged the background connect
loop in an infinite retry. _compute_client_id() makes the loop cycle to
alternate ids so a dead connection can't lock the engine out forever.
"""
from __future__ import annotations

import sys
from pathlib import Path

_root = Path(__file__).parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))

from src.blocking_ib_client import _CLIENT_ID_FALLBACK_OFFSETS, _compute_client_id

BASE = 31  # engine_client_id in config


def test_first_two_attempts_use_configured_id():
    """Attempts 0 and 1 must use the configured base id (give it a fair shot)."""
    assert _compute_client_id(BASE, 0) == BASE
    assert _compute_client_id(BASE, 1) == BASE


def test_falls_back_after_two_failures():
    """From the 3rd failure on, cycle to the first alternate (base + 100)."""
    assert _compute_client_id(BASE, 2) == BASE + 100
    assert _compute_client_id(BASE, 3) == BASE + 200
    assert _compute_client_id(BASE, 4) == BASE + 300


def test_offsets_avoid_sibling_collisions():
    """Alternates are spaced ≥100 apart and never reuse the base id."""
    seen = {_compute_client_id(BASE, f) for f in range(2, 50)}
    assert BASE not in seen
    # Every alternate is at least 100 away from common sibling ids (15, 16, 31).
    for cid in seen:
        assert cid >= BASE + 100


def test_fallback_ladder_wraps_and_stays_bounded():
    """Cycling never exceeds base + max offset (range stays bounded)."""
    max_offset = max(_CLIENT_ID_FALLBACK_OFFSETS)
    for f in range(2, 200):
        cid = _compute_client_id(BASE, f)
        assert BASE + 100 <= cid <= BASE + max_offset


def test_ladder_is_deterministic_and_cycles_all_alternates():
    """Over a full cycle, every non-zero offset is visited exactly once."""
    n_alternates = len(_CLIENT_ID_FALLBACK_OFFSETS) - 1
    produced = {_compute_client_id(BASE, 2 + k) for k in range(n_alternates)}
    expected = {BASE + off for off in _CLIENT_ID_FALLBACK_OFFSETS if off != 0}
    assert produced == expected


def test_reset_to_base_after_clean_connect():
    """A successful connect resets consecutive_failures → next attempt is base."""
    # simulate: fail, fail, fall back, succeed (failures reset to 0)
    assert _compute_client_id(BASE, 3) == BASE + 200
    assert _compute_client_id(BASE, 0) == BASE  # post-success self-heal
