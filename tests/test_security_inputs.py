"""Tracker TEST-1 (audit P0 item 10): security-input payloads END TO END at the
HTTP layer. test_input_guards.py proves the validators and models; this file
proves the WIRING — that a hostile payload sent to the real route is refused
(422 / 403 / 404), never accepted (2xx) and never a server error (5xx).

No Isabelle: a stub session manager is injected through FastAPI dependency
overrides so `LeasedSession` resolves (its sub-dependency runs BEFORE body
validation, so without the stub every text endpoint would answer 500 and the
guard would never be reached). Needs fastapi (container env); skips otherwise.
"""
from __future__ import annotations

import types

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed (container-only test)")

from fastapi.testclient import TestClient  # noqa: E402

from server.app.core.config import Server  # noqa: E402
from server.app.core.diagnostic_guard import validate_diagnostic_command  # noqa: E402
from server.app.dependencies import get_heap_pool, get_session_manager  # noqa: E402
from server.app.main import app  # noqa: E402

SID = "11111111-1111-1111-1111-111111111111"
LEASE = {"X-Lease-Id": "lease-1"}


class _StubSession:
    """Never reached by a rejected payload; explodes loudly if it is."""
    session_id = SID
    field = "HOL"

    def __getattr__(self, name):  # any execution attempt = the guard was bypassed
        raise AssertionError(f"guard bypassed: handler called session.{name}")


class _StubManager:
    def get_session(self, session_id, lease_id=None, require_lease=True):
        return _StubSession()

    def __getattr__(self, name):  # create/acquire with a hostile body = bypass
        raise AssertionError(f"guard bypassed: handler called session_manager.{name}")


class _StubHeapPool:
    def __getattr__(self, name):
        raise AssertionError(f"guard bypassed: handler called heap_pool.{name}")


@pytest.fixture()
def client():
    app.dependency_overrides[get_session_manager] = lambda: _StubManager()
    app.dependency_overrides[get_heap_pool] = lambda: _StubHeapPool()
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_session_manager, None)
        app.dependency_overrides.pop(get_heap_pool, None)


def _rejected(resp, *, allow=(422, 403, 404)):
    assert resp.status_code in allow, (resp.status_code, resp.text[:300])
    assert resp.status_code < 500


# ------------------------------------------------------- ML / code execution

ML_PAYLOADS = [
    'ML \\<open>OS.Process.system "id"\\<close>',
    "ML ‹OS.Process.system \"id\"›",
    'ML_val \\<open>1\\<close>',
    'ML_file "/etc/passwd"',
    'setup \\<open>fn thy => thy\\<close>',
    'lemma x: "True" by simp\nML \\<open>1\\<close>',           # second-command injection
    'lemma x: "True" by simp\n\nsetup \\<open>I\\<close>',
    "SML_file \"x.sml\"",
    "compile_generated_files _ external_files \"a\" in \"b\" where {}",
]


@pytest.mark.parametrize("payload", ML_PAYLOADS)
def test_commands_endpoint_refuses_code_execution(client, payload, monkeypatch):
    monkeypatch.setattr(Server, "ALLOW_ML_COMMANDS", False)
    _rejected(client.post(f"/api/v1/sessions/{SID}/commands",
                          json={"command": payload, "timeout": 5}, headers=LEASE))


@pytest.mark.parametrize("payload", ML_PAYLOADS)
def test_verify_chunk_endpoint_refuses_code_execution(client, payload, monkeypatch):
    monkeypatch.setattr(Server, "ALLOW_ML_COMMANDS", False)
    _rejected(client.post(f"/api/v1/sessions/{SID}/verify_chunk",
                          json={"chunk": payload, "timeout": 5}, headers=LEASE))


@pytest.mark.parametrize("payload", ML_PAYLOADS)
def test_document_endpoint_refuses_code_execution(client, payload, monkeypatch):
    monkeypatch.setattr(Server, "ALLOW_ML_COMMANDS", False)
    text = f"theory T imports Main begin\n{payload}\nend\n"
    _rejected(client.put(f"/api/v1/sessions/{SID}/document",
                         json={"text": text, "report": True, "timeout": 5}, headers=LEASE))


@pytest.mark.parametrize("payload", ML_PAYLOADS)
def test_bigstep_endpoint_refuses_code_execution(client, payload, monkeypatch):
    monkeypatch.setattr(Server, "ALLOW_ML_COMMANDS", False)
    _rejected(client.post("/api/v1/sessions/bigstep", json={
        "theory_name": "T", "dependencies": ["Main"],
        "theory": f"theory T imports Main begin\n{payload}\nend\n", "timeout": 5}))


