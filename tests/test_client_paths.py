"""The async client must hit the server's real routes.

Uses httpx.MockTransport so no server is needed. Regression guard for the
2026-09-27 bug where `parse_theory_header` posted under /api/v1/sessions/
(matched /sessions/{session_id} → 405). Route paths are checked against the
server's OpenAPI path set when FastAPI is importable, so a renamed route on
either side fails here.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from client.async_client import IsabelleGymAsyncClient


def _client_with_recorder(recorded):
    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append((request.method, request.url.path, dict(request.headers)))
        body = {"theory_name": "T", "imports": ["Main"], "suggested_field": None,
                "session_id": "s1", "lease_id": "L1", "success": True, "sessions": [],
                "heaps": [], "source": "", "theory": "T", "subgoals": [], "count": 0,
                "proof_finished": True, "facts": []}
        return httpx.Response(200, json=body)

    c = IsabelleGymAsyncClient("http://gym")
    c.client = httpx.AsyncClient(base_url="http://gym", transport=httpx.MockTransport(handler))
    return c


CALLS = [
    ("parse_theory_header", ("x",), {}, "POST", "/api/v1/parse_theory_header"),
    ("create_session", (), {}, "POST", "/api/v1/sessions"),
    ("acquire_session", (), {}, "POST", "/api/v1/sessions/acquire"),
    ("list_sessions", (), {}, "GET", "/api/v1/sessions"),
    ("release_session", ("s1",), {"lease_id": "L1"}, "POST", "/api/v1/sessions/s1/release"),
    ("close_session", ("s1",), {"lease_id": "L1"}, "DELETE", "/api/v1/sessions/s1"),
    ("verify_chunk", ("s1", "lemma True by simp"), {"lease_id": "L1"}, "POST", "/api/v1/sessions/s1/verify_chunk"),
    ("load_document", ("s1", "theory T imports Main begin end"), {"lease_id": "L1"}, "PUT", "/api/v1/sessions/s1/document"),
    ("get_proof_state", ("s1",), {"lease_id": "L1"}, "GET", "/api/v1/sessions/s1/state"),
    ("get_source", ("s1",), {"lease_id": "L1"}, "GET", "/api/v1/sessions/s1/source"),
    ("get_local_facts", ("s1",), {"lease_id": "L1"}, "GET", "/api/v1/sessions/s1/facts/local"),
    ("list_heaps", (), {}, "GET", "/api/v1/heaps"),
    ("list_available_heaps", (), {}, "GET", "/api/v1/heaps/available"),
    ("heap_build", ("g", "/app/x"), {}, "POST", "/api/v1/heaps/build"),
]


@pytest.mark.parametrize("method,args,kwargs,http_method,path", CALLS, ids=[c[0] for c in CALLS])
def test_client_hits_expected_route(method, args, kwargs, http_method, path):
    recorded = []
    c = _client_with_recorder(recorded)
    asyncio.run(getattr(c, method)(*args, **kwargs))
    assert recorded, "no request sent"
    m, p, headers = recorded[-1]
    assert (m, p) == (http_method, path)
    if kwargs.get("lease_id"):
        assert headers.get("x-lease-id") == kwargs["lease_id"]


def test_client_paths_exist_on_server():
    """Cross-check every path above against the server's route table
    (path templates), so a rename on either side is caught."""
    fastapi = pytest.importorskip("fastapi")
    from server.app.api.v1.router import router

    # Go through OpenAPI rather than router.routes: newer FastAPI versions keep
    # lazy `_IncludedRouter` entries there (no .path), older ones flatten them.
    app = fastapi.FastAPI()
    app.include_router(router)
    templates = set(app.openapi()["paths"])
    for _, _, _, _, path in CALLS:
        # substitute concrete ids back into template form
        t = path.replace("/sessions/s1", "/sessions/{session_id}")
        assert t in templates, f"{t} is not a server route (have: {sorted(templates)[:5]}...)"
