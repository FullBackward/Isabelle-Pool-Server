"""enter_theory refuses imports the session's theories do not cover (owner
decision D7, training-metrics plan v1.5; review afd3de9 S1): IPS fixes a
session's context at acquisition, so such a request used to be accepted and the
theory checked without its imports. The coverage answer comes from the backend
(Isabelle's import graph, Repl_Session.uncovered_imports); here it is stubbed.
"""

from __future__ import annotations

import concurrent.futures

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed (container-only test)")

from fastapi.testclient import TestClient  # noqa: E402

from server.app.api.v1.routes import execution  # noqa: E402
from server.app.dependencies import get_session_manager  # noqa: E402
from server.app.main import app  # noqa: E402
from server.app.services.import_coverage import uncovered_imports  # noqa: E402

SID = "11111111-1111-1111-1111-111111111111"
LEASE = {"X-Lease-Id": "lease-1"}


class _Raw:
    def __init__(self, answer: str):
        self.answer = answer
        self.calls = []

    def uncovered_imports(self, theories: str, requested: str) -> str:
        self.calls.append((theories, requested))
        return self.answer


class _Backend:
    def __init__(self, answer: str):
        self.raw = _Raw(answer)

    def submit(self, fn):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        fut.set_result(fn())
        return fut


class _Session:
    session_id = SID
    field = "HOL"

    def __init__(self, theories, answer):
        self.theories = theories
        self.backend = _Backend(answer)
        self.entered = []

    def enter_thy(self, name, timeout=None, imports=None):
        self.entered.append((name, imports))


class _Manager:
    def __init__(self, session):
        self.session = session

    def get_session(self, session_id, lease_id=None, require_lease=True):
        return self.session


def test_uncovered_imports_passes_lines_and_splits_the_answer():
    s = _Session(["Main", "HOL-Library.FSet"], "HOL.Real\nHOL-Analysis.Derivative\n")
    assert uncovered_imports(s, ["HOL.Real", "HOL-Analysis.Derivative", "Main"]) == [
        "HOL.Real",
        "HOL-Analysis.Derivative",
    ]
    assert s.backend.raw.calls == [
        ("Main\nHOL-Library.FSet", "HOL.Real\nHOL-Analysis.Derivative\nMain")
    ]
    assert uncovered_imports(_Session(["Main"], ""), ["Main"]) == []
    assert uncovered_imports(_Session([], "X"), ["X"]) == []  # nothing to judge


@pytest.fixture()
def route(monkeypatch):
    def run_hooks(fn, session, manager):
        return fn(), None

    monkeypatch.setattr(execution, "run_with_memory_hooks", run_hooks)

    def make(session):
        app.dependency_overrides[get_session_manager] = lambda: _Manager(session)
        return TestClient(app, raise_server_exceptions=False)

    yield make
    app.dependency_overrides.pop(get_session_manager, None)


def test_enter_theory_refuses_uncovered_imports_with_409(route):
    s = _Session(["Main"], "HOL.Real")
    r = route(s).post(
        f"/api/v1/sessions/{SID}/enter_theory/T",
        json={"imports": ["HOL.Real"]},
        headers=LEASE,
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "imports_not_covered" and body["uncovered"] == ["HOL.Real"]
    assert body["session_theories"] == ["Main"] and s.entered == []  # never entered


def test_enter_theory_with_covered_imports_proceeds(route):
    s = _Session(["Complex_Main"], "")
    r = route(s).post(
        f"/api/v1/sessions/{SID}/enter_theory/T",
        json={"imports": ["Main"]},
        headers=LEASE,
    )
    assert r.status_code == 200 and s.entered == [("T", ["Main"])]


def test_enter_theory_without_imports_is_not_checked(route):
    s = _Session(["Main"], "SHOULD-NOT-BE-ASKED")
    r = route(s).post(f"/api/v1/sessions/{SID}/enter_theory/T", headers=LEASE)
    assert r.status_code == 200 and s.backend.raw.calls == []
