"""Memory policy for non-session work and the startup capacity check (batch 1 of
the memory-awareness plan, items 1.2 and 1.3).

- big-step and heap-pool builds are refused with 503 (Retry-After) while the
  memory gate has no room, instead of bypassing it;
- the big-step build concurrency comes from ISABELLE_MAX_CONCURRENT_BIGSTEP_BUILDS,
  not from pool_size // 2;
- startup logs capacity warnings (pool does not fit, ML heap cap over half the
  limit, monitor blind).
No Isabelle: the manager is built without a gateway and the monitor is stubbed.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app.core import metrics
from server.app.core.config import Memory, Server
from server.app.dependencies import get_heap_pool, get_session_manager
from server.app.errors import PoolExhausted
from server.app.main import app
from server.app.services import memory_monitor as mm
from server.app.services.session_manager import SessionManager


class _Monitor:
    """MemoryMonitor stand-in with a fixed verdict and snapshot."""

    def __init__(
        self,
        admit: bool,
        used_mb: float = 1000.0,
        limit_mb: float = 2000.0,
        source: str = mm.SOURCE_CGROUP_V2,
    ):
        self._admit = admit
        self._snap = mm.MemorySnapshot(
            used_bytes=int(used_mb * 1024**2),
            limit_bytes=int(limit_mb * 1024**2),
            source=source,
        )

    def read(self):
        return self._snap

    def can_admit(self, snapshot=None):
        return self._admit

    def state_for(self, snapshot):
        return mm.STATE_OK if self._admit else mm.STATE_CRITICAL


class _ExplodingVerifier:
    """Honours the admit hook (the gate runs inside verify, after a cache
    miss) and explodes if a build would actually start."""

    async def verify(self, *, admit=None, **kwargs):
        if admit is not None:
            await admit()
        raise AssertionError("build must not start under memory pressure")


def _manager(admit: bool, enabled: bool = True, **monitor_kw) -> SessionManager:
    sm = SessionManager()  # no gateway, no startup
    sm.memory = _Monitor(admit, **monitor_kw)
    sm.memory_management_enabled = enabled
    return sm


def _counter(reason: str) -> float:
    return metrics.pool_exhausted.labels(reason)._value.get()


# ---------------------------------------------------------- build admission


def test_bigstep_build_refused_under_pressure():
    sm = _manager(admit=False)
    sm.build_verifier = _ExplodingVerifier()
    before = _counter("memory_build")
    with pytest.raises(PoolExhausted) as exc:
        asyncio.run(
            sm.verify_big_step_build(
                theory_name="T",
                theory="theory T imports Main begin end",
                field="HOL",
                timeout=1.0,
            )
        )
    assert "bigstep build" in str(exc.value)
    assert _counter("memory_build") == before + 1


def test_bigstep_build_proceeds_when_memory_ok():
    sm = _manager(admit=True)

    class _Verifier:
        async def verify(self, **kwargs):
            return {"ran": True, **kwargs}

    sm.build_verifier = _Verifier()
    result = asyncio.run(
        sm.verify_big_step_build(
            theory_name="T",
            theory="theory T imports Main begin end",
            field="HOL",
            timeout=1.0,
        )
    )
    assert result["ran"] is True
    assert result["field"] == "HOL"


def test_build_gate_is_off_when_management_disabled():
    sm = _manager(admit=False, enabled=False)
    sm.require_memory_for_build("bigstep")  # no raise


def test_bigstep_concurrency_comes_from_its_own_knob():
    sm = SessionManager(pool_size=24)  # pool_size // 2 would be 12
    assert (
        sm.build_verifier.max_concurrent_builds == Server.MAX_CONCURRENT_BIGSTEP_BUILDS
    )


@pytest.fixture()
def pressured_client():
    sm = _manager(admit=False)

    class _StubHeapPool:
        def get(self, task_group, project):  # the route checks "already building" first
            return None

        def __getattr__(self, name):
            raise AssertionError(f"heap build must not start: heap_pool.{name} called")

    app.dependency_overrides[get_session_manager] = lambda: sm
    app.dependency_overrides[get_heap_pool] = lambda: _StubHeapPool()
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_session_manager, None)
        app.dependency_overrides.pop(get_heap_pool, None)


def test_heap_build_route_refused_under_pressure(pressured_client):
    # A project under an allowed root (the checkout itself, host-aware default),
    # so the request passes schema validation and reaches the memory gate.
    project = str(
        Path(__file__).resolve().parents[1] / "examples" / "heap_demo_project"
    )
    resp = pressured_client.post(
        "/api/v1/heaps/build",
        json={"task_group": "alpha", "project": project},
    )
    assert resp.status_code == 503
    assert resp.headers.get("Retry-After") == "5"
    assert "heap build" in resp.json()["detail"]


# ---------------------------------------------------- startup capacity check


def test_capacity_warning_when_pool_does_not_fit(caplog):
    sm = _manager(admit=True, limit_mb=8000.0)
    sm.pool_size = 6  # 6 x 2048 + 4096 = 16384 > 8000
    with caplog.at_level(logging.WARNING):
        warnings = sm.log_capacity_warnings()
    assert any("exceeds the memory limit" in w for w in warnings)
    assert any("capacity check" in r.message for r in caplog.records)


def test_capacity_warning_when_ml_heap_cap_over_half_limit():
    sm = _manager(admit=True, limit_mb=float(Memory.ML_MAXHEAP_MB))  # cap == limit
    sm.pool_size = 1
    warnings = sm.log_capacity_warnings()
    assert any("ISABELLE_ML_MAXHEAP_MB" in w for w in warnings)


def test_capacity_warning_when_monitor_is_blind():
    sm = _manager(admit=True, used_mb=0.0, limit_mb=10**6, source=mm.SOURCE_FALLBACK)
    sm.pool_size = 1
    warnings = sm.log_capacity_warnings()
    assert any("never fires" in w for w in warnings)


def test_no_capacity_warnings_for_a_sane_setup():
    sm = _manager(admit=True, limit_mb=10**6)  # 1 TB: nothing to complain about
    sm.pool_size = 3
    assert sm.log_capacity_warnings() == []


# ------------------------------------------------ batch 2: facts + build hook

from server.app.errors import MemoryPressure  # noqa: E402
from server.app.services.success_checker import classify_error_kind  # noqa: E402


class _Row:
    """Minimal session for the facts payload (no backend)."""

    def __init__(self, sid, leased=False, in_use=False, rss_mb=None, label=None):
        self.session_id = sid
        self.label = label
        self.task_group = "default"
        self.field = "HOL"
        self.leased = leased
        self.in_use = in_use
        self.last_activity = 0.0
        self.rss_bytes = None if rss_mb is None else int(rss_mb * 1024**2)
        self.heap_bytes = None
        self.lease_id = "SECRET-LEASE"


def test_build_refusal_is_memory_pressure_with_facts():
    sm = _manager(admit=False, used_mb=1900.0, limit_mb=2000.0)
    with pytest.raises(MemoryPressure) as exc:
        sm.require_memory_for_build("heap")
    facts = exc.value.facts
    assert facts["state"] == "critical"
    assert facts["memory"]["limit_mb"] == 2000.0
    assert facts["builds_in_flight"] == {"bigstep": 0, "heap": 0}
    assert "evictable" in facts and "leased" in facts


def test_memory_facts_split_cap_and_never_leak_leases(monkeypatch):
    monkeypatch.setattr(Memory, "FACTS_LIST_CAP", 3)
    sm = _manager(admit=True)
    with sm._lock:
        for i in range(5):
            sm._lru[f"idle{i}"] = _Row(f"idle{i}", rss_mb=100.0 * i, label=f"L{i}")
        sm._lru["busy"] = _Row("busy", in_use=True, rss_mb=50.0)
        sm._lru["mine"] = _Row("mine", leased=True, rss_mb=900.0)
    facts = sm.memory_facts()
    assert [r["session_id"] for r in facts["evictable"]] == ["idle4", "idle3", "idle2"]
    assert facts["evictable_truncated"] is True
    assert [r["session_id"] for r in facts["leased"]] == ["mine"]
    assert facts["leased_truncated"] is False
    assert facts["leased"][0]["rss_mb"] == 900.0
    assert "SECRET-LEASE" not in repr(facts)
    assert all("lease_id" not in r for r in facts["evictable"] + facts["leased"])


def test_memory_facts_counts_heap_builds_in_flight():
    class _HeapPool:
        def list(self, task_group=None):
            return [{"status": "building"}, {"status": "ready"}, {"status": "building"}]

    sm = _manager(admit=True)
    sm.heap_pool = _HeapPool()
    sm.build_verifier.inflight = 2
    assert sm.builds_in_flight() == {"bigstep": 2, "heap": 2}


class _CountingVerifier:
    """BuildVerifier stand-in honouring the admit hook contract."""

    def __init__(self, cached=False):
        self.cached = cached
        self.runs = 0

    async def verify(self, *, admit=None, **kwargs):
        if self.cached:
            return {"cached": True}
        if admit is not None:
            await admit()
        self.runs += 1
        return {"cached": False}


def test_bigstep_passes_admit_hook_and_refuses_on_miss():
    sm = _manager(admit=False)
    sm.build_verifier = _CountingVerifier(cached=False)
    with pytest.raises(MemoryPressure):
        asyncio.run(
            sm.verify_big_step_build(
                theory_name="T",
                theory="theory T imports Main begin end",
                field="HOL",
                timeout=1.0,
            )
        )
    assert sm.build_verifier.runs == 0


def test_bigstep_cache_hit_is_served_under_pressure():
    sm = _manager(admit=False)
    sm.build_verifier = _CountingVerifier(cached=True)
    out = asyncio.run(
        sm.verify_big_step_build(
            theory_name="T",
            theory="theory T imports Main begin end",
            field="HOL",
            timeout=1.0,
        )
    )
    assert out == {"cached": True}


def test_real_verifier_cache_hit_skips_admit_and_miss_calls_it():
    from server.app.services.build_verify import BuildVerifier

    bv = BuildVerifier(max_concurrent_builds=2)
    assert bv.max_concurrent_builds == 2
    assert bv.inflight == 0
    calls = []

    async def admit():
        calls.append(1)
        raise MemoryPressure("no room")

    async def fake_run_build(**kwargs):
        from server.app.services.internal_models import BigStepExecuteResult

        return BigStepExecuteResult(success=True, execution_time=0.1)

    bv._run_build = fake_run_build  # type: ignore[method-assign]
    text = "theory T imports Main begin end"
    # miss: admit refuses before any build
    with pytest.raises(MemoryPressure):
        asyncio.run(
            bv.verify(
                theory_name="T", theory_text=text, field="HOL", timeout=1.0, admit=admit
            )
        )
    assert calls == [1]
    # populate the cache without a gate, then a hit never consults admit
    ok = asyncio.run(
        bv.verify(theory_name="T", theory_text=text, field="HOL", timeout=1.0)
    )
    assert ok.success is True
    hit = asyncio.run(
        bv.verify(
            theory_name="T", theory_text=text, field="HOL", timeout=1.0, admit=admit
        )
    )
    assert hit.success is True
    assert calls == [1]
    assert bv.inflight == 0


@pytest.mark.parametrize(
    "text,state,timed_out,expected",
    [
        ("*** Out of memory", "ok", False, "memory"),
        ("exception Size raised (line 3)", "ok", False, "memory"),
        ("Run out of store - interrupting threads", "warning", False, "memory"),
        ("Interrupt", "critical", False, "memory"),
        ("Interrupt", "warning", False, None),
        ("Interrupt", "critical", True, None),  # timeout classification wins
        ("Out of memory", "critical", True, None),  # never override timed_out
        ("Failed to apply initial proof method", "critical", False, None),
        ("", "critical", False, None),
        (None, "critical", False, None),
    ],
)
def test_classify_error_kind(text, state, timed_out, expected):
    assert (
        classify_error_kind(text, memory_state=state, timed_out=timed_out) == expected
    )
