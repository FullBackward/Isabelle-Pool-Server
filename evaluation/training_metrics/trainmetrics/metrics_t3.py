"""T3 summary (plan stage 5): the branch operations' p50 / p95, the two
alternatives per position stratum against the branch cost, the branch cost
against the distance it goes back, and the integrity checks.

Per state (:func:`derived`), means over the repeats:

- *restore return*: when ``restore_checkpoint`` returns; it does not wait for
  the prover, so it is reported apart and is not the cost of going back;
- *at once*: the verify of block i + 1 right after a restore (no call between);
- *settled*: the verify of the same block when the node had settled ((b), and
  each one after a rollback, which waits for the node);
- *branch* = restore return + (at once - settled): what going back and
  continuing costs over continuing from a settled state (review 07120bc B1).

A verify's time comes in steps of the server's status poll (``verify_chunk``
re-reads the node every ``session.output_delay``, 0.1 s: Document_Utils
.node_status_report), so work under one step shows only as a shift in how
often a verify lands a step higher. Medians snap to the steps; means do not,
so the per-state values are means, nothing is clipped at zero, and the branch
cost is reported as a mean over states with its 95 % interval (normal
approximation, 1.96 standard errors).

(e) = ``enter_s + replay_s`` of the new node on the same session; (f) =
``create_s`` + the same of the created session's first node. A ratio is a
lower bound: the alternative's p50 over *branch at most*, the upper end of the
branch mean's 95 % interval on the same states, and never below the restore's
mean return time (going back costs at least that). A branch cost near zero,
as for short blocks, would make the plain ratio meaningless. A state is
*complete* when (a)-(f) all ran; stopped states (a session that died) and
errors are counted and listed apart."""

from __future__ import annotations

import math
import statistics
from typing import Any, Dict, List, Optional, Sequence

from .metrics_t12 import CLASSES, p50, p95, size_class

STRATA = ("early", "middle", "late")
REPORT_KEYS = ("proved", "success", "n_commands", "statuses", "first_error")


def _r(x: Optional[float], nd: int = 4) -> Optional[float]:
    return None if x is None else round(float(x), nd)


def _mean(xs: Sequence[Optional[float]]) -> Optional[float]:
    vals = [float(x) for x in xs if x is not None]
    return statistics.fmean(vals) if vals else None


def _ci95(xs: Sequence[float]) -> Optional[float]:
    """Half-width of the mean's 95 % interval (None below two values)."""
    if len(xs) < 2:
        return None
    return 1.96 * statistics.stdev(xs) / math.sqrt(len(xs))


def _stats(xs: Sequence[float]) -> Dict[str, Any]:
    return {
        "n": len(xs),
        "p50_s": _r(p50(xs)),
        "p95_s": _r(p95(xs)),
        "mean_s": _r(_mean(xs)),
        "ci95_s": _r(_ci95(xs)),
    }


def warm_s(s: Dict[str, Any]) -> Optional[float]:
    """(e): a new node on the same session, enter + replay."""
    w = s.get("warm") or {}
    if w.get("error") or not w:
        return None
    return float(w["enter_s"]) + float(w["replay_s"])


def fresh_s(s: Dict[str, Any]) -> Optional[float]:
    """(f): create + enter + replay."""
    f = s.get("fresh") or {}
    if f.get("error") or not f or s.get("create_s") is None:
        return None
    return float(s["create_s"]) + float(f["enter_s"]) + float(f["replay_s"])


