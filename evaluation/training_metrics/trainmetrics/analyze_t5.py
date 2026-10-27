"""T5 tables and the T5 part of REPORT_SNIPPET.md (plan stage 3c-2). Every
number is recomputed from the cells' raw records with the current analysis
code (the attribution and contention fixes after a cell ran apply): the W = 1
cell's family x budget table, the memory a timeout leaves behind beside peak
growth and the prover level each file ends at, the seeds that did not loop,
and for the contention cell the other workers' blocks grouped by what the
others were doing, the share of blocks that straddle a state change, and the
loss against T1's cell at the same W on matched blocks (reviews f3220d5,
73f9cd4, e0f8ac6, f2739e1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .contention import (
    STATES,
    census_walls,
    state_at,
    state_table,
    tier_summary,
    worker_intervals,
)
from .csvout import ok_cells, write_csv
from .metrics_t5 import MB, annotate, load_samples, summarize
from .metrics_t12 import p50, p95


def load_t5_cells(results: Path) -> List[Dict[str, Any]]:
    """Every finished (ok) T5 cell under ``<results>/t5/w*/rep*``: its
    manifest, its file records (re-annotated from its samples) and samples."""
    cells: List[Dict[str, Any]] = []
    for d, m in ok_cells(results, "t5/w*/rep*/manifest.json"):
        files = load_samples(d / "positions.jsonl")
        samples = load_samples(d / "samples.jsonl")
        annotate(files, samples, by_session=int(m.get("workers") or 1) > 1)
        cells.append({"dir": d, "manifest": m, "files": files, "samples": samples})
    return cells


def last_call_end(f: Dict[str, Any]) -> Optional[float]:
    """When the file's last call before the close ended: ``t_close`` when
    recorded, else the latest end of its blocks, attempts and the blocks after
    the seeds (records from before 0268dbe)."""
    if f.get("t_close") is not None:
        return float(f["t_close"])
    ends = [
        float(x["t_start"]) + float(x["wall_s"])
        for p in f.get("positions") or []
        for x in list(p.get("attempts") or [])
        + ([p["block"]] if p.get("block") else [])
        if x.get("t_start") is not None and x.get("wall_s") is not None
    ] + [
        float(b["t_start"]) + float(b["wall_s"])
        for b in f.get("blocks") or []
        if b.get("t_start") is not None
    ]
    return max(ends) if ends else None


def file_levels(
    files: Sequence[Dict[str, Any]], samples: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Per file with a matched prover: its RSS before the first attempt, its
    peak over the attempts, and the level the file ends at — the prover's last
    sampler row at or before the end of the file's last call before the close
    (review 0268dbe B1: a row inside the close sees the prover tearing down);
    ``end_censored`` when there is no such row. The lasting cost beside the
    per-attempt figures (review f3220d5 note)."""
    out: List[Dict[str, Any]] = []
    for f in files:
        mem = [
            a["prover_mem"]
            for p in f.get("positions") or []
            for a in p.get("attempts") or []
            if (a.get("prover_mem") or {}).get("pid")
        ]
        if not mem:
            continue
        pid = int(mem[0]["pid"])
        limit = last_call_end(f)
        rows = [
            r
            for r in samples
            if limit is not None
            and float(r["t"]) <= limit
            and any(int(q["pid"]) == pid for q in r.get("procs") or [])
        ]
        end = None
        if rows:
            q = next(q for q in rows[-1]["procs"] if int(q["pid"]) == pid)
            end = round(int(q["rss"]) / MB, 1)
        out.append(
            {
                "stem": f["stem"],
                "worker": int(f.get("worker") or 0),
                "start_mb": mem[0].get("rss_before_mb"),
                "peak_mb": max(float(m.get("rss_peak_mb") or 0) for m in mem),
                "end_mb": end,
                "end_censored": end is None,
            }
        )
    return out


def straddle_share(files: Sequence[Dict[str, Any]]) -> Optional[float]:
    """The share of corpus blocks whose other-worker state mix differs between
    the block's start and end (review e0f8ac6 note: the midpoint key is one
    reading of a block that may span a state change)."""
    ivs = worker_intervals(files)
    n = changed = 0
    for f in files:
        w = int(f.get("worker") or 0)
        for b in f.get("blocks") or []:
            t0 = float(b["t_start"])
            t1 = t0 + float(b["wall_s"])

            def mix(t: float, w: int = w) -> Tuple[int, ...]:
                st = [state_at(iv, e, t) for v, (iv, e) in ivs.items() if v != w]
                return tuple(st.count(s) for s in STATES)

            n += 1
            changed += int(mix(t0) != mix(t1))
    return round(changed / n, 3) if n else None


