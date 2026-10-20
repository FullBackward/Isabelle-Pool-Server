"""Analysis of the cells under results/: steady-state, retained and peak
memory per cell (tool PSS by role, host used), least-squares marginal and
fixed cost per (tool, heap) — on PSS and on host used (two slopes, design
§4) — "sessions per 100 GB" on the host-used slope, start/load/re-check
latencies (p50/p95), the gateway/MCP JVM's growth over a series, and the
report snippet with the tables; PNG plots when matplotlib is installed.

    python -m memscale.analyze [--results results] [--out results/tables] [--budget-gb 100]
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

MB = 2**20
GB = 2**30
SESSION_POLY_MIN = (
    64 * MB
)  # a poly below this is the statistics monitor, not a session (fallback rule)


# ------------------------------------------------------------------ loading
def load_cells(results: Path) -> List[Dict[str, Any]]:
    """Every cell with a manifest and samples under ``results``."""
    cells: List[Dict[str, Any]] = []
    for manifest in sorted(results.rglob("manifest.json")):
        samples = manifest.with_name("samples.jsonl")
        if not samples.exists():
            continue
        with open(manifest, "r", encoding="utf-8") as fh:
            m = dict(json.load(fh))
        rows = []
        with open(samples, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        m["_rows"] = rows
        m["_dir"] = str(manifest.parent)
        cells.append(m)
    return cells


def _phase_rows(cell: Dict[str, Any], phase: str) -> List[Dict[str, Any]]:
    return [r for r in cell["_rows"] if r.get("phase") == phase]


def _tail(rows: List[Dict[str, Any]], tail_s: float) -> List[Dict[str, Any]]:
    if not rows:
        return []
    t1 = rows[-1]["t"]
    return [r for r in rows if r["t"] >= t1 - tail_s] or rows[-1:]


def _median(xs: Iterable[float]) -> Optional[float]:
    xs = list(xs)
    return statistics.median(xs) if xs else None


def _pss_total(r: Dict[str, Any]) -> float:
    return float(r["sum"]["total"]["pss"])


def _pss_role(r: Dict[str, Any], role: str) -> float:
    return float((r["sum"].get(role) or {}).get("pss", 0))


def _session_polys(r: Dict[str, Any]) -> int:
    return sum(
        1
        for p in r.get("procs", [])
        if p.get("role") == "poly" and p.get("pss", 0) >= SESSION_POLY_MIN
    )


def _host_used(r: Dict[str, Any]) -> Optional[float]:
    v = (r.get("host") or {}).get("used")
    return None if v is None else float(v)


# ------------------------------------------------------------- per cell
def summarize_cell(cell: Dict[str, Any], tail_s: float = 30.0) -> Dict[str, Any]:
    """One row per cell: medians of the last ``tail_s`` of P2/P4, the P5 peak,
    the P0 baseline (first rows), per-role PSS, host used, latencies."""
    p0 = _phase_rows(cell, "P0")
    p2, p4 = (_tail(_phase_rows(cell, ph), tail_s) for ph in ("P2", "P4"))
    p5all = _phase_rows(cell, "P5")
    out: Dict[str, Any] = {
        "tool": cell.get("tool"),
        "topology": cell.get("topology"),
        "heap": cell.get("heap"),
        "k": int(cell.get("k", 0)),
        "rep": int(cell.get("rep", 0)),
        "outcome": cell.get("outcome"),
        "scope_used": (cell.get("environment") or {}).get("scope_used"),
        "external": bool(
            (cell.get("driver") or {}).get("external", False)
        ),  # smoke on a running server
        "cgroup_dir": (cell.get("environment") or {}).get("cgroup_dir"),
        "host_baseline_mb": _mb(_median(_host_used(r) or 0 for r in p0[:5])),
        "pss_baseline_mb": _mb(_median(_pss_total(r) for r in p0[:5])),
        "pss_idle_mb": _mb(_median(_pss_total(r) for r in p2)),
        "pss_settled_mb": _mb(_median(_pss_total(r) for r in p4)),
        "pss_peak_mb": _mb(max((_pss_total(r) for r in p5all), default=None)),
        "jvm_idle_mb": _mb(_median(_pss_role(r, "jvm") for r in p2)),
        "jvm_settled_mb": _mb(_median(_pss_role(r, "jvm") for r in p4)),
        "poly_idle_mb": _mb(_median(_pss_role(r, "poly") for r in p2)),
        "poly_settled_mb": _mb(_median(_pss_role(r, "poly") for r in p4)),
        "session_polys_settled": _median(_session_polys(r) for r in p4),
        "host_idle_mb": _mb(_median(_host_used(r) or 0 for r in p2)),
        "host_settled_mb": _mb(_median(_host_used(r) or 0 for r in p4)),
        "host_peak_mb": _mb(max(((_host_used(r) or 0) for r in p5all), default=None)),
        # the cell's own cgroup peak = max sampled `current` over P5; the kernel's
        # `memory.peak` is a lifetime value (review facc4c9 S2): reported only when it rose
        "cgroup_cell_peak_mb": _mb(
            max(
                ((r.get("cgroup") or {}).get("current") or 0 for r in p5all),
                default=None,
            )
        ),
        "cgroup_peak_rose": _peak_rose(p0, p5all),
    }
    # host used as a DELTA over the cell's own P0 (review facc4c9 S1): the
    # intercept of the fit is then the tool's fixed cost, not the host's baseline
    base = out["host_baseline_mb"]
    for state in ("idle", "settled", "peak"):
        v = out.get(f"host_{state}_mb")
        out[f"host_delta_{state}_mb"] = (
            None if v is None or base is None else round(v - base, 1)
        )
    sessions = cell.get("sessions") or []
    for key in ("start_s", "load_s", "recheck_s"):
        vals = [float(s[key]) for s in sessions if s.get(key) is not None]
        out[f"{key}_p50"] = _median(vals)
        out[f"{key}_p95"] = _p95(vals)
    out["warm_hits"] = sum(1 for s in sessions if s.get("warm_hit"))
    return out


def _peak_rose(p0: List[Dict[str, Any]], p5: List[Dict[str, Any]]) -> Optional[bool]:
    """Did the cgroup's lifetime ``memory.peak`` rise during the cell?"""
    first = next(((r.get("cgroup") or {}).get("peak") for r in p0), None)
    last = next(((r.get("cgroup") or {}).get("peak") for r in reversed(p5)), None)
    if first is None or last is None:
        return None
    return bool(last > first)


