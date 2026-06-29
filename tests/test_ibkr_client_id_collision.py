"""Tests for TASK-2026-275: engine failfast on duplicate clientId.

Background:
  Today's incident (2026-06-29, TASK-2026-278) saw two engine instances
  collide on the same client_id for 3h40m. PR #10 (commit 3bb44b4) added
  a client-id rotation ladder that was supposed to self-heal, but in
  practice both instances just kept stepping on each other. The fix in
  TASK-2026-275 is to *crash fast* on the first collision: exit code 2
  signals the watchdog (TASK-2026-276) to back off 15 min instead of
  restarting in 5 min and creating the same collision.

What's tested here:
  - On Error 326 ("client id already in use"), the background connect path
    calls sys.exit(2) instead of returning False into the retry loop.
  - The exit happens on the *first* collision — no rotation, no ladder.
  - On a successful connect, sys.exit is NOT called.
  - On a non-326 failure (e.g. plain TimeoutError), sys.exit is NOT called
    (those still fall through to the retry/backoff loop).
  - The rotation ladder from PR #10 is gone.

We exercise the real worker thread (not just _do_connect in isolation)
because that's what will run in production. The IB class is mocked at
the module level so the worker thinks it has a real TWS to talk to.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# The worker thread raises SystemExit on Error 326 — that's the behavior
# under test. pytest treats it as an "unhandled thread exception" warning;
# silence it for this module.
pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"
)

# Mirror the path setup used by the other tests in this dir.
_root = Path(__file__).parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "src"))

# Import after path tweak so the relative `from config import CONFIG` inside
# blocking_ib_client resolves the same way as in production.
import blocking_ib_client  # noqa: E402
from blocking_ib_client import (  # noqa: E402
    _IBThreadState,
    _ib_thread_worker,
)


def _make_state(client_id: int = 31) -> _IBThreadState:
    """Build a minimal _IBThreadState suitable for running the worker thread."""
    log = logging.getLogger(f"test_collision_{threading.get_ident()}_{time.time_ns()}")
    log.addHandler(logging.NullHandler())
    return _IBThreadState(
        host="127.0.0.1",
        port=7497,
        client_id=client_id,
        log=log,
    )


# ---------------------------------------------------------------------------
# Fake IBs — simulate (a) collision with Error 326, (b) clean connect, (c) plain
# timeout. The worker's _do_connect() wires up `ib.errorEvent += _capture_err`
# and then calls `ib.connect(...)`. We need a fake where `+=` registers a
# handler that we can invoke ourselves.
# ---------------------------------------------------------------------------

class _FakeEvent:
    """Stand-in for ib_async's errorEvent Event.

    The worker does `ib.errorEvent += handler` and `ib.errorEvent -= handler`.
    We store registered handlers and let `connect()` invoke them.
    """
    def __init__(self):
        self._handlers = []

    def __iadd__(self, handler):
        if handler not in self._handlers:
            self._handlers.append(handler)
        return self  # real Event returns self

    def __isub__(self, handler):
        try:
            self._handlers.remove(handler)
        except ValueError:
            pass
        return self

    def fire(self, reqId, errorCode, errorString, contract=None):
        for h in list(self._handlers):
            try:
                h(reqId, errorCode, errorString, contract)
            except Exception:
                pass


class _FakeIB326:
    """Fake ib_async.IB that simulates a 'client id already in use' rejection.

    On connect(), fire Error 326 to all registered handlers THEN raise
    TimeoutError (matching how real ib_async surfaces Error 326: as a
    TimeoutError with the error code captured via errorEvent).
    """

    def __init__(self):
        self.errorEvent = _FakeEvent()
        self._connect_calls = 0

    def connect(self, host, port, clientId, timeout=15, readonly=False):
        self._connect_calls += 1
        self.errorEvent.fire(None, 326, "client id already in use", None)
        raise TimeoutError("could not connect (simulated 326)")

    def sleep(self, _seconds):
        return None

    def disconnect(self):
        return None


class _FakeIBOK:
    """Fake ib_async.IB that simulates a successful connect."""

    def __init__(self):
        self.errorEvent = _FakeEvent()
        self._connect_calls = 0

    def connect(self, host, port, clientId, timeout=15, readonly=False):
        self._connect_calls += 1
        return None

    def sleep(self, _seconds):
        return None

    def disconnect(self):
        return None


class _FakeIBTimeout:
    """Fake ib_async.IB that times out with no error code (not a 326)."""

    def __init__(self):
        self.errorEvent = _FakeEvent()
        self._connect_calls = 0

    def connect(self, host, port, clientId, timeout=15, readonly=False):
        self._connect_calls += 1
        raise TimeoutError("plain timeout, no IB error")

    def sleep(self, _seconds):
        return None

    def disconnect(self):
        return None


def _run_worker_until_exit(state, timeout_s: float = 3.0) -> threading.Thread:
    """Start the worker thread; let it terminate naturally (via SystemExit)."""
    t = threading.Thread(target=_ib_thread_worker, args=(state,), daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    return t


def _run_worker_until_shutdown(state, settle_s: float = 0.5, timeout_s: float = 3.0) -> threading.Thread:
    """Start the worker thread, let it run briefly, then request shutdown."""
    t = threading.Thread(target=_ib_thread_worker, args=(state,), daemon=True)
    t.start()
    time.sleep(settle_s)
    state._shutdown.set()
    t.join(timeout=timeout_s)
    return t


# ---------------------------------------------------------------------------
# Core test: Error 326 → sys.exit(2) on the first collision.
# ---------------------------------------------------------------------------

def test_error_326_triggers_sys_exit_2():
    """On Error 326 ('client id already in use'), the worker calls sys.exit(2).

    This is the core failfast behavior. Two engine instances collided for
    3h40m today (TASK-2026-278); the new behavior is to crash fast so the
    watchdog (TASK-2026-276) can back off instead of restart-loop.
    """
    state = _make_state(client_id=31)

    exit_calls: list = []

    def fake_sys_exit(code=0):
        exit_calls.append(code)
        # Raise SystemExit so the worker thread actually stops. If we
        # didn't raise, the thread would keep looping and the test would
        # hang until t.join() times out.
        raise SystemExit(code)

    fake_ib = _FakeIB326()
    with patch.object(blocking_ib_client, "IB", lambda *a, **kw: fake_ib), \
         patch.object(blocking_ib_client.sys, "exit", side_effect=fake_sys_exit):
        thread = _run_worker_until_exit(state)

    assert exit_calls == [2], (
        f"Expected exactly one sys.exit(2) call on Error 326, got: {exit_calls}"
    )
    # The worker thread must have terminated (not still running).
    assert not thread.is_alive(), (
        "Worker thread is still alive after sys.exit(2) — the exit didn't "
        "actually stop the thread"
    )


# ---------------------------------------------------------------------------
# Negative tests: ensure sys.exit(2) is NOT called in other cases.
# ---------------------------------------------------------------------------

def test_successful_connect_does_not_call_sys_exit():
    """A clean connect must not exit 2 — exit 2 is collision-specific."""
    state = _make_state(client_id=31)

    exit_calls: list = []

    def fake_sys_exit(code=0):
        exit_calls.append(code)
        # Don't raise — let the worker proceed so we can observe normal flow.

    fake_ib = _FakeIBOK()
    with patch.object(blocking_ib_client, "IB", lambda *a, **kw: fake_ib), \
         patch.object(blocking_ib_client.sys, "exit", side_effect=fake_sys_exit):
        t = _run_worker_until_shutdown(state, settle_s=0.5)

    assert exit_calls == [], (
        f"Successful connect must not call sys.exit, but got: {exit_calls}"
    )
    assert not t.is_alive(), "Worker should have shut down cleanly"


def test_non_326_timeout_does_not_call_sys_exit_2():
    """A bare timeout (no IB error code) must NOT trigger exit 2.

    Only Error 326 = "client id already in use" should exit 2. Plain
    network timeouts (TWS loading, etc.) still fall through to the
    retry/backoff loop. Exit 2 is specifically the duplicate-instance
    signal.
    """
    state = _make_state(client_id=31)

    exit_calls: list = []

    def fake_sys_exit(code=0):
        exit_calls.append(code)
        # Don't raise — let the retry loop run so we observe the retry path.

    fake_ib = _FakeIBTimeout()
    with patch.object(blocking_ib_client, "IB", lambda *a, **kw: fake_ib), \
         patch.object(blocking_ib_client.sys, "exit", side_effect=fake_sys_exit):
        t = _run_worker_until_shutdown(state, settle_s=0.5, timeout_s=3.0)

    assert 2 not in exit_calls, (
        f"Plain TimeoutError (no IB error code) must not exit 2. Got: {exit_calls}"
    )
    assert not t.is_alive(), "Worker should shut down cleanly when requested"


# ---------------------------------------------------------------------------
# Belt-and-suspenders: the rotation ladder from PR #10 is gone.
# ---------------------------------------------------------------------------

def test_collision_does_not_trigger_client_id_rotation():
    """After Error 326, the engine must NOT try a different client_id.

    PR #10 (commit 3bb44b4) added _compute_client_id() to rotate through
    base+100, base+200, ... on collision. That ladder turned a one-shot
    collision into a 3h40m retry storm (TASK-2026-278). It has been
    removed in TASK-2026-275.
    """
    import blocking_ib_client as bic

    # The rotation function and the offsets tuple must no longer exist.
    assert not hasattr(bic, "_compute_client_id"), (
        "_compute_client_id() must be removed — PR #10's rotation ladder "
        "caused the 3h40m retry storm (TASK-2026-278). See TASK-2026-275."
    )
    assert not hasattr(bic, "_CLIENT_ID_FALLBACK_OFFSETS"), (
        "_CLIENT_ID_FALLBACK_OFFSETS must be removed with the rotation ladder."
    )