def matched_t1(
    files: Sequence[Dict[str, Any]], results: Path, workers: int, rep: int
) -> Optional[Dict[str, Any]]:
    """The contention cell's corpus blocks against the same blocks (worker,
    file, index) in T1's cell at the same W and repeat: the T1 assignment
    makes a contention worker's files the first of the T1 worker's. None when
    that T1 cell is absent."""
    path = results / "t1" / f"w{workers}" / f"rep{rep}" / "replays.jsonl"
    t1 = {
        (int(r["worker"]), str(r["stem"]), int(b["index"])): float(b["wall_s"])
        for r in load_samples(path)
        for b in r.get("blocks") or []
        if b.get("proved") and b.get("wall_s")
    }
    if not t1:
        return None
    pairs = [
        (float(b["wall_s"]), t1[key])
        for f in files
        for b in f.get("blocks") or []
        for key in [(int(f.get("worker") or 0), str(f["stem"]), int(b["index"]))]
        if key in t1 and b.get("proved")
    ]
    if not pairs:
        return None
    ratios = [a / b for a, b in pairs if b > 0]
    return {
        "blocks": len(pairs),
        "ratio_p50": round(float(p50(ratios) or 0), 3),
        "ratio_p95": round(float(p95(ratios) or 0), 3),
        "sum_ratio": round(sum(a for a, _ in pairs) / sum(b for _, b in pairs), 3),
    }


