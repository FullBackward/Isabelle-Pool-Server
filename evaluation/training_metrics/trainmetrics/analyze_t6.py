"""T6 tables, the rate plot and the T6 part of REPORT_SNIPPET.md (plan stage
6b), plus the calibration the smoke prints (design S8). Every number is
recomputed from a cell's raw records (``events.jsonl``, ``files.jsonl``,
``tiers.jsonl``, ``samples.jsonl``) with the current
:mod:`trainmetrics.metrics_t6`.

Across cells, per W (repeats averaged): both rates per hour, their speedup
against W = 1, the per-rollout loop time on the matched files (the W = 1
cell's, present in every cell, S3) and its slowdown, the setup share
(acquire, enter, prefix, close), the timeout share of the rollouts, queueing
p95, stopped files, outcomes apart, failed integrity checks and the worst
memory tier.

    python -m trainmetrics.analyze_t6 --calibrate results/smoke/t6a/w1/rep0
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import EXPERIMENT_DIR
from .census import load_config
from .contention import tier_summary
from .csvout import ok_cells, write_csv
from .metrics_t5 import load_samples
from .metrics_t6 import summarize_t6
from .samples import window_summary

SETUP = ("acquire", "enter", "prefix", "close")
TIER_ORDER = ("ok", "warning", "critical")


def load_t6_cells(results: Path, style: str) -> List[Dict[str, Any]]:
    """Every finished (ok) cell of ``style`` with its summary recomputed."""
    out: List[Dict[str, Any]] = []
    for d, m in ok_cells(results, f"{style}/w*/rep*/manifest.json"):
        events, files = load_samples(d / "events.jsonl"), load_samples(
            d / "files.jsonl"
        )
        s = summarize_t6(events, files, m)
        s["tiers"] = tier_summary(load_samples(d / "tiers.jsonl"))
        s["host"] = window_summary(
            load_samples(d / "samples.jsonl"),
            float(m.get("window_start") or 0),
            float(m.get("window_end") or 0),
        )
        out.append({"manifest": m, "summary": s, "events": events, "files": files})
    return out


def _worst_tier(tiers: Dict[str, Any]) -> Optional[str]:
    seen = [t for t in TIER_ORDER if (tiers.get("states") or {}).get(t)]
    return seen[-1] if seen else None


def cell_row(cell: Dict[str, Any]) -> Dict[str, Any]:
    """One cell, flat (t6_cells.csv)."""
    m, s = cell["manifest"], cell["summary"]
    sh = s["shares"]
    roll = s["rollouts"] or 0
    timed = sum(
        (c["outcomes"].get("timed_out") or 0) for c in s["by_candidate"].values()
    )
    return {
        "style": m.get("experiment"),
        "workers": m.get("workers"),
        "rep": m.get("rep"),
        "window_s": s["window_s"],
        "rollouts": roll,
        "proved_rollouts": s["proved_rollouts"],
        "problems": s["problems"],
        "rollouts_per_h": s["rollouts_per_h"],
        "loop_rollouts_per_h": s["loop_rollouts_per_h"],
        "proved_per_h": s["proved_per_h"],
        "problems_per_h": s["problems_per_h"],
        "setup_share": round(sum(float(sh.get(k) or 0) for k in SETUP), 4),
        "timeout_share": round(timed / roll, 4) if roll else None,
        "restores_per_rollout": s["restores_per_rollout"],
        "checkpoint_p50_s": s["checkpoint"]["p50_s"],
        "restore_p50_s": s["restore"]["p50_s"],
        "queue_p50_s": s["queueing"]["p50_s"],
        "queue_p95_s": s["queueing"]["p95_s"],
        "matched_per_rollout_s": s["matched"]["per_rollout_s"],
        "stopped": len(s["stopped"]),
        "outcomes_apart": sum(s["outcomes_apart"].values()),
        "integrity_failed": len(s["integrity"]["failed_groups"]),
        "worst_tier": _worst_tier(s["tiers"]),
        "host_cpu_util": s["host"].get("host_cpu_util"),
        "pss_peak_mb": s["host"].get("pss_peak_mb"),
        "executor_threads": m.get("server_executor_threads"),
    }


def _mean(xs: Sequence[Any]) -> Optional[float]:
    vals = [float(x) for x in xs if x is not None]
    return statistics.fmean(vals) if vals else None


def series(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per W, repeats averaged, with speedup and slowdown against W = 1."""
    out: List[Dict[str, Any]] = []
    for w in sorted({int(r["workers"]) for r in rows}):
        rs = [r for r in rows if int(r["workers"]) == w]
        out.append(
            {
                "workers": w,
                "reps": len(rs),
                **{
                    k: _mean([r[k] for r in rs])
                    for k in (
                        "rollouts_per_h",
                        "loop_rollouts_per_h",
                        "proved_per_h",
                        "problems_per_h",
                        "matched_per_rollout_s",
                        "setup_share",
                        "timeout_share",
                    )
                },
                "queue_p95_s": max((r["queue_p95_s"] or 0) for r in rs),
                "stopped": sum(r["stopped"] for r in rs),
                "outcomes_apart": sum(r["outcomes_apart"] for r in rs),
                "integrity_failed": sum(r["integrity_failed"] for r in rs),
                "worst_tier": max(
                    (r["worst_tier"] for r in rs if r["worst_tier"]),
                    key=TIER_ORDER.index,
                    default=None,
                ),
            }
        )
    base = next((s for s in out if s["workers"] == 1), None)
    for s in out:
        for key, ref, name in (
            ("loop_rollouts_per_h", base, "loop_speedup"),
            ("rollouts_per_h", base, "e2e_speedup"),
        ):
            den = (ref or {}).get(key)
            s[name] = round(s[key] / den, 2) if den and s[key] is not None else None
        den = (base or {}).get("matched_per_rollout_s")
        num = s["matched_per_rollout_s"]
        s["matched_slowdown"] = round(num / den, 2) if den and num is not None else None
    return out


