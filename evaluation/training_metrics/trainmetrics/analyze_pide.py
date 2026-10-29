"""T1's PIDE MCP row (design D3, plan B2, stage 8a): the PIDE cells are
written in T1's format (:mod:`trainmetrics.drive_pide_chunks`), so each is
summarised by T1's own :func:`metrics_t12.summarize_cell` and set beside the
IPS row at the same W. The same records pair block by block with IPS's for
the per-call comparison (D20, the latency paragraph of the report)."""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .csvout import write_csv
from .metrics_t12 import (
    CLASSES,
    census_index,
    load_t1_cells,
    p50,
    p95,
    series_t1,
    size_class,
    summarize_cell,
)

POLL_S = 0.1  # both rows' resolution: a pair within it differs by one poll at most

Key = Tuple[str, int, str]  # stem, block index, block sha
Rec = Tuple[float, bool, bool]  # wall_s, proved, timed_out
Pair = Tuple[float, float, Optional[str]]  # IPS s, PIDE s, size class


def _f(v: Optional[float], nd: int = 0) -> str:
    return "-" if v is None else f"{v:,.{nd}f}"


def _ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or not b else a / b


def _round(x: Optional[float], nd: int = 4) -> Optional[float]:
    return None if x is None else round(x, nd)


def _blocks_by_w(cells: Sequence[Dict[str, Any]]) -> Dict[int, Dict[Key, List[Rec]]]:
    """Per W over the ok cells: every block's records, keyed by block."""
    out: Dict[int, Dict[Key, List[Rec]]] = {}
    for cell in cells:
        if cell.get("outcome") != "ok":
            continue
        acc = out.setdefault(int(cell.get("workers") or 0), {})
        for rr in cell.get("_replays") or []:
            for b in rr.get("blocks") or []:
                key = (str(rr["stem"]), int(b["index"]), str(b.get("block_sha")))
                acc.setdefault(key, []).append(
                    (
                        float(b.get("wall_s") or 0),
                        bool(b.get("proved")),
                        bool(b.get("timed_out")),
                    )
                )
    return out


def _pair_row(
    w: int, cls: str, sub: Sequence[Pair], in_both: int, dropped: Dict[str, int]
) -> Dict[str, Any]:
    ia, pb = [p[0] for p in sub], [p[1] for p in sub]
    diff = [x - y for x, y in zip(ia, pb)]
    return {
        "workers": w,
        "class": cls,
        "pairs": len(sub),
        "blocks_in_both": in_both,
        "dropped_timed_out": dropped["timed_out"],
        "dropped_one_tool_only": dropped["one_tool_only"],
        "dropped_neither": dropped["neither"],
        "ips_sum_s": round(sum(ia), 1),
        "pide_sum_s": round(sum(pb), 1),
        "ips_over_pide": _round(_ratio(sum(ia), sum(pb))),
        "ips_mean_s": round(statistics.mean(ia), 4),
        "pide_mean_s": round(statistics.mean(pb), 4),
        "ips_p50_s": _round(p50(ia)),
        "pide_p50_s": _round(p50(pb)),
        "ips_p95_s": _round(p95(ia)),
        "pide_p95_s": _round(p95(pb)),
        "diff_mean_s": round(statistics.mean(diff), 4),
        "diff_p50_s": _round(p50(diff)),
        "within_one_poll": round(
            sum(1 for d in diff if abs(d) <= POLL_S + 1e-9) / len(diff), 4
        ),
        "ips_faster": round(sum(1 for d in diff if d < 0) / len(diff), 4),
    }


