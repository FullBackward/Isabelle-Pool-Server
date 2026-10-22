"""Contention measures shared by the T5 contention cell (design §T5, plan
stage 3c) and T6: the server's memory tier over the whole cell (review
beac25c S2: a loop that pushes the server to warning / critical changes
admission for the other workers), and how the other workers' calls fare while
one session sits in a loop: each corpus block's time against its census time,
for calls that overlap another worker's loop and for calls that do not, with
the queueing part (client wall minus the server's execution time; plan_v1.6
S6)."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from .metrics_t12 import p50, p95


def executor_threads() -> int:
    """The server's default backend executor size, ``min(32, cpu_count + 4)``
    (``asyncio.to_thread``; the server runs on this host): W = 32 sits on it."""
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


def loop_windows(files: Sequence[Dict[str, Any]]) -> List[Tuple[int, float, float]]:
    """(worker, start, end) of every seed attempt that timed out."""
    return [
        (
            int(f.get("worker") or 0),
            float(a["t_start"]),
            float(a["t_start"]) + float(a["wall_s"]),
        )
        for f in files
        for p in f.get("positions") or []
        for a in p.get("attempts") or []
        if a.get("outcome") == "timed_out"
    ]


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


def overlap_table(
    files: Sequence[Dict[str, Any]], census_wall: Dict[Tuple[str, int], float]
) -> Dict[str, Any]:
    """Each corpus block of every worker (``keep_blocks`` records): its wall
    time over its census wall time and its queueing, split by whether it
    overlaps a loop of ANOTHER worker. ``slowdown`` = the overlapping p50 ratio
    over the clear one (1.0: a loop on one session costs the others nothing)."""
    loops = loop_windows(files)
    split: Dict[str, List[Dict[str, Any]]] = {"overlapping": [], "clear": []}
    for f in files:
        w = int(f.get("worker") or 0)
        for b in f.get("blocks") or []:
            if b.get("timed_out") or b.get("error_kind"):
                continue
            t0 = float(b["t_start"])
            t1 = t0 + float(b["wall_s"])
            hit = any(lw != w and s < t1 and t0 < e for lw, s, e in loops)
            split["overlapping" if hit else "clear"].append(
                _block_row(str(f["stem"]), b, census_wall)
            )
    out: Dict[str, Any] = {k: _stats(v) for k, v in split.items()}
    o, c = out["overlapping"]["ratio_p50"], out["clear"]["ratio_p50"]
    out["slowdown"] = round(o / c, 3) if o and c else None
    out["loops"] = len(loops)
    return out


def census_walls(census: Dict[str, Any]) -> Dict[Tuple[str, int], float]:
    """(stem, block index) -> the census wall time of proved blocks."""
    return {
        (str(f["stem"]), int(b["index"])): float(b["wall_s"])
        for f in census.get("files", [])
        for b in f.get("blocks", [])
        if b.get("proved") and b.get("wall_s")
    }
