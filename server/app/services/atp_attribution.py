"""ATP attribution for sledgehammer runs (fill_sorries plan §1.6, review B4).

The ATP/SMT solvers sledgehammer spawns are descendants of the gateway JVM
(ML's bash goes through Scala: JVM -> bash_process -> bash -> solver, each
script in its own session), so ``rss_tree(poly)`` never sees them. While a
step (or an interactive sledgehammer) runs for a session, a sampler walks the
gateway's descendants, skips the plumbing and the sessions' poly subtrees, and
attributes the rest to that session; with several steps in flight the children cannot be told apart, so
the sum goes to each with ``shared_with`` = the other in-flight steps.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Optional

from server.app.core.config import Memory
from server.app.services.memory_monitor import gateway_children_rss


class AtpAttributionMixin:
    """Mixed into the manager through ``FillJobMixin``."""

    gateway: Any
    _lru: Any
    _lock: Any

    def sample_sledgehammer_rss(self) -> Optional[int]:
        """Bytes held right now by the gateway's descendants that are neither
        plumbing nor a session's poly (the ATPs); None without a gateway."""
        gw = getattr(self, "gateway", None)
        proc = getattr(gw, "process", None)
        if proc is None:
            return None
        root = getattr(proc, "pid", None)
        if root is None:
            return None
        try:
            with self._lock:
                provers = [
                    s.ml_pid for s in self._lru.values() if getattr(s, "ml_pid", None)
                ]
        except AttributeError:
            provers = []
        total, _count = gateway_children_rss(int(root), provers)
        return total

    @contextlib.asynccontextmanager
    async def track_sledgehammer(self, session: Any) -> Any:
        """Sample the ATP memory every FILL_SAMPLE_S while the body runs and
        attribute it to ``session`` (shared with the other in-flight steps)."""
        slots = getattr(self, "slots", None)
        stop = asyncio.Event()

        async def loop() -> None:
            while not stop.is_set():
                rss = await asyncio.to_thread(self.sample_sledgehammer_rss)
                if rss:
                    running = max(1, int(getattr(slots, "running", 1)))
                    note = getattr(session, "note_sledgehammer_rss", None)
                    if note is not None:
                        note(int(rss), running - 1)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=Memory.FILL_SAMPLE_S)
                except asyncio.TimeoutError:
                    pass

        task = asyncio.get_running_loop().create_task(loop())
        cancelled = False
        try:
            yield
        except asyncio.CancelledError:
            # the solvers of a cancelled step live on until their own timeout:
            # the caller clears the attribution when the future completes
            # (review 260d78d S3); the sampler stops here all the same
            cancelled = True
            raise
        finally:
            stop.set()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            if not cancelled:
                clear = getattr(session, "clear_sledgehammer_rss", None)
                if clear is not None:
                    clear()
