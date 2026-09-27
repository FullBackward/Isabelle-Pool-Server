#!/usr/bin/env python
"""Consolidate raw benchmark run files into one recomputable summary.

Input (any of these that exist under --root):
  runs/<family>/[<config>/]<tool>_results_run_<N>.json   (eval_*.py outputs)
  mcp_bench_results*.json                                (legacy MCP bench)

Output:
  results/benchmark_runs.json   one record per run with per-theory rows and
                                the bare per-step timing vector (no command
                                text / subgoals / previews — that is the 70 %
                                of the raw bytes nobody recomputes from)
  results/benchmark_runs.csv    the per-theory rows flattened, one line each,
                                for spreadsheet users

Everything in the old analysis_exports/*.csv (per-run manifest, family
summaries, p95 step latency) is derivable from the JSON: run-level totals are
kept verbatim under "recorded", per-theory scalars are kept in full, and the
step timing vector allows any per-step distribution to be recomputed.

Usage:
  python -m evaluation.scripts.consolidate_runs --root evaluation \
      [--out evaluation/results] [--check]
--check recomputes a few run-level numbers from the rows and reports drift.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_RUN_RE = re.compile(r"_run_(\d+)\.json$")
_CONFIG_RE = re.compile(r"wo(?:r)?ker_(\d+)_reuse_(true|false)(?:_(.+))?$")  # tolerates the 'woker' typo

#: run-level scalars that are configuration rather than results
_CONFIG_KEYS = {
    "tool", "benchmark_kind", "corpus", "repo_root", "server", "field", "session_name",
    "parent_session", "jobs", "timeout_sec", "num_workers", "per_transition_timeout_sec",
    "execute_timeout_sec", "sledgehammer_ready", "master_dir", "only_import_from_session_heap",
}

#: per-theory fields kept verbatim (everything except the bulky per-step list and
#: the long stdout/stderr tails; `error` is kept only for failed theories)
_THEORY_DROP = {"steps", "stdout_tail", "stderr_tail", "response", "last_recorded_preview"}


def _family_and_config(path: Path, root: Path) -> tuple[str, Optional[str], Dict[str, Any]]:
    rel = path.relative_to(root)
    parts = rel.parts
    if parts[0] != "runs":
        return "mcp_bench_legacy", None, {}
    family = parts[1]
    config = parts[2] if len(parts) > 3 else None
    cfg: Dict[str, Any] = {}
    if config:
        m = _CONFIG_RE.match(config)
        if m:
            cfg = {"workers": int(m.group(1)), "reuse": m.group(2) == "true"}
            if m.group(3):
                cfg["field_override"] = m.group(3)
    return family, config, cfg


def _run_id(path: Path) -> Optional[int]:
    m = _RUN_RE.search(path.name)
    return int(m.group(1)) if m else None


def _theory_row(entry: Dict[str, Any]) -> Dict[str, Any]:
    row = {k: v for k, v in entry.items() if k not in _THEORY_DROP}
    if row.get("ok") and "error" in row and not row["error"]:
        row.pop("error", None)
    steps = entry.get("steps")
    if isinstance(steps, list) and steps:
        vec = []
        for s in steps:
            v = s.get("elapsed_sec")
            vec.append(round(float(v), 6) if isinstance(v, (int, float)) else None)
        row["step_elapsed_sec"] = vec
        api = [s.get("execution_time_sec") for s in steps]
        if any(isinstance(a, (int, float)) for a in api):
            row["step_api_execution_sec"] = [
                round(float(a), 6) if isinstance(a, (int, float)) else None for a in api
            ]
        row["step_accepted"] = [bool(s.get("accepted")) for s in steps]
    return row


def _consolidate_eval_run(path: Path, root: Path) -> Dict[str, Any]:
    d = json.loads(path.read_text(encoding="utf-8"))
    family, config, cfg = _family_and_config(path, root)
    recorded = {k: v for k, v in d.items() if not isinstance(v, (list, dict))}
    config_block = {k: recorded.pop(k) for k in list(recorded) if k in _CONFIG_KEYS}
    config_block.update(cfg)
    stats_blocks = {k: v for k, v in d.items() if isinstance(v, dict) and k != "environment"}
    return {
        "family": family,
        "config": config,
        "run_id": _run_id(path),
        "source_file": str(path.relative_to(root)).replace("\\", "/"),
        "config_params": config_block,
        "environment": d.get("environment"),
        "recorded": recorded,          # run-level totals/means as the script wrote them
        "recorded_stats": stats_blocks,  # the script's own distribution blocks
        "theories": [_theory_row(e) for e in d.get("results", [])],
    }


def _consolidate_mcp_bench(path: Path, root: Path) -> Dict[str, Any]:
    d = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for r in d.get("results", []):
        row = {k: v for k, v in r.items() if k not in {"proof", "source", "statement"}}
        row["proof_chars"] = len(r.get("proof") or "")
        rows.append(row)
    return {
        "family": "mcp_bench_legacy",
        "config": path.stem.replace("mcp_bench_results", "").strip("_") or "default",
        "run_id": None,
        "source_file": str(path.relative_to(root)).replace("\\", "/"),
        "config_params": {"model": d.get("model"), "runs": d.get("runs")},
        "environment": None,
        "recorded": {k: v for k, v in d.items() if not isinstance(v, (list, dict))},
        "recorded_stats": {},
        "theories": rows,
    }


def collect(root: Path) -> List[Dict[str, Any]]:
    runs: List[Dict[str, Any]] = []
    for p in sorted((root / "runs").rglob("*.json")) if (root / "runs").is_dir() else []:
        runs.append(_consolidate_eval_run(p, root))
    for p in sorted(root.glob("mcp_bench_results*.json")):
        runs.append(_consolidate_mcp_bench(p, root))
    return runs


# ----------------------------------------------------------------- checks

def _pct(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def check(runs: Iterable[Dict[str, Any]]) -> int:
    """Recompute a handful of recorded run-level numbers from the rows."""
    worst = 0.0
    n = 0
    for r in runs:
        th = r["theories"]
        rec = r["recorded"]
        if not th or "mean_wall_time_sec_per_file" not in rec:
            continue
        walls = [t["wall_time_sec"] for t in th if isinstance(t.get("wall_time_sec"), (int, float))]
        pairs = [("mean_wall_time_sec_per_file", statistics.mean(walls)),
                 ("median_wall_time_sec_per_file", statistics.median(walls)),
                 ("successes", sum(1 for t in th if t.get("ok")))]
        steps = [v for t in th for v in t.get("step_elapsed_sec", []) if v is not None]
        if steps and "mean_step_elapsed_sec" in rec:
            pairs.append(("mean_step_elapsed_sec", statistics.mean(steps)))
        for key, mine in pairs:
            theirs = rec.get(key)
            if isinstance(theirs, (int, float)):
                drift = abs(mine - theirs) / (abs(theirs) or 1.0)
                worst = max(worst, drift)
                n += 1
                if drift > 1e-6:
                    print(f"  drift {drift:.2e} on {r['source_file']} {key}: rows={mine} recorded={theirs}")
    print(f"check: {n} recorded values recomputed from rows, worst relative drift {worst:.2e}")
    return 0 if worst < 1e-6 else 1


# ----------------------------------------------------------------- output

_CSV_FIELDS = [
    "family", "config", "run_id", "workers", "reuse", "tool", "corpus", "field",
    "theory_name", "file", "ok", "wall_time_sec", "startup_sec", "api_execution_time_sec",
    "client_overhead_sec", "accepted_steps", "total_steps", "expected_steps",
    "reused_session", "worker_id", "child_cpu_total_sec", "return_code", "http_status",
    "n_steps", "step_elapsed_mean_sec", "step_elapsed_p95_sec", "error",
]


def write_outputs(runs: List[Dict[str, Any]], out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "isabellegym.benchmark_runs.v1",
        "description": (
            "Consolidated benchmark runs. `recorded` holds each run's own totals; "
            "`theories[]` holds every per-theory scalar the run script wrote plus the "
            "per-step timing vector `step_elapsed_sec` (command text/subgoals dropped). "
            "All summary tables are recomputable from `theories[]`."
        ),
        "families": sorted({r["family"] for r in runs}),
        "n_runs": len(runs),
        "runs": runs,
    }
    (out / "benchmark_runs.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
    with (out / "benchmark_runs.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in runs:
            cp = r["config_params"]
            base = {
                "family": r["family"], "config": r["config"], "run_id": r["run_id"],
                "workers": cp.get("workers", cp.get("num_workers")), "reuse": cp.get("reuse"),
                "tool": cp.get("tool"), "corpus": cp.get("corpus"),
                "field": cp.get("field_override", cp.get("field", cp.get("parent_session"))),
            }
            for t in r["theories"]:
                row = dict(base)
                row.update({k: t.get(k) for k in _CSV_FIELDS if k in t})
                vec = [v for v in t.get("step_elapsed_sec", []) if v is not None]
                row["n_steps"] = len(vec) or t.get("total_steps")
                row["step_elapsed_mean_sec"] = round(statistics.mean(vec), 6) if vec else None
                row["step_elapsed_p95_sec"] = round(_pct(vec, 0.95), 6) if vec else None
                row["theory_name"] = t.get("theory_name", t.get("name"))
                w.writerow(row)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=Path("evaluation"))
    ap.add_argument("--out", type=Path, default=None, help="default: <root>/results")
    ap.add_argument("--check", action="store_true", help="recompute recorded values from rows")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    out = (args.out or root / "results").resolve()
    runs = collect(root)
    if not runs:
        print(f"no run files under {root}", file=sys.stderr)
        return 2
    write_outputs(runs, out)
    by_family: Dict[str, int] = {}
    for r in runs:
        by_family[r["family"]] = by_family.get(r["family"], 0) + 1
    print(f"wrote {out / 'benchmark_runs.json'} and .csv: {len(runs)} runs, "
          f"{sum(len(r['theories']) for r in runs)} theory rows; per family: {by_family}")
    return check(runs) if args.check else 0


if __name__ == "__main__":
    sys.exit(main())
