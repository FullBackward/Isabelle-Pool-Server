"""IPS driver: one server process (started here in its own systemd scope, or
an external URL), k leased sessions on the requested heap, loads through
``load_document``.

Session i = one lease. P1: a fresh server's pool is empty, so
``acquire_session(reuse_dirty=False)`` creates a session (cold start); on an
external server ``create_session`` is used instead, so a P1 latency is a cold
start on both paths (review c209e6c S2) — the warm hit is measured by
``warm_hit`` (E2). P3 ``load_document(text, report=True)``; P5 the same theory
name with the marker lemma after the first paragraph, which takes the
INCREMENTAL path and re-checks every later command — a header change would
make the server reset (restart) the session's ML process (docs/ISSUES.md,
2026-10-20). P6 ``close_session`` for every lease, then the server is stopped.
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

from .workload import insert_after_first_paragraph


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


PROBE_LEMMA = 'lemma memscale_probe: "True" by simp'


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
        self.last_text: Dict[int, str] = {}  # what each session holds (E4 probe)
        self.last_probe: Dict[str, Any] = {}
        self.warm_hits: Dict[int, bool] = (
            {}
        )  # P1 handed back an idle session? (c209e6c S2)
        self.server_report: Dict[str, Any] = {}
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
        self.scope_mismatch = False

    # ------------------------------------------------------------ server
    async def _url_answers(self) -> bool:
        """Does any HTTP server answer at the URL (any status)?"""
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                await c.get(self.url + "/healthz")
            return True
        except httpx.HTTPError:
            return False

    def _log_tail(self, n: int = 20) -> str:
        if not self.log_path:
            return ""
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-n:]).strip()
        except OSError:
            return ""

    def _check_report(self) -> None:
        """The server that answered must be the one configured here: its own
        report's pool size equals the ISABELLE_POOL_SIZE we started it with
        (training-metrics review 0df4258 B1: a busy port made a run measure a
        different server while its manifest claimed a fresh one). A second
        layer only: a foreign server with the same configuration passes it —
        the busy-port refusal before the start and the exit check while waiting
        are what identify the server (review 5141144 S2)."""
        want = self.env.get("ISABELLE_POOL_SIZE")
        got = self.server_report.get("max_pool_size")
        if want is not None and str(got) != str(want):
            raise RuntimeError(
                f"the server at {self.url} reports max_pool_size={got}, "
                f"configured {want}: not the server this run started"
            )

    async def setup(self) -> None:
        """Start the server (unless external) and wait for /readyz. A fresh
        server refuses a busy port, fails if its process exits before it is
        ready, and cross-checks the answering server's report against the
        configuration (training-metrics review 0df4258 B1)."""
        if not self.external:
            if await self._url_answers():
                raise RuntimeError(
                    f"port busy: a server answers at {self.url}; stop it or "
                    "pass --ips-external to measure that server on purpose"
                )
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
        self.client = PoolAsyncClient(self.url, timeout=self.load_timeout_s + 120.0)
        deadline = time.monotonic() + self.ready_timeout_s
        while True:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(
                    f"the IPS server exited (rc {self.proc.returncode}) before it was "
                    f"ready; log tail:\n{self._log_tail()}"
                )
            try:
                async with httpx.AsyncClient(timeout=5.0) as c:
                    r = await c.get(self.url + "/readyz")
                if r.status_code == 200:
                    try:  # the server's own view of its limits (c209e6c S4)
                        self.server_report = await self._c().health()
                    except httpx.HTTPError:
                        self.server_report = {}
                    self._read_cgroup()  # after readiness: systemd-run has exec'd by now
                    if not self.external:
                        self._check_report()
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"IPS not ready at {self.url} after {self.ready_timeout_s}s"
                )
            await asyncio.sleep(2.0)

    def session_options(self) -> Dict[str, Any]:
        """The session options IPS's Scala session manager applies (from the
        server's environment; Isabelle defaults when unset) — recorded next to
        PIDE MCP's, which uses Isabelle's defaults (review 13c06cf S3)."""
        env = {**os.environ, **self.env}
        return {
            "parallel_proofs": env.get("ISABELLE_PARALLEL_PROOFS", "2 (IPS default)"),
            "threads": env.get(
                "ISABELLE_SESSION_THREADS",
                "4 (IPS default; the ML farm is sized from hardware)",
            ),
        }

    def _read_cgroup(self) -> None:
        """The server's cgroup directory (review ac32954 B1): read after
        readiness — right after Popen the child is still `systemd-run`, whose
        cgroup is the runner's. When a scope was requested but the directory
        equals the runner's own, the scope did not take: recorded as
        ``scope_mismatch`` (a cell result, not hidden)."""
        if self.proc is None:
            return
        self.cgroup_dir = cgroup_dir_of(self.proc.pid)
        own = cgroup_dir_of(os.getpid())
        self.scope_mismatch = bool(
            self.scope_used and (self.cgroup_dir is None or self.cgroup_dir == own)
        )

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
        """Driver facts for the manifest (the cgroup re-read each time)."""
        if self.proc is not None and self.proc.poll() is None:
            self._read_cgroup()
        return {
            "name": self.name,
            "topology": self.topology,
            "url": self.url,
            "external": self.external,
            "env": self.env,
            "heap": self.heap,
            "imports": self.imports,
            "server_pid": self.proc.pid if self.proc is not None else None,
            "sessions": {str(i): dict(v) for i, v in self.sessions.items()},
            "warm_hits": {str(i): v for i, v in self.warm_hits.items()},
            "server_report": {
                k: self.server_report.get(k)
                for k in (
                    "memory_management_enabled",
                    "memory_limit_mb",
                    "memory_source",
                    "max_pool_size",
                    "memory_state",
                )
            },
            "scope_used": self.scope_used,
            "memory_max": self.memory_max,
            "cgroup_dir": self.cgroup_dir,
            "scope_mismatch": self.scope_mismatch,
            "server_started_at": self.server_started_at,  # fresh per cell (plan review B2)
            "start_cmd": None if self.external else self.start_cmd,
            "server_pgid": self.proc.pid if self.proc is not None else None,
        }

    # ---------------------------------------------------------- sessions
    def _c(self) -> PoolAsyncClient:
        assert self.client is not None, "setup() first"
        return self.client

    async def start_session(self, i: int) -> None:
        """Cold start: a fresh server's empty pool creates a session on acquire;
        an external server gets ``create_session`` (never a warm hit)."""
        label = f"memscale-{self.heap}-{i}"
        if self.external:
            s = await self._c().create_session(self.imports, self.heap, label=label)
            reused = False
        else:
            s = await self._c().acquire_session(
                self.imports, self.heap, reuse_dirty=False, label=label
            )
            reused = bool(s.get("reused"))
        self.sessions[i] = {
            "session_id": s["session_id"],
            "lease_id": s.get("lease_id") or "",
            "reused": str(reused),
        }
        self.warm_hits[i] = reused

    async def load(self, i: int, text: str) -> Dict[str, Any]:
        """Whole-document load with the per-command report."""
        s = self.sessions[i]
        self.last_text[i] = text
        try:
            res = await self._c().load_document(
                s["session_id"],
                text,
                timeout=self.load_timeout_s,
                report=True,
                lease_id=s["lease_id"],
            )
        except (
            httpx.HTTPStatusError
        ) as exc:  # a refusal/404 on the load: keep the body (review facc4c9 S4)
            resp = exc.response
            try:
                body: Any = resp.json() if resp is not None else {}
            except ValueError:
                body = {"text": (resp.text[:300] if resp is not None else "")}
            return {
                "ok": False,
                "error": f"http {resp.status_code if resp is not None else '?'}: "
                + str(body.get("error") if isinstance(body, dict) else body)[:200],
                "status": resp.status_code if resp is not None else None,
                "body": body,
            }
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
        """P5: the same theory name with the marker after the first paragraph —
        the incremental path re-checks every later command (no header change,
        which would reset the prover; docs/ISSUES.md 2026-10-20)."""
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

    async def release_half(self) -> None:
        """E3 recovery: release every second session (leased sessions are
        never evicted; released idle ones are what the gate can reclaim)."""
        for i in sorted(self.sessions)[::2]:
            s = self.sessions.pop(i)
            try:
                await self._c().release_session(s["session_id"], lease_id=s["lease_id"])
            except httpx.HTTPError:
                pass

    async def probe(self, i: int) -> bool:
        """E4: does session i still re-check? One more lemma after the first
        paragraph of the text it holds, through the P5 path (load_document).
        NOT a chunk appended to the document: after the theory's ``end`` every
        chunk fails with "missing theory context" (server run 2026-10-21: all
        eight answers False, kill or no kill; reproduced on the container)."""
        text = self.last_text.get(i)
        if text is None:
            self.last_probe = {"ok": False, "error": "nothing loaded in this session"}
            return False
        res = await self.load(i, insert_after_first_paragraph(text, PROBE_LEMMA))
        self.last_probe = res
        return bool(res.get("ok"))

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
