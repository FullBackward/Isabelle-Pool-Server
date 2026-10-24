"""The T6 cell timeouts (design S8, reviews 5422bf0 S1 and ec85e74). The
smoke's calibration cells (W = 1, one file: T6a, and T6b when it found a
problem) give the cost of the loop; the census and the assignments give each
worker's work in every cell of the T6 matrix, so each style's timeout is set
for the slowest worker the matrix will meet, not for the small smoke file.

A worker's estimate is, over its files: the calibration file's setup
(acquire + enter) and the census time of the blocks up to the file's last
problem (the prefix and the advances), then per problem

- T6a: the corpus candidate (the block's census time) plus the 7 other
  candidates at their calibrated mean wall (timeouts included) and 3
  restores;
- T6b: per proof command (from the census spans) a checkpoint, the 2
  alternatives at their calibrated mean wall (T6a's other candidates as the
  proxy without a T6b cell) and 2 restores, plus one status poll (0.11 s,
  ``verify_chunk``'s step) per piece, since the path verifies the block piece
  by piece.

The suggested ``cell_timeout_s`` of a style is its largest worker x
``contention`` (T1's slowdown at the largest W once measured, else 3), plus
``server_start_s`` (the cell starts a fresh server: 1-2 min for the gateway
JVM), and never below 7200 s.

    python -m trainmetrics.calibrate_t6 --cell results/smoke/t6a/w1/rep0 [--cell-t6b results/smoke/t6b/w1/rep0]
    python -m trainmetrics.calibrate_t6 --cell ... --census results/census/census.json [--contention 3]
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import EXPERIMENT_DIR
from .census import load_census, load_config
from .corpus import CorpusFile, assignment, load_corpus, load_paths
from .metrics_t5 import load_samples
from .metrics_t6 import summarize_t6
from .run_t1 import usable_stems
from .t6_core import COUNTED, problems_of
from .t6b import problems_t6b, steps_of

FLOOR_S = 7200.0
SERVER_START_S = 180.0
POLL_S = 0.11


def _mean(xs: Sequence[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 4) if xs else None


def calibration(cell_dir: Path) -> Dict[str, Any]:
    """The loop's cost from a W = 1 cell: the timeout share, the per-rollout
    loop time, the non-corpus T6a candidates' and the T6b alternatives' mean
    wall, checkpoint and restore p50, a file's setup (acquire + enter), and
    the file's wall time."""
    manifest = json.loads((cell_dir / "manifest.json").read_text(encoding="utf-8"))
    events = load_samples(cell_dir / "events.jsonl")
    files = load_samples(cell_dir / "files.jsonl")
    s = summarize_t6(events, files, manifest)
    roll = s["rollouts"] or 0

    def walls(kind: str, skip: str = "") -> List[float]:
        return [
            float(e["wall_s"])
            for e in events
            if e["kind"] == kind
            and e.get("outcome") in COUNTED
            and (not skip or e.get("candidate") != skip)
        ]

    setup = [float(e["wall_s"]) for e in events if e["kind"] in ("acquire", "enter")]
    tried = [c["outcomes"] for c in s["by_candidate"].values()]
    n_tried = sum(sum(o.values()) for o in tried)
    timed = sum(o.get("timed_out") or 0 for o in tried)
    lt = (s["workers"].get(0) or s["workers"].get("0") or {}).get("loop_s")
    fw = [float(f["t_end"]) - float(f["t_start"]) for f in files]
    return {
        "cell": str(cell_dir),
        "style": manifest.get("experiment"),
        "rollouts": roll,
        # of the T6a candidates, or of the T6b alternatives
        "timeout_share": round(timed / n_tried, 4) if n_tried else None,
        "per_rollout_loop_s": round(float(lt) / roll, 4) if lt and roll else None,
        "other_candidate_mean_s": _mean(walls("rollout", skip="corpus")),
        "alternative_mean_s": _mean(walls("alternative")),
        "checkpoint_p50_s": s["checkpoint"]["p50_s"],
        "restore_p50_s": s["restore"]["p50_s"],
        "setup_s": round(sum(setup) / max(1, len(files)), 3),
        "file_wall_s": round(max(fw), 1) if fw else None,
    }


Cost = Callable[[CorpusFile, Dict[str, Any], List[float]], float]
Problems = Callable[[CorpusFile, Dict[str, Any]], List[int]]


def cost_t6a(cal: Dict[str, Any], n: int) -> Cost:
    """Per file: the groups' cost beyond the census replay (module docstring)."""
    other = float(cal.get("other_candidate_mean_s") or 0)
    restore = float(cal.get("restore_p50_s") or 0)

    def cost(cf: CorpusFile, row: Dict[str, Any], wall: List[float]) -> float:
        probs = problems_of(cf, row, n)
        return sum(wall[i] + 7 * other + 3 * restore for i in probs)

    return cost


def cost_t6b(cal: Dict[str, Any], t6b: Dict[str, Any]) -> Cost:
    """Per file: the paths' cost beyond the census replay (module docstring)."""
    alt = float(cal.get("alternative_mean_s") or cal.get("other_candidate_mean_s") or 0)
    cp = float(cal.get("checkpoint_p50_s") or 0)
    restore = float(cal.get("restore_p50_s") or 0)
    per = int(t6b.get("alternatives_per_step", 2))
    n, lo, hi = (
        int(t6b["problems_per_file"]),
        int(t6b.get("min_steps", 2)),
        int(t6b.get("max_steps", 40)),
    )

    def cost(cf: CorpusFile, row: Dict[str, Any], _wall: List[float]) -> float:
        total = 0.0
        for i in problems_t6b(cf, row, n, lo, hi):
            split = steps_of(cf.blocks[i], (row.get("blocks") or [])[i].get("spans"))
            k = len(split[1]) if split else 0
            total += (k + 1) * POLL_S + k * (cp + per * alt + per * restore)
        return total

    return cost


