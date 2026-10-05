"""Lease-leak fix tests (isabelle-pool-server-lease-leak-issue.md):
the public listing must never carry lease ids, the admin listing is
token-gated, isabelle_close(destroy=…) is the sanctioned teardown, and the
MCP rebind path survives another client destroying its session.
No Isabelle backend needed.
"""
from __future__ import annotations

import asyncio
import threading
import types

import httpx
import pytest

from server.app.core.config import Server
from server.app.services.session_manager import SessionManager
from mcp_servers.lsp.pool import LspPool
from test_mcp_lsp_server import FakeClient


# ----------------------------------------------------------------- fixtures


class _FakeSession:
    def __init__(self, lease_id="lease-abc"):
        self.created_at = 0.0
        self.last_activity = 0.0
        self.status = types.SimpleNamespace(value="active")
        self.theories = ["Main"]
        self.loaded_theories = ["Main"]
        self.wrapper_theory = "IsabelleREPL"
        self.dependency_key = "k"
        self.field = "HOL"
        self.command_history = []
        self.verified_theories = 0
        self.in_use = False
        self.active_request_count = 0
        self.leased = True
        self.lease_id = lease_id
        self.label = "test"
        self.task_group = "default"


def _bare_manager():
    mgr = SessionManager.__new__(SessionManager)
    mgr._lock = threading.Lock()
    mgr._lru = {"sid-1": _FakeSession()}
    return mgr


# ------------------------------------------------------- listing: no secrets


def test_public_listing_has_no_lease_id():
    entries = _bare_manager().list_sessions()
    assert entries and "lease_id" not in entries[0]


def test_admin_listing_has_lease_id():
    entries = _bare_manager().list_sessions(include_lease=True)
    assert entries[0]["lease_id"] == "lease-abc"


# ------------------------------------------------------- admin endpoint gate


def _admin_endpoint():
    pytest.importorskip("fastapi")
    from server.app.api.v1.routes.sessions import list_sessions_admin

    return list_sessions_admin


def test_admin_endpoint_refuses_without_token(monkeypatch):
    endpoint = _admin_endpoint()
    from fastapi import HTTPException

    monkeypatch.setattr(Server, "ADMIN_TOKEN", "")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(x_admin_token="anything", session_manager=_bare_manager()))
    assert exc.value.status_code == 403


def test_admin_endpoint_refuses_wrong_token(monkeypatch):
    endpoint = _admin_endpoint()
    from fastapi import HTTPException

    monkeypatch.setattr(Server, "ADMIN_TOKEN", "secret-token")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(x_admin_token="wrong", session_manager=_bare_manager()))
    assert exc.value.status_code == 403


def test_admin_endpoint_serves_with_token(monkeypatch):
    endpoint = _admin_endpoint()
    monkeypatch.setattr(Server, "ADMIN_TOKEN", "secret-token")
    out = asyncio.run(
        endpoint(x_admin_token="secret-token", session_manager=_bare_manager())
    )
    assert out["sessions"][0]["lease_id"] == "lease-abc"


# ------------------------------------------- DELETE: unleased session close


def _recording_manager():
    class M:
        def __init__(self):
            self.calls = []

        def close_session(self, sid, **kwargs):
            self.calls.append((sid, kwargs))
            return True

    return M()


def test_delete_unleased_requires_admin_token(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import HTTPException
    from server.app.api.v1.routes.sessions import close_session as endpoint

    monkeypatch.setattr(Server, "ADMIN_TOKEN", "secret-token")
    mgr = _recording_manager()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            endpoint(
                "sid-1", x_lease_id=None, x_admin_token="wrong",
                session_manager=mgr,
            )
        )
    assert exc.value.status_code == 400
    assert mgr.calls == []


def test_delete_unleased_with_admin_token(monkeypatch):
    pytest.importorskip("fastapi")
    from server.app.api.v1.routes.sessions import close_session as endpoint

    monkeypatch.setattr(Server, "ADMIN_TOKEN", "secret-token")
    mgr = _recording_manager()
    out = asyncio.run(
        endpoint(
            "sid-1", x_lease_id=None, x_admin_token="secret-token",
            session_manager=mgr,
        )
    )
    assert out == {"success": True}
    assert mgr.calls == [("sid-1", {"require_lease": False})]


# ------------------------------------------------------- MCP destroy path


def _pool_with_fake():
    pool = LspPool()
    pool._client = FakeClient()
    return pool


def test_close_default_releases(tmp_path):
    f = tmp_path / "T.thy"
    f.write_text("theory T imports Main begin\nend\n")
    pool = _pool_with_fake()
    asyncio.run(pool.get_binding(str(f)))
    assert asyncio.run(pool.close_binding(str(f))) is True
    assert pool._client.released == ["s1"]
    assert pool._client.closed == []


def test_close_destroy_tears_down(tmp_path):
    f = tmp_path / "T.thy"
    f.write_text("theory T imports Main begin\nend\n")
    pool = _pool_with_fake()
    asyncio.run(pool.get_binding(str(f)))
    assert asyncio.run(pool.close_binding(str(f), destroy=True)) is True
    assert pool._client.closed == ["s1"]
    assert pool._client.released == []


def test_rebind_after_external_destroy(tmp_path):
    """Acceptance #4: another client destroys the bound session; the next
    MCP query must transparently rebind (404 -> fresh binding -> answer).

    Note the path this exercises: `sync` short-circuits when the text is
    unchanged, so recovery happens in `pool.call`'s rebind-on-404 wrapper,
    not in the sync path."""
    f = tmp_path / "T.thy"
    f.write_text("theory T imports Main begin\nlemma t: True by simp\nend\n")

    class DestroyingClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.destroyed = set()

        async def close_session(self, session_id, lease_id=None):
            await super().close_session(session_id, lease_id=lease_id)
            self.destroyed.add(session_id)

        async def goals_at_line(self, session_id, line, lease_id=None):
            if session_id in self.destroyed:
                raise httpx.HTTPStatusError(
                    "404",
                    request=httpx.Request("GET", "http://test"),
                    response=httpx.Response(404),
                )
            return {"found": True, "goals_after": ["True"]}

    pool = LspPool()
    pool._client = DestroyingClient()

    async def run():
        binding = await pool.get_binding(str(f))
        await pool.sync(binding)
        assert binding.session_id == "s1"
        # "another client" destroys our session
        await pool._client.close_session("s1", lease_id="L1")
        result = await pool.call(binding, lambda c, sid: c.goals_at_line(sid, 1))
        return result, binding

    result, binding = asyncio.run(run())
    assert result == {"found": True, "goals_after": ["True"]}
    assert binding.session_id == "s2"  # rebound to a fresh session
