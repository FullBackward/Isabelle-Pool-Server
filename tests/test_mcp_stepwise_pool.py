"""Stepwise pool: the verify_batch fan-out answers a memory 503 with the
structured item (review d2bc679 B1) instead of the old opaque string."""

from __future__ import annotations

import asyncio

import httpx

from mcp_servers.stepwise.pool import SessionPool

FACTS = {
    "error": "memory_pressure",
    "state": "critical",
    "retry_after_s": 6,
    "memory": {"pressure_pct": 90.0, "available_mb": 900.0, "limit_mb": 14336.0},
    "evictable": [],
    "leased": [],
    "builds_in_flight": {"bigstep": 1, "heap": 0},
}


def _503():
    resp = httpx.Response(
        503, json=FACTS, request=httpx.Request("POST", "http://gym/acquire")
    )
    return httpx.HTTPStatusError("503", request=resp.request, response=resp)


class _Client:
    def __init__(self):
        self.n = 0
        self.closed = []

    async def acquire_session(self, theories, field, reuse_dirty=True, **kw):
        self.n += 1
        if self.n == 2:
            raise _503()  # the second create is refused for memory
        return {"session_id": f"s{self.n}", "lease_id": f"L{self.n}"}

    async def enter_theory(self, sid, name, imports=None, lease_id=None):
        return {"success": True}

    async def verify_chunk(self, sid, chunk, timeout=None, lease_id=None):
        return {
            "success": True,
            "timed_out": False,
            "commands": [{"status": "ok"}],
            "execution_time": 0.1,
        }

    async def close_session(self, sid, lease_id=None):
        self.closed.append(sid)
        return {}


def test_verify_batch_item_carries_memory_pressure_and_advice():
    pool = SessionPool()
    pool._client = _Client()
    items = [
        {"name": "A", "chunk": "lemma a: True by simp"},
        {"name": "B", "chunk": "lemma b: True by simp"},
    ]
    out = asyncio.run(
        pool.verify_batch(items, max_parallel=1, timeout=5.0, owner="conn")
    )
    a, b = sorted(out, key=lambda r: r["name"])
    assert a["success"] is True
    assert b["error"] == "memory_pressure" and b["state"] == "critical"
    assert b["advice"] and any(
        "tell the user" in x or "Retry" in x for x in b["advice"]
    )
    assert "HTTPStatusError" not in str(b)
    assert pool.registry.all() == [] and pool._client.closed == ["s1"]  # nothing leaked
