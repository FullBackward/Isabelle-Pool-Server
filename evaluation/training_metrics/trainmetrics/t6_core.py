"""T6 rollouts, the machinery one worker runs per file (design §T6, plan
stage 6). A file is replayed on a session acquired with its imports (D6):
the blocks before a problem advance the context (census verdict checked), and
at each problem a *group* runs (T6a: the D9 candidates, stage 6; T6b: a path,
stage 7), then the corpus block advances.

Every call is an *event* ``{worker, stem, kind, t_start, wall_s, ...}`` in one
list for the cell — kinds ``acquire``, ``enter``, ``advance`` (``phase``
``prefix`` before the file's first problem), ``checkpoint``, ``rollout``,
``restore``, ``integrity``, ``rollback`` (an advance kept unproved, undone
before its retry) and ``close`` (the session's, review f0d85df S2) — so the
rates and time shares are computed from the timestamps
(:mod:`trainmetrics.metrics_t6`).

- **Outcome classes (plan_v1.6 B1):** a rollout is ``proved``,
  ``kept_unproved`` (undone by a restore), ``failed`` or ``timed_out``;
  ``memory``, ``prover_died``, ``session_gone`` and ``error`` are their own
  classes outside the rollout counts. A dead session (prover died, or 404)
  stops the file, as does a restore that fails.
- **Advance (S5):** a corpus block the census proved that does not prove is
  retried once at the maximum budget (600 s); if it still does not prove the
  file stops with ``advance_failed``.
- **Integrity (S1, stage 6a):** the node source hash before the checkpoint
  and again at the group's end, after its last candidate (and restore): they
  must be equal, so any text a restore or a discard left behind shows; if not,
  the file stops (``integrity_failed``) and the group's rollouts are counted
  apart (review f0d85df S1). Every group at W = 1, a seeded quarter at
  W > 1; its time is its own kind. Not
  after each restore: a source read waits for a stable snapshot, so right
  after a restore it takes ~0.11 s against 4-9 ms settled (stage 6a live),
  time the next verify mostly does not pay (T3: -14 +- 19 ms on short
  blocks) but pays in part on long ones; read there, it would shift time into
  or out of the loop. At the group's end it sits between the last loop call
  and the advance, both outside the loop time."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from . import mutate
from .corpus import CorpusFile
from .replay import (
    BUDGET_MAX_S,
    BlockRecord,
    SessionRef,
    _acquire_retrying,
    open_session,
    verify_block,
    verify_in_file,
)
from .seeds import split_statement
from .t5_core import DEAD, _source_hash, classify

COUNTED = ("proved", "kept_unproved", "failed", "timed_out")
KEPT = ("proved", "kept_unproved")


def outcome(rec: BlockRecord) -> str:
    """The rollout's class (module docstring)."""
    c = classify(rec)
    if c == "kept":
        return "proved" if rec.proved else "kept_unproved"
    return c


def problems_of(cf: CorpusFile, census_file: Dict[str, Any], n: int) -> List[int]:
    """The first ``n`` theorem blocks of the file the census proved."""
    rows = census_file.get("blocks") or []
    return [
        i
        for i, row in enumerate(rows)
        if row.get("proved") and i < len(cf.blocks) and split_statement(cf.blocks[i])
    ][:n]


@dataclass
class FileRecord:  # pylint: disable=too-many-instance-attributes
    """One file on one worker."""

    worker: int
    stem: str
    problems: List[int] = field(default_factory=list)
    groups_done: int = 0
    session_id: str = ""
    acquire_retries: int = 0
    acquire_wait_s: float = 0.0
    acquire_error: Optional[str] = None
    enter_error: Optional[str] = None
    stopped: Optional[str] = None  # why the file stopped early
    t_start: float = 0.0
    t_end: float = 0.0
    unclosed: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class FileCtx:  # pylint: disable=too-many-instance-attributes
    """What a group needs: the session, the file, its census rows, where the
    events go, and the integrity sampling."""

    client: Any
    sess: SessionRef
    cf: CorpusFile
    rows: List[Dict[str, Any]]
    rec: FileRecord
    events: List[Dict[str, Any]]
    seed: int
    integrity_p: float  # 1.0 at W = 1
    # set by a group that leaves the corpus block in the document (a proved
    # T6b path): the walk then skips the block's advance
    in_document: bool = False

    def add(self, kind: str, t_start: float, wall_s: float, **kw: Any) -> None:
        """One event of this file."""
        self.events.append(
            {
                "worker": self.rec.worker,
                "stem": self.cf.stem,
                "kind": kind,
                "t_start": round(t_start, 4),
                "wall_s": round(wall_s, 4),
                **kw,
            }
        )

    async def call(self, kind: str, coro: Awaitable[Any], **kw: Any) -> Any:
        """``coro`` timed as one event; its result."""
        t_start, t0 = time.time(), time.monotonic()
        out = await coro
        self.add(kind, t_start, time.monotonic() - t0, **kw)
        return out

    def verify_event(self, kind: str, blk: BlockRecord, **kw: Any) -> None:
        """A verify call as an event (the server's execution time beside the wall)."""
        self.add(
            kind,
            blk.t_start,
            blk.wall_s,
            index=blk.index,
            proved=blk.proved,
            outcome=outcome(blk),
            budget_s=blk.budget_s,
            execution_time=blk.execution_time,
            error_kind=blk.error_kind,
            first_error=(blk.first_error or "")[:200] or None,
            **kw,
        )

    async def source(
        self, expect: Optional[str] = None, check: bool = False, **kw: Any
    ) -> Optional[str]:
        """The node source hash, timed as an ``integrity`` event; with
        ``check``, the event's ``ok`` says whether it equals ``expect``."""
        t_start, t0 = time.time(), time.monotonic()
        h = await _source_hash(self.client, self.sess)
        ok = (h is not None and h == expect) if check else None
        self.add("integrity", t_start, time.monotonic() - t0, ok=ok, **kw)
        return h


