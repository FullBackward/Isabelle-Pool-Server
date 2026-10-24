"""T3 branch cost (design §T3, plan stage 5): what does it cost to branch at
a proof state, against the alternatives?

A state is the document after block ``i`` of a file F. ``states`` (F, i)
pairs are sampled, seeded, in three position strata (the early, middle and
late third of the file's blocks: replay cost grows with position), among the
states whose continuation (block i + 1) the census proved. Each state runs on
a session created for it (``POST /sessions`` with the file's imports: plan
review S6 hygiene, D6), at W = 1:

- (f) ``create_session`` + ``enter_theory`` + replay blocks 0..i: the
  fresh-process alternative, and the way to the state;
- (a) ``save_checkpoint``;
- (b) verify block i + 1 (the continuation): the first *settled* verify;
- (c) ``restore_repeats`` times: ``restore_checkpoint``, then block i + 1
  *at once* (nothing between the two calls); the source is read after that
  verify and must equal the source after (b);
- (d) ``settled_repeats - 1`` times: ``rollback`` (it waits for the node to
  settle), the source checked against the state's, then block i + 1 again: a
  further settled verify;
- (e) ``enter_theory`` with a fresh name on the same (warm) session + replay
  blocks 0..i: the same-prover alternative (design B1);

then the session is closed. Every repeated verify's report must equal (b)'s.
The distance between checkpoint and head is the census's command count of
block i + 1 (plan review S6). A restore goes back (it removes the
continuation's text) and returns without waiting for the prover
(``Repl_Session.restore_state`` sends the edit), so the work it leaves shows
in the next call (review 07120bc B1: a source read in between absorbed it,
~150 ms against ~10 ms reported). The branch cost of a state is therefore its
restore plus (verify at once - settled verify), medians of the repeats; the
restore's own time is reported apart, as the time it takes to return.

    python -m trainmetrics.run_t3 [--local-smoke] [--ips-external]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .census import load_census, load_config
from .corpus import CorpusFile, corpus_sha, load_corpus, load_paths
from .harness import cell_arg_parser, cli_extra, open_registry, run_cli_cell
from .metrics_t3 import STRATA, summarize_t3
from .replay import (
    BlockRecord,
    SessionRef,
    _acquire_retrying,
    open_session,
    verify_in_file,
)
from .run_t1 import usable_stems
from .t5_core import DEAD, _source_hash


def stratum(i: int, n_blocks: int) -> str:
    """The third of the file's blocks that block ``i`` sits in."""
    return STRATA[min(2, 3 * i // max(1, n_blocks))]


def sample_states(
    files: Sequence[CorpusFile], census: Dict[str, Any], n: int, rng_seed: int
) -> List[Tuple[str, int]]:
    """``n`` (stem, i) states, seeded, split evenly over the strata (the
    remainder to the first ones), among the states whose block i + 1 the census
    proved; in a seeded run order, so drift over the run is not aligned with
    position."""
    rows = {f["stem"]: f.get("blocks") or [] for f in census.get("files", [])}
    pools: Dict[str, List[Tuple[str, int]]] = {s: [] for s in STRATA}
    for cf in files:
        blocks = rows.get(cf.stem) or []
        n_blocks = len(cf.blocks)
        for i in range(n_blocks - 1):
            if i + 1 < len(blocks) and blocks[i + 1].get("proved"):
                pools[stratum(i, n_blocks)].append((cf.stem, i))
    rng = random.Random(rng_seed)
    picked: List[Tuple[str, int]] = []
    for k, s in enumerate(STRATA):
        want = n // len(STRATA) + (1 if k < n % len(STRATA) else 0)
        picked += rng.sample(pools[s], min(want, len(pools[s])))
    rng.shuffle(picked)
    return picked


@dataclass
class Replay:
    """``enter_theory`` + blocks 0..i on one node."""

    theory_name: str
    enter_s: float = 0.0
    replay_s: float = 0.0
    blocks: int = 0
    proved: int = 0
    error: Optional[str] = None


@dataclass
class StateRecord:  # pylint: disable=too-many-instance-attributes
    """One state: the timings of (a)-(f) and the integrity checks."""

    stem: str
    index: int
    stratum: str
    n_blocks: int
    distance: Optional[int]  # census commands of block i + 1
    prefix_commands: int  # census commands of blocks 0..i
    census_prefix_proved: int
    session_id: str = ""
    create_s: Optional[float] = None
    create_retries: int = 0
    create_wait_s: float = 0.0
    fresh: Optional[Dict[str, Any]] = None  # (f) after the create
    save_s: Optional[float] = None
    cont: Optional[Dict[str, Any]] = None  # (b), the first settled verify
    # (c): restore_s, restore_ok, message, again (the verify at once), source_ok
    restores: List[Dict[str, Any]] = field(default_factory=list)
    # (d): rollback_s, rollback_ok, source_ok, settled (the verify after it)
    rollbacks: List[Dict[str, Any]] = field(default_factory=list)
    warm: Optional[Dict[str, Any]] = None  # (e)
    stopped: Optional[str] = None  # the session died or is gone; or why it was not run
    error: Optional[str] = None
    t_start: float = 0.0
    t_end: float = 0.0
    unclosed: List[Dict[str, Any]] = field(default_factory=list)


def _view(rec: BlockRecord) -> Dict[str, Any]:
    keys = ("proved", "success", "n_commands", "statuses", "first_error")
    return {
        **{k: getattr(rec, k) for k in keys},
        "wall_s": rec.wall_s,
        "execution_time": rec.execution_time,
        "error_kind": rec.error_kind,
    }


async def _timed(coro: Any) -> Tuple[Any, float]:
    t0 = time.monotonic()
    out = await coro
    return out, round(time.monotonic() - t0, 4)


async def replay_prefix(
    client: Any, sess: SessionRef, cf: CorpusFile, rows: List[Dict[str, Any]], i: int
) -> Tuple[Replay, Optional[str]]:
    """A new node for ``cf`` and its blocks 0..i; (record, why the session is
    gone if it is)."""
    rp = Replay(theory_name=sess.new_name(cf.stem))
    try:
        _, rp.enter_s = await _timed(
            client.enter_theory(
                sess.session_id,
                rp.theory_name,
                imports=list(cf.imports),
                lease_id=sess.lease_id,
            )
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        rp.error = f"enter: {type(exc).__name__}: {exc}"[:300]
        return rp, None
    t0 = time.monotonic()
    for k in range(i + 1):
        blk = await verify_in_file(
            client, sess, cf, k, rows[k] if k < len(rows) else {}
        )
        rp.blocks += 1
        rp.proved += int(blk.proved)
        if blk.error_kind in DEAD:
            rp.replay_s = round(time.monotonic() - t0, 4)
            return rp, blk.error_kind
    rp.replay_s = round(time.monotonic() - t0, 4)
    return rp, None


@dataclass
class _Branch:
    """The calls at one state: block i + 1 of ``cf`` on ``sess``, recorded
    on ``rec``."""

    client: Any
    sess: SessionRef
    cf: CorpusFile
    row: Dict[str, Any]
    rec: StateRecord

    async def verify(self) -> Dict[str, Any]:
        """Block i + 1 once (``rec.stopped`` set when the session is gone)."""
        blk = await verify_in_file(
            self.client, self.sess, self.cf, self.rec.index + 1, self.row
        )
        if blk.error_kind in DEAD:
            self.rec.stopped = blk.error_kind
        return _view(blk)

    def kept(self, view: Dict[str, Any]) -> bool:
        """The block stayed in the document and the session lives."""
        return bool(view.get("success")) and not self.rec.stopped

    async def source(self) -> Optional[str]:
        """The node source's hash (never between a restore and its verify)."""
        return await _source_hash(self.client, self.sess)

    async def restores(self, n: int, cp_id: int, head_src: Optional[str]) -> bool:
        """``n`` times: restore, then the block at once; the source read after
        that verify must equal ``head_src``. False when the loop stopped."""
        for _ in range(n):
            res, restore_s = await _timed(
                self.client.restore_checkpoint(
                    self.sess.session_id, cp_id, lease_id=self.sess.lease_id
                )
            )
            again = await self.verify()  # at once: no call in between
            ok = bool(res.get("success"))
            self.rec.restores.append(
                {
                    "restore_s": restore_s,
                    "restore_ok": ok,
                    "message": None if ok else str(res.get("message"))[:200],
                    "again": again,
                    "source_ok": self.kept(again) and (await self.source()) == head_src,
                }
            )
            if not self.kept(again):
                return False
        return True

    async def rollbacks(self, n: int, state_src: Optional[str]) -> None:
        """``n`` times: rollback (it waits for the node), the source checked
        against the state's, then a settled verify of the block."""
        for _ in range(n):
            rb, rollback_s = await _timed(
                self.client.rollback(self.sess.session_id, lease_id=self.sess.lease_id)
            )
            source_ok = (await self.source()) == state_src
            settled = await self.verify()
            self.rec.rollbacks.append(
                {
                    "rollback_s": rollback_s,
                    "rollback_ok": bool(rb.get("success")),
                    "source_ok": source_ok,
                    "settled": settled,
                }
            )
            if not self.kept(settled):
                return


async def _branch_ops(br: _Branch, repeats: Tuple[int, int]) -> None:
    """(a)-(d) at the state the session is in (the old node ends at the
    state plus block i + 1; (e) enters a new node)."""
    state_src = await br.source()
    cp, br.rec.save_s = await _timed(
        br.client.save_checkpoint(br.sess.session_id, lease_id=br.sess.lease_id)
    )
    br.rec.cont = await br.verify()
    if not br.kept(br.rec.cont):  # a failed block is discarded by verify_chunk
        return
    if await br.restores(repeats[0], int(cp["checkpoint_id"]), await br.source()):
        await br.rollbacks(repeats[1] - 1, state_src)


async def _on_session(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    client: Any,
    sess: SessionRef,
    cf: CorpusFile,
    rows: List[Dict[str, Any]],
    rec: StateRecord,
    repeats: Tuple[int, int],
) -> None:
    """(f)'s replay to the state, (a)-(d), (e) on the created session."""
    i = rec.index
    fresh, rec.stopped = await replay_prefix(client, sess, cf, rows, i)
    rec.fresh = asdict(fresh)
    if rec.stopped or fresh.error:
        return
    await _branch_ops(_Branch(client, sess, cf, rows[i + 1], rec), repeats)
    if not rec.stopped:
        warm, rec.stopped = await replay_prefix(client, sess, cf, rows, i)
        rec.warm = asdict(warm)


def new_record(cf: CorpusFile, i: int, census_file: Dict[str, Any]) -> StateRecord:
    """The state's record with its census facts."""
    rows = census_file.get("blocks") or []
    nxt = rows[i + 1] if i + 1 < len(rows) else {}
    return StateRecord(
        stem=cf.stem,
        index=i,
        stratum=stratum(i, len(cf.blocks)),
        n_blocks=len(cf.blocks),
        distance=nxt.get("n_commands"),
        prefix_commands=sum(int(r.get("n_commands") or 0) for r in rows[: i + 1]),
        census_prefix_proved=sum(1 for r in rows[: i + 1] if r.get("proved")),
    )


async def run_state(  # pylint: disable=too-many-arguments
    client: Any,
    cf: CorpusFile,
    i: int,
    census_file: Dict[str, Any],
    *,
    heap: str,
    registry: Dict[str, str],
    on_change: Callable[[Dict[str, str]], Any],
    repeats: Tuple[int, int] = (3, 3),
) -> StateRecord:
    """One state on a session created for it: (f), (a)-(d), (e); closed.
    ``repeats``: (restore_repeats, settled_repeats)."""
    rec = new_record(cf, i, census_file)
    rows = census_file.get("blocks") or []
    rec.t_start = time.time()
    t0 = time.monotonic()
    try:  # a stop (CancelledError) propagates; the create closes what it got
        acq, rec.create_retries, rec.create_wait_s = await _acquire_retrying(
            client, cf, heap, "trainmetrics-t3", create=True
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        rec.error = f"create: {type(exc).__name__}: {exc}"[:300]
        rec.t_end = time.time()
        return rec
    rec.create_s = round(time.monotonic() - t0, 4)
    try:
        async with open_session(client, acq, registry, on_change, rec.unclosed) as sess:
            rec.session_id = sess.session_id
            await _on_session(client, sess, cf, rows, rec, repeats)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        rec.error = f"{type(exc).__name__}: {exc}"[:300]
    rec.t_end = time.time()
    return rec


def replay_estimate_s(
    picks: Sequence[Tuple[str, int]], census: Dict[str, Any], verifies: int
) -> float:
    """Census replay time the cell repeats: two prefix replays and ``verifies``
    continuations per state (creates and checkpoint calls not included)."""
    rows = {f["stem"]: f.get("blocks") or [] for f in census.get("files", [])}
    total = 0.0
    for stem, i in picks:
        blocks = rows.get(stem) or []
        total += 2 * sum(float(b.get("wall_s") or 0) for b in blocks[: i + 1])
        if i + 1 < len(blocks):
            total += verifies * float(blocks[i + 1].get("wall_s") or 0)
    return round(total, 1)


async def run_cell(  # pylint: disable=too-many-arguments,too-many-locals
    client: Any,
    t3: Dict[str, Any],
    files: Sequence[CorpusFile],
    census: Dict[str, Any],
    *,
    heap: str,
    seed: int,
    out_dir: Path,
    sampler: Any = None,
    body: Optional[Dict[str, Any]] = None,
    checkpoint: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    """Every sampled state, one at a time; fills and returns the manifest body."""
    rows = {f["stem"]: f for f in census.get("files", [])}
    usable = [cf for cf in files if cf.stem in set(usable_stems(files, census))]
    by_stem = {cf.stem: cf for cf in usable}
    picks = sample_states(usable, census, int(t3["states"]), seed)
    repeats = (int(t3.get("restore_repeats", 3)), int(t3.get("settled_repeats", 3)))
    body = body if body is not None else {}
    body.update(
        {
            "experiment": "t3",
            "seed": seed,
            "heap": heap,
            "t3": t3,
            "census_id": census.get("census_id"),
            "corpus_sha": corpus_sha(files),
            "files": sorted({s for s, _ in picks}),
            "picks": picks,
            "census_replay_estimate_s": replay_estimate_s(picks, census, sum(repeats)),
            "outcome": "running",
        }
    )
    print(
        f"t3: {len(picks)} states in {len(body['files'])} files; census replay time "
        f"repeated: about {body['census_replay_estimate_s']:.0f} s plus a session start per state",
        flush=True,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    registry, changed = open_registry(body, checkpoint)
    if sampler is not None:
        sampler.phase("run")
    body["window_start"] = time.time()
    records: List[Dict[str, Any]] = []
    with open(out_dir / "states.jsonl", "w", encoding="utf-8") as fh:
        for stem, i in picks:
            rec = await run_state(
                client,
                by_stem[stem],
                i,
                rows[stem],
                heap=heap,
                registry=registry,
                on_change=changed,
                repeats=repeats,
            )
            records.append(asdict(rec))
            fh.write(json.dumps(records[-1]) + "\n")
            fh.flush()
    body["window_end"] = time.time()
    if sampler is not None:
        sampler.phase("stop")
    body["summary"] = summarize_t3(records)
    body["unclosed_sessions"] = [
        {"session_id": k, "lease_id": v} for k, v in registry.items()
    ]
    body["outcome"] = "ok" if records else "empty"
    return body


async def amain(args: argparse.Namespace) -> int:
    """One T3 cell under the shared harness."""
    cfg = load_config(args.config)
    t3 = dict(cfg["t3"])
    if args.local_smoke:
        files = load_paths(cfg["local_smoke"]["files"])
        heap = cfg["local_smoke"]["field"]
        t3.update(t3.get("local_smoke") or {})
    else:
        files = load_corpus(
            cfg["corpus"], args.files.split(",") if args.files else None
        )
        heap = cfg["session"]["field"]
    if args.states:
        t3["states"] = args.states
    return await run_cli_cell(
        cfg,
        run_cell,
        (t3, files, load_census(args.census)),
        heap=heap,
        seed=int(cfg["seed"]) + args.rep,
        out_dir=Path(args.out) / "t3" / f"rep{args.rep}",
        head={"experiment": "t3", "rep": args.rep},
        extra=cli_extra(cfg, args),
        url=args.url,
        external=bool(args.ips_external),
        label=f"t3 rep={args.rep}",
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """CLI."""
    ap = cell_arg_parser(__doc__)
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--files", default="", help="restrict the corpus to these stems")
    ap.add_argument("--states", type=int, default=0, help="override config t3.states")
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    return asyncio.run(amain(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
