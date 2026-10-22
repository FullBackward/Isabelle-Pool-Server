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
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from . import EXPERIMENT_DIR
from .census import load_config
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


def _group_states(pgid: int) -> Optional[List[str]]:
    """States of the processes in process group ``pgid`` from ``/proc`` (None
    where there is no ``/proc``, e.g. macOS)."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    states: List[str] = []
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "r", encoding="utf-8") as fh:
                fields = fh.read().rpartition(")")[2].split()
        except OSError:
            continue
        if len(fields) > 2 and fields[2] == str(pgid):
            states.append(fields[0])
    return states


def group_alive(pgid: int) -> bool:
    """Does any LIVE process of the process group still exist? A zombie is
    dead (it holds no port and no memory) and waits only for its reaper,
    which in a container can lag (container gate of fix 0df4258/830f9ac)."""
    states = _group_states(pgid)
    if states is not None:
        return any(s != "Z" for s in states)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_group(pgid: int, grace_s: float = 60.0) -> None:
    """SIGTERM to the whole group, up to ``grace_s`` for it to exit, then SIGKILL."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not group_alive(pgid):
            return
        time.sleep(0.5)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


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
            kill_group(
                proc.pid, grace_s=args.term_grace_s
            )  # SIGTERM: the cell stops its server
            proc.wait()
    m = manifest_of(Path(args.out), w, rep) or {}
    server_pgid = (m.get("server") or {}).get("pgid")
    if server_pgid and group_alive(int(server_pgid)):
        # the cell died before its finally (SIGKILL, OOM): its server survived in
        # its own session; never let the next cell meet it on the port
        kill_group(int(server_pgid), grace_s=args.term_grace_s)
        print(f"  killed the leftover server group {server_pgid} of W={w} rep={rep}")
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
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", default=str(EXPERIMENT_DIR / "config.yaml"))
    ap.add_argument(
        "--census", default=str(EXPERIMENT_DIR / "results/census/census.json")
    )
    ap.add_argument("--out", default=str(EXPERIMENT_DIR / "results"))
    ap.add_argument("--workers", default="", help="comma-separated W (config)")
    ap.add_argument("--reps", type=int, default=None)
    ap.add_argument("--quota", type=int, default=0)
    ap.add_argument("--url", default=None)
    ap.add_argument("--ips-external", action="store_true")
    ap.add_argument("--local-smoke", action="store_true")
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
