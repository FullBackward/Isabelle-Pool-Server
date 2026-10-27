"""T6 tables, the rate plot and the T6 part of REPORT_SNIPPET.md (plan stage
6b), plus the calibration the smoke prints (design S8). Every number is
recomputed from a cell's raw records (``events.jsonl``, ``files.jsonl``,
``tiers.jsonl``, ``samples.jsonl``) with the current
:mod:`trainmetrics.metrics_t6`.

Across cells, per W (repeats averaged): both rates per hour (not divided into
a speedup, owner 2026-10-27: a cell at W runs more files than the W = 1 cell
and each repeat its own seeded files, so a ratio of rates mixes the files'
proof times and reached 40.8 at W = 32 on the server), the per-rollout loop
time on the matched files (the W = 1 cell's, present in every cell, S3) and
its slowdown, the setup share
(acquire, enter, prefix, close), the timeout share of the rollouts, queueing
p95, stopped files, outcomes apart, failed integrity checks and the worst
memory tier. The cell timeout comes from :mod:`trainmetrics.calibrate_t6`."""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
    tried = sum(c["wall"]["n"] for c in s["by_candidate"].values())
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
        # of the T6a candidates, or of the T6b alternatives
        "timeout_share": round(timed / tried, 4) if tried else None,
        "steps_per_h": s.get("steps_per_h"),
        "alternatives_kept_share": (s.get("alternatives") or {}).get("kept_share"),
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
    """Per W, repeats averaged, with the slowdown on the matched files against
    W = 1."""
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
                        "steps_per_h",
                        "alternatives_kept_share",
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


INTRO = {
    "t6a": (
        "T6a whole-proof rollouts",
        "At a problem: a checkpoint and the 8 D9 candidates in a seeded order (the corpus proof, "
        "`by auto` and `by simp` twice each, `sorry`, the proof truncated, a method that fails at once), "
        "a kept one undone by a restore. A rollout is one candidate. The proved share is fixed by the "
        "mix: up to 5 of 8 candidates can prove (the corpus proof, `by auto` twice, `by simp` twice).",
    ),
    "t6b": (
        "T6b step-level paths",
        "At a problem (a theorem of 2 to 40 proof commands): the statement, then per proof command a "
        "checkpoint, 2 alternatives drawn from `apply auto`, `apply simp`, `apply -`, `by auto`, "
        "`by simp` (10 s each; a kept one undone by a restore, a failed one discarded) and the corpus "
        "command. A rollout is one path; it ends with the theorem proved unless a corpus command fails "
        "(then the path is undone and the block replayed whole).",
    ),
}


def snippet_t6(
    style: str, cells: Sequence[Dict[str, Any]], ser: List[Dict[str, Any]]
) -> str:
    """The paragraph of one style."""
    t6 = cells[0]["manifest"].get("t6") or {}
    head, what = INTRO.get(style, (style, ""))
    steps = style == "t6b"
    lines = [
        f"## {head} (generated by trainmetrics.analyze)",
        "",
        f"{len(cells)} cells. Each worker takes {t6.get('files_per_worker')} file(s) of the T1 assignment, "
        f"each on its own session; each of a file's first {(t6.get(style) or {}).get('problems_per_file')} "
        f"eligible census-proved theorems is a problem. {what} No model runs, so the rates are the "
        "server's share of a training loop. End-to-end rates include acquisition, entering and replaying "
        "up to each problem; the loop rate counts each worker's loop calls only.",
        "",
        "| W | reps | rollouts/h end to end | loop rollouts/h | proved/h | problems/h "
        + ("| steps/h " if steps else "")
        + "| per-rollout time on the W = 1 files (s) | slowdown | setup share | timeouts | queue p95 (s) |",
        "|---|---|---|---|---|---|"
        + ("---|" if steps else "")
        + "---|---|---|---|---|",
    ]
    for s in ser:
        lines.append(
            f"| {s['workers']} | {s['reps']} | {_f(s['rollouts_per_h'])} | {_f(s['loop_rollouts_per_h'])} | "
            f"{_f(s['proved_per_h'])} | {_f(s['problems_per_h'])} | "
            + (f"{_f(s['steps_per_h'])} | " if steps else "")
            + f"{_f(s['matched_per_rollout_s'], 3)} | {_f(s['matched_slowdown'], 2)} | "
            f"{_f(s['setup_share'], 2)} | {_f(s['timeout_share'], 3)} | {_f(s['queue_p95_s'], 3)} |"
        )
    stopped = sum(s["stopped"] for s in ser)
    apart = sum(s["outcomes_apart"] for s in ser)
    failed = sum(s["integrity_failed"] for s in ser)
    tiers = ", ".join(f"W = {s['workers']}: {s['worst_tier'] or '-'}" for s in ser)
    kept = ", ".join(
        f"W = {s['workers']}: {_f(s['alternatives_kept_share'], 2)}" for s in ser
    )
    lines += [
        "",
        "The rates at different W are not divided into a speedup: each W runs more files than W = 1 "
        "(and each repeat its own seeded files), so a ratio of rates mixes the files' proof times and "
        "can exceed W. The per-rollout time on the W = 1 files, present in every cell, is the "
        "like-for-like comparison: a slowdown of 1.00 means a worker's loop runs as fast as alone.",
        "",
        f"Stopped files: {stopped}; outcomes outside the rollouts (memory, prover death, gone session, "
        f"error): {apart}; failed integrity checks: {failed}. Worst memory tier per W: {tiers}."
        + (
            f" Share of alternatives kept, and so restored (it follows from the proof style): {kept}. "
            "Timeouts are the alternatives' share."
            if steps
            else " Timeouts are the candidates' share."
        ),
    ]
    return "\n".join(lines) + "\n"


def plot_t6(style: str, ser: List[Dict[str, Any]], out: Path) -> Optional[str]:
    """The end-to-end rate and the per-rollout time on the W = 1 files against
    W (no loop rate across W: it mixes files, D19); skipped without
    matplotlib."""
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
    ax.plot(
        ws,
        [s["rollouts_per_h"] for s in ser],
        marker="s",
        label="rollouts / h, end to end",
    )
    ax.set_xscale("log", base=2)
    ax.set_xticks(ws)
    ax.set_xticklabels([str(w) for w in ws])
    ax.set_xlabel("workers W")
    ax.set_ylabel("rollouts / h")
    ax.set_ylim(bottom=0)
    ax.grid(True, alpha=0.3)
    ax2 = ax.twinx()
    ax2.plot(
        ws,
        [s["matched_per_rollout_s"] for s in ser],
        color="grey",
        ls="--",
        marker="o",
        label="s per rollout, W = 1 files (right)",
    )
    ax2.set_ylabel("seconds per rollout (W = 1 files)")
    per = [s["matched_per_rollout_s"] for s in ser if s["matched_per_rollout_s"]]
    ax2.set_ylim(0, 1.5 * max(per) if per else None)  # the line off the frame
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left")
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
    for style in ("t6a", "t6b"):
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
