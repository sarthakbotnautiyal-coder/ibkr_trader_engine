"""
schema.py — Shared column lists for signals and positions tables.

Single source of truth for column names used across both pipelines.
Both trades_db.py and backtest_db.py import from here.

Signal columns:
  Used by:  trades_db.insert_signal(),  backtest_db.insert_backtest_signal()
  Live:     signals table  (positions.db)
  Backtest: backtest_signals table (backtest.db)

Position columns:
  Used by:  trades_db.insert_position(),  backtest_db.insert_backtest_position()
  Live:     positions table  (positions.db)
  Backtest: backtest_positions table (backtest.db)

TASK-2026-174: Unified schema between positions.db and backtest.db.
  - Both use total_credit (net = credit - debit) as single field
  - Backtest positions has all exit-tracking columns that live positions has
  - Backtest signals has decision column like live signals
"""

# ---------------------------------------------------------------------------
# Signal columns
# ---------------------------------------------------------------------------

# Shared signal columns — present in both live signals and backtest_signals
SIGNAL_COMMON_COLS = [
    "layer",
    "spx_spot",
    "em",
    "gex",
    "vix",
    "rsi",
    "signalled",
    "signal_reason",
    "premium_passed",
    "distance_passed",
    "collision_passed",
    "filled",
    "short_strike",
    "long_strike",
    "credit",
    "blocked_reason",
    "action",
]

# Live signals table unique columns (joined to common)
SIGNAL_LIVE_UNIQUE = [
    "timestamp",
    "task_id",
]

# Backtest signals table unique columns (joined to common)
# "decision" is the JSON-serialized EntryDecision | ExitDecision
SIGNAL_BACKTEST_UNIQUE = [
    "backtest_run_id",
    "ts",
    "side",
    "decision",
]

# Full column lists per table
SIGNAL_LIVE_COLS = SIGNAL_LIVE_UNIQUE + SIGNAL_COMMON_COLS
SIGNAL_BACKTEST_COLS = SIGNAL_BACKTEST_UNIQUE + SIGNAL_COMMON_COLS

# ---------------------------------------------------------------------------
# Position columns
# ---------------------------------------------------------------------------

# Shared position columns — present in both live positions and backtest_positions
# NOTE: live positions uses total_credit as net figure (credit × 100 × contracts).
# Backtest uses the same convention via backtest_db.insert_backtest_position.
POSITION_COMMON_COLS = [
    # Core
    "side",
    "short_strike",
    "long_strike",
    "credit",
    "spread_width",
    "layer",
    "status",
    # Contracts & net credit
    "contracts",
    "total_credit",          # net credit (credit × 100 × contracts)
    # Entry indicators
    "entry_spx_spot",
    "entry_em",
    "entry_gex_by_volume",
    "entry_bb_position",
    "entry_bb_expanding",
    "entry_adx",
    "entry_macd_hist",
    "entry_rsi",
    "entry_atm_call_mid",
    "entry_atm_put_mid",
    "entry_atm_strike",
    # Entry regime
    "entry_regime",
    "entry_major_positive_by_volume",
    "entry_zero_gamma",
    # Exit indicators
    "exit_spx_spot",
    "exit_em",
    "exit_gex_by_volume",
    "exit_bb_position",
    "exit_bb_expanding",
    "exit_adx",
    "exit_macd_hist",
    "exit_rsi",
    "exit_regime",
]

# Live positions table unique columns (joined to common)
POSITION_LIVE_UNIQUE = [
    "id",
    "task_id",
    "ticker",
    "open_time",
    "close_time",
    "debit",
    "pnl",
    "max_profit",
    "max_loss",
    "notes",
    "num_contracts",
    "entry_zero_gamma_dist",
]

# Backtest positions table unique columns (joined to common)
POSITION_BACKTEST_UNIQUE = [
    "id",
    "backtest_run_id",
    "entry_ts",
    "exit_ts",
    "pnl",
    "exit_reason",
]

# Full column lists per table
POSITION_LIVE_COLS = POSITION_LIVE_UNIQUE + POSITION_COMMON_COLS
POSITION_BACKTEST_COLS = POSITION_BACKTEST_UNIQUE + POSITION_COMMON_COLS