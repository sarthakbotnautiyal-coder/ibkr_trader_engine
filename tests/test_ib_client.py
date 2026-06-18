"""
tests/test_ib_client.py — Tests for src.executor.IBKRClient (wrapping BlockingIBKRClient).

TASK-2026-179: Tests for pending-state protocol.
  - DRY_RUN mode: returns 'dry_run' status, no real IBKR calls
  - LIVE mode: delegates to BlockingIBKRClient
  - reqAllOpenOrders() and query_order_status() added for polling sync

The new architecture:
  - executor.IBKRClient wraps src.blocking_ib_client.BlockingIBKRClient
  - In DRY_RUN mode, executor.DRY_RUN=True → IBKRClient bypasses BlockingIBKRClient
  - In LIVE mode, calls go through BlockingIBKRClient (background thread, queue-based)
  - Tests inject a mock _real directly into the client instance

Key: executor.DRY_RUN is a module-level bool. When DRY_RUN=True, place_order()
takes the dry-run path (returns status='dry_run'). When DRY_RUN=False, it
delegates to _real.place_order(). The patch must stay active through the
actual method call, not just through __init__.
"""
import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

_root = Path(__file__).parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))


# ---------------------------------------------------------------------------
# Mock BlockingIBKRClient — replaces the real background-thread client
# ---------------------------------------------------------------------------

def make_mock_blocking_client(connected: bool = True, order_id: int = 12345):
    """
    Build a mock BlockingIBKRClient that the executor.IBKRClient can use.
    Injects as client._real in LIVE mode.
    """
    mock_client = MagicMock()

    mock_client.is_connected.return_value = connected
    mock_client._health_check.return_value = True
    mock_client.register_fill_callback.return_value = None
    mock_client.disconnect.return_value = None

    from src.executor import FillResult

    def place_order_impl(params):
        if not connected:
            return FillResult(
                order_id=None, filled=False, avg_price=None,
                contracts=params.quantity, status="rejected",
                message="Not connected to IBKR",
            )
        return FillResult(
            order_id=order_id, filled=False, avg_price=None,
            contracts=params.quantity, status="pending",
            message=f"Order {order_id} submitted, awaiting fill callback",
        )

    mock_client.place_order.side_effect = place_order_impl
    mock_client.cancel_order.return_value = connected
    mock_client.get_buying_power.return_value = 50000.0
    mock_client.get_available_cash.return_value = 25000.0
    mock_client.reconcile.return_value = None

    # TASK-2026-179: Polling methods
    mock_trade = MagicMock()
    mock_trade.order = MagicMock(orderId=order_id)
    mock_trade.orderStatus = MagicMock(status="Submitted", filled=0, avgFillPrice=0.0)
    mock_client.reqAllOpenOrders.return_value = [mock_trade]
    mock_client.query_order_status.return_value = None
    mock_client.get_open_positions_ibkr.return_value = []

    return mock_client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_blocking_client():
    return make_mock_blocking_client(connected=True)


@pytest.fixture
def order_params():
    from src.executor import OrderParams
    return OrderParams(
        symbol="SPX", side="PUT", contract_type="PUT",
        strike=5500.0, long_strike=5490.0, expiry="20250519",
        quantity=4, action="OPEN", credit_debit=0.50,
    )


@pytest.fixture
def close_order_params():
    from src.executor import OrderParams
    return OrderParams(
        symbol="SPX", side="PUT", contract_type="PUT",
        strike=5500.0, long_strike=5490.0, expiry="20250519",
        quantity=4, action="CLOSE", credit_debit=None,
    )


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------

def make_client(mock_blocking_client, dry_run: bool):
    """
    Build executor.IBKRClient with DRY_RUN set and mock _real injected.

    DRY_RUN is a module-level bool in src/executor.py. In LIVE mode (dry_run=False),
    we inject mock_blocking_client as client._real (bypassing the real
    BlockingIBKRClient that would be created in __init__).
    In DRY_RUN mode, _real stays None and place_order takes the dry-run path.
    """
    import src.executor as ex

    with patch.object(ex, "DRY_RUN", dry_run, create=False):
        if not dry_run:
            # Block real BlockingIBKRClient from being instantiated
            with patch.dict("sys.modules", {"src.blocking_ib_client": MagicMock()}):
                client = ex.IBKRClient(host="127.0.0.1", port=7497, client_id=42)
        else:
            client = ex.IBKRClient(host="127.0.0.1", port=7497, client_id=42)

    if not dry_run:
        client._real = mock_blocking_client

    return client


