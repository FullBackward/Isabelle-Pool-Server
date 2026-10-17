"""HTTP contract of the memory facts (plan §3.1 / §3.1a, batch 2):

- every 503 body carries an ``error`` key (memory_pressure / pool_full /
  gateway_unavailable) and a Retry-After header;
- a raised ``MemoryPressure`` resolves to ITS handler (structured facts), not
  the parent PoolExhausted prose body (plan-review S8);
- every response carries ``X-Memory-State`` from the monitor's cached tier.
No Isabelle: temporary routes raise the exceptions; the manager is a stub.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.app.errors import GatewayUnavailable, MemoryPressure, PoolExhausted
from server.app.main import app

FACTS = {
    "state": "critical",
    "memory": {
        "used_mb": 12600.0,
        "limit_mb": 14336.0,
        "available_mb": 1200.0,
        "pressure_pct": 88.0,
        "source": "cgroup_v2",
    },
    "retry_after_s": 5,
    "evictable": [
        {
            "session_id": "e1",
            "label": "idle",
            "task_group": "default",
            "rss_mb": 2100.0,
            "idle_s": 410.0,
            "leased": False,
            "in_use": False,
        }
    ],
    "leased": [
        {
            "session_id": "l1",
            "label": "mine",
            "task_group": "default",
            "rss_mb": 3100.0,
            "idle_s": 900.0,
            "leased": True,
            "in_use": False,
        }
    ],
    "builds_in_flight": {"bigstep": 1, "heap": 0},
    "sledgehammer_inflight": 0.0,
    "external_pressure": None,
}


@app.get("/__test_memory_pressure")
async def _raise_memory_pressure():
    raise MemoryPressure("cannot create session: memory pressure too high", facts=FACTS)


@app.get("/__test_pool_full")
async def _raise_pool_full():
    raise PoolExhausted("Session pool is full")


@app.get("/__test_gateway_down")
async def _raise_gateway_down():
    raise GatewayUnavailable("gateway unresponsive")


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_memory_pressure_resolves_to_structured_body(client):
    resp = client.get("/__test_memory_pressure")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"] == "memory_pressure"
    assert body["state"] == "critical"
    assert body["memory"]["pressure_pct"] == 88.0
    assert body["evictable"][0]["session_id"] == "e1"
    assert body["leased"][0]["label"] == "mine"
    assert body["builds_in_flight"] == {"bigstep": 1, "heap": 0}
    assert "memory pressure too high" in body["detail"]
    assert resp.headers["Retry-After"] == "5"
    # the parent handler's prose-only shape must NOT have won
    assert "evictable" in body


def test_no_lease_ids_in_memory_facts(client):
    text = client.get("/__test_memory_pressure").text
    assert "lease_id" not in text  # `leased` (a flag) is fine; the token never is


def test_pool_full_body_has_error_key(client):
    resp = client.get("/__test_pool_full")
    assert resp.status_code == 503
    assert resp.json()["error"] == "pool_full"
    assert resp.headers["Retry-After"] == "5"


def test_gateway_unavailable_body_has_error_key(client):
    resp = client.get("/__test_gateway_down")
    assert resp.status_code == 503
    assert resp.json()["error"] == "gateway_unavailable"
    assert resp.headers["Retry-After"] == "10"


def test_memory_state_header_unknown_without_manager(client):
    had = getattr(app.state, "session_manager", None)
    if had is not None:
        del app.state.session_manager
    try:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.headers["X-Memory-State"] == "unknown"
    finally:
        if had is not None:
            app.state.session_manager = had


def test_memory_state_header_reflects_monitor(client):
    class _Monitor:
        last_state = "warning"

    class _Manager:
        memory = _Monitor()

    had = getattr(app.state, "session_manager", None)
    app.state.session_manager = _Manager()
    try:
        for path in ("/healthz", "/__test_pool_full"):
            assert client.get(path).headers["X-Memory-State"] == "warning"
    finally:
        if had is None:
            del app.state.session_manager
        else:
            app.state.session_manager = had


def test_memory_state_header_tolerates_broken_manager(client):
    class _Explodes:
        def __getattr__(self, name):
            raise AssertionError(name)

    had = getattr(app.state, "session_manager", None)
    app.state.session_manager = _Explodes()
    try:
        assert client.get("/healthz").headers["X-Memory-State"] == "unknown"
    finally:
        if had is None:
            del app.state.session_manager
        else:
            app.state.session_manager = had
