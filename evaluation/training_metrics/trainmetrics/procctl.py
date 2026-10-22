"""Stopping what a cell started (reviews 830f9ac B1, 5141144 S1). The IPS
server runs in its own process group; its gateway JVM and the provers run in
other groups or sessions, so killing the server's group alone would orphan
them when the server does not shut down on SIGTERM. On the server each IPS
runs in its own systemd scope, and ``cgroup.kill`` stops everything in it
exactly; without a scope (a container) the server's process tree is
snapshotted while it is intact and every process in it is signalled.
Linux ``/proc``; on macOS only the process-group calls apply."""

from __future__ import annotations

import os
import signal
import time
from typing import Dict, Iterable, List, Optional, Set, Tuple


def proc_table() -> Optional[Dict[int, Tuple[int, int, str]]]:
    """pid -> (ppid, pgrp, state) from ``/proc`` (None without ``/proc``)."""
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    out: Dict[int, Tuple[int, int, str]] = {}
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "r", encoding="utf-8") as fh:
                fields = fh.read().rpartition(")")[2].split()
            out[int(name)] = (int(fields[1]), int(fields[2]), fields[0])
        except (OSError, ValueError, IndexError):
            continue
    return out


def group_alive(pgid: int) -> bool:
    """Does any LIVE process of the process group still exist? A zombie is
    dead (it holds no port and no memory) and waits only for its reaper,
    which in a container can lag (container gate of fix 0df4258/830f9ac)."""
    table = proc_table()
    if table is not None:
        return any(g == pgid and st != "Z" for _p, (_pp, g, st) in table.items())
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


def group_tree(pgid: int) -> Set[int]:
    """Every live process of group ``pgid`` and all their descendants, in any
    group or session (empty without ``/proc``)."""
    table = proc_table()
    if table is None:
        return set()
    children: Dict[int, List[int]] = {}
    for pid, (ppid, _g, _st) in table.items():
        children.setdefault(ppid, []).append(pid)
    stack = [p for p, (_pp, g, st) in table.items() if g == pgid and st != "Z"]
    seen: Set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
    return seen


def _alive(pids: Iterable[int]) -> Set[int]:
    table = proc_table()
    if table is None:
        out: Set[int] = set()
        for p in pids:
            try:
                os.kill(p, 0)
                out.add(p)
            except ProcessLookupError:
                pass
            except PermissionError:
                out.add(p)
        return out
    return {p for p in pids if p in table and table[p][2] != "Z"}


def _own_cgroup() -> Optional[str]:
    try:
        with open("/proc/self/cgroup", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("0::"):
                    return "/sys/fs/cgroup" + line[3:].strip()
    except OSError:
        return None
    return None


def cgroup_kill(cgroup_dir: Optional[str]) -> bool:
    """Write ``1`` to ``<cgroup_dir>/cgroup.kill`` (cgroup v2: SIGKILL to every
    process in the cgroup). Never the runner's own cgroup nor one of its
    ancestors (a mis-scoped server could sit in the user's slice; review
    afd3de9 S2). True when written."""
    if not cgroup_dir:
        return False
    own = _own_cgroup()
    target = cgroup_dir.rstrip("/")
    if own is not None and (own == target or own.startswith(target + "/")):
        return False
    path = os.path.join(cgroup_dir, "cgroup.kill")
    if not os.path.exists(path):
        return False
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("1")
        return True
    except OSError:
        return False


def cgroup_busy(cgroup_dir: Optional[str]) -> bool:
    """Does the cgroup still hold a process (``cgroup.procs`` not empty)?"""
    if not cgroup_dir:
        return False
    try:
        with open(
            os.path.join(cgroup_dir, "cgroup.procs"), "r", encoding="utf-8"
        ) as fh:
            return bool(fh.read().strip())
    except OSError:
        return False


def _signal_all(pgid: Optional[int], pids: Iterable[int], sig: int) -> None:
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass
    for p in pids:
        try:
            os.kill(p, sig)
        except (ProcessLookupError, PermissionError):
            pass


def kill_server(
    pgid: Optional[int],
    tree: Iterable[int] = (),
    cgroup_dir: Optional[str] = None,
    grace_s: float = 60.0,
) -> Dict[str, object]:
    """Stop a cell's server and everything under it: its scope via
    ``cgroup.kill`` when there is one; then SIGTERM to every live process of
    ``tree`` (a snapshot taken while the tree was intact) and of the group's
    current tree, ``grace_s`` to exit, SIGKILL to what is left. Returns what
    was done, for the runner's log."""
    via_cgroup = cgroup_kill(cgroup_dir)
    pids = set(tree) | (group_tree(pgid) if pgid else set())
    targets = _alive(pids)
    _signal_all(pgid, targets, signal.SIGTERM)  # the group always (also without /proc)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline and (
        _alive(targets) or (pgid is not None and group_alive(pgid))
    ):
        time.sleep(0.5)
    left = _alive(targets)
    group_left = pgid is not None and group_alive(pgid)
    _signal_all(pgid if group_left else None, left, signal.SIGKILL)
    return {
        "cgroup_kill": via_cgroup,
        "signalled": len(targets),
        "sigkilled": len(left) + (1 if group_left else 0),
    }
