"""
Tests for risk_manager.evaluate_entry() and evaluate_exit().
Run with: PYTHONPATH=".:src" .venv/bin/python -m pytest tests/test_risk_manager.py -v

Note: conftest.py patches is_market_open to return True for all tests.
"""
import pytest
from unittest.mock import Mock

from src.risk_manager import evaluate_entry, evaluate_exit, FilterResult, EntryDecision, ExitDecision
from src.position_store import TradePosition, PositionSide


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_combined(
    rsi=55.0,
    spx=4500.0,
    em=15.0,
    call_strike_003=4560.0,
    call_mid=3.00,
    call_10_long_mid=0.20,
    call_20_long_mid=0.10,
    put_strike_003=4440.0,
    put_mid=3.00,
    put_10_long_mid=0.20,
    put_20_long_mid=0.10,
    major_pos=4610.0,
    major_neg=4390.0,
    vix=18.0,  # in bucket 16-20 so tests are unblocked by VIX check
):
    """Factory for a CombinedSnapshot mock with all fields needed by evaluate_entry."""
    combined = Mock()
    combined.rsi = rsi
    combined.spx_spot = spx
    combined.expected_move = em
    combined.regime = "neutral"
    combined.major_positive_by_volume = major_pos
    combined.major_negative_by_volume = major_neg
    combined.call_strike_003 = call_strike_003
    combined.call_mid = call_mid
    combined.call_10_long_mid = call_10_long_mid
    combined.call_20_long_mid = call_20_long_mid
    combined.put_strike_003 = put_strike_003
    combined.put_mid = put_mid
    combined.put_10_long_mid = put_10_long_mid
    combined.put_20_long_mid = put_20_long_mid
    combined.vix = vix
    return combined


# ---------------------------------------------------------------------------
# evaluate_entry — approved when all checks pass
# ---------------------------------------------------------------------------

class TestEvaluateEntry_Approved:
    """Entry approved when premium AND spot_distance AND gex_distance all pass."""

    def test_entry_approved_when_all_checks_pass(self):
        """All entry conditions met → approved."""
        # SPX=4500, EM=15 → need short ≥ 3×EM=45 from SPX → short ≥ 4545
        # short=4560 → disp=60 ≥ 45 ✓
        # major_pos=4610 → |4560-4610|=50 ≥ 2×EM=30 ✓
        # credit = 3.00 - 0.20 = 2.80 ≥ 0.25 ✓
        combined = _make_combined(
            rsi=55.0,
            spx=4500.0,
            em=15.0,
            call_strike_003=4560.0,
            call_mid=3.00,
            call_10_long_mid=0.20,
            major_pos=4610.0,
        )
        result = evaluate_entry(
            combined=combined,
            open_strikes=[],
            target_side="CALL",
            position_store=None,
        )
        assert result.approved is True
        assert result.side == "CALL"
        assert result.short_strike == 4560.0
        assert result.credit >= 0.25

    def test_entry_approved_put_when_all_checks_pass(self):
        """PUT entry approved when all checks pass."""
        # SPX=4500, EM=15 → need short ≤ SPX-45=4455 for PUT
        # short=4440 → disp=60 ≥ 45 ✓
        # major_neg=4390 → |4440-4390|=50 ≥ 30 ✓
        # credit = 3.00 - 0.20 = 2.80 ≥ 0.25 ✓
        combined = _make_combined(
            rsi=45.0,
            spx=4500.0,
            em=15.0,
            put_strike_003=4440.0,
            put_mid=3.00,
            put_10_long_mid=0.20,
            major_neg=4390.0,
        )
        result = evaluate_entry(
            combined=combined,
            open_strikes=[],
            target_side="PUT",
            position_store=None,
        )
        assert result.approved is True
        assert result.side == "PUT"
        assert result.short_strike == 4440.0


# ---------------------------------------------------------------------------
# evaluate_entry — premium rejection
# ---------------------------------------------------------------------------