def style_model(
    style: str, st: Dict[str, Any], cal: Dict[str, Any]
) -> Tuple[Problems, Cost]:
    """The style's problem selector and per-file cost."""
    n = int(st["problems_per_file"])
    if style == "t6a":

        def probs_a(cf: CorpusFile, row: Dict[str, Any]) -> List[int]:
            return problems_of(cf, row, n)

        return probs_a, cost_t6a(cal, n)
    lo, hi = int(st.get("min_steps", 2)), int(st.get("max_steps", 40))

    def probs_b(cf: CorpusFile, row: Dict[str, Any]) -> List[int]:
        return problems_t6b(cf, row, n, lo, hi)

    return probs_b, cost_t6b(cal, st)


def worker_estimate_s(  # pylint: disable=too-many-arguments
    stems: Sequence[str],
    by_stem: Dict[str, CorpusFile],
    rows: Dict[str, Dict[str, Any]],
    *,
    setup_s: float,
    style: Problems,
    cost: Cost,
) -> float:
    """One worker's estimated time over its files (module docstring): setup,
    the census time up to the last problem, and the style's ``cost``."""
    total = 0.0
    for stem in stems:
        cf, row = by_stem[stem], rows.get(stem) or {}
        probs = style(cf, row)
        if not probs:
            continue
        wall = [float(b.get("wall_s") or 0) for b in row.get("blocks") or []]
        total += setup_s + sum(wall[: max(probs) + 1]) + cost(cf, row, wall)
    return total


def estimate(  # pylint: disable=too-many-locals,too-many-arguments
    cals: Dict[str, Dict[str, Any]],
    census: Dict[str, Any],
    files: Sequence[CorpusFile],
    t6: Dict[str, Any],
    *,
    seed: int,
    contention: float,
) -> Dict[str, Any]:
    """Per style: the largest worker over the matrix's cells and the suggested
    timeout. ``cals``: the calibrations by style (T6b falls back to T6a's)."""
    by_stem = {cf.stem: cf for cf in files}
    rows = {f["stem"]: f for f in census.get("files", [])}
    stems = usable_stems(files, census)
    quota = int(t6["files_per_worker"])
    out: Dict[str, Any] = {}
    for style in ("t6a", "t6b"):
        if style not in t6:
            continue
        cal = cals.get(style) or cals["t6a"]
        probs, cost = style_model(style, t6[style], cal)
        worst: Dict[str, Any] = {"estimate_s": 0.0}
        for w in sorted(int(x) for x in t6["workers"]):
            for rep in range(int(t6.get("repeats", 1))):
                for k, ws in enumerate(assignment(stems, seed + rep, w, quota)):
                    e = worker_estimate_s(
                        ws,
                        by_stem,
                        rows,
                        setup_s=float(cal.get("setup_s") or 0),
                        style=probs,
                        cost=cost,
                    )
                    if e > worst["estimate_s"]:
                        worst = {
                            "estimate_s": e,
                            "workers": w,
                            "rep": rep,
                            "worker": k,
                            "stems": list(ws),
                        }
        out[style] = {
            "calibration": cal.get("cell"),
            "largest_worker": {**worst, "estimate_s": round(worst["estimate_s"], 1)},
            "suggested_cell_timeout_s": int(
                max(
                    FLOOR_S,
                    math.ceil(worst["estimate_s"] * contention + SERVER_START_S),
                )
            ),
        }
    return {"contention": contention, "server_start_s": SERVER_START_S, **out}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI (module docstring)."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", default=str(EXPERIMENT_DIR / "config.yaml"))
    ap.add_argument(
        "--cell", required=True, help="the smoke's W = 1 single-file T6a cell"
    )
    ap.add_argument(
        "--cell-t6b", default="", help="the smoke's W = 1 single-file T6b cell"
    )
    ap.add_argument("--census", default="", help="the full census: adds the timeouts")
    ap.add_argument("--local-smoke", action="store_true")
    ap.add_argument("--contention", type=float, default=3.0)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    cals = {"t6a": calibration(Path(args.cell))}
    if args.cell_t6b and (Path(args.cell_t6b) / "manifest.json").exists():
        cal_b = calibration(Path(args.cell_t6b))
        if cal_b["rollouts"]:  # a smoke file without a T6b path: T6a's figures stand in
            cals["t6b"] = cal_b
    for style, cal in cals.items():
        print(f"{style} calibration: " + ", ".join(f"{k} {v}" for k, v in cal.items()))
    if not args.census:
        print(
            "after the census: python -m trainmetrics.calibrate_t6 --cell "
            f"{args.cell} --cell-t6b {args.cell_t6b or '<t6b cell>'} "
            "--census results/census/census.json -> the cell timeouts"
        )
        return 0 if cals["t6a"]["rollouts"] else 1
    files: List[CorpusFile] = (
        load_paths(cfg["local_smoke"]["files"])
        if args.local_smoke
        else load_corpus(cfg["corpus"])
    )
    est = estimate(
        cals,
        load_census(args.census),
        files,
        cfg["t6"],
        seed=int(cfg["seed"]),
        contention=args.contention,
    )
    print(json.dumps(est, indent=1))
    for style in ("t6a", "t6b"):
        if style in est:
            print(
                f"set t6.{style}.cell_timeout_s: {est[style]['suggested_cell_timeout_s']} in config.yaml"
            )
    return 0 if cals["t6a"]["rollouts"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
