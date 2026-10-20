"""The E1 matrix runner: every (tool, heap, k, rep) cell of config.yaml, one
after the other, resumable (a cell with a manifest whose outcome is not
"running" is skipped), ascending k per (tool, heap) series, with the series
stopped when the host's used memory passes the configured ceiling. Each cell
runs in a child process (``python -m memscale.run_cell``) so a hung tool
cannot take the runner down; a per-cell wall timeout ends it as ``error``.

    python -m memscale.run_matrix [--config config.yaml] [--out results]
        [--tools ips,pide-a,pide-b] [--heaps HOL,...] [--ks 1,2,4] [--reps 1]
        [--dry-run] [--cell-timeout-s 7200]

Budget rule (plan review S1): P0 is measured in full only for the first cell
of a (tool, heap) series; later cells use ``baseline_short_s``; cells with
k >= ``large_k`` get ``repeats_large_k`` repeats.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import yaml

from . import procs

HERE = Path(__file__).resolve().parent.parent
TOOL_DIR = {
    "ips": "ips",
    "pide-a": "pide-a",
    "pide-b": "pide-b",
}  # one spelling per tool (13c06cf S5)


def load_config(path: str) -> Dict[str, Any]:
    """config.yaml as a dict."""
    with open(path, "r", encoding="utf-8") as fh:
        return dict(yaml.safe_load(fh) or {})


def cells(
    cfg: Dict[str, Any],
    tools: List[str],
    heaps: Optional[List[str]] = None,
    ks: Optional[List[int]] = None,
    reps: Optional[int] = None,
) -> Iterator[Tuple[str, str, int, int]]:
    """(tool, heap, k, rep) in run order: per tool, per heap, ascending k,
    repeats innermost — so a series can stop early at the memory ceiling."""
    large_k = int(cfg.get("large_k", 16))
    base_reps = int(cfg.get("repeats", 3))
    large_reps = int(cfg.get("repeats_large_k", 1))
    for tool in tools:
        for heap, hc in cfg["heaps"].items():
            if heaps and heap not in heaps:
                continue
            for k in sorted(int(x) for x in hc["ks"]):
                if ks and k not in ks:
                    continue
                n = (
                    reps
                    if reps is not None
                    else (large_reps if k >= large_k else base_reps)
                )
                for rep in range(n):
                    yield tool, heap, k, rep


def manifest_of(
    out: Path, tool: str, heap: str, k: int, rep: int
) -> Optional[Dict[str, Any]]:
    """The cell's manifest if it exists."""
    path = out / TOOL_DIR[tool] / heap / f"k{k}" / f"rep{rep}" / "manifest.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return dict(json.load(fh))
    except (OSError, ValueError):
        return None


def host_used_gb() -> Optional[float]:
    """Host used memory in GB (None when unreadable)."""
    used = procs.host_memory().get("used")
    return None if used is None else used / 2**30


def run_one(  # pylint: disable=too-many-positional-arguments,too-many-arguments
    args: argparse.Namespace, tool: str, heap: str, k: int, rep: int, baseline_s: float
) -> str:
    """Run one cell in a child process; returns its outcome string."""
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "memscale.run_cell",
        "--tool",
        tool,
        "--heap",
        heap,
        "--k",
        str(k),
        "--rep",
        str(rep),
        "--config",
        args.config,
        "--out",
        args.out,
        "--baseline-s",
        str(baseline_s),
        "--force",
    ]
    log_dir = Path(args.out) / "runner-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{tool}-{heap}-k{k}-rep{rep}.log"
    t0 = time.time()
    with open(log_path, "ab") as log:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(HERE),
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=args.cell_timeout_s,
                check=False,
            )
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = -1
    m = manifest_of(Path(args.out), tool, heap, k, rep) or {}
    outcome = str(m.get("outcome") or ("timeout" if rc == -1 else "error"))
    print(
        f"[{time.strftime('%H:%M:%S')}] {tool} {heap} k={k} rep={rep}: {outcome} "
        f"({time.time() - t0:.0f}s, rc={rc}, log {log_path})",
        flush=True,
    )
    return outcome


def main(argv: Any = None) -> int:  # pylint: disable=too-many-locals
    """CLI entry point."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--tools", default="ips,pide-a,pide-b")
    ap.add_argument("--heaps", default=None)
    ap.add_argument("--ks", default=None)
    ap.add_argument("--reps", type=int, default=None)
    ap.add_argument("--cell-timeout-s", type=float, default=7200.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    heaps = [h.strip() for h in args.heaps.split(",")] if args.heaps else None
    ks = [int(x) for x in args.ks.split(",")] if args.ks else None
    ceiling = float(cfg.get("stop_when_host_used_gb_exceeds", 110))
    baseline_full = float(cfg["phases"]["baseline_s"])
    baseline_short = float(cfg["phases"].get("baseline_short_s", 10))
    plan = list(cells(cfg, tools, heaps, ks, args.reps))
    print(f"{len(plan)} cells planned", flush=True)
    done = skipped = failed = 0
    stopped_series: Set[Tuple[str, str]] = set()
    seen_series: Set[Tuple[str, str]] = set()
    for tool, heap, k, rep in plan:
        series = (tool, heap)
        m = manifest_of(Path(args.out), tool, heap, k, rep)
        if m and m.get("outcome") not in (None, "running"):
            skipped += 1
            continue
        if series in stopped_series:
            print(
                f"  skip {tool} {heap} k={k} rep={rep}: series stopped at the memory ceiling",
                flush=True,
            )
            continue
        used = host_used_gb()
        if used is not None and used > ceiling:
            print(
                f"  host used {used:.1f} GB > {ceiling} GB before {tool} {heap} k={k}: series stopped",
                flush=True,
            )
            stopped_series.add(series)
            continue
        baseline_s = baseline_full if series not in seen_series else baseline_short
        seen_series.add(series)
        if args.dry_run:
            print(f"  would run {tool} {heap} k={k} rep={rep} (baseline {baseline_s}s)")
            continue
        outcome = run_one(args, tool, heap, k, rep, baseline_s)
        if outcome == "ok":
            done += 1
        else:
            failed += 1
            if outcome in ("timeout", "error"):
                # a tool that hung or died at this k will not do better at a larger k
                stopped_series.add(series)
    print(
        f"done {done}, failed/partial {failed}, skipped (already run) {skipped}",
        flush=True,
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
