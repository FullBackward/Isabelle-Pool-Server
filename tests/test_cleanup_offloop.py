"""Bug 16 / audit SRV-1: the idle-cleanup sweep must never block the event loop.

Before the fix ``cleanup_idle_sessions`` called the synchronous ``close_session``
(up to the backend exit/join timeouts per session), ``_relieve_memory_pressure``
(a loop with ``time.sleep`` settles) and the Py4J liveness probe inline, freezing
every endpoint — ``/healthz`` included — for as long as they took. Now each of
those runs in ``asyncio.to_thread``. No Isabelle backend needed: the manager is a
bare shell and the blocking calls are stand-ins that sleep.
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid

from server.app.services.session import SessionStatus
from server.app.services.session_manager import SessionManager


class _IdleSession:
    """Just what the sweep reads: status, leased, is_idle, last_activity, field."""
    status = SessionStatus.ACTIVE
    leased = False
    lease_id = None
    field = "HOL"

    def __init__(self):
        self.last_activity = 0.0  # idle since the epoch

    def is_idle(self, timeout, now=None):
        return True


class _AdmitsMemory:
    def can_admit(self, snap=None):
        return True


def _bare_manager(n_sessions: int, close_seconds: float):
    mgr = SessionManager.__new__(SessionManager)
    mgr._lock = threading.Lock()
    mgr._lru = {uuid.uuid4(): _IdleSession() for _ in range(n_sessions)}
    mgr.idle_timeout = 1.0
    mgr.max_lease_age = 1.0
    mgr.memory_management_enabled = True
    mgr.memory = _AdmitsMemory()
    mgr.gateway = None  # skip the gateway-recovery branch
    closed = []

    def slow_close(sid, *, require_lease=True):  # stands in for the blocking close
        time.sleep(close_seconds)
        closed.append(sid)
        mgr._lru.pop(sid, None)
        return True

    mgr.close_session = slow_close
    return mgr, closed


async def _ticker(stop: asyncio.Event, gaps: list):
    """Records the largest gap between consecutive loop iterations — a blocked
    loop shows up as one gap the size of the blocking call."""
    last = time.monotonic()
    while not stop.is_set():
        await asyncio.sleep(0.01)
        now = time.monotonic()
        gaps.append(now - last)
        last = now


def test_cleanup_sweep_keeps_event_loop_responsive():
    asyncio.run(_run_responsive())


async def _run_responsive():
    mgr, closed = _bare_manager(n_sessions=3, close_seconds=0.3)
    stop = asyncio.Event()
    gaps: list = []
    ticker = asyncio.create_task(_ticker(stop, gaps))

    t0 = time.monotonic()
    result = await mgr.cleanup_once()
    elapsed = time.monotonic() - t0
    stop.set()
    await ticker

    assert len(closed) == 3 and sorted(result) == sorted(closed)
    assert elapsed >= 0.9  # the three closes really did block ~0.3 s each ...
    # ... but never the loop: with the closes inline the ticker would have seen one
    # ~0.3 s gap per close; off-loop it keeps ticking at ~10 ms.
    assert max(gaps) < 0.15, f"event loop stalled for {max(gaps):.3f}s during cleanup"


def test_cleanup_sweep_survives_a_failing_close(caplog):
    asyncio.run(_run_failing_close(caplog))


async def _run_failing_close(caplog):
    mgr, closed = _bare_manager(n_sessions=2, close_seconds=0.0)

    def boom(sid, *, require_lease=True):
        raise RuntimeError("backend gone")

    mgr.close_session = boom
    result = await mgr.cleanup_once()
    assert result == [] and closed == []
    assert any("failed to close idle session" in r.message for r in caplog.records)


def test_memory_relief_and_probe_run_off_loop():
    asyncio.run(_run_off_loop())


async def _run_off_loop():
    mgr, _ = _bare_manager(n_sessions=0, close_seconds=0.0)

    class _Pressured:
        def can_admit(self, snap=None):
            return False

    mgr.memory = _Pressured()
    mgr._where = lambda name: name
    relief_thread = []
    mgr._relieve_memory_pressure = lambda where: relief_thread.append(threading.current_thread())

    class _Gateway:
        pass

    mgr.gateway = _Gateway()
    probe_thread = []

    def alive():
        probe_thread.append(threading.current_thread())
        return True

    mgr.gateway_alive = alive
    await mgr.cleanup_once()
    main = threading.main_thread()
    assert relief_thread and relief_thread[0] is not main
    assert probe_thread and probe_thread[0] is not main
