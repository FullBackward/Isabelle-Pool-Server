"""Per-session memory attribution + prover death (plan §2.B part 2, review B2).

No Isabelle: a fake threaded backend answers ``mem_stats``; /proc is a fake
tree. Covers: fresh-sample rule (size_heap, not non-empty), pid-only answers,
error answers, interval gating, failed probes logged without traceback,
prover_alive, expiry fields, discard_dead + sweep sampling, listing fields.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
import uuid

import pytest

from server.app.core import metrics
from server.app.services import memory_monitor as mm
from server.app.services.session import SessionStatus
from server.app.services.session_manager import SessionManager
from server.app.services.session_memory import (
    MemoryAwareSession,
    SessionMemoryMixin,
    expiry_fields,
    memory_fields,
    probe_memory,
    prover_dead,
)

STATS = {
    "pid": "4242",
    "now": "1792195609.03",
    "size_heap": "521142272",
    "size_heap_free_last_GC": "519615088",
    "size_heap_free_last_full_GC": "0",
    "full_GCs": "0",
}


class _Raw:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def mem_stats(self):
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else {}
        if isinstance(answer, Exception):
            raise answer
        return answer

    def exit(self):
        return None


class _Backend:
    """ThreadedBackend stand-in: runs jobs inline, returns concurrent futures."""

    def __init__(self, answers=()):
        self.raw = _Raw(answers)

    def submit(self, fn):
        fut = concurrent.futures.Future()
        try:
            fut.set_result(fn())
        except Exception as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def close(self):
        return None


def _session(answers=()):
    return MemoryAwareSession(
        session_id=uuid.uuid4(),
        session_theories=[],
        loaded_theories=["x"],
        dependency_key="k",
        wrapper_theory=None,
        session_field="HOL",
        backend=_Backend(answers),
        task_group="default",
    )


# ------------------------------------------------------------ probe + records


def test_defaults_and_fresh_sample():
    s = _session()
    assert (s.ml_pid, s.heap_bytes, s.rss_mb, s.mem_sampled_at) == (
        None,
        None,
        None,
        0.0,
    )
    assert s.record_mem_stats(STATS, now=100.0) is True
    assert s.ml_pid == 4242
    assert s.heap_bytes == 521142272
    assert s.heap_mb == 497.0
    assert s.ml_heap["now"] == 1792195609  # float strings are accepted
    assert s.mem_sampled_at == 100.0


def test_pid_only_and_empty_are_not_samples_but_count_as_probed():
    s = _session()
    assert s.record_mem_stats({}, now=1.0) is False
    assert s.mem_sampled_at == 1.0 and s.ml_pid is None
    assert s.record_mem_stats({"pid": "77"}, now=2.0) is False
    assert s.ml_pid == 77 and s.heap_bytes is None


def test_error_answer_keeps_pid_and_records_error():
    s = _session()
    s.record_mem_stats(STATS, now=1.0)
    assert (
        s.record_mem_stats({"pid": "4242", "error": "Timeout waiting ..."}, now=2.0)
        is False
    )
    assert s.mem_probe_error.startswith("Timeout")
    assert s.heap_bytes == 521142272  # last good sample kept
    assert s.record_mem_stats(STATS, now=3.0) is True
    assert s.mem_probe_error is None


def test_probe_interval_gating():
    s = _session(answers=[STATS, STATS, STATS])
    assert s.maybe_probe_mem(now=100.0, min_interval=15) is True
    assert s.maybe_probe_mem(now=110.0, min_interval=15) is False  # too soon: no call
    assert s.backend.raw.calls == 1
    assert s.maybe_probe_mem(now=116.0, min_interval=15) is True
    assert s.backend.raw.calls == 2


def test_failed_probe_is_a_sample_and_logs_without_traceback(caplog):
    s = _session(answers=[RuntimeError("Py4JJavaError: boom")])
    with caplog.at_level(logging.WARNING):
        assert s.maybe_probe_mem(now=50.0, min_interval=15) is False
    assert s.mem_sampled_at == 50.0
    assert "boom" in (s.mem_probe_error or "")
    recs = [r for r in caplog.records if "memory probe failed" in r.message]
    assert recs and recs[0].exc_info is None
    assert (
        s.maybe_probe_mem(now=55.0, min_interval=15) is False
    )  # back-off: not retried
    assert s.backend.raw.calls == 1


def test_probe_skips_closed_sessions():
    s = _session(answers=[STATS])
    s.status = SessionStatus.CLOSED
    assert s.maybe_probe_mem(now=1.0, min_interval=0) is False
    assert s.backend.raw.calls == 0


def test_helpers_are_noops_for_plain_objects():
    class _Plain:
        last_activity = 0.0
        leased = False

    assert probe_memory(_Plain()) is False
    assert prover_dead(_Plain()) is False
    assert memory_fields(_Plain())["rss_mb"] is None


def test_prover_alive_uses_proc(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    (root / "4242").mkdir(parents=True)
    monkeypatch.setattr(mm, "_PROC_ROOT", str(root))
    s = _session()
    assert s.prover_alive() is None and prover_dead(s) is False  # pid unknown
    s.record_mem_stats(STATS)
    assert s.prover_alive() is True and prover_dead(s) is False
    (root / "4242").rmdir()
    assert s.prover_alive() is False and prover_dead(s) is True


def test_expiry_fields():
    class _S:
        last_activity = 1000.0
        leased = False

    f = expiry_fields(_S(), now=1100.0, idle_timeout=1800, max_lease_age=7200)
    assert f == {
        "idle_s": 100.0,
        "idle_expires_at": 2800.0,
        "lease_expires_at": None,
        "expires_in_s": 1700.0,
    }
    _S.leased = True
    f = expiry_fields(_S(), now=1100.0, idle_timeout=1800, max_lease_age=7200)
    assert f["lease_expires_at"] == 8200.0 and f["expires_in_s"] == 7100.0
    assert f["idle_expires_at"] is None  # the idle timeout does not apply while leased


def _full_stat(pid: int, starttime: int) -> str:
    """A 52-field /proc/<pid>/stat line (start time is field 22)."""
    fields = [str(pid), "(poly)", "S", "1", "1", "1", "0", "-1", "4194560"] + ["0"] * 12
    fields.append(str(starttime))  # field 22
    fields += ["0"] * 30
    return " ".join(fields) + "\n"


def test_start_time_guard_detects_pid_reuse(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    d = root / "4242"
    d.mkdir(parents=True)
    (d / "stat").write_text(_full_stat(4242, 1000))
    (d / "status").write_text("Name:\tpoly\nVmRSS:\t100 kB\n")
    monkeypatch.setattr(mm, "_PROC_ROOT", str(root))
    s = _session()
    s.record_mem_stats(STATS)  # pid 4242, start time 1000 recorded
    assert s.ml_starttime == 1000 and s.prover_alive() is True
    (d / "stat").write_text(_full_stat(4242, 5000))  # same pid, a NEW process
    assert s.prover_alive() is False and prover_dead(s) is True
    sm = _manager_with([s])
    assert sm.sample_sessions_rss(now=1.0) == {"sampled": 0, "dead": 1}
    assert s.session_id not in sm._lru


def test_check_prover_dead_uses_phase_when_pid_unknown():
    s = _session()
    assert (
        s.check_prover_dead() is False
    )  # fake backend has no session_phase -> can't tell
    s.backend.raw.session_phase = lambda: "Terminated(Process_Result(137))"
    assert s.check_prover_dead() is True and s.ml_phase.startswith("Terminated")
    s.backend.raw.session_phase = lambda: "Ready"
    assert s.check_prover_dead() is False


def test_memory_fields_never_expose_pid():
    s = _session()
    s.record_mem_stats(STATS)
    s.set_rss(850 * 1024**2, now=5.0)
    fields = s.memory_fields()
    assert fields["rss_mb"] == 850.0 and fields["heap_mb"] == 497.0
    assert "pid" not in repr(fields) and "ml_pid" not in fields


# ------------------------------------------------- manager: discard + sampling


def _manager_with(sessions):
    sm = SessionManager()
    sm.memory_management_enabled = False
    with sm._lock:
        for s in sessions:
            sm._lru[s.session_id] = s
    return sm


def _counter(metric, label):
    return metric.labels(label)._value.get()


def test_discard_dead_removes_session_and_counts():
    alive, dead = _session(), _session()
    sm = _manager_with([alive, dead])
    before = (
        _counter(metrics.sessions_evicted, "prover_died"),
        _counter(metrics.memory_failures, "prover_died"),
    )
    assert sm.discard_dead(dead.session_id) is True
    assert sm.discard_dead(dead.session_id) is False  # already gone
    ids = {e["session_id"] for e in sm.list_sessions()}
    assert ids == {str(alive.session_id)}
    assert sm.get_lru_info()["active_sessions"] == 1
    assert dead.status == SessionStatus.CLOSED
    after = (
        _counter(metrics.sessions_evicted, "prover_died"),
        _counter(metrics.memory_failures, "prover_died"),
    )
    assert after == (before[0] + 1, before[1] + 1)


def test_sweep_sampling_sets_rss_and_discards_dead(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    for pid, rss in ((100, 1000), (200, 2000)):
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / "stat").write_text(f"{pid} (poly) S 1 1 1 0 -1 0 0\n")
        (d / "status").write_text(f"Name:\tpoly\nVmRSS:\t{rss} kB\n")
    monkeypatch.setattr(mm, "_PROC_ROOT", str(root))
    a, b, gone, unknown = _session(), _session(), _session(), _session()
    a.record_mem_stats({**STATS, "pid": "100"})
    b.record_mem_stats({**STATS, "pid": "200"})
    b.acquire_lease("L")
    gone.record_mem_stats({**STATS, "pid": "300"})  # not in /proc: died
    sm = _manager_with([a, b, gone, unknown])
    out = sm.sample_sessions_rss(now=9.0)
    assert out == {"sampled": 2, "dead": 1}
    assert a.rss_bytes == 1000 * 1024 and b.rss_bytes == 2000 * 1024
    assert gone.session_id not in sm._lru and unknown.session_id in sm._lru
    info = sm.get_lru_info()
    assert info["sessions_rss_leased_mb"] == round(2000 / 1024, 1)
    assert info["sessions_rss_unleased_mb"] == round(1000 / 1024, 1)
    assert info["sessions_rss_mb"] == round(3000 / 1024, 1)


def test_listing_carries_expiry_and_memory_fields():
    s = _session()
    s.record_mem_stats(STATS)
    s.set_rss(10 * 1024**2)
    sm = _manager_with([s])
    sm.idle_timeout, sm.max_lease_age = 100.0, 200.0
    (entry,) = sm.list_sessions()
    assert entry["rss_mb"] == 10.0 and entry["heap_mb"] == 497.0
    assert entry["lease_expires_at"] is None
    assert 0 <= entry["expires_in_s"] <= 100.0
    assert "lease_id" not in entry and "ml_pid" not in entry
