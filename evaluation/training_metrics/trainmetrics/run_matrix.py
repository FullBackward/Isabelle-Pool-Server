"""The cell matrix of an experiment: every (W, rep) cell of config.yaml in
ascending W, each in a child process with a wall timeout, resumable (a cell
whose manifest is finished is skipped; ``--rerun-partial`` re-runs
error/partial cells), one runner per results tree (``RUNNER_LOCK``, as in
memscale). ``--experiment t1`` (default: T1/T2, ``python -m
trainmetrics.run_t1``, config ``workers`` / ``repeats``) or ``t6a`` (``python -m
trainmetrics.run_t6 --style t6a``, config ``t6.workers`` / ``t6.repeats`` and
``t6.t6a.cell_timeout_s``) or ``t6b`` (the same with ``--style t6b``);
``--quota`` is then the files per worker; ``pide`` (T1's PIDE MCP row,
``python -m trainmetrics.drive_pide_chunks``, config ``pide.workers`` /
``pide.repeats``).

    python -m trainmetrics.run_matrix [--experiment t6a] [--workers 1,2,4] [--reps 1] [--local-smoke] [--ips-external] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from .census import load_config
from .drive_pide_chunks import cell_dir as pide_cell_dir
from .harness import cell_arg_parser
from .procctl import cgroup_busy, group_alive, group_tree, kill_group, kill_server
from .run_t1 import cell_dir
from .run_t6 import STYLES
from .run_t6 import cell_dir as t6_cell_dir

EXPERIMENTS = ("t1", "pide") + STYLES


def _section(cfg: Dict[str, Any], experiment: str) -> Dict[str, Any]:
    """Where the experiment's W list and repeats live in the config."""
    if experiment in ("t1", "pide"):
        return cfg if experiment == "t1" else dict(cfg.get("pide") or {})
    return dict(cfg.get("t6") or {})


def cell_timeout_s(cfg: Dict[str, Any], experiment: str) -> float:
    """The experiment's cell timeout: T1 7200 s; a T6 style its own
    ``t6.<style>.cell_timeout_s``, else ``t6.cell_timeout_s``, else 7200 s."""
    if experiment in ("t1", "pide"):
        return 7200.0
    t6 = dict(cfg.get("t6") or {})
    style = dict(t6.get(experiment) or {})
    return float(style.get("cell_timeout_s") or t6.get("cell_timeout_s") or 7200)


def cells(
    cfg: Dict[str, Any],
    workers: Optional[List[int]] = None,
    reps: Optional[int] = None,
    experiment: str = "t1",
) -> Iterator[Tuple[int, int]]:
    """(W, rep) in run order: ascending W, repeats innermost."""
    sec = _section(cfg, experiment)
    ws = workers or [int(w) for w in sec["workers"]]
    n = reps if reps is not None else int(sec.get("repeats", 3))
    for w in sorted(ws):
        for rep in range(n):
            yield w, rep


def cell_path(out: Path, w: int, rep: int, experiment: str = "t1") -> Path:
    """The experiment's cell directory."""
    if experiment == "t1":
        return cell_dir(out, w, rep)
    if experiment == "pide":
        return pide_cell_dir(out, w, rep)
    return t6_cell_dir(out, experiment, w, rep)


def manifest_of(
    out: Path, w: int, rep: int, experiment: str = "t1"
) -> Optional[Dict[str, Any]]:
    """The cell's manifest if present and readable."""
    path = cell_path(out, w, rep, experiment) / "manifest.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return dict(json.load(fh))
    except (OSError, ValueError):
        return None