def cell_rows(
    cells: Sequence[Dict[str, Any]], census: Dict[str, Any], results: Path
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """The T5 table rows (family x budget per cell) and one contention row per
    contention cell."""
    table: List[Dict[str, Any]] = []
    contention: List[Dict[str, Any]] = []
    for c in cells:
        m = c["manifest"]
        w = int(m.get("workers") or 1)
        s = summarize(c["files"])
        for r in s["table"]:
            table.append({"workers": w, "rep": m.get("rep"), **r})
        if w > 1:
            st = state_table(c["files"], census_walls(census))
            contention.append(
                {
                    "workers": w,
                    "rep": m.get("rep"),
                    "loops": st["loops"],
                    **{
                        f"loop_vs_replay_{k}": v
                        for k, v in st["loop_vs_replay"].items()
                    },
                    "straddle_share": straddle_share(c["files"]),
                    "t1_matched": matched_t1(
                        c["files"], results, w, int(m.get("rep") or 0)
                    ),
                    "tiers": tier_summary(load_samples(c["dir"] / "tiers.jsonl")),
                    "executor_threads": m.get("server_executor_threads"),
                    "by_state": st["by_state"],
                }
            )
    return table, contention


def _f(v: Any, nd: int = 1) -> str:
    return "-" if v is None else f"{float(v):.{nd}f}"


def _why(errors: Sequence[str]) -> str:
    """The errors of the seeds that did not loop as one table cell: Isabelle's
    messages span lines, which broke the markdown table of the server run
    (2026-10-27)."""
    return "; ".join(" ".join(e.split()).replace("|", "\\|") for e in errors) or "-"


def _family_label(name: str, fam: Dict[str, Any]) -> str:
    return f"{name} {fam['seeds']} ({'/'.join(fam['goal_sources'])} goals)"


def _w1_lines(cell: Dict[str, Any], table: Sequence[Dict[str, Any]]) -> List[str]:
    """The W = 1 cell's paragraph and tables."""
    s = summarize(cell["files"])
    fams = s["families"]
    seeds = ", ".join(_family_label(k, v) for k, v in fams.items())
    lines = [
        f"Seeds: {seeds}, checked right before {s['positions']} census-proved theorems of "
        f"{s['files']} files, a session per file (W = 1, rep {cell['manifest'].get('rep')}). "
        "Every seed takes the smallest budget; a seed "
        "that still loops climbs to the larger ones while its family's count lasts, so the "
        "larger budgets' counts are maxima.",
        "",
        "| family | budget s | attempts | timed out | with stuck line | undo p50 / p95 / max s | "
        "source unchanged | retained p50 / max MB | peak growth max MB | censored |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    rep = cell["manifest"].get("rep")
    for r in (r for r in table if r["workers"] == 1 and r.get("rep") == rep):
        lines.append(
            f"| {r['family']} | {_f(r['budget_s'], 0)} | {r['attempts']} | "
            f"{_f(r['timed_out_frac'], 3)} | {_f(r['stuck_line_frac'], 3)} | "
            f"{_f(r['undo_p50_s'], 2)} / {_f(r['undo_p95_s'], 2)} / {_f(r['undo_max_s'], 2)} | "
            f"{_f(r['source_unchanged_frac'], 3)} | "
            f"{_f(r['retained_p50_mb'], 0)} / {_f(r['retained_max_mb'], 0)} | "
            f"{_f(r['peak_growth_max_mb'], 0)} | {r.get('retained_censored', 0)} |"
        )
    lines += [
        "",
        "| family | seeds that did not loop | why | memory / prover died | "
        "next block as in census | recovery p50 s (census p50) |",
        "|---|---|---|---|---|---|",
    ]
    for k, v in fams.items():
        lines.append(
            f"| {k} | {v['not_looping']} | {_why(v['not_looping_errors'])} | "
            f"{sum(v['ended_apart'].values())} | {_f(v['verdict_match_frac'], 3)} | "
            f"{_f(v['recovery_p50_s'], 2)} ({_f(v['census_p50_s'], 2)}) |"
        )
    levels = file_levels(cell["files"], cell["samples"])
    if levels:
        per = "; ".join(
            f"{x['stem']} {_f(x['start_mb'], 0)} -> peak {_f(x['peak_mb'], 0)} -> "
            f"end {_f(x['end_mb'], 0) if not x['end_censored'] else 'n/a'}"
            for x in levels
        )
        lines += ["", f"Prover level per file (MB): {per}."]
    return lines + [
        "",
        "Undo = wall time past the budget. Recovery = the next corpus block's own time, "
        "measured after a 1.5 s quiet pause that follows every attempt (for the memory "
        "sampler), not the time from the timeout. Retained = the prover's RSS change across "
        "one attempt and the pause: it can be negative (Poly/ML compacts during a long loop) "
        "and is not a leak measure; read it beside the peak growth and the level each file "
        "ends at. Memory and prover-death outcomes are counted apart, outside the fractions.",
    ]


def _contention_lines(c: Dict[str, Any]) -> List[str]:
    """One contention cell's paragraph and its by-state table."""
    t1 = c["t1_matched"] or {}
    straddle = c["straddle_share"]
    if c["loop_vs_replay_blocks"]:
        lvr = (
            f"A corpus block while others loop takes {_f(c['loop_vs_replay_slowdown'], 3)}x "
            "its time while the same number of others replay "
            f"(p95 {_f(c['loop_vs_replay_slowdown_p95'], 3)}, "
            f"{c['loop_vs_replay_blocks']} blocks); "
        )
    else:  # review 0268dbe S1: no broken sentence for a cell without the baseline
        lvr = "No replay baseline at this W (no block ran while others replayed, none looping); "
    lines = [
        "",
        f"Contention at W = {c['workers']} (rep {c['rep']}): {c['loops']} loops. {lvr}"
        f"{_f(100 * straddle if straddle is not None else None)} % of blocks span a change "
        "of the others' states (they are keyed at their midpoint). Against T1's "
        f"W = {c['workers']} cell on {t1.get('blocks', 0)} matched blocks: p50 ratio "
        f"{_f(t1.get('ratio_p50'), 3)}, summed {_f(t1.get('sum_ratio'), 3)}. Memory tier: "
        f"{c['tiers'].get('states')}, used max {_f(c['tiers'].get('used_max_mb'), 0)} MB. "
        f"Backend executor: {c['executor_threads']} threads.",
        "",
        "| others in loop / replay / setup / idle / done | blocks | ratio to census p50 / p95 "
        "| queue p50 s |",
        "|---|---|---|---|",
    ]
    for r in c["by_state"]:
        mix = " / ".join(str(r[s]) for s in STATES)
        lines.append(
            f"| {mix} | {r['n']} | {_f(r['ratio_p50'], 3)} / {_f(r['ratio_p95'], 3)} | "
            f"{_f(r['queue_p50_s'], 3)} |"
        )
    return lines


def snippet_t5(
    cells: Sequence[Dict[str, Any]],
    table: Sequence[Dict[str, Any]],
    contention: Sequence[Dict[str, Any]],
) -> str:
    """The T5 part of REPORT_SNIPPET.md."""
    w1 = [c for c in cells if int(c["manifest"].get("workers") or 1) == 1]
    lines = ["## T5 bounded step (generated by trainmetrics.analyze)", ""]
    lines += (
        _w1_lines(w1[0], table) if w1 else ["No W = 1 T5 cell in the results tree."]
    )
    for c in contention:
        lines += _contention_lines(c)
    return "\n".join(lines) + "\n"


def write_t5(results: Path, census: Dict[str, Any], tables: Path) -> Tuple[str, int]:
    """The T5 tables under ``tables`` and the T5 snippet text ("" without a
    T5 cell); returns (text, number of T5 cells)."""
    cells = load_t5_cells(results)
    if not cells:
        return "", 0
    table, contention = cell_rows(cells, census, results)
    write_csv(tables / "t5_bounded.csv", table)
    write_csv(tables / "t5_contention.csv", contention)
    return snippet_t5(cells, table, contention), len(cells)
