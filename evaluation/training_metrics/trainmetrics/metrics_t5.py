"""T5 numbers from ``positions.jsonl`` records (design §T5, review S8): per
family and budget the fraction returned as ``timed_out`` (the guarantee) and,
separately, the fraction with ``stuck_line`` (the diagnostic); the undo cost
past the budget; whether the node source was unchanged; per family the seeds
that did not loop (review 7bedac5 S1) and the corpus block after the seeds:
verdict as in the census, and its time (recovery) against the census time.
Memory and a dead session are their own classes, outside the fractions
(review beac25c S1). What a timeout leaves behind (S2) comes from the
sampler: the prover that ran the attempt (the prover process with the most CPU
in the attempt's window; at W = 1 the attempt's own) and its RSS just before,
at peak and just after (the session rows of /api/v1/memory are probed at most
every 15 s, too seldom for 5 s attempts; they still give the server tier)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .metrics_t12 import p50, p95

OUTCOMES = ("timed_out", "failed", "kept", "error")
APART = ("memory", "prover_died", "session_gone")  # not counted in the fractions


def _r(x: Any, nd: int = 3) -> Any:
    return None if x is None else round(float(x), nd)


def _frac(k: int, n: int) -> Optional[float]:
    return round(k / n, 3) if n else None


def attempts(files: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every seed attempt, with its file and position."""
    return [
        {**a, "stem": f["stem"], "position": p["position"]}
        for f in files
        for p in f.get("positions") or []
        for a in p.get("attempts") or []
    ]


MB = 2**20


