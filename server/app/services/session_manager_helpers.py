"""Helper/lifecycle methods split out of session_manager.py to keep files modular.

`SessionManagerHelpersMixin` is mixed into `SessionManager`
(server/app/services/session_manager.py): gateway lifecycle/recovery, value
normalizers, dependency-key builders, memory-pressure relief, idle-cleanup loop, and
read-only info/reporting. All methods reference `self.*` (instance state created in
`SessionManager.__init__`) and are resolved via the MRO at runtime.
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple, Union

from server.repl.src.python.repl_backend_gateway import ReplBackendGatewayProcess
from server.app.core.config import Memory, Server
from server.app.core.logging import get_logger, logging_context
from server.app.core import metrics
from server.app.errors import GatewayUnavailable, SessionError
from server.app.services.session import SessionStatus, _Isabelle_Session

# Single global lock guarding gateway (re)creation across all manager operations.
_gateway_lock = threading.Lock()
logger = get_logger(__name__)


class SessionManagerHelpersMixin:
    # ----- gateway lifecycle / recovery -----------------------------------
    def _where(self, func_name: str) -> str:
        return f"{__name__}.{self.__class__.__name__}.{func_name}"

    def _ensure_gateway(self):
        where = self._where("_ensure_gateway")
        try:
            with _gateway_lock:
                # If the gateway JVM died (e.g. OOM-killed under a sledgehammer
                # burst) or is wedged alive (Bug 9: Event_Timer cancelled), its
                # sessions are all invalid. Purge them and rebuild instead of
                # leaving the server bricked with 500s.
                if self.gateway is not None and not self.gateway.is_alive():
                    logger.error(
                        "REPL gateway is dead or wedged (probe failed); recovering"
                    )
                    self._recover_gateway_locked()
                if self.gateway is None:
                    logger.info("starting REPL gateway")
                    self.gateway = ReplBackendGatewayProcess()
                    logger.info("REPL gateway started")
        except Exception as e:
            logger.exception("failed to start REPL gateway")
            raise GatewayUnavailable(f"{where}: failed to start REPL gateway: {e}") from e

    #: Seconds a functional liveness result is cached, so health probes don't
    #: pay a Py4J round-trip per request.
    _ALIVE_PROBE_CACHE_S: float = 5.0

    def gateway_alive(self) -> bool:
        gw = self.gateway
        if gw is None:
            return False
        now = time.monotonic()
        if now - getattr(self, "_alive_probe_ts", 0.0) < self._ALIVE_PROBE_CACHE_S:
            return getattr(self, "_alive_probe", False)
        try:
            alive = gw.is_alive()
        except Exception as e:
            # Never swallow silently: a probe that ERRORS (rather than answers
            # False) is exactly the wedge signature we need to diagnose.
            logger.warning("gateway liveness probe raised: %s: %s", type(e).__name__, e)
            alive = False
        self._alive_probe, self._alive_probe_ts = alive, now
        return alive

    def _recover_gateway_locked(self) -> None:
        """Tear down a dead gateway and its now-invalid sessions so the next
        ``_ensure_gateway`` rebuilds. Caller holds ``_gateway_lock``.

        The sessions' backends point at the dead JVM, so close() is best-effort
        only. thy_init is reset and recreated lazily on the next create.
        """
        metrics.gateway_restarts.inc()
        with self._lock:
            dead = list(self._lru.values())
            self._lru.clear()
        logger.error("purging %d session(s) belonging to the dead gateway", len(dead))
        for s in dead:
            try:
                s.close()
            except Exception:
                pass  # backend is dead; nothing to talk to
        old = self.gateway
        self.gateway = None
        self.thy_init = None
        if old is not None:
            try:
                old.terminate()
            except Exception:
                pass

    # ----- value normalizers / dependency keys ----------------------------
    def _normalize_field(self, field: Optional[str]) -> str:
        if field is None:
            return Server.DEFAULT_FIELD
        f = str(field).strip()
        if f == "" or f.lower() in {"null", "none", "default"}:
            return Server.DEFAULT_FIELD
        return f

    def _normalize_session_id(self, session_id: Union[str, uuid.UUID]) -> uuid.UUID:
        where = self._where("_normalize_session_id")
        if isinstance(session_id, uuid.UUID):
            return session_id
        try:
            return uuid.UUID(str(session_id))
        except Exception as e:
            raise SessionError(f"{where}: invalid session_id '{session_id}'") from e

    def _normalize_theories(self, theories: Optional[List[str]]) -> List[str]:
        if not theories:
            return []
        cleaned = []
        for theory in theories:
            if theory is None:
                continue
            value = str(theory).strip()
            if value:
                cleaned.append(value)
        return sorted(set(cleaned))

    def build_dependency_key(self, theories: Optional[List[str]], field: Optional[str], extra: Optional[str] = None) -> str:
        normalized_field = self._normalize_field(field)
        normalized_theories = self._normalize_theories(theories)
        # extra: tenancy/heap versioning (task_group + heap fingerprint, Stage 3) —
        # pooling stays per group AND per heap-version. None keeps the legacy key.
        base = f"{normalized_field}::{'|'.join(normalized_theories)}"
        if extra:
            base += f"::{extra}"
        return hashlib.sha256(base.encode("utf-8")).hexdigest()

    def _build_wrapper_theory_name(self, theories: Optional[List[str]], field: Optional[str]) -> str:
        dep_key = self.build_dependency_key(theories, field)
        return f"wrap_{dep_key[:16]}"

    # ----- memory-pressure relief -----------------------------------------
    def _pop_oldest_idle_locked(self) -> Optional[Tuple[uuid.UUID, _Isabelle_Session]]:
        """Pop the oldest idle (not in_use, not leased) session. Caller holds _lock."""
        for sid in self._lru:
            s = self._lru[sid]
            if not s.in_use and not s.leased:
                return sid, self._lru.pop(sid)
        return None

    def _relieve_memory_pressure(self, where: str):
        """Evict idle LRU sessions oldest-first while under memory pressure.

        Returns the latest MemorySnapshot. Closes sessions outside the manager
        lock. Stops when memory can admit again or no idle session remains.
        Synchronous (closing a session blocks); call via asyncio.to_thread from
        async contexts.
        """
        snap = self.memory.read()
        if self.memory.can_admit(snap):
            return snap
        while not self.memory.can_admit(snap):
            with self._lock:
                popped = self._pop_oldest_idle_locked()
            if popped is None:
                break  # nothing idle left to reclaim
            sid, sess = popped
            try:
                with logging_context(session_id=sid, field=sess.field):
                    logger.warning(
                        "evicting idle session under memory pressure "
                        "pressure=%.1f%% used=%.0fMB available=%.0fMB where=%s",
                        snap.pressure_pct, snap.used_mb, snap.available_mb, where,
                    )
                    sess.close()
                    metrics.sessions_evicted.labels("memory").inc()
            except Exception:
                logger.exception("failed to close session during memory eviction sid=%s", sid)
            # cgroup accounting lags the close (poly exit + kernel reclaim);
            # settle briefly so we don't over-evict on a stale snapshot.
            time.sleep(Memory.EVICTION_SETTLE_S)
            snap = self.memory.read()
        return snap

    # ----- read-only info / reporting + idle cleanup loop -----------------
    def list_sessions(self, *, include_lease: bool = False) -> List[Dict[str, Any]]:
        """Pool listing for the sessions endpoint.

        ``lease_id`` is an ownership proof for mutation endpoints, so it is
        NEVER included in the public listing (any caller can read it and then
        delete the session it belongs to — the lease leak). Only the
        token-gated admin endpoint passes ``include_lease=True``.
        """
        with self._lock:
            entries = []
            for sid, session in self._lru.items():
                entry = {
                    "session_id": str(sid),
                    "created_at": session.created_at,
                    "last_activity": session.last_activity,
                    "status": session.status.value,
                    "theories": session.theories,
                    "loaded_theories": session.loaded_theories,
                    "wrapper_theory": session.wrapper_theory,
                    "dependency_key": session.dependency_key,
                    "field": session.field,
                    "commands_executed": len(session.command_history),
                    "verified_theories": session.verified_theories,
                    "in_use": session.in_use,
                    "active_requests": session.active_request_count,
                    "leased": session.leased,
                    "label": session.label,
                    "task_group": session.task_group,
                }
                if include_lease:
                    entry["lease_id"] = session.lease_id
                entries.append(entry)
        return entries

    async def cleanup_idle_sessions(self) -> None:
        max_lease_age = self.max_lease_age

        while True:
            await asyncio.sleep(self.cleanup_interval)
            now = time.time()
            to_close: List[uuid.UUID] = []

            with self._lock:
                for sid, session in list(self._lru.items()):
                    if session.status == SessionStatus.CLOSED:
                        continue
                    if session.leased:
                        if session.is_idle(max_lease_age, now=now):
                            logger.warning(
                                "force-closing abandoned leased session "
                                "session_id=%s lease_id=%s idle_for=%.0fs",
                                sid, session.lease_id, now - session.last_activity,
                            )
                            to_close.append(sid)
                        continue
                    if session.is_idle(self.idle_timeout, now=now):
                        to_close.append(sid)

            for sid in to_close:
                logger.info("closing idle session session_id=%s", sid)
                try:
                    self.close_session(sid, require_lease=False)
                    metrics.sessions_evicted.labels("idle").inc()
                except Exception:
                    logger.exception("failed to close idle session session_id=%s", sid)

            # Proactively reclaim idle sessions if the container is under memory
            # pressure, independent of the idle-timeout sweep above.
            if self.memory_management_enabled and not self.memory.can_admit():
                self._relieve_memory_pressure(self._where("cleanup_idle_sessions"))

            # Proactively recover a dead gateway (e.g. OOM-killed) so the server
            # doesn't sit bricked until the next create request comes in.
            if self.gateway is not None and not self.gateway_alive():
                logger.error("cleanup detected dead gateway; recovering")
                try:
                    self._ensure_gateway()
                except Exception:
                    logger.exception("background gateway recovery failed")

    def start_cleanup_task(self) -> None:
        if self._cleanup_task is None:
            logger.info("starting idle session cleanup task")
            self._cleanup_task = asyncio.create_task(self.cleanup_idle_sessions())

    def get_lru_info(self) -> Dict[str, Any]:
        with self._lock:
            sessions = list(self._lru.values())
            info = {
                "active_sessions": len(sessions),
                "max_pool_size": self.pool_size,
                "busy_sessions": sum(1 for s in sessions if s.in_use),
                "leased_sessions": sum(1 for s in sessions if s.leased),
            }
        # Read memory outside the lock (file reads, no shared state).
        info["memory_management_enabled"] = self.memory_management_enabled
        info.update(self.memory.status_dict())
        info["gateway_alive"] = self.gateway_alive()
        info["max_concurrent_sledgehammer"] = self.max_concurrent_sledgehammer
        return info