def dry_run_client(order_params):
    """
    Build executor.IBKRClient in DRY_RUN mode AND call place_order inside
    the patch context so DRY_RUN=True is active during the method call.
    Returns (client, result).
    """
    import src.executor as ex
    from unittest.mock import MagicMock

    with patch.object(ex, "DRY_RUN", True, create=False):
        client = ex.IBKRClient(host="127.0.0.1", port=7497, client_id=42)
        result = client.place_order(order_params)

    return client, result


# ---------------------------------------------------------------------------
# connect()
# ---------------------------------------------------------------------------

def test_ib_client_connect_success(mock_blocking_client):
    """LIVE: connect() → _real.is_connected() = True → returns True."""
    mock_blocking_client.connect.return_value = True
    client = make_client(mock_blocking_client, dry_run=False)
    result = client.connect()
    assert result is True


def test_ib_client_disconnect_calls_ib_disconnect(mock_blocking_client):
    """LIVE: disconnect() → _real.disconnect() called once."""
    client = make_client(mock_blocking_client, dry_run=False)
    client.disconnect()
    mock_blocking_client.disconnect.assert_called_once()


def test_ib_client_is_connected_true(mock_blocking_client):
    """LIVE: _real.is_connected() = True → is_connected() returns True."""
    mock_blocking_client.is_connected.return_value = True
    client = make_client(mock_blocking_client, dry_run=False)
    assert client.is_connected() is True


def test_ib_client_is_connected_false(mock_blocking_client):
    """LIVE: _real.is_connected() = False → is_connected() returns False."""
    mock_blocking_client.is_connected.return_value = False
    client = make_client(mock_blocking_client, dry_run=False)
    assert client.is_connected() is False


# ---------------------------------------------------------------------------
# place_order() — LIVE mode
# ---------------------------------------------------------------------------

def test_place_order_returns_pending_when_connected(mock_blocking_client, order_params):
    """LIVE + connected: place_order() → status='pending', order_id from IBKR."""
    client = make_client(mock_blocking_client, dry_run=False)
    result = client.place_order(order_params)

    assert result.status == "pending"
    assert result.order_id == 12345
    assert result.contracts == 4
    assert result.filled is False


def test_place_order_rejected_when_not_connected(order_params):
    """LIVE + disconnected: place_order() → status='rejected'."""
    disconnected = make_mock_blocking_client(connected=False)
    client = make_client(disconnected, dry_run=False)
    result = client.place_order(order_params)

    assert result.status == "rejected"
    assert result.order_id is None
    assert "Not connected" in result.message


def test_place_order_dry_run_returns_dry_run_status(mock_blocking_client, order_params):
    """DRY_RUN=True: place_order() → status='dry_run', no _real call."""
    # Patch must be active during the place_order call (not just __init__)
    client, result = dry_run_client(order_params)
    assert result.status == "dry_run"
    assert result.order_id is not None  # local counter


def test_place_order_calls_real_place_order(mock_blocking_client, order_params):
    """LIVE: place_order() delegates to _real.place_order()."""
    client = make_client(mock_blocking_client, dry_run=False)
    result = client.place_order(order_params)

    mock_blocking_client.place_order.assert_called_once()
    called_params = mock_blocking_client.place_order.call_args[0][0]
    assert called_params.symbol == "SPX"
    assert called_params.strike == 5500.0


# ---------------------------------------------------------------------------
# cancel_order()
# ---------------------------------------------------------------------------

def test_cancel_order_calls_ib_cancel_order(mock_blocking_client):
    """LIVE: cancel_order() → _real.cancel_order(12345)."""
    client = make_client(mock_blocking_client, dry_run=False)
    result = client.cancel_order(12345)

    mock_blocking_client.cancel_order.assert_called_with(12345)
    assert result is True


def test_cancel_order_returns_false_when_not_connected(order_params):
    """LIVE + disconnected: cancel_order() → False."""
    disconnected = make_mock_blocking_client(connected=False)
    client = make_client(disconnected, dry_run=False)
    result = client.cancel_order(12345)
    assert result is False


def test_cancel_order_dry_run_returns_true(mock_blocking_client):
    """DRY_RUN=True: cancel_order() → True without calling IB."""
    import src.executor as ex

    with patch.object(ex, "DRY_RUN", True, create=False):
        client = ex.IBKRClient(host="127.0.0.1", port=7497, client_id=42)
        result = client.cancel_order(12345)

    assert result is True


# ---------------------------------------------------------------------------
# get_buying_power() / get_available_cash()
# ---------------------------------------------------------------------------