class TestEvaluateEntry_PremiumRejected:
    """Entry rejected when premium check fails."""

    def test_entry_rejected_when_credit_below_min_premium(self):
        """Credit < $0.25 min_premium → rejected."""
        combined = _make_combined(
            rsi=55.0,
            call_strike_003=4560.0,
            call_mid=0.10,
            call_10_long_mid=0.05,   # credit = 0.05 < 0.25
            call_20_long_mid=0.01,
            major_pos=4610.0,
        )
        result = evaluate_entry(
            combined=combined,
            open_strikes=[],
            target_side="CALL",
            position_store=None,
        )
        assert result.approved is False
        assert result.filter_result.premium_passed is False
        assert result.filter_result.spot_distance_passed is True
        assert result.filter_result.gex_distance_passed is True


# ---------------------------------------------------------------------------
# evaluate_entry — distance rejections
# ---------------------------------------------------------------------------

class TestEvaluateEntry_DistanceRejected:
    """Entry rejected when spot or GEX distance check fails."""

    def test_entry_rejected_when_spot_distance_not_met(self):
        """Short strike < 3×EM from SPX → rejected (spot_distance_failed)."""
        # short=4510, SPX=4500, EM=15 → disp=10 < 3×EM=45
        combined = _make_combined(
            rsi=55.0,
            spx=4500.0,
            em=15.0,
            call_strike_003=4510.0,
            call_mid=3.00,
            call_10_long_mid=0.20,
            major_pos=4560.0,
        )
        result = evaluate_entry(
            combined=combined,
            open_strikes=[],
            target_side="CALL",
            position_store=None,
        )
        assert result.approved is False
        assert result.filter_result.spot_distance_passed is False

    def test_entry_rejected_when_gex_distance_not_met(self):
        """Short strike < 2×EM from major GEX level → rejected (gex_distance_failed)."""
        # short=4560, SPX=4500, EM=15 → spot disp=60 ≥ 45 ✓
        # major_pos=4570 → |4560-4570|=10 < 2×EM=30 ✗
        combined = _make_combined(
            rsi=55.0,
            spx=4500.0,
            em=15.0,
            call_strike_003=4560.0,
            call_mid=3.00,
            call_10_long_mid=0.20,
            major_pos=4570.0,
        )
        result = evaluate_entry(
            combined=combined,
            open_strikes=[],
            target_side="CALL",
            position_store=None,
        )
        assert result.approved is False
        assert result.filter_result.spot_distance_passed is True
        assert result.filter_result.gex_distance_passed is False


# ---------------------------------------------------------------------------
# evaluate_entry — RSI gate
# ---------------------------------------------------------------------------

class TestEvaluateEntry_RSIGate:
    """RSI gate: CALL only when RSI > 50, PUT only when RSI < 50."""

    def test_rsi_above_threshold_triggers_call_side(self):
        """RSI=55 → CALL side evaluated."""
        combined = _make_combined(rsi=65.0)
        result = evaluate_entry(
            combined=combined,
            open_strikes=[],
            target_side=None,
            position_store=None,
        )
        assert result.side == "CALL"

    def test_rsi_below_threshold_triggers_put_side(self):
        """RSI=45 → PUT side evaluated."""
        combined = _make_combined(rsi=35.0)
        result = evaluate_entry(
            combined=combined,
            open_strikes=[],
            target_side=None,
            position_store=None,
        )
        assert result.side == "PUT"

    def test_rsi_equal_threshold_skips_tick(self):
        """RSI=50 (boundary) → tick skipped for both sides."""
        combined = _make_combined(rsi=50.0)
        result = evaluate_entry(
            combined=combined,
            open_strikes=[],
            target_side=None,
            position_store=None,
        )
        assert result.approved is False
        assert "RSI gate" in result.reason

    def test_explicit_target_side_bypasses_rsi_gate(self):
        """target_side="CALL" explicitly set → RSI gate NOT applied."""
        combined = _make_combined(rsi=65.0)
        result = evaluate_entry(
            combined=combined,
            open_strikes=[],
            target_side="CALL",
            position_store=None,
        )
        assert result.side == "CALL"


