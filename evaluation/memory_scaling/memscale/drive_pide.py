"""PIDE MCP driver (`isabelle pide_mcp`, Kappelmann) in two topologies:

- **A** (``pide-a``): ONE MCP process; session i = ``start_session(logic=heap)``
  inside it (the one-agent, many-sessions shape of its README);
- **B** (``pide-b``): k MCP processes, one session each (one process per
  agent — the multi-agent shape the comparison is about).

Both start sessions through the same tool, with ``no_build`` (heaps exist),
so P1 latencies mean the same thing; a ``-l HEAP`` launch would hide the
start inside the handshake. P3: ``create_scratch`` + ``edit`` (mode replace,
empty ``old_text`` = the whole file) — ``edit`` returns once PIDE processed
the text; P5: ``edit`` again with the marker inserted after the first
paragraph (PIDE pushes the minimal diff — the same incremental re-check as
IPS's sync path); P6: ``stop_session`` then the processes are terminated.
Root pids = the spawned MCP processes (the JVM and its provers are below).
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional

from .mcp_stdio import MCPError, StdioMCP
from .workload import with_theory_name


class PIDEDriver:  # pylint: disable=too-many-instance-attributes
    """See the module docstring."""

    name = "pide"
    poll_s = 1.0  # get_state poll interval while a load settles
    empty_polls = (
        5  # give up when get_state still reports zero commands after this many polls
    )

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        heap: str,
        topology: str,
        k: int,
        command: Optional[List[str]] = None,
        extra_args: Optional[List[str]] = None,
        start_timeout_s: float = 900.0,
        load_timeout_s: float = 900.0,
        no_build: bool = True,
        log_dir: str = "results/pide-logs",
    ) -> None:
        assert topology in ("A", "B")
        self.heap = heap
        self.topology = topology
        self.k = k
        self.command = list(command or ["isabelle", "pide_mcp"]) + list(
            extra_args or []
        )
        self.start_timeout_s = start_timeout_s
        self.load_timeout_s = load_timeout_s
        self.no_build = no_build
        self.log_dir = log_dir
        self.procs: List[StdioMCP] = []  # A: one; B: one per session
        self.session_ids: Dict[int, str] = {}
        self.scratch: Dict[int, str] = {}
        self.tools: List[str] = []
        self.server_info: Dict[str, Any] = {}

    # ----------------------------------------------------------- helpers
    def _mcp(self, i: int) -> StdioMCP:
        return self.procs[0] if self.topology == "A" else self.procs[i]

    async def _spawn(self, tag: str) -> StdioMCP:
        os.makedirs(self.log_dir, exist_ok=True)
        m = StdioMCP(
            self.command,
            stderr_path=os.path.join(self.log_dir, f"pide_mcp-{tag}.stderr.log"),
            name=f"memscale-{tag}",
        )
        await m.start(init_timeout_s=self.start_timeout_s)
        if not self.tools:
            self.tools = [str(t.get("name")) for t in await m.list_tools()]
            self.server_info = dict(m.server_info)
        return m

    # ------------------------------------------------------------ driver
    async def setup(self) -> None:
        """A: start the single MCP process; B: start k processes (no sessions yet)."""
        n = 1 if self.topology == "A" else self.k
        for j in range(n):
            self.procs.append(await self._spawn(f"{self.heap}-{self.topology}{j}"))
        for required in ("start_session", "create_scratch", "edit", "stop_session"):
            if required not in self.tools:
                raise RuntimeError(f"PIDE MCP has no tool {required!r}: {self.tools}")

    async def start_session(self, i: int) -> None:
        """``start_session(logic=heap, no_build)`` on process A or B_i."""
        res = await self._mcp(i).call(
            "start_session",
            {"logic": self.heap, "session": f"s{i}", "no_build": self.no_build},
            timeout_s=self.start_timeout_s,
        )
        self.session_ids[i] = str(res.get("session") or f"s{i}")

    async def load(self, i: int, text: str) -> Dict[str, Any]:
        """A scratch theory per session, its whole text written with one edit."""
        m, sid = self._mcp(i), self.session_ids[i]
        t0 = time.monotonic()
        try:
            if i not in self.scratch:
                sc = await m.call(
                    "create_scratch",
                    {
                        "session": sid,
                        "name_suffix": f"memscale_{i}",
                        "extension": ".thy",
                    },
                    timeout_s=120.0,
                )
                self.scratch[i] = str(sc["path"])
            # Isabelle requires theory name == file name: the scratch's stem
            # (a mismatch fails the header and nothing is checked)
            stem = os.path.splitext(os.path.basename(self.scratch[i]))[0]
            res = await m.call(
                "edit",
                {
                    "session": sid,
                    "origin": self.scratch[i],
                    "mode": "replace",
                    "old_text": "",
                    "text": with_theory_name(text, stem),
                },
                timeout_s=self.load_timeout_s,
            )
            # `edit` returns once PIDE accepted the text, NOT when checking is
            # done (the provers kept growing for a minute after a 0.5 s "load"
            # in the stage-3 smoke): poll the counts until nothing runs
            state, polls = await self._settled(
                i, time.monotonic() + self.load_timeout_s
            )
        except MCPError as exc:
            return {
                "ok": False,
                "error": str(exc)[:300],
                "execution_time": time.monotonic() - t0,
            }
        counts = _counts(state)
        counts["polls"] = polls
        bad = _sum_matching(counts, ("bad", "failed"))
        if bad or state.get("timed_out") or state.get("no_commands"):
            return {
                "ok": False,
                "error": (
                    f"{bad} bad/failed command(s)"
                    if bad
                    else (
                        "no commands (header failed?)"
                        if state.get("no_commands")
                        else "not settled within load_timeout_s"
                    )
                ),
                "execution_time": time.monotonic() - t0,
                "commands": counts,
            }
        return {
            "ok": True,
            "error": None,
            "execution_time": time.monotonic() - t0,
            "edit": _small(res),
            "commands": counts,
        }

    async def recheck(self, i: int, text: str) -> Dict[str, Any]:
        """P5: the same scratch, the new text (PIDE diffs it)."""
        return await self.load(i, text)

    async def _settled(self, i: int, deadline: float) -> Any:
        """Poll ``get_state`` until no command is running or unprocessed
        (counts 0 with at least one command seen) or the deadline passes.
        Returns ``(state, polls)``; a timed-out state carries ``timed_out``."""
        polls = 0
        while True:
            state = await self._mcp(i).call(
                "get_state", self._state_args(i), timeout_s=120.0
            )
            polls += 1
            c = _counts(state)
            busy = _sum_matching(c, ("unprocessed", "running"))
            if c.get("count", 0) > 0 and busy == 0:
                return state, polls
            if c.get("count", 0) == 0 and polls >= self.empty_polls:
                state["no_commands"] = (
                    True  # the header failed outright (review 13c06cf S4)
                )
                return state, polls
            if time.monotonic() > deadline:
                state["timed_out"] = True
                return state, polls
            await asyncio.sleep(self.poll_s)

    def _state_args(self, i: int) -> Dict[str, Any]:
        """Counts only: no types/facts/infos/markup and no command details,
        so the answer is small (a full state is megabytes on one line)."""
        return {
            "session": self.session_ids[i],
            "origin": self.scratch[i],
            "include_types": False,
            "include_facts": False,
            "include_infos": False,
            "include_full_markup": False,
            "commands_limit": 0,
        }

    async def state(self, i: int) -> Dict[str, Any]:
        """``get_state`` of session i's scratch (E4's "keeps answering" probe)."""
        return await self._mcp(i).call("get_state", self._state_args(i), timeout_s=60.0)

    async def probe(self, i: int) -> bool:
        """E4: does session i still answer ``get_state``?"""
        state = await self.state(i)
        return bool(_counts(state).get("count", 0) > 0)

    async def stop_all(self) -> None:
        """``stop_session`` for every session, then terminate the processes."""
        for i, sid in list(self.session_ids.items()):
            try:
                await self._mcp(i).call(
                    "stop_session", {"sessions": [sid]}, timeout_s=120.0
                )
            except (MCPError, IndexError):
                pass
        self.session_ids.clear()
        for m in self.procs:
            await m.close()
        self.procs.clear()

    def session_options(self) -> Dict[str, Any]:
        """PIDE MCP starts sessions with Isabelle's defaults unless `-o` options
        are passed (config `extra_args`); recorded for fairness (13c06cf S3)."""
        opts = [a for a in self.command if "=" in a]
        return {
            "isabelle_options": opts
            or "defaults (parallel_proofs 1, threads = hardware)"
        }

    def root_pids(self) -> List[int]:
        """The spawned MCP processes (their JVMs and provers are below them)."""
        return [m.pid for m in self.procs if m.pid is not None]

    def describe(self) -> Dict[str, Any]:
        """Driver facts for the manifest."""
        return {
            "name": self.name,
            "topology": self.topology,
            "command": self.command,
            "heap": self.heap,
            "no_build": self.no_build,
            "processes": [
                {"pid": m.pid, "started_at": m.started_at} for m in self.procs
            ],
            "sessions": dict(self.session_ids),
            "scratch": dict(self.scratch),
            "tools": self.tools,
            "server_info": self.server_info,
        }


def _sum_matching(counts: Dict[str, int], needles: Any) -> int:
    """Sum of the counts whose key contains any needle — PIDE MCP's keys are
    descriptive (``commands_unprocessed``,
    ``commands_still_running_possibly_nonterminating``, ``commands_failed``)."""
    return sum(
        v for k, v in counts.items() if any(n in k for n in needles) and k != "count"
    )


def _counts(state: Dict[str, Any]) -> Dict[str, int]:
    """Command counts from a ``get_state`` result, whatever keys it carries:
    ``commands.count`` and per-status entries (``finished``, ``bad``, …) as
    ints or as objects with a ``count``."""
    out: Dict[str, int] = {}
    cmds = state.get("commands")
    if isinstance(cmds, dict) and isinstance(cmds.get("count"), int):
        out["count"] = int(cmds["count"])
    for key, val in state.items():
        if isinstance(val, dict) and isinstance(val.get("count"), int):
            out[str(key)] = int(val["count"])
        elif isinstance(val, int) and not isinstance(val, bool):
            out[str(key)] = val
    return out


def _small(res: Dict[str, Any]) -> Dict[str, Any]:
    """Keep a tool result's scalars only (the manifest stays small)."""
    return {k: v for k, v in res.items() if isinstance(v, (str, int, float, bool))}


def make_pide_driver(args: Any, cfg: Dict[str, Any], imports: List[str]) -> PIDEDriver:
    """Factory used by run_cell (``--tool pide-a`` / ``pide-b``)."""
    del imports  # the heap is the session; the theory's imports resolve inside it
    pide = cfg["tools"]["pide_mcp"]
    return PIDEDriver(
        heap=args.heap,
        topology="A" if args.tool == "pide-a" else "B",
        k=int(args.k),
        command=list(pide.get("command", ["isabelle", "pide_mcp"])),
        extra_args=list(pide.get("extra_args") or []),
        start_timeout_s=float(pide.get("start_timeout_s", 900)),
        load_timeout_s=float(cfg["phases"].get("load_timeout_s", 900)),
        no_build=bool(pide.get("no_build", True)),
        log_dir=str(pide.get("log_dir", "results/pide-logs")),
    )
