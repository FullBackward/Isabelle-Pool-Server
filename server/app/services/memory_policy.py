"""Memory policy shared by the session manager: capacity sanity checks and the
admission check for non-session work (big-step and heap-pool `isabelle build`).

Mixed into ``SessionManager`` (server/app/services/session_manager.py) like
``SessionManagerHelpersMixin``; every method reads ``self.memory`` /
``self.memory_management_enabled`` / ``self.pool_size`` created in
``SessionManager.__init__``. Kept in its own module because the manager is
at the source-size gate (tests/test_source_limits.py).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from server.app.core import metrics
from server.app.core.config import Memory
from server.app.core.logging import get_logger
from server.app.errors import MemoryPressure
from server.app.services.memory_monitor import (
    SOURCE_FALLBACK,
    MemoryMonitor,
    MemorySnapshot,
)

logger = get_logger(__name__)


def _mb(value: Optional[int]) -> Optional[float]:
    return None if value is None else round(value / (1024.0 * 1024.0), 1)


def session_facts_row(session: Any, now: float) -> Dict[str, Any]:
    """One session as the memory facts show it: the fields the public listing
    already exposes plus memory. NEVER the lease id (an ownership proof)."""
    return {
        "session_id": str(session.session_id),
        "label": getattr(session, "label", None),
        "task_group": getattr(session, "task_group", None),
        "field": getattr(session, "field", None),
        "leased": bool(session.leased),
        "in_use": bool(session.in_use),
        "idle_s": round(max(0.0, now - float(session.last_activity)), 1),
        "rss_mb": _mb(getattr(session, "rss_bytes", None)),
        "heap_mb": _mb(getattr(session, "heap_bytes", None)),
    }


def _capped(rows: List[Dict[str, Any]], cap: int) -> Tuple[List[Dict[str, Any]], bool]:
    rows = sorted(rows, key=lambda r: r.get("rss_mb") or 0.0, reverse=True)
    return rows[:cap], len(rows) > cap


class MemoryPolicyMixin:
    """Capacity warnings, memory facts and build admission on top of
    ``MemoryMonitor``."""

    memory: MemoryMonitor
    memory_management_enabled: bool
    pool_size: int
    _lru: Any
    _lock: threading.Lock
    build_verifier: Any
    heap_pool: Any = None  # set by the app lifespan (builds-in-flight facts)

    def builds_in_flight(self) -> Dict[str, int]:
        heap = 0
        pool = getattr(self, "heap_pool", None)
        if pool is not None:
            try:
                heap = sum(1 for e in pool.list() if e.get("status") == "building")
            except Exception:  # pylint: disable=broad-exception-caught
                heap = 0
        return {
            "bigstep": int(getattr(self.build_verifier, "inflight", 0)),
            "heap": heap,
        }

    def memory_facts(
        self, snapshot: Optional[MemorySnapshot] = None, include_sessions: bool = True
    ) -> Dict[str, Any]:
        """The structured facts behind a memory 503 and ``GET /api/v1/memory``
        (plan §3.1): snapshot + tier, builds in flight, and the pool's sessions
        split into ``evictable`` (idle, unleased — the server may reclaim them)
        and ``leased`` (client-owned — never evicted, only reported), each
        capped at Memory.FACTS_LIST_CAP by RSS. Facts only, no advice."""
        snap = snapshot if snapshot is not None else self.memory.read()
        now = time.time()
        facts: Dict[str, Any] = {
            "state": self.memory.state_for(snap),
            "memory": {
                "used_mb": round(snap.used_mb, 1),
                "limit_mb": round(snap.limit_mb, 1),
                "available_mb": round(snap.available_mb, 1),
                "pressure_pct": round(snap.pressure_pct, 1),
                "source": snap.source,
            },
            "retry_after_s": 5,
            "builds_in_flight": self.builds_in_flight(),
            "sledgehammer_inflight": metrics.sledgehammer_inflight_value(),
            "external_pressure": None,  # batch 3
        }
        if include_sessions:
            with self._lock:
                sessions = list(self._lru.values())
            rows = [session_facts_row(s, now) for s in sessions]
            evictable, ev_cut = _capped(
                [r for r in rows if not r["leased"] and not r["in_use"]],
                Memory.FACTS_LIST_CAP,
            )
            leased, le_cut = _capped(
                [r for r in rows if r["leased"]], Memory.FACTS_LIST_CAP
            )
            facts.update(
                evictable=evictable,
                evictable_truncated=ev_cut,
                leased=leased,
                leased_truncated=le_cut,
            )
        return facts

    def create_refused(self, where: str, snap: MemorySnapshot) -> MemoryPressure:
        """The 503 for a session create/acquire the gate refused."""
        metrics.pool_exhausted.labels("memory").inc()
        return MemoryPressure(
            f"{where}: cannot create session: memory pressure too high "
            f"({snap.pressure_pct:.0f}%, {snap.available_mb:.0f}MB available) "
            f"and no idle sessions to evict. Try again later or raise the "
            f"container memory limit / ISABELLE_MEMORY_PRESSURE_THRESHOLD.",
            facts=self.memory_facts(snap),
        )

    def log_capacity_warnings(
        self, snapshot: Optional[MemorySnapshot] = None
    ) -> List[str]:
        """Log (and return) configuration warnings against the memory limit.

        Checked once at startup: a pool that cannot fit under the limit, an ML
        heap cap that lets two processes exceed the limit together, and a
        monitor that cannot measure anything (the gate would be inert).
        Never raises — a warning must not stop the server.
        """
        warnings: List[str] = []
        try:
            snap = snapshot if snapshot is not None else self.memory.read()
        except (
            Exception
        ):  # pylint: disable=broad-exception-caught  # never stop startup
            logger.exception("capacity check could not read memory")
            return warnings
        limit_mb = snap.limit_mb
        need_mb = self.pool_size * Memory.SESSION_ESTIMATE_MB + Memory.JVM_HEAP_MB
        if need_mb > limit_mb:
            warnings.append(
                f"pool_size={self.pool_size} x {Memory.SESSION_ESTIMATE_MB}MB per session "
                f"+ {Memory.JVM_HEAP_MB}MB gateway JVM = {need_mb}MB exceeds the memory "
                f"limit of {limit_mb:.0f}MB; lower ISABELLE_POOL_SIZE or raise the limit"
            )
        if Memory.ML_MAXHEAP_MB > limit_mb / 2:
            warnings.append(
                f"ISABELLE_ML_MAXHEAP_MB={Memory.ML_MAXHEAP_MB} is more than half the "
                f"memory limit ({limit_mb:.0f}MB): two ML processes at the cap can exceed "
                f"the limit together"
            )
        if self.memory_management_enabled and snap.source == SOURCE_FALLBACK:
            warnings.append(
                "memory management is enabled but neither cgroup accounting nor "
                "/proc/meminfo is readable: memory_used_mb reads 0 and the admission "
                "gate never fires on this host"
            )
        for text in warnings:
            logger.warning("capacity check: %s", text)
        return warnings

    def require_memory_for_build(self, kind: str) -> None:
        """Refuse a new `isabelle build` (big-step verification or heap build)
        while the container has no room for another session-sized process.

        Builds used to bypass the admission gate entirely. Idle sessions are
        NOT evicted for a build (a build is transient; the create path owns
        eviction). Raises ``MemoryPressure`` -> HTTP 503 with the facts.
        """
        if not self.memory_management_enabled:
            return
        snap = self.memory.read()
        if self.memory.can_admit(snap):
            return
        metrics.pool_exhausted.labels("memory_build").inc()
        raise MemoryPressure(
            f"cannot start {kind} build: memory pressure too high "
            f"({snap.pressure_pct:.0f}%, {snap.available_mb:.0f}MB available). "
            f"Try again later, close idle sessions, or raise the container memory "
            f"limit / ISABELLE_MEMORY_PRESSURE_THRESHOLD.",
            facts=self.memory_facts(snap),
        )

    async def build_admit(self, kind: str) -> None:
        """Async form of the gate for ``BuildVerifier.verify(admit=...)``: runs
        the file reads off the event loop (Bug 16 rule)."""
        await asyncio.to_thread(self.require_memory_for_build, kind)
