"""Reading a cell's ``samples.jsonl`` (the memscale sampler) over a time window:
CPU from per-pid tick deltas and from the cgroup's ``cpu.stat``, host CPU
utilisation, and memory peaks. Ticks are cumulative per process, so totals are
never differenced (review a66fa6e S1)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


def load_samples(path: str) -> List[Dict[str, Any]]:
    """Rows of a samples.jsonl (empty when the file is missing)."""
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except OSError:
        return []
    return rows


def in_window(rows: List[Dict[str, Any]], t0: float, t1: float) -> List[Dict[str, Any]]:
    """Rows with ``t0 <= t <= t1``."""
    return [r for r in rows if t0 <= float(r.get("t", 0)) <= t1]


def cpu_by_role(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    """CPU seconds per role over consecutive rows, from per-pid deltas: a pid
    contributes ``ticks(row i) - ticks(row i-1)`` for every pair of rows that
    both contain it (clamped at 0), so a process that appears contributes from
    its first sample on and one that vanishes contributes up to its last."""
    out: Dict[str, float] = {}
    prev: Dict[int, int] = {}
    for r in rows:
        tck = int(r.get("clk_tck") or 100)
        cur: Dict[int, int] = {}
        for p in r.get("procs") or []:
            ticks = p.get("cpu_ticks")
            if ticks is None:
                continue
            pid = int(p["pid"])
            cur[pid] = int(ticks)
            if pid in prev:
                delta = max(0, int(ticks) - prev[pid])
                role = str(p.get("role") or "other")
                out[role] = out.get(role, 0.0) + delta / tck
        prev = cur
    out["total"] = sum(v for k, v in out.items() if k != "total")
    return {k: round(v, 3) for k, v in out.items()}


def host_cpu_utilisation(rows: List[Dict[str, Any]]) -> Optional[float]:
    """Busy share of all host CPUs between the first and last row (0..1)."""
    pts = [
        r["host_cpu"]
        for r in rows
        if (r.get("host_cpu") or {}).get("total") is not None
    ]
    if len(pts) < 2:
        return None
    d_total = pts[-1]["total"] - pts[0]["total"]
    d_busy = pts[-1]["busy"] - pts[0]["busy"]
    return round(d_busy / d_total, 4) if d_total > 0 else None


def cgroup_cpu_seconds(rows: List[Dict[str, Any]]) -> Optional[float]:
    """CPU seconds of the tool's cgroup between the first and last row, from
    ``cpu.stat usage_usec`` (exact, incl. exited processes; review a66fa6e S2)."""
    pts: List[int] = []
    for r in rows:
        usage = (r.get("cgroup_cpu") or {}).get("usage_usec")
        if usage is not None:
            pts.append(int(usage))
    if len(pts) < 2:
        return None
    return round((pts[-1] - pts[0]) / 1e6, 3)


def memory_peaks(rows: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """Peak summed PSS of the tool and peak host used over the rows (MB), and
    host used at the first row."""
    mb = 2**20
    pss = [
        float(r["sum"]["total"]["pss"])
        for r in rows
        if (r.get("sum") or {}).get("total")
    ]
    host = [
        float(r["host"]["used"])
        for r in rows
        if (r.get("host") or {}).get("used") is not None
    ]
    return {
        "pss_peak_mb": round(max(pss) / mb, 1) if pss else None,
        "host_used_first_mb": round(host[0] / mb, 1) if host else None,
        "host_used_peak_mb": round(max(host) / mb, 1) if host else None,
    }


def window_summary(rows: List[Dict[str, Any]], t0: float, t1: float) -> Dict[str, Any]:
    """CPU and memory over ``[t0, t1]`` for a cell manifest / table row."""
    win = in_window(rows, t0, t1)
    span = max(1e-9, t1 - t0)
    cpu = cpu_by_role(win)
    cg = cgroup_cpu_seconds(win)
    return {
        "samples": len(win),
        "cpu_s_by_role": cpu,
        "cpu_cores_mean": round(cpu.get("total", 0.0) / span, 3),
        "cgroup_cpu_s": cg,
        "cgroup_cores_mean": None if cg is None else round(cg / span, 3),
        "host_cpu_util": host_cpu_utilisation(win),
        **memory_peaks(win),
    }
