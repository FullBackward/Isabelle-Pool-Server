"""One worker's replay of a theory file on a leased IPS session:
``enter_theory`` under a name that is NEW on that session (plan review B1),
then ``verify_chunk`` per top-level block under the budget rule (design B3),
recording every block. Used by the census and by every later experiment."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Tuple

from .corpus import CorpusFile

BUDGET_PER_COMMAND_S = 2.0
BUDGET_MIN_S = 30.0
BUDGET_MAX_S = 600.0
BUDGET_NO_CENSUS_S = 120.0


def budget_for(n_commands: Optional[int], partial: bool = False) -> float:
    """``min(600, max(30, 2 n))`` seconds from the census count; 120 s for a
    block with no census row (plan v1.1 §3, review S3); the maximum for a block
    whose census count is partial — it timed out at the census's maximum budget,
    so its count is a lower bound (review 86afb15 S3)."""
    if partial:
        return BUDGET_MAX_S
    if n_commands is None:
        return BUDGET_NO_CENSUS_S
    return min(BUDGET_MAX_S, max(BUDGET_MIN_S, BUDGET_PER_COMMAND_S * n_commands))


def is_proved(res: Dict[str, Any]) -> bool:
    """``proved := success ∧ ¬proof_open ∧ ¬used_sorry`` (report §3.4)."""
    return (
        bool(res.get("success"))
        and not res.get("proof_open")
        and not res.get("used_sorry")
        and not res.get("timed_out")
    )


class ChunkClient(Protocol):  # pragma: no cover - typing only
    """The two client calls a replay needs (``PoolAsyncClient`` has them)."""

    async def enter_theory(
        self,
        session_id: str,
        theory_name: str,
        *,
        imports: Optional[List[str]] = None,
        lease_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Enter a theory node (with a server-built header when imports given)."""

    async def verify_chunk(
        self,
        session_id: str,
        chunk: str,
        timeout: Optional[float] = None,
        *,
        lease_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Verify one chunk under a wall budget."""


@dataclass
class SessionRef:
    """A leased session and the theory names entered on it so far.

    Invariant (review 86afb15 S2): a ``SessionRef`` lives exactly as long as
    its session, so ``names`` is every name ever entered on it and
    :meth:`new_name` cannot collide. It holds because every acquire in this
    package uses ``reuse_dirty=False`` (never a session with history) and every
    session is closed, not released, when its ``SessionRef`` is done. A caller
    that wraps a re-acquired session in a new ``SessionRef`` breaks it."""

    session_id: str
    lease_id: str
    worker: int = 0
    names: List[str] = field(default_factory=list)

    def new_name(self, stem: str) -> str:
        """``<stem>__w<worker>_r<counter>``: never reused on this session, so
        ``enter_theory`` always creates a fresh node (plan review B1)."""
        name = f"{stem}__w{self.worker}_r{len(self.names)}"
        if name in self.names:  # defensive: the counter makes this impossible
            raise RuntimeError(f"theory name {name} reused on {self.session_id}")
        self.names.append(name)
        return name

    @property
    def nodes(self) -> int:
        """Theory nodes entered on this session (each stays in the document)."""
        return len(self.names)


@dataclass
class BlockRecord:  # pylint: disable=too-many-instance-attributes
    """One ``verify_chunk`` call."""

    stem: str
    index: int
    block_sha: str
    n_commands: Optional[int]  # from the server's report
    census_commands: Optional[int]  # what the budget was computed from
    budget_s: float
    budget_source: str  # census | census_partial | none | override
    success: bool = False
    proof_open: bool = False
    used_sorry: bool = False
    timed_out: bool = False
    stuck_line: Optional[int] = None
    proved: bool = False
    execution_time: Optional[float] = None
    wall_s: float = 0.0
    t_start: float = 0.0
    error: Optional[str] = None
    statuses: Dict[str, int] = field(default_factory=dict)


@dataclass
class ReplayRecord:  # pylint: disable=too-many-instance-attributes
    """One file replayed on one session."""

    stem: str
    theory_name: str
    session_id: str
    worker: int
    nodes_on_session: int
    enter_s: float = 0.0
    enter_error: Optional[str] = None
    blocks: List[BlockRecord] = field(default_factory=list)
    t_start: float = 0.0
    t_end: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        """JSON form."""
        return asdict(self)


def _statuses(commands: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for c in commands:
        st = str(c.get("status") or "unknown")
        out[st] = out.get(st, 0) + 1
    return out


def _budget(
    census_commands: Optional[int], census_partial: bool, override: Optional[float]
) -> Tuple[float, str]:
    """The block's budget and where it came from (recorded per block)."""
    if census_partial:
        source = "census_partial"
    elif census_commands is not None:
        source = "census"
    else:
        source = "none"
    if override is not None:
        return override, "override"
    return budget_for(census_commands, census_partial), source


async def verify_block(  # pylint: disable=too-many-arguments,too-many-locals
    client: ChunkClient,
    sess: SessionRef,
    text: str,
    *,
    stem: str,
    index: int,
    block_sha: str,
    census_commands: Optional[int],
    census_partial: bool = False,
    budget_s: Optional[float] = None,
) -> BlockRecord:
    """One block under its budget; every outcome is recorded, never raised."""
    budget, source = _budget(census_commands, census_partial, budget_s)
    rec = BlockRecord(
        stem=stem,
        index=index,
        block_sha=block_sha,
        n_commands=None,
        census_commands=census_commands,
        budget_s=budget,
        budget_source=source,
        t_start=time.time(),
    )
    t0 = time.monotonic()
    try:
        res = await client.verify_chunk(
            sess.session_id, text, timeout=budget, lease_id=sess.lease_id
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        rec.error = f"{type(exc).__name__}: {exc}"[:300]
        rec.wall_s = round(time.monotonic() - t0, 4)
        return rec
    rec.wall_s = round(time.monotonic() - t0, 4)
    commands = list(res.get("commands") or [])
    rec.n_commands = len(commands)
    rec.statuses = _statuses(commands)
    rec.success = bool(res.get("success"))
    rec.proof_open = bool(res.get("proof_open"))
    rec.used_sorry = bool(res.get("used_sorry"))
    rec.timed_out = bool(res.get("timed_out"))
    rec.stuck_line = res.get("stuck_line")
    rec.execution_time = res.get("execution_time")
    rec.proved = is_proved(res)
    if res.get("error"):
        rec.error = str(res.get("error"))[:300]
    return rec


async def replay_file(  # pylint: disable=too-many-arguments
    client: ChunkClient,
    sess: SessionRef,
    cf: CorpusFile,
    census_counts: Optional[List[Optional[int]]] = None,
    *,
    census_partial: Optional[List[bool]] = None,
    budget_s: Optional[float] = None,
) -> ReplayRecord:
    """Enter a fresh node for ``cf`` with its own imports, then verify every
    block in order. ``census_counts[i]`` is block i's census command count
    (None outside the census), ``census_partial[i]`` whether that count is
    partial (the block timed out in the census). ``budget_s`` overrides the
    rule (the census itself runs at the maximum)."""
    name = sess.new_name(cf.stem)
    rr = ReplayRecord(
        stem=cf.stem,
        theory_name=name,
        session_id=sess.session_id,
        worker=sess.worker,
        nodes_on_session=sess.nodes,
        t_start=time.time(),
    )
    t0 = time.monotonic()
    try:
        res = await client.enter_theory(
            sess.session_id, name, imports=list(cf.imports), lease_id=sess.lease_id
        )
        if isinstance(res, dict) and (
            res.get("error") or res.get("success") is False
        ):  # the header failed: nothing after it would check
            rr.enter_error = str(res.get("error") or "enter_theory failed")[:300]
    except Exception as exc:  # pylint: disable=broad-exception-caught
        rr.enter_error = f"{type(exc).__name__}: {exc}"[:300]
    rr.enter_s = round(time.monotonic() - t0, 4)
    if rr.enter_error:
        rr.t_end = time.time()
        return rr
    for i, text in enumerate(cf.blocks):
        cc = census_counts[i] if census_counts and i < len(census_counts) else None
        cp = (
            bool(census_partial[i])
            if census_partial and i < len(census_partial)
            else False
        )
        rr.blocks.append(
            await verify_block(
                client,
                sess,
                text,
                stem=cf.stem,
                index=i,
                block_sha=cf.block_sha[i],
                census_commands=cc,
                census_partial=cp,
                budget_s=budget_s,
            )
        )
    rr.t_end = time.time()
    return rr