def _mb(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v / MB, 1)


def _p95(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    xs = sorted(vals)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


# ------------------------------------------------------------- per series
def fit(points: List[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
    """Least squares (slope, intercept) of y against x; None with < 2 distinct x."""
    xs = [p[0] for p in points]
    if len(set(xs)) < 2:
        return None
    n = len(points)
    mx = sum(xs) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in points)
    slope = sxy / sxx
    return slope, my - slope * mx


def series_table(  # pylint: disable=too-many-locals
    rows: List[Dict[str, Any]], budget_gb: float = 100.0
) -> List[Dict[str, Any]]:
    """Per (tool, topology, heap): marginal MB per session and fixed MB from
    the settled (P4) and idle (P2) states, on PSS and on host used as a delta
    over each cell's own baseline (the intercept is then the tool's fixed
    cost, not the host's standing load); sessions
    per ``budget_gb`` on the host-used slope; the JVM's growth over the series.
    Refuses to mix scoped and unscoped IPS cells (review ac32954 S2)."""
    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        if r["outcome"] != "ok":
            continue
        groups.setdefault(
            (str(r["tool"]), str(r["topology"]), str(r["heap"])), []
        ).append(r)
    out: List[Dict[str, Any]] = []
    for (tool, topo, heap), rs in sorted(groups.items()):
        scopes = {bool(r.get("scope_used")) for r in rs}
        if tool == "ips" and len(scopes) > 1:
            raise ValueError(
                f"{tool}/{heap}: scoped and unscoped IPS cells in one series — analyse them apart"
            )
        smoke_only = any(r.get("external") for r in rs)  # review c17ec04 S1
        row: Dict[str, Any] = {
            "tool": tool,
            "topology": topo,
            "heap": heap,
            "cells": len(rs),
            "ks": sorted({r["k"] for r in rs}),
            "smoke_only": smoke_only,  # an external (long-lived) server: its history is in the deltas; no fit
        }
        for state in ("idle", "settled"):
            for metric, key in (
                ("pss", f"pss_{state}_mb"),
                ("host", f"host_delta_{state}_mb"),  # delta over the cell's P0 (S1)
            ):
                pts = [
                    (float(r["k"]), float(r[key])) for r in rs if r.get(key) is not None
                ]
                f = None if smoke_only else fit(pts)
                row[f"{metric}_{state}_marginal_mb"] = (
                    None if f is None else round(f[0], 1)
                )
                row[f"{metric}_{state}_fixed_mb"] = (
                    None if f is None else round(f[1], 1)
                )
        peak = [
            (float(r["k"]), float(r["pss_peak_mb"]))
            for r in rs
            if r.get("pss_peak_mb") is not None
        ]
        f = None if smoke_only else fit(peak)
        row["pss_peak_marginal_mb"] = None if f is None else round(f[0], 1)
        marg, fixed = row.get("host_settled_marginal_mb"), row.get(
            "host_settled_fixed_mb"
        )
        row[f"sessions_per_{int(budget_gb)}gb"] = (
            None
            if not marg or marg <= 0
            else int((budget_gb * 1024 - (fixed or 0)) // marg)
        )
        jvm = [
            (r["k"], r.get("jvm_settled_mb"))
            for r in rs
            if r.get("jvm_settled_mb") is not None
        ]
        row["jvm_settled_mb_by_k"] = {str(k): v for k, v in sorted(jvm)}
        for key in ("start_s", "load_s", "recheck_s"):
            vals = [
                float(r[f"{key}_p50"]) for r in rs if r.get(f"{key}_p50") is not None
            ]
            row[f"{key}_p50"] = _median(vals)
            row[f"{key}_p95"] = max(
                (float(r[f"{key}_p95"]) for r in rs if r.get(f"{key}_p95") is not None),
                default=None,
            )
        out.append(row)
    return out


# -------------------------------------------------------------- outputs
def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    """A CSV with the union of the rows' keys."""
    if not rows:
        return
    keys: List[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                    for k, v in r.items()
                }
            )


def plots(rows: List[Dict[str, Any]], out: Path) -> List[str]:
    """Memory vs k per heap, one line per tool/topology, for PSS settled,
    host settled and PSS peak. Silently skipped without matplotlib."""
    try:
        import matplotlib  # pylint: disable=import-outside-toplevel

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # pylint: disable=import-outside-toplevel
    except ImportError:
        print("plots skipped: matplotlib is not installed (pip install -e '.[eval]')")
        return []
    made: List[str] = []
    heaps = sorted({str(r["heap"]) for r in rows})
    for metric, title in (
        ("pss_settled_mb", "tool PSS after the loads"),
        (
            "host_delta_settled_mb",
            "host used after the loads (delta over the cell's baseline)",
        ),
        ("pss_peak_mb", "tool PSS peak (concurrent re-check)"),
    ):
        fig, axes = plt.subplots(
            1, len(heaps), figsize=(5 * len(heaps), 4), squeeze=False
        )
        for ax, heap in zip(axes[0], heaps):
            for label in sorted(
                {f"{r['tool']}/{r['topology']}" for r in rows if r["heap"] == heap}
            ):
                pts = sorted(
                    (r["k"], r[metric])
                    for r in rows
                    if r["heap"] == heap
                    and f"{r['tool']}/{r['topology']}" == label
                    and r.get(metric) is not None
                    and r["outcome"] == "ok"
                )
                if pts:
                    ax.plot(
                        [p[0] for p in pts],
                        [p[1] / 1024 for p in pts],
                        marker="o",
                        label=label,
                    )
            ax.set_title(f"{heap}: {title}")
            ax.set_xlabel("sessions k")
            ax.set_ylabel("GB")
            ax.grid(True, alpha=0.3)
            ax.legend()
        fig.tight_layout()
        path = out / f"{metric}.png"
        fig.savefig(path, dpi=120)
        plt.close(fig)
        made.append(str(path))
    return made


def snippet(
    series: List[Dict[str, Any]], cell_rows: List[Dict[str, Any]], budget_gb: float
) -> str:
    """The markdown block for the report section."""
    lines = ["# Memory scaling — results snippet (generated by memscale.analyze)", ""]
    lines.append(
        f"Cells: {len(cell_rows)} ({sum(1 for r in cell_rows if r['outcome'] == 'ok')} ok). Budget for the capacity column: {budget_gb:.0f} GB."
    )
    lines.append("")
    lines.append(
        "| tool | heap | k | marginal MB/session (host, settled) | fixed MB (host) | marginal MB/session (PSS, settled) | fixed MB (PSS) | peak marginal (PSS) | sessions per budget | start p50/p95 s | load p50/p95 s | re-check p50/p95 s |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for s in series:
        if s.get("smoke_only"):
            lines.append(
                f"| {s['tool']}/{s['topology']} | {s['heap']} | {','.join(map(str, s['ks']))} | smoke only (external server) | | | | | | | | |"
            )
            continue
        lines.append(
            f"| {s['tool']}/{s['topology']} | {s['heap']} | {','.join(map(str, s['ks']))} | {s.get('host_settled_marginal_mb')} | {s.get('host_settled_fixed_mb')} | "
            f"{s.get('pss_settled_marginal_mb')} | {s.get('pss_settled_fixed_mb')} | {s.get('pss_peak_marginal_mb')} | {s.get(f'sessions_per_{int(budget_gb)}gb')} | "
            f"{_f(s.get('start_s_p50'))}/{_f(s.get('start_s_p95'))} | {_f(s.get('load_s_p50'))}/{_f(s.get('load_s_p95'))} | {_f(s.get('recheck_s_p50'))}/{_f(s.get('recheck_s_p95'))} |"
        )
    lines.append("")
    lines.append(
        "JVM PSS after the loads, by k (MB) — the growth of the gateway / MCP JVM over a series:"
    )
    for s in series:
        lines.append(
            f"- {s['tool']}/{s['topology']} {s['heap']}: {s['jvm_settled_mb_by_k']}"
        )
    lines.append("")
    lines.append("Per-cell table: tables/cells.csv; plots: plots/*.png.")
    return "\n".join(lines) + "\n"


def _f(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.1f}"


def main(argv: Any = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    here = Path(__file__).resolve().parent.parent
    ap.add_argument("--results", default=str(here / "results"))
    ap.add_argument("--out", default=None, help="default: <results>/tables")
    ap.add_argument("--budget-gb", type=float, default=100.0)
    ap.add_argument("--tail-s", type=float, default=30.0)
    args = ap.parse_args(argv)
    results = Path(args.results)
    out = Path(args.out) if args.out else results / "tables"
    out.mkdir(parents=True, exist_ok=True)
    cells = load_cells(results)
    rows = [summarize_cell(c, args.tail_s) for c in cells]
    series = series_table(rows, args.budget_gb)
    write_csv(out / "cells.csv", rows)
    write_csv(out / "series.csv", series)
    plot_dir = results / "plots"
    plot_dir.mkdir(parents=True, exist_ok=True)
    made = plots(rows, plot_dir)
    text = snippet(series, rows, args.budget_gb)
    with open(results / "REPORT_SNIPPET.md", "w", encoding="utf-8") as fh:
        fh.write(text)
    print(
        f"{len(cells)} cells -> {out}/cells.csv, {out}/series.csv, {len(made)} plots, {results}/REPORT_SNIPPET.md"
    )
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
