"""
data_sources.py — Abstraction layer for switching between LOCAL and CLOUD data sources.

Supports:
  - LOCAL: Read from SQLite databases (gex_extractor, premium_extractor, tradingview)
  - CLOUD: Read from Supabase tables (trading.gex_snapshots, trading.scan_results, trading.spx_standardized)

Backtesting:
  - Only works with LOCAL mode
  - Raises error if CLOUD mode selected and backtesting attempted
"""
import sqlite3
import os
from pathlib import Path
from typing import Optional, Any
from datetime import datetime, timedelta
import logging

from config import CONFIG

_LOG = logging.getLogger(__name__)


class DataSourceError(Exception):
    """Raised when data source is misconfigured or unavailable."""
    pass


def get_data_source_mode() -> str:
    """Return configured data source mode: 'local' or 'cloud'."""
    return CONFIG.get("data_source_mode", "local").lower()


def is_local_mode() -> bool:
    """Check if LOCAL mode is enabled."""
    return get_data_source_mode() == "local"


def is_cloud_mode() -> bool:
    """Check if CLOUD mode is enabled."""
    return get_data_source_mode() == "cloud"


def verify_data_source() -> None:
    """
    Verify data source is properly configured.
    Raises DataSourceError if configuration invalid.
    """
    mode = get_data_source_mode()

    if mode == "local":
        # Verify LOCAL mode paths exist
        gex_db = _get_gex_db_path()
        scanner_db = _get_scanner_db_path()

        if not gex_db.exists():
            raise DataSourceError(f"LOCAL mode: gex.db not found at {gex_db}")
        if not scanner_db.exists():
            raise DataSourceError(f"LOCAL mode: scanner.db not found at {scanner_db}")

        _LOG.info("✓ Data source verified: LOCAL mode (gex.db, scanner.db ready)")

    elif mode == "cloud":
        # Verify CLOUD mode credentials
        app_id = os.getenv("SUPABASE_APP_ID", "").strip()
        if not app_id:
            raise DataSourceError("CLOUD mode requires SUPABASE_APP_ID environment variable in .env")

        supabase_url = os.getenv("SUPABASE_URL", "").strip()
        if not supabase_url:
            raise DataSourceError("CLOUD mode requires SUPABASE_URL environment variable")

        # Try to import supabase client (may not be installed)
        try:
            import supabase
        except ImportError:
            raise DataSourceError(
                "CLOUD mode requires 'supabase' package. "
                "Install with: pip install supabase"
            )

        _LOG.info("✓ Data source verified: CLOUD mode (Supabase ready)")

    else:
        raise DataSourceError(f"Unknown data_source_mode: {mode}")


def check_backtesting_compatibility() -> None:
    """
    Verify backtesting is only attempted with LOCAL mode.
    Raises DataSourceError if CLOUD mode with backtesting.
    """
    is_backtesting = CONFIG.get("backtesting", {}).get("enabled", False)

    if is_backtesting and is_cloud_mode():
        raise DataSourceError(
            "Backtesting only works with LOCAL data source mode. "
            "Set data_source_mode: 'local' in config.yaml"
        )


# ============================================================================
# Path resolution helpers
# ============================================================================

# Engine root (ibkr_trader_engine/). Relative config paths resolve against this,
# matching combined_reader._resolve so both agree on where the sibling DBs live.
_ENGINE_ROOT = Path(__file__).parent.parent


def _resolve(path_str: str) -> Path:
    p = Path(path_str)
    if p.is_absolute():
        return p
    return (_ENGINE_ROOT / p).resolve()


def _get_gex_db_path() -> Path:
    """Resolve gex_db path from config."""
    return _resolve(CONFIG.get("data_sources", {}).get("gex_db", "../gex_extractor/data/gex.db"))


def _get_scanner_db_path() -> Path:
    """Resolve scanner_db path from config."""
    return _resolve(CONFIG.get("data_sources", {}).get("scanner_db", "../premium_extractor/data/scanner.db"))


def _get_tradingview_db_path() -> Path:
    """Resolve tradingview_db path from config."""
    return _resolve(CONFIG.get("data_sources", {}).get("tradingview_db", "../tradingView_signal_generator/data/tradingview.db"))


# ============================================================================
# Supabase client (lazy-loaded for CLOUD mode)
# ============================================================================

_SUPABASE_CLIENT = None


def get_supabase_client():
    """Get or create Supabase client (CLOUD mode only)."""
    global _SUPABASE_CLIENT

    if _SUPABASE_CLIENT is not None:
        return _SUPABASE_CLIENT

    if not is_cloud_mode():
        raise DataSourceError("Supabase client only available in CLOUD mode")

    try:
        from supabase import create_client
    except ImportError:
        raise DataSourceError("Supabase client not installed")

    app_id = os.getenv("SUPABASE_APP_ID", "")
    supabase_url = os.getenv("SUPABASE_URL", "")

    if not app_id or not supabase_url:
        raise DataSourceError("Missing Supabase credentials (SUPABASE_APP_ID and SUPABASE_URL in .env)")

    _SUPABASE_CLIENT = create_client(supabase_url, app_id)
    return _SUPABASE_CLIENT


# ============================================================================
# Data retrieval helpers (switching between LOCAL and CLOUD)
# ============================================================================

