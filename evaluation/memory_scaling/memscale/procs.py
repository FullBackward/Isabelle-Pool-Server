"""/proc readers for the memory-scaling sampler: process tree by root pids or
by command-line pattern, RSS/PSS/USS per process (``smaps_rollup``), host
memory (``/proc/meminfo``) and the caller's cgroup v2 memory files.

Everything reads text files; nothing is tool-specific. ``PROC_ROOT`` and
``CGROUP_ROOT`` are module variables so tests can point them at a fake tree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple

PROC_ROOT = "/proc"
CGROUP_ROOT = "/sys/fs/cgroup"

# comm -> role; everything else is "other" (bash wrappers, sh, timeout, ...)
ROLE_BY_COMM = {
    "java": "jvm",
    "poly": "poly",
    "python": "python",
    "python3": "python",
    "python3.12": "python",
    "eprover": "atp",
    "vampire": "atp",
    "cvc5-bin": "atp",
    "cvc5": "atp",
    "veriT": "atp",
    "SPASS": "atp",
    "zipperposition": "atp",
    "z3": "atp",
}


@dataclass(frozen=True)
class ProcRow:  # pylint: disable=too-many-instance-attributes
    """One process in a sampled tree (bytes)."""

    pid: int
    ppid: int
    comm: str
    role: str
    rss: int
    pss: int
    uss: int
    shared: int

    def as_dict(self) -> Dict[str, object]:
        """JSON row."""
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "comm": self.comm,
            "role": self.role,
            "rss": self.rss,
            "pss": self.pss,
            "uss": self.uss,
            "shared": self.shared,
        }


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def read_stat(pid: int) -> Optional[Tuple[int, str, str]]:
    """``(ppid, comm, state)`` from ``/proc/<pid>/stat``; comm may contain
    spaces or parentheses, so the line is split after its last ``)``."""
    stat = _read(f"{PROC_ROOT}/{pid}/stat")
    if not stat:
        return None
    head, _, rest = stat.rpartition(")")
    comm = head.partition("(")[2]
    fields = rest.split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1]), comm, fields[0]
    except ValueError:
        return None


def process_table() -> Dict[int, Tuple[int, str, str]]:
    """pid -> (ppid, comm, state) for every process (one /proc scan)."""
    out: Dict[int, Tuple[int, str, str]] = {}
    try:
        names = os.listdir(PROC_ROOT)
    except OSError:
        return out
    for name in names:
        if not name.isdigit():
            continue
        row = read_stat(int(name))
        if row is not None:
            out[int(name)] = row
    return out


def cmdline(pid: int) -> str:
    """The process's argv joined by spaces ('' when unreadable)."""
    raw = _read(f"{PROC_ROOT}/{pid}/cmdline") or ""
    return raw.replace("\0", " ").strip()


def find_roots(
    patterns: Iterable[str], table: Optional[Dict[int, Tuple[int, str, str]]] = None
) -> Set[int]:
    """Pids whose command line contains any of ``patterns`` (substring match)."""
    pats = [p for p in patterns if p]
    if not pats:
        return set()
    table = table if table is not None else process_table()
    roots: Set[int] = set()
    for pid in table:
        line = cmdline(pid)
        if line and any(p in line for p in pats):
            roots.add(pid)
    return roots


def descendants(
    roots: Iterable[int], table: Dict[int, Tuple[int, str, str]]
) -> Set[int]:
    """The roots plus every process below them by parent link."""
    children: Dict[int, List[int]] = {}
    for pid, (ppid, _comm, _state) in table.items():
        children.setdefault(ppid, []).append(pid)
    seen: Set[int] = set()
    stack = [int(r) for r in roots if int(r) in table]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
    return seen


