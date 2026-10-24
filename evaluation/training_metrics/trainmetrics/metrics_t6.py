"""T6 summary of one cell (design §T6 Metrics, plan stage 6), from the event
log of :mod:`trainmetrics.t6_core` and the file records.

Rates (S2), defined once:

- *end-to-end rate* = rollouts / the cell's window (acquire, enter, prefix,
  advances and integrity reads inside), per hour;
- *loop rate* = sum over workers of rollouts_w / loop_w, per hour, where
  loop_w is worker w's wall time from its first checkpoint to the end of its
  last rollout (or the restore after it), minus the acquire, enter, prefix,
  advance, rollback, integrity and close time that started inside that span
  (the close: review f0d85df S2, a worker's span covers its file switches).

Integrity reads sit before a group's checkpoint and at its end, never between
loop calls (:mod:`trainmetrics.t6_core`), so excluding them moves no loop
work out of the loop time.

A *rollout* is a candidate whose outcome is proved, kept unproved, failed or
timed out (``t6_core.COUNTED``); memory, prover death, a gone session and
errors are counted apart, and so are the candidates of a group whose
integrity check failed (review f0d85df S1: they ran in a changed document). *Problems* are completed groups. Time shares are
call time per kind (the rollouts per outcome) over W x the window; the rest
is the client's time between calls and workers that finished early.
Queueing (S6) is a verify's client wall minus the server's execution time."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .metrics_t12 import p50, p95
from .t6_core import COUNTED

LOOP = ("checkpoint", "rollout", "restore")
OUTSIDE = ("acquire", "enter", "advance", "rollback", "integrity", "close")


def _r(x: Optional[float], nd: int = 3) -> Optional[float]:
    return None if x is None else round(float(x), nd)


def _stats(xs: Sequence[float]) -> Dict[str, Any]:
    return {"n": len(xs), "p50_s": _r(p50(xs)), "p95_s": _r(p95(xs))}


def _end(e: Dict[str, Any]) -> float:
    return float(e["t_start"]) + float(e["wall_s"])


def loop_time(events: Sequence[Dict[str, Any]]) -> Optional[float]:
    """One worker's loop time (module docstring); None without a checkpoint."""
    cps = [e for e in events if e["kind"] == "checkpoint"]
    ends = [
        _end(e)
        for e in events
        if e["kind"] in ("rollout", "restore") and e.get("outcome") != "not_applicable"
    ]
    if not cps or not ends:
        return None
    t0, t1 = min(float(e["t_start"]) for e in cps), max(ends)
    outside = sum(
        float(e["wall_s"])
        for e in events
        if e["kind"] in OUTSIDE and t0 <= float(e["t_start"]) < t1
    )
    return max(0.0, t1 - t0 - outside)


def _key(e: Dict[str, Any]) -> Tuple[Any, Any, Any]:
    return (e["worker"], e["stem"], e.get("index"))


def failed_groups(events: Sequence[Dict[str, Any]]) -> Set[Tuple[Any, Any, Any]]:
    """(worker, stem, index) of the groups whose end check failed."""
    return {
        _key(e)
        for e in events
        if e["kind"] == "integrity"
        and e.get("at") == "group_end"
        and e.get("ok") is False
    }


