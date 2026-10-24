"""The per-file machinery of T5 (design §T5), shared by the W = 1 cell
(:mod:`trainmetrics.run_t5`) and the contention cell
(:mod:`trainmetrics.run_t5w8`); moved out of run_t5 in stage 3c so that the
two cells import it without a cycle. Per seed attempt: the outcome
(timed_out / failed / kept / error / memory / prover_died / session_gone;
reviews 7bedac5 S1, beac25c S1), the wall time and the undo cost past the
budget, ``stuck_line``, whether the node source is unchanged (a hash of
``GET …/source``), a rollback of a kept seed (7bedac5 S2), a quiet pause for
the sampler (1676d29 B1). Then the corpus block: its verdict against the
census and its time (recovery)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .corpus import CorpusFile, sha256_text
from .replay import (
    BlockRecord,
    SessionRef,
    _acquire_retrying,
    close_all,
    verify_block,
    verify_in_file,
)
from .seeds import BudgetLadder, Seed


@dataclass
class SeedAttempt:  # pylint: disable=too-many-instance-attributes
    """One seed at one budget."""

    seed_id: str
    family: str
    goal_source: str
    budget_s: float
    outcome: (
        str  # timed_out | failed | kept | error | memory | prover_died | session_gone
    )
    wall_s: float
    undo_s: Optional[float]  # wall time past the budget (timed-out attempts)
    stuck_line: Optional[int]
    n_commands: Optional[int]
    statuses: Dict[str, int]
    error: Optional[str]
    source_unchanged: Optional[bool]  # None: the source could not be read
    rolled_back: bool = False
    t_start: float = 0.0
    error_kind: Optional[str] = None
    first_error: Optional[str] = None  # why a seed failed (review beac25c S5)
    mem_before: Optional[Dict[str, Any]] = None  # the session's row + server tier (S2)
    mem_after: Optional[Dict[str, Any]] = None
    t_settled: Optional[float] = (
        None  # end of the quiet pause after the call (1676d29 B1)
    )


@dataclass
class PositionRecord:  # pylint: disable=too-many-instance-attributes
    """The seeds at one position and the corpus block checked after them."""

    stem: str
    position: int
    attempts: List[SeedAttempt] = field(default_factory=list)
    census_proved: bool = False
    census_wall_s: Optional[float] = None
    block: Optional[Dict[str, Any]] = None
    verdict_match: Optional[bool] = None
    recovery_s: Optional[float] = None
    stopped: Optional[str] = None  # the session died here (review beac25c S1)


@dataclass
class T5FileRecord:  # pylint: disable=too-many-instance-attributes
    """One file: its session, the prefix replayed, the seeded positions."""

    stem: str
    worker: int = 0
    session_id: str = ""
    theory_name: str = ""
    acquire_s: float = 0.0
    acquire_error: Optional[str] = None
    enter_s: float = 0.0
    enter_error: Optional[str] = None
    prefix_blocks: int = 0
    prefix_proved: int = 0
    prefix_wall_s: float = 0.0
    positions: List[PositionRecord] = field(default_factory=list)
    blocks: List[Dict[str, Any]] = field(
        default_factory=list
    )  # keep_blocks: every corpus block
    stopped: Optional[str] = None  # prover_died | session_gone: the file ended there
    unclosed: List[Dict[str, Any]] = field(default_factory=list)
    t_start: float = 0.0
    t_close: Optional[float] = None  # just before the session is closed (0268dbe B1)
    t_end: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        """JSON form."""
        return asdict(self)


DEAD = ("prover_died", "session_gone")  # the session is gone: the file stops
# after each attempt, no call on the session for this long (outside every timed
# span), so the 1 Hz sampler has a row of the prover as the attempt left it
SETTLE_S = 1.5


def classify(rec: BlockRecord) -> str:
    """memory / prover_died / session_gone (their own classes, review beac25c
    S1), timed_out (the bound fired), error (no report at all), kept (the seed
    stayed in the document: success, or an open proof), failed (ended with an
    error and was discarded)."""
    if rec.error_kind in ("memory",) + DEAD:
        return str(rec.error_kind)
    if rec.timed_out:
        return "timed_out"
    if rec.n_commands is None:
        return "error"
    if rec.success or rec.proof_open:
        return "kept"
    return "failed"


async def _source_hash(client: Any, sess: SessionRef) -> Optional[str]:
    try:
        res = await client.get_source(sess.session_id, lease_id=sess.lease_id)
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    return sha256_text(str((res or {}).get("source", "")))


async def session_memory(client: Any, session_id: str) -> Optional[Dict[str, Any]]:
    """The session's row of ``GET /api/v1/memory`` (rss_mb, heap_mb and when
    they were sampled: the probe runs after a call at most every 15 s) and the
    server's tier and used memory (review beac25c S2)."""
    try:
        m = await client.memory_status()
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    rows: List[Dict[str, Any]] = list(m.get("sessions") or [])
    row: Dict[str, Any] = next(
        (r for r in rows if r.get("session_id") == session_id), {}
    )
    return {
        "state": m.get("state"),
        "used_mb": (m.get("memory") or {}).get("used_mb"),
        "rss_mb": row.get("rss_mb"),
        "heap_mb": row.get("heap_mb"),
        "sampled_at": row.get("mem_sampled_at"),
    }


