"""Tier-aware sledgehammer slots (fill_sorries plan §1.4, review B3).

One counter for every sledgehammer run on the server — the interactive
``/sledgehammer`` and ``/sledgehammer_at`` calls and the steps of ``fill_sorries``
jobs — with a capacity that follows the memory tier the sweep caches:
``ok`` → ``MAX_CONCURRENT_SLEDGEHAMMER``, ``warning`` → 1, ``critical`` or
``external_pressure`` → 0. A running step is never interrupted when the tier
drops; waiters are woken on every release and every ``PARK_POLL_S`` so a
parked job resumes when capacity returns. ``acquire(parkable=False)`` (the
synchronous endpoints) raises ``MemoryPressure`` instead of waiting at capacity
0 — the plan keeps the interactive call refused at critical (§5 Q4).
Waiters are served in FIFO order; ``queue_position`` is a waiter's rank.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Any, Dict, List, Optional

from server.app.core.config import Memory, Server
from server.app.core.logging import get_logger
from server.app.errors import MemoryPressure

logger = get_logger(__name__)

STATE_CAPACITY = {"ok": None, "unknown": None, "warning": 1, "critical": 0}


class SlotWaitExpired(Exception):
    """The wait for a slot (queued or parked) reached the caller's deadline;
    ``parked_reason`` is the tier reason at that moment or None (queued)."""

    def __init__(self, parked_reason: Optional[str]) -> None:
        super().__init__(parked_reason or "queued")
        self.parked_reason = parked_reason


class SledgehammerSlots:
    """See the module docstring. ``manager`` provides ``memory`` (the monitor
    with the cached ``last_state`` / ``last_snapshot``), ``memory_management_enabled``
    and ``memory_facts`` (for the refusal body)."""

    def __init__(self, manager: Any, max_slots: Optional[int] = None) -> None:
        self._manager = manager
        self.max_slots = max(1, int(max_slots or Server.MAX_CONCURRENT_SLEDGEHAMMER))
        self.running = 0
        self._waiters: List[int] = []  # FIFO of waiter tokens
        self._tokens = itertools.count(1)
        self._cond: Optional[asyncio.Condition] = (
            None  # bound lazily to the running loop
        )

    # ------------------------------------------------------------- capacity
    def capacity_now(self) -> int:
        """Slots allowed by the cached memory tier (no I/O)."""
        if not getattr(self._manager, "memory_management_enabled", True):
            return self.max_slots
        monitor = getattr(self._manager, "memory", None)
        state = str(getattr(monitor, "last_state", "unknown"))
        snap = getattr(monitor, "last_snapshot", None)
        if snap is not None and getattr(snap, "external_pressure", None):
            return 0
        cap = STATE_CAPACITY.get(state)
        return self.max_slots if cap is None else min(cap, self.max_slots)

    def parked_reason(self) -> Optional[str]:
        """Why capacity is 0 right now, or None."""
        monitor = getattr(self._manager, "memory", None)
        snap = getattr(monitor, "last_snapshot", None)
        if snap is not None and getattr(snap, "external_pressure", None):
            return "external_pressure"
        if str(getattr(monitor, "last_state", "unknown")) == "critical":
            return "critical"
        return None

    def queue_position(self, token: int) -> int:
        """0-based rank of a waiter (0 = next to run); -1 when not waiting."""
        try:
            return self._waiters.index(token)
        except ValueError:
            return -1

    def waiters(self) -> int:
        """How many callers are waiting for a slot."""
        return len(self._waiters)

    # ------------------------------------------------------------- acquire
    def _condition(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    async def acquire(
        self, parkable: bool, on_wait: Any = None, deadline: Optional[float] = None
    ) -> int:
        """Take a slot. ``parkable=False``: raise ``MemoryPressure`` when the tier
        allows nothing (the interactive endpoints' refusal); ``parkable=True``:
        wait, re-checking the tier every ``PARK_POLL_S``. ``on_wait(position,
        reason)`` is called on every re-check so a job can publish its queue
        position / parked state. ``deadline`` (``time.time()``-based): a wait —
        queued or parked — that reaches it raises ``SlotWaitExpired`` so a job's
        ``max_total_s`` bounds its wall time including parking (plan §4, review
        ed2d751 B1). Returns the waiter token (for diagnostics)."""
        cond = self._condition()
        token = next(self._tokens)
        async with cond:
            self._waiters.append(token)
            try:
                while True:
                    cap = self.capacity_now()
                    first = self._waiters[0] == token
                    if first and self.running < cap:
                        self._waiters.remove(token)
                        self.running += 1
                        return token
                    if cap == 0 and not parkable:
                        self._waiters.remove(token)
                        raise self._refusal()
                    if deadline is not None and time.time() >= deadline:
                        self._waiters.remove(token)
                        cond.notify_all()
                        raise SlotWaitExpired(self.parked_reason())
                    if on_wait is not None:
                        try:
                            on_wait(self.queue_position(token), self.parked_reason())
                        except Exception:  # pylint: disable=broad-exception-caught
                            pass
                    wait_s = float(Memory.FILL_PARK_POLL_S)
                    if deadline is not None:
                        wait_s = max(0.05, min(wait_s, deadline - time.time()))
                    try:
                        await asyncio.wait_for(cond.wait(), timeout=wait_s)
                    except asyncio.TimeoutError:
                        pass  # re-check the tier
            except BaseException:
                if token in self._waiters:
                    self._waiters.remove(token)
                cond.notify_all()
                raise

    async def release(self) -> None:
        """Give a slot back and wake the waiters."""
        cond = self._condition()
        async with cond:
            self.running = max(0, self.running - 1)
            cond.notify_all()

    def _refusal(self) -> MemoryPressure:
        facts: Dict[str, Any] = {}
        try:
            facts = self._manager.memory_facts(
                getattr(self._manager.memory, "last_snapshot", None)
            )
        except Exception:  # pylint: disable=broad-exception-caught
            facts = {"state": "critical", "retry_after_s": 5}
        return MemoryPressure(
            "cannot start sledgehammer: memory is critical; release idle sessions "
            "or use fill_sorries, which waits for capacity",
            facts=facts,
            retry_after_s=int(facts.get("retry_after_s", 5) or 5),
        )

    def snapshot(self) -> Dict[str, Any]:
        """Facts for /memory and the job status."""
        return {
            "sledgehammer_slots": self.capacity_now(),
            "sledgehammer_running": self.running,
            "sledgehammer_waiting": len(self._waiters),
            "sledgehammer_parked_reason": self.parked_reason(),
            "sampled_at": time.time(),
        }