Group = Callable[[FileCtx, int], Awaitable[Optional[str]]]


async def advance(ctx: FileCtx, i: int, phase: str) -> Optional[str]:
    """The corpus block i; why the file stops, if it does (module docstring)."""
    row = ctx.rows[i] if i < len(ctx.rows) else {}
    blk = await verify_in_file(ctx.client, ctx.sess, ctx.cf, i, row)
    ctx.verify_event("advance", blk, phase=phase, census_proved=bool(row.get("proved")))
    if blk.error_kind in DEAD:
        return blk.error_kind
    if blk.proved or not row.get("proved"):
        return None
    if classify(blk) == "kept":  # in the document but not proved: undo first
        await ctx.call(
            "rollback",
            ctx.client.rollback(ctx.sess.session_id, lease_id=ctx.sess.lease_id),
            index=i,
        )
    retry = await verify_block(
        ctx.client,
        ctx.sess,
        ctx.cf.blocks[i],
        stem=ctx.cf.stem,
        index=i,
        block_sha=ctx.cf.block_sha[i],
        census_commands=row.get("n_commands"),
        budget_s=BUDGET_MAX_S,
    )
    ctx.verify_event("advance", retry, phase=phase, census_proved=True, retried=True)
    if retry.error_kind in DEAD:
        return retry.error_kind
    return None if retry.proved else "advance_failed"


def candidates_t6a(block: str) -> List[Tuple[str, Optional[str]]]:
    """The D9 mix: (kind, text after nothing; None where the kind does not
    apply). The corpus proof, ``by auto`` and ``by simp`` twice each,
    ``sorry``, T4's truncation, and a method that fails at once."""
    split = split_statement(block)
    if split is None:
        raise ValueError("not a theorem block")
    stmt = split[0]
    auto, simp = f"{stmt}\n  by auto", f"{stmt}\n  by simp"
    return [
        ("corpus", block),
        ("by_auto", auto),
        ("by_auto", auto),
        ("by_simp", simp),
        ("by_simp", simp),
        ("sorry", mutate.with_sorry(block)),
        ("truncated", mutate.truncated(block)),
        ("false_e", f"{stmt}\n  by (rule FalseE)"),
    ]


def group_t6a(budget_s: float) -> Group:
    """T6a's group at a problem: checkpoint, the candidates in a seeded order
    (the corpus proof under the census rule, the others under ``budget_s``),
    a kept one undone by restoring the checkpoint."""

    async def run(ctx: FileCtx, i: int) -> Optional[str]:
        rng = random.Random(f"{ctx.seed}:{ctx.cf.stem}:{i}")
        cands = candidates_t6a(ctx.cf.blocks[i])
        rng.shuffle(cands)
        check = rng.random() < ctx.integrity_p
        state = await ctx.source(index=i, at="checkpoint") if check else None
        cp = await ctx.call(
            "checkpoint",
            ctx.client.save_checkpoint(ctx.sess.session_id, lease_id=ctx.sess.lease_id),
            index=i,
        )
        for order, (kind, text) in enumerate(cands):
            if text is None:
                ctx.add(
                    "rollout",
                    time.time(),
                    0.0,
                    index=i,
                    candidate=kind,
                    order=order,
                    outcome="not_applicable",
                )
                continue
            if kind == "corpus":
                blk = await verify_in_file(ctx.client, ctx.sess, ctx.cf, i, ctx.rows[i])
            else:
                blk = await verify_block(
                    ctx.client,
                    ctx.sess,
                    text,
                    stem=ctx.cf.stem,
                    index=i,
                    block_sha=f"t6a:{kind}",
                    census_commands=None,
                    budget_s=budget_s,
                )
            ctx.verify_event("rollout", blk, candidate=kind, order=order)
            if blk.error_kind in DEAD:
                return blk.error_kind
            if outcome(blk) not in KEPT:
                continue
            res = await ctx.call(
                "restore",
                ctx.client.restore_checkpoint(
                    ctx.sess.session_id,
                    int(cp["checkpoint_id"]),
                    lease_id=ctx.sess.lease_id,
                ),
                index=i,
            )
            if not res.get("success"):
                return "restore_failed"
        if check:
            end = await ctx.source(expect=state, check=True, index=i, at="group_end")
            if end is None or end != state:  # the document is not what the loop assumes
                return "integrity_failed"
        ctx.rec.groups_done += 1
        return None

    return run


