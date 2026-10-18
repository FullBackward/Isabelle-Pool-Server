"""fill_sorries stage 1a (plan 2026-10-18 §1.1 / review S1, B1): the sledgehammer
outcome classifier, the session mixin that hosts sledgehammer on a report index,
the memory-hooks move, and the `success` semantics of both sledgehammer routes.
No Isabelle: fake backends and dependency overrides."""

from __future__ import annotations

import concurrent.futures
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from server.app.dependencies import get_session_manager
from server.app.main import app
from server.app.services import success_checker as sc
from server.app.services.session_memory import MemoryAwareSession, SessionFillMixin

SID = "11111111-1111-1111-1111-111111111111"
LEASE = {"X-Lease-Id": "lease-1"}


# ------------------------------------------------------------------ classifier


@pytest.mark.parametrize(
    "lines,outcome,method",
    [
        (
            ["simp found a proof...", "simp: Try this: by simp (1 ms)"],
            "proved",
            "by simp",
        ),
        (["Try this: using assms by metis (6 ms)"], "proved", "using assms by metis"),
        (
            ["zipperposition: Try this: using assms by blast (0.8 ms)"],
            "proved",
            "using assms by blast",
        ),
        (["Try this: by (metis foo bar) (1.2 s)"], "proved", "by (metis foo bar)"),
        (["Try this: by (simp add: x) (auto)"], "proved", "by (simp add: x) (auto)"),
        (
            ["No proof found", "Timed out"],
            "none",
            None,
        ),  # a false goal prints both: sledgehammer's verdict wins
        (["Timed out"], "timeout", None),
        (["e: Timeout"], "timeout", None),
        (["error=No subgoal 3"], "error", None),
        ([], "none", None),
        (None, "none", None),
    ],
)
def test_classify_sledgehammer(lines, outcome, method):
    assert sc.classify_sledgehammer(lines) == (outcome, method)


# -------------------------------------------------------------- session mixin


class _Raw:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def sledgehammer_at_command(self, index, subgoal, timeout_s, max_proofs, provers):
        self.calls.append((index, subgoal, timeout_s, max_proofs, provers))
        return self.reply

    def mem_stats(self):
        return {}

    def exit(self):
        return None


class _Backend:
    def __init__(self, reply):
        self.raw = _Raw(reply)

    def submit(self, fn):
        fut = concurrent.futures.Future()
        try:
            fut.set_result(fn())
        except Exception as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def close(self):
        return None


def _session(reply):
    return MemoryAwareSession(
        session_id=uuid.uuid4(),
        session_theories=[],
        loaded_theories=["x"],
        dependency_key="k",
        wrapper_theory=None,
        session_field="HOL",
        backend=_Backend(reply),
        task_group="default",
    )


def test_session_is_fill_capable():
    assert issubclass(MemoryAwareSession, SessionFillMixin)


def test_sledgehammer_at_command_parses_and_accounts():
    reply = json.dumps(
        {"found": True, "subgoals": 2, "results": ["Try this: by simp (1 ms)"]}
    )
    s = _session(reply)
    out = s.sledgehammer_at_command(
        7, subgoal=2, timeout_s=20, max_proofs=1, provers="e vampire"
    )
    assert out == {
        "found": True,
        "subgoals": 2,
        "results": ["Try this: by simp (1 ms)"],
    }
    assert s.backend.raw.calls == [(7, 2, 20, 1, "e vampire")]
    assert s.in_use is False  # request accounting released


def test_sledgehammer_at_command_bad_reply():
    s = _session("not json")
    out = s.sledgehammer_at_command(0)
    assert (
        out["found"] is False
        and out["subgoals"] == -1
        and "unparseable" in out["error"]
    )
    s2 = _session(json.dumps({"found": False, "error": "no open goal at command 0"}))
    assert s2.sledgehammer_at_command(0)["subgoals"] == -1  # header missing -> -1


# ---------------------------------------------------------- memory hooks move


def test_memory_hooks_moved_and_reexported():
    from server.app.api.v1 import memory_hooks as api_hooks
    from server.app.services import memory_hooks as svc_hooks

    assert api_hooks.run_with_memory_hooks is svc_hooks.run_with_memory_hooks
    assert api_hooks.PROVER_DIED_TEXT == svc_hooks.PROVER_DIED_TEXT


# ---------------------------------------------------------------- routes


class _Monitor:
    last_state = "ok"


class _Session:
    session_id = uuid.UUID(SID)
    field = "HOL"
    leased = True
    in_use = False
    ml_pid = None

    def __init__(self, lines, at=None):
        self.lines = lines
        self.at = at

    def sledgehammer(self, timeout_s):
        return list(self.lines)

    def sledgehammer_at(self, line, subgoal, timeout_s):
        return self.at

    def prover_alive(self):
        return None

    def check_prover_dead(self):
        return False


class _Manager:
    def __init__(self, session):
        self.memory = _Monitor()
        self.session = session
        self.sledgehammer_sem = None
        self.memory_management_enabled = False

    def get_session(self, session_id, lease_id=None, require_lease=True):
        return self.session

    def refuse_heavy_if_critical(self, kind):
        return None

    def discard_dead(self, sid, reason="prover_died"):
        return True


@pytest.fixture()
def api():
    def _make(session):
        app.dependency_overrides[get_session_manager] = lambda: _Manager(session)
        return TestClient(app, raise_server_exceptions=False)

    try:
        yield _make
    finally:
        app.dependency_overrides.pop(get_session_manager, None)


def test_tip_sledgehammer_no_proof_is_not_success(api):
    body = (
        api(_Session(["No proof found", "Timed out"]))
        .post(
            f"/api/v1/sessions/{SID}/sledgehammer", json={"timeout_s": 5}, headers=LEASE
        )
        .json()
    )
    assert (
        body["success"] is False
        and body["outcome"] == "none"
        and body["method"] is None
    )
    assert body["suggestions"] == ["No proof found", "Timed out"]


def test_tip_sledgehammer_proof_is_success(api):
    body = (
        api(_Session(["simp: Try this: by simp (1 ms)"]))
        .post(
            f"/api/v1/sessions/{SID}/sledgehammer", json={"timeout_s": 5}, headers=LEASE
        )
        .json()
    )
    assert (
        body["success"] is True
        and body["outcome"] == "proved"
        and body["method"] == "by simp"
    )


def test_positional_found_requires_a_proof(api):
    at = {
        "found": True,
        "results": ["No proof found"],
    }  # the backend's flag is true whenever it answered
    body = (
        api(_Session([], at=at))
        .post(
            f"/api/v1/sessions/{SID}/sledgehammer_at",
            json={"line": 3, "subgoal": 1, "timeout_s": 5},
            headers=LEASE,
        )
        .json()
    )
    assert body["found"] is False and body["outcome"] == "none"
    at = {"found": True, "results": ["Try this: using assms by metis (6 ms)"]}
    body = (
        api(_Session([], at=at))
        .post(
            f"/api/v1/sessions/{SID}/sledgehammer_at",
            json={"line": 3, "subgoal": 1, "timeout_s": 5},
            headers=LEASE,
        )
        .json()
    )
    assert body["found"] is True and body["method"] == "using assms by metis"
    at = {"found": False, "error": "no open goal at line 3"}
    body = (
        api(_Session([], at=at))
        .post(
            f"/api/v1/sessions/{SID}/sledgehammer_at",
            json={"line": 3, "subgoal": 1, "timeout_s": 5},
            headers=LEASE,
        )
        .json()
    )
    assert body["found"] is False and body["outcome"] == "error" and body["error"]
