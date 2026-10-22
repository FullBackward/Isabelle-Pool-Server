"""The T1/T2 matrix: every (W, rep) cell of config.yaml in ascending W, each in
a child process (``python -m trainmetrics.run_t1``) with a wall timeout,
resumable (a cell whose manifest is finished is skipped; ``--rerun-partial``
re-runs error/partial cells), one runner per results tree (``RUNNER_LOCK``, as
in memscale).

    python -m trainmetrics.run_matrix [--workers 1,2,4] [--reps 1] [--local-smoke] [--ips-external] [--dry-run]
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
from .harness import cell_arg_parser
from .procctl import cgroup_busy, group_alive, group_tree, kill_group, kill_server
from .run_t1 import cell_dir


def cells(
    cfg: Dict[str, Any],
    workers: Optional[List[int]] = None,
    reps: Optional[int] = None,
) -> Iterator[Tuple[int, int]]:
    """(W, rep) in run order: ascending W, repeats innermost."""
    ws = workers or [int(w) for w in cfg["workers"]]
    n = reps if reps is not None else int(cfg.get("repeats", 3))
    for w in sorted(ws):
        for rep in range(n):
            yield w, rep


def manifest_of(out: Path, w: int, rep: int) -> Optional[Dict[str, Any]]:
    """The cell's manifest if present and readable."""
    path = cell_dir(out, w, rep) / "manifest.json"
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
    """The run_t1 command line for one cell."""
    cmd = [
        sys.executable,
        "-u",
        "-m",
        "trainmetrics.run_t1",
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
        ("--ips-external", args.ips_external),
    ):
        if on:
            cmd.append(flag)
    if args.url:
        cmd += ["--url", args.url]
    if args.quota:
        cmd += ["--quota", str(args.quota)]
    return cmd


def run_one(args: argparse.Namespace, w: int, rep: int) -> str:
    """One cell in a child process; its outcome string."""
    log_dir = Path(args.out) / "runner-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"t1-w{w}-rep{rep}.log"
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
                (manifest_of(Path(args.out), w, rep) or {}).get("server") or {}
            ).get("pgid")
            snapshot = group_tree(int(pgid)) if pgid else set()
            kill_group(
                proc.pid, grace_s=args.term_grace_s
            )  # SIGTERM: the cell stops its server
            proc.wait()
    m = manifest_of(Path(args.out), w, rep) or {}
    srv = m.get("server") or {}
    if srv.get("pgid") and not srv.get("external"):
        pgid = int(srv["pgid"])
        if not snapshot:
            snapshot = group_tree(pgid)
        if group_alive(pgid) or snapshot or cgroup_busy(srv.get("cgroup_dir")):
            # the cell died before its finally (SIGKILL, OOM), or its server did
            # not stop: never let the next cell meet it (port, memory)
            done = kill_server(pgid, snapshot, srv.get("cgroup_dir"), args.term_grace_s)
            print(f"  stopped the leftover server of W={w} rep={rep}: {done}")
    outcome = str(m.get("outcome") or ("timeout" if rc == -1 else "error"))
    if rc == -1:
        outcome = "timeout"
    print(
        f"[{time.strftime('%H:%M:%S')}] t1 W={w} rep={rep}: {outcome} "
        f"({time.monotonic() - t0:.0f}s, rc={rc}, log {log})",
        flush=True,
    )
    return outcome


def run_plan(args: argparse.Namespace, plan: List[Tuple[int, int]]) -> int:
    """The cells in order, with resume; returns the number not ok."""
    done = skipped = failed = 0
    for w, rep in plan:
        m = manifest_of(Path(args.out), w, rep)
        if m and m.get("outcome") == "ok":
            skipped += 1
            continue
        if m and m.get("outcome") not in (None, "running") and not args.rerun_partial:
            skipped += 1
            continue
        if args.dry_run:
            print(f"  would run t1 W={w} rep={rep}")
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
    ap.add_argument("--workers", default="", help="comma-separated W (config)")
    ap.add_argument("--reps", type=int, default=None)
    ap.add_argument("--quota", type=int, default=0)
    ap.add_argument("--cell-timeout-s", type=float, default=7200.0)
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
    ws = [int(x) for x in args.workers.split(",") if x] or None
    plan = list(cells(cfg, ws, args.reps))
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