def load_samples(path: Path) -> List[Dict[str, Any]]:
    """The sampler rows of a cell (unreadable lines skipped)."""
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def _polys(row: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    return {int(p["pid"]): p for p in row.get("procs") or [] if p.get("role") == "poly"}


def _after_row(
    samples: Sequence[Dict[str, Any]], t1: float, t_limit: Optional[float]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """The last row in the quiet pause [t1, t_limit] and why it is censored."""
    first_after = next((r for r in samples if float(r["t"]) >= t1), None)
    if t_limit is None:
        return first_after, "no_pause"
    quiet = [r for r in samples if t1 <= float(r["t"]) <= t_limit]
    return (quiet[-1], None) if quiet else (first_after, "no_row_in_pause")


def _busiest(
    pb: Dict[int, Dict[str, Any]], window: Sequence[Dict[str, Any]]
) -> Optional[Tuple[int, Dict[int, Dict[str, Any]]]]:
    """The prover with the most CPU from ``pb`` (before) to its last row in
    ``window``, and every prover's last row; None when none worked."""
    last: Dict[int, Dict[str, Any]] = {}
    for r in window:
        last.update({pid: q for pid, q in _polys(r).items() if pid in pb})
    busy = {
        pid: int(q.get("cpu_ticks") or 0) - int(pb[pid].get("cpu_ticks") or 0)
        for pid, q in last.items()
    }
    if not busy or max(busy.values()) <= 0:
        return None
    return max(busy, key=lambda q: busy[q]), last


def prover_memory(
    samples: Sequence[Dict[str, Any]],
    t0: float,
    t1: float,
    t_limit: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """The prover that ran during [t0, t1] and its RSS before, at peak and
    after (MB). before = the last sample at or before t0; after = the last
    sample in [t1, t_limit], the quiet pause after the call before the next call
    on the session (review 1676d29 B1: a later row sees the next call or the
    session closing); the prover = the one with the most CPU from before to
    after. ``censored`` names why "after" cannot be trusted (no row in the
    pause, no pause recorded, the prover gone) and then ``retained_mb`` is
    None. ``retained_mb`` can be negative: Poly/ML may compact its heap during
    a long loop and end it smaller than it began (seen live). None when no
    prover worked in the window."""
    before = next((r for r in reversed(samples) if float(r["t"]) <= t0), None)
    if before is None:
        return None
    after, censored = _after_row(samples, t1, t_limit)
    stop = float(after["t"]) if after is not None else t1
    window = [r for r in samples if t0 < float(r["t"]) <= stop]
    found = _busiest(_polys(before), window)
    if found is None:
        return None
    pid, last = found
    if after is None or pid not in _polys(after):
        censored = censored or "prover_gone"
    rb = int(_polys(before)[pid]["rss"])
    peak = max([rb] + [int(_polys(r)[pid]["rss"]) for r in window if pid in _polys(r)])
    ra = int(last[pid]["rss"])
    return {
        "pid": pid,
        "rss_before_mb": round(rb / MB, 1),
        "rss_peak_mb": round(peak / MB, 1),
        "rss_after_mb": round(ra / MB, 1),
        "retained_mb": None if censored else round((ra - rb) / MB, 1),
        "censored": censored,
    }


def annotate(
    files: Sequence[Dict[str, Any]], samples: Sequence[Dict[str, Any]]
) -> None:
    """Add ``prover_mem`` to every attempt of the records (in place)."""
    if not samples:
        return
    for f in files:
        for p in f.get("positions") or []:
            for a in p.get("attempts") or []:
                t0 = float(a.get("t_start") or 0)
                t1 = t0 + float(a["wall_s"])
                lim = a.get("t_settled")
                a["prover_mem"] = prover_memory(
                    samples, t0, t1, None if lim is None else float(lim)
                )


def _cell(all_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = [a for a in all_rows if a["outcome"] not in APART]
    n = len(rows)
    out: Dict[str, Any] = {"attempts": n}
    for o in OUTCOMES + APART:
        out[o] = sum(1 for a in all_rows if a["outcome"] == o)
    out["timed_out_frac"] = _frac(out["timed_out"], n)
    out["stuck_line_frac"] = _frac(
        sum(1 for a in rows if a.get("stuck_line") is not None), n
    )
    undo = [float(a["undo_s"]) for a in rows if a.get("undo_s") is not None]
    mem = [a["prover_mem"] for a in rows if a.get("prover_mem")]
    kept_mb = [float(m["retained_mb"]) for m in mem if m.get("retained_mb") is not None]
    peak_up = [float(m["rss_peak_mb"]) - float(m["rss_before_mb"]) for m in mem]
    known = [a for a in rows if a.get("source_unchanged") is not None]
    out.update(
        {
            "undo_p50_s": _r(p50(undo)),
            "undo_p95_s": _r(p95(undo)),
            "undo_max_s": _r(max(undo) if undo else None),
            "source_unchanged_frac": _frac(
                sum(1 for a in known if a["source_unchanged"]), len(known)
            ),
            "rolled_back": sum(1 for a in rows if a.get("rolled_back")),
            "wall_max_s": _r(max((float(a["wall_s"]) for a in rows), default=None)),
            "retained_p50_mb": _r(p50(kept_mb), 1),
            "retained_max_mb": _r(max(kept_mb) if kept_mb else None, 1),
            "retained_censored": sum(1 for m in mem if m.get("censored")),
            "peak_growth_max_mb": _r(max(peak_up) if peak_up else None, 1),
            "prover_peak_max_mb": _r(
                max((float(m["rss_peak_mb"]) for m in mem), default=None), 1
            ),
            "tiers_after": sorted(
                {
                    str((a.get("mem_after") or {}).get("state"))
                    for a in rows
                    if a.get("mem_after")
                }
            ),
        }
    )
    return out


def _family(
    files: Sequence[Dict[str, Any]], rows: List[Dict[str, Any]], fam: str
) -> Dict[str, Any]:
    first: Dict[str, Dict[str, Any]] = {}
    for a in sorted(
        (a for a in rows if a["family"] == fam),
        key=lambda x: (x["seed_id"], x["budget_s"]),
    ):
        first.setdefault(a["seed_id"], a)  # the attempt at the smallest budget
    not_looping = [
        a for a in first.values() if a["outcome"] in ("failed", "kept", "error")
    ]
    pos = [
        p
        for f in files
        for p in f.get("positions") or []
        if not p.get("stopped")
        and any(x["family"] == fam for x in p.get("attempts") or [])
    ]
    rec = [float(p["recovery_s"]) for p in pos if p.get("recovery_s") is not None]
    cen = [float(p["census_wall_s"]) for p in pos if p.get("census_wall_s") is not None]
    return {
        "seeds": len(first),
        "goal_sources": sorted({str(a.get("goal_source")) for a in first.values()}),
        "not_looping": len(not_looping),
        "not_looping_outcomes": {
            o: sum(1 for a in not_looping if a["outcome"] == o)
            for o in ("failed", "kept", "error")
        },
        "not_looping_errors": sorted(
            {str(a["first_error"])[:80] for a in not_looping if a.get("first_error")}
        )[:5],
        "ended_apart": {
            o: sum(1 for a in rows if a["family"] == fam and a["outcome"] == o)
            for o in APART
        },
        "verdict_match_frac": _frac(
            sum(1 for p in pos if p.get("verdict_match")), len(pos)
        ),
        "recovery_p50_s": _r(p50(rec)),
        "census_p50_s": _r(p50(cen)),
    }


def summarize(files: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The cell's T5 table rows (by family x budget) and per-family recovery."""
    rows = attempts(files)
    by: Dict[Tuple[str, float], List[Dict[str, Any]]] = {}
    for a in rows:
        by.setdefault((a["family"], float(a["budget_s"])), []).append(a)
    return {
        "files": len(files),
        "acquire_errors": sum(1 for f in files if f.get("acquire_error")),
        "enter_errors": sum(1 for f in files if f.get("enter_error")),
        "positions": sum(len(f.get("positions") or []) for f in files),
        "stopped_files": {f["stem"]: f["stopped"] for f in files if f.get("stopped")},
        "table": [
            {"family": fam, "budget_s": b, **_cell(v)}
            for (fam, b), v in sorted(by.items())
        ],
        "families": {
            fam: _family(files, rows, fam)
            for fam in sorted({a["family"] for a in rows})
        },
    }
