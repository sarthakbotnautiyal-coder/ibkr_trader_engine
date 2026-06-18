"""
Tests for check_strike_collision() — TASK-2026-147 strike collision logic.
Run with: PYTHONPATH=".:src" .venv/bin/python -m pytest tests/test_strike_collision.py -v
"""
import pytest

from src.position_store import check_strike_collision


class TestCondition1_SameShortStrike:
    """Condition 1: new_short == existing short → reject."""

    def test_reject_exact_short_match(self):
        """Same short strike on same side — must reject."""
        can_proceed, reason = check_strike_collision(
            new_short=4500.0,
            new_long=4510.0,
            open_strikes=[(4500.0, 4510.0)],
        )
        assert can_proceed is False
        assert reason == "same_short_strike"

    def test_reject_same_short_different_long(self):
        """Same short strike, different long — still reject."""
        can_proceed, reason = check_strike_collision(
            new_short=4500.0,
            new_long=4520.0,
            open_strikes=[(4500.0, 4510.0)],
        )
        assert can_proceed is False
        assert reason == "same_short_strike"

    def test_allow_different_short_completely_separate(self):
        """Different short strike with no overlap — allowed."""
        # Existing: (4450, 4460) — far from new (4510, 4520)
        can_proceed, reason = check_strike_collision(
            new_short=4510.0,
            new_long=4520.0,
            open_strikes=[(4450.0, 4460.0)],
        )
        assert can_proceed is True
        assert reason == ""

    def test_allow_new_short_below_existing_short(self):
        """New short below existing short, no other collision — allowed."""
        # Existing: (4500, 4520), new short=4490, new long=4510
        # new_short(4490) != ex_short(4500)
        # new_long(4510) != ex_short(4500)
        # new_short(4490) != ex_long(4520)
        can_proceed, reason = check_strike_collision(
            new_short=4490.0,
            new_long=4510.0,
            open_strikes=[(4500.0, 4520.0)],
        )
        assert can_proceed is True
        assert reason == ""


class TestCondition2_LongClosesExistingShort:
    """Condition 2: new_long == existing short → reject."""

    def test_reject_long_equals_existing_short(self):
        """New long leg matches existing short — would close position, reject."""
        can_proceed, reason = check_strike_collision(
            new_short=4500.0,
            new_long=4510.0,
            open_strikes=[(4510.0, 4520.0)],
        )
        assert can_proceed is False
        assert reason == "long_closes_existing_short"

    def test_allow_long_above_existing_short(self):
        """New long above existing short — condition 2 does not trigger."""
        can_proceed, reason = check_strike_collision(
            new_short=4500.0,
            new_long=4530.0,
            open_strikes=[(4510.0, 4520.0)],
        )
        assert can_proceed is True
        assert reason == ""


class TestCondition3_NewShortEqualsExistingLong:
    """Condition 3: new_short == existing long → reject (same_short_strike).

    This is the inverse of Condition 2 — placing a new short at the existing
    long strike would neutralize the existing position.
    """

    def test_reject_new_short_equals_existing_long_call(self):
        """New CALL short == existing CALL long → reject."""
        # Existing: CALL 4500 short, 4510 long → long = 4510
        # New: CALL 4510 short, 4520 long → short = 4510 == existing long
        can_proceed, reason = check_strike_collision(
            new_short=4510.0,
            new_long=4520.0,
            open_strikes=[(4500.0, 4510.0)],
        )
        assert can_proceed is False
        assert reason == "same_short_strike"

    def test_reject_new_short_equals_existing_long_put(self):
        """New PUT short == existing PUT long → reject."""
        # Existing: PUT 4500 short, 4490 long → long = 4490
        # New: PUT 4490 short, 4480 long → short = 4490 == existing long
        can_proceed, reason = check_strike_collision(
            new_short=4490.0,
            new_long=4480.0,
            open_strikes=[(4500.0, 4490.0)],
        )
        assert can_proceed is False
        assert reason == "same_short_strike"

    def test_allow_new_short_below_existing_long(self):
        """New short below existing long — condition 3 does not trigger."""
        # Existing: CALL 4500 short, 4520 long → long = 4520
        # New: CALL 4510 short, 4530 long → short = 4510 < existing long
        can_proceed, reason = check_strike_collision(
            new_short=4510.0,
            new_long=4530.0,
            open_strikes=[(4500.0, 4520.0)],
        )
        assert can_proceed is True
        assert reason == ""

    def test_allow_new_short_above_existing_long(self):
        """New short above existing long — condition 3 does not trigger."""
        # Existing: CALL 4500 short, 4510 long → long = 4510
        # New: CALL 4520 short, 4530 long → short = 4520 > existing long
        can_proceed, reason = check_strike_collision(
            new_short=4520.0,
            new_long=4530.0,
            open_strikes=[(4500.0, 4510.0)],
        )
        assert can_proceed is True
        assert reason == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])