async def _walk(ctx: FileCtx, group: Group) -> Optional[str]:
    """Enter, then advance and run the groups up to the last problem."""
    try:
        await ctx.call(
            "enter",
            ctx.client.enter_theory(
                ctx.sess.session_id,
                ctx.sess.new_name(ctx.cf.stem),
                imports=list(ctx.cf.imports),
                lease_id=ctx.sess.lease_id,
            ),
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        ctx.rec.enter_error = f"{type(exc).__name__}: {exc}"[:300]
        return None
    probs = set(ctx.rec.problems)
    for i in range(max(ctx.rec.problems) + 1):
        if i in probs:
            ctx.in_document = False
            stop = await group(ctx, i)
            if stop:
                return stop
            if ctx.in_document:
                continue
        stop = await advance(ctx, i, "prefix" if i < min(probs) else "advance")
        if stop:
            return stop
    return None


@dataclass
class CellCtx:  # pylint: disable=too-many-instance-attributes
    """What every file of a cell shares."""

    client: Any
    group: Group
    problems: Callable[[CorpusFile, Dict[str, Any]], List[int]]  # the style's
    heap: str
    seed: int
    integrity_p: float
    events: List[Dict[str, Any]]
    registry: Dict[str, str]
    on_change: Callable[[Dict[str, str]], Any]


async def _acquire(
    cell: CellCtx, cf: CorpusFile, rec: FileRecord
) -> Optional[Dict[str, Any]]:
    """The file's session (refusals retried, as T1), as an ``acquire`` event;
    None when it could not be had."""
    t_start, t0 = time.time(), time.monotonic()
    acq: Optional[Dict[str, Any]] = None
    try:  # a stop (CancelledError) propagates; the acquire closes what it got
        acq, rec.acquire_retries, rec.acquire_wait_s = await _acquire_retrying(
            cell.client, cf, cell.heap, f"trainmetrics-t6-w{rec.worker}"
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        rec.acquire_error = f"{type(exc).__name__}: {exc}"[:300]
    cell.events.append(
        {
            "worker": rec.worker,
            "stem": cf.stem,
            "kind": "acquire",
            "t_start": round(t_start, 4),
            "wall_s": round(time.monotonic() - t0, 4),
            "retries": rec.acquire_retries,
            "wait_s": rec.acquire_wait_s,
            "error": rec.acquire_error,
        }
    )
    return acq


async def run_file(
    cell: CellCtx, cf: CorpusFile, census_file: Dict[str, Any], worker: int
) -> FileRecord:
    """One file on a session of its own: acquire, enter, walk, close."""
    rec = FileRecord(worker=worker, stem=cf.stem, t_start=time.time())
    rec.problems = cell.problems(cf, census_file)
    acq = await _acquire(cell, cf, rec) if rec.problems else None
    if acq is None:
        rec.t_end = time.time()
        return rec
    mark: List[float] = []  # when the close began (wall clock, monotonic)
    try:
        async with open_session(
            cell.client, acq, cell.registry, cell.on_change, rec.unclosed
        ) as sess:
            rec.session_id = sess.session_id
            ctx = FileCtx(
                cell.client,
                sess,
                cf,
                census_file.get("blocks") or [],
                rec,
                cell.events,
                cell.seed,
                cell.integrity_p,
            )
            try:
                rec.stopped = await _walk(ctx, cell.group)
            finally:
                mark += [time.time(), time.monotonic()]
    except Exception as exc:  # pylint: disable=broad-exception-caught
        rec.stopped = f"error: {type(exc).__name__}: {exc}"[:300]
    if mark:
        cell.events.append(
            {
                "worker": worker,
                "stem": cf.stem,
                "kind": "close",
                "t_start": round(mark[0], 4),
                "wall_s": round(time.monotonic() - mark[1], 4),
            }
        )
    rec.t_end = time.time()
    return rec
