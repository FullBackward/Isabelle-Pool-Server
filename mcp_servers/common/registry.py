"""Registration list of the sessions an MCP server holds (plan §2.C).

Owner decision D4: the prover server is hidden from the agent. The registry is
the MCP's own book of what it holds — session id and lease stay PRIVATE here;
the agent sees a display-only ``ref``, what the session is for (theory name,
file path, scratch context), how long it has been idle, when the server will
reap it, and the memory attributed to it (refreshed from the server's
``/memory`` facts). No tool takes a ``ref`` as an argument: agents act by
theory name (stepwise) or file path (LSP).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

KIND_THEORY = "theory"  # stepwise: the connection's current theory
KIND_BATCH = "batch"  # stepwise verify_batch item (transient)
KIND_BINDING = "binding"  # lsp: an open file
KIND_SCRATCH = "scratch"  # lsp: a warm scratch session


@dataclass
class Entry:  # pylint: disable=too-many-instance-attributes
    """One session this MCP holds (see the module docstring)."""

    session_id: str
    lease_id: Optional[str]
    kind: str
    owner: Any
    label: Optional[str] = None
    task_group: Optional[str] = None
    heap_session: Optional[str] = None
    imports: Tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    # refreshed from the server's /memory or listing rows
    server_idle_s: Optional[float] = None
    expires_in_s: Optional[float] = None
    lease_expires_at: Optional[float] = None
    rss_mb: Optional[float] = None
    heap_mb: Optional[float] = None
    sledgehammer_mb: Optional[float] = (
        None  # solvers of a running sledgehammer/fill step
    )
    state: str = "active"  # active | gone (no longer listed by the server)

    @property
    def ref(self) -> str:
        """Display-only handle (never an argument to any tool)."""
        return hashlib.sha1(self.session_id.encode("utf-8")).hexdigest()[:8]

    def idle_s(self, now: Optional[float] = None) -> float:
        """Seconds since the last tool call touched this session."""
        return max(0.0, (time.time() if now is None else now) - self.last_used_at)

    def describe(self, now: Optional[float] = None) -> str:
        """Short human text for advice lines: kind, what for, idle time."""
        what = self.label or (self.owner if isinstance(self.owner, str) else self.kind)
        return f"{self.kind} {what} (idle {int(self.idle_s(now))}s)"

    def to_agent(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Agent-facing view: never the session id, the lease, or server
        diagnostics (review f1fb31a S4)."""
        return {
            "ref": self.ref,
            "kind": self.kind,
            "for": self.label or (self.owner if isinstance(self.owner, str) else None),
            "task_group": self.task_group,
            "heap_session": self.heap_session,
            "idle_s": round(self.idle_s(now), 1),
            "expires_in_s": self.expires_in_s,
            "rss_mb": self.rss_mb,
            "heap_mb": self.heap_mb,
            "sledgehammer_mb": self.sledgehammer_mb,
            "state": self.state,
        }


class SessionRegistry:
    """The sessions this MCP process holds, keyed by session id."""

    def __init__(self) -> None:
        self._entries: Dict[str, Entry] = {}
        self._memory_failures: Dict[Any, int] = {}

    # ------------------------------------------------------------ lifecycle
    def register(  # pylint: disable=too-many-arguments
        self,
        session_id: str,
        lease_id: Optional[str],
        kind: str,
        owner: Any,
        *,
        label: Optional[str] = None,
        task_group: Optional[str] = None,
        heap_session: Optional[str] = None,
        imports: Iterable[str] = (),
    ) -> Entry:
        entry = Entry(
            session_id=str(session_id),
            lease_id=lease_id,
            kind=kind,
            owner=owner,
            label=label,
            task_group=task_group,
            heap_session=heap_session,
            imports=tuple(imports),
        )
        self._entries[entry.session_id] = entry
        return entry

    def touch(self, session_id: str, now: Optional[float] = None) -> None:
        """Record a tool call on the session (resets its idle time)."""
        entry = self._entries.get(str(session_id))
        if entry is not None:
            entry.last_used_at = time.time() if now is None else now
            entry.state = "active"

    def unregister(self, session_id: str) -> Optional[Entry]:
        """Forget a session (closed, released, evicted or dropped)."""
        return self._entries.pop(str(session_id), None)

    def get(self, session_id: str) -> Optional[Entry]:
        """The entry for a session id, if held."""
        return self._entries.get(str(session_id))

    # -------------------------------------------------------------- queries
    def all(self) -> List[Entry]:
        """Every held session."""
        return list(self._entries.values())

    def owned(self, owner: Any = None) -> List[Entry]:
        """Entries of ``owner``; every entry when ``owner`` is None (a
        single-agent server such as the LSP one)."""
        if owner is None:
            return self.all()
        return [e for e in self._entries.values() if e.owner == owner]

    def ids(self, owner: Any = None) -> set[str]:
        """Session ids of ``owner`` (all when None)."""
        return {e.session_id for e in self.owned(owner)}

    def idle(
        self,
        owner: Any = None,
        *,
        min_idle_s: float = 0.0,
        now: Optional[float] = None,
        kinds: Optional[Iterable[str]] = None,
    ) -> List[Entry]:
        """Held sessions of ``owner`` idle for at least ``min_idle_s`` (and of
        the given kinds, when restricted)."""
        wanted = set(kinds) if kinds is not None else None
        return [
            e
            for e in self.owned(owner)
            if e.idle_s(now) >= min_idle_s and (wanted is None or e.kind in wanted)
        ]

    # ------------------------------------------------- server facts merging
    def merge(self, server_rows: Iterable[Dict[str, Any]]) -> None:
        """Update expiry / memory from server rows (``/memory`` ``sessions``,
        the 503 facts' ``leased`` / ``evictable``, or the public listing).
        Entries the rows do not mention are left untouched (a 503 only lists
        capped subsets); ``mark_missing`` handles full listings."""
        for row in server_rows:
            entry = self._entries.get(str(row.get("session_id", "")))
            if entry is None:
                continue
            for key in (
                "expires_in_s",
                "lease_expires_at",
                "rss_mb",
                "heap_mb",
                "sledgehammer_mb",
            ):
                if key in row:
                    setattr(entry, key, row[key])
            if "idle_s" in row:
                entry.server_idle_s = row["idle_s"]
            entry.state = "active"

    def mark_missing(self, server_rows: Iterable[Dict[str, Any]]) -> List[Entry]:
        """After a FULL listing: entries the server no longer lists are
        ``gone`` (evicted, reaped, or a dead prover). Returns them."""
        listed = {str(r.get("session_id", "")) for r in server_rows}
        gone = [e for e in self._entries.values() if e.session_id not in listed]
        for e in gone:
            e.state = "gone"
        return gone

    # --------------------------------------------- memory-failure bookkeeping
    def note_memory_failure(self, owner: Any) -> int:
        """Count a memory_pressure refusal for ``owner``; returns the streak."""
        self._memory_failures[owner] = self._memory_failures.get(owner, 0) + 1
        return self._memory_failures[owner]

    def memory_failures(self, owner: Any) -> int:
        """Current memory_pressure streak of ``owner``."""
        return self._memory_failures.get(owner, 0)

    def clear_memory_failures(self, owner: Any) -> None:
        """A successful call ends the streak."""
        self._memory_failures.pop(owner, None)
