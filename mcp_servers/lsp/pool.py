"""File→session bindings (the copied-buffer sync model) + warm scratch pool.

Copied-buffer model: the MCP NEVER writes files. Each open file maps to a leased
session; EVERY file-scoped tool call re-reads the file from disk and, if the text
changed since the last sync, pushes it via ``load_document(text, report=true)``
(the lean-lsp-mcp reload_from_disk analog). A server-side 404 (session evicted)
rebinds transparently.

Bindings ACQUIRE (not create) their session, passing the file's own header
imports as the acquire `theories`: a session released by another binding with
the same dependency key (task_group + heap / same imports, default field) is
reused warm — every sync reloads via load_document, which resets the backend,
so dirty reuse is safe. Theories-keyed acquire is also what lets non-`Main`
parents resolve at all: sessions only see theories from their own
heap-ancestor chain plus what they were acquired with.

Scratch sessions (for multi_attempt / run_code) are leased sessions keyed by
context (task_group, heap_session, imports, field), kept warm and reused — each
use is a load_document reset, so candidates never pollute each other.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

from client.async_client import IsabelleGymAsyncClient

from ..common import GymClientMixin, is_not_found  # noqa: F401  (is_not_found re-exported)
from .config import Config


def canonical_path(file_path: str) -> str:
    return os.path.realpath(os.path.expanduser(file_path))


async def header_imports(client: IsabelleGymAsyncClient, text: str) -> List[str]:
    """Import names from a full .thy source's header (quotes stripped).

    Asks the server's canonical parser (`POST /api/v1/parse_theory_header`,
    comments stripped first so a leading `(* TASK: ... *)` comment can never
    pollute the imports — isabellegym-header-imports-issue.md). Going through
    the endpoint keeps the MCP free of server code and guarantees it parses
    headers exactly as the server it talks to does."""
    resp = await client.parse_theory_header(text)
    return list(resp.get("imports") or [])


def attempt_prefix(text: str, line: int) -> str:
    """The file's text BEFORE 1-based `line` (multi_attempt prefix truncation)."""
    lines = text.splitlines()
    return "\n".join(lines[: line - 1]) + "\n"