def pair_latency(  # pylint: disable=too-many-locals
    ips_cells: Sequence[Dict[str, Any]],
    pide_cells: Sequence[Dict[str, Any]],
    census: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Per W, the blocks both rows proved, paired by block (D20): a block's
    time per tool is the median over the repeats that ran it; a block timed
    out in any repeat of either tool, or proved under one tool only (PIDE
    MCP's scratch names, D13), is left out and counted. One row for all the
    pairs, then one per size class: the sums and means (the comparison; the
    0.1 s poll steps both medians), medians, p95s, the paired difference, the
    share of pairs within one poll of each other and the share with IPS the
    faster."""
    _, idx = census_index(census)
    ips_w, pide_w = _blocks_by_w(ips_cells), _blocks_by_w(pide_cells)
    out: List[Dict[str, Any]] = []
    for w in sorted(set(ips_w) & set(pide_w)):
        ips, pide = ips_w[w], pide_w[w]
        both = sorted(set(ips) & set(pide))
        dropped = {"timed_out": 0, "one_tool_only": 0, "neither": 0}
        pairs: List[Pair] = []
        for key in both:
            a, b = ips[key], pide[key]
            if any(r[2] for r in a + b):
                dropped["timed_out"] += 1
                continue
            pa, pb = all(r[1] for r in a), all(r[1] for r in b)
            if pa != pb:
                dropped["one_tool_only"] += 1
                continue
            if not pa:
                dropped["neither"] += 1
                continue
            n, partial = idx.get(key[:2], (None, False))
            pairs.append(
                (
                    statistics.median(r[0] for r in a),
                    statistics.median(r[0] for r in b),
                    None if partial else size_class(n),
                )
            )
        for cls in ("all",) + tuple(c for c, _lo, _hi in CLASSES):
            sub = pairs if cls == "all" else [p for p in pairs if p[2] == cls]
            if sub:
                out.append(_pair_row(w, cls, sub, len(both), dropped))
    return out


def _paired_lines(paired: Sequence[Dict[str, Any]]) -> List[str]:
    """The paired per-call paragraph and table (D20)."""
    counts = "; ".join(
        f"W = {r['workers']}: {r['blocks_in_both']} blocks in both rows, {r['pairs']} "
        f"paired ({r['dropped_timed_out']} timed out, {r['dropped_one_tool_only']} "
        f"proved under one tool only, {r['dropped_neither']} under neither: left out)"
        for r in paired
        if r["class"] == "all"
    )
    lines = [
        "",
        "Per call, the same blocks paired (`t1_pide_latency.csv`, D20): a block both "
        "rows proved, its time per tool the median over the repeats that ran it. The "
        "sums and means are the comparison, the 0.1 s poll steps both medians; *within "
        "one poll* is the share of pairs whose times differ by at most 0.1 s. "
        f"{counts}.",
        "",
        "| W | chunk size | pairs | IPS sum s | PIDE sum s | IPS / PIDE | "
        "IPS mean / p50 / p95 s | PIDE mean / p50 / p95 s | diff mean / p50 s | "
        "within one poll | IPS faster |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in paired:
        lines.append(
            f"| {r['workers']} | {r['class']} | {r['pairs']} | "
            f"{_f(r['ips_sum_s'], 1)} | {_f(r['pide_sum_s'], 1)} | "
            f"{_f(r['ips_over_pide'], 3)} | "
            f"{_f(r['ips_mean_s'], 3)} / {_f(r['ips_p50_s'], 3)} / "
            f"{_f(r['ips_p95_s'], 3)} | {_f(r['pide_mean_s'], 3)} / "
            f"{_f(r['pide_p50_s'], 3)} / {_f(r['pide_p95_s'], 3)} | "
            f"{_f(r['diff_mean_s'], 3)} / {_f(r['diff_p50_s'], 3)} | "
            f"{_f(r['within_one_poll'], 2)} | {_f(r['ips_faster'], 2)} |"
        )
    return lines


def snippet_pide(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    pide: Sequence[Dict[str, Any]],
    ips: Sequence[Dict[str, Any]],
    cells: Sequence[Dict[str, Any]],
    ips_env: Optional[Dict[str, Any]] = None,
    paired: Optional[Sequence[Dict[str, Any]]] = None,
) -> str:
    """The PIDE MCP row paragraph; ``ips_env``: the IPS cells' server env
    (its proof-checking settings are stated beside PIDE's, review 9ad8799 S1);
    ``paired``: the per-call pairing of :func:`pair_latency`."""
    by_w = {int(s["workers"]): s for s in ips}
    env = ips_env or {}
    opts = (
        (cells[0].get("session_options") or {}).get("isabelle_options")
        if cells
        else None
    )
    poll = cells[0].get("poll_s") if cells else None
    lines = [
        "## T1: the PIDE MCP row (generated by trainmetrics.analyze)",
        "",
        f"PIDE MCP A (one `isabelle pide_mcp` process), the same assignment and census as IPS. "
        "Both rows pay the same setup: each file runs on a fresh session, started inside the window "
        "and stopped after the file (IPS acquires it with the file's imports, D6; PIDE MCP starts a "
        "session on the same heap and opens a scratch theory with them, D18). Each block is appended "
        f"with one `edit` and timed until `get_state` shows it settled (polled every {poll} s: the "
        "same 0.1 s resolution as IPS's `verify_chunk`). Edit-and-settle throughput, not a verdict "
        "call: IPS's `verify_chunk` also undoes a chunk that fails and, after one that succeeds, asks "
        "whether a proof is still open, about one 0.1 s poll per call that the PIDE row does not "
        "pay. Proof checking: "
        f"PIDE MCP sessions {opts}; IPS `parallel_proofs={env.get('ISABELLE_PARALLEL_PROOFS', '?')}` "
        "(ISABELLE_PARALLEL_PROOFS); both are interactive PIDE sessions, where any value above 0 is "
        "Isabelle's interactive level 3, so the rows check proofs alike. IPS enters each file under its "
        "own theory name (D13); PIDE MCP names it after its scratch file, so the corpus' blocks that name "
        "their own theory (36 in 17 files, about 1 %) and the blocks that use them fail there, which "
        "lowers PIDE's verified chunks by that share. The loop closes the text with `end` "
        "after every edit, so a block that opens an `instantiation` is counted as failed "
        "(the bad count grows before the instance proof arrives and falls again at the "
        "block that brings it: both deltas count as failed); with the self-naming blocks, "
        "under 1 % of the blocks (the paired table's *proved under one tool only*). "
        "Latencies step by the 0.1 s "
        "poll in both rows (a block settles at the first or the second reading), so p50s that differ "
        "by up to 0.1 s differ by one poll, not by their ratio: chunks/min is the comparison.",
        "",
        "| W | tool | chunks/min (verify only) | chunks/min (incl. setup) | census work rate | p50 / p95 s "
        "| timeout rate | PIDE / IPS (verify only) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in pide:
        w = int(s["workers"])
        i = by_w.get(w) or {}
        for tool, row in (("IPS", i), ("PIDE MCP A", s)):
            if not row:
                continue
            ratio = (
                _ratio(s.get("chunks_per_min"), i.get("chunks_per_min"))
                if tool != "IPS"
                else None
            )
            lines.append(
                f"| {w} | {tool} | {_f(row.get('chunks_per_min'))} | {_f(row.get('chunks_per_min_incl_setup'))} | "
                f"{_f(row.get('census_work_rate'), 2)} | {_f(row.get('latency_p50_s'), 3)} / "
                f"{_f(row.get('latency_p95_s'), 3)} | {_f(row.get('timeout_rate'), 4)} | "
                f"{_f(ratio, 2) if tool != 'IPS' else ''} |"
            )
    if paired:
        lines += _paired_lines(paired)
    return "\n".join(lines) + "\n"


def write_pide(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    results: Path,
    census: Dict[str, Any],
    tables: Path,
    ips_series: Sequence[Dict[str, Any]],
    ips_env: Optional[Dict[str, Any]] = None,
    ips_cells: Optional[Sequence[Dict[str, Any]]] = None,
) -> Tuple[str, int]:
    """The PIDE row's tables and paragraph ("" without a PIDE cell); (text,
    cells). The IPS cells (``ips_cells``, else loaded from ``results``) pair
    with the PIDE cells for ``t1_pide_latency.csv``."""
    cells = load_t1_cells(results, "pide")
    if not cells:
        return "", 0
    rows: List[Dict[str, Any]] = [
        {"tool": "pide-a", **summarize_cell(c, census)} for c in cells
    ]
    ser = series_t1(rows)
    write_csv(tables / "t1_pide_cells.csv", rows)
    write_csv(tables / "t1_pide_series.csv", ser)
    paired = pair_latency(
        load_t1_cells(results) if ips_cells is None else ips_cells, cells, census
    )
    if paired:
        write_csv(tables / "t1_pide_latency.csv", paired)
    return snippet_pide(ser, ips_series, cells, ips_env, paired), len(cells)
