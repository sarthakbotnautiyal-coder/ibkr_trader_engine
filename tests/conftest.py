"""
Pytest configuration for ibkr_auto_trader tests.

Sets up the Python path so that:
  `from src.foo import bar` resolves to ibkr_auto_trader/src/foo.py
  `from trades_db import ...` resolves to ibkr_auto_trader/src/trades_db.py

This mimics how run_engine.py resolves imports via:
  sys.path.insert(0, str(Path(__file__).parent.parent))
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_root = Path(__file__).parent.parent   # ibkr_auto_trader/ (not tests/)
sys.path.insert(0, str(_root))          # root: makes 'from trades_db import X' resolve to src/
sys.path.insert(0, str(_root / "src")) # src/: makes 'from src.foo import X' also work


# ---------------------------------------------------------------------------
# Global patches — apply to all tests
# ---------------------------------------------------------------------------

# is_market_open() returns True for all tests (avoids clock-time dependency)
_patch_market_open = patch("src.risk_manager.is_market_open", return_value=True)
_patch_market_open.start()


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def mock_ib_client():
    """Minimal IB client that satisfies contract lookups."""
    from unittest.mock import MagicMock
    client = MagicMock()
    client.reqContractDetails = MagicMock(return_value=[])
    client.reqMktData = MagicMock(return_value=[])
    return client