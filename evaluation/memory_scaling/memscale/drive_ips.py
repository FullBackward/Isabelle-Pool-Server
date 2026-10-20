"""IPS driver: one server process (started here, or an external URL), k
leased sessions on the requested heap, loads through ``load_document``.

Session i = one lease. P1 ``acquire_session(theories=imports, field=heap,
reuse_dirty=False)`` (cold starts: the pool is empty and ``reuse_dirty`` is
off, so every acquire creates a session); P3 ``load_document(text,
report=True)``; P5 the same with the renamed text (header change = full
re-check); P6 ``close_session`` for every lease, then the server is stopped.
``warm_hit`` (E2) releases one session and re-acquires with the same imports.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import time
from typing import Any, Dict, List, Optional

import httpx

from client.async_client import PoolAsyncClient


def cgroup_dir_of(pid: int) -> Optional[str]:
    """``/sys/fs/cgroup<path>`` of a process's cgroup v2 membership (None for
    the root or when unreadable) — the directory the sampler should read for
    the server's own `memory.current` / `memory.peak`."""
    try:
        with open(f"/proc/{pid}/cgroup", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("0::"):
                    rel = line[3:].strip()
                    return "/sys/fs/cgroup" + rel if rel and rel != "/" else None
    except OSError:
        return None
    return None


class IPSDriver:  # pylint: disable=too-many-instance-attributes
    """See the module docstring."""

    name = "ips"
    topology = "pool"

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        heap: str,
        imports: List[str],
        url: str = "http://127.0.0.1:8000",
        external: bool = False,
        start_cmd: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        ready_timeout_s: float = 300.0,
        load_timeout_s: float = 900.0,
        repo_root: Optional[str] = None,
        scope: str = "auto",
        memory_max: Optional[str] = None,
    ) -> None:
        self.heap = heap
        self.imports = list(imports)
        self.url = url
        self.external = external
        self.start_cmd = start_cmd or ["python", "-m", "server.app.main"]
        self.env = dict(env or {})
        self.ready_timeout_s = ready_timeout_s
        self.load_timeout_s = load_timeout_s
        self.repo_root = repo_root
        self.proc: Optional[subprocess.Popen[bytes]] = None
        self.client: Optional[PoolAsyncClient] = None
        self.sessions: Dict[int, Dict[str, str]] = {}
        self.log_path: Optional[str] = None
        self.server_started_at: Optional[float] = None
        # own transient cgroup via `systemd-run --user --scope` (review 62c7262 S1):
        # natively the server would otherwise share the user's session scope with
        # the PIDE MCP processes and the runner, and its `used` would count them.
        # "auto" uses it when available, "yes" requires it, "no" never.
        self.scope = scope
        self.memory_max = memory_max  # e.g. "48G" for E3
        self.cgroup_dir: Optional[str] = None
        self.scope_used = False

    # ------------------------------------------------------------ server
    async def setup(self) -> None:
        """Start the server (unless external) and wait for /readyz."""
        if not self.external:
            env = dict(os.environ)
            env.update(self.env)
            log_dir = env.get("ISABELLE_SERVER_LOG_DIR", "results/ips-logs")
            os.makedirs(log_dir, exist_ok=True)
            self.log_path = os.path.join(log_dir, "server.stdout.log")
            self.server_started_at = time.time()
            cmd = self._scoped(self.start_cmd)
            with open(self.log_path, "ab") as out:
                self.proc = subprocess.Popen(  # pylint: disable=consider-using-with
                    cmd,
                    cwd=self.repo_root,
                    env=env,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            self.cgroup_dir = cgroup_dir_of(self.proc.pid)
        self.client = PoolAsyncClient(self.url, timeout=self.load_timeout_s + 120.0)
        deadline = time.monotonic() + self.ready_timeout_s
        while True:
            try:
                async with httpx.AsyncClient(timeout=5.0) as c:
                    r = await c.get(self.url + "/readyz")
                if r.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"IPS not ready at {self.url} after {self.ready_timeout_s}s"
                )
            await asyncio.sleep(2.0)

    def _scoped(self, cmd: List[str]) -> List[str]:
        """Wrap ``cmd`` in a transient user scope when wanted and available."""
        if self.scope == "no":
            return cmd
        available = shutil.which("systemd-run") is not None
        if not available:
            if self.scope == "yes":
                raise RuntimeError("scope=yes but systemd-run is not available")
            return cmd
        wrapper = ["systemd-run", "--user", "--scope", "--quiet"]
        if self.memory_max:
            wrapper += ["-p", f"MemoryMax={self.memory_max}"]
        self.scope_used = True
        return wrapper + cmd

    def root_pids(self) -> List[int]:
        """The server we started (an external server is found by pattern)."""
        return [self.proc.pid] if self.proc is not None else []

    def describe(self) -> Dict[str, Any]:
        """Driver facts for the manifest."""
        return {
            "name": self.name,
            "topology": self.topology,
            "url": self.url,
            "external": self.external,
            "env": self.env,
            "heap": self.heap,
            "imports": self.imports,
            "server_pid": self.proc.pid if self.proc is not None else None,
            "scope_used": self.scope_used,
            "memory_max": self.memory_max,
            "cgroup_dir": self.cgroup_dir,
            "server_started_at": self.server_started_at,  # fresh per cell (plan review B2)
        }

    # ---------------------------------------------------------- sessions
    def _c(self) -> PoolAsyncClient:
        assert self.client is not None, "setup() first"
        return self.client

    async def start_session(self, i: int) -> None:
        """Cold start: an empty pool with reuse_dirty=False creates a session."""
        s = await self._c().acquire_session(
            self.imports,
            self.heap,
            reuse_dirty=False,
            label=f"memscale-{self.heap}-{i}",
        )
        self.sessions[i] = {
            "session_id": s["session_id"],
            "lease_id": s["lease_id"],
            "reused": str(s.get("reused")),
        }

    async def load(self, i: int, text: str) -> Dict[str, Any]:
        """Whole-document load with the per-command report."""
        s = self.sessions[i]
        try:
            res = await self._c().load_document(
                s["session_id"],
                text,
                timeout=self.load_timeout_s,
                report=True,
                lease_id=s["lease_id"],
            )
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        report = res.get("report") or {}
        failed = [
            c for c in (report.get("commands") or []) if c.get("status") == "failed"
        ]
        ok = bool(res.get("success")) and not failed and not report.get("timed_out")
        return {
            "ok": ok,
            "error": res.get("error")
            or (f"{len(failed)} failed commands" if failed else None),
            "execution_time": res.get("execution_time"),
        }

    async def recheck(self, i: int, text: str) -> Dict[str, Any]:
        """P5: the renamed text with the marker lemma; a header change re-checks all."""
        return await self.load(i, text)

    async def warm_hit(self, i: int) -> Dict[str, Any]:
        """E2: release session i and re-acquire with the same imports; the pool
        should hand the same session back (`reused`)."""
        s = self.sessions[i]
        await self._c().release_session(s["session_id"], lease_id=s["lease_id"])
        t0 = time.monotonic()
        again = await self._c().acquire_session(
            self.imports, self.heap, reuse_dirty=True, label=f"memscale-warm-{i}"
        )
        dt = time.monotonic() - t0
        self.sessions[i] = {
            "session_id": again["session_id"],
            "lease_id": again["lease_id"],
            "reused": str(again.get("reused")),
        }
        return {
            "warm_s": round(dt, 3),
            "reused": bool(again.get("reused")),
            "same_session": again["session_id"] == s["session_id"],
        }

    async def memory_rows(self) -> Dict[str, Any]:
        """IPS's own view (/api/v1/memory) for cross-checking the sampler."""
        try:
            return await self._c().memory_status()
        except httpx.HTTPError as exc:
            return {"error": str(exc)}

    async def stop_all(self) -> None:
        """Close every session, then stop the server we started."""
        for i, s in list(self.sessions.items()):
            try:
                await self._c().close_session(s["session_id"], lease_id=s["lease_id"])
            except httpx.HTTPError:
                pass
            self.sessions.pop(i, None)
        if self.client is not None:
            await self.client.close()
            self.client = None
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=60)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.proc = None
