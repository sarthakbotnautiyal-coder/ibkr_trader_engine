"""
risk_manager.py — distance-based entry logic (simplified from regime approach).

Entry requires BOTH:
  1. Premium check — spread must collect ≥ $0.25 credit at 0.03 delta
  2. Distance checks — short strike must be ≥ N× EM from SPX AND ≥ N× EM from major GEX level

Both conditions must pass for entry.

VIX-Adaptive Entry Parameters (TASK-2026-191):
  Entry param set (min_premium, rsi thresholds, distance multiples) is selected
  based on live VIX level. Each VIX bucket maps to a different parameter profile
  defined in config/config.yaml under entry.vix_buckets. Bucket boundaries are
  derived solely from the config keys ("13-16", "16-20", ...) — adding or
  re-ranging buckets is a config-only change (see src/vix_buckets.py).

  If combined.vix is unavailable, fall back to expected_move * 16.
  VIX outside all buckets (below the lowest or above the highest) → skip
  entry entirely (no trade).

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
from zoneinfo import ZoneInfo
from typing import Optional

from config import CONFIG

import vix_buckets


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

# Single source of truth for the trading timezone. ZoneInfo handles EST/EDT
# automatically, so timestamps carry the correct offset (-05:00 in winter,
# -04:00 in summer) without any hardcoding.
ET = ZoneInfo("America/New_York")

# Backtest clock override. When set (callable returning a datetime), all time
# helpers below use it instead of the wall clock. Live/cloud leave it None →
# real time, so live behavior is unchanged. The engine sets this in backtest so
# market-hours gating (is_market_open) evaluates against the historical date.
# The override is expected to return an ET-aware datetime.
_CLOCK_OVERRIDE = None


def set_clock_override(fn) -> None:
    """Install a clock function (returns datetime) for backtest, or None to clear."""
    global _CLOCK_OVERRIDE
    _CLOCK_OVERRIDE = fn


def _now_dt():
    """Current ET-aware datetime (backtest clock when overridden, else now)."""
    return _CLOCK_OVERRIDE() if _CLOCK_OVERRIDE is not None else datetime.now(ET)


def _now_et() -> str:
    return _now_dt().strftime("%H:%M:%S")


def _timestamp_et() -> str:
    # isoformat() emits the correct EST/EDT offset for the date (DST-aware).
    return _now_dt().isoformat(timespec="seconds")


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

def _get_vix_bucket(vix: float) -> Optional[str]:
    """
    Map a VIX value to a bucket name string. Boundaries are derived from
    config.yaml entry.vix_buckets keys (see src/vix_buckets.py).

    Returns:
      bucket name  — VIX falls in a bucket
      None         — VIX outside all buckets (skip entry entirely)
    """
    bucket = vix_buckets.classify(vix)
    return bucket.name if bucket is not None else None


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


def _num(x) -> Optional[float]:
    """Return x as a float if it is a real finite number, else None. Indicator
    votes that depend on a missing/malformed value are simply skipped."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        xf = float(x)
        return None if xf != xf else xf  # drop NaN
    return None


def _exit_cfg() -> dict:
    """L2 exit thresholds from config, with safe defaults if the section is absent."""
    cfg = CONFIG.get("exit", {}) or {}
    return dict(
        votes_to_exit = int(cfg.get("votes_to_exit", 2)),
        adx_floor     = float(cfg.get("adx_floor", 20.0)),
        adx_rise      = float(cfg.get("adx_rise", 8.0)),
        vix1d_mult    = float(cfg.get("vix1d_mult", 1.20)),
        rsi_put       = float(cfg.get("rsi_put", 35.0)),
        rsi_call      = float(cfg.get("rsi_call", 65.0)),
        premium_mult  = float(cfg.get("premium_mult", 3.0)),
        # Profit-take (layer 3). Default 0.0 → feature OFF, so configs written
        # before these keys existed keep today's behavior exactly (backward compat).
        profit_take_debit         = float(cfg.get("profit_take_debit", 0.0)),
        profit_take_cutoff        = str(cfg.get("profit_take_cutoff", "15:00")),
        profit_take_confirm_ticks = int(cfg.get("profit_take_confirm_ticks", 2)),
        profit_take_max_quote_age = float(cfg.get("profit_take_max_quote_age", 90.0)),
    )


# ---------------------------------------------------------------------------
# Profit-take exit (layer 3) — buy back a decayed spread to lock in the win
# ---------------------------------------------------------------------------

# Debounce state: consecutive ticks the profit-take condition has held, keyed
# by position db_id. In-memory only — after an engine restart the streak simply
# rebuilds (worst case the exit fires confirm_ticks-1 ticks later). Entries are
# dropped the moment the condition breaks or the exit fires, so the dict stays
# tiny (bounded by concurrently-open positions).
_PT_STREAKS: dict = {}


