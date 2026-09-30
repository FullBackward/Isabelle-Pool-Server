"""Bug 22 / audit REPL-4: a checkpoint restore must never report success when the
backend applied nothing. The Python side now honours the backend's boolean and
drops an id the backend no longer holds. Fake backend, no Isabelle needed."""
from __future__ import annotations

import concurrent.futures
import uuid

from server.app.services.internal_models import SessionExecutionError
from server.app.services.session import _Isabelle_Session


class _FakeRaw:
    def __init__(self):
        self.next_id = 0
        self.known = set()
        self.restored = []

    def save_state(self):
        sid = self.next_id
        self.next_id += 1
        self.known.add(sid)
        return sid

    def restore_state(self, state_id):
        self.restored.append(state_id)
        return state_id in self.known   # the Scala side's all-or-nothing answer


class _FakeBackend:
    def __init__(self):
        self.raw = _FakeRaw()

    def submit(self, fn):
        fut: concurrent.futures.Future = concurrent.futures.Future()
        fut.set_result(fn())
        return fut


def _session():
    return _Isabelle_Session(session_id=uuid.uuid4(), session_theories=[],
                             session_field="HOL", backend=_FakeBackend())


def test_restore_known_checkpoint_succeeds():
    s = _session()
    cp = s.save_checkpoint()
    assert s.restore_checkpoint(cp.checkpoint_id) is True
    assert s.backend.raw.restored == [cp.checkpoint_id]


def test_restore_unknown_id_is_rejected_before_the_backend():
    s = _session()
    r = s.restore_checkpoint(42)
    assert isinstance(r, SessionExecutionError) and "not found" in r.error
    assert s.backend.raw.restored == []


def test_backend_rejection_is_an_error_and_drops_the_id():
    """Python knows the id, the backend has invalidated it (e.g. a document
    replace wiped its saved states): the old code returned True regardless."""
    s = _session()
    cp = s.save_checkpoint()
    s.backend.raw.known.clear()          # backend forgot it
    r = s.restore_checkpoint(cp.checkpoint_id)
    assert isinstance(r, SessionExecutionError)
    assert "unknown to the backend" in r.error and "nothing restored" in r.error
    assert cp.checkpoint_id not in s.checkpoints  # dropped, a retry is refused up front
    assert isinstance(s.restore_checkpoint(cp.checkpoint_id), SessionExecutionError)
    assert s.backend.raw.restored == [cp.checkpoint_id]  # not asked twice
