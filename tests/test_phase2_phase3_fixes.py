"""Tests for the Phase 2/3 audit fixes (claude-work/2026-7-15(2)-research-server-code-audit).

Covers the pieces testable without a running Isabelle backend:
- A3: base SessionError handler preserves the error detail (HTTP 500 + message)
- A6: MemoryMonitor subtracts reclaimable inactive_file page cache
- A7: a concurrently-closed backend surfaces as SessionNotFound (404), not a 500
- A8: empty verify_chunk chunks are rejected at the schema (422)
- B2: MCP pool keys connection state by the session object, weakly
"""
from __future__ import annotations

import concurrent.futures
import gc
import uuid

import pytest

from server.app.errors import SessionError, SessionNotFound
from server.app.services import memory_monitor as mm
from server.app.services.session import _Isabelle_Session


# ---------------------------------------------------------------- A6: memory gate


def test_memory_monitor_subtracts_inactive_file(tmp_path, monkeypatch):
    current = tmp_path / "memory.current"
    stat = tmp_path / "memory.stat"
    limit = tmp_path / "memory.max"
    current.write_text(str(8 * 1024**3))          # 8 GiB charged
    stat.write_text("anon 1024\ninactive_file 2147483648\nactive_file 99\n")  # 2 GiB cache
    limit.write_text(str(16 * 1024**3))

    monkeypatch.setattr(mm, "_CG_V2_CURRENT", str(current))
    monkeypatch.setattr(mm, "_CG_V2_STAT", str(stat))
    monkeypatch.setattr(mm, "_CG_V2_MAX", str(limit))
    monkeypatch.setattr(mm, "_CG_V1_USAGE", str(tmp_path / "absent"))
    monkeypatch.setattr(mm, "_CG_V1_STAT", str(tmp_path / "absent"))

    snap = mm.MemoryMonitor().read()
    assert snap.used_bytes == 6 * 1024**3  # 8 GiB - 2 GiB reclaimable cache
    assert snap.limit_bytes == 16 * 1024**3


def test_memory_monitor_missing_stat_degrades_gracefully(tmp_path, monkeypatch):
    current = tmp_path / "memory.current"
    current.write_text("1000")
    monkeypatch.setattr(mm, "_CG_V2_CURRENT", str(current))
    monkeypatch.setattr(mm, "_CG_V2_STAT", str(tmp_path / "absent"))
    monkeypatch.setattr(mm, "_CG_V1_STAT", str(tmp_path / "absent"))
    monkeypatch.setattr(mm, "_CG_V1_USAGE", str(tmp_path / "absent"))
    assert mm.MemoryMonitor()._read_used() == 1000


# ------------------------------------------------- A7: concurrent close -> 404


class _ClosedBackendStub:
    """Mimics ThreadedBackend after close(): every submit is rejected."""

    def submit(self, fn):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        fut.set_exception(RuntimeError("Backend test is shutting down"))
        return fut


def _make_session(backend) -> _Isabelle_Session:
    return _Isabelle_Session(
        session_id=uuid.uuid4(),
        session_theories=[],
        session_field="HOL",
        backend=backend,
    )


def test_call_backend_translates_concurrent_close_to_session_not_found():
    session = _make_session(_ClosedBackendStub())
    with pytest.raises(SessionNotFound):
        session._call_backend(lambda: None, timeout=1)


def test_execute_command_propagates_session_not_found():
    session = _make_session(_ClosedBackendStub())
    with pytest.raises(SessionNotFound):
        session.execute_command("lemma x: True", timeout=1)


# --------------------------------------------------- A3: SessionError handler


def test_session_error_handler_returns_detail():
    from fastapi.testclient import TestClient

    from server.app.main import app

    @app.get("/__test_session_error")
    async def _boom():
        raise SessionError(error="TimeoutError: Backend call timed out after 1.0s")

    # TestClient without triggering lifespan (no Isabelle in unit tests).
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/__test_session_error")
    assert resp.status_code == 500
    assert "timed out" in resp.json()["detail"]


# ------------------------------------------------------ A8: empty chunk -> 422


def test_chunk_verify_request_rejects_empty_chunk():
    from pydantic import ValidationError

    from server.app.api.v1.schemas.API_models import ChunkVerifyRequest

    with pytest.raises(ValidationError):
        ChunkVerifyRequest(chunk="   \n  ")
    assert ChunkVerifyRequest(chunk="lemma x: True").chunk


# ------------------------------------------- B2: weak per-connection pool keys


def test_mcp_pool_conn_key_is_weak_and_not_recyclable():
    from mcp_stepwise_server.pool import Current, SessionPool

    pool = SessionPool()

    class FakeSession:
        pass

    class FakeCtx:
        def __init__(self, session):
            self.session = session

    sess = FakeSession()
    key = pool.conn_key(FakeCtx(sess))
    assert key is sess
    pool._current[key] = Current(session_id="s1", lease_id="l1", theory="T")
    assert pool._current.get(pool.conn_key(FakeCtx(sess))).session_id == "s1"

    # When the connection's session object dies, its entry vanishes with it —
    # a new connection can never inherit stale state via a recycled id().
    del sess, key
    gc.collect()
    assert len(pool._current) == 0


def test_mcp_pool_falls_back_to_stable_sentinel():
    from mcp_stepwise_server.pool import SessionPool

    pool = SessionPool()

    class NoSessionCtx:
        session = None

    k1 = pool.conn_key(NoSessionCtx())
    k2 = pool.conn_key(NoSessionCtx())
    assert k1 is k2 is pool._default_key


# ------------------------------------ pending_qed: MCP chunk rendering (rep3 fix)


def test_render_chunk_pending_qed_note():
    from mcp_servers.stepwise.app import _render_chunk

    report = {
        "success": True, "proof_open": True, "pending_qed": True,
        "used_sorry": False, "timed_out": False, "stuck_line": None,
        "execution_time": 0.5,
        "commands": [{"line": 1, "kind": "show", "status": "ok", "messages": []}],
    }
    out = _render_chunk(report, detail=False)
    assert "pending_qed=True" in out
    assert "bare `qed`" in out
    assert "proof is still OPEN" not in out  # pending_qed note takes precedence


def test_render_chunk_open_without_pending_qed_keeps_open_note():
    from mcp_servers.stepwise.app import _render_chunk

    report = {
        "success": True, "proof_open": True, "pending_qed": False,
        "used_sorry": False, "timed_out": False, "stuck_line": None,
        "execution_time": 0.5,
        "commands": [{"line": 1, "kind": "have", "status": "ok", "messages": []}],
    }
    out = _render_chunk(report, detail=False)
    assert "proof is still OPEN" in out
    assert "bare `qed`" not in out
