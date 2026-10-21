"""fill_sorries stage 1c (plan §1.6, review B4): ATP processes attributed to the
session whose sledgehammer runs — the gateway's descendants minus the
plumbing and the prover subtrees — shared between in-flight steps. Fake /proc."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from server.app.services import fill_sorries as fs
from server.app.services import memory_monitor as mm
from server.app.services.session_memory import MemoryAwareSession


def _fake_proc(tmp_path, procs):
    """procs: {pid: (ppid, pgrp, rss_kb, comm)}"""
    root = tmp_path / "proc"
    root.mkdir()
    for pid, (ppid, pgrp, rss_kb, comm) in procs.items():
        d = root / str(pid)
        d.mkdir()
        (d / "stat").write_text(
            f"{pid} ({comm}) S {ppid} {pgrp} 1 0 -1 4194560 0 0 0 0 0 0 0 0\n"
        )
        (d / "status").write_text(f"Name:\t{comm}\nVmRSS:\t   {rss_kb} kB\n")
    (root / "self").mkdir()
    return root


PROCS = {
    1: (0, 1, 50, "python"),  # the server
    161: (1, 161, 3, "bash"),  # gateway launcher (setsid -> pgrp 161)
    276: (161, 161, 3, "bash"),
    298: (276, 161, 3000, "java"),  # the JVM
    650: (298, 161, 1, "bash_process"),  # each script runs in its OWN session
    652: (650, 652, 2, "bash"),
    654: (652, 652, 500, "poly"),  # session A's prover ...
    655: (654, 652, 36, "Naproche"),  # ... and what it spawned: skipped whole
    700: (298, 161, 1, "bash_process"),
    702: (700, 702, 2, "bash"),
    800: (702, 702, 1600, "veriT"),  # ATPs: under the JVM, other pgrps
    801: (702, 702, 90, "SPASS"),
    790: (298, 161, 1, "bash_process"),
    792: (790, 792, 2, "bash"),
    793: (792, 792, 1, "timeout"),
    802: (793, 792, 77, "cvc5-bin"),
    900: (1, 900, 400, "eprover"),  # an orphan / another server's solver
}


def test_pgrp_map_and_gateway_children(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "_PROC_ROOT", str(_fake_proc(tmp_path, PROCS)))
    m = mm.pgrp_map()
    assert m[298] == (276, 161, "java") and m[800] == (702, 702, "veriT")
    total, count = mm.gateway_children_rss(161, exclude_pids=[654])
    assert (total, count) == (
        (1600 + 90 + 77) * 1024,
        3,
    )  # not the plumbing, not the prover's subtree, not the stranger 900
    assert mm.gateway_children_rss(161, exclude_pids=[654, 800]) == (
        (90 + 77) * 1024,
        2,
    )
    assert mm.gateway_children_rss(298, exclude_pids=[654]) == (total, count)
    assert mm.gateway_children_rss(4242) == (0, 0)


class _Backend:
    def __init__(self):
        self.raw = type(
            "R", (), {"mem_stats": lambda self: {}, "exit": lambda self: None}
        )()

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


def test_note_and_clear_sledgehammer_rss():
    s = _session()
    assert (
        s.memory_fields()["sledgehammer_mb"] == 0.0
        and s.memory_fields()["sledgehammer_peak_mb"] == 0.0
    )
    s.note_sledgehammer_rss(1500 * 1024**2, shared_with=1, now=5.0)
    f = s.memory_fields()
    assert (
        f["sledgehammer_mb"] == 1500.0
        and f["sledgehammer_peak_mb"] == 1500.0
        and f["sledgehammer_shared_with"] == 1
    )
    s.note_sledgehammer_rss(700 * 1024**2, shared_with=0, now=6.0)
    assert (
        s.memory_fields()["sledgehammer_mb"] == 700.0
        and s.memory_fields()["sledgehammer_peak_mb"] == 1500.0
    )
    s.clear_sledgehammer_rss()
    f = s.memory_fields()
    assert (
        f["sledgehammer_mb"] == 0.0
        and f["sledgehammer_peak_mb"] == 1500.0
        and f["sledgehammer_shared_with"] == 0
    )


class _Mgr(fs.FillJobMixin):
    def __init__(self, samples, running=1):
        self.samples = list(samples)
        self.slots = type("S", (), {"running": running})()
        self.gateway = None

    def sample_sledgehammer_rss(self):
        return self.samples.pop(0) if self.samples else None


def test_tracker_attributes_samples_and_clears(monkeypatch):
    from server.app.core.config import Memory

    monkeypatch.setattr(Memory, "FILL_SAMPLE_S", 0.02)
    s = _session()
    mgr = _Mgr([100 * 1024**2, 300 * 1024**2, 200 * 1024**2], running=2)

    async def go():
        async with mgr.track_sledgehammer(s):
            await asyncio.sleep(0.1)
            mid = s.memory_fields()
        return mid

    mid = asyncio.run(go())
    assert (
        mid["sledgehammer_peak_mb"] == 300.0 and mid["sledgehammer_shared_with"] == 1
    )  # two in flight
    end = s.memory_fields()
    assert (
        end["sledgehammer_mb"] == 0.0
        and end["sledgehammer_peak_mb"] == 300.0
        and end["sledgehammer_shared_with"] == 0
    )


def test_sample_walks_the_gateway_tree_and_excludes_provers(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "_PROC_ROOT", str(_fake_proc(tmp_path, PROCS)))
    s = _session()
    s.ml_pid = 654
    import threading

    mgr = fs.FillJobMixin.__new__(fs.FillJobMixin)
    mgr.gateway = type("G", (), {"process": type("P", (), {"pid": 161})()})()
    mgr._lru = {s.session_id: s}
    mgr._lock = threading.Lock()
    assert mgr.sample_sledgehammer_rss() == (1600 + 90 + 77) * 1024
    mgr.gateway = None
    assert mgr.sample_sledgehammer_rss() is None


def test_facts_row_and_memory_route_fields():
    from server.app.api.v1.routes import memory as memory_route
    from server.app.services.memory_policy import session_facts_row

    s = _session()
    s.note_sledgehammer_rss(1234 * 1024**2, shared_with=1)
    row = session_facts_row(s, now=0.0)
    assert row["sledgehammer_mb"] == 1234.0 and row["sledgehammer_shared_with"] == 1
    assert {
        "sledgehammer_mb",
        "sledgehammer_peak_mb",
        "sledgehammer_shared_with",
    } <= set(memory_route._SESSION_FIELDS)


def test_cancelled_step_keeps_attribution_until_the_solvers_end(monkeypatch):
    """Review 260d78d S3: cancel_job_nowait cancels the job task while the
    executor future (the solvers) still runs; the attribution stays on the
    session until that future completes, then it is cleared and the slot
    released."""
    from test_fill_job import _cmd, _JobMgr, _Session

    from server.app.core.config import Memory

    monkeypatch.setattr(Memory, "FILL_SAMPLE_S", 0.01)
    cmds = [_cmd(2, "lemma", 1), _cmd(4, "sorry", 1, col=12)]
    replies = {
        (2, 1): {"found": True, "subgoals": 1, "results": ["Try this: by simp (1 ms)"]}
    }
    s = _Session(replies, delay=0.3)
    mgr = _JobMgr()
    mgr.sample_sledgehammer_rss = lambda: 50 * 1024**2

    async def go():
        job = mgr.start_fill(
            s,
            text="lemma a: A sorry\n",
            mode="chunk",
            commands=cmds,
            timeout_s=20,
            max_total_s=600,
            provers="",
        )
        await asyncio.sleep(0.08)
        assert s.memory_fields()["sledgehammer_mb"] == 50.0
        mgr.cancel_job_nowait(s.session_id)
        await asyncio.sleep(0.05)  # task cancelled, the 0.3 s "solver" still runs
        assert job.task.done() or job.task.cancelled() or job.cancelled
        assert s.memory_fields()["sledgehammer_mb"] == 50.0  # still attributed
        assert mgr.slots.running == 1  # and the slot still taken
        await asyncio.sleep(0.4)
        assert s.memory_fields()["sledgehammer_mb"] == 0.0
        assert mgr.slots.running == 0

    asyncio.run(go())
