"""
telegram_notifier.py — Telegram notifications for IBKR auto-trader.

DRY mode: controlled by config["telegram"]["dry_run"].
  - true  → log to console + engine.log only, no Telegram API call
  - false → send to Telegram channel

No third-party deps — stdlib urllib only.

TASK-2026-179: All Telegram notifications (entry, exit, rejection, timeout)
fire ONLY after confirmed fill via polling in LIVE mode.

TASK-2026-210: Only notify_entry() and notify_exit() send Telegram.
notify_rejection(), notify_timeout(), and order cancellation in engine.py
log only — no Telegram messages.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

_SELF_DIR = Path(__file__).parent
_SRC_DIR   = _SELF_DIR.parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))

# ---------------------------------------------------------------------------
# Config — resolved at import time, but can be overridden by env at runtime
# ---------------------------------------------------------------------------

_TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_SIGNALS_CHAT_ID", "-1003979595328")

# Lazy-load logger to avoid circular import with engine.py
_logger: logging.Logger | None = None

def _get_logger() -> logging.Logger:
    global _logger
    if _logger is None:
        _logger = logging.getLogger("telegram_notifier")
        if not _logger.handlers:
            _logger.setLevel(logging.INFO)
    return _logger

def _dry_run() -> bool:
    """Check DRY mode from config."""
    try:
        from config import CONFIG
        return CONFIG.get("telegram", {}).get("dry_run", True)
    except Exception:
        return True  # safe default: no sends until config is present


# ---------------------------------------------------------------------------
# Core send
# ---------------------------------------------------------------------------

def send_telegram_message(text: str) -> bool:
    """
    Send a message to the configured Telegram channel.

    Returns True on success, False on failure.
    In DRY mode, logs the message but does NOT call the Telegram API.
    """
    dry = _dry_run()
    if dry:
        _get_logger().info(f"[TELEGRAM DRY] {text}")
        return True

    # Resolve token at call time (env vars may have been set after import)
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", _TELEGRAM_BOT_TOKEN)
    chat_id   = os.environ.get("TELEGRAM_SIGNALS_CHAT_ID", _TELEGRAM_CHAT_ID)

    if not bot_token:
        _get_logger().warning("[TELEGRAM] TELEGRAM_BOT_TOKEN not set — skipping send")
        return False
    if not chat_id:
        _get_logger().warning("[TELEGRAM] TELEGRAM_CHAT_ID not set — skipping send")
        return False

    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }).encode("utf-8")

    req = urllib.request.Request(
        api_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            result = json.loads(body)
            if result.get("ok"):
                _get_logger().info(f"[TELEGRAM] Sent: {text[:80]}...")
                return True
            else:
                _get_logger().error(f"[TELEGRAM] API error: {result}")
                return False
    except urllib.error.HTTPError as e:
        _get_logger().error(f"[TELEGRAM] HTTP error {e.code}: {e.reason}")
        return False
    except urllib.error.URLError as e:
        _get_logger().error(f"[TELEGRAM] Network error: {e}")
        return False
    except Exception as e:
        _get_logger().error(f"[TELEGRAM] Unexpected error: {e}")
        return False


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------

def notify_entry(
    side: str,
    short_strike: float,
    long_strike: float,
    credit: float,
    num_contracts: int,
    spx: float,
    entry_em: float,
    fill_price: Optional[float] = None,
    is_live: bool = False,
) -> bool:
    """
    Notify on position open (ENTRY).

    TASK-2026-179: is_live flag distinguishes LIVE vs DRY_RUN mode.
    In LIVE mode, this is called only after confirmed fill via polling.

    Format: 🚀 ENTRY | CALL | 4500/4510 | $2.80 credit | 1 contract | SPX=4500 | EM=15.0
    """
    mode_str = "[LIVE]" if is_live else "[DRY_RUN]"
    text = (
        f"{mode_str} 🚀 ENTRY | {side} | "
        f"{short_strike:.0f}/{long_strike:.0f} | "
        f"${credit:.2f} credit | "
        f"{num_contracts} contract{'s' if num_contracts > 1 else ''} | "
        f"SPX={spx:.0f} | EM={entry_em:.1f}"
    )
    if fill_price is not None:
        text += f" | fill=${fill_price:.2f}"
    return send_telegram_message(text)


def notify_exit(
    side: str,
    short_strike: float,
    long_strike: float,
    num_contracts: int,
    pnl: float,
    reason: str,
    spx: float,
    exit_layer: int = 1,
    is_live: bool = False,
) -> bool:
    """
    Notify on position close (EXIT).

    TASK-2026-179: is_live flag distinguishes LIVE vs DRY_RUN mode.
    In LIVE mode, this is called only after confirmed fill via polling.

    Format: ✅ EXIT | CALL | 4500/4510 | 1 contract | P&L: +$280 | L1 crossed | SPX=4510
    """
    mode_str = "[LIVE]" if is_live else "[DRY_RUN]"
    pnl_str = f"+${pnl:.0f}" if pnl >= 0 else f"-${abs(pnl):.0f}"
    text = (
        f"{mode_str} ✅ EXIT | {side} | "
        f"{short_strike:.0f}/{long_strike:.0f} | "
        f"{num_contracts} contract{'s' if num_contracts > 1 else ''} | "
        f"P&L: {pnl_str} | "
        f"{reason} | "
        f"SPX={spx:.0f} | L{exit_layer}"
    )
    return send_telegram_message(text)


def notify_rejection(
    msg_type: str,
    side: Optional[str] = None,
    short_strike: float = 0,
    long_strike: float = 0,
    reason: str = "",
    pos_db_id: Optional[int] = None,
) -> bool:
    """
    Notify on order rejection / cancellation.

    TASK-2026-179: Called when IBKR reports order inactive/rejected/cancelled.
    In DRY_RUN mode, logs only (not sent to Telegram).

    TASK-2026-210: Log only — no Telegram message sent.

    msg_type: 'entry' | 'exit' | 'cancel'
    """
    if msg_type == "entry":
        text = (
            f"❌ ENTRY REJECTED | {side or '?'} | "
            f"{short_strike:.0f}/{long_strike:.0f} | "
            f"reason={reason}"
        )
    elif msg_type == "exit":
        text = (
            f"⚠️ CLOSE REJECTED | order_id={pos_db_id} | "
            f"reason={reason} | Position may be closed in TWS"
        )
    elif msg_type == "cancel":
        text = (
            f"⚠️ ORDER CANCELLED | {side or '?'} | "
            f"{short_strike:.0f}/{long_strike:.0f}"
        )
    else:
        text = f"❌ REJECTION | {msg_type} | {reason}"

    _get_logger().info(f"[REJECTION] {text}")
    return True


def notify_timeout(
    msg_type: str,
    side: Optional[str] = None,
    short_strike: float = 0,
    long_strike: float = 0,
    pos_db_id: Optional[int] = None,
    note: str = "",
) -> bool:
    """
    Notify on pending order timeout.

    TASK-2026-179: Called when a pending order exceeds PENDING_TIMEOUT_SECONDS.
    Entry timeout: DB row rolled back, position not opened.
    Exit timeout: position stays open, retry on next tick.

    TASK-2026-210: Log only — no Telegram message sent.

    msg_type: 'entry' | 'exit'
    """
    if msg_type == "entry":
        text = (
            f"⏰ ENTRY TIMEOUT | {side or '?'} | "
            f"{short_strike:.0f}/{long_strike:.0f} | "
            f"No fill after 10 min | DB row rolled back"
        )
    elif msg_type == "exit":
        text = (
            f"⏰ EXIT TIMEOUT | pos_db_id={pos_db_id} | "
            f"No fill after 10 min | Position kept open"
        )
    else:
        text = f"⏰ TIMEOUT | {msg_type} | {note}"

    _get_logger().info(f"[TIMEOUT] {text}")
    return True


def notify_day_gate(
    blocked: bool,
    ts: str,
    avg_gex: float,
    avg_dist: float,
    avg_rsi: float,
    signal_1: bool,
    signal_2: bool,
    signal_3: bool,
    n_samples: int,
) -> bool:
    """
    Telegram alert on day-gate state transition (blocked ↔ cleared).
    Sends a real Telegram message (not log-only) so the trader is notified
    immediately when volatile conditions suspend entries.
    """
    s1 = f"GEX-OI={avg_gex:.1f} {'❌' if signal_1 else '✅'}"
    s2 = f"Dist={avg_dist:.1f} {'❌' if signal_2 else '✅'}"
    s3 = f"RSI={avg_rsi:.1f} {'❌' if signal_3 else '✅'}"
    window = f"(n={n_samples} ticks)"

    if blocked:
        text = (
            f"🚨 *DAY GATE BLOCKED* | {ts} ET\n"
            f"New entries suspended — danger signals fired:\n"
            f"  {s1}  |  {s2}  |  {s3}  {window}"
        )
    else:
        text = (
            f"✅ *DAY GATE CLEARED* | {ts} ET\n"
            f"Entries re-enabled — rolling averages recovered:\n"
            f"  {s1}  |  {s2}  |  {s3}  {window}"
        )

    _get_logger().info(f"[DAY_GATE] {text}")
    return send_telegram_message(text)
