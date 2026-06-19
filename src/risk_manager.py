"""
risk_manager.py — distance-based entry logic (simplified from regime approach).

Entry requires BOTH:
  1. Premium check — spread must collect ≥ $0.25 credit at 0.03 delta
  2. Distance checks — short strike must be ≥ N× EM from SPX AND ≥ N× EM from major GEX level

Both conditions must pass for entry.

VIX-Adaptive Entry Parameters (TASK-2026-191):
  Entry param set (min_premium, rsi thresholds, distance multiples) is selected
  based on live VIX level. Each VIX bucket maps to a different parameter profile
  defined in config/config.yaml under entry.vix_buckets.

  VIX < 13  → NO TRADE
  VIX 13-16 → bucket "13-16"
  VIX 16-20 → bucket "16-20"
  VIX 20-25 → bucket "20-25"
  VIX 25-30 → bucket "25-30"
  VIX > 30  → NO TRADE

  If combined.vix is unavailable, fall back to expected_move * 16.
  VIX outside all buckets → skip entry entirely (no trade).

RSI Gate (TASK-2026-146):
  Each tick, RSI is read from CombinedSnapshot. If RSI > rsi_gate_threshold,
  only CALL side is evaluated (PUT is skipped). If RSI < threshold, only PUT
  side is evaluated (CALL is skipped). If RSI == threshold, this tick is skipped
  for entry (no side qualifies). The threshold is configurable via
  config["entry"]["rsi_gate_threshold"].

Strike Collision Check (TASK-2026-147):
  Before any entry is approved, all three collision conditions are checked via
  check_strike_collision(). If ANY condition triggers, the entry is rejected
  with structured logging:
    {TIME} ET [SKIP] {SIDE} | reason=strike_collision | collision_type={type} | ...
  collision_type values: same_short_strike, long_closes_existing_short,
  short_inside_existing_spread

  ALL THREE conditions are HARD BLOCKS — no inner trades allowed.
  Condition 3 (no inner trades): new short inside existing spread → hard reject.

Callers:
  - AutoTraderEngine.tick(): evaluate_entry(combined=combined, open_strikes=..., target_side=..., position_store=...)
  - backtest.py:             evaluate_entry(combined, None, None, open_strikes, target_side, store)
  Both use the CombinedSnapshot object as the single source of truth for scanner + GEX + TV data.
"""
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from config import CONFIG


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

# Backtest clock override. When set (callable returning a datetime), all time
# helpers below use it instead of the wall clock. Live/cloud leave it None →
# real time, so live behavior is unchanged. The engine sets this in backtest so
# market-hours gating (is_market_open) evaluates against the historical date.
_CLOCK_OVERRIDE = None


def set_clock_override(fn) -> None:
    """Install a clock function (returns datetime) for backtest, or None to clear."""
    global _CLOCK_OVERRIDE
    _CLOCK_OVERRIDE = fn


def _now_dt():
    return _CLOCK_OVERRIDE() if _CLOCK_OVERRIDE is not None else datetime.now()


def _now_et() -> str:
    return _now_dt().strftime("%H:%M:%S")


def _timestamp_et() -> str:
    return _now_dt().strftime("%Y-%m-%dT%H:%M:%S-04:00")


def _parse_hhmm(ts: str) -> tuple[int, int]:
    parts = ts.strip().split(":")
    return int(parts[0]), int(parts[1])


def current_et_hour_minute() -> tuple[int, int]:
    dt = _now_dt()
    return dt.hour, dt.minute


def is_market_open() -> bool:
    h, m = current_et_hour_minute()
    es_h, es_m = _parse_hhmm(CONFIG["market"]["entry_start"])
    ee_h, ee_m = _parse_hhmm(CONFIG["market"]["entry_end"])
    if h < es_h or (h == es_h and m < es_m):
        return False
    if h > ee_h or (h == ee_h and m >= ee_m):
        return False
    return True


def is_force_close_time() -> bool:
    return False


# ---------------------------------------------------------------------------
# VIX-adaptive helpers
# ---------------------------------------------------------------------------