def reset_profit_take_state() -> None:
    """Clear profit-take debounce state (used by tests)."""
    _PT_STREAKS.clear()


def _profit_take_decision(position, combo_quote, exit_regime) -> Optional["ExitDecision"]:
    """Return an exit_layer=3 ExitDecision when the profit-take fires, else None.

    Every gate below is suppress-only: any missing/doubtful input returns None
    (position rides to expiry exactly as before this feature existed). Gates:
      config    — exit.profit_take_debit > 0 (absent/0 → feature off)
      cutoff    — before exit.profit_take_cutoff ET; in the last hour theta
                  finishes the decay for free, so ride to expiry instead
      quote     — live TWO-SIDED book only (never a last/close fallback: a
                  stale-low mark must not trigger a buy-back)
      freshness — ticker updated within profit_take_max_quote_age seconds
      price     — close_debit (= what a market-order close pays, the BID side)
                  <= profit_take_debit, so the fill lands at ~threshold
      debounce  — condition must hold profit_take_confirm_ticks consecutive
                  ticks so one flickering print can't fire the exit
    """
    cfg = _exit_cfg()
    pt = cfg["profit_take_debit"]
    if pt <= 0:
        return None  # unconfigured → feature off (backward compat)

    pos_id = getattr(position, "db_id", None)
    if pos_id is None:
        return None

    def _reset() -> None:
        _PT_STREAKS.pop(pos_id, None)

    # Last-hour gate (backtest-safe via the clock override).
    try:
        cut_h, cut_m = _parse_hhmm(cfg["profit_take_cutoff"])
    except Exception:
        cut_h, cut_m = 15, 0
    now = _now_dt()
    if (now.hour, now.minute) >= (cut_h, cut_m):
        _reset()
        return None

    # Quote-quality gates.
    if combo_quote is None or not getattr(combo_quote, "two_sided", False):
        _reset()
        return None
    close_debit = _num(getattr(combo_quote, "close_debit", None))
    if close_debit is None:
        _reset()
        return None
    age = _num(getattr(combo_quote, "age_sec", None))
    if age is None or age > cfg["profit_take_max_quote_age"]:
        _reset()
        return None

    # Price gate.
    if close_debit > pt:
        _reset()
        return None

    # Condition holds this tick — debounce across consecutive ticks.
    streak = _PT_STREAKS.get(pos_id, 0) + 1
    if streak < cfg["profit_take_confirm_ticks"]:
        _PT_STREAKS[pos_id] = streak
        return None
    _reset()

    credit = getattr(position, "credit", None) or 0.0
    pct_txt = ""
    if credit > 0:
        pct_txt = f" (~{(credit - close_debit) / credit * 100.0:.0f}% of credit locked)"
    return ExitDecision(
        should_exit=True,
        reason=(
            f"PROFIT_TAKE | close debit {close_debit:.2f} <= {pt:.2f} "
            f"for {streak} ticks (bid={combo_quote.bid:.2f} ask={combo_quote.ask:.2f} "
            f"age={age:.0f}s){pct_txt}"
        ),
        exit_layer=3,
        exit_conditions_met=streak,
        exit_regime=exit_regime,
    )