async def attempt(
    client: Any, sess: SessionRef, seed: Seed, budget_s: float, before: Optional[str]
) -> SeedAttempt:
    """One seed under ``budget_s``; a kept seed is rolled back (7bedac5 S2)."""
    mem_before = await session_memory(client, sess.session_id)
    rec = await verify_block(
        client,
        sess,
        seed.text_for(budget_s),
        stem=seed.stem,
        index=seed.position,
        block_sha=f"seed:{seed.seed_id}",
        census_commands=None,
        budget_s=budget_s,
    )
    out = SeedAttempt(
        seed_id=seed.seed_id,
        family=seed.family,
        goal_source=seed.goal_source,
        budget_s=budget_s,
        outcome=classify(rec),
        wall_s=rec.wall_s,
        undo_s=round(rec.wall_s - budget_s, 4) if rec.timed_out else None,
        stuck_line=rec.stuck_line,
        n_commands=rec.n_commands,
        statuses=rec.statuses,
        error=rec.error,
        source_unchanged=None,
        t_start=rec.t_start,
        error_kind=rec.error_kind,
        first_error=rec.first_error,
        mem_before=mem_before,
    )
    if out.outcome in DEAD:
        return out
    await asyncio.sleep(SETTLE_S)
    out.t_settled = time.time()
    if out.outcome == "kept":
        try:
            await client.rollback(sess.session_id, lease_id=sess.lease_id)
            out.rolled_back = True
        except Exception as exc:  # pylint: disable=broad-exception-caught
            out.error = f"rollback failed: {type(exc).__name__}: {exc}"[:300]
    after = await _source_hash(client, sess)
    if before is not None and after is not None:
        out.source_unchanged = before == after
    out.mem_after = await session_memory(client, sess.session_id)
    return out


async def run_position(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    client: Any,
    sess: SessionRef,
    cf: CorpusFile,
    at: List[Seed],
    ladder: BudgetLadder,
    census_file: Dict[str, Any],
) -> PositionRecord:
    """The seeds at one position (smallest budget; the larger ones for seeds
    that looped), then the corpus block they precede."""
    i = at[0].position
    row = (census_file.get("blocks") or [])[i]
    pos = PositionRecord(
        stem=cf.stem,
        position=i,
        census_proved=bool(row.get("proved")),
        census_wall_s=row.get("wall_s"),
    )
    before = await _source_hash(client, sess)
    for seed in at:
        b: Optional[float] = ladder.first(seed.family)
        while b is not None:  # one budget at a time (review beac25c S4)
            a = await attempt(client, sess, seed, b, before)
            pos.attempts.append(a)
            if a.outcome in DEAD:
                pos.stopped = a.outcome
                return pos
            b = ladder.next_budget(
                seed.seed_id, seed.family, b, a.outcome == "timed_out"
            )
    blk = await verify_in_file(client, sess, cf, i, row)
    pos.block = asdict(blk)
    if blk.error_kind in DEAD:
        pos.stopped = blk.error_kind
        return pos
    pos.verdict_match = blk.proved == pos.census_proved
    pos.recovery_s = blk.wall_s
    return pos