def candidates(cells: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per W and candidate kind, pooled over repeats: outcomes and wall p50."""
    rows: List[Dict[str, Any]] = []
    for cell in cells:
        m = cell["manifest"]
        for kind, c in cell["summary"]["by_candidate"].items():
            rows.append(
                {
                    "workers": m.get("workers"),
                    "rep": m.get("rep"),
                    "candidate": kind,
                    **{f"n_{k}": v for k, v in sorted(c["outcomes"].items())},
                    "wall_p50_s": c["wall"]["p50_s"],
                    "timeout_share": c["timeout_share"],
                }
            )
    return rows


def _f(v: Any, nd: int = 0) -> str:
    return "-" if v is None else f"{float(v):,.{nd}f}"


def snippet_t6(
    style: str, cells: Sequence[Dict[str, Any]], ser: List[Dict[str, Any]]
) -> str:
    """The paragraph of one style."""
    t6 = cells[0]["manifest"].get("t6") or {}
    head = {
        "t6a": "T6a whole-proof rollouts",
    }.get(style, style)
    lines = [
        f"## {head} (generated by trainmetrics.analyze)",
        "",
        f"{len(cells)} cells. Each worker takes {t6.get('files_per_worker')} file(s) of the T1 assignment, "
        f"each on its own session; each of a file's first {(t6.get(style) or {}).get('problems_per_file')} "
        "census-proved theorems is a problem. At a problem: a checkpoint and the 8 D9 candidates in a "
        "seeded order (the corpus proof, `by auto` and `by simp` twice each, `sorry`, the proof truncated, "
        "a method that fails at once), a kept one undone by a restore. A rollout is one candidate; no "
        "model runs, so the rates are the server's share of a training loop. End-to-end rates include "
        "acquisition, entering and replaying up to each problem; the "
        "loop rate counts each worker's loop calls only.",
        "",
        "| W | reps | rollouts/h end to end | loop rollouts/h | loop speedup | proved/h | problems/h "
        "| per-rollout time on the W = 1 files (s) | slowdown | setup share | timeouts | queue p95 (s) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in ser:
        lines.append(
            f"| {s['workers']} | {s['reps']} | {_f(s['rollouts_per_h'])} | {_f(s['loop_rollouts_per_h'])} | "
            f"{_f(s['loop_speedup'], 2)} | {_f(s['proved_per_h'])} | {_f(s['problems_per_h'])} | "
            f"{_f(s['matched_per_rollout_s'], 3)} | {_f(s['matched_slowdown'], 2)} | "
            f"{_f(s['setup_share'], 2)} | {_f(s['timeout_share'], 3)} | {_f(s['queue_p95_s'], 3)} |"
        )
    stopped = sum(s["stopped"] for s in ser)
    apart = sum(s["outcomes_apart"] for s in ser)
    failed = sum(s["integrity_failed"] for s in ser)
    tiers = ", ".join(f"W = {s['workers']}: {s['worst_tier'] or '-'}" for s in ser)
    lines += [
        "",
        f"Stopped files: {stopped}; outcomes outside the rollouts (memory, prover death, gone session, "
        f"error): {apart}; failed integrity checks: {failed}. Worst memory tier per W: {tiers}. "
        "The proved share is fixed by the mix (1 to 3 of 8 candidates can prove).",
    ]
    return "\n".join(lines) + "\n"


def plot_t6(style: str, ser: List[Dict[str, Any]], out: Path) -> Optional[str]:
    """Both rates against W; skipped without matplotlib."""
    try:
        import matplotlib  # pylint: disable=import-outside-toplevel

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # pylint: disable=import-outside-toplevel
    except ImportError:
        return None
    if not ser:
        return None
    fig, ax = plt.subplots(figsize=(6, 4))
    ws = [s["workers"] for s in ser]
    ax.plot(ws, [s["loop_rollouts_per_h"] for s in ser], marker="o", label="loop rate")
    ax.plot(ws, [s["rollouts_per_h"] for s in ser], marker="s", label="end to end")
    ax.set_xscale("log", base=2)
    ax.set_xticks(ws)
    ax.set_xticklabels([str(w) for w in ws])
    ax.set_xlabel("workers W")
    ax.set_ylabel("rollouts / h")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{style}_rollouts.png"
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return str(path)


def write_t6(results: Path, tables: Path) -> Tuple[str, int, int]:
    """The T6 tables, plots and snippet text per style ("" without a cell);
    (text, cells, plots)."""
    texts: List[str] = []
    n_cells = n_plots = 0
    for style in ("t6a",):
        cells = load_t6_cells(results, style)
        if not cells:
            continue
        rows = [cell_row(c) for c in cells]
        ser = series(rows)
        write_csv(tables / f"{style}_cells.csv", rows)
        write_csv(tables / f"{style}_series.csv", ser)
        write_csv(tables / f"{style}_candidates.csv", candidates(cells))
        texts.append(snippet_t6(style, cells, ser))
        n_cells += len(cells)
        n_plots += int(plot_t6(style, ser, results / "plots") is not None)
    return "\n".join(texts), n_cells, n_plots


def calibration(cell_dir: Path, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """From a W = 1 single-file cell (the smoke's, design S8): the timeout
    share, the per-rollout loop time, one file's wall time, and a suggested
    ``t6.cell_timeout_s``: 3 x files_per_worker x that file's wall (the factor
    covers contention at large W and a deeper file)."""
    manifest = json.loads((cell_dir / "manifest.json").read_text(encoding="utf-8"))
    events, files = load_samples(cell_dir / "events.jsonl"), load_samples(
        cell_dir / "files.jsonl"
    )
    s = summarize_t6(events, files, manifest)
    roll = s["rollouts"] or 0
    timed = sum(
        (c["outcomes"].get("timed_out") or 0) for c in s["by_candidate"].values()
    )
    walls = [float(f["t_end"]) - float(f["t_start"]) for f in files]
    file_s = max(walls) if walls else None
    per_worker = int((cfg.get("t6") or {}).get("files_per_worker", 3))
    lt = (s["workers"].get(0) or s["workers"].get("0") or {}).get("loop_s")
    return {
        "cell": str(cell_dir),
        "rollouts": roll,
        "timeout_share": round(timed / roll, 4) if roll else None,
        "per_rollout_loop_s": round(float(lt) / roll, 4) if lt and roll else None,
        "file_wall_s": None if file_s is None else round(file_s, 1),
        "suggested_cell_timeout_s": (
            None if file_s is None else int(3 * per_worker * file_s)
        ),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: ``--calibrate <cell dir>``."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(EXPERIMENT_DIR / "config.yaml"))
    ap.add_argument("--calibrate", required=True, help="a W = 1 single-file T6 cell")
    args = ap.parse_args(argv)
    cal = calibration(Path(args.calibrate), load_config(args.config))
    for k, v in cal.items():
        print(f"{k}: {v}")
    print(
        "set t6.cell_timeout_s in config.yaml to at least suggested_cell_timeout_s "
        "before scripts/14_run_t6.sh"
    )
    return 0 if cal["rollouts"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