def evaluate_exit(
    position,
    combined,
    current_debit: Optional[float] = None,
    combo_quote=None,
) -> ExitDecision:
    """
    Momentum-aware exit logic (TASK-2026-187 → TASK-2026-199 → momentum upgrade).

    L1 (unchanged): SPX crosses the short strike -> EXIT (hard stop).

    Profit-take (layer 3, between L1 and L2): the spread has decayed to nearly
    worthless — buy it back to lock in the win instead of carrying strike risk
    to expiry. Fires only from a fresh two-sided combo_quote (see
    _profit_take_decision for the full gate list); combo_quote=None (dry-run,
    backtest, cloud, or unconfigured) leaves behavior identical to before.

    L2 (adverse-condition vote): instead of waiting for price to reach the strike,
    count how many independent "market is turning against this position" conditions
    are true, and exit once >= votes_to_exit (default 2) fire. This lets L2 trigger
    while price is still well away from the strike, capping losses on trending days.

    The conditions (each contributes one vote):
      1. trend     — ADX >= adx_floor and (ADX - entry_adx) >= adx_rise, price adverse
      2. vol       — VIX1D >= entry_vix1d * vix1d_mult (intraday move expanding), price adverse
      3. momentum  — PUT: RSI <= rsi_put & falling; CALL: RSI >= rsi_call & rising
      4. proximity — |SPX - short_strike| < entry_em (price near the strike)
      5. near_major— short strike within CURRENT EM of the side's major GEX level
      6. premium   — debit-to-close >= credit * premium_mult (live mark; skipped if None)

    Backward-compat: the OLD L2 (proximity AND near_major) maps exactly onto two
    votes (4 + 5), so positions opened before this upgrade — which lack indicator
    baselines — retain identical protection. Indicator votes simply don't fire when
    their entry baseline is missing.

    Baselines (entry_adx/entry_rsi/entry_vix1d/entry_spx_spot/entry_em) are captured
    at open time (position_store.add_position, sourced from the decision snapshot).
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

    # --- Profit-take (layer 3): premium decayed to near-zero -> lock in the win ---
    # Checked before L2 (a decayed spread can't also be in trouble) and WITHOUT
    # requiring entry_em, so legacy positions lacking baselines still profit-take.
    pt_decision = _profit_take_decision(position, combo_quote, exit_regime)
    if pt_decision is not None:
        return pt_decision

    # --- L2: adverse-condition vote ---

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

    cfg = _exit_cfg()
    is_call = side == "CALL"
    displacement = abs(spx - position.short_strike)

    # Side's major GEX volume level
    if is_call:
        major_level = _num(getattr(combined, "major_positive_by_volume", None))
    else:
        major_level = _num(getattr(combined, "major_negative_by_volume", None))

    # Is price moving against the short side vs entry? (gate for trend/vol/momentum)
    entry_spx = _num(getattr(position, "entry_spx_spot", None))
    if entry_spx is not None:
        adverse_dir = (spx > entry_spx) if is_call else (spx < entry_spx)
    else:
        adverse_dir = True  # no baseline → don't gate (proximity already implies adverse)

    # Current indicators
    adx   = _num(getattr(combined, "adx", None))
    rsi   = _num(getattr(combined, "rsi", None))
    vix1d = _num(getattr(combined, "vix1d", None))

    # Entry baselines (may be None for legacy positions)
    entry_adx   = _num(getattr(position, "entry_adx", None))
    entry_rsi   = _num(getattr(position, "entry_rsi", None))
    entry_vix1d = _num(getattr(position, "entry_vix1d", None))

    votes: list[str] = []

    # 1. trend — ADX trending up through a real-trend floor, price adverse
    if adverse_dir and adx is not None and entry_adx is not None:
        if adx >= cfg["adx_floor"] and (adx - entry_adx) >= cfg["adx_rise"]:
            votes.append(f"trend(adx {entry_adx:.0f}->{adx:.0f})")

    # 2. vol — VIX1D expanding vs entry, price adverse
    if adverse_dir and vix1d and entry_vix1d and entry_vix1d > 0:
        if vix1d >= entry_vix1d * cfg["vix1d_mult"]:
            votes.append(f"vol(vix1d {entry_vix1d:.1f}->{vix1d:.1f})")

    # 3. momentum — RSI pressing against the short side and worse than entry
    if rsi is not None:
        if is_call:
            if rsi >= cfg["rsi_call"] and (entry_rsi is None or rsi > entry_rsi):
                votes.append(f"momentum(rsi {rsi:.0f}>= {cfg['rsi_call']:.0f})")
        else:
            if rsi <= cfg["rsi_put"] and (entry_rsi is None or rsi < entry_rsi):
                votes.append(f"momentum(rsi {rsi:.0f}<= {cfg['rsi_put']:.0f})")

    # 4. proximity — price near the short strike (legacy L2 condition 1)
    proximity = displacement < position.entry_em
    if proximity:
        votes.append(f"proximity(disp {displacement:.1f}<em {position.entry_em:.1f})")

    # 5. near_major — short strike near the side's major GEX level (legacy condition 2)
    near_major = False
    if major_level is not None and major_level != 0.0:
        near_major = abs(position.short_strike - major_level) < em  # CURRENT em
    if near_major:
        votes.append("near_major")

    # 6. premium — live debit-to-close past the stop multiple (skipped if no mark)
    if current_debit is not None and position.credit and position.credit > 0:
        if current_debit >= position.credit * cfg["premium_mult"]:
            votes.append(f"premium(debit {current_debit:.2f}>= {cfg['premium_mult']:.1f}x credit)")

    n = len(votes)
    should_exit = n >= cfg["votes_to_exit"]
    verb = "EXIT" if should_exit else "STAY"
    reason = (
        f"L2 | {verb} | votes={n}/{cfg['votes_to_exit']} [{', '.join(votes) or 'none'}] | "
        f"SPX={spx:.2f} short={position.short_strike:.0f} disp={displacement:.1f}"
    )

    return ExitDecision(
        should_exit=should_exit,
        reason=reason,
        exit_layer=2,
        exit_conditions_met=n,
        exit_regime=exit_regime,
        displacement=displacement,
        near_major=near_major,
        major_level=major_level if major_level is not None else None,
    )


if __name__ == "__main__":
    print(f"Market open: {is_market_open()}")
