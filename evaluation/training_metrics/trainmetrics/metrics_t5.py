"""T5 numbers from ``positions.jsonl`` records (design §T5, review S8): per
family and budget the fraction returned as ``timed_out`` (the guarantee) and,
separately, the fraction with ``stuck_line`` (the diagnostic); the undo cost
past the budget; whether the node source was unchanged; per family the seeds
that did not loop (review 7bedac5 S1) and the corpus block after the seeds:
verdict as in the census, and its time (recovery) against the census time."""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from .metrics_t12 import p50, p95


def _r(x: Any, nd: int = 3) -> Any:
    return None if x is None else round(float(x), nd)


def attempts(files: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every seed attempt, with its file and position."""
    return [
        {**a, "stem": f["stem"], "position": p["position"]}
        for f in files
        for p in f.get("positions") or []
        for a in p.get("attempts") or []
    ]


def _cell(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    out: Dict[str, Any] = {"attempts": n}
    for o in ("timed_out", "failed", "kept", "error"):
        out[o] = sum(1 for a in rows if a["outcome"] == o)
    out["timed_out_frac"] = _r(out["timed_out"] / n) if n else None
    out["stuck_line_frac"] = (
        _r(sum(1 for a in rows if a.get("stuck_line") is not None) / n) if n else None
    )
    undo = [float(a["undo_s"]) for a in rows if a.get("undo_s") is not None]
    out.update(
        {
            "undo_p50_s": _r(p50(undo)),
            "undo_p95_s": _r(p95(undo)),
            "undo_max_s": _r(max(undo) if undo else None),
        }
    )
    known = [a for a in rows if a.get("source_unchanged") is not None]
    out["source_unchanged_frac"] = (
        _r(sum(1 for a in known if a["source_unchanged"]) / len(known))
        if known
        else None
    )
    out["rolled_back"] = sum(1 for a in rows if a.get("rolled_back"))
    out["wall_max_s"] = _r(max((float(a["wall_s"]) for a in rows), default=None))
    return out


def summarize(files: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The cell's T5 table rows (by family x budget) and per-family recovery."""
    rows = attempts(files)
    by: Dict[Tuple[str, float], List[Dict[str, Any]]] = {}
    for a in rows:
        by.setdefault((a["family"], float(a["budget_s"])), []).append(a)
    table = [
        {"family": fam, "budget_s": b, **_cell(v)} for (fam, b), v in sorted(by.items())
    ]
    families: Dict[str, Any] = {}
    for fam in sorted({a["family"] for a in rows}):
        first: Dict[str, Dict[str, Any]] = {}
        for a in sorted(
            (a for a in rows if a["family"] == fam),
            key=lambda x: (x["seed_id"], x["budget_s"]),
        ):
            first.setdefault(a["seed_id"], a)  # the attempt at the smallest budget
        not_looping = [a for a in first.values() if a["outcome"] != "timed_out"]
        pos = [
            p
            for f in files
            for p in f.get("positions") or []
            if any(x["family"] == fam for x in p.get("attempts") or [])
        ]
        rec = [float(p["recovery_s"]) for p in pos if p.get("recovery_s") is not None]
        cen = [
            float(p["census_wall_s"]) for p in pos if p.get("census_wall_s") is not None
        ]
        families[fam] = {
            "seeds": len(first),
            "goal_sources": sorted({str(a.get("goal_source")) for a in first.values()}),
            "not_looping": len(not_looping),
            "not_looping_outcomes": {
                o: sum(1 for a in not_looping if a["outcome"] == o)
                for o in ("failed", "kept", "error")
            },
            "verdict_match_frac": (
                _r(sum(1 for p in pos if p.get("verdict_match")) / len(pos))
                if pos
                else None
            ),
            "recovery_p50_s": _r(p50(rec)),
            "census_p50_s": _r(p50(cen)),
        }
    return {
        "files": len(files),
        "acquire_errors": sum(1 for f in files if f.get("acquire_error")),
        "enter_errors": sum(1 for f in files if f.get("enter_error")),
        "positions": sum(len(f.get("positions") or []) for f in files),
        "table": table,
        "families": families,
    }
