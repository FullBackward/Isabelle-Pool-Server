"""Background sampler: every ``interval_s`` it snapshots the process trees
under the configured roots (explicit pids plus command-line patterns), the
host memory and the cgroup, tags the row with the current phase and appends
it as one JSON line. Tool-agnostic: IPS and PIDE MCP are sampled the same way.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from . import procs


class Sampler:  # pylint: disable=too-many-instance-attributes
    """``with Sampler(path, patterns=[...]) as s: s.phase("P1"); ...``"""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        out_path: str,
        *,
        root_pids: Iterable[int] = (),
        patterns: Iterable[str] = (),
        interval_s: float = 1.0,
        cgroup_dir: Optional[str] = None,
        snapshot_fn: Optional[Callable[..., List[procs.ProcRow]]] = None,
    ) -> None:
        self.out_path = out_path
        self.root_pids: Set[int] = {int(p) for p in root_pids}
        self.patterns: List[str] = list(patterns)
        self.interval_s = float(interval_s)
        self.cgroup_dir = cgroup_dir
        self._snapshot = snapshot_fn or procs.snapshot
        self._phase = "P0"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.rows_written = 0
        self.errors = 0  # sampling exceptions, visible in every row (review 2e1935e S3)
        self.last_error: Optional[str] = None
        self.last_row: Optional[Dict[str, Any]] = None

    # ----------------------------------------------------------- control
    def phase(self, name: str) -> None:
        """Tag every later row with ``name`` (and write one row right away)."""
        with self._lock:
            self._phase = name
        self.sample_once()

    def add_roots(self, pids: Iterable[int]) -> None:
        """Also sample the trees under these pids from now on."""
        with self._lock:
            self.root_pids.update(int(p) for p in pids)

    def start(self) -> "Sampler":
        """Start the background thread (idempotent with stop)."""
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="memscale-sampler", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop the thread; the last row stays on disk."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s * 3 + 1)
            self._thread = None

    def __enter__(self) -> "Sampler":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ---------------------------------------------------------- sampling
    def sample_once(self) -> Dict[str, Any]:
        """Take one sample now and append it (also used by ``phase``)."""
        table = procs.process_table()
        with self._lock:
            roots = set(self.root_pids)
            patterns = list(self.patterns)
            phase = self._phase
        if patterns:
            roots |= procs.find_roots(patterns, table)
        skipped: List[int] = []
        rows = self._snapshot(roots, table, skipped) if roots else []
        sums = procs.sum_rows(rows)
        row: Dict[str, Any] = {
            "t": round(time.time(), 3),
            "phase": phase,
            "roots": sorted(roots),
            "host": procs.host_memory(),
            "cgroup": procs.cgroup_memory(self.cgroup_dir),
            "sum": sums,
            "procs": [r.as_dict() for r in rows],
            "cgroup_dir": self.cgroup_dir or procs.own_cgroup_dir(),
            "skipped_pids": skipped,
            "pss_estimated": sum(1 for r in rows if r.pss_estimated),
            "errors": self.errors,
        }
        with self._lock:
            with open(self.out_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            self.rows_written += 1
            self.last_row = row
        return row

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.sample_once()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                self.errors += 1  # counted, visible in the next row (S3)
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            self._stop.wait(max(0.05, self.interval_s - (time.time() - t0)))


def load_rows(path: str) -> List[Dict[str, Any]]:
    """Read a samples.jsonl file."""
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out
