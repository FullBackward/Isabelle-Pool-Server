"""T4 numbers from part A (files as written) and part B (block x variant)
records (design §T4; design review S7; review ab492af S1). Verdicts:
in-session *proved* := success, no open proof, no sorry, not timed out;
cold *accepted* := the build succeeded. Memory / prover-death outcomes and
cold builds that did not answer are counted apart, outside the matrix.

Per variant the confusion matrix: both accept, in-session proved but cold
rejected (the safety finding, target 0), in-session rejected but cold
accepted, both reject. The unchanged variant is the baseline: a mutant is
judged only where its block's file as written proves in-session and builds
cold (else ``baseline_failed``). For a mutant, "both accept" means the
mutation did not break the proof (listed as ineffective, not counted in the
rejection rates)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

APART = ("memory", "prover_died", "session_gone", "error")


def _in(r: Dict[str, Any]) -> Optional[bool]:
    s = r.get("in_session") or {}
    if not s or s.get("outcome") in APART:
        return None
    return bool(s.get("proved"))


def _cold(r: Dict[str, Any]) -> Optional[bool]:
    c = r.get("cold") or {}
    return None if c.get("accepted") is None else bool(c["accepted"])


def _part_a(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    judged = [r for r in rows if _cold(r) is not None]
    disagree = [
        {
            "stem": r["stem"],
            "in_session_proved": r["in_session_proved"],
            "cold_accepted": _cold(r),
            "first_unproved": r.get("first_unproved"),
            "cold_error": ((r.get("cold") or {}).get("error") or "")[-200:],
        }
        for r in judged
        if bool(r["in_session_proved"]) != _cold(r)
    ]
    return {
        "files": len(rows),
        "judged": len(judged),
        "cold_unanswered": len(rows) - len(judged),
        "agree": len(judged) - len(disagree),
        "both_proved": sum(1 for r in judged if r["in_session_proved"] and _cold(r)),
        "in_session_only": sum(1 for d in disagree if d["in_session_proved"]),
        "cold_only": sum(1 for d in disagree if not d["in_session_proved"]),
        "disagreements": disagree,
    }


def _variant(
    rows: List[Dict[str, Any]], baseline: Optional[Dict[Any, bool]] = None
) -> Dict[str, Any]:
    """The matrix for one variant. With ``baseline`` (a mutant) a block is
    judged only when its unchanged file proves in-session AND builds cold:
    otherwise the cold rejection of the mutant says nothing about the
    mutation (seen locally: CTL fails cold as written, for its citations);
    such blocks are counted apart."""
    applicable = [r for r in rows if r["applicable"]]
    judged = [r for r in applicable if _in(r) is not None and _cold(r) is not None]
    if baseline is not None:
        judged = [r for r in judged if baseline.get((r["stem"], r["index"]))]
    cell = {
        "both_accept": [r for r in judged if _in(r) and _cold(r)],
        "in_session_only": [r for r in judged if _in(r) and not _cold(r)],
        "cold_only": [r for r in judged if not _in(r) and _cold(r)],
        "both_reject": [r for r in judged if not _in(r) and not _cold(r)],
    }
    out: Dict[str, Any] = {k: len(v) for k, v in cell.items()}
    out.update(
        {
            "blocks": len(rows),
            "not_applicable": len(rows) - len(applicable),
            "apart": len(applicable) - len(judged),
        }
    )
    if baseline is None:
        return out
    effective = len(judged) - len(cell["both_accept"])
    out.update(
        {
            "baseline_failed": sum(
                1 for r in applicable if not baseline.get((r["stem"], r["index"]))
            ),
            "safety_findings": [
                (r["stem"], r["index"]) for r in cell["in_session_only"]
            ],
            "ineffective": [(r["stem"], r["index"]) for r in cell["both_accept"]],
            "in_session_reject_rate": (
                round(
                    (len(cell["cold_only"]) + len(cell["both_reject"])) / effective, 4
                )
                if effective
                else None
            ),
            "cold_reject_rate": (
                round(
                    (len(cell["in_session_only"]) + len(cell["both_reject"]))
                    / effective,
                    4,
                )
                if effective
                else None
            ),
        }
    )
    return out


def summarize_t4(
    part_a: Sequence[Dict[str, Any]], part_b: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    """Part A's agreement and part B's matrix per variant."""
    unchanged = [r for r in part_b if r["variant"] == "unchanged"]
    baseline = {(r["stem"], r["index"]): bool(_in(r) and _cold(r)) for r in unchanged}
    variants: Dict[str, Any] = {"unchanged": _variant(unchanged)}
    for v in ("sorry", "truncated", "wrong_fact"):
        variants[v] = _variant([r for r in part_b if r["variant"] == v], baseline)
    return {"part_a": _part_a(part_a), "part_b": variants}
