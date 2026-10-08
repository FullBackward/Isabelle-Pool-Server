"""Regression tests for ThreadedBackend shutdown semantics.

Guards against the e6c3869 regression where close() set _shutting_down and then
called self.submit(self._backend.exit) — which the guard itself rejected, so the
exit job never reached the JVM and every closed session leaked its poly process
and in-JVM Isabelle server (session memory-release research, internal working notes).
"""
from __future__ import annotations

import threading
import time

import pytest

from server.app.services.threaded_backend import ThreadedBackend


class StubBackend:
    """Minimal stand-in for the Py4J ReplBackend."""

    def __init__(self):
        self.exit_called = threading.Event()

    def exit(self) -> None:
        self.exit_called.set()


def test_close_delivers_exit_to_backend():
    stub = StubBackend()
    tb = ThreadedBackend(stub, name="test-exit")
    tb.close()
    assert stub.exit_called.is_set(), (
        "backend.exit() was never executed during close() — the shutdown guard "
        "rejected the exit job"
    )


def test_submit_rejected_after_close():
    stub = StubBackend()
    tb = ThreadedBackend(stub, name="test-reject")
    tb.close()
    fut = tb.submit(lambda: "late")
    with pytest.raises(RuntimeError, match="shutting down"):
        fut.result(timeout=1)


def test_pending_jobs_cancelled_but_exit_still_runs():
    stub = StubBackend()
    tb = ThreadedBackend(stub, name="test-drain")

    # Occupy the worker so subsequent jobs stay queued.
    release = threading.Event()
    running = threading.Event()

    def blocking_job():
        running.set()
        release.wait(timeout=10)
        return "done"

    blocking_fut = tb.submit(blocking_job)
    assert running.wait(timeout=5)
    queued = [tb.submit(lambda: None) for _ in range(3)]

    # Unblock the worker shortly after close() starts draining.
    threading.Timer(0.2, release.set).start()
    tb.close()

    assert blocking_fut.result(timeout=1) == "done"
    for fut in queued:
        with pytest.raises(RuntimeError, match="shut down before job executed"):
            fut.result(timeout=1)
    assert stub.exit_called.is_set()


def test_worker_thread_stops_after_close():
    stub = StubBackend()
    tb = ThreadedBackend(stub, name="test-join")
    tb.close()
    deadline = time.time() + 5
    while tb._t.is_alive() and time.time() < deadline:
        time.sleep(0.05)
    assert not tb._t.is_alive()
