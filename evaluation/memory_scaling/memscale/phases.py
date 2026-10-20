"""The P0–P6 phase machine shared by both drivers (design §3), the cell
manifest, and the driver protocol a tool must implement.

A *cell* is (tool, topology, heap, k, rep). The runner starts the sampler,
walks the phases, times every per-session operation, and writes
``manifest.json`` next to ``samples.jsonl``. A failure inside a phase ends the
cell with ``outcome != "ok"`` and the phases that did run; nothing is retried.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple

from .sampler import Sampler
from .workload import Workload, insert_after_first_paragraph, with_theory_name


class Driver(Protocol):
    """What a tool driver provides. Sessions are addressed by index 0..k-1."""

    name: str
    topology: str

    async def setup(self) -> None:
        """Start the tool (server / MCP process) and wait until it answers."""

    async def start_session(self, i: int) -> None:
        """Cold-start session ``i`` on the cell's heap."""

    async def load(self, i: int, text: str) -> Dict[str, Any]:
        """Load the workload text into session ``i``; ``{ok, error, ...}``."""

    async def recheck(self, i: int, text: str) -> Dict[str, Any]:
        """Replace session ``i``'s text (P5); ``{ok, error, ...}``."""

    async def stop_all(self) -> None:
        """Close every session and stop the tool's processes."""

    def root_pids(self) -> List[int]:
        """Pids whose trees the sampler should add (empty when found by pattern)."""

    def describe(self) -> Dict[str, Any]:
        """Driver facts for the manifest."""


@dataclass
class SessionTiming:  # pylint: disable=too-many-instance-attributes
    """Per-session timings (seconds) and status."""

    index: int
    start_s: Optional[float] = None
    load_s: Optional[float] = None
    recheck_s: Optional[float] = None
    load_ok: Optional[bool] = None
    recheck_ok: Optional[bool] = None
    warm_hit: bool = False  # P1 handed back an idle session instead of starting one
    error: Optional[str] = None
    load_detail: Dict[str, Any] = field(
        default_factory=dict
    )  # driver scalars (counts, polls)
    recheck_detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Cell:  # pylint: disable=too-many-instance-attributes
    """The cell manifest."""

    tool: str
    topology: str
    heap: str
    k: int
    rep: int
    out_dir: str
    workload: Dict[str, Any]
    config: Dict[str, Any]
    phases: Dict[str, Dict[str, float]] = field(default_factory=dict)
    sessions: List[SessionTiming] = field(default_factory=list)
    outcome: str = "running"
    error: Optional[str] = None
    memory_rows: Dict[str, Any] = field(
        default_factory=dict
    )  # the tool's own view per phase
    environment: Dict[str, Any] = field(default_factory=dict)
    driver: Dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        """JSON form."""
        d = dict(self.__dict__)
        d["sessions"] = [s.__dict__ for s in self.sessions]
        return d

    def write(self) -> str:
        """Write ``manifest.json`` into ``out_dir`` (every phase boundary)."""
        path = os.path.join(self.out_dir, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.as_dict(), fh, indent=1, default=str)
        return path