DIAGNOSTIC_BAD = [
    "ML ‹1›",                       # not a diagnostic at all
    "thm foo\nML ‹OS.Process.system \"id\"›",   # allowlisted head, injected 2nd command
    "find_theorems name: ML",       # denylist token anywhere (documented trade-off)
    "lemma x: True",                # not read-only
    "apply simp",
    "print_theorems; setup ‹I›",
    "ML_file \"/etc/passwd\"",
    "",
]


@pytest.mark.parametrize("payload", DIAGNOSTIC_BAD)
def test_diagnostic_endpoint_refuses_non_diagnostics(client, payload):
    _rejected(client.post(f"/api/v1/sessions/{SID}/diagnostic",
                          json={"command": payload}, headers=LEASE))


@pytest.mark.parametrize("ok", ["thm conjI", "find_theorems \"_ + _\"", "print_theorems",
                                "term \"x + y\"", "typ nat", "unused_thms", "print_ML_antiquotations"])
def test_diagnostic_guard_allows_read_only_queries(ok):
    assert validate_diagnostic_command(ok) == ok


# -------------------------------------------------------- header injection

# NOTE: path-like names (`../Other/Thy`, `~~/src/HOL/Foo`, `$AFP/thys/X`) are
# legitimate Isabelle import syntax and are ACCEPTED by design (input_guards
# IMPORT_NAME_RE); they cannot break out of the generated header's quotes.
QUOTE_INJECTIONS = [
    'Main" "HOL-Library.Sublist',
    "Main\nML ‹1›",
    "Main begin ML ‹1› end theory X imports",
    "Main;",
]


@pytest.mark.parametrize("bad", ['T" imports Main begin ML ‹1› end', "T\nML ‹1›", "T;"])
def test_document_load_refuses_theory_name_injection(client, bad):
    _rejected(client.put(f"/api/v1/sessions/{SID}/document",
                         json={"text": "lemma x: True by simp", "thy_name": bad,
                               "imports": ["Main"], "report": True}, headers=LEASE))


@pytest.mark.parametrize("bad", ['T" imports Main begin ML ‹1› end', "T\nML ‹1›", "T;"])
def test_bigstep_refuses_theory_name_injection(client, bad):
    _rejected(client.post("/api/v1/sessions/bigstep", json={
        "theory_name": bad, "dependencies": ["Main"],
        "theory": "theory T imports Main begin\nend\n", "timeout": 5}))


@pytest.mark.parametrize("bad", QUOTE_INJECTIONS)
def test_session_create_refuses_import_injection(client, bad):
    _rejected(client.post("/api/v1/sessions", json={"theories": [bad], "field": "HOL"}))


@pytest.mark.parametrize("bad", QUOTE_INJECTIONS)
def test_session_acquire_refuses_import_injection(client, bad):
    _rejected(client.post("/api/v1/sessions/acquire", json={"theories": [bad], "field": "HOL"}))


@pytest.mark.parametrize("bad", ['T" imports Main begin ML ‹1› end', "T%0AML%20%E2%80%B91%E2%80%BA", "T;"])
def test_enter_theory_refuses_name_injection(client, bad):
    """The path segment is URL-decoded server-side: a newline travels as %0A (httpx
    refuses a raw one); `../T` is a legitimate path-like name and is not listed."""
    _rejected(client.post(f"/api/v1/sessions/{SID}/enter_theory/{bad}", headers=LEASE))


# ---------------------------------------------------- path traversal + admin

@pytest.mark.parametrize("group,project", [
    ("../etc", "/app"), ("g", "/etc/passwd"), ("g/../../x", "/app"), ("g", "../../../"),
])
def test_heap_build_refuses_traversal(client, group, project, monkeypatch):
    monkeypatch.setattr(Server, "ADMIN_TOKEN", "t")
    _rejected(client.post("/api/v1/heaps/build",
                          json={"task_group": group, "project": project},
                          headers={"X-Admin-Token": "t"}))


@pytest.mark.parametrize("path", [
    "/api/v1/heaps/../etc/passwd", "/api/v1/heaps/images/..%2F..%2Fetc",
    "/api/v1/heap_groups/..%2F..", "/api/v1/heaps/g/..%2F..%2Fetc%2Fpasswd",
])
def test_destructive_heap_routes_need_admin_token(client, path, monkeypatch):
    monkeypatch.setattr(Server, "ADMIN_TOKEN", "secret")
    _rejected(client.delete(path))                                    # no token
    _rejected(client.delete(path, headers={"X-Admin-Token": "wrong"}))  # wrong token


def test_admin_listing_needs_token(client, monkeypatch):
    monkeypatch.setattr(Server, "ADMIN_TOKEN", "secret")
    _rejected(client.get("/api/v1/admin/sessions"))
    _rejected(client.get("/api/v1/admin/sessions", headers={"X-Admin-Token": "nope"}))
