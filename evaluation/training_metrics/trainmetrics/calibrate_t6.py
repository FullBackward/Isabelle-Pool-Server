"""The T6 cell timeout (design S8, review 5422bf0 S1). The smoke's T6a
calibration cell (W = 1, one file) gives the cost of the loop; the census and
the assignments give each worker's work in every cell of the T6 matrix, so
the timeout is set for the slowest worker the matrix will meet, not for the
small smoke file.

A worker's estimate is, over its files: the calibration file's setup
(acquire + enter), the census time of the blocks up to the file's last
problem (the prefix and the advances), and per problem the corpus candidate
(the problem block's census time) plus the 7 other candidates at their
calibrated mean wall (timeouts included) and the restores. The suggested
``t6.cell_timeout_s`` is the largest worker's estimate x ``contention``
(T1's slowdown at the largest W once measured, else 3), and never below
7200 s.

    python -m trainmetrics.calibrate_t6 --cell results/smoke/t6a/w1/rep0                    # the loop's cost
    python -m trainmetrics.calibrate_t6 --cell results/smoke/t6a/w1/rep0 \\
        --census results/census/census.json [--contention 3]                              # + the timeout
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import EXPERIMENT_DIR
from .census import load_census, load_config
from .corpus import CorpusFile, assignment, load_corpus, load_paths
from .metrics_t5 import load_samples
from .metrics_t6 import summarize_t6
from .run_t1 import usable_stems
from .t6_core import COUNTED, problems_of

FLOOR_S = 7200.0


def calibration(cell_dir: Path) -> Dict[str, Any]:
    """The loop's cost from a W = 1 cell: the timeout share, the per-rollout
    loop time, the non-corpus candidates' mean wall, restore p50, the setup
    (acquire + enter) of a file, and the file's wall time."""
    manifest = json.loads((cell_dir / "manifest.json").read_text(encoding="utf-8"))
    events = load_samples(cell_dir / "events.jsonl")
    files = load_samples(cell_dir / "files.jsonl")
    s = summarize_t6(events, files, manifest)
    roll = s["rollouts"] or 0
    others = [
        float(e["wall_s"])
        for e in events
        if e["kind"] == "rollout"
        and e.get("candidate") != "corpus"
        and e.get("outcome") in COUNTED
    ]
    setup = [float(e["wall_s"]) for e in events if e["kind"] in ("acquire", "enter")]
    timed = sum(
        (c["outcomes"].get("timed_out") or 0) for c in s["by_candidate"].values()
    )
    lt = (s["workers"].get(0) or s["workers"].get("0") or {}).get("loop_s")
    walls = [float(f["t_end"]) - float(f["t_start"]) for f in files]
    return {
        "cell": str(cell_dir),
        "rollouts": roll,
        "timeout_share": round(timed / roll, 4) if roll else None,
        "per_rollout_loop_s": round(float(lt) / roll, 4) if lt and roll else None,
        "other_candidate_mean_s": (
            round(sum(others) / len(others), 4) if others else None
        ),
        "restore_p50_s": s["restore"]["p50_s"],
        "setup_s": round(sum(setup) / max(1, len(files)), 3),
        "file_wall_s": round(max(walls), 1) if walls else None,
    }


def worker_estimate_s(
    stems: Sequence[str],
    by_stem: Dict[str, CorpusFile],
    rows: Dict[str, Dict[str, Any]],
    cal: Dict[str, Any],
    n_problems: int,
) -> float:
    """One worker's estimated time over its files (module docstring)."""
    other = float(cal.get("other_candidate_mean_s") or 0)
    restore = float(cal.get("restore_p50_s") or 0)
    total = 0.0
    for stem in stems:
        row = rows.get(stem) or {}
        blocks = row.get("blocks") or []
        probs = problems_of(by_stem[stem], row, n_problems)
        if not probs:
            continue
        wall = [float(b.get("wall_s") or 0) for b in blocks]
        total += float(cal.get("setup_s") or 0) + sum(wall[: max(probs) + 1])
        total += sum(wall[i] + 7 * other + 3 * restore for i in probs)
    return total


def estimate(  # pylint: disable=too-many-locals,too-many-arguments
    cal: Dict[str, Any],
    census: Dict[str, Any],
    files: Sequence[CorpusFile],
    t6: Dict[str, Any],
    *,
    seed: int,
    contention: float,
) -> Dict[str, Any]:
    """The largest worker over the matrix's cells and the suggested timeout."""
    by_stem = {cf.stem: cf for cf in files}
    rows = {f["stem"]: f for f in census.get("files", [])}
    stems = usable_stems(files, census)
    quota = int(t6["files_per_worker"])
    n_problems = int(t6["t6a"]["problems_per_file"])
    worst: Dict[str, Any] = {"estimate_s": 0.0}
    for w in sorted(int(x) for x in t6["workers"]):
        for rep in range(int(t6.get("repeats", 1))):
            plan = assignment(stems, seed + rep, w, quota)
            for k, worker_stems in enumerate(plan):
                est = worker_estimate_s(worker_stems, by_stem, rows, cal, n_problems)
                if est > worst["estimate_s"]:
                    worst = {
                        "estimate_s": est,
                        "workers": w,
                        "rep": rep,
                        "worker": k,
                        "stems": list(worker_stems),
                    }
    suggested = max(FLOOR_S, math.ceil(worst["estimate_s"] * contention))
    return {
        "largest_worker": {**worst, "estimate_s": round(worst["estimate_s"], 1)},
        "contention": contention,
        "suggested_cell_timeout_s": int(suggested),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI (module docstring)."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", default=str(EXPERIMENT_DIR / "config.yaml"))
    ap.add_argument(
        "--cell", required=True, help="the smoke's W = 1 single-file T6a cell"
    )
    ap.add_argument("--census", default="", help="the full census: adds the timeout")
    ap.add_argument("--local-smoke", action="store_true")
    ap.add_argument("--contention", type=float, default=3.0)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    cal = calibration(Path(args.cell))
    for k, v in cal.items():
        print(f"{k}: {v}")
    if not args.census:
        print(
            "after the census: python -m trainmetrics.calibrate_t6 --cell "
            f"{args.cell} --census results/census/census.json -> t6.cell_timeout_s"
        )
        return 0 if cal["rollouts"] else 1
    files: List[CorpusFile] = (
        load_paths(cfg["local_smoke"]["files"])
        if args.local_smoke
        else load_corpus(cfg["corpus"])
    )
    est = estimate(
        cal,
        load_census(args.census),
        files,
        cfg["t6"],
        seed=int(cfg["seed"]),
        contention=args.contention,
    )
    for k, v in est.items():
        print(f"{k}: {v}")
    print(
        f"set t6.cell_timeout_s: {est['suggested_cell_timeout_s']} in config.yaml before scripts/14_run_t6.sh"
    )
    return 0 if cal["rollouts"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