def smaps_rollup(pid: int) -> Optional[Dict[str, int]]:
    """``{rss, pss, uss, shared}`` in bytes from ``/proc/<pid>/smaps_rollup``;
    None when the process is gone or the file is unreadable (needs the same
    user or root)."""
    text = _read(f"{PROC_ROOT}/{pid}/smaps_rollup")
    if not text:
        return None
    vals: Dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if len(parts) >= 2 and parts[1] == "kB":
            try:
                vals[key] = int(parts[0]) * 1024
            except ValueError:
                continue
    if "Rss" not in vals:
        return None
    private = vals.get("Private_Clean", 0) + vals.get("Private_Dirty", 0)
    shared = vals.get("Shared_Clean", 0) + vals.get("Shared_Dirty", 0)
    return {
        "rss": vals["Rss"],
        "pss": vals.get("Pss", vals["Rss"]),
        "uss": private,
        "shared": shared,
    }


def role_of(comm: str) -> str:
    """jvm / poly / python / atp / other, by the process's comm."""
    return ROLE_BY_COMM.get(comm, "other")


def snapshot(
    roots: Iterable[int], table: Optional[Dict[int, Tuple[int, str, str]]] = None
) -> List[ProcRow]:
    """RSS/PSS/USS of every process under ``roots`` (zombies and unreadable
    processes are skipped: a zombie has no memory)."""
    table = table if table is not None else process_table()
    rows: List[ProcRow] = []
    for pid in sorted(descendants(roots, table)):
        ppid, comm, state = table[pid]
        if state.startswith("Z"):
            continue
        mem = smaps_rollup(pid)
        if mem is None:
            continue
        rows.append(
            ProcRow(
                pid,
                ppid,
                comm,
                role_of(comm),
                mem["rss"],
                mem["pss"],
                mem["uss"],
                mem["shared"],
            )
        )
    return rows


def sum_rows(rows: Iterable[ProcRow]) -> Dict[str, Dict[str, int]]:
    """Totals and per-role totals (bytes) of ``rss``/``pss``/``uss``."""
    total = {"rss": 0, "pss": 0, "uss": 0, "count": 0}
    by_role: Dict[str, Dict[str, int]] = {}
    for r in rows:
        for key, val in (("rss", r.rss), ("pss", r.pss), ("uss", r.uss)):
            total[key] += val
            by_role.setdefault(r.role, {"rss": 0, "pss": 0, "uss": 0, "count": 0})[
                key
            ] += val
        total["count"] += 1
        by_role[r.role]["count"] += 1
    return {"total": total, **by_role}


def host_memory() -> Dict[str, Optional[int]]:
    """``{total, available, used}`` in bytes from ``/proc/meminfo``
    (``used = total - available``, the memory the host has actually lost)."""
    text = _read(f"{PROC_ROOT}/meminfo") or ""
    vals: Dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and key in ("MemTotal", "MemAvailable"):
            try:
                vals[key] = int(parts[0]) * 1024
            except ValueError:
                continue
    total, avail = vals.get("MemTotal"), vals.get("MemAvailable")
    used = total - avail if total is not None and avail is not None else None
    return {"total": total, "available": avail, "used": used}


def own_cgroup_dir() -> Optional[str]:
    """The cgroup v2 directory of this process (``/proc/self/cgroup``: ``0::/path``)."""
    text = _read(f"{PROC_ROOT}/self/cgroup") or ""
    for line in text.splitlines():
        if line.startswith("0::"):
            rel = line[3:].strip()
            return CGROUP_ROOT + (rel if rel != "/" else "")
    return None


def cgroup_memory(cg_dir: Optional[str] = None) -> Dict[str, Optional[int]]:
    """``{current, peak, max, oom_kill}`` of a cgroup v2 directory (None where
    a file is missing; ``max`` None when unlimited)."""
    cg_dir = cg_dir or own_cgroup_dir()
    out: Dict[str, Optional[int]] = {
        "current": None,
        "peak": None,
        "max": None,
        "oom_kill": None,
    }
    if not cg_dir:
        return out
    for key, name in (
        ("current", "memory.current"),
        ("peak", "memory.peak"),
        ("max", "memory.max"),
    ):
        text = (_read(f"{cg_dir}/{name}") or "").strip()
        if text and text != "max":
            try:
                out[key] = int(text)
            except ValueError:
                pass
    events = _read(f"{cg_dir}/memory.events") or ""
    for line in events.splitlines():
        if line.startswith("oom_kill "):
            try:
                out["oom_kill"] = int(line.split()[1])
            except (ValueError, IndexError):
                pass
    return out
