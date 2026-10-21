"""fill_sorries routes (plan 2026-10-18 §3.1–3.2) and the job lifecycle hooks
(review B2): submit 202 / 409, poll bumps activity, cancel; the release and close
routes and the lease reaper cancel the job before the backend stops; the
interactive slot dependency uses the tier-aware slots. No Isabelle."""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from fastapi.testclient import TestClient

from server.app.dependencies import get_session_manager
from server.app.main import app
from server.app.services import fill_sorries as fs
from server.app.services.session_manager import SessionManager
from server.app.services.sledgehammer_slots import SledgehammerSlots

SID = "11111111-1111-1111-1111-111111111111"
LEASE = {"X-Lease-Id": "lease-1"}
TEXT = 'lemma a: "A" sorry\n'
REPORT = {
    "success": True,
    "commands": [
        {
            "i": 2,
            "kind": "lemma",
            "line": 1,
            "node_line": 2,
            "range": {"start": {"line": 2, "col": 1}, "end": {"line": 2, "col": 13}},
        },
        {
            "i": 4,
            "kind": "sorry",
            "line": 1,
            "node_line": 2,
            "range": {"start": {"line": 2, "col": 15}, "end": {"line": 2, "col": 20}},
        },
    ],
}


class _Session:
    """A session whose load returns a report with one sorry and whose
    sledgehammer step answers after a short delay (so a job is observable)."""

    def __init__(self, delay=0.2):
        self.session_id = uuid.UUID(SID)
        self.field, self.leased, self.in_use, self.ml_pid = "HOL", True, False, None
        self.activity, self.delay, self.calls = 0, delay, 0
        self.last_chunk_report = None

    def update_activity(self):
        self.activity += 1

    def load_document(self, text, thy_name, imports, timeout, report):
        self.last_chunk_report = {"report": REPORT}
        return type("R", (), {"success": True, "error": None})()

    def verify_chunk(self, chunk, timeout):
        return {"report": REPORT, "execution_time": 0.1}

    def sledgehammer_at_command(self, index, subgoal, timeout_s, max_proofs, provers):
        self.calls += 1
        import time as _t

        _t.sleep(self.delay)
        return {"found": True, "subgoals": 1, "results": ["Try this: by simp (1 ms)"]}

    def prover_alive(self):
        return None

    def check_prover_dead(self):
        return False


class _Monitor:
    last_state = "ok"
    last_snapshot = None


class _Manager(fs.FillJobMixin):
    def __init__(self, session):
        self.memory = _Monitor()
        self.memory_management_enabled = True
        self.session = session
        self.slots = SledgehammerSlots(self, 2)
        self.sledgehammer_sem = None

    def get_session(self, session_id, lease_id=None, require_lease=True):
        return self.session

    def memory_facts(self, snap=None):
        return {"state": "ok", "retry_after_s": 5}

    def refuse_heavy_if_critical(self, kind):
        return None

    def discard_dead(self, sid, reason="prover_died"):
        return True


@contextlib.asynccontextmanager
async def _no_lifespan(_app):
    """The job needs the TestClient's persistent loop (`with client:`), which
    runs the lifespan; the real one starts a gateway JVM. Swap it out."""
    yield


@pytest.fixture()
def api():
    saved = app.router.lifespan_context
    app.router.lifespan_context = _no_lifespan

    def _make(session):
        mgr = _Manager(session)
        app.dependency_overrides[get_session_manager] = lambda: mgr
        return TestClient(app, raise_server_exceptions=False), mgr

    try:
        yield _make
    finally:
        app.dependency_overrides.pop(get_session_manager, None)
        app.router.lifespan_context = saved


