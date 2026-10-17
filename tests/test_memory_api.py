"""API surface of the memory facts (plan §2.B part 3, §3.2):

- GET /api/v1/memory: facts + per-session rows (allowed fields only — no lease
  ids, no pids);
- GET / carries memory_source / memory_state / sessions_rss_mb;
- execution routes label results with ``error_kind`` ("memory" from the text
  classifier, "prover_died" from the dead-prover hook, which also discards the
  session) and never report success for a dead prover;
- heap build: already-building answers 409 even under memory pressure;
- client: ``memory_status()`` route and ``last_memory_state`` from the header.
No Isabelle: dependency overrides + a fake /proc.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from client.async_client import PoolAsyncClient
from server.app.dependencies import get_heap_pool, get_session_manager
from server.app.main import app
from server.app.services import memory_monitor as mm
from server.app.services.internal_models import SmallStepExecuteResult
from server.app.services.session_memory import MemoryAwareSession

SID = "11111111-1111-1111-1111-111111111111"
LEASE = {"X-Lease-Id": "lease-1"}

FACTS = {
    "state": "warning",
    "memory": {
        "used_mb": 10000.0,
        "limit_mb": 14336.0,
        "available_mb": 4336.0,
        "pressure_pct": 69.8,
        "source": "cgroup_v2",
    },
    "retry_after_s": 5,
    "builds_in_flight": {"bigstep": 0, "heap": 1},
    "sledgehammer_inflight": 0,
    "external_pressure": None,
    "evictable": [],
    "evictable_truncated": False,
    "leased": [],
    "leased_truncated": False,
}
LISTING = [
    {
        "session_id": SID,
        "label": "Foo.thy",
        "task_group": "default",
        "field": "HOL",
        "status": "active",
        "leased": True,
        "in_use": False,
        "idle_s": 12.0,
        "idle_expires_at": 1.0,
        "lease_expires_at": 2.0,
        "expires_in_s": 3.0,
        "rss_mb": 850.0,
        "heap_mb": 497.0,
        "mem_sampled_at": 9.0,
        "mem_probe_error": None,
        "lease_id": "MUST-NOT-APPEAR",
        "ml_pid": 4242,
        "dependency_key": "k",
        "theories": [],
    }
]


class _Monitor:
    last_state = "ok"


class _Backend:
    def __init__(self):
        self.raw = _Raw()

    def submit(self, fn):
        fut = concurrent.futures.Future()
        try:
            fut.set_result(fn())
        except Exception as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def close(self):
        return None


class _Raw:
    phase = "Ready"

    def mem_stats(self):
        return {}

    def session_phase(self):
        return self.phase

    def exit(self):
        return None


class _CannedSession(MemoryAwareSession):
    """A memory-aware session whose operations return canned results."""

    canned_step = SmallStepExecuteResult(
        success=True, output="ok", subgoals=[], execution_time=0.1
    )
    canned_chunk = {
        "report": {"success": True, "timed_out": False, "commands": []},
        "execution_time": 0.1,
    }

    def execute_command(self, command, timeout=30.0):  # type: ignore[override]
        return self.canned_step

    def verify_chunk(self, chunk, timeout=30.0):  # type: ignore[override]
        return self.canned_chunk

    def load_document(self, *a, **kw):  # type: ignore[override]
        return self.canned_step


def _session():
    return _CannedSession(
        session_id=uuid.UUID(SID),
        session_theories=[],
        loaded_theories=["x"],
        dependency_key="k",
        wrapper_theory=None,
        session_field="HOL",
        backend=_Backend(),
        task_group="default",
    )


class _Manager:
    def __init__(self, session=None, admit=True):
        self.memory = _Monitor()
        self.session = session
        self.discarded = []
        self.admit = admit
        self.idle_timeout, self.max_lease_age = 1800.0, 7200.0

    def get_session(self, session_id, lease_id=None, require_lease=True):
        if self.session is None:
            raise AssertionError("no session configured")
        self.session.acquire_lease(lease_id) if not self.session.leased else None
        return self.session

    def memory_facts(self, *a, **kw):
        return dict(FACTS)

    def list_sessions(self, *a, **kw):
        return list(LISTING)

    def get_lru_info(self):
        return {
            "memory_state": "warning",
            "memory_source": "cgroup_v2",
            "sessions_rss_mb": 850.0,
            "gateway_alive": True,
            "active_sessions": 1,
        }

    def discard_dead(self, session_id, reason="prover_died"):
        self.discarded.append(str(session_id))
        return True

    def require_memory_for_build(self, kind):
        if not self.admit:
            from server.app.errors import MemoryPressure

            raise MemoryPressure("no room", facts=dict(FACTS))


class _HeapPool:
    def __init__(self, status=None):
        self.status = status
        self.built = False

    def get(self, task_group, project):
        return {"status": self.status} if self.status else None

    async def build(self, *a, **kw):
        self.built = True
        raise AssertionError("build must not run in these tests")


@pytest.fixture()
def api():
    def _make(manager, heap_pool=None):
        app.dependency_overrides[get_session_manager] = lambda: manager
        app.dependency_overrides[get_heap_pool] = lambda: heap_pool or _HeapPool()
        return TestClient(app, raise_server_exceptions=False)

    try:
        yield _make
    finally:
        app.dependency_overrides.pop(get_session_manager, None)
        app.dependency_overrides.pop(get_heap_pool, None)


# ------------------------------------------------------------------ /memory + /


def test_memory_endpoint_shape_and_field_whitelist(api):
    client = api(_Manager())
    resp = client.get("/api/v1/memory")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "warning" and body["builds_in_flight"]["heap"] == 1
    (row,) = body["sessions"]
    assert (
        row["rss_mb"] == 850.0 and row["expires_in_s"] == 3.0 and row["leased"] is True
    )
    assert "lease_id" not in row and "ml_pid" not in row and "dependency_key" not in row
    assert "MUST-NOT-APPEAR" not in resp.text


def test_root_health_carries_memory_tier(api):
    body = api(_Manager()).get("/").json()
    assert body["memory_state"] == "warning"
    assert body["memory_source"] == "cgroup_v2"
    assert body["sessions_rss_mb"] == 850.0


# -------------------------------------------------------- error_kind labelling


def test_command_out_of_memory_is_labelled(api):
    s = _session()
    s.canned_step = SmallStepExecuteResult(
        success=False, error="*** Out of memory", subgoals=[], execution_time=0.1
    )
    client = api(_Manager(s))
    body = client.post(
        f"/api/v1/sessions/{SID}/commands", json={"command": "by auto"}, headers=LEASE
    ).json()
    assert body["success"] is False and body["error_kind"] == "memory"


def test_command_ordinary_error_has_no_kind(api):
    s = _session()
    s.canned_step = SmallStepExecuteResult(
        success=False,
        error="Failed to apply initial proof method",
        subgoals=[],
        execution_time=0.1,
    )
    body = (
        api(_Manager(s))
        .post(
            f"/api/v1/sessions/{SID}/commands",
            json={"command": "by auto"},
            headers=LEASE,
        )
        .json()
    )
    assert body["error_kind"] is None


def test_dead_prover_overrides_stale_success_and_discards(api, tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "_PROC_ROOT", str(tmp_path / "proc-empty"))
    (tmp_path / "proc-empty").mkdir()
    s = _session()
    s.record_mem_stats({"pid": "999", "size_heap": "1"})  # pid known, process absent
    mgr = _Manager(s)
    body = (
        api(mgr)
        .post(
            f"/api/v1/sessions/{SID}/commands",
            json={"command": "by simp"},
            headers=LEASE,
        )
        .json()
    )
    assert body["success"] is False
    assert body["error_kind"] == "prover_died"
    assert "discarded" in body["error"]
    assert mgr.discarded == [SID]


def test_dead_prover_detected_by_session_phase_when_pid_unknown(api):
    """B1: before the first probe the pid is unknown; Isabelle's phase is the signal."""
    s = _session()
    s.backend.raw.phase = (
        "Terminated(Process_Result(137,List(),List(),8.1s elapsed time))"
    )
    mgr = _Manager(s)
    body = (
        api(mgr)
        .post(
            f"/api/v1/sessions/{SID}/commands",
            json={"command": "by simp"},
            headers=LEASE,
        )
        .json()
    )
    assert body["success"] is False and body["error_kind"] == "prover_died"
    assert mgr.discarded == [SID]


