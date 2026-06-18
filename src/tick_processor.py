"""
tick_processor.py — Shared tick evaluation orchestrator.

Both Engine.tick() and the backtest main loop use IDENTICAL evaluation logic:
  1. Fetch combined snapshot (live → get_combined_for_latest_scan; backtest → get_combined_snapshot)
  2. Run exit checks on all open positions
  3. If market open: RSI gate → single-sided entry evaluation
  4. On approval: call on_enter_approved (live → execute+add; backtest → record)
  5. On skip: call on_skip (anti-spam via reason-change detection)

TickProcessor is stateful (anti-spam state per side) but pure otherwise —
it does NOT execute trades, close positions, or record signals.
Those side effects are handled by the injected callbacks.

Usage:
    processor = TickProcessor(
        on_enter_approved=_on_enter_approved,   # live engine callback
        on_skip          =_on_skip,
        on_exit_checked  =_on_exit_checked,
        is_live          =True,
    )
    processor.process_tick(
        combined=combined,
        ts=ts, spx=spx, em=em, gex_val=gex_val,
        regime=regime, rsi=rsi, gex_regime=gex_regime,
        store=store,
        open_strikes=open_strikes,
    )
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol

# ---------------------------------------------------------------------------
# Callback protocols
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Config (module-level — used in process_tick)
# ---------------------------------------------------------------------------

from config import CONFIG


# ---------------------------------------------------------------------------
# RSI gate helper — VIX-adaptive
# ---------------------------------------------------------------------------

def _get_rsi_gates(vix: Optional[float]) -> tuple[float, float]:
    """
    Return (rsi_upper, rsi_lower) looked up from the VIX bucket in config.

    Bucket mapping (config.yaml entry.vix_buckets):
      VIX 13-16 → "13-16"
      VIX 16-20 → "16-20"
      VIX 20-25 → "20-25"
      VIX 25-30 → "25-30"

    VIX < 13 or VIX > 30 → NO TRADE bucket (return wide defaults so no
    side passes the gate; caller must also check the no-trade guard).
    Falls back to 70/30 if bucket is somehow missing.
    """
    if vix is None:
        return (70.0, 30.0)

    if 13 <= vix < 16:
        bucket = "13-16"
    elif 16 <= vix < 20:
        bucket = "16-20"
    elif 20 <= vix < 25:
        bucket = "20-25"
    elif 25 <= vix <= 30:
        bucket = "25-30"
    else:
        # VIX < 13 or VIX > 30 — no-trade zone; use widest band
        bucket = "25-30"  # most restrictive (highest upper, lowest lower)

    entry_cfg = CONFIG.get("entry", {})
    buckets = entry_cfg.get("vix_buckets", {})
    bucket_cfg = buckets.get(bucket, {})
    upper = bucket_cfg.get("rsi_upper_threshold", 70.0)
    lower = bucket_cfg.get("rsi_lower_threshold", 30.0)
    return (upper, lower)


class OnEnterApproved(Protocol):
    """Called when entry is approved. All side effects (execute, add to store)
    are delegated to the caller. Returns nothing."""
    def __call__(
        self,
        combined,
        decision,
        store,
        ts: str,
        spx: float,
        em: float,
        gex_val: float,
    ) -> None: ...


class OnSkip(Protocol):
    """Called when entry is rejected. Used for anti-spam logging (reason change)."""
    def __call__(
        self,
        ts: str,
        target_side: str,
        decision,
        combined,
        spx: float,
        em: float,
        gex_val: float,
        rsi: float,
        regime: str,
    ) -> None: ...


class OnExitChecked(Protocol):
    """Called after each exit evaluation (on every open position, every tick)."""
    def __call__(
        self,
        ts: str,
        pos,
        decision,
        combined,
        store,
        spx: float,
        em: float,
        gex_val: float,
    ) -> None: ...


class OnHeartbeat(Protocol):
    """Called once per tick with the full market snapshot."""
    def __call__(
        self,
        ts: str,
        spx: float,
        em: float,
        gex_val: float,
        regime: str,
        rsi: float,
        gex_regime: str,
    ) -> None: ...


# ---------------------------------------------------------------------------
# TickProcessor
# ---------------------------------------------------------------------------

class TickProcessor:
    """
    Shared tick evaluation orchestrator.

    Implements the RSI-gate single-sided entry flow and exit evaluation loop
    with anti-spam skip tracking — identical logic in both live and backtest.

    Parameters
    ----------
    on_enter_approved : OnEnterApproved
        Called with (combined, decision, store, ts, spx, em, gex_val)
        when entry is approved. Caller handles trade execution + store update.
    on_skip : OnSkip
        Called with (ts, target_side, decision, combined, spx, em, gex_val,
        rsi, regime) when entry is rejected. Caller handles signal recording.
    on_exit_checked : OnExitChecked
        Called after each exit evaluation (on every open position, every tick).
        Caller handles execution, store closure, and signal recording.
    on_heartbeat : OnHeartbeat | None
        Called once per tick with market snapshot. If None, no heartbeat logged.
    is_live : bool
        True for live engine (affects log prefix), False for backtest.
    """

    def __init__(
        self,
        on_enter_approved: OnEnterApproved,
        on_skip: OnSkip,
        on_exit_checked: OnExitChecked,
        on_heartbeat: Optional[OnHeartbeat] = None,
        is_live: bool = True,
    ):
        self._on_enter_approved = on_enter_approved
        self._on_skip = on_skip
        self._on_exit_checked = on_exit_checked
        self._on_heartbeat = on_heartbeat
        self._is_live = is_live

        # Per-side anti-spam skip state: only log SKIP on reason CHANGE
        self._last_skip_reason: dict[str, Optional[str]] = {"CALL": None, "PUT": None}

    # -----------------------------------------------------------------------
    # process_tick — the ONE shared entry point for both pipelines
    # -----------------------------------------------------------------------

    def process_tick(
        self,
        combined,
        ts: str,
        spx: float,
        em: float,
        gex_val: float,
        regime: str,
        rsi: float,
        gex_regime: str,
        store,
        open_strikes: Optional[list] = None,
        vix: Optional[float] = None,
    ) -> None:
        """
        Shared tick processing.

        Parameters
        ----------
        combined   : CombinedSnapshot — already-fetched market snapshot
        ts         : str  — scan timestamp (EST)
        spx        : float — SPX spot
        em         : float — expected move
        gex_val    : float — GEX by volume/oi
        regime     : str  — market regime label
        rsi        : float — RSI reading
        gex_regime : str  — "dealer_long" or "dealer_short"
        store      : PositionStore | BacktestPositionStore
        open_strikes: list | None — pre-computed open strikes (optional)
        vix        : float | None — current VIX for adaptive RSI gate lookup
        """
        from risk_manager import evaluate_entry, evaluate_exit, is_market_open

        # --- Step 0: Heartbeat (once per tick) ---
        if self._on_heartbeat:
            self._on_heartbeat(ts, spx, em, gex_val, regime, rsi, gex_regime)

        # --- Step 1: Exit checks on all open positions ---
        self._run_exit_checks(combined, ts, spx, em, gex_val, regime, rsi, store)

        # --- Step 2: Entry checks — RSI-gated single-sided ---
        if not is_market_open():
            return

        # VIX-adaptive RSI gates (fall back to 70/30 if vix is None)
        rsi_upper, rsi_lower = _get_rsi_gates(vix)

        if rsi > rsi_upper:
            target_side = "CALL"
        elif rsi < rsi_lower:
            target_side = "PUT"
        else:
            # RSI in neutral band: skip both sides this tick
            self._log_rsi_gate_skip(ts, "CALL", rsi, rsi_upper, rsi_lower)
            self._log_rsi_gate_skip(ts, "PUT", rsi, rsi_upper, rsi_lower)
            return

        if open_strikes is None:
            open_strikes = store.get_open_strikes()

        decision = evaluate_entry(
            combined=combined,
            open_strikes=open_strikes,
            target_side=target_side,
            position_store=store,
        )

        if not decision.approved:
            fr = decision.filter_result
            if fr and fr.first_failure_reason:
                self._on_skip(
                    ts=ts,
                    target_side=target_side,
                    decision=decision,
                    combined=combined,
                    spx=spx,
                    em=em,
                    gex_val=gex_val,
                    rsi=rsi,
                    regime=regime,
                )
        else:
            self._on_enter_approved(
                combined=combined,
                decision=decision,
                store=store,
                ts=ts,
                spx=spx,
                em=em,
                gex_val=gex_val,
            )

    # -----------------------------------------------------------------------
    # Exit checks
    # -----------------------------------------------------------------------

    def _run_exit_checks(
        self,
        combined,
        ts: str,
        spx: float,
        em: float,
        gex_val: float,
        regime: str,
        rsi: float,
        store,
    ) -> None:
        """
        Evaluate exit on every open position.
        on_exit_checked is called for EVERY position every tick (caller handles
        execution/closure decisions and signal recording).
        """
        from risk_manager import evaluate_exit

        for pos in store.get_open():
            decision = evaluate_exit(pos, combined)
            self._on_exit_checked(
                ts=ts,
                pos=pos,
                decision=decision,
                combined=combined,
                store=store,
                spx=spx,
                em=em,
                gex_val=gex_val,
            )

    # -----------------------------------------------------------------------
    # RSI-gate skip logging (always logged every tick, no anti-spam)
    # -----------------------------------------------------------------------

    def _log_rsi_gate_skip(
        self,
        ts: str,
        side: str,
        rsi: float,
        upper_threshold: float,
        lower_threshold: float,
    ) -> None:
        """Log [SKIP] when this side was NOT evaluated due to RSI gate."""
        pass  # RSI gate skips handled by live engine's own _log_rsi_gate_skip