def environment_block(isabelle_home: Optional[str] = None) -> Dict[str, Any]:
    """Host, kernel, Isabelle version/id, repo commit — Appendix D material."""
    env: Dict[str, Any] = {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
    }
    isa = (
        os.path.join(isabelle_home, "bin", "isabelle") if isabelle_home else "isabelle"
    )
    for key, args in (
        ("isabelle_version", ["version"]),
        ("isabelle_id", ["version", "-i"]),
    ):
        try:
            env[key] = subprocess.run(
                [isa, *args], capture_output=True, text=True, timeout=60, check=False
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            env[key] = None
    for key, var in (
        ("ml_options", "ML_OPTIONS"),
        ("java_options", "ISABELLE_TOOL_JAVA_OPTIONS"),
        ("home_user", "ISABELLE_HOME_USER"),
    ):  # plan review S6: the launch environment both tools share
        try:
            env[key] = subprocess.run(
                [isa, "getenv", "-b", var],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            env[key] = None
    try:
        env["ips_commit"] = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        env["ips_commit"] = None
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    env["mem_total_kb"] = int(line.split()[1])
    except (OSError, ValueError):
        pass
    return env


def _scalars(res: Dict[str, Any]) -> Dict[str, Any]:
    """Scalars and one level of dict scalars of a driver result (no text)."""
    out: Dict[str, Any] = {}
    for k, v in res.items():
        if isinstance(v, (int, float, bool)) and k != "ok":
            out[k] = v
        elif isinstance(v, dict):
            out[k] = {
                kk: vv
                for kk, vv in v.items()
                if isinstance(vv, (int, float, bool, str))
            }
    return out


async def _timed(coro: Any) -> Tuple[float, Any]:
    t0 = time.monotonic()
    result = await coro
    return time.monotonic() - t0, result


def p5_text(workload: Workload, i: int) -> str:
    """Session i's P3 text with the marker lemma inserted after the first
    paragraph that follows ``begin``, SAME theory name: every later command
    re-checks on the incremental path (a renamed header, or an edit inside
    the header's whitespace, made IPS reset — i.e. restart — the session's ML
    process; stage-2 smoke + live probe)."""
    named = with_theory_name(workload.text, f"{workload.theory_name}_{i}")
    return insert_after_first_paragraph(named, workload.trailing_lemma)


async def run_cell(  # pylint: disable=too-many-arguments,too-many-locals,too-many-statements
    cell: Cell,
    driver: Driver,
    workload: Workload,
    sampler: Sampler,
    *,
    baseline_s: float,
    idle_s: float,
    settle_after_stop_s: float = 10.0,
    log: Any = print,
) -> Cell:
    """Walk P0–P6 for one cell. The sampler is already started by the caller
    (its roots/patterns configured); this function only tags phases."""

    async def own_view(name: str) -> None:
        """The tool's own memory report at a phase boundary (c209e6c S3)."""
        rows_fn = getattr(driver, "memory_rows", None)
        if rows_fn is None:
            return
        try:
            cell.memory_rows[name] = await rows_fn()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            cell.memory_rows[name] = {"error": f"{type(exc).__name__}: {exc}"[:200]}

    def mark(name: str) -> None:
        now = time.time()
        for ph in cell.phases.values():
            ph.setdefault("t1", now)
        cell.phases[name] = {"t0": now}
        sampler.phase(name)
        cell.write()
        log(
            f"[{cell.tool}/{cell.topology} {cell.heap} k={cell.k} rep={cell.rep}] phase {name}"
        )

    cell.driver = driver.describe()
    cell.config["cgroup_dir"] = getattr(
        sampler, "cgroup_dir", None
    )  # review 2e1935e S4
    cell.sessions = [SessionTiming(i) for i in range(cell.k)]
    try:
        mark("P0")
        await driver.setup()
        sampler.add_roots(driver.root_pids())
        cell.driver = (
            driver.describe()
        )  # now with the spawned pids / start time (plan review B2)
        cell.config["roots"] = sorted(set(driver.root_pids()))
        # the tool's own cgroup (a systemd scope) is what the sampler's cgroup
        # columns must read from now on (review ac32954 B1); hard facts in the
        # environment block (S2)
        tool_cg = getattr(driver, "cgroup_dir", None)
        if tool_cg:
            sampler.cgroup_dir = tool_cg
        cell.config["cgroup_dir"] = getattr(sampler, "cgroup_dir", None)
        cell.environment["cgroup_dir"] = cell.config["cgroup_dir"]
        cell.environment["scope_used"] = bool(getattr(driver, "scope_used", False))
        cell.environment["scope_mismatch"] = bool(
            getattr(driver, "scope_mismatch", False)
        )
        sampler.sample_once()
        await asyncio.sleep(baseline_s)

        mark("P1")
        for i in range(cell.k):
            dt, _ = await _timed(driver.start_session(i))
            cell.sessions[i].start_s = round(dt, 3)
            cell.sessions[i].warm_hit = bool(
                getattr(driver, "warm_hits", {}).get(i, False)
            )
            sampler.add_roots(driver.root_pids())
            sampler.sample_once()
            cell.write()
            log(f"  session {i} started in {dt:.1f}s")

        await own_view("P1")
        mark("P2")
        await asyncio.sleep(idle_s)

        await own_view("P2")
        mark("P3")
        for i in range(cell.k):
            text = with_theory_name(workload.text, f"{workload.theory_name}_{i}")
            dt, res = await _timed(driver.load(i, text))
            cell.sessions[i].load_s = round(dt, 3)
            cell.sessions[i].load_ok = bool(res.get("ok"))
            cell.sessions[i].load_detail = _scalars(res)
            if not res.get("ok"):
                cell.sessions[i].error = str(res.get("error"))[:300]
            sampler.sample_once()
            cell.write()
            log(f"  session {i} loaded in {dt:.1f}s ok={res.get('ok')}")

        await own_view("P3")
        mark("P4")
        await asyncio.sleep(idle_s)

        await own_view("P4")
        mark("P5")

        async def one(i: int) -> None:
            dt, res = await _timed(driver.recheck(i, p5_text(workload, i)))
            cell.sessions[i].recheck_s = round(dt, 3)
            cell.sessions[i].recheck_ok = bool(res.get("ok"))
            cell.sessions[i].recheck_detail = _scalars(res)
            if not res.get("ok"):
                cell.sessions[i].error = str(res.get("error"))[:300]

        await asyncio.gather(*(one(i) for i in range(cell.k)))
        cell.write()
        log(
            "  concurrent re-check done: "
            + ", ".join(f"{s.recheck_s}s" for s in cell.sessions)
        )
        await asyncio.sleep(idle_s)  # settle after the burst (design §3)
        await own_view("P5")

        mark("P6")
        await driver.stop_all()
        await asyncio.sleep(settle_after_stop_s)
        cell.outcome = (
            "ok"
            if all(s.load_ok and s.recheck_ok for s in cell.sessions)
            else "partial"
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        cell.outcome, cell.error = "error", f"{type(exc).__name__}: {exc}"[:500]
        log(f"  CELL ERROR {cell.error}")
        try:
            await driver.stop_all()
        except Exception as exc2:  # pylint: disable=broad-exception-caught
            cell.error += f" | stop_all: {type(exc2).__name__}: {exc2}"[:200]
    finally:
        now = time.time()
        for ph in cell.phases.values():
            ph.setdefault("t1", now)
        cell.finished_at = now
        cell.write()
    return cell