@dataclass
class FileBinding:
    file_path: str  # canonical
    session_id: str
    lease_id: str
    task_group: str
    heap_session: Optional[str] = None
    cached_text: Optional[str] = None
    last_report: Optional[Dict[str, Any]] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LspPool(GymClientMixin):
    def __init__(self) -> None:
        self._init_client(Config.GYM_URL, Config.HTTP_TIMEOUT)
        self._bindings: Dict[str, FileBinding] = {}
        self._bindings_lock = asyncio.Lock()
        # scratch pool: context key -> (queue of idle sessions, created count)
        self._scratch: Dict[Tuple, asyncio.Queue] = {}
        self._scratch_counts: Dict[Tuple, int] = {}
        self._scratch_lock = asyncio.Lock()

    # ---------------------------------------------------------- file bindings

    async def _create_binding(
        self, canon: str, task_group: Optional[str], heap_session: Optional[str],
        label: Optional[str],
    ) -> FileBinding:
        c = await self.client()
        group = task_group or Config.DEFAULT_TASK_GROUP
        # The session must be acquired WITH the file's own imports: gym REPL
        # sessions only see theories from their own heap-ancestor chain, and
        # only a theories-keyed acquire pulls extra parents (Complex_Main,
        # "HOL-Analysis.Derivative", …) into the session's dependency context.
        # Without this, every LSP tool call on a non-Main file ran against a
        # session that could not resolve the file's imports ("Undefined type
        # name" after a ~130 s doomed parent-resolution attempt).
        # The file may not exist yet (sync() reports FileNotFoundError later);
        # an unreadable file falls back to the empty-deps key, as before.
        imports: Optional[List[str]] = None
        try:
            with open(canon, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = None
        if text is not None:
            imports = await header_imports(c, text) or None
        # Acquire (not create): sessions released by other bindings with the
        # same dependency key (task_group + heap / imports, default field)
        # are reused WARM instead of building a fresh session per file. Safe
        # because every sync reloads via load_document, which resets the
        # backend. The label is re-applied on every acquire server-side, so a
        # reused session shows THIS file in the admin console, not its
        # previous holder.
        resp = await c.acquire_session(
            theories=imports,
            task_group=group, heap_session=heap_session, label=label or canon,
        )
        binding = FileBinding(
            file_path=canon,
            session_id=resp["session_id"],
            lease_id=resp["lease_id"],
            task_group=group,
            heap_session=heap_session,
        )
        async with self._bindings_lock:
            old = self._bindings.pop(canon, None)
            self._bindings[canon] = binding
        if old is not None:
            await self._safe_release(old)
        return binding

    async def get_binding(
        self, file_path: str, task_group: Optional[str] = None,
        heap_session: Optional[str] = None, label: Optional[str] = None,
    ) -> FileBinding:
        """The binding for a file; AUTO-OPENS with defaults on first use."""
        canon = canonical_path(file_path)
        binding = self._bindings.get(canon)
        if binding is None:
            binding = await self._create_binding(canon, task_group, heap_session, label)
        return binding

    async def sync(self, binding: FileBinding) -> bool:
        """Re-read the file from disk; if changed vs the cache, push it via
        load_document(report=true). Returns True if a reload happened.

        On 404 (session evicted/closed server-side) the binding is recreated
        once and the load retried. Raises FileNotFoundError for a missing file.
        """
        canon = binding.file_path
        if not os.path.isfile(canon):
            raise FileNotFoundError(f"file not found: {canon}")
        with open(canon, encoding="utf-8") as f:
            text = f.read()
        async with binding.lock:
            if text == binding.cached_text:
                return False
            c = await self.client()
            try:
                result = await c.load_document(
                    binding.session_id, text, report=True,
                    timeout=Config.LOAD_TIMEOUT, lease_id=binding.lease_id,
                )
            except httpx.HTTPStatusError as e:
                if not is_not_found(e):
                    raise
                # session evicted server-side: rebind once and retry
                fresh = await self._create_binding(
                    canon, binding.task_group, binding.heap_session, None)
                binding.session_id = fresh.session_id
                binding.lease_id = fresh.lease_id
                binding.cached_text = None
                result = await c.load_document(
                    binding.session_id, text, report=True,
                    timeout=Config.LOAD_TIMEOUT, lease_id=binding.lease_id,
                )
            binding.cached_text = text
            binding.last_report = result.get("report") or {}
            return True

    async def call(self, binding: FileBinding, fn, *args, **kwargs):
        """Run a client call against a binding's session, rebinding once on 404."""
        c = await self.client()
        try:
            return await fn(c, binding.session_id, *args, **kwargs)
        except httpx.HTTPStatusError as e:
            if not is_not_found(e):
                raise
        fresh = await self._create_binding(
            binding.file_path, binding.task_group, binding.heap_session, None)
        binding.session_id = fresh.session_id
        binding.lease_id = fresh.lease_id
        binding.cached_text = None  # force reload on next sync
        await self.sync(binding)
        return await fn(c, binding.session_id, *args, **kwargs)

    async def close_binding(self, file_path: str, destroy: bool = False) -> bool:
        canon = canonical_path(file_path)
        async with self._bindings_lock:
            binding = self._bindings.pop(canon, None)
        if binding is None:
            return False
        if destroy:
            await self._safe_destroy(binding)
        else:
            await self._safe_release(binding)
        return True

    async def _safe_release(self, binding: FileBinding) -> None:
        try:
            c = await self.client()
            await c.release_session(binding.session_id, lease_id=binding.lease_id)
        except Exception:
            pass

    async def _safe_destroy(self, binding: FileBinding) -> None:
        """Sanctioned immediate teardown: DELETE the binding's own session
        (authorized by the binding's own lease). The rebind-on-404 path
        recovers transparently if the file is opened again afterwards."""
        try:
            c = await self.client()
            await c.close_session(binding.session_id, lease_id=binding.lease_id)
        except Exception:
            pass

    # ---------------------------------------------------------- scratch pool

    def scratch_key(
        self, task_group: Optional[str], heap_session: Optional[str],
        imports: List[str], field: Optional[str],
    ) -> Tuple:
        return (
            task_group or Config.DEFAULT_TASK_GROUP,
            heap_session or "",
            tuple(sorted(imports)),
            field or Config.DEFAULT_FIELD,
        )

    async def acquire_scratch(self, key: Tuple) -> Tuple[str, str]:
        """Get a warm scratch session for the context (create if under the cap,
        else wait for one to be returned)."""
        async with self._scratch_lock:
            queue = self._scratch.setdefault(key, asyncio.Queue())
        try:
            return queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        async with self._scratch_lock:
            count = self._scratch_counts.get(key, 0)
            if count < Config.SCRATCH_POOL_SIZE:
                self._scratch_counts[key] = count + 1
                create = True
            else:
                create = False
        if not create:
            return await queue.get()
        task_group, heap_session, imports, field = key
        try:
            c = await self.client()
            resp = await c.create_session(
                theories=list(imports) or None,
                field=field,
                task_group=task_group,
                heap_session=heap_session or None,
                label=f"scratch:{task_group}:{heap_session or 'plain'}",
            )
            return resp["session_id"], resp["lease_id"]
        except BaseException:
            async with self._scratch_lock:
                self._scratch_counts[key] -= 1
            raise

    async def release_scratch(self, key: Tuple, session_id: str, lease_id: str) -> None:
        queue = self._scratch.get(key)
        if queue is not None:
            queue.put_nowait((session_id, lease_id))

    async def drop_scratch(self, key: Tuple, session_id: str, lease_id: str) -> None:
        """Discard a broken scratch session (e.g. 404) and close it best-effort."""
        async with self._scratch_lock:
            self._scratch_counts[key] = max(0, self._scratch_counts.get(key, 1) - 1)
        try:
            c = await self.client()
            await c.close_session(session_id, lease_id=lease_id)
        except Exception:
            pass
