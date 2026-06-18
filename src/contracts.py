"""
contracts.py — SPX COMBO contract builders for ib_async.

Provides:
  build_spread_combo()  — generic COMBO contract builder
  SPX_COMBO_PCS()       — Put Calendar Spread (short=higher strike put, long=lower strike put)
  SPX_COMBO_CCS()       — Call Calendar Spread (short=lower strike call, long=higher strike call)

All contracts are 2-leg COMBO spreads for SPX 0DTE. No single-leg orders.
"""
from ib_async import Contract, ComboLeg


# ---------------------------------------------------------------------------
# Option right mapping — OrderParams.contract_type ("PUT"/"CALL") → ib_async ("P"/"C")
# ---------------------------------------------------------------------------

_RIGHT_MAP = {"PUT": "P", "CALL": "C"}


def _right_for(contract_type: str) -> str:
    """Convert 'PUT' → 'P', 'CALL' → 'C'; pass through if already short."""
    if contract_type in _RIGHT_MAP:
        return _RIGHT_MAP[contract_type]
    return contract_type      # already "P" or "C"


# ---------------------------------------------------------------------------
# Combo contract builders
# ---------------------------------------------------------------------------

def build_spread_combo(
    right: str,
    short_strike: float,
    long_strike: float,
    expiry: str,
    exchange: str = "BOX",
) -> Contract:
    """
    Build a COMB contract for an SPX 0DTE calendar spread.
    Used for both opening (SELL combo) and closing (BUY combo).

    right:        "P" (PCS) or "C" (CCS) — short option right code
    short_strike: the strike of the leg being sold (higher for puts, lower for calls)
    long_strike:  the strike of the leg being bought
    expiry:       YYYYMMDD — same for both legs (0DTE single-expiry spread)
    exchange:     "BOX" (Nasdaq Options)
    """
    contract = Contract()
    contract.symbol = "SPX"
    contract.secType = "COMB"
    contract.exchange = exchange
    contract.primaryExchange = "NASDAQ"
    contract.currency = "USD"
    contract.right = _right_for(right)     # "PUT"/"CALL" → "P"/"C"
    contract.strike = short_strike          # reference price (net credit/debit strike)
    contract.lastTradeDateOrContractMonth = expiry
    contract.multiplier = "100"

    # Leg 1: SHORT — the higher-premium leg we are selling when opening
    short_leg = ComboLeg()
    short_leg.conId = 0                     # resolved via qualifyContracts() before place
    short_leg.exchange = exchange
    short_leg.action = "SELL"              # always SELL the short leg when opening
    short_leg.ratio = 1
    short_leg.shortSaleSlot = 0
    short_leg.designatedLocation = ""
    short_leg.exemptCode = -1

    # Leg 2: LONG — the lower-premium leg we are buying when opening
    long_leg = ComboLeg()
    long_leg.conId = 0
    long_leg.exchange = exchange
    long_leg.action = "BUY"                # always BUY the long leg when opening
    long_leg.ratio = 1
    long_leg.shortSaleSlot = 0
    long_leg.designatedLocation = ""
    long_leg.exemptCode = -1

    contract.comboLegs = [short_leg, long_leg]
    return contract


def SPX_COMBO_PCS(short_strike: float, long_strike: float, expiry: str) -> Contract:
    """
    SPX Put Calendar Spread (PCS).

    - right: "P"
    - Short leg: SELL the higher-strike put (closer to ATM, collects premium)
    - Long leg:  BUY the lower-strike put (further OTM, limits risk)

    Example: SPX_COMBO_PCS(short_strike=5500, long_strike=5490, expiry="20250519")
      → SELL 5500P / BUY 5490P (10-wide spread)
    """
    return build_spread_combo(
        right="P",
        short_strike=short_strike,
        long_strike=long_strike,
        expiry=expiry,
    )


def SPX_COMBO_CCS(short_strike: float, long_strike: float, expiry: str) -> Contract:
    """
    SPX Call Calendar Spread (CCS).

    - right: "C"
    - Short leg: SELL the lower-strike call (closer to ATM, collects premium)
    - Long leg:  BUY the higher-strike call (further OTM, limits risk)

    Example: SPX_COMBO_CCS(short_strike=5500, long_strike=5510, expiry="20250519")
      → SELL 5500C / BUY 5510C (10-wide spread)
    """
    return build_spread_combo(
        right="C",
        short_strike=short_strike,
        long_strike=long_strike,
        expiry=expiry,
    )