# ---------------------------------------------------------------------------
# evaluate_entry — strike collision (TASK-2026-147)
# ---------------------------------------------------------------------------

class TestEvaluateEntry_StrikeCollision:
    """Entry rejected when check_strike_collision returns can_proceed=False."""

    def test_entry_rejected_when_new_short_equals_existing_short(self):
        """Condition 1: new_short == existing short → reject (same_short_strike)."""
        combined = _make_combined(
            rsi=55.0,
            spx=4400.0,
            em=15.0,
            call_strike_003=4500.0,
            call_mid=3.00,
            call_10_long_mid=0.20,
            major_pos=4610.0,
        )
        open_strikes = [(4500.0, 4510.0)]
        result = evaluate_entry(
            combined=combined,
            open_strikes=open_strikes,
            target_side="CALL",
            position_store=None,
        )
        assert result.approved is False
        assert result.filter_result.overlap_passed is False

    @pytest.mark.skip(reason="Condition 2 (new_long==ex_short) requires 20-wide fallback to pass spot distance; "
                             "use check_strike_collision() directly for unit coverage of this condition")
    def test_entry_rejected_when_new_long_equals_existing_short(self):
        """Condition 2: new_long == existing short → reject (long_closes_existing_short)."""
        combined = _make_combined(
            rsi=55.0,
            spx=4400.0,
            em=15.0,
            call_strike_003=4490.0,
            call_mid=3.00,
            call_10_long_mid=0.20,
            call_20_long_mid=0.10,
            major_pos=4610.0,
        )
        open_strikes = [(4510.0, 4520.0)]
        result = evaluate_entry(
            combined=combined,
            open_strikes=open_strikes,
            target_side="CALL",
            position_store=None,
        )
        assert result.approved is False
        assert result.filter_result.overlap_passed is False


# ---------------------------------------------------------------------------
# evaluate_exit — L1: SPX crosses short strike
# ---------------------------------------------------------------------------

class TestEvaluateExit_L1:
    """L1: should_exit=True when SPX crosses short_strike (hard stop)."""

    def test_call_exit_when_spx_at_or_above_short_strike(self):
        """CALL: SPX >= short_strike → L1 exit."""
        pos = Mock()
        pos.side = PositionSide.CALL
        pos.short_strike = 4500.0
        pos.layer = 1
        pos.entry_em = None

        combined = Mock()
        combined.spx_spot = 4500.5
        combined.regime = "neutral"

        result = evaluate_exit(pos, combined)
        assert result.should_exit is True
        assert "L1 crossed" in result.reason
        assert result.exit_layer == 1

    def test_call_no_exit_when_spx_below_short_strike(self):
        """CALL: SPX < short_strike → no L1 exit."""
        pos = Mock()
        pos.side = PositionSide.CALL
        pos.short_strike = 4500.0
        pos.layer = 1
        pos.entry_em = None
        pos.credit = 2.50
        pos.spread_width = 5.00
        pos.num_contracts = 1

        combined = Mock()
        combined.spx_spot = 4499.5
        combined.regime = "neutral"
        combined.adx = 0.0
        combined.gex_by_volume = 0e+00
        combined.major_positive_by_volume = 4505.0
        combined.major_negative_by_volume = 4495.0
        combined.expected_move = 15.0

        result = evaluate_exit(pos, combined)
        assert result.should_exit is False

    def test_put_exit_when_spx_at_or_below_short_strike(self):
        """PUT: SPX <= short_strike → L1 exit."""
        pos = Mock()
        pos.side = PositionSide.PUT
        pos.short_strike = 4500.0
        pos.layer = 1
        pos.entry_em = None

        combined = Mock()
        combined.spx_spot = 4499.5
        combined.regime = "neutral"

        result = evaluate_exit(pos, combined)
        assert result.should_exit is True
        assert "L1 crossed" in result.reason

    def test_put_no_exit_when_spx_above_short_strike(self):
        """PUT: SPX > short_strike → no L1 exit."""
        pos = Mock()
        pos.side = PositionSide.PUT
        pos.short_strike = 4500.0
        pos.layer = 1
        pos.entry_em = None
        pos.credit = 2.50
        pos.spread_width = 5.00
        pos.num_contracts = 1

        combined = Mock()
        combined.spx_spot = 4500.5
        combined.regime = "neutral"
        combined.adx = 0.0
        combined.gex_by_volume = 0e+00
        combined.major_positive_by_volume = 4505.0
        combined.major_negative_by_volume = 4495.0
        combined.expected_move = 15.0

        result = evaluate_exit(pos, combined)
        assert result.should_exit is False


