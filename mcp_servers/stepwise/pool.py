"""Per-MCP-connection leased-session pool + concurrent batch fan-out.

The "current session" is keyed by the MCP connection (``id(ctx.session)``) so concurrent
Streamable-HTTP clients are isolated and never share Isabelle state — this is what keeps us
from re-introducing the single-global-session limitation of other Isabelle MCP servers.
All Isabelle access goes through one shared ``PoolAsyncClient`` (httpx is safe for
concurrent requests on distinct sessions/leases).
"""
from __future__ import annotations

import asyncio
import weakref
from dataclasses import dataclass
from typing import Any, List, MutableMapping, Optional

from client.async_client import PoolAsyncClient

from ..common import GymClientMixin
from .config import Config


@dataclass
class Current:
    session_id: str
    lease_id: Optional[str]
    theory: str


class _StdioKey:
    """Weak-referenceable sentinel key for transports without a per-connection
    session object (e.g. stdio). Held strongly by the pool, so its entry lives
    for the pool's lifetime."""


class SessionPool(GymClientMixin):
    def __init__(self) -> None:
        self._init_client(Config.GYM_URL, Config.HTTP_TIMEOUT)
        # Keyed by the MCP session OBJECT (weakly): a dropped connection's
        # entry vanishes with its session, and a recycled id() can never bind
        # a new connection to a stale Current (the old id-string keys leaked
        # and were recyclable). The orphaned gym session behind a vanished
        # entry is reclaimed by the server's abandoned-lease force-close.
        self._current: "MutableMapping[Any, Current]" = weakref.WeakKeyDictionary()
        self._default_key = _StdioKey()  # stable sentinel for keyless transports
        self._lock = asyncio.Lock()

    def conn_key(self, ctx: Any) -> Any:
        """Per-connection key: the MCP session object itself (weak-keyed);
        falls back to a pool-owned sentinel (e.g. stdio)."""
        try:
            sess = ctx.session
            if sess is not None:
                weakref.ref(sess)  # verify weak-referenceable
                return sess
        except Exception:
            pass
        return self._default_key

    async def _begin_theory(self, name: str, imports: List[str], field: str):
        """Acquire a leased session and enter+begin the theory. The SERVER builds the
        correctly-quoted `theory ... begin` header from `imports` (see session.enter_thy),
        so the MCP layer never constructs Isar header text or worries about quoting.

        If enter_theory fails, the just-acquired session is closed before re-raising —
        otherwise it would stay exclusively leased (invisible to acquire_session) until
        the server's abandoned-lease reaper fires (MAX_LEASE_AGE, 2h)."""
        c = await self.client()
        # reuse_dirty=False: never accept a pooled session that has ANY command
        # history. A leaked dirty session once let an agent see a PREVIOUS
        # attempt's in-progress proof via source() (stepwise/run1 rep0,
        # 2026-07-10) — clean-only reuse closes that class regardless of how
        # the dirty session escaped its lease.
        sp = await c.acquire_session(list(imports), field, reuse_dirty=False)
        sid, lease = sp["session_id"], sp.get("lease_id")
        try:
            await c.enter_theory(sid, name, imports=list(imports), lease_id=lease)
        except BaseException:
            try:
                await c.close_session(sid, lease_id=lease)
            except Exception:
                pass
            raise
        return sid, lease

    async def open_theory(self, ctx: Any, name: str, imports: List[str], field: str) -> Current:
        key = self.conn_key(ctx)
        async with self._lock:
            old = self._current.pop(key, None)
        if old is not None:
            await self._safe_close(old)
        sid, lease = await self._begin_theory(name, imports, field)
        cur = Current(session_id=sid, lease_id=lease, theory=name)
        async with self._lock:
            self._current[key] = cur
        return cur

    def require_current(self, ctx: Any) -> Current:
        cur = self._current.get(self.conn_key(ctx))
        if cur is None:
            raise RuntimeError("No active theory for this connection — call enter_theory first.")
        return cur

    async def _safe_close(self, cur: Current) -> None:
        """Best-effort DELETE of the gym session (destroys the backend)."""
        try:
            c = await self.client()
            await c.close_session(cur.session_id, lease_id=cur.lease_id)
        except Exception:
            pass

    async def close_for(self, ctx: Any) -> bool:
        async with self._lock:
            cur = self._current.pop(self.conn_key(ctx), None)
        if cur is not None:
            await self._safe_close(cur)
            return True
        return False

    async def verify_batch(
        self, items: List[Dict[str, Any]], max_parallel: int, timeout: float
    ) -> List[Dict[str, Any]]:
        """Verify many independent chunks CONCURRENTLY across the server's session pool.

        Each item = {name, imports?, field?, chunk}. Bounded by a semaphore so we never
        oversubscribe the server pool / memory gate. This single tool is how a sequential
        MCP agent exploits the server's inter-session parallelism.
        """
        c = await self.client()
        cap = max(1, min(int(max_parallel), Config.MAX_PARALLEL))
        sem = asyncio.Semaphore(cap)

        async def run_one(item: Dict[str, Any]) -> Dict[str, Any]:
            name = item.get("name", "Chunk")
            imports = item.get("imports") or ["Main"]
            field = item.get("field") or Config.DEFAULT_FIELD
            chunk = item.get("chunk", "")
            async with sem:
                sid = lease = None
                try:
                    sid, lease = await self._begin_theory(name, imports, field)
                    rep = await c.verify_chunk(sid, chunk, timeout=timeout, lease_id=lease)
                    cmds = rep.get("commands", []) or []
                    return {
                        "name": name,
                        "success": rep.get("success"),
                        "timed_out": rep.get("timed_out"),
                        "stuck_line": rep.get("stuck_line"),
                        "execution_time": rep.get("execution_time"),
                        "n_failed": sum(1 for x in cmds if x.get("status") == "failed"),
                        "n_running": sum(1 for x in cmds if x.get("status") == "running"),
                    }
                except Exception as e:  # noqa: BLE001
                    return {"name": name, "error": f"{type(e).__name__}: {e}"}
                finally:
                    if sid is not None:
                        try:
                            await c.close_session(sid, lease_id=lease)
                        except Exception:
                            pass

        return list(await asyncio.gather(*(run_one(it) for it in items)))
