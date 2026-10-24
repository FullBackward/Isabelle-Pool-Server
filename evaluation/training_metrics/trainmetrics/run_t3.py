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
- (b) verify block i + 1 (the continuation);
- (c) ``restore_checkpoint``, the source checked against the state's, then
  block i + 1 again: its report must equal (b)'s;
- (d) ``rollback`` of the kept continuation, the source checked again, and
  block i + 1 a third time (a control for (c), below);
- (e) ``enter_theory`` with a fresh name on the same (warm) session + replay
  blocks 0..i: the same-prover alternative (design B1);

then the session is closed. The distance between checkpoint and head is the
census's command count of block i + 1 (plan review S6). A restore here goes
back (it removes the continuation's text) and returns without waiting for the
prover (``Repl_Session.restore_state`` sends the edit; a rollback waits for the
node to settle). Work it leaves would show in the next call, so (c)'s second
verify and (d)'s third are each compared with (b)'s, and the branch cost is
the restore plus that extra time (two local cells: +101 ms and +2 ms p50 after
the restore, 0 ms after the rollback: measured, not assumed).

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
    cont: Optional[Dict[str, Any]] = None  # (b)
    restore_s: Optional[float] = None
    restore_ok: Optional[bool] = None
    restore_message: Optional[str] = None
    source_after_restore: Optional[bool] = None
    again: Optional[Dict[str, Any]] = None  # (c)'s second verify
    identical: Optional[bool] = None
    rollback_s: Optional[float] = None
    rollback_ok: Optional[bool] = None
    source_after_rollback: Optional[bool] = None
    after_rollback: Optional[Dict[str, Any]] = None  # (d)'s control verify
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


def same_report(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Two verifies of one block gave the same verdict and command statuses."""
    keys = ("proved", "success", "n_commands", "statuses", "first_error")
    return all(a.get(k) == b.get(k) for k in keys)


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


async def _branch_ops(
    client: Any,
    sess: SessionRef,
    cf: CorpusFile,
    row: Dict[str, Any],
    rec: StateRecord,
) -> None:
    """(a)-(d) at the state the session is in (the old node ends at the
    state plus block i + 1; (e) enters a new node)."""
    i = rec.index
    state_src = await _source_hash(client, sess)
    cp, rec.save_s = await _timed(
        client.save_checkpoint(sess.session_id, lease_id=sess.lease_id)
    )
    b = await verify_in_file(client, sess, cf, i + 1, row)
    rec.cont = _view(b)
    if b.error_kind in DEAD:
        rec.stopped = b.error_kind
        return
    if not b.success:  # discarded by verify_chunk: nothing to restore or roll back
        return
    res, rec.restore_s = await _timed(
        client.restore_checkpoint(
            sess.session_id, int(cp["checkpoint_id"]), lease_id=sess.lease_id
        )
    )
    rec.restore_ok = bool(res.get("success"))
    rec.restore_message = None if rec.restore_ok else str(res.get("message"))[:200]
    rec.source_after_restore = (await _source_hash(client, sess)) == state_src
    again = await verify_in_file(client, sess, cf, i + 1, row)
    rec.again = _view(again)
    rec.identical = same_report(rec.cont, rec.again)
    if again.error_kind in DEAD:
        rec.stopped = again.error_kind
        return
    if not again.success:
        return
    rb, rec.rollback_s = await _timed(
        client.rollback(sess.session_id, lease_id=sess.lease_id)
    )
    rec.rollback_ok = bool(rb.get("success"))
    rec.source_after_rollback = (await _source_hash(client, sess)) == state_src
    third = await verify_in_file(client, sess, cf, i + 1, row)
    rec.after_rollback = _view(third)
    if third.error_kind in DEAD:
        rec.stopped = third.error_kind


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
) -> StateRecord:
    """One state on a session created for it: (f), (a)-(d), (e); closed."""
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
            fresh, rec.stopped = await replay_prefix(client, sess, cf, rows, i)
            rec.fresh = asdict(fresh)
            if not rec.stopped and not fresh.error:
                await _branch_ops(client, sess, cf, rows[i + 1], rec)
            if not rec.stopped and not fresh.error:
                warm, rec.stopped = await replay_prefix(client, sess, cf, rows, i)
                rec.warm = asdict(warm)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        rec.error = f"{type(exc).__name__}: {exc}"[:300]
    rec.t_end = time.time()
    return rec


def replay_estimate_s(
    picks: Sequence[Tuple[str, int]], census: Dict[str, Any]
) -> float:
    """Census replay time the cell repeats: two prefix replays and four
    continuations per state (creates and checkpoint calls not included)."""
    rows = {f["stem"]: f.get("blocks") or [] for f in census.get("files", [])}
    total = 0.0
    for stem, i in picks:
        blocks = rows.get(stem) or []
        total += 2 * sum(float(b.get("wall_s") or 0) for b in blocks[: i + 1])
        if i + 1 < len(blocks):
            total += 4 * float(blocks[i + 1].get("wall_s") or 0)
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
            "census_replay_estimate_s": replay_estimate_s(picks, census),
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