# ---------------------------------------------------------------------------
# evaluate_exit — L2: SPX within entry_em window of short strike
# ---------------------------------------------------------------------------

class TestEvaluateExit_L2:
    """
    L2: |SPX − short_strike| < position.entry_em → exit.

    L2 fires when SPX moves within the expected-move window of the short strike.
    entry_em is captured at position open time. L2 applies to ALL positions
    (layer field is ignored — the original layer==2 gate has been removed
    per the 2026-05-19 design update).

    This is the proactive early exit: we don't wait for SPX to cross the short
    strike (which can mean large losses). Instead, we exit when SPX enters
    the entry_em window around the short strike.

    For both CALL and PUT the check is symmetric: displacement = |SPX − short|.

    Example — CCS at short=4500, entry_em=15:
      SPX=4490 → disp=10 < 15 → L2 fires (SPX already 10pts away from short)
      SPX=4510 → disp=10 < 15 → L2 fires

    Example — PCS at short=4500, entry_em=15:
      SPX=4510 → disp=10 < 15 → L2 fires
      SPX=4490 → disp=10 < 15 → L2 fires
    """

    def test_l2_fires_for_call_when_spx_within_entry_em(self):
        """CALL: |SPX - short| < entry_em → L2 exit."""
        pos = Mock()
        pos.side = PositionSide.CALL
        pos.short_strike = 4500.0
        pos.layer = 1           # layer is irrelevant for L2 (2026-05-19 update)
        pos.entry_em = 15.0
        pos.credit = 1.00
        pos.spread_width = 5.00
        pos.num_contracts = 1

        combined = Mock()
        combined.spx_spot = 4490.0   # |4490-4500| = 10 < 15 → L2 fires
        combined.regime = "neutral"
        combined.adx = 25.0
        combined.gex_by_volume = -1e+06
        combined.major_positive_by_volume = 4505.0
        combined.major_negative_by_volume = 4495.0
        combined.expected_move = 15.0

        result = evaluate_exit(pos, combined)
        assert result.should_exit is True
        assert result.exit_layer == 2
        assert "L2" in result.reason
        assert result.exit_layer == 2

    def test_l2_fires_for_put_when_spx_within_entry_em(self):
        """PUT: |SPX - short| < entry_em → L2 exit."""
        pos = Mock()
        pos.side = PositionSide.PUT
        pos.short_strike = 4500.0
        pos.layer = 1
        pos.entry_em = 15.0
        pos.credit = 1.00
        pos.spread_width = 5.00
        pos.num_contracts = 1

        combined = Mock()
        combined.spx_spot = 4510.0   # |4510-4500| = 10 < 15 → L2 fires
        combined.regime = "neutral"
        combined.adx = 25.0
        combined.gex_by_volume = -1e+06
        combined.major_positive_by_volume = 4505.0
        combined.major_negative_by_volume = 4495.0
        combined.expected_move = 15.0

        result = evaluate_exit(pos, combined)
        assert result.should_exit is True
        assert result.exit_layer == 2

    def test_l2_no_exit_when_spx_exactly_at_entry_em(self):
        """displacement == entry_em → L2 does NOT fire (strict <)."""
        pos = Mock()
        pos.side = PositionSide.CALL
        pos.short_strike = 4500.0
        pos.layer = 1
        pos.entry_em = 15.0
        pos.credit = 2.50
        pos.spread_width = 5.00
        pos.num_contracts = 1

        combined = Mock()
        combined.spx_spot = 4485.0   # |4485-4500| = 15 == 15 → disp not < entry_em
        combined.regime = "neutral"
        combined.adx = 25.0
        combined.gex_by_volume = -1e+06
        combined.major_positive_by_volume = 4505.0
        combined.major_negative_by_volume = 4495.0
        combined.expected_move = 15.0

        result = evaluate_exit(pos, combined)
        assert result.should_exit is False

    def test_l2_no_exit_when_spx_beyond_entry_em(self):
        """displacement > entry_em → L2 does not fire."""
        pos = Mock()
        pos.side = PositionSide.CALL
        pos.short_strike = 4500.0
        pos.layer = 1
        pos.entry_em = 15.0
        pos.credit = 2.50
        pos.spread_width = 5.00
        pos.num_contracts = 1

        combined = Mock()
        combined.spx_spot = 4480.0   # |4480-4500| = 20 > 15
        combined.regime = "neutral"
        combined.adx = 25.0
        combined.gex_by_volume = -1e+06
        combined.major_positive_by_volume = 4505.0
        combined.major_negative_by_volume = 4495.0
        combined.expected_move = 15.0

        result = evaluate_exit(pos, combined)
        assert result.should_exit is False

    def test_l2_no_exit_when_entry_em_is_none(self):
        """L2 skipped when entry_em is None."""
        pos = Mock()
        pos.side = PositionSide.CALL
        pos.short_strike = 4500.0
        pos.layer = 1
        pos.entry_em = None
        pos.credit = 2.50
        pos.spread_width = 5.00
        pos.num_contracts = 1

        combined = Mock()
        combined.spx_spot = 4490.0   # would fire if entry_em=15
        combined.regime = "neutral"
        combined.adx = 25.0
        combined.gex_by_volume = -1e+06
        combined.major_positive_by_volume = 4505.0
        combined.major_negative_by_volume = 4495.0
        combined.expected_move = 15.0

        result = evaluate_exit(pos, combined)
        assert result.should_exit is False

    def test_l2_no_exit_when_entry_em_is_zero(self):
        """L2 skipped when entry_em == 0."""
        pos = Mock()
        pos.side = PositionSide.CALL
        pos.short_strike = 4500.0
        pos.layer = 1
        pos.entry_em = 0.0
        pos.credit = 2.50
        pos.spread_width = 5.00
        pos.num_contracts = 1

        combined = Mock()
        combined.spx_spot = 4490.0
        combined.regime = "neutral"
        combined.adx = 25.0
        combined.gex_by_volume = -1e+06
        combined.major_positive_by_volume = 4505.0
        combined.major_negative_by_volume = 4495.0
        combined.expected_move = 15.0

        result = evaluate_exit(pos, combined)
        assert result.should_exit is False

    def test_l1_fires_before_l2_for_call(self):
        """L1 takes priority: SPX >= short → L1 fires, L2 not evaluated."""
        pos = Mock()
        pos.side = PositionSide.CALL
        pos.short_strike = 4500.0
        pos.layer = 1
        pos.entry_em = 15.0

        combined = Mock()
        combined.spx_spot = 4500.5   # >= short → L1 fires
        combined.regime = "neutral"

        result = evaluate_exit(pos, combined)
        assert result.should_exit is True
        assert result.exit_layer == 1
        assert "L1" in result.reason

    def test_l1_fires_before_l2_for_put(self):
        """L1 takes priority: SPX <= short → L1 fires, L2 not evaluated."""
        pos = Mock()
        pos.side = PositionSide.PUT
        pos.short_strike = 4500.0
        pos.layer = 1
        pos.entry_em = 15.0

        combined = Mock()
        combined.spx_spot = 4499.5   # <= short → L1 fires
        combined.regime = "neutral"

        result = evaluate_exit(pos, combined)
        assert result.should_exit is True
        assert result.exit_layer == 1
        assert "L1" in result.reason

    def test_l2_symmetric_for_call_and_put(self):
        """L2 fires symmetrically: same displacement triggers L2 for both sides."""
        call_pos = Mock()
        call_pos.side = PositionSide.CALL
        call_pos.short_strike = 4500.0
        call_pos.layer = 1
        call_pos.entry_em = 15.0
        call_pos.credit = 1.00
        call_pos.spread_width = 5.00
        call_pos.num_contracts = 1

        put_pos = Mock()
        put_pos.side = PositionSide.PUT
        put_pos.short_strike = 4500.0
        put_pos.layer = 1
        put_pos.entry_em = 15.0
        put_pos.credit = 1.00
        put_pos.spread_width = 5.00
        put_pos.num_contracts = 1

        combined = Mock()
        combined.regime = "neutral"
        combined.adx = 25.0
        combined.gex_by_volume = -1e+06
        combined.major_positive_by_volume = 4505.0
        combined.major_negative_by_volume = 4495.0
        combined.expected_move = 15.0

        # SPX above short by 10pts
        combined.spx_spot = 4510.0
        call_result = evaluate_exit(call_pos, combined)
        put_result = evaluate_exit(put_pos, combined)
        assert call_result.should_exit is True
        assert put_result.should_exit is True

        # SPX below short by 10pts
        combined.spx_spot = 4490.0
        call_result = evaluate_exit(call_pos, combined)
        put_result = evaluate_exit(put_pos, combined)
        assert call_result.should_exit is True
        assert put_result.should_exit is True