def get_gex_snapshots_table(as_of_date: Optional[str] = None, limit: int = 100) -> list:
    """
    Get GEX snapshots.

    Args:
        as_of_date: Filter to specific date (for backtesting). Format: "YYYY-MM-DD"
        limit: Number of rows to return

    Returns:
        List of dict-like objects with gex data
    """
    if is_local_mode():
        return _get_gex_snapshots_local(as_of_date, limit)
    else:
        return _get_gex_snapshots_cloud(as_of_date, limit)


def get_scan_results_table(as_of_date: Optional[str] = None, limit: int = 100) -> list:
    """
    Get scan results.

    Args:
        as_of_date: Filter to specific date (for backtesting). Format: "YYYY-MM-DD"
        limit: Number of rows to return

    Returns:
        List of dict-like objects with scan data
    """
    if is_local_mode():
        return _get_scan_results_local(as_of_date, limit)
    else:
        return _get_scan_results_cloud(as_of_date, limit)


def get_tradingview_fundamentals_table(as_of_date: Optional[str] = None, limit: int = 100) -> list:
    """
    Get TradingView fundamentals.

    Args:
        as_of_date: Filter to specific date (for backtesting). Format: "YYYY-MM-DD"
        limit: Number of rows to return

    Returns:
        List of dict-like objects with TV data
    """
    if is_local_mode():
        return _get_tradingview_fundamentals_local(as_of_date, limit)
    else:
        return _get_tradingview_fundamentals_cloud(as_of_date, limit)


# ============================================================================
# LOCAL mode implementations
# ============================================================================

def _get_gex_snapshots_local(as_of_date: Optional[str] = None, limit: int = 100) -> list:
    """Query gex.db (LOCAL mode)."""
    gex_db = _get_gex_db_path()

    try:
        conn = sqlite3.connect(f"file:{gex_db}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        query = "SELECT * FROM gex_snapshots"
        params = []

        if as_of_date:
            query += " WHERE DATE(snapshot_timestamp) = ?"
            params.append(as_of_date)

        query += " ORDER BY snapshot_timestamp DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()

        return rows
    except Exception as e:
        _LOG.warning(f"Failed to read gex_snapshots from {gex_db}: {e}")
        return []


def _get_scan_results_local(as_of_date: Optional[str] = None, limit: int = 100) -> list:
    """Query scanner.db (LOCAL mode)."""
    scanner_db = _get_scanner_db_path()

    try:
        conn = sqlite3.connect(f"file:{scanner_db}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        query = "SELECT * FROM scan_results"
        params = []

        if as_of_date:
            query += " WHERE DATE(timestamp_est) = ?"
            params.append(as_of_date)

        query += " ORDER BY timestamp_est DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()

        return rows
    except Exception as e:
        _LOG.warning(f"Failed to read scan_results from {scanner_db}: {e}")
        return []


def _get_tradingview_fundamentals_local(as_of_date: Optional[str] = None, limit: int = 100) -> list:
    """Query tradingview.db (LOCAL mode)."""
    tv_db = _get_tradingview_db_path()

    try:
        conn = sqlite3.connect(f"file:{tv_db}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        query = "SELECT * FROM spx_standardized WHERE alert_type = 'fundamentals'"
        params = []

        if as_of_date:
            query += " AND DATE(received_at) = ?"
            params.append(as_of_date)

        query += " ORDER BY received_at DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()

        return rows
    except Exception as e:
        _LOG.warning(f"Failed to read spx_standardized from {tv_db}: {e}")
        return []


# ============================================================================
# CLOUD mode implementations
# ============================================================================

def _get_gex_snapshots_cloud(as_of_date: Optional[str] = None, limit: int = 100) -> list:
    """Query Supabase trading.gex_snapshots (CLOUD mode)."""
    try:
        client = get_supabase_client()
        query = client.table("gex_snapshots").select("*")

        if as_of_date:
            query = query.gte("snapshot_timestamp", f"{as_of_date}T00:00:00").lt("snapshot_timestamp", f"{as_of_date}T23:59:59")

        query = query.order("snapshot_timestamp", desc=True).limit(limit)
        response = query.execute()

        return response.data if response.data else []
    except Exception as e:
        _LOG.warning(f"Failed to read gex_snapshots from Supabase: {e}")
        return []


def _get_scan_results_cloud(as_of_date: Optional[str] = None, limit: int = 100) -> list:
    """Query Supabase trading.scan_results (CLOUD mode)."""
    try:
        client = get_supabase_client()
        query = client.table("scan_results").select("*")

        if as_of_date:
            query = query.gte("timestamp_est", f"{as_of_date}T00:00:00").lt("timestamp_est", f"{as_of_date}T23:59:59")

        query = query.order("timestamp_est", desc=True).limit(limit)
        response = query.execute()

        return response.data if response.data else []
    except Exception as e:
        _LOG.warning(f"Failed to read scan_results from Supabase: {e}")
        return []


def _get_tradingview_fundamentals_cloud(as_of_date: Optional[str] = None, limit: int = 100) -> list:
    """Query Supabase trading.spx_standardized (CLOUD mode)."""
    try:
        client = get_supabase_client()
        query = client.table("spx_standardized").select("*").eq("alert_type", "fundamentals")

        if as_of_date:
            query = query.gte("received_at", f"{as_of_date}T00:00:00").lt("received_at", f"{as_of_date}T23:59:59")

        query = query.order("received_at", desc=True).limit(limit)
        response = query.execute()

        return response.data if response.data else []
    except Exception as e:
        _LOG.warning(f"Failed to read spx_standardized from Supabase: {e}")
        return []