def test_enter_theory_reports_dead_prover(api):
    s = _session()
    s.enter_thy = lambda *a, **kw: None  # type: ignore[method-assign]
    s.backend.raw.phase = "Terminated(x)"
    body = (
        api(_Manager(s))
        .post(f"/api/v1/sessions/{SID}/enter_theory/T", headers=LEASE)
        .json()
    )
    assert body["success"] is False and body["error_kind"] == "prover_died"


def test_chunk_error_message_is_classified(api):
    s = _session()
    s.canned_chunk = {
        "report": {
            "success": False,
            "timed_out": False,
            "commands": [
                {"i": 0, "line": 1, "kind": "lemma", "status": "ok", "messages": []},
                {
                    "i": 1,
                    "line": 2,
                    "kind": "by",
                    "status": "failed",
                    "messages": [
                        {"sev": "error", "text": "exception Size raised (line 2)"}
                    ],
                },
            ],
        },
        "execution_time": 0.2,
    }
    body = (
        api(_Manager(s))
        .post(
            f"/api/v1/sessions/{SID}/verify_chunk",
            json={"chunk": "lemma x: True by simp"},
            headers=LEASE,
        )
        .json()
    )
    assert body["success"] is False and body["error_kind"] == "memory"


def test_chunk_timeout_is_never_relabelled(api):
    s = _session()
    s.canned_chunk = {
        "report": {
            "success": False,
            "timed_out": True,
            "commands": [
                {
                    "i": 0,
                    "line": 1,
                    "kind": "by",
                    "status": "running",
                    "messages": [{"sev": "error", "text": "Interrupt"}],
                }
            ],
        },
        "execution_time": 30.0,
    }
    mgr = _Manager(s)
    mgr.memory.last_state = "critical"
    body = (
        api(mgr)
        .post(
            f"/api/v1/sessions/{SID}/verify_chunk",
            json={"chunk": "lemma x: True by auto"},
            headers=LEASE,
        )
        .json()
    )
    assert body["timed_out"] is True and body["error_kind"] is None


