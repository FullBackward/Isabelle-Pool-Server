"""T3 summary (plan stage 5): the branch operations' p50 / p95, the two
alternatives per position stratum against the branch cost, the restore
against the distance it goes back, and the integrity checks.

- (e) = ``enter_s + replay_s`` of the new node on the same session;
  (f) = ``create_s`` + the same of the created session's first node.
- *Extra* time of a verify: the continuation verified after the restore
  (``again``) or after the rollback (``after_rollback``) minus the first verify
  of the same block (``cont``), per state. A restore returns without waiting
  for the prover, so work it leaves would show here; the rollback, which waits
  for the node to settle, is the control.
- The branch cost of a state is its restore plus the extra time after it
  (not below zero); the ratios are per state ((e) / branch, (f) / branch),
  then the median.
- A state is *complete* when (a)-(f) all ran; stopped states (a session that
  died) and errors are counted and listed apart."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .metrics_t12 import CLASSES, p50, p95, size_class

STRATA = ("early", "middle", "late")


def _r(x: Optional[float], nd: int = 4) -> Optional[float]:
    return None if x is None else round(float(x), nd)


def _stats(xs: Sequence[float]) -> Dict[str, Any]:
    return {"n": len(xs), "p50_s": _r(p50(xs)), "p95_s": _r(p95(xs))}


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


def complete(s: Dict[str, Any]) -> bool:
    """(a)-(f) all ran on this state."""
    return (
        not s.get("stopped")
        and not s.get("error")
        and s.get("restore_s") is not None
        and s.get("rollback_s") is not None
        and warm_s(s) is not None
        and fresh_s(s) is not None
    )


def _ratio(num: Optional[float], den: Optional[float]) -> Optional[float]:
    return None if num is None or not den else num / den


def extra_s(s: Dict[str, Any], key: str) -> Optional[float]:
    """The verify ``key`` (again | after_rollback) minus the first one."""
    if not (s.get(key) or {}).get("success") or not (s.get("cont") or {}):
        return None
    return float(s[key]["wall_s"]) - float(s["cont"]["wall_s"])


def branch_s(s: Dict[str, Any]) -> Optional[float]:
    """The restore and the extra time it leaves to the next verify."""
    if s.get("restore_s") is None:
        return None
    return float(s["restore_s"]) + max(0.0, extra_s(s, "again") or 0.0)


def _by_stratum(done: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for st in STRATA:
        ss = [s for s in done if s["stratum"] == st]
        if not ss:
            continue
        w = [x for x in (warm_s(s) for s in ss) if x is not None]
        f = [x for x in (fresh_s(s) for s in ss) if x is not None]
        rw = [x for x in (_ratio(warm_s(s), branch_s(s)) for s in ss) if x is not None]
        rf = [x for x in (_ratio(fresh_s(s), branch_s(s)) for s in ss) if x is not None]
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
                "branch_p50_s": _r(
                    p50([x for x in (branch_s(s) for s in ss) if x is not None])
                ),
                "warm_over_branch_p50": _r(p50(rw), 1),
                "fresh_over_branch_p50": _r(p50(rf), 1),
            }
        )
    return out


def _by_distance(done: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for cls, _lo, _hi in CLASSES:
        xs = [
            float(s["restore_s"]) for s in done if size_class(s.get("distance")) == cls
        ]
        if xs:
            out.append({"distance": cls, **_stats(xs)})
    return out


def summarize_t3(states: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The T3 numbers from the cell's state records."""
    done = [s for s in states if complete(s)]
    ran = [s for s in states if s.get("restore_s") is not None]

    def col(key: str) -> List[float]:
        return [float(s[key]) for s in done if s.get(key) is not None]

    def extras(key: str) -> List[float]:
        return [x for x in (extra_s(s, key) for s in done) if x is not None]

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
            "save": _stats(col("save_s")),
            "restore": _stats(col("restore_s")),
            "rollback": _stats(col("rollback_s")),
            "create": _stats(col("create_s")),
            "continuation": _stats(
                [float(s["cont"]["wall_s"]) for s in done if s.get("cont")]
            ),
            "extra_after_restore": _stats(extras("again")),
            "extra_after_rollback": _stats(extras("after_rollback")),
            "branch": _stats([x for x in (branch_s(s) for s in done) if x is not None]),
        },
        "by_stratum": _by_stratum(done),
        "restore_by_distance": _by_distance(done),
        "integrity": {
            "restored": sum(1 for s in ran if s.get("restore_ok")),
            "source_after_restore": sum(
                1 for s in ran if s.get("source_after_restore")
            ),
            "identical": sum(1 for s in ran if s.get("identical")),
            "rolled_back": sum(1 for s in ran if s.get("rollback_ok")),
            "source_after_rollback": sum(
                1 for s in ran if s.get("source_after_rollback")
            ),
            "of": len(ran),
        },
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
