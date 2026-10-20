"""Run ONE cell from the command line (the matrix runner calls this per cell;
the smoke script calls it directly):

    python -m memscale.run_cell --tool ips --heap HOL --k 2 --rep 0 \
        [--config config.yaml] [--out results] [--baseline-s 60] [--idle-s 60]
        [--ips-url http://127.0.0.1:8000 --ips-external]

Writes ``<out>/<tool>/<heap>/k<k>/rep<rep>/{samples.jsonl,manifest.json}``.
Run it from the repo root (the IPS server is started with cwd = repo root).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .drive_ips import IPSDriver
from .phases import Cell, Driver, environment_block, run_cell
from .sampler import Sampler
from .workload import load_workload

HERE = Path(__file__).resolve().parent.parent  # evaluation/memory_scaling
REPO_ROOT = HERE.parent.parent


def load_config(path: str) -> Dict[str, Any]:
    """config.yaml as a dict."""
    with open(path, "r", encoding="utf-8") as fh:
        return dict(yaml.safe_load(fh) or {})


def make_driver(
    args: argparse.Namespace, cfg: Dict[str, Any], imports: List[str]
) -> Driver:
    """The driver for ``--tool``; PIDE MCP drivers are registered by stage 3."""
    if args.tool == "ips":
        ips = cfg["tools"]["ips"]
        return IPSDriver(
            heap=args.heap,
            imports=imports,
            url=args.ips_url or ips.get("url", "http://127.0.0.1:8000"),
            external=bool(args.ips_external or ips.get("external", False)),
            start_cmd=list(ips.get("start_cmd", ["python", "-m", "server.app.main"])),
            env={k: str(v) for k, v in (ips.get("env") or {}).items()},
            ready_timeout_s=float(ips.get("ready_timeout_s", 300)),
            load_timeout_s=float(cfg["phases"].get("load_timeout_s", 900)),
            repo_root=str(REPO_ROOT),
            scope=str(ips.get("scope", "auto")),
            memory_max=args.memory_max,
        )
    try:
        from .drive_pide import (  # pylint: disable=import-outside-toplevel
            make_pide_driver,
        )
    except ImportError as exc:  # stage 3 adds it
        raise SystemExit(f"tool {args.tool!r} not available: {exc}") from exc
    driver: Driver = make_pide_driver(args, cfg, imports)
    return driver


def out_dir_for(args: argparse.Namespace, topology: str) -> Path:
    """``<out>/<tool>/<heap>/k<k>/rep<rep>`` — the tool name carries its topology
    (ips, pide-a, pide-b), one spelling everywhere (review 13c06cf S5)."""
    del topology
    out: Path = (
        Path(str(args.out))
        / str(args.tool)
        / str(args.heap)
        / f"k{args.k}"
        / f"rep{args.rep}"
    )
    return out


async def amain(args: argparse.Namespace) -> int:
    """Build everything for one cell and run it."""
    cfg = load_config(args.config)
    heap_cfg = cfg["heaps"][args.heap]
    workload = load_workload(
        args.heap, heap_cfg["workload"], cfg["trailing_lemma"], repo_root=HERE
    )
    driver = make_driver(args, cfg, list(workload.imports))
    out = out_dir_for(args, driver.topology)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    if manifest_path.exists() and not args.force:
        print(f"cell already has a manifest: {manifest_path} (use --force to redo)")
        return 0
    samples = out / "samples.jsonl"
    if samples.exists():
        samples.unlink()
    patterns = list(cfg["tools"].get(args.tool, {}).get("root_patterns") or [])
    cell = Cell(
        tool=args.tool,
        topology=driver.topology,
        heap=args.heap,
        k=args.k,
        rep=args.rep,
        out_dir=str(out),
        workload=workload.as_manifest(),
        config={"phases": cfg["phases"], "patterns": patterns},
        environment={
            **environment_block(cfg.get("isabelle", {}).get("home")),
            # review 2e1935e S5 / plan review S6: the IPS memory env E1 runs under
            "ips_memory_env": {  # what the server got: config env OVER os.environ (c209e6c S4)
                k: (cfg["tools"]["ips"].get("env") or {}).get(
                    k, os.environ.get(k, "default")
                )
                for k in (
                    "ISABELLE_ENABLE_MEMORY_MANAGEMENT",
                    "ISABELLE_MEMORY_PRESSURE_THRESHOLD",
                    "ISABELLE_MEMORY_WARNING_THRESHOLD",
                    "ISABELLE_MEMORY_MIN_AVAILABLE_MB",
                    "ISABELLE_ML_MAXHEAP_MB",
                    "ISABELLE_POOL_SIZE",
                )
            },
        },
    )
    sampler = Sampler(
        str(samples),
        patterns=patterns,
        interval_s=float(cfg["phases"].get("sample_interval_s", 1.0)),
    )
    baseline = (
        args.baseline_s
        if args.baseline_s is not None
        else float(cfg["phases"]["baseline_s"])
    )
    idle = args.idle_s if args.idle_s is not None else float(cfg["phases"]["idle_s"])
    with sampler:
        cell = await run_cell(
            cell, driver, workload, sampler, baseline_s=baseline, idle_s=idle
        )
    print(f"cell outcome={cell.outcome} error={cell.error} -> {manifest_path}")
    return 0 if cell.outcome == "ok" else 1


def main(argv: Any = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--tool", required=True, choices=["ips", "pide-a", "pide-b"])
    ap.add_argument("--heap", required=True)
    ap.add_argument("--k", type=int, required=True)
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--baseline-s", type=float, default=None)
    ap.add_argument("--idle-s", type=float, default=None)
    ap.add_argument("--ips-url", default=None)
    ap.add_argument("--ips-external", action="store_true")
    ap.add_argument(
        "--memory-max", default=None, help="E3: MemoryMax of the IPS scope, e.g. 48G"
    )
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    os.chdir(REPO_ROOT)
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