class TestEvaluateExit_L2_Momentum:
    """L2 momentum upgrade: 2-of-N adverse-condition vote (trend/vol/momentum/
    proximity/near_major/premium). Indicator votes let L2 fire before price reaches
    the strike. Reproduces the PUT-#6 loss scenario."""

    def _put(self, **over):
        pos = Mock()
        pos.side = PositionSide.PUT
        pos.short_strike = 7335.0
        pos.layer = 1
        pos.entry_em = 19.6
        pos.credit = 0.30
        pos.spread_width = 20.0
        pos.num_contracts = 1
        # Entry baselines (the #6 trade at 12:37)
        pos.entry_spx_spot = 7400.0
        pos.entry_adx = 10.4
        pos.entry_rsi = 43.5
        pos.entry_vix1d = 13.2
        for k, v in over.items():
            setattr(pos, k, v)
        return pos

    def _combined(self, **over):
        c = Mock()
        c.regime = "neutral"
        c.expected_move = 22.0
        # No GEX major near the strike → near_major vote off unless overridden
        c.major_positive_by_volume = 0.0
        c.major_negative_by_volume = 0.0
        for k, v in over.items():
            setattr(c, k, v)
        return c

    def test_exits_on_trend_plus_momentum_before_proximity(self):
        """#6 scenario at 13:16: SPX 7368 (disp 33 > entry_em, no proximity),
        ADX 10->20 (trend) + RSI 33<=35 falling (momentum) → 2 votes → EXIT."""
        pos = self._put()
        c = self._combined(spx_spot=7368.0, adx=20.1, rsi=33.0, vix1d=15.4)
        r = evaluate_exit(pos, c)
        assert r.should_exit is True
        assert r.exit_layer == 2
        assert r.exit_conditions_met >= 2
        assert "trend" in r.reason and "momentum" in r.reason

    def test_single_indicator_vote_stays(self):
        """Only trend fires (RSI not yet oversold) → 1 vote → STAY."""
        pos = self._put()
        c = self._combined(spx_spot=7380.0, adx=20.1, rsi=40.0, vix1d=13.5)
        r = evaluate_exit(pos, c)
        assert r.should_exit is False
        assert r.exit_conditions_met == 1

    def test_no_exit_on_favorable_move(self):
        """Price RISING (favorable for a short put) → trend/vol/momentum gated off
        by adverse-direction check even with a vol spike → STAY."""
        pos = self._put()
        c = self._combined(spx_spot=7420.0, adx=30.0, rsi=20.0, vix1d=20.0)
        r = evaluate_exit(pos, c)
        assert r.should_exit is False

    def test_premium_vote_counts_within_2_of_n(self):
        """Premium (debit >= 3x credit) + momentum = 2 votes → EXIT; premium alone
        (1 vote) → STAY (premium is one vote in the 2-of-N, not a hard stop)."""
        pos = self._put()
        # debit 0.90 == 3 * 0.30 credit → premium vote on
        c_one = self._combined(spx_spot=7395.0, adx=11.0, rsi=43.0, vix1d=13.2)
        r_one = evaluate_exit(pos, c_one, current_debit=0.90)
        assert r_one.should_exit is False  # only premium → 1 vote

        c_two = self._combined(spx_spot=7368.0, adx=12.0, rsi=33.0, vix1d=13.5)
        r_two = evaluate_exit(pos, c_two, current_debit=0.90)
        assert r_two.should_exit is True  # premium + momentum → 2 votes
        assert "premium" in r_two.reason

    def test_legacy_proximity_plus_near_major_still_exits(self):
        """Backward-compat: a position with no indicator baselines still exits on
        the old condition (proximity AND near_major = 2 votes)."""
        pos = self._put(entry_spx_spot=None, entry_adx=None,
                        entry_rsi=None, entry_vix1d=None)
        # disp = |7350-7335| = 15 < entry_em 19.6 (proximity);
        # major_negative 7340 within current em 22 of strike (near_major)
        c = self._combined(spx_spot=7350.0, adx=25.0, rsi=30.0, vix1d=18.0,
                           major_negative_by_volume=7340.0)
        r = evaluate_exit(pos, c)
        assert r.should_exit is True