def test_submit_poll_cancel_and_in_flight_409(api):
    s = _Session(delay=0.5)
    client, mgr = api(s)
    with client:  # the TestClient's loop runs the job task
        r = client.post(
            f"/api/v1/sessions/{SID}/fill_sorries",
            json={"text": TEXT, "mode": "document"},
            headers=LEASE,
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert (
            body["status"] in ("queued", "running")
            and body["n"] == 1
            and body["eta_s"]["worst"] >= 60
        )
        assert body["load_success"] is True
        r2 = client.post(
            f"/api/v1/sessions/{SID}/fill_sorries", json={"text": TEXT}, headers=LEASE
        )
        assert r2.status_code == 409 and r2.json()["error"] == "fill_in_flight"
        before = s.activity
        st = client.get(f"/api/v1/sessions/{SID}/fill_sorries", headers=LEASE).json()
        assert s.activity == before + 1  # polling counts as activity (B2)
        assert st["n"] == 1 and "patched_text" in st and "sledgehammer_slots" in st
        c = client.delete(f"/api/v1/sessions/{SID}/fill_sorries", headers=LEASE).json()
        assert c["cancelled"] is True and c["status"] == "cancelled"


def test_submit_completes_and_patches(api):
    s = _Session(delay=0.05)
    client, mgr = api(s)
    with client:
        r = client.post(
            f"/api/v1/sessions/{SID}/fill_sorries",
            json={"text": TEXT, "mode": "chunk", "timeout_s": 5},
            headers=LEASE,
        )
        assert r.status_code == 202
        for _ in range(100):
            st = client.get(
                f"/api/v1/sessions/{SID}/fill_sorries", headers=LEASE
            ).json()
            if st["status"] == "done":
                break
            import time as _t

            _t.sleep(0.05)
        assert st["status"] == "done" and st["filled"] == 1
        assert st["patched_text"] == 'lemma a: "A" by simp\n'
        assert st["results"][0]["method"] == "by simp" and st["eta_s"] == {
            "worst": 0.0,
            "typical": 0.0,
        }
        assert s.calls == 1 and mgr.slots.running == 0


def test_no_sorry_returns_200_done(api):
    s = _Session()
    s.load_document = lambda *a, **k: (
        setattr(
            s,
            "last_chunk_report",
            {"report": {"success": True, "commands": [REPORT["commands"][0]]}},
        )
        or type("R", (), {"success": True})()
    )
    client, _ = api(s)
    with client:
        r = client.post(
            f"/api/v1/sessions/{SID}/fill_sorries",
            json={"text": "lemma a: A by simp\n"},
            headers=LEASE,
        )
        assert (
            r.status_code == 200
            and r.json()["n"] == 0
            and r.json()["patched_text"] == "lemma a: A by simp\n"
        )


def test_status_404_without_job(api):
    client, _ = api(_Session())
    with client:
        r = client.get(f"/api/v1/sessions/{SID}/fill_sorries", headers=LEASE)
        assert r.status_code == 404
        assert r.json()["error"] == "no_fill_job"  # top-level key (review aa96c6d S3)


def test_provers_pattern_validated(api):
    client, _ = api(_Session())
    with client:
        r = client.post(
            f"/api/v1/sessions/{SID}/fill_sorries",
            json={"text": TEXT, "provers": "e; rm -rf"},
            headers=LEASE,
        )
        assert r.status_code == 422


# -------------------------------------------- lifecycle: close / release / reaper


class _Backend:
    def __init__(self):
        self.closed = False

    def submit(self, fn):
        import concurrent.futures

        fut = concurrent.futures.Future()
        try:
            fut.set_result(fn())
        except Exception as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def close(self):
        self.closed = True


def _real_manager_with_job():
    """A real SessionManager (no gateway) holding one leased MemoryAwareSession
    with a slow fill job in flight."""
    from server.app.services.session_memory import MemoryAwareSession

    sm = SessionManager()
    sm.memory_management_enabled = False
    sess = MemoryAwareSession(
        session_id=uuid.UUID(SID),
        session_theories=[],
        loaded_theories=["x"],
        dependency_key="k",
        wrapper_theory=None,
        session_field="HOL",
        backend=_Backend(),
        task_group="default",
    )
    sess.acquire_lease("lease-1")
    sess.sledgehammer_at_command = lambda *a, **k: (__import__("time").sleep(0.3) or {"found": True, "subgoals": 1, "results": ["Try this: by simp (1 ms)"]})  # type: ignore[method-assign]
    with sm._lock:
        sm._lru[sess.session_id] = sess
    return sm, sess


class _RecordingManager(_Manager):
    """Records that the close/release routes cancel the job BEFORE closing."""

    def __init__(self, session):
        super().__init__(session)
        self.events = []

    def close_session(self, session_id, lease_id=None, require_lease=True):
        job = self.fill_job(session_id)
        self.events.append(("close", job.status if job else None))
        return True

    def release_session(self, session_id, lease_id):
        job = self.fill_job(session_id)
        self.events.append(("release", job.status if job else None))
        return True


def test_close_and_release_routes_cancel_the_job_first():
    saved = app.router.lifespan_context
    app.router.lifespan_context = _no_lifespan
    for route in ("close", "release"):
        s = _Session(delay=0.5)
        mgr = _RecordingManager(s)
        app.dependency_overrides[get_session_manager] = (lambda m: (lambda: m))(
            mgr
        )  # zero-arg: no request params
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                r = client.post(
                    f"/api/v1/sessions/{SID}/fill_sorries",
                    json={"text": TEXT, "mode": "chunk"},
                    headers=LEASE,
                )
                assert r.status_code == 202
                if route == "close":
                    assert (
                        client.delete(
                            f"/api/v1/sessions/{SID}", headers=LEASE
                        ).status_code
                        == 200
                    )
                else:
                    assert (
                        client.post(
                            f"/api/v1/sessions/{SID}/release", headers=LEASE
                        ).status_code
                        == 200
                    )
                assert mgr.events == [
                    (route, "cancelled")
                ], mgr.events  # cancelled before the backend call
        finally:
            app.dependency_overrides.pop(get_session_manager, None)


def test_release_and_reaper_cancel_the_job():
    sm, sess = _real_manager_with_job()

    async def go():
        job = sm.start_fill(
            sess,
            text=TEXT,
            mode="chunk",
            commands=REPORT["commands"],
            timeout_s=5,
            max_total_s=60,
            provers="",
        )
        await asyncio.sleep(0.01)
        # the reaper path: cleanup_once force-closes an abandoned lease -> cancel_job first
        sm.max_lease_age = 0.0
        sess.last_activity = 0.0
        closed = await sm.cleanup_once()
        assert (
            str(sess.session_id) in [str(c) for c in closed]
            or job.status == "cancelled"
        )
        assert job.status == "cancelled" and sess.backend.closed is True

    asyncio.run(go())

    sm2, sess2 = _real_manager_with_job()

    async def go2():
        job = sm2.start_fill(
            sess2,
            text=TEXT,
            mode="chunk",
            commands=REPORT["commands"],
            timeout_s=5,
            max_total_s=60,
            provers="",
        )
        await asyncio.sleep(0.01)
        sm2.release_session(
            sess2.session_id, "lease-1"
        )  # sync safety net sets the flag + cancels the task
        await asyncio.sleep(0.05)
        assert job.cancelled is True and sess2.leased is False

    asyncio.run(go2())


def test_interactive_slot_uses_tier_aware_slots(api):
    s = _Session()
    client, mgr = api(s)
    mgr.memory.last_state = "critical"
    with client:
        r = client.post(
            f"/api/v1/sessions/{SID}/sledgehammer_at",
            json={"line": 2, "subgoal": 1, "timeout_s": 5},
            headers=LEASE,
        )
        assert r.status_code == 503 and r.json()["error"] == "memory_pressure"
    mgr.memory.last_state = "ok"
    s.sledgehammer_at = lambda line, subgoal, timeout_s: {
        "found": True,
        "results": ["Try this: by simp (1 ms)"],
    }
    with client:
        r = client.post(
            f"/api/v1/sessions/{SID}/sledgehammer_at",
            json={"line": 2, "subgoal": 1, "timeout_s": 5},
            headers=LEASE,
        )
        assert (
            r.status_code == 200
            and r.json()["found"] is True
            and mgr.slots.running == 0
        )


# ------------------------------------------ review 4c253fe B1: validate, then cancel


class _LeaseCheckingManager(_RecordingManager):
    """The real manager raises on a wrong lease; the routes must do that BEFORE
    touching the job (B1)."""

    def get_session(self, session_id, lease_id=None, require_lease=True):
        from server.app.errors import SessionLeaseError

        if require_lease and lease_id != "lease-1":
            raise SessionLeaseError("Invalid lease token")
        return self.session


def test_wrong_or_missing_lease_cannot_cancel_the_job():
    saved = app.router.lifespan_context
    app.router.lifespan_context = _no_lifespan
    try:
        for route in ("close", "release"):
            s = _Session(delay=0.5)
            mgr = _LeaseCheckingManager(s)
            app.dependency_overrides[get_session_manager] = (lambda m: (lambda: m))(mgr)
            with TestClient(app, raise_server_exceptions=False) as client:
                assert (
                    client.post(
                        f"/api/v1/sessions/{SID}/fill_sorries",
                        json={"text": TEXT, "mode": "chunk"},
                        headers=LEASE,
                    ).status_code
                    == 202
                )
                for headers in ({"X-Lease-Id": "WRONG"}, {}):
                    if route == "close":
                        r = client.delete(f"/api/v1/sessions/{SID}", headers=headers)
                    else:
                        r = client.post(
                            f"/api/v1/sessions/{SID}/release", headers=headers
                        )
                    assert r.status_code in (400, 403), (
                        route,
                        headers,
                        r.status_code,
                        r.text,
                    )
                    assert mgr.fill_job(SID).status in ("queued", "running"), (
                        route,
                        headers,
                    )
                assert mgr.events == []  # neither close nor release was reached
                client.delete(f"/api/v1/sessions/{SID}/fill_sorries", headers=LEASE)
            app.dependency_overrides.pop(get_session_manager, None)
    finally:
        app.router.lifespan_context = saved


def test_registry_pruned_when_the_session_closes():
    sm, sess = _real_manager_with_job()

    async def go():
        job = sm.start_fill(
            sess,
            text=TEXT,
            mode="chunk",
            commands=REPORT["commands"],
            timeout_s=5,
            max_total_s=60,
            provers="",
        )
        await asyncio.sleep(0.01)
        await sm.cancel_job(sess.session_id)
        sm.close_session(sess.session_id, lease_id="lease-1")
        assert (
            job.status == "cancelled"
            and sm.fill_job(sess.session_id) is None
            and sm.fill_status(sess.session_id) is None
        )

    asyncio.run(go())
