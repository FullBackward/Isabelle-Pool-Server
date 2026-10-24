"""T6b step-level paths (design §T6b, plan stage 7): at a problem the worker
proves the theorem command by command, as Isabelle splits it (the census's
command spans, stage 3d), and branches at every step.

A path: a checkpoint, the statement, then per proof command a checkpoint,
``per_step`` alternatives drawn (seeded) from ``alternatives`` under
``budget_s`` each — a kept one undone by restoring the step's checkpoint, a
failed one (e.g. ``apply`` in Isar state mode) discarded by the server — and
the corpus command. One path is one rollout (a ``path`` event, a summary of
its calls, not a call itself). A proved path leaves the theorem in the
document (the walk skips its advance); a path that is not proved is undone
by restoring the path's checkpoint, and the corpus block advances as in T6a.

Integrity (S1, as T6a's: never between a restore and a loop call): the node
text is read at the path's start and end, both after settled verifies; the
end text must be the start text plus the path's pieces (a proved path) or
the start text (an undone one), compared with whitespace normalised — the
server inserts each chunk plus a newline (Edit_Utils.insert_text_edit) and
the source route strips the node text."""

from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .corpus import CorpusFile
from .mutate import DOC
from .replay import BlockRecord, verify_block
from .seeds import split_statement
from .t5_core import DEAD
from .t6_core import KEPT, FileCtx, Group, outcome

ALTERNATIVES = ("apply auto", "apply simp", "apply -", "by auto", "by simp")


def steps_of(
    block: str, spans: Optional[Sequence[Dict[str, Any]]]
) -> Optional[Tuple[str, List[Tuple[str, str]]]]:
    """(statement text, [(kind, text)] proof steps) of a block, cut at each
    command's start; a document command is merged into the piece before it;
    None without spans or when the pieces do not re-assemble the block."""
    if not spans or len(spans) < 2:
        return None
    cuts = [0] + [int(s["start"]) for s in spans[1:]]
    pieces = [block[a:b] for a, b in zip(cuts, cuts[1:] + [len(block)])]
    if "".join(pieces) != block:
        return None
    stmt = pieces[0]
    steps: List[Tuple[str, str]] = []
    for span, piece in zip(spans[1:], pieces[1:]):
        kind = str(span.get("kind") or "")
        if kind in DOC:
            if steps:
                steps[-1] = (steps[-1][0], steps[-1][1] + piece)
            else:
                stmt += piece
        else:
            steps.append((kind, piece))
    return stmt, steps


def problems_t6b(
    cf: CorpusFile, census_file: Dict[str, Any], n: int, lo: int, hi: int
) -> List[int]:
    """The first ``n`` theorem blocks the census proved whose proof has ``lo``
    to ``hi`` commands (S4: a longer proof would take an hour on its own)."""
    out: List[int] = []
    for i, row in enumerate(census_file.get("blocks") or []):
        if not row.get("proved") or i >= len(cf.blocks) or row.get("spans_error"):
            continue
        if not split_statement(cf.blocks[i]):
            continue
        split = steps_of(cf.blocks[i], row.get("spans"))
        if split and lo <= len(split[1]) <= hi:
            out.append(i)
    return out[:n]


def _norm(text: str) -> str:
    return " ".join(text.split())


async def _text(ctx: FileCtx, i: int, at: str, expect: Optional[str] = None) -> str:
    """The node text, timed as an ``integrity`` event (``ok`` against
    ``expect``, whitespace normalised, when given)."""
    t_start, t0 = time.time(), time.monotonic()
    try:
        res = await ctx.client.get_source(
            ctx.sess.session_id, lease_id=ctx.sess.lease_id
        )
        text = str((res or {}).get("source", ""))
    except Exception:  # pylint: disable=broad-exception-caught
        text = ""
    ok = None if expect is None else _norm(text) == _norm(expect)
    ctx.add("integrity", t_start, time.monotonic() - t0, index=i, at=at, ok=ok)
    return text