def test_get_buying_power_returns_float(mock_blocking_client):
    """LIVE: get_buying_power() → _real.get_buying_power()."""
    mock_blocking_client.get_buying_power.return_value = 12345.67
    client = make_client(mock_blocking_client, dry_run=False)
    result = client.get_buying_power()
    assert isinstance(result, float)
    assert result == 12345.67


def test_get_buying_power_returns_zero_when_tag_missing(mock_blocking_client):
    """LIVE: _real returns 0 → get_buying_power() returns 0.0."""
    mock_blocking_client.get_buying_power.return_value = 0.0
    client = make_client(mock_blocking_client, dry_run=False)
    result = client.get_buying_power()
    assert result == 0.0


def test_get_available_cash_returns_float(mock_blocking_client):
    """LIVE: get_available_cash() → _real.get_available_cash()."""
    mock_blocking_client.get_available_cash.return_value = 50000.0
    client = make_client(mock_blocking_client, dry_run=False)
    result = client.get_available_cash()
    assert result == 50000.0


def test_get_buying_power_dry_run_returns_large_value(mock_blocking_client):
    """DRY_RUN=True: get_buying_power() → 999999.0."""
    import src.executor as ex

    with patch.object(ex, "DRY_RUN", True, create=False):
        client = ex.IBKRClient(host="127.0.0.1", port=7497, client_id=42)
        result = client.get_buying_power()

    assert result == 999999.0


def test_get_available_cash_dry_run_returns_large_value(mock_blocking_client):
    """DRY_RUN=True: get_available_cash() → 999999.0."""
    import src.executor as ex

    with patch.object(ex, "DRY_RUN", True, create=False):
        client = ex.IBKRClient(host="127.0.0.1", port=7497, client_id=42)
        result = client.get_available_cash()

    assert result == 999999.0


# ---------------------------------------------------------------------------
# reconcile()
# ---------------------------------------------------------------------------

def test_reconcile_calls_req_positions(mock_blocking_client):
    """LIVE: reconcile() → _real.reconcile(mock_store)."""
    client = make_client(mock_blocking_client, dry_run=False)
    mock_store = MagicMock()
    mock_store.get_open.return_value = []
    client.reconcile(mock_store)

    mock_blocking_client.reconcile.assert_called_once_with(mock_store)


# ---------------------------------------------------------------------------
# TASK-2026-179: Polling methods
# ---------------------------------------------------------------------------

def test_reqAllOpenOrders_delegates_to_real(mock_blocking_client):
    """LIVE: reqAllOpenOrders() → _real.reqAllOpenOrders()."""
    client = make_client(mock_blocking_client, dry_run=False)
    result = client.reqAllOpenOrders()

    mock_blocking_client.reqAllOpenOrders.assert_called_once()
    assert isinstance(result, list)


def test_reqAllOpenOrders_dry_run_returns_empty_list(mock_blocking_client):
    """DRY_RUN=True: reqAllOpenOrders() → [] (no _real call)."""
    import src.executor as ex

    with patch.object(ex, "DRY_RUN", True, create=False):
        client = ex.IBKRClient(host="127.0.0.1", port=7497, client_id=42)
        result = client.reqAllOpenOrders()

    assert result == []


def test_query_order_status_delegates_to_real(mock_blocking_client):
    """LIVE: query_order_status(12345) → _real.query_order_status(12345)."""
    client = make_client(mock_blocking_client, dry_run=False)
    result = client.query_order_status(12345)

    mock_blocking_client.query_order_status.assert_called_with(12345)
    assert result is None  # mock returns None (no active order)


def test_query_order_status_dry_run_returns_none(mock_blocking_client):
    """DRY_RUN=True: query_order_status() → None (no _real call)."""
    import src.executor as ex

    with patch.object(ex, "DRY_RUN", True, create=False):
        client = ex.IBKRClient(host="127.0.0.1", port=7497, client_id=42)
        result = client.query_order_status(12345)

    assert result is None


def test_get_open_positions_ibkr_delegates_to_real(mock_blocking_client):
    """LIVE: get_open_positions_ibkr() → _real.get_open_positions_ibkr()."""
    client = make_client(mock_blocking_client, dry_run=False)
    result = client.get_open_positions_ibkr()

    mock_blocking_client.get_open_positions_ibkr.assert_called_once()
    assert result == []


def test_get_open_positions_ibkr_dry_run_returns_empty_list(mock_blocking_client):
    """DRY_RUN=True: get_open_positions_ibkr() → [] (no _real call)."""
    import src.executor as ex

    with patch.object(ex, "DRY_RUN", True, create=False):
        client = ex.IBKRClient(host="127.0.0.1", port=7497, client_id=42)
        result = client.get_open_positions_ibkr()

    assert result == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])