def _rollouts(events: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bad = failed_groups(events)
    return [
        e
        for e in events
        if e["kind"] == "rollout" and e.get("outcome") in COUNTED and _key(e) not in bad
    ]


def _rates(
    events: Sequence[Dict[str, Any]], files: Sequence[Dict[str, Any]], window_s: float
) -> Dict[str, Any]:
    roll = _rollouts(events)
    per_worker: Dict[int, Dict[str, Any]] = {}
    loop = 0.0
    for w in sorted({int(e["worker"]) for e in events}):
        evs = [e for e in events if int(e["worker"]) == w]
        lt, n = loop_time(evs), len(_rollouts(evs))
        per_worker[w] = {"rollouts": n, "loop_s": _r(lt)}
        loop += n / lt if lt else 0.0
    hours = window_s / 3600 if window_s > 0 else None
    proved = sum(1 for e in roll if e["outcome"] == "proved")
    problems = sum(int(f.get("groups_done") or 0) for f in files)
    return {
        "rollouts": len(roll),
        "proved_rollouts": proved,
        "problems": problems,
        "rollouts_per_h": _r(len(roll) / hours, 1) if hours else None,
        "proved_per_h": _r(proved / hours, 1) if hours else None,
        "problems_per_h": _r(problems / hours, 1) if hours else None,
        "loop_rollouts_per_h": _r(loop * 3600, 1),
        "workers": per_worker,
    }


def _shares(
    events: Sequence[Dict[str, Any]], workers: int, window_s: float
) -> Dict[str, Optional[float]]:
    total = workers * window_s
    out: Dict[str, float] = {}
    for e in events:
        if e["kind"] == "rollout":
            key = f"verify_{e.get('outcome')}"
        elif e["kind"] == "advance":
            key = str(e.get("phase") or "advance")
        else:
            key = str(e["kind"])
        out[key] = out.get(key, 0.0) + float(e["wall_s"])
    shares: Dict[str, Optional[float]] = {
        k: _r(v / total, 4) if total > 0 else None for k, v in sorted(out.items())
    }
    shares["between_calls_or_idle"] = (
        _r(1 - sum(out.values()) / total, 4) if total > 0 else None
    )
    return shares


def _by_candidate(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for kind in sorted(
        {str(e.get("candidate")) for e in events if e["kind"] == "rollout"}
    ):
        es = [
            e for e in events if e["kind"] == "rollout" and e.get("candidate") == kind
        ]
        ran = [e for e in es if e.get("outcome") in COUNTED]
        counts: Dict[str, int] = {}
        for e in es:
            counts[str(e.get("outcome"))] = counts.get(str(e.get("outcome")), 0) + 1
        out[kind] = {
            "outcomes": counts,
            "wall": _stats([float(e["wall_s"]) for e in ran]),
            "timeout_share": (
                _r(sum(1 for e in ran if e["outcome"] == "timed_out") / len(ran), 4)
                if ran
                else None
            ),
        }
    return out


def _matched(events: Sequence[Dict[str, Any]], stems: Sequence[str]) -> Dict[str, Any]:
    """Per-rollout loop time on the W = 1 cell's files (S3): checkpoint,
    rollouts and restores of their groups over their rollouts."""
    evs = [e for e in events if e["stem"] in set(stems) and e["kind"] in LOOP]
    n = len(_rollouts(evs))
    t = sum(float(e["wall_s"]) for e in evs)
    return {
        "stems": list(stems),
        "rollouts": n,
        "per_rollout_s": _r(t / n, 4) if n else None,
    }


def _counts(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Integrity checks, advances, and the outcomes outside the rollouts."""
    integ = [e for e in events if e["kind"] == "integrity" and e.get("ok") is not None]
    adv = [e for e in events if e["kind"] == "advance"]
    bad = failed_groups(events)
    groups = {_key(e) for e in events if e["kind"] == "checkpoint"}
    apart: Dict[str, int] = {}
    for e in events:
        if e["kind"] == "rollout" and e.get("outcome") not in COUNTED + (
            "not_applicable",
        ):
            apart[str(e["outcome"])] = apart.get(str(e["outcome"]), 0) + 1
    return {
        "outcomes_apart": apart,
        "integrity": {
            "checked": len(integ),
            "ok": sum(1 for e in integ if e["ok"]),
            "groups": len(groups),
            "groups_unchecked": len(groups) - len(integ),
            "failed_groups": sorted(bad),
            "rollouts_in_failed_groups": sum(
                1
                for e in events
                if e["kind"] == "rollout"
                and e.get("outcome") in COUNTED
                and _key(e) in bad
            ),
            "reads": _stats(
                [float(e["wall_s"]) for e in events if e["kind"] == "integrity"]
            ),
        },
        "advances": {
            "n": len(adv),
            "census_match": sum(
                1 for e in adv if bool(e.get("proved")) == bool(e.get("census_proved"))
            ),
            "retried": sum(1 for e in adv if e.get("retried")),
        },
    }


def summarize_t6(
    events: Sequence[Dict[str, Any]],
    files: Sequence[Dict[str, Any]],
    body: Dict[str, Any],
) -> Dict[str, Any]:
    """The T6 numbers of one cell (module docstring)."""
    window_s = float(body.get("window_end") or 0) - float(body.get("window_start") or 0)
    roll = _rollouts(events)
    verifies = [
        e
        for e in events
        if e["kind"] in ("rollout", "advance") and e.get("execution_time")
    ]
    restores = [e for e in events if e["kind"] == "restore"]
    return {
        "window_s": _r(window_s),
        **_rates(events, files, window_s),
        "shares": _shares(events, int(body.get("workers") or 1), window_s),
        "by_candidate": _by_candidate(events),
        **_counts(events),
        "restores_per_rollout": _r(len(restores) / len(roll), 3) if roll else None,
        "checkpoint": _stats(
            [float(e["wall_s"]) for e in events if e["kind"] == "checkpoint"]
        ),
        "restore": _stats([float(e["wall_s"]) for e in restores]),
        "queueing": _stats(
            [float(e["wall_s"]) - float(e["execution_time"]) for e in verifies]
        ),
        "files": len(files),
        "stopped": [
            (f["stem"], f["worker"], f["stopped"]) for f in files if f.get("stopped")
        ],
        "acquire_errors": sum(1 for f in files if f.get("acquire_error")),
        "matched": _matched(events, body.get("matched_stems") or []),
    }