class _Path:
    """One path's calls (module docstring)."""

    def __init__(self, ctx: FileCtx, i: int, budget_s: float) -> None:
        self.ctx, self.i, self.budget_s = ctx, i, budget_s
        self.row = ctx.rows[i]

    async def checkpoint(self, **kw: Any) -> int:
        """A checkpoint event; its id."""
        c = self.ctx
        cp = await c.call(
            "checkpoint",
            c.client.save_checkpoint(c.sess.session_id, lease_id=c.sess.lease_id),
            index=self.i,
            **kw,
        )
        return int(cp["checkpoint_id"])

    async def restore(self, cp: int, **kw: Any) -> bool:
        """A restore event; whether it applied."""
        c = self.ctx
        res = await c.call(
            "restore",
            c.client.restore_checkpoint(
                c.sess.session_id, cp, lease_id=c.sess.lease_id
            ),
            index=self.i,
            **kw,
        )
        return bool(res.get("success"))

    async def verify(self, text: str, kind: str, step: int, **kw: Any) -> BlockRecord:
        """A corpus piece under the block's census budget, or an alternative
        (``kind`` = alternative) under the alternatives' budget."""
        c = self.ctx
        alt = kind == "alternative"
        blk = await verify_block(
            c.client,
            c.sess,
            text,
            stem=c.cf.stem,
            index=self.i,
            block_sha=f"t6b:{kind}:{step}",
            census_commands=None if alt else self.row.get("n_commands"),
            census_partial=False if alt else bool(self.row.get("count_partial")),
            budget_s=self.budget_s if alt else None,
        )
        c.verify_event(kind, blk, step=step, **kw)
        return blk

    async def steps(
        self, steps: List[Tuple[str, str]], picks: List[List[str]]
    ) -> Tuple[Optional[str], str]:
        """Every step with its alternatives; (stop reason, path outcome)."""
        result = "proved"
        for k, ((kind, text), alts) in enumerate(zip(steps, picks), start=1):
            cp = await self.checkpoint(role="step", step=k)
            for a in alts:
                blk = await self.verify(a, "alternative", k, candidate=a)
                if blk.error_kind in DEAD:
                    return blk.error_kind, outcome(blk)
                if outcome(blk) in KEPT and not await self.restore(cp, step=k):
                    return "restore_failed", "error"
            blk = await self.verify(text, "step", k, role="step", command=kind)
            result = outcome(blk)
            if blk.error_kind in DEAD:
                return blk.error_kind, result
            if result not in KEPT:
                return None, result
        return None, result

    async def run_path(
        self, stmt: str, steps: List[Tuple[str, str]], picks: List[List[str]]
    ) -> Tuple[Optional[str], str]:
        """The path from its checkpoint, as one ``path`` event; (stop reason,
        outcome). A path not proved is undone by restoring its checkpoint."""
        t_start, t0 = time.time(), time.monotonic()
        cp0 = await self.checkpoint(role="path")
        st = await self.verify(stmt, "step", 0, role="statement")
        stop, result = None, outcome(st)
        if st.error_kind in DEAD:
            stop = st.error_kind
        elif result in KEPT:
            stop, result = await self.steps(steps, picks)
            if (
                not stop
                and result != "proved"
                and not await self.restore(cp0, role="path")
            ):
                stop = "restore_failed"
        self.ctx.add(
            "path",
            t_start,
            time.monotonic() - t0,
            index=self.i,
            outcome=result,
            steps=len(steps),
            summary=True,
        )
        return stop, result


async def _end_ok(ctx: FileCtx, i: int, start: str, pieces: List[str]) -> bool:
    """The path-end read against the start text plus ``pieces``."""
    expect = start + "\n" + "\n".join(pieces)
    end = await _text(ctx, i, "path_end", expect=expect)
    return _norm(end) == _norm(expect)


def group_t6b(alternatives: Sequence[str], per_step: int, budget_s: float) -> Group:
    """T6b's group at a problem (module docstring)."""

    async def run(ctx: FileCtx, i: int) -> Optional[str]:
        split = steps_of(ctx.cf.blocks[i], ctx.rows[i].get("spans"))
        if split is None:
            raise ValueError(f"{ctx.cf.stem}#{i} has no command spans")
        stmt, steps = split
        rng = random.Random(f"{ctx.seed}:{ctx.cf.stem}:{i}")
        picks = [rng.sample(list(alternatives), per_step) for _ in steps]
        check = rng.random() < ctx.integrity_p
        start = await _text(ctx, i, "path_start") if check else None
        stop, result = await _Path(ctx, i, budget_s).run_path(stmt, steps, picks)
        if stop:
            return stop
        ctx.in_document = result == "proved"
        pieces = [stmt] + [text for _k, text in steps] if ctx.in_document else []
        if start is not None and not await _end_ok(ctx, i, start, pieces):
            return "integrity_failed"
        ctx.rec.groups_done += 1
        return None

    return run