def test_chunk_dead_prover(api, tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "_PROC_ROOT", str(tmp_path / "proc-empty"))
    (tmp_path / "proc-empty").mkdir()
    s = _session()
    s.record_mem_stats({"pid": "999", "size_heap": "1"})
    mgr = _Manager(s)
    body = (
        api(mgr)
        .post(
            f"/api/v1/sessions/{SID}/verify_chunk",
            json={"chunk": "lemma x: True by simp"},
            headers=LEASE,
        )
        .json()
    )
    assert body["success"] is False and body["error_kind"] == "prover_died"
    assert mgr.discarded == [SID]


def test_document_load_carries_error_kind(api):
    s = _session()
    s.canned_step = SmallStepExecuteResult(
        success=False, error="Run out of store", subgoals=[], execution_time=0.1
    )
    body = (
        api(_Manager(s))
        .put(
            f"/api/v1/sessions/{SID}/document",
            json={"text": "theory T imports Main begin end"},
            headers=LEASE,
        )
        .json()
    )
    assert body["error_kind"] == "memory"


# ------------------------------------------------------------- heap ordering


def test_heap_build_in_progress_beats_memory_gate(api):
    from pathlib import Path

    project = str(
        Path(__file__).resolve().parents[1] / "examples" / "heap_demo_project"
    )
    client = api(_Manager(admit=False), _HeapPool(status="building"))
    resp = client.post(
        "/api/v1/heaps/build", json={"task_group": "alpha", "project": project}
    )
    assert resp.status_code == 409


# ------------------------------------------------------------------- client


def test_client_memory_status_route_and_header():
    recorded = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append((request.method, request.url.path))
        return httpx.Response(
            200, json={"state": "critical"}, headers={"X-Memory-State": "critical"}
        )

    c = PoolAsyncClient("http://gym")
    c.client = httpx.AsyncClient(
        base_url="http://gym", transport=httpx.MockTransport(handler)
    )
    assert c.last_memory_state == "unknown"
    out = asyncio.run(c.memory_status())
    assert out == {"state": "critical"}
    assert recorded == [("GET", "/api/v1/memory")]
    assert c.last_memory_state == "critical"
