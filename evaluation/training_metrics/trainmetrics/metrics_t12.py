"""T1 throughput and T2 verify latency from the cell files (manifest,
replays.jsonl, samples.jsonl) and the census. Definitions (plan v1.3 §3):

- *verified* = proved (``success ∧ ¬proof_open ∧ ¬used_sorry ∧ ¬timed_out``);
- *verify-only rate* = Σ over workers of (verified chunks / that worker's
  verify time), per minute — the headline, enter excluded;
- *rate including enter* = verified chunks / cell window, per minute; the
  window starts after all sessions are acquired and before the first enter;
- *slowdown* = Σ replay time of a cell / Σ census time of the SAME files (the
  census is a W = 1 replay of every file), so it does not depend on the mix;
- T2 size classes come from the census count (1, 2–9, ≥ 10 commands); rows
  whose census count is partial are left out (review 86afb15 S3).
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .samples import load_samples, window_summary

CLASSES = (("1", 1, 1), ("2-9", 2, 9), ("10+", 10, 10**9))


def p95(xs: Sequence[float]) -> Optional[float]:
    """Nearest-rank 95th percentile (None for no data)."""
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


def p50(xs: Sequence[float]) -> Optional[float]:
    """Median (None for no data)."""
    return statistics.median(xs) if xs else None


def _r(x: Optional[float], nd: int = 3) -> Optional[float]:
    return None if x is None else round(x, nd)


def load_t1_cells(results: Path) -> List[Dict[str, Any]]:
    """Every finished T1 cell under ``results/t1``: the manifest plus
    ``_replays`` and ``_samples_path``."""
    out: List[Dict[str, Any]] = []
    for mpath in sorted((results / "t1").glob("w*/rep*/manifest.json")):
        with open(mpath, "r", encoding="utf-8") as fh:
            m = dict(json.load(fh))
        reps: List[Dict[str, Any]] = []
        rpath = mpath.with_name("replays.jsonl")
        if rpath.exists():
            with open(rpath, "r", encoding="utf-8") as fh:
                reps = [json.loads(line) for line in fh if line.strip()]
        m["_replays"] = reps
        m["_samples_path"] = str(mpath.with_name("samples.jsonl"))
        m["_dir"] = str(mpath.parent)
        out.append(m)
    return out


def census_index(
    census: Dict[str, Any],
) -> Tuple[Dict[str, float], Dict[Tuple[str, int], Tuple[Optional[int], bool]]]:
    """(stem -> census replay time, (stem, block) -> (count, partial))."""
    times: Dict[str, float] = {}
    blocks: Dict[Tuple[str, int], Tuple[Optional[int], bool]] = {}
    for f in census["files"]:
        times[f["stem"]] = float(f.get("enter_s") or 0) + sum(
            float(b.get("wall_s") or 0) for b in f["blocks"]
        )
        for b in f["blocks"]:
            blocks[(f["stem"], int(b["index"]))] = (
                b.get("n_commands"),
                bool(b.get("count_partial")),
            )
    return times, blocks


def _worker_stats(replays: List[Dict[str, Any]]) -> Dict[int, Dict[str, float]]:
    out: Dict[int, Dict[str, float]] = {}
    for rr in replays:
        w = out.setdefault(
            int(rr["worker"]),
            {"verify_s": 0.0, "enter_s": 0.0, "verified": 0.0, "verified_cmds": 0.0},
        )
        w["enter_s"] += float(rr.get("enter_s") or 0)
        for b in rr.get("blocks") or []:
            w["verify_s"] += float(b.get("wall_s") or 0)
            if b.get("proved"):
                w["verified"] += 1
                w["verified_cmds"] += float(b.get("n_commands") or 0)
    return out


def summarize_cell(  # pylint: disable=too-many-locals
    cell: Dict[str, Any], census: Dict[str, Any]
) -> Dict[str, Any]:
    """One row per T1 cell."""
    times, _ = census_index(census)
    replays = cell.get("_replays") or []
    blocks = [b for rr in replays for b in rr.get("blocks") or []]
    walls = [float(b.get("wall_s") or 0) for b in blocks]
    workers = _worker_stats(replays)
    t0, t1 = cell.get("window_start"), cell.get("window_end")
    window_s = (t1 - t0) if t0 and t1 else None
    verified = sum(1 for b in blocks if b.get("proved"))
    rate_verify = sum(
        w["verified"] / w["verify_s"] for w in workers.values() if w["verify_s"] > 0
    )
    cmds_verify = sum(
        w["verified_cmds"] / w["verify_s"]
        for w in workers.values()
        if w["verify_s"] > 0
    )
    replay_time = sum(
        float(rr.get("enter_s") or 0)
        + sum(float(b.get("wall_s") or 0) for b in rr.get("blocks") or [])
        for rr in replays
    )
    census_time = sum(times.get(rr["stem"], 0.0) for rr in replays)
    acq = [float(a["acquire_s"]) for a in cell.get("acquire") or []]
    row: Dict[str, Any] = {
        "workers": cell.get("workers"),
        "rep": cell.get("rep"),
        "outcome": cell.get("outcome"),
        "census_match": cell.get("census_id") == census.get("census_id"),
        "replays": len(replays),
        "blocks": len(blocks),
        "verified": verified,
        "timeouts": sum(1 for b in blocks if b.get("timed_out")),
        "timeout_rate": _r(
            (
                sum(1 for b in blocks if b.get("timed_out")) / len(blocks)
                if blocks
                else None
            ),
            4,
        ),
        "errors": sum(1 for b in blocks if b.get("error"))
        + sum(1 for rr in replays if rr.get("enter_error")),
        "window_s": _r(window_s, 2),
        "chunks_per_min": _r(rate_verify * 60, 1),
        "commands_per_min": _r(cmds_verify * 60, 1),
        "chunks_per_min_incl_enter": _r(
            verified / window_s * 60 if window_s else None, 1
        ),
        "slowdown_vs_census": _r(
            replay_time / census_time if census_time > 0 else None, 3
        ),
        "latency_p50_s": _r(p50(walls), 4),
        "latency_p95_s": _r(p95(walls), 4),
        "enter_p50_s": _r(p50([float(r.get("enter_s") or 0) for r in replays]), 3),
        "acquire_p50_s": _r(p50(acq), 3),
        "acquire_max_s": _r(max(acq) if acq else None, 3),
        "nodes_per_session_max": max(
            (int(v) for v in (cell.get("nodes_per_session") or {}).values()),
            default=None,
        ),
    }
    if t0 and t1:
        win = window_summary(load_samples(cell["_samples_path"]), t0, t1)
        row.update(
            {
                "cpu_cores_mean": win["cpu_cores_mean"],
                "cgroup_cores_mean": win["cgroup_cores_mean"],
                "host_cpu_util": win["host_cpu_util"],
                "jvm_cpu_s": win["cpu_s_by_role"].get("jvm"),
                "poly_cpu_s": win["cpu_s_by_role"].get("poly"),
                "pss_peak_mb": win["pss_peak_mb"],
                "samples": win["samples"],
            }
        )
    return row


SERIES_KEYS = (
    "chunks_per_min",
    "commands_per_min",
    "chunks_per_min_incl_enter",
    "slowdown_vs_census",
    "latency_p50_s",
    "latency_p95_s",
    "timeout_rate",
    "cpu_cores_mean",
    "cgroup_cores_mean",
    "host_cpu_util",
    "pss_peak_mb",
    "acquire_p50_s",
)


def series_t1(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per W: medians over the ok repeats, speedup and efficiency vs W = 1."""
    by_w: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows:
        if r.get("outcome") == "ok":
            by_w.setdefault(int(r["workers"]), []).append(r)
    out: List[Dict[str, Any]] = []
    for w in sorted(by_w):
        rs = by_w[w]
        s: Dict[str, Any] = {"workers": w, "repeats": len(rs)}
        for k in SERIES_KEYS:
            vals = [float(r[k]) for r in rs if r.get(k) is not None]
            s[k] = _r(p50(vals), 4)
        out.append(s)
    base = next((s for s in out if s["workers"] == 1), None)
    for s in out:
        b = base["chunks_per_min"] if base else None
        speed = s["chunks_per_min"] / b if b and s["chunks_per_min"] else None
        s["speedup"] = _r(speed, 3)
        s["efficiency"] = _r(speed / s["workers"] if speed else None, 3)
    return out


