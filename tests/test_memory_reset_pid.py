"""A document reset gives the session a new ML process: the memory hook must
not read the stale pid as "prover died" and discard the session (seen live in
the memory-scaling smoke, 2026-10-20)."""

from __future__ import annotations

import uuid

from server.app.api.v1.memory_hooks import run_with_memory_hooks
from server.app.services.session_memory import MemoryAwareSession


class _Raw:
    def session_phase(self):
        return "Ready"

    def mem_stats(self):
        return {"pid": "4242", "size_heap": "1000"}

    def exit(self):
        return None


class _Backend:
    def __init__(self):
        self.raw = _Raw()

    def submit(self, fn):
        import concurrent.futures

        fut = concurrent.futures.Future()
        try:
            fut.set_result(fn())
        except Exception as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def close(self):
        return None


class _Mgr:
    def __init__(self):
        self.discarded = []

    def discard_dead(self, sid, reason="prover_died"):
        self.discarded.append(sid)
        return True


def _session():
    return MemoryAwareSession(
        session_id=uuid.uuid4(),
        session_theories=[],
        loaded_theories=["x"],
        dependency_key="k",
        wrapper_theory=None,
        session_field="HOL",
        backend=_Backend(),
        task_group="default",
    )


def test_reset_forgets_the_old_pid_and_the_hook_keeps_the_session():
    s = _session()
    s.ml_pid, s.ml_starttime = (
        999_999_999,
        1,
    )  # a pid that does not exist: "dead" by /proc
    mgr = _Mgr()
    _, override = run_with_memory_hooks(lambda: "x", s, mgr)
    assert override == "prover_died" and mgr.discarded == [
        s.session_id
    ]  # the stale pid rule, unchanged
    s2 = _session()
    s2.ml_pid, s2.ml_starttime = 999_999_999, 1
    s2._reset_bookkeeping()  # what load_document's reset path calls
    assert s2.ml_pid is None and s2.mem_sampled_at == 0.0
    mgr2 = _Mgr()
    result, override = run_with_memory_hooks(lambda: "ok", s2, mgr2)
    assert result == "ok" and override is None and mgr2.discarded == []
    assert s2.ml_pid == 4242  # the probe after the call recorded the NEW pid
