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


class _ExplodingVerifier:
    async def verify(self, **kwargs):
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
    assert sm.build_verifier._sem._value == Server.MAX_CONCURRENT_BIGSTEP_BUILDS


@pytest.fixture()
def pressured_client():
    sm = _manager(admit=False)

    class _StubHeapPool:
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
