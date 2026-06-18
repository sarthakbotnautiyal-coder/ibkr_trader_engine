"""
tests/test_contracts.py — TDD for src/contracts.py
Tests SPX COMBO contract builders for CCS and PCS calendar spreads.
"""
import pytest
import sys
from pathlib import Path

_root = Path(__file__).parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))

from ib_async import Contract, ComboLeg
from src.contracts import build_spread_combo, SPX_COMBO_PCS, SPX_COMBO_CCS, _right_for


# ---------------------------------------------------------------------------
# _right_for — helper tests
# ---------------------------------------------------------------------------

def test_right_for_put_maps_to_P():
    assert _right_for("PUT") == "P"


def test_right_for_call_maps_to_C():
    assert _right_for("CALL") == "C"


def test_right_for_already_short_unchanged():
    assert _right_for("P") == "P"
    assert _right_for("C") == "C"


# ---------------------------------------------------------------------------
# build_spread_combo — structural tests
# ---------------------------------------------------------------------------

def test_build_spread_combo_returns_contract():
    c = build_spread_combo(right="P", short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert isinstance(c, Contract)


def test_build_spread_combo_symbol_spx():
    c = build_spread_combo(right="P", short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert c.symbol == "SPX"


def test_build_spread_combo_sec_type_comb():
    c = build_spread_combo(right="P", short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert c.secType == "COMB"


def test_build_spread_combo_exchange_box():
    c = build_spread_combo(right="P", short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert c.exchange == "BOX"


def test_build_spread_combo_primary_exchange_nasdaq():
    c = build_spread_combo(right="P", short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert c.primaryExchange == "NASDAQ"


def test_build_spread_combo_currency_usd():
    c = build_spread_combo(right="P", short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert c.currency == "USD"


def test_build_spread_combo_multiplier_100():
    c = build_spread_combo(right="P", short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert c.multiplier == "100"


def test_build_spread_combo_has_two_combo_legs():
    c = build_spread_combo(right="P", short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert len(c.comboLegs) == 2


def test_build_spread_combo_right_P_for_put_input():
    """build_spread_combo accepts 'PUT' as right and converts to 'P'."""
    c = build_spread_combo(right="PUT", short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert c.right == "P"


def test_build_spread_combo_right_C_for_call_input():
    """build_spread_combo accepts 'CALL' as right and converts to 'C'."""
    c = build_spread_combo(right="CALL", short_strike=5500.0, long_strike=5510.0, expiry="20250519")
    assert c.right == "C"


# ---------------------------------------------------------------------------
# SPX_COMBO_PCS — Put Calendar Spread
# ---------------------------------------------------------------------------

def test_spx_combo_pcs_right_is_P():
    """PCS right should be 'P' (put option)."""
    c = SPX_COMBO_PCS(short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert c.right == "P"


def test_spx_combo_pcs_strike_equals_short_strike():
    """Contract.strike should be the short strike (reference price)."""
    c = SPX_COMBO_PCS(short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert c.strike == 5500.0


def test_spx_combo_pcs_expiry_matches_param():
    """Both legs share the same expiry (0DTE — same YYYYMMDD)."""
    c = SPX_COMBO_PCS(short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert c.lastTradeDateOrContractMonth == "20250519"


def test_spx_combo_pcs_legs_have_conid_zero():
    """Combo leg conIds are 0 (unresolved until reqContractDetails)."""
    c = SPX_COMBO_PCS(short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert c.comboLegs[0].conId == 0
    assert c.comboLegs[1].conId == 0


def test_spx_combo_pcs_short_leg_action_sell():
    """PCS: short leg (higher strike put) is SELL when opening."""
    c = SPX_COMBO_PCS(short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    short_leg = c.comboLegs[0]
    assert short_leg.action == "SELL"


def test_spx_combo_pcs_long_leg_action_buy():
    """PCS: long leg (lower strike put) is BUY when opening."""
    c = SPX_COMBO_PCS(short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    long_leg = c.comboLegs[1]
    assert long_leg.action == "BUY"


def test_spx_combo_pcs_short_leg_ratio_1():
    c = SPX_COMBO_PCS(short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert c.comboLegs[0].ratio == 1


def test_spx_combo_pcs_long_leg_ratio_1():
    c = SPX_COMBO_PCS(short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert c.comboLegs[1].ratio == 1


def test_spx_combo_pcs_legs_underlyer_spx():
    """Both legs should reference SPX (symbol on the contract)."""
    c = SPX_COMBO_PCS(short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert c.symbol == "SPX"


def test_spx_combo_pcs_both_legs_same_exchange():
    """Both combo legs use BOX exchange."""
    c = SPX_COMBO_PCS(short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert c.comboLegs[0].exchange == "BOX"
    assert c.comboLegs[1].exchange == "BOX"


# ---------------------------------------------------------------------------
# SPX_COMBO_CCS — Call Calendar Spread
# ---------------------------------------------------------------------------

def test_spx_combo_ccs_right_is_C():
    """CCS right should be 'C' (call option)."""
    c = SPX_COMBO_CCS(short_strike=5500.0, long_strike=5510.0, expiry="20250519")
    assert c.right == "C"


def test_spx_combo_ccs_short_leg_action_sell():
    """CCS: short leg (lower strike call) is SELL when opening."""
    c = SPX_COMBO_CCS(short_strike=5500.0, long_strike=5510.0, expiry="20250519")
    short_leg = c.comboLegs[0]
    assert short_leg.action == "SELL"


def test_spx_combo_ccs_long_leg_action_buy():
    """CCS: long leg (higher strike call) is BUY when opening."""
    c = SPX_COMBO_CCS(short_strike=5500.0, long_strike=5510.0, expiry="20250519")
    long_leg = c.comboLegs[1]
    assert long_leg.action == "BUY"


def test_spx_combo_ccs_strike_equals_short_strike():
    c = SPX_COMBO_CCS(short_strike=5500.0, long_strike=5510.0, expiry="20250519")
    assert c.strike == 5500.0


def test_spx_combo_ccs_legs_have_conid_zero():
    """Both legs of CCS have conId=0 (unresolved)."""
    c = SPX_COMBO_CCS(short_strike=5500.0, long_strike=5510.0, expiry="20250519")
    assert c.comboLegs[0].conId == 0
    assert c.comboLegs[1].conId == 0


# ---------------------------------------------------------------------------
# Cross-spread invariant tests
# ---------------------------------------------------------------------------

def test_pcs_and_ccs_have_correct_rights():
    """PCS must have right='P', CCS must have right='C'."""
    pcs = SPX_COMBO_PCS(short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    ccs = SPX_COMBO_CCS(short_strike=5500.0, long_strike=5510.0, expiry="20250519")
    assert pcs.right == "P"
    assert ccs.right == "C"


def test_combo_legs_action_opposite_for_pcs():
    """PCS short leg is SELL, long leg is BUY."""
    c = SPX_COMBO_PCS(short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    assert c.comboLegs[0].action == "SELL"
    assert c.comboLegs[1].action == "BUY"


def test_combo_legs_action_opposite_for_ccs():
    """CCS short leg is SELL, long leg is BUY."""
    c = SPX_COMBO_CCS(short_strike=5500.0, long_strike=5510.0, expiry="20250519")
    assert c.comboLegs[0].action == "SELL"
    assert c.comboLegs[1].action == "BUY"


def test_combo_expiry_today_same_for_both_legs():
    """Both legs share the same expiry string (0DTE calendar spread = single date)."""
    expiry = "20250519"
    c = SPX_COMBO_PCS(short_strike=5500.0, long_strike=5490.0, expiry=expiry)
    assert c.lastTradeDateOrContractMonth == expiry
    assert c.comboLegs[0].conId == 0  # unresolved at this stage
    assert c.comboLegs[1].conId == 0


def test_combo_underlyer_spx():
    """SPX is the underlyer for all combos."""
    for right in ["P", "C"]:
        c = build_spread_combo(right=right, short_strike=5500.0, long_strike=5490.0, expiry="20250519")
        assert c.symbol == "SPX"


# ---------------------------------------------------------------------------
# Combo leg field completeness
# ---------------------------------------------------------------------------

def test_combo_legs_have_short_sale_slot_zero():
    """shortSaleSlot=0 for both legs (no special locate required)."""
    c = SPX_COMBO_PCS(short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    for leg in c.comboLegs:
        assert leg.shortSaleSlot == 0


def test_combo_legs_have_exempt_code_minus_one():
    """exemptCode=-1 means no exemption (standard locate)."""
    c = SPX_COMBO_PCS(short_strike=5500.0, long_strike=5490.0, expiry="20250519")
    for leg in c.comboLegs:
        assert leg.exemptCode == -1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])