VIX_BUCKET_BOUNDARIES = [
    (13.0, 16.0, "13-16"),   # [13, 16)
    (16.0, 20.0, "16-20"),   # [16, 20)
    (20.0, 25.0, "20-25"),   # [20, 25)
    (25.0, 30.0, "25-30"),   # [25, 30]  inclusive
]


def _get_vix_bucket(vix: float) -> Optional[str]:
    """
    Map a VIX value to a bucket name string.

    Returns:
      bucket name  — VIX falls in a bucket
      None         — VIX < 13 or VIX > 30 (skip entry entirely)
    """
    for lo, hi, name in VIX_BUCKET_BOUNDARIES:
        if name == "25-30":
            # Inclusive on both ends for the top bucket
            if lo <= vix <= hi:
                return name
        else:
            if lo <= vix < hi:
                return name
    return None


def _get_vix_effective(combined) -> float:
    """
    Return effective VIX value for a CombinedSnapshot.

    Uses combined.vix directly if available, otherwise derives from
    expected_move * 16 (per spec fallback pattern). Returns the derived
    value even when it would be None — callers handle the no-bucket case.
    """
    vix_attr = getattr(combined, "vix", None)
    if vix_attr is not None:
        return vix_attr
    em = getattr(combined, "expected_move", None)
    if em is not None and em > 0:
        return em * 16
    # Return a value outside all buckets so entry is blocked
    return 0.0


def _get_entry_params(combined) -> Optional[dict]:
    """
    Return the active entry param dict for a CombinedSnapshot.

    Logic:
      1. Resolve VIX (from combined.vix, or expected_move * 16)
      2. Map VIX → bucket name (_get_vix_bucket)
      3. If bucket found → use bucket params from config
      4. If VIX outside all buckets → return None
         (caller skips entry with [SKIP] log)

    Returns dict with keys: min_premium, rsi_upper_threshold,
    rsi_lower_threshold, distance_from_spot, distance_from_gex_level
    Or None if VIX is outside all buckets.
    """
    vix = _get_vix_effective(combined)
    bucket = _get_vix_bucket(vix)

    if bucket is not None:
        bucket_cfg = CONFIG["entry"]["vix_buckets"].get(bucket, {})
        if bucket_cfg:
            return {
                "min_premium":         bucket_cfg.get("min_premium", 0.25),
                "rsi_upper_threshold": bucket_cfg.get("rsi_upper_threshold", 60.0),
                "rsi_lower_threshold": bucket_cfg.get("rsi_lower_threshold", 40.0),
                "distance_from_spot":   bucket_cfg["distance"]["from_spot"],
                "distance_from_gex":    bucket_cfg["distance"]["from_gex_level"],
            }

    # VIX outside all buckets — signal caller to skip entry entirely
    return None


# ---------------------------------------------------------------------------
# Filter result tracking for structured logging
# ---------------------------------------------------------------------------

@dataclass
class FilterResult:
    premium_passed:       bool = True
    spot_distance_passed: bool = True
    gex_distance_passed:  bool = True
    max_positions_passed: bool = True
    overlap_passed:       bool = True

    @property
    def filters_passed(self) -> list[str]:
        passed = []
        if self.premium_passed:       passed.append("premium")
        if self.spot_distance_passed: passed.append("spot_distance")
        if self.gex_distance_passed:  passed.append("gex_distance")
        if self.max_positions_passed: passed.append("max_positions")
        if self.overlap_passed:       passed.append("overlap")
        return [] + passed

    @property
    def filters_failed(self) -> list[str]:
        failed = []
        if not self.premium_passed:       failed.append("premium_failed")
        if not self.spot_distance_passed: failed.append("spot_distance_failed")
        if not self.gex_distance_passed:  failed.append("gex_distance_failed")
        if not self.max_positions_passed: failed.append("max_positions_reached")
        if not self.overlap_passed:       failed.append("overlap_detected")
        return [] + failed

    @property
    def first_failure_reason(self) -> str:
        if not self.premium_passed:       return "premium_failed"
        if not self.spot_distance_passed: return "spot_distance_failed"
        if not self.gex_distance_passed:  return "gex_distance_failed"
        if not self.max_positions_passed: return "max_positions_reached"
        if not self.overlap_passed:       return "overlap_detected"
        return "unknown"

    def distance_passed_any(self) -> bool:
        return self.spot_distance_passed or self.gex_distance_passed