def running_runner(lock: Path) -> Optional[int]:
    """The pid in ``lock`` if that process is alive; None for no or stale lock."""
    try:
        pid = int(lock.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def child_cmd(args: argparse.Namespace, w: int, rep: int) -> List[str]:
    """The cell's command line (run_t1, or run_t6 with its style)."""
    experiment = getattr(args, "experiment", "t1")
    module = ["trainmetrics.run_t1"]
    if experiment == "pide":
        module = ["trainmetrics.drive_pide_chunks"]
    elif experiment != "t1":
        module = ["trainmetrics.run_t6", "--style", experiment]
    cmd = [
        sys.executable,
        "-u",
        "-m",
        *module,
        "--config",
        args.config,
        "--workers",
        str(w),
        "--rep",
        str(rep),
        "--census",
        args.census,
        "--out",
        args.out,
    ]
    for flag, on in (
        ("--local-smoke", args.local_smoke),
        ("--ips-external", args.ips_external and experiment != "pide"),
    ):
        if on:
            cmd.append(flag)
    if args.url and experiment != "pide":  # the PIDE row starts its own MCP process
        cmd += ["--url", args.url]
    if args.quota:
        cmd += [
            "--quota" if experiment in ("t1", "pide") else "--files-per-worker",
            str(args.quota),
        ]
    return cmd


def _stop_leftover(
    srv: Dict[str, Any], snapshot: Set[int], grace_s: float, label: str
) -> None:
    """Kill the cell's own server if it outlived the cell: the cell died before
    its finally (SIGKILL, OOM), or its server did not stop; the next cell must
    never meet it (port, memory)."""
    if not srv.get("pgid") or srv.get("external"):
        return
    pgid = int(srv["pgid"])
    tree = snapshot or group_tree(pgid)
    if group_alive(pgid) or tree or cgroup_busy(srv.get("cgroup_dir")):
        done = kill_server(pgid, tree, srv.get("cgroup_dir"), grace_s)
        print(f"  stopped the leftover server of {label}: {done}")


def run_one(args: argparse.Namespace, w: int, rep: int) -> str:
    """One cell in a child process; its outcome string."""
    experiment = getattr(args, "experiment", "t1")
    log_dir = Path(args.out) / "runner-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"{experiment}-w{w}-rep{rep}.log"
    t0 = time.monotonic()
    snapshot: Set[int] = set()
    with open(log, "w", encoding="utf-8") as fh:
        # its own process group, so a timeout stops the cell AND what it started
        # (review 830f9ac B1: subprocess.run(timeout=...) kills the child only)
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            child_cmd(args, w, rep),
            stdout=fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            rc = proc.wait(timeout=args.cell_timeout_s)
        except subprocess.TimeoutExpired:
            rc = -1
            # the server's whole tree (gateway JVM, provers: other groups) while
            # it is intact, before the cell is stopped (review 5141144 S1)
            pgid = (
                (manifest_of(Path(args.out), w, rep, experiment) or {}).get("server")
                or {}
            ).get("pgid")
            snapshot = group_tree(int(pgid)) if pgid else set()
            kill_group(
                proc.pid, grace_s=args.term_grace_s
            )  # SIGTERM: the cell stops its server
            proc.wait()
    m = manifest_of(Path(args.out), w, rep, experiment) or {}
    _stop_leftover(
        m.get("server") or {}, snapshot, args.term_grace_s, f"W={w} rep={rep}"
    )
    outcome = str(m.get("outcome") or ("timeout" if rc == -1 else "error"))
    if rc == -1:
        outcome = "timeout"
    print(
        f"[{time.strftime('%H:%M:%S')}] {experiment} W={w} rep={rep}: {outcome} "
        f"({time.monotonic() - t0:.0f}s, rc={rc}, log {log})",
        flush=True,
    )
    return outcome


def run_plan(args: argparse.Namespace, plan: List[Tuple[int, int]]) -> int:
    """The cells in order, with resume; returns the number not ok."""
    done = skipped = failed = 0
    experiment = getattr(args, "experiment", "t1")
    for w, rep in plan:
        m = manifest_of(Path(args.out), w, rep, experiment)
        if m and m.get("outcome") == "ok":
            skipped += 1
            continue
        if m and m.get("outcome") not in (None, "running") and not args.rerun_partial:
            skipped += 1
            continue
        if args.dry_run:
            print(
                f"  would run {experiment} W={w} rep={rep}: {' '.join(child_cmd(args, w, rep))}"
            )
            continue
        if run_one(args, w, rep) == "ok":
            done += 1
        else:
            failed += 1
    print(f"done {done}, failed {failed}, skipped (already run) {skipped}", flush=True)
    return failed


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    ap = cell_arg_parser(__doc__)
    ap.add_argument("--experiment", choices=EXPERIMENTS, default="t1")
    ap.add_argument("--workers", default="", help="comma-separated W (config)")
    ap.add_argument("--reps", type=int, default=None)
    ap.add_argument("--quota", type=int, default=0)
    ap.add_argument(
        "--cell-timeout-s",
        type=float,
        default=None,
        help="default 7200; T6: config t6.<style>.cell_timeout_s (trainmetrics.calibrate_t6)",
    )
    ap.add_argument(
        "--term-grace-s",
        type=float,
        default=90.0,
        help="after a timeout: SIGTERM, this long for the cell to stop its server, then SIGKILL",
    )
    ap.add_argument("--rerun-partial", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.cell_timeout_s is None:
        args.cell_timeout_s = cell_timeout_s(cfg, args.experiment)
    ws = [int(x) for x in args.workers.split(",") if x] or None
    plan = list(cells(cfg, ws, args.reps, args.experiment))
    print(f"{len(plan)} cells planned", flush=True)
    lock = Path(args.out) / "RUNNER_LOCK"
    if not args.dry_run:
        other = running_runner(lock)
        if other is not None:
            print(
                f"another runner (pid {other}) holds {lock}; one runner per results tree"
            )
            return 2
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(f"{os.getpid()} {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    try:
        return 0 if run_plan(args, plan) == 0 else 1
    finally:
        if not args.dry_run:
            lock.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
