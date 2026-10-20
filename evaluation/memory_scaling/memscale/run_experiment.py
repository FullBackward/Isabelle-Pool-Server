# pylint: disable=duplicate-code
"""E2 / E3 / E4 from the command line:

    python -m memscale.run_experiment --exp warm     --tool ips    --heap HOL --k 4
    python -m memscale.run_experiment --exp pressure --tool ips    --heap HOL-Analysis --memory-max 48G
    python -m memscale.run_experiment --exp pressure --tool pide-b --heap HOL-Analysis   # run it under
        systemd-run --user --scope -p MemoryMax=48G ... yourself (12_run_e3_pressure.sh does)
    python -m memscale.run_experiment --exp kill     --tool pide-a --heap HOL --k 8

Outputs go to <out>/<exp>-<tool>-<heap>/.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from . import procs
from .experiments import kill_probe, pressure, warm_hit
from .run_cell import HERE, REPO_ROOT, load_config, make_driver
from .workload import load_workload


async def amain(args: argparse.Namespace) -> int:
    """Build the driver and run the experiment."""
    cfg = load_config(args.config)
    heap_cfg = cfg["heaps"][args.heap]
    workload = load_workload(
        args.heap, heap_cfg["workload"], cfg["trailing_lemma"], repo_root=HERE
    )
    driver: Any = make_driver(args, cfg, list(workload.imports))
    out = Path(args.out) / f"{args.exp}-{args.tool}-{args.heap}"
    patterns = list(
        cfg["tools"]
        .get(args.tool if args.tool == "ips" else "pide_mcp", {})
        .get("root_patterns")
        or []
    )
    if args.exp == "warm":
        if args.tool != "ips":
            raise SystemExit(
                "warm-hit latency is an IPS measurement (PIDE MCP has no pool)"
            )
        body = await warm_hit(driver, workload, args.k, out, rounds=args.rounds)
        print(
            f"cold p50 {body['cold_s_p50']} s, warm p50 {body['warm_s_p50']} s, reused_all={body['reused_all']} -> {out}"
        )
    elif args.exp == "pressure":
        body = await pressure(
            driver,
            workload,
            out,
            max_sessions=args.max_sessions,
            patterns=patterns,
            cgroup_dir=args.cgroup_dir or procs.own_cgroup_dir(),
            recover_wait_s=args.recover_wait_s,
        )
        print(
            f"stop: {body.get('stop_reason')} after {body.get('sessions_before_stop')} sessions; "
            f"recovery {body.get('recovery_s')} s; oom_kill {body.get('cgroup_at_end', {}).get('oom_kill')} -> {out}"
        )
    else:
        body = await kill_probe(driver, workload, args.k, out, answer_s=args.answer_s)
        print(
            f"victim {body.get('victim_pid')} others_answered={body.get('others_answered')} "
            f"processes_alive={body.get('processes_alive')} -> {out}"
        )
    return 0 if body.get("outcome") == "ok" else 1


def main(argv: Any = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--exp", required=True, choices=["warm", "pressure", "kill"])
    ap.add_argument("--tool", required=True, choices=["ips", "pide-a", "pide-b"])
    ap.add_argument("--heap", required=True)
    ap.add_argument("--k", type=int, default=4)
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--max-sessions", type=int, default=64)
    ap.add_argument("--answer-s", type=float, default=30.0)
    ap.add_argument("--recover-wait-s", type=float, default=300.0)
    ap.add_argument(
        "--memory-max", default=None, help="IPS: MemoryMax of its scope (E3)"
    )
    ap.add_argument(
        "--cgroup-dir",
        default=None,
        help="the limit's cgroup to sample (PIDE under a caller's scope)",
    )
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--ips-url", default=None)
    ap.add_argument("--ips-external", action="store_true")
    args = ap.parse_args(argv)
    os.chdir(REPO_ROOT)
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