# ---------------------------------------------------------------------------
# Entry evaluation
# ---------------------------------------------------------------------------

@dataclass
class EntryDecision:
    approved:            bool
    side:                Optional[str] = None
    short_strike:        Optional[float] = None
    long_strike:         Optional[float] = None
    credit:              Optional[float] = None
    layer:               Optional[int] = None
    reason:              str = ""
    filter_result:       Optional[FilterResult] = None


def evaluate_entry(
    combined,
    open_strikes,
    target_side:         Optional[str] = None,
    position_store=None,
) -> EntryDecision:
    """
    VIX-Adaptive distance-based entry evaluation.

    Uses _get_entry_params() to select the active param set based on live VIX.
    VIX outside all buckets → skip entry (no trade).
    """
    if not is_market_open():
        fr = FilterResult()
        return EntryDecision(approved=False, reason="Outside market hours", filter_result=fr)

    # --- VIX bucket check ---
    vix = _get_vix_effective(combined)
    bucket = _get_vix_bucket(vix)
    logger = logging.getLogger("risk_manager")
    now_ts = _now_et()

    if bucket is None:
        logger.info(
            f"{now_ts} ET [SKIP] VIX={vix:.2f} outside all buckets — no trade"
        )
        fr = FilterResult()
        return EntryDecision(
            approved=False,
            reason=f"VIX={vix:.2f} outside all buckets — no trade",
            filter_result=fr,
        )

    # --- Resolve entry params via VIX bucket ---
    ep = _get_entry_params(combined)
    min_premium          = ep["min_premium"]
    rsi_upper_threshold  = ep["rsi_upper_threshold"]
    rsi_lower_threshold  = ep["rsi_lower_threshold"]
    dist_from_spot        = ep["distance_from_spot"]
    dist_from_gex         = ep["distance_from_gex"]

    rsi = getattr(combined, "rsi", 50.0)

    if target_side is None:
        if rsi > rsi_upper_threshold:
            target_side = "CALL"
        elif rsi < rsi_lower_threshold:
            target_side = "PUT"
        else:
            fr = FilterResult()
            return EntryDecision(
                approved=False,
                reason=(
                    f"RSI gate: RSI={rsi:.1f} in [{rsi_lower_threshold:.1f}, "
                    f"{rsi_upper_threshold:.1f}] — tick skipped"
                ),
                filter_result=fr,
            )
    else:
        pass

    em  = combined.expected_move
    spx = combined.spx_spot
    major_pos = combined.major_positive_by_volume
    major_neg = combined.major_negative_by_volume

    for side in [target_side]:
        fr = FilterResult()
        width_primary  = CONFIG["entry"].get("spread_width_primary", 10)
        width_fallback  = CONFIG["entry"].get("spread_width_fallback", 20)

        credit    = None
        short_strike_final = None
        long_strike_final  = None
        width_used         = None

        if side == "CALL":
            short_strike_base = combined.call_strike_003
            short_mid = combined.call_mid
            long_mid_10 = combined.call_10_long_mid
            long_mid_20 = combined.call_20_long_mid
        else:
            short_strike_base = combined.put_strike_003
            short_mid = combined.put_mid
            long_mid_10 = combined.put_10_long_mid
            long_mid_20 = combined.put_20_long_mid

        for width in (width_primary, width_fallback):
            long_mid = long_mid_10 if width == 10 else long_mid_20
            if short_mid is None or long_mid is None:
                continue
            if side == "CALL":
                short_strike_final = short_strike_base
                long_strike_final  = short_strike_base + width
            else:
                short_strike_final = short_strike_base
                long_strike_final  = short_strike_base - width
            credit = short_mid - long_mid
            width_used = width
            if credit >= min_premium:
                break

        if credit is None or credit < min_premium:
            fr.premium_passed = False
            return EntryDecision(
                approved=False, side=side, filter_result=fr,
                short_strike=short_strike_final, long_strike=long_strike_final,
                credit=credit,
            )

        if side == "CALL":
            major_level = major_pos if major_pos is not None else 0.0
        else:
            major_level = major_neg if major_neg is not None else 0.0

        displacement_from_spot = abs(short_strike_final - spx)
        displacement_from_gex   = abs(short_strike_final - major_level) if major_level != 0.0 else 0.0

        required_from_spot = dist_from_spot * em
        required_from_gex  = dist_from_gex  * em

        if displacement_from_spot < required_from_spot:
            fr.spot_distance_passed = False
            return EntryDecision(
                approved=False, side=side, filter_result=fr,
                short_strike=short_strike_final, long_strike=long_strike_final,
                credit=credit,
            )

        if displacement_from_gex < required_from_gex:
            fr.gex_distance_passed = False
            return EntryDecision(
                approved=False, side=side, filter_result=fr,
                short_strike=short_strike_final, long_strike=long_strike_final,
                credit=credit,
            )

        from position_store import check_strike_collision
        can_proceed, collision_type = check_strike_collision(
            new_short=short_strike_final,
            new_long=long_strike_final,
            open_strikes=open_strikes,
        )
        if not can_proceed:
            fr.overlap_passed = False
            logger.info(
                f"{now_ts} ET [SKIP] {side} | "
                f"reason=strike_collision | collision_type={collision_type} | "
                f"short={short_strike_final} long={long_strike_final}"
            )
            return EntryDecision(
                approved=False, side=side, short_strike=short_strike_final,
                long_strike=long_strike_final, filter_result=fr,
                reason=f"strike_collision | collision_type={collision_type}",
            )

        layer = 1
        em_dist_label = (
            f"spot_disp={displacement_from_spot:.2f}/{required_from_spot:.2f}, "
            f"gex_disp={displacement_from_gex:.2f}/{required_from_gex:.2f}"
        )
        rsi_note = (
            f"RSI_gate bucket={bucket} | "
            f"RSI={rsi:.1f} in [{rsi_lower_threshold:.1f}, {rsi_upper_threshold:.1f}] "
            f"-> {side}"
        )

        return EntryDecision(
            approved=True, side=side, short_strike=short_strike_final,
            long_strike=long_strike_final, credit=credit, layer=layer,
            filter_result=fr,
            reason=(
                f"Layer {layer} | VIX bucket={bucket} | {side} "
                f"0.03delta@{short_strike_final}x{long_strike_final} "
                f"({width_used:.0f}-wide) | credit=${credit:.2f} | "
                f"SPX={spx:.2f} EM={em:.2f} | {em_dist_label} | {rsi_note}"
            ),
        )

    fr = FilterResult()
    return EntryDecision(approved=False, filter_result=fr, reason="No side passed all entry filters")