def derived(s: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """The state's means: restore return, at once, settled, extra, branch,
    rollback (None where the state did not get that far)."""
    restores = s.get("restores") or []
    rollbacks = s.get("rollbacks") or []
    restore = _mean([r.get("restore_s") for r in restores])
    at_once = _mean([(r.get("again") or {}).get("wall_s") for r in restores])
    views = [s.get("cont") or {}] + [r.get("settled") or {} for r in rollbacks]
    settled = _mean([v.get("wall_s") for v in views if v.get("success")])
    extra = None if at_once is None or settled is None else at_once - settled
    return {
        "restore_s": restore,
        "at_once_s": at_once,
        "settled_s": settled,
        "extra_s": extra,
        "branch_s": None if restore is None or extra is None else restore + extra,
        "rollback_s": _mean([r.get("rollback_s") for r in rollbacks]),
    }


def complete(s: Dict[str, Any]) -> bool:
    """(a)-(f) all ran on this state."""
    d = derived(s)
    return (
        not s.get("stopped")
        and not s.get("error")
        and d["branch_s"] is not None
        and d["rollback_s"] is not None
        and warm_s(s) is not None
        and fresh_s(s) is not None
    )


def _col(ds: Sequence[Dict[str, Optional[float]]], key: str) -> List[float]:
    return [float(v) for v in (d[key] for d in ds) if v is not None]


def _ratio(num: Optional[float], den: Optional[float]) -> Optional[float]:
    return None if num is None or den is None or den <= 0 else num / den


def _by_stratum(done: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for st in STRATA:
        ss = [s for s in done if s["stratum"] == st]
        if not ss:
            continue
        w = [x for x in (warm_s(s) for s in ss) if x is not None]
        f = [x for x in (fresh_s(s) for s in ss) if x is not None]
        branch = _col([derived(s) for s in ss], "branch_s")
        mean, ci = _mean(branch), _ci95(branch)
        floor = _mean(_col([derived(s) for s in ss], "restore_s")) or 0.0
        upper = None if mean is None else max(mean + (ci or 0.0), floor)
        out.append(
            {
                "stratum": st,
                "states": len(ss),
                "prefix_commands_p50": _r(
                    p50([float(s["prefix_commands"]) for s in ss]), 1
                ),
                "warm_p50_s": _r(p50(w)),
                "warm_p95_s": _r(p95(w)),
                "fresh_p50_s": _r(p50(f)),
                "fresh_p95_s": _r(p95(f)),
                "branch_mean_s": _r(mean),
                "branch_ci95_s": _r(ci),
                "branch_at_most_s": _r(upper),
                "warm_over_branch_min": _r(_ratio(p50(w), upper), 1),
                "fresh_over_branch_min": _r(_ratio(p50(f), upper), 1),
            }
        )
    return out


def _by_distance(done: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for cls, _lo, _hi in CLASSES:
        ds = [derived(s) for s in done if size_class(s.get("distance")) == cls]
        if ds:
            out.append(
                {
                    "distance": cls,
                    **_stats(_col(ds, "branch_s")),
                    "restore_return_p50_s": _r(p50(_col(ds, "restore_s"))),
                }
            )
    return out


def _integrity(states: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    restores = [r for s in states for r in s.get("restores") or []]
    rollbacks = [r for s in states for r in s.get("rollbacks") or []]
    pairs = [
        (v, s.get("cont") or {})
        for s in states
        for v in [r.get("again") for r in s.get("restores") or []]
        + [r.get("settled") for r in s.get("rollbacks") or []]
        if v
    ]
    return {
        "restores": len(restores),
        "restore_applied": sum(1 for r in restores if r.get("restore_ok")),
        "source_after_restore": sum(1 for r in restores if r.get("source_ok")),
        "rollbacks": len(rollbacks),
        "rollback_applied": sum(1 for r in rollbacks if r.get("rollback_ok")),
        "source_after_rollback": sum(1 for r in rollbacks if r.get("source_ok")),
        "repeated_verifies": len(pairs),
        "identical": sum(
            1 for v, b in pairs if all(v.get(k) == b.get(k) for k in REPORT_KEYS)
        ),
    }


def summarize_t3(states: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The T3 numbers from the cell's state records."""
    done = [s for s in states if complete(s)]
    ds = [derived(s) for s in done]
    mismatched = [
        (s["stem"], s["index"])
        for s in states
        if (s.get("fresh") or {}).get("blocks")
        and (s["fresh"].get("proved") or 0) < int(s.get("census_prefix_proved") or 0)
    ]
    return {
        "states": len(states),
        "complete": len(done),
        "ops": {
            "save": _stats([float(s["save_s"]) for s in done]),
            "restore_return": _stats(_col(ds, "restore_s")),
            "at_once": _stats(_col(ds, "at_once_s")),
            "settled": _stats(_col(ds, "settled_s")),
            "extra": _stats(_col(ds, "extra_s")),
            "branch": _stats(_col(ds, "branch_s")),
            "rollback": _stats(_col(ds, "rollback_s")),
            "create": _stats([float(s["create_s"]) for s in done]),
        },
        "by_stratum": _by_stratum(done),
        "by_distance": _by_distance(done),
        "integrity": _integrity(states),
        "continuation_failed": [
            (s["stem"], s["index"])
            for s in states
            if s.get("cont") and not s["cont"].get("success")
        ],
        "prefix_short_of_census": mismatched,
        "stopped": [
            (s["stem"], s["index"], s["stopped"]) for s in states if s.get("stopped")
        ],
        "errors": [
            (s["stem"], s["index"], s["error"]) for s in states if s.get("error")
        ],
    }