async def run_file(  # pylint: disable=too-many-arguments,too-many-locals
    client: Any,
    cf: CorpusFile,
    file_seeds: List[Seed],
    ladder: BudgetLadder,
    census_file: Dict[str, Any],
    *,
    heap: str,
    registry: Dict[str, str],
    on_change: Callable[[Dict[str, str]], Any],
    worker: int = 0,
    to_end: bool = False,
    keep_blocks: bool = False,
) -> T5FileRecord:
    """Acquire (with the file's imports), enter, replay up to the last seeded
    position with the seeds in place (``to_end``: the whole file, as T1 does;
    ``keep_blocks``: every corpus block's record, for the contention cell),
    close."""
    rec = T5FileRecord(stem=cf.stem, worker=worker, t_start=time.time())
    t0 = time.monotonic()
    try:  # a stop (CancelledError) propagates; the acquire closes what it got
        acq, _retries, _waited = await _acquire_retrying(
            client, cf, heap, "trainmetrics-t5"
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        rec.acquire_error = f"{type(exc).__name__}: {exc}"[:300]
        rec.t_end = time.time()
        return rec
    rec.acquire_s = round(time.monotonic() - t0, 4)
    sess = SessionRef(str(acq["session_id"]), str(acq["lease_id"]))
    rec.session_id = sess.session_id
    registry[sess.session_id] = sess.lease_id
    on_change(registry)
    try:
        rec.theory_name = sess.new_name(cf.stem)
        t1 = time.monotonic()
        try:
            await client.enter_theory(
                sess.session_id,
                rec.theory_name,
                imports=list(cf.imports),
                lease_id=sess.lease_id,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            rec.enter_error = f"{type(exc).__name__}: {exc}"[:300]
            return rec
        rec.enter_s = round(time.monotonic() - t1, 4)
        by_pos: Dict[int, List[Seed]] = {}
        for sd in file_seeds:
            by_pos.setdefault(sd.position, []).append(sd)
        rows = census_file.get("blocks") or []
        last = len(cf.blocks) - 1 if to_end or not by_pos else max(by_pos)
        for i in range(last + 1):
            if i in by_pos:
                rec.positions.append(
                    await run_position(client, sess, cf, by_pos[i], ladder, census_file)
                )
                if keep_blocks and rec.positions[-1].block is not None:
                    rec.blocks.append(dict(rec.positions[-1].block, after_seed=True))
                rec.stopped = rec.positions[-1].stopped
                if rec.stopped:  # a dead session: no more rollouts on it (B1)
                    break
                continue
            row = rows[i] if i < len(rows) else {}
            blk = await verify_in_file(client, sess, cf, i, row)
            rec.prefix_blocks += 1
            rec.prefix_proved += int(blk.proved)
            rec.prefix_wall_s = round(rec.prefix_wall_s + blk.wall_s, 4)
            if keep_blocks:
                rec.blocks.append(asdict(blk))
            if blk.error_kind in DEAD:
                rec.stopped = blk.error_kind
                break
    finally:
        rec.t_close = time.time()
        rec.unclosed = await close_all(client, [sess])
        if not rec.unclosed:
            registry.pop(sess.session_id, None)
        on_change(registry)
        rec.t_end = time.time()
    return rec