def t2_table(  # pylint: disable=too-many-locals
    cells: List[Dict[str, Any]], census: Dict[str, Any], t2_workers: Sequence[int]
) -> List[Dict[str, Any]]:
    """Per (W, size class): n, p50/p95 latency, timeout rate, ms per command."""
    _, idx = census_index(census)
    acc: Dict[Tuple[int, str], List[Tuple[float, bool, int]]] = {}
    for cell in cells:
        w = int(cell.get("workers") or 0)
        if w not in t2_workers or cell.get("outcome") != "ok":
            continue
        for rr in cell.get("_replays") or []:
            for b in rr.get("blocks") or []:
                n, partial = idx.get((rr["stem"], int(b["index"])), (None, False))
                if n is None or partial:
                    continue
                cls = next(c for c, lo, hi in CLASSES if lo <= int(n) <= hi)
                acc.setdefault((w, cls), []).append(
                    (float(b.get("wall_s") or 0), bool(b.get("timed_out")), int(n))
                )
    out: List[Dict[str, Any]] = []
    for w in sorted(set(k[0] for k in acc)):
        for cls, _lo, _hi in CLASSES:
            pts = acc.get((w, cls), [])
            if not pts:
                continue
            walls = [p[0] for p in pts if not p[1]]
            out.append(
                {
                    "workers": w,
                    "class": cls,
                    "n": len(pts),
                    "latency_p50_s": _r(p50(walls), 4),
                    "latency_p95_s": _r(p95(walls), 4),
                    "timeout_rate": _r(sum(1 for p in pts if p[1]) / len(pts), 4),
                    "ms_per_command_p50": _r(
                        p50([1000 * p[0] / p[2] for p in pts if not p[1] and p[2]]), 2
                    ),
                }
            )
    return out
