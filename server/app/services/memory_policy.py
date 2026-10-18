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
    rss_tree,
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


def _parse_estimates(spec: str) -> Dict[str, int]:
    """``"HOL=2048,HOL-Analysis=4096"`` -> {field: MB}; bad entries are skipped."""
    out: Dict[str, int] = {}
    spec = spec.strip().strip(
        "\"'"
    )  # a loader may keep the .env quotes (review aa860ea S3)
    for item in spec.split(","):
        name, _, value = item.partition("=")
        name, value = name.strip(), value.strip()
        if not name or not value:
            continue
        try:
            out[name] = int(value)
        except ValueError:
            continue
    return out


SESSION_ESTIMATES_MB: Dict[str, int] = _parse_estimates(Memory.SESSION_ESTIMATES)


def estimate_session_mb(field: Optional[str]) -> int:
    """Expected resident size of a new session on ``field`` (static table,
    plan-review answer 5; SESSION_ESTIMATE_MB for unknown fields)."""
    if field and field in SESSION_ESTIMATES_MB:
        return SESSION_ESTIMATES_MB[field]
    return Memory.SESSION_ESTIMATE_MB


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
    creates_in_flight: int = 0  # backends being created right now (budget reservation)

    # ----- budget admission (batch 3) ---------------------------------------
    def reserved_bytes(self) -> int:
        """Memory the work already admitted but not yet resident will take:
        in-flight session creates (by field estimate... one generic estimate,
        the field is not tracked per create) and running builds."""
        builds = self.builds_in_flight()
        creates = max(0, int(getattr(self, "creates_in_flight", 0)))
        mb = (
            creates * Memory.SESSION_ESTIMATE_MB
            + sum(builds.values()) * Memory.BUILD_ESTIMATE_MB
        )
        return mb * 1024 * 1024

    def session_budget_bytes(self, field: Optional[str]) -> int:
        """Bytes a new session on ``field`` must fit on top of current usage:
        reserved in-flight work + the field's estimate (relief + admission)."""
        return self.reserved_bytes() + estimate_session_mb(field) * 1024 * 1024

    def admits_session(
        self,
        snapshot: MemorySnapshot,
        field: Optional[str],
        extra_bytes: Optional[int] = None,
    ) -> bool:
        """THE budget rule for a new session (single definition, review 4815c37
        S2): the plain rule, then used + ``extra_bytes`` (reserved in-flight
        work + the field's estimate; computed here unless the caller already
        has it) under the threshold and the floor."""
        if not self.memory.can_admit(snapshot):
            return False
        extra = self.session_budget_bytes(field) if extra_bytes is None else extra_bytes
        return self.memory.can_admit(snapshot, extra_bytes=extra)

    def admits_build(self, snapshot: MemorySnapshot) -> bool:
        """Budget rule for one more `isabelle build` process."""
        if not self.memory.can_admit(snapshot):
            return False
        extra = self.reserved_bytes() + Memory.BUILD_ESTIMATE_MB * 1024 * 1024
        return self.memory.can_admit(snapshot, extra_bytes=extra)

    def refuse_heavy_if_critical(self, kind: str) -> None:
        """Refuse heavy work (sledgehammer) while the tier is critical
        (plan-review answer 4: critical only, never warning). Light calls
        proceed. Raises MemoryPressure -> 503 with the facts."""
        if not self.memory_management_enabled:
            return
        snap = self.memory.read()
        if self.memory.state_for(snap) != "critical":
            return
        metrics.pool_exhausted.labels("memory_heavy").inc()
        facts = self.memory_facts(snap)
        raise MemoryPressure(
            f"cannot start {kind}: memory is critical "
            f"({snap.pressure_pct:.0f}%, {snap.available_mb:.0f}MB available). "
            f"Release idle sessions or try again later.",
            facts=facts,
            retry_after_s=int(facts["retry_after_s"]),
        )

    # ----- size-aware eviction (batch 3) -------------------------------------
    def pick_evictable_locked(self) -> Optional[Any]:
        """Session id to evict under pressure: among idle UNLEASED sessions,
        the largest by attributed RSS when any is known, else the oldest
        (LRU). Leased sessions are never candidates (D3). Caller holds _lock."""
        candidates = [
            (sid, s) for sid, s in self._lru.items() if not s.in_use and not s.leased
        ]
        if not candidates:
            return None
        sized = [(sid, s) for sid, s in candidates if getattr(s, "rss_bytes", None)]
        if sized:
            return max(sized, key=lambda p: p[1].rss_bytes or 0)[0]
        return candidates[0][0]

    def discard_dead(self, session_id: Any, reason: str = "prover_died") -> bool:
        """Remove a session whose ML process is gone (plan-review B2).

        Marking it CLOSED is not enough: a CLOSED session left in the LRU is
        skipped by the sweep, refused by close_session, keeps its lease and its
        pool slot. Pops under the lock, closes outside it (best effort — the
        backend has nothing to talk to), counts the eviction. Returns False
        when the id is unknown (already gone)."""
        cancel = getattr(self, "cancel_job_nowait", None)
        if cancel is not None:  # a dead prover's fill job stops (B2)
            cancel(session_id)
        with self._lock:
            session = self._lru.pop(session_id, None)
        if session is None:
            return False
        forget = getattr(self, "forget_job", None)
        if forget is not None:  # registry pruned with the session (review S4)
            forget(session_id)
        logger.error(
            "discarding dead session session_id=%s reason=%s pid=%s",
            session_id,
            reason,
            getattr(session, "ml_pid", None),
        )
        try:
            session.close()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("close of dead session %s raised (ignored)", session_id)
        metrics.sessions_evicted.labels(reason).inc()
        metrics.memory_failures.labels(reason).inc()
        return True

    def sample_sessions_rss(self, now: Optional[float] = None) -> Dict[str, int]:
        """Sweep-side attribution: one /proc scan for every session with a known
        pid; a pid that vanished means the prover died -> discard_dead."""
        now = time.time() if now is None else now
        with self._lock:
            pairs = [
                (sid, s) for sid, s in self._lru.items() if getattr(s, "ml_pid", None)
            ]
        tree = rss_tree({s.ml_pid for _, s in pairs})
        dead = []
        for sid, session in pairs:
            rss = tree.get(session.ml_pid)
            # prover_alive is start-time aware: a reused pid is NOT the session's poly.
            if rss is None or session.prover_alive() is False:
                dead.append(sid)
            else:
                session.set_rss(rss, now)
        for sid in dead:
            self.discard_dead(sid)
        return {"sampled": len(pairs) - len(dead), "dead": len(dead)}

    @staticmethod
    def rss_totals(sessions: List[Any]) -> Dict[str, float]:
        """Pool RSS totals in MB, split by lease state (metrics S5)."""
        leased = unleased = 0
        for s in sessions:
            rss = getattr(s, "rss_bytes", None) or 0
            if s.leased:
                leased += rss
            else:
                unleased += rss
        return {
            "sessions_rss_mb": _mb(leased + unleased) or 0.0,
            "sessions_rss_leased_mb": _mb(leased) or 0.0,
            "sessions_rss_unleased_mb": _mb(unleased) or 0.0,
        }

    def builds_in_flight(self) -> Dict[str, int]:
        """Running big-step builds and heap builds (facts + reservations)."""
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
        capped at Memory.FACTS_LIST_CAP by RSS. Facts only, no advice.

        Takes ``_lock`` (non-reentrant): NEVER call while holding it — compute
        under the lock, call this outside (review a96f76d S2)."""
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
            "sledgehammer_inflight": int(metrics.sledgehammer_inflight_value()),
            "external_pressure": snap.external_pressure,
            "host_available_mb": _mb(snap.host_available_bytes),
            "host_others_used_mb": _mb(snap.host_others_used_bytes),
            "reserved_mb": round(self.reserved_bytes() / (1024.0 * 1024.0), 1),
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
        facts = self.memory_facts(snap)
        return MemoryPressure(
            f"{where}: cannot create session: memory pressure too high "
            f"({snap.pressure_pct:.0f}%, {snap.available_mb:.0f}MB available) "
            f"and no idle sessions to evict. Try again later or raise the "
            f"container memory limit / ISABELLE_MEMORY_PRESSURE_THRESHOLD.",
            facts=facts,
            retry_after_s=int(facts["retry_after_s"]),  # one source for body + header
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
        except Exception:  # pylint: disable=broad-exception-caught
            # never stop startup
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
        if Memory.WARNING_THRESHOLD >= Memory.PRESSURE_THRESHOLD:
            warnings.append(
                f"ISABELLE_MEMORY_WARNING_THRESHOLD={Memory.WARNING_THRESHOLD} is not below "
                f"ISABELLE_MEMORY_PRESSURE_THRESHOLD={Memory.PRESSURE_THRESHOLD}: the "
                f"'warning' tier can never be reported"
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
        if self.admits_build(snap):
            return
        metrics.pool_exhausted.labels("memory_build").inc()
        facts = self.memory_facts(snap)
        raise MemoryPressure(
            f"cannot start {kind} build: memory pressure too high "
            f"({snap.pressure_pct:.0f}%, {snap.available_mb:.0f}MB available). "
            f"Try again later, close idle sessions, or raise the container memory "
            f"limit / ISABELLE_MEMORY_PRESSURE_THRESHOLD.",
            facts=facts,
            retry_after_s=int(facts["retry_after_s"]),
        )

    async def build_admit(self, kind: str) -> None:
        """Async form of the gate for ``BuildVerifier.verify(admit=...)``: runs
        the file reads off the event loop (Bug 16 rule)."""
        await asyncio.to_thread(self.require_memory_for_build, kind)