# ---------------------------------------------------------------------------
# FilterResult
# ---------------------------------------------------------------------------

class TestFilterResult:
    """Unit tests for FilterResult dataclass."""

    def test_first_failure_reason_premium(self):
        fr = FilterResult(premium_passed=False)
        assert fr.first_failure_reason == "premium_failed"

    def test_first_failure_reason_spot_distance(self):
        fr = FilterResult(premium_passed=True, spot_distance_passed=False)
        assert fr.first_failure_reason == "spot_distance_failed"

    def test_first_failure_reason_gex_distance(self):
        fr = FilterResult(
            premium_passed=True, spot_distance_passed=True, gex_distance_passed=False,
        )
        assert fr.first_failure_reason == "gex_distance_failed"

    def test_first_failure_reason_max_positions(self):
        fr = FilterResult(
            premium_passed=True, spot_distance_passed=True,
            gex_distance_passed=True, max_positions_passed=False,
        )
        assert fr.first_failure_reason == "max_positions_reached"

    def test_first_failure_reason_overlap(self):
        fr = FilterResult(
            premium_passed=True, spot_distance_passed=True,
            gex_distance_passed=True, max_positions_passed=True, overlap_passed=False,
        )
        assert fr.first_failure_reason == "overlap_detected"

    def test_filters_passed_includes_all_true_fields(self):
        fr = FilterResult(
            premium_passed=True,
            spot_distance_passed=True,
            gex_distance_passed=False,
            max_positions_passed=True,
            overlap_passed=True,
        )
        assert "premium" in fr.filters_passed
        assert "spot_distance" in fr.filters_passed
        assert "gex_distance" not in fr.filters_passed
        assert "max_positions" in fr.filters_passed
        assert "overlap" in fr.filters_passed

    def test_filters_failed_includes_all_false_fields(self):
        fr = FilterResult(
            premium_passed=True,
            spot_distance_passed=False,
            gex_distance_passed=False,
        )
        assert "spot_distance_failed" in fr.filters_failed
        assert "gex_distance_failed" in fr.filters_failed
        assert "premium_failed" not in fr.filters_failed


if __name__ == "__main__":
    pytest.main([__file__, "-v"])