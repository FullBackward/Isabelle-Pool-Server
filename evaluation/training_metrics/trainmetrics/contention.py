"""Contention measures shared by the T5 contention cell (design §T5, plan
stage 3c) and T6: the server's memory tier over the whole cell (review
beac25c S2: a loop that pushes the server to warning / critical changes
admission for the other workers), and how the other workers' calls fare while
one session sits in a loop: each corpus block's time against its census time,
grouped by what the other workers were doing (loop, replay, setup, idle,
done; review 73f9cd4 S1), with the queueing part (client wall minus the
server's execution time, measured in its worker thread; plan_v1.6 S6)."""

from __future__ import annotations

import asyncio
import bisect
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .metrics_t12 import p50, p95


def executor_threads() -> int:
    """The server's default backend executor size, ``min(32, cpu_count + 4)``
    (``asyncio.to_thread``) from THIS host's CPUs: the server's only when it runs
    on the same host, as on the Ubuntu server (native) and in the container
    smoke (review 73f9cd4 note). W = 32 sits on it."""
    return min(32, (os.cpu_count() or 1) + 4)


async def poll_tiers(
    client: Any, path: Path, stop: asyncio.Event, interval_s: float = 5.0
) -> None:
    """Append ``GET /api/v1/memory``'s tier and used / limit to ``path`` every
    ``interval_s`` until ``stop`` is set (one row more after it)."""
    while True:
        row: Dict[str, Any] = {"t": time.time()}
        try:
            m = await client.memory_status()
            mem = m.get("memory") or {}
            row.update(
                {
                    "state": m.get("state"),
                    "used_mb": mem.get("used_mb"),
                    "limit_mb": mem.get("limit_mb"),
                    "pressure_pct": mem.get("pressure_pct"),
                }
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            row["error"] = f"{type(exc).__name__}: {exc}"[:200]
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        if stop.is_set():
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass


def tier_summary(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Polls per tier, the highest used memory and pressure, poll errors."""
    states: Dict[str, int] = {}
    for r in rows:
        if r.get("state"):
            states[str(r["state"])] = states.get(str(r["state"]), 0) + 1
    used = [float(r["used_mb"]) for r in rows if r.get("used_mb") is not None]
    pressure = [
        float(r["pressure_pct"]) for r in rows if r.get("pressure_pct") is not None
    ]
    return {
        "polls": len(rows),
        "states": states,
        "used_max_mb": max(used) if used else None,
        "pressure_max_pct": max(pressure) if pressure else None,
        "errors": sum(1 for r in rows if r.get("error")),
    }


STATES = ("loop", "replay", "setup", "idle", "done")


def worker_intervals(
    files: Sequence[Dict[str, Any]],
) -> Dict[int, Tuple[List[Tuple[float, float, str]], float]]:
    """worker -> (its intervals sorted by start, its end): ``setup`` = the
    acquisition and the enter of a file, ``replay`` = a corpus block or a seed
    that did not loop, ``loop`` = a seed attempt that timed out."""
    out: Dict[int, Tuple[List[Tuple[float, float, str]], float]] = {}
    for f in files:
        w = int(f.get("worker") or 0)
        iv, end = out.get(w, ([], 0.0))
        t0 = float(f.get("t_start") or 0)
        ready = t0 + float(f.get("acquire_s") or 0)
        iv.append((t0, ready + float(f.get("enter_s") or 0), "setup"))
        for b in f.get("blocks") or []:
            t = float(b["t_start"])
            iv.append((t, t + float(b["wall_s"]), "replay"))
        for p in f.get("positions") or []:
            for a in p.get("attempts") or []:
                t = float(a["t_start"])
                state = "loop" if a.get("outcome") == "timed_out" else "replay"
                iv.append((t, t + float(a["wall_s"]), state))
        out[w] = (iv, max(end, float(f.get("t_end") or 0)))
    return {w: (sorted(iv), end) for w, (iv, end) in out.items()}


def state_at(intervals: List[Tuple[float, float, str]], end: float, t: float) -> str:
    """A worker's state at time t (its intervals do not overlap)."""
    if t > end:
        return "done"
    i = bisect.bisect_right([a for a, _b, _s in intervals], t) - 1
    if i >= 0 and intervals[i][0] <= t <= intervals[i][1]:
        return intervals[i][2]
    return "idle"


def _block_row(
    stem: str, b: Dict[str, Any], census_wall: Dict[Tuple[str, int], float]
) -> Dict[str, Any]:
    """A corpus block's time over its census time, and its queueing."""
    cw = census_wall.get((stem, int(b["index"])))
    ex = b.get("execution_time")
    return {
        "ratio": float(b["wall_s"]) / cw if cw else None,
        "queue_s": float(b["wall_s"]) - float(ex) if ex is not None else None,
    }


def _stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    ratio = [r["ratio"] for r in rows if r["ratio"] is not None]
    queue = [r["queue_s"] for r in rows if r["queue_s"] is not None]
    return {
        "n": len(rows),
        "ratio_p50": None if not ratio else round(float(p50(ratio) or 0), 3),
        "ratio_p95": None if not ratio else round(float(p95(ratio) or 0), 3),
        "queue_p50_s": None if not queue else round(float(p50(queue) or 0), 3),
        "queue_p95_s": None if not queue else round(float(p95(queue) or 0), 3),
    }


def _relative(groups: Dict[Tuple[int, ...], List[Dict[str, Any]]]) -> List[float]:
    """Each block's ratio while k > 0 others loop, over the p50 of the blocks
    while none loops, at the same number of others working (loop + replay)
    and none in setup."""
    base: Dict[int, Optional[float]] = {}
    for k, v in groups.items():
        if k[0] == 0 and k[2] == 0:
            base[k[1]] = _stats(v)["ratio_p50"]
    out: List[float] = []
    for k, v in groups.items():
        b0 = base.get(k[0] + k[1]) if k[0] > 0 and k[2] == 0 else None
        if b0:
            out.extend(r["ratio"] / b0 for r in v if r["ratio"] is not None)
    return out


def state_table(
    files: Sequence[Dict[str, Any]], census_wall: Dict[Tuple[str, int], float]
) -> Dict[str, Any]:
    """Every corpus block (``keep_blocks`` records), its time over its census
    time and its queueing, grouped by what the OTHER workers were doing at the
    block's midpoint (review 73f9cd4 S1: a block that merely misses a loop is
    no contention-free baseline, the others may be acquiring, the heaviest
    phase). ``loop_vs_replay.slowdown`` = the p50 of the blocks' ratios while
    k others loop over their no-loop p50 at the same number of others working
    (1.0: a loop costs the others no more than replay does)."""
    ivs = worker_intervals(files)
    groups: Dict[Tuple[int, ...], List[Dict[str, Any]]] = {}
    for f in files:
        w = int(f.get("worker") or 0)
        for b in f.get("blocks") or []:
            if b.get("timed_out") or b.get("error_kind"):
                continue
            mid = float(b["t_start"]) + float(b["wall_s"]) / 2
            others = [state_at(iv, end, mid) for v, (iv, end) in ivs.items() if v != w]
            key = tuple(others.count(s) for s in STATES)
            groups.setdefault(key, []).append(
                _block_row(str(f["stem"]), b, census_wall)
            )
    rel = _relative(groups)
    return {
        "by_state": [
            {**dict(zip(STATES, k)), **_stats(v)} for k, v in sorted(groups.items())
        ],
        "loop_vs_replay": {
            "blocks": len(rel),
            "slowdown": None if not rel else round(float(p50(rel) or 0), 3),
            "slowdown_p95": None if not rel else round(float(p95(rel) or 0), 3),
        },
        "loops": sum(1 for iv, _e in ivs.values() for x in iv if x[2] == "loop"),
    }


def census_walls(census: Dict[str, Any]) -> Dict[Tuple[str, int], float]:
    """(stem, block index) -> the census wall time of proved blocks."""
    return {
        (str(f["stem"]), int(b["index"])): float(b["wall_s"])
        for f in census.get("files", [])
        for b in f.get("blocks", [])
        if b.get("proved") and b.get("wall_s")
    }
