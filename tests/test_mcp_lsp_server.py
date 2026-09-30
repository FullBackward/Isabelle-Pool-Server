"""Unit tests for mcp_lsp_server (file-sync pool logic) — no backend, no mcp
package: tests target pool.py with a fake IsabelleGymAsyncClient.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from mcp_servers.lsp.pool import (
    LspPool,
    attempt_prefix,
    canonical_path,
    header_imports,
)


class FakeClient:
    """Stub for IsabelleGymAsyncClient — records calls, programmable 404s.

    Emulates the server's acquire semantics: released sessions go back to an
    idle pool and the next acquire reuses one (warm) before creating new."""

    def __init__(self):
        self.created = []
        self.acquired = []
        self.released = []
        self.closed = []
        self.loads = []
        self.idle = []  # (session_id, lease_id) released back to the pool
        self.dirty = set()  # sessions that have loaded a document (command history)
        self._counter = 0
        self.fail_next_load_404 = False

    async def create_session(self, theories=None, field="HOL", task_group=None,
                             heap_session=None, project=None, label=None):
        self._counter += 1
        self.created.append({
            "theories": theories, "field": field, "task_group": task_group,
            "heap_session": heap_session, "label": label,
        })
        return {"session_id": f"s{self._counter}", "lease_id": f"L{self._counter}"}

    async def acquire_session(self, theories=None, field="HOL", reuse_dirty=True,
                              task_group=None, heap_session=None, project=None,
                              label=None):
        self.acquired.append({
            "theories": theories, "field": field, "reuse_dirty": reuse_dirty,
            "task_group": task_group, "heap_session": heap_session,
            "label": label,
        })
        # Server rule: a session with command history (every load_document
        # records one) is DIRTY and is skipped when reuse_dirty is False.
        for i in range(len(self.idle) - 1, -1, -1):
            session_id, lease_id = self.idle[i]
            if reuse_dirty or session_id not in self.dirty:
                del self.idle[i]
                return {"session_id": session_id, "lease_id": lease_id, "reused": True}
        self._counter += 1
        return {
            "session_id": f"s{self._counter}",
            "lease_id": f"L{self._counter}",
            "reused": False,
        }

    async def release_session(self, session_id, lease_id=None):
        self.released.append(session_id)
        self.idle.append((session_id, lease_id))
        return {"success": True}

    async def close_session(self, session_id, lease_id=None):
        self.closed.append(session_id)
        return {"success": True}

    async def parse_theory_header(self, text):
        # Emulates POST /api/v1/parse_theory_header with the server's own
        # parser (tests may import server code; the MCP itself may not).
        from server.app.services.theory_parsing import parse_theory_header
        name, imports = parse_theory_header(text)
        return {"theory_name": name, "imports": imports, "suggested_field": None}

    async def load_document(self, session_id, text, thy_name=None, imports=None,
                            timeout=None, report=False, lease_id=None):
        if self.fail_next_load_404:
            self.fail_next_load_404 = False
            raise httpx.HTTPStatusError(
                "404",
                request=httpx.Request("PUT", "http://test"),
                response=httpx.Response(404),
            )
        self.loads.append({"session_id": session_id, "text": text, "report": report})
        self.dirty.add(session_id)
        return {"success": True, "report": {"success": True, "commands": []}}

    async def goals_at_line(self, session_id, line, lease_id=None):
        return {"found": True, "goals_after": ["True"]}


def _pool_with_fake():
    pool = LspPool()
    pool._client = FakeClient()
    return pool


def test_sync_unchanged_file_no_reload(tmp_path):
    f = tmp_path / "T.thy"
    f.write_text("theory T imports Main begin\nlemma t: True by simp\nend\n")
    pool = _pool_with_fake()

    async def run():
        binding = await pool.get_binding(str(f))
        first = await pool.sync(binding)   # first sync loads
        second = await pool.sync(binding)  # unchanged: no reload
        return first, second

    first, second = asyncio.run(run())
    assert first is True and second is False
    assert len(pool._client.loads) == 1
    assert pool._client.loads[0]["report"] is True


def test_sync_changed_file_reloads_once(tmp_path):
    f = tmp_path / "T.thy"
    f.write_text("theory T imports Main begin\nlemma t: True by simp\nend\n")
    pool = _pool_with_fake()

    async def run():
        binding = await pool.get_binding(str(f))
        await pool.sync(binding)
        f.write_text("theory T imports Main begin\nlemma t: True by simp\nlemma u: True by simp\nend\n")
        return await pool.sync(binding)

    assert asyncio.run(run()) is True
    assert len(pool._client.loads) == 2
    assert "lemma u" in pool._client.loads[1]["text"]


def test_sync_missing_file_clean_error(tmp_path):
    pool = _pool_with_fake()

    async def run():
        binding = await pool.get_binding(str(tmp_path / "Nope.thy"))
        await pool.sync(binding)

    with pytest.raises(FileNotFoundError):
        asyncio.run(run())


def test_auto_open_defaults(tmp_path):
    f = tmp_path / "T.thy"
    f.write_text("theory T imports Main begin\nlemma t: True by simp\nend\n")
    pool = _pool_with_fake()
    binding = asyncio.run(pool.get_binding(str(f)))
    acquired = pool._client.acquired[0]
    assert acquired["task_group"] == "default"
    assert acquired["heap_session"] is None
    assert acquired["reuse_dirty"] is False  # clean-only reuse (Bug 21 / MCP-4)
    assert acquired["label"] == canonical_path(str(f))
    assert binding.session_id == "s1"


def test_binding_reuse_is_clean_only(tmp_path):
    """Bug 21 / MCP-4: a released binding whose session LOADED a document (dirty)
    is NOT handed to the next binding — it would carry the previous file's
    document; a released session that never loaded anything is reused warm."""
    f1, f2, f3 = (tmp_path / n for n in ("A.thy", "B.thy", "C.thy"))
    for f in (f1, f2, f3):
        f.write_text(f"theory {f.stem} imports Main begin\nend\n")
    pool = _pool_with_fake()

    async def run():
        b1 = await pool.get_binding(str(f1))
        await pool.sync(b1)                      # s1 loads A → dirty
        await pool.close_binding(str(f1))        # s1 released, dirty
        b2 = await pool.get_binding(str(f2))     # must NOT be s1
        await pool.close_binding(str(f2))        # s2 released CLEAN (never synced)
        b3 = await pool.get_binding(str(f3))     # may reuse s2 warm
        return b1, b2, b3

    b1, b2, b3 = asyncio.run(run())
    assert b2.session_id != b1.session_id
    assert b3.session_id == b2.session_id
    assert all(a["reuse_dirty"] is False for a in pool._client.acquired)


def test_rebind_on_404(tmp_path):
    f = tmp_path / "T.thy"
    f.write_text("theory T imports Main begin\nlemma t: True by simp\nend\n")
    pool = _pool_with_fake()

    async def run():
        binding = await pool.get_binding(str(f))
        await pool.sync(binding)
        assert binding.session_id == "s1"
        pool._client.fail_next_load_404 = True
        f.write_text("theory T imports Main begin\nlemma t2: True by simp\nend\n")
        reloaded = await pool.sync(binding)
        return binding, reloaded

    binding, reloaded = asyncio.run(run())
    assert reloaded is True
    assert binding.session_id == "s2"  # rebound to a fresh session
    assert pool._client.released == ["s1"]  # old session released on rebind
    assert pool._client.loads[-1]["session_id"] == "s2"


def test_close_binding_releases(tmp_path):
    f = tmp_path / "T.thy"
    f.write_text("theory T imports Main begin\nend\n")
    pool = _pool_with_fake()

    async def run():
        await pool.get_binding(str(f))
        return await pool.close_binding(str(f)), await pool.close_binding(str(f))

    first, second = asyncio.run(run())
    assert first is True and second is False
    assert pool._client.released == ["s1"]


def test_scratch_pool_reuse_and_cap():
    pool = _pool_with_fake()
    key = pool.scratch_key("default", None, ["Main"], None)

    async def run():
        a = await pool.acquire_scratch(key)
        b = await pool.acquire_scratch(key)
        # at cap (default 4? set explicitly) — release and re-acquire reuses
        await pool.release_scratch(key, *a)
        c = await pool.acquire_scratch(key)
        return a, b, c

    a, b, c = asyncio.run(run())
    assert c == a  # reused the released session
    assert len(pool._client.created) == 2  # only two ever created
    # key components flow into session creation
    created = pool._client.created[0]
    assert created["task_group"] == "default"
    assert created["theories"] == ["Main"]


def test_scratch_drop_decrements_count():
    pool = _pool_with_fake()
    key = pool.scratch_key("g", "Hp1", ["Main"], None)

    async def run():
        a = await pool.acquire_scratch(key)
        await pool.drop_scratch(key, *a)
        b = await pool.acquire_scratch(key)
        return a, b

    a, b = asyncio.run(run())
    assert a != b  # a fresh session after the drop
    assert pool._client.closed == [a[0]]
    assert pool._client.created[0]["heap_session"] == "Hp1"


def test_attempt_prefix_truncation():
    text = "theory T imports Main begin\nlemma a: True by simp\nlemma b: True by auto\nend\n"
    assert attempt_prefix(text, 3) == "theory T imports Main begin\nlemma a: True by simp\n"
    assert attempt_prefix(text, 1) == "\n"


def test_header_imports_parsing():
    # header_imports goes through the server endpoint (client.parse_theory_header)
    # so the MCP never imports server code; FakeClient emulates the endpoint.
    fake = FakeClient()
    text = 'theory T imports Main "HOL-Library.Multiset" Sub/Dir begin\nlemma a: True by simp'
    assert asyncio.run(header_imports(fake, text)) == ["Main", "HOL-Library.Multiset", "Sub/Dir"]
    assert asyncio.run(header_imports(fake, "lemma a: True by simp")) == []


# ------------------------------------------- theories-at-acquire (issue fix)


def test_binding_passes_file_imports_at_acquire(tmp_path):
    """Regression: a file importing beyond Main must be acquired WITH its
    imports, or the session cannot resolve them (Undefined type name …)."""
    f = tmp_path / "P.thy"
    f.write_text(
        'theory P imports Complex_Main "HOL-Analysis.Derivative" begin\n'
        'lemma p: "(x::real) + 0 = x" by simp\nend\n'
    )
    pool = _pool_with_fake()
    asyncio.run(pool.get_binding(str(f)))
    acquired = pool._client.acquired[0]
    assert acquired["theories"] == ["Complex_Main", "HOL-Analysis.Derivative"]


def test_binding_main_only_file_passes_main(tmp_path):
    """A Main-only file acquires with theories=["Main"] (one code path)."""
    f = tmp_path / "T.thy"
    f.write_text("theory T imports Main begin\nlemma t: True by simp\nend\n")
    pool = _pool_with_fake()
    asyncio.run(pool.get_binding(str(f)))
    assert pool._client.acquired[0]["theories"] == ["Main"]


def test_binding_missing_file_acquires_nothing(tmp_path):
    """Bug 19 / MCP-2: a path that does not exist must fail BEFORE any session
    is acquired or any binding registered — a typo used to pin a leased session
    under the bogus path until close / the lease reaper."""
    pool = _pool_with_fake()
    missing = str(tmp_path / "Nope.thy")

    with pytest.raises(FileNotFoundError):
        asyncio.run(pool.get_binding(missing))
    assert pool._client.acquired == [] and pool._client.created == []
    assert canonical_path(missing) not in pool._bindings

    # the file appearing later binds normally
    (tmp_path / "Nope.thy").write_text("theory Nope imports Main begin\nend\n")
    binding = asyncio.run(pool.get_binding(missing))
    assert binding.session_id == "s1" and pool._client.acquired[0]["theories"] == ["Main"]


def test_rebind_on_404_reparses_current_imports(tmp_path):
    """The 404-rebind path re-reads the file, so an imports change made while
    the session was evicted is picked up by the fresh acquire."""
    f = tmp_path / "T.thy"
    f.write_text("theory T imports Main begin\nlemma t: True by simp\nend\n")
    pool = _pool_with_fake()

    async def run():
        binding = await pool.get_binding(str(f))
        await pool.sync(binding)
        pool._client.fail_next_load_404 = True
        f.write_text(
            "theory T imports Complex_Main begin\nlemma t2: True by simp\nend\n"
        )
        await pool.sync(binding)
        return binding

    asyncio.run(run())
    assert pool._client.acquired[-1]["theories"] == ["Complex_Main"]


# ---------------------------------------------------- Bug 17 / MCP-1: scratch slots

def test_scratch_slot_returned_when_holder_is_cancelled(monkeypatch):
    """A cancelled holder used to leak its slot: with the pool at its cap the next
    acquire then waited forever. The bracket drops the slot on cancellation and
    the next acquire gets a fresh session immediately."""
    from mcp_servers.lsp.config import Config
    monkeypatch.setattr(Config, "SCRATCH_POOL_SIZE", 1)
    monkeypatch.setattr(Config, "SCRATCH_WAIT_TIMEOUT", 5.0)
    pool = _pool_with_fake()
    key = pool.scratch_key("default", None, ["Main"], None)

    async def run():
        entered = asyncio.Event()

        async def holder():
            async with pool.scratch_session(key):
                entered.set()
                await asyncio.sleep(3600)  # "verification in flight"

        task = asyncio.create_task(holder())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # slot is free again: this must not hang (bounded anyway) and must succeed
        return await asyncio.wait_for(pool.acquire_scratch(key), 2.0)

    sid, _ = asyncio.run(run())
    assert pool._client.closed == ["s1"]  # the cancelled holder's session was dropped
    assert sid == "s2"                     # and a fresh one created in its place


def test_scratch_bracket_drops_on_404_and_releases_otherwise():
    pool = _pool_with_fake()
    key = pool.scratch_key("default", None, ["Main"], None)

    async def run():
        # ordinary exception → released (warm) and reused next time
        with pytest.raises(ValueError):
            async with pool.scratch_session(key):
                raise ValueError("candidate failed to parse")
        reused, _ = await pool.acquire_scratch(key)
        await pool.release_scratch(key, reused, "L1")
        # 404 → dropped and closed; next acquire creates a fresh session
        with pytest.raises(httpx.HTTPStatusError):
            async with pool.scratch_session(key):
                raise httpx.HTTPStatusError(
                    "404", request=httpx.Request("PUT", "http://test"),
                    response=httpx.Response(404))
        fresh, _ = await pool.acquire_scratch(key)
        return reused, fresh

    reused, fresh = asyncio.run(run())
    assert reused == "s1" and fresh == "s2"
    assert pool._client.closed == ["s1"]


def test_scratch_wait_is_bounded(monkeypatch):
    from mcp_servers.lsp.config import Config
    monkeypatch.setattr(Config, "SCRATCH_POOL_SIZE", 1)
    monkeypatch.setattr(Config, "SCRATCH_WAIT_TIMEOUT", 0.2)
    pool = _pool_with_fake()
    key = pool.scratch_key("default", None, ["Main"], None)

    async def run():
        await pool.acquire_scratch(key)  # slot taken and never returned
        with pytest.raises(RuntimeError, match="scratch pool exhausted"):
            await pool.acquire_scratch(key)

    asyncio.run(run())


# ------------------------------------------- Bug 20 / MCP-3: single-flight bindings

def test_concurrent_first_calls_share_one_binding(tmp_path):
    """Two concurrent first calls on one file must create ONE session and return
    the same binding. Before the fix each acquired its own session and the second
    registration released the first one mid-use."""
    f = tmp_path / "Race.thy"
    f.write_text("theory Race imports Main begin\nend\n")
    pool = _pool_with_fake()
    slow_acquire = pool._client.acquire_session

    async def acquire_slowly(*a, **kw):
        await asyncio.sleep(0.05)  # a real acquire takes seconds; let the race happen
        return await slow_acquire(*a, **kw)

    pool._client.acquire_session = acquire_slowly

    async def run():
        return await asyncio.gather(pool.get_binding(str(f)), pool.get_binding(str(f)),
                                    pool.get_binding(str(f)))

    b1, b2, b3 = asyncio.run(run())
    assert b1 is b2 is b3
    assert len(pool._client.acquired) == 1
    assert pool._client.released == []


def test_failed_creation_is_not_cached_and_waiters_see_the_error(tmp_path):
    f = tmp_path / "Boom.thy"
    f.write_text("theory Boom imports Main begin\nend\n")
    pool = _pool_with_fake()
    calls = []

    async def failing_acquire(*a, **kw):
        calls.append(1)
        await asyncio.sleep(0.02)
        if len(calls) == 1:
            raise RuntimeError("server down")
        return {"session_id": "s9", "lease_id": "L9", "reused": False}

    pool._client.acquire_session = failing_acquire

    async def run():
        results = await asyncio.gather(pool.get_binding(str(f)), pool.get_binding(str(f)),
                                       return_exceptions=True)
        retry = await pool.get_binding(str(f))  # a later call retries cleanly
        return results, retry

    results, retry = asyncio.run(run())
    assert all(isinstance(r, RuntimeError) for r in results)  # both saw the one failure
    assert retry.session_id == "s9" and len(calls) == 2
    assert pool._creating == {}