# ---------------------------------------------------------------------------
# Exit evaluation
# ---------------------------------------------------------------------------

@dataclass
class ExitDecision:
    should_exit:          bool
    reason:               str = ""
    forced:               bool = False
    exit_layer:           int = 1
    exit_conditions_met:  int = 0
    exit_regime:          Optional[str] = None
    displacement:         float = 0.0
    near_major:           bool = False
    major_level:          Optional[float] = None


def evaluate_exit(
    position,
    combined,
) -> ExitDecision:
    """
    Two-level exit logic (TASK-2026-187, simplified TASK-2026-199):

    L1: SPX crosses short strike -> EXIT (hard stop, unchanged).

    L2: BOTH conditions must be true to EXIT:
        1. |SPX - short_strike| < position.entry_em   (SPX is near the short strike)
        2. near major (|short_strike - major_level| < current_em)
           (short strike is near a major GEX volume level)
        Otherwise -> STAY

    ADX and GEX checks removed in TASK-2026-199 (L2 simplified to 2 conditions).

    near major uses CURRENT expected_move (combined.expected_move), NOT entry_em.

    Decision table (L2 -- L1 always fires first when SPX crosses strike):

    | Displacement vs entry_em | Near major | Action |
    |--------------------------|-------------|--------|
    | >= entry_em (away)       | --          | STAY   |
    | < entry_em (near)        | False       | STAY   |
    | < entry_em (near)        | True        | **EXIT** |

    entry_em is captured and stored at position open time (via add_position
    in position_store.py). No force-close -- positions run naturally.
    """
    spx = combined.spx_spot
    em  = combined.expected_move
    regime = combined.regime or "neutral"
    exit_regime = regime

    side = position.side.value

    # --- L1: SPX crosses short strike -> exit (hard stop) ---
    if side == "CALL":
        if spx >= position.short_strike:
            return ExitDecision(
                should_exit=True,
                reason=f"SPX {spx:.2f} >= CALL short strike {position.short_strike} (L1 crossed)",
                exit_layer=1,
                exit_conditions_met=0,
                exit_regime=exit_regime,
                displacement=0.0,
                near_major=False,
                major_level=None,
            )
    else:
        if spx <= position.short_strike:
            return ExitDecision(
                should_exit=True,
                reason=f"SPX {spx:.2f} <= PUT short strike {position.short_strike} (L1 crossed)",
                exit_layer=1,
                exit_conditions_met=0,
                exit_regime=exit_regime,
                displacement=0.0,
                near_major=False,
                major_level=None,
            )

    # --- L2: proximity + near-major exit (TASK-2026-199: simplified to 2 conditions) ---

    if position.entry_em is None or position.entry_em <= 0:
        return ExitDecision(
            should_exit=False,
            reason="entry_em not set -- L2 inactive",
            exit_layer=2,
            exit_conditions_met=0,
            exit_regime=exit_regime,
            displacement=0.0,
            near_major=False,
            major_level=None,
        )

    displacement = abs(spx - position.short_strike)

    # Assign major_level BEFORE first use (Condition 1 proximity check)
    if side == "CALL":
        major_level = getattr(combined, "major_positive_by_volume", None)
    else:
        major_level = getattr(combined, "major_negative_by_volume", None)

    # Condition 1: proximity check
    if displacement >= position.entry_em:
        return ExitDecision(
            should_exit=False,
            reason=(
                f"L2 | STAY | SPX={spx:.2f} short={position.short_strike:.0f} "
                f"disp={displacement:.2f} >= entry_em={position.entry_em:.2f}"
            ),
            exit_layer=2,
            exit_conditions_met=0,
            exit_regime=exit_regime,
            displacement=displacement,
            near_major=False,
            major_level=major_level if major_level is not None else None,
        )

    # Condition 2: near-major check
    # (moved above Condition 1 — major_level now assigned before first use)

    near_major = False
    if major_level is not None and major_level != 0.0:
        displacement_from_major = abs(position.short_strike - major_level)
        near_major = displacement_from_major < em  # uses CURRENT expected_move

    if near_major:
        return ExitDecision(
            should_exit=True,
            reason=(
                f"L2 | EXIT | disp={displacement:.2f} < entry_em={position.entry_em:.2f} "
                f"near_major=True | SPX={spx:.2f} short={position.short_strike:.0f}"
            ),
            exit_layer=2,
            exit_conditions_met=2,
            exit_regime=exit_regime,
            displacement=displacement,
            near_major=True,
            major_level=major_level if major_level is not None else None,
        )

    return ExitDecision(
        should_exit=False,
        reason=(
            f"L2 | STAY | proximity OK disp={displacement:.2f} < entry_em={position.entry_em:.2f} "
            f"but near_major=False | SPX={spx:.2f} short={position.short_strike:.0f}"
        ),
        exit_layer=2,
        exit_conditions_met=1,
        exit_regime=exit_regime,
        displacement=displacement,
        near_major=False,
        major_level=major_level if major_level is not None else None,
    )


if __name__ == "__main__":
    print(f"Market open: {is_market_open()}")
