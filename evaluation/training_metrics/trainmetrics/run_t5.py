"""T5 bounded step at W = 1 (design §T5, plan stage 3b). The seeds of
:mod:`trainmetrics.seeds` are checked right before census-proved corpus
blocks of a few files, each file on a session acquired with its own imports
(D6). Per seed attempt: the outcome (timed_out / failed / kept / error;
review 7bedac5 S1), the wall time and the undo cost past the budget,
``stuck_line``, and whether the node source is unchanged (a hash of
``GET …/source``; the plan's history hash would be vacuous, chunk calls are
not in the command history). A seed that stayed in the document is rolled
back and the rollback recorded (review 7bedac5 S2). Then the corpus block:
its verdict against the census and its time (recovery).

    python -m trainmetrics.run_t5 [--local-smoke] [--ips-external] [--budgets 5:50,30:10,120:3]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .census import load_census, load_config
from .corpus import CorpusFile, corpus_sha, load_corpus, load_paths, sha256_text
from .harness import cell_arg_parser, run_with_server
from .metrics_t5 import summarize
from .replay import (
    BlockRecord,
    SessionRef,
    _acquire_retrying,
    close_all,
    verify_block,
)
from .seeds import BudgetLadder, Seed, make_seeds


@dataclass
class SeedAttempt:  # pylint: disable=too-many-instance-attributes
    """One seed at one budget."""

    seed_id: str
    family: str
    goal_source: str
    budget_s: float
    outcome: str  # timed_out | failed | kept | error
    wall_s: float
    undo_s: Optional[float]  # wall time past the budget (timed-out attempts)
    stuck_line: Optional[int]
    n_commands: Optional[int]
    statuses: Dict[str, int]
    error: Optional[str]
    source_unchanged: Optional[bool]  # None: the source could not be read
    rolled_back: bool = False
    t_start: float = 0.0


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


@dataclass
class T5FileRecord:  # pylint: disable=too-many-instance-attributes
    """One file: its session, the prefix replayed, the seeded positions."""

    stem: str
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
    unclosed: List[Dict[str, Any]] = field(default_factory=list)
    t_start: float = 0.0
    t_end: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        """JSON form."""
        return asdict(self)


def classify(rec: BlockRecord) -> str:
    """timed_out (the bound fired), error (no report at all), kept (the seed
    stayed in the document: success, or an open proof), failed (ended with an
    error and was discarded)."""
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


async def attempt(
    client: Any, sess: SessionRef, seed: Seed, budget_s: float, before: Optional[str]
) -> SeedAttempt:
    """One seed under ``budget_s``; a kept seed is rolled back (S2)."""
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
    )
    if out.outcome == "kept":
        try:
            await client.rollback(sess.session_id, lease_id=sess.lease_id)
            out.rolled_back = True
        except Exception as exc:  # pylint: disable=broad-exception-caught
            out.error = f"rollback failed: {type(exc).__name__}: {exc}"[:300]
    after = await _source_hash(client, sess)
    if before is not None and after is not None:
        out.source_unchanged = before == after
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
        first = ladder.first(seed.family)
        if first is None:
            continue
        pos.attempts.append(await attempt(client, sess, seed, first, before))
        if pos.attempts[-1].outcome == "timed_out":
            for b in ladder.more(seed.family):
                pos.attempts.append(await attempt(client, sess, seed, b, before))
    blk = await verify_block(
        client,
        sess,
        cf.blocks[i],
        stem=cf.stem,
        index=i,
        block_sha=cf.block_sha[i],
        census_commands=row.get("n_commands"),
        census_partial=bool(row.get("count_partial")),
    )
    pos.block = asdict(blk)
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
) -> T5FileRecord:
    """Acquire (with the file's imports), enter, replay up to the last seeded
    position with the seeds in place, close."""
    rec = T5FileRecord(stem=cf.stem, t_start=time.time())
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
        for i in range(max(by_pos) + 1):
            if i in by_pos:
                rec.positions.append(
                    await run_position(client, sess, cf, by_pos[i], ladder, census_file)
                )
                continue
            row = rows[i] if i < len(rows) else {}
            blk = await verify_block(
                client,
                sess,
                cf.blocks[i],
                stem=cf.stem,
                index=i,
                block_sha=cf.block_sha[i],
                census_commands=row.get("n_commands"),
                census_partial=bool(row.get("count_partial")),
            )
            rec.prefix_blocks += 1
            rec.prefix_proved += int(blk.proved)
            rec.prefix_wall_s = round(rec.prefix_wall_s + blk.wall_s, 4)
    finally:
        rec.unclosed = await close_all(client, [sess])
        if not rec.unclosed:
            registry.pop(sess.session_id, None)
        on_change(registry)
        rec.t_end = time.time()
    return rec


async def run_cell(  # pylint: disable=too-many-arguments,too-many-locals
    client: Any,
    t5: Dict[str, Any],
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
    """The W = 1 cell: files in seed order, one session each."""
    seeds = make_seeds(
        files,
        census,
        per_family=int(t5["seeds_per_family"]),
        rng_seed=seed,
        max_files=int(t5["max_files"]),
        facts_cap=int(t5.get("metis_facts_max", 40)),
    )
    counts = {float(b): int(n) for b, n in dict(t5["budgets"]).items()}
    ladder = BudgetLadder(counts)
    body = body if body is not None else {}
    body.update(
        {
            "experiment": "t5",
            "workers": 1,
            "seed": seed,
            "heap": heap,
            "budgets": {str(b): n for b, n in counts.items()},
            "census_id": census.get("census_id"),
            "corpus_sha": corpus_sha(files),
            "seeds": len(seeds),
            "seed_files": sorted({s.stem for s in seeds}),
            "budget_rule_genuine": "census (min(600, max(30, 2 n)))",
            "outcome": "running",
        }
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "seeds.json", "w", encoding="utf-8") as fh:
        json.dump([s.as_dict() for s in seeds], fh, indent=1)
    registry: Dict[str, str] = {}
    body["open_sessions"] = registry

    def changed(_reg: Dict[str, str]) -> None:
        if checkpoint is not None:
            checkpoint(body)

    rows = {f["stem"]: f for f in census.get("files", [])}
    by_stem = {cf.stem: cf for cf in files}
    records: List[T5FileRecord] = []
    if sampler is not None:
        sampler.phase("run")
    body["window_start"] = time.time()
    for stem in body["seed_files"]:
        file_seeds = [s for s in seeds if s.stem == stem]
        records.append(
            await run_file(
                client,
                by_stem[stem],
                file_seeds,
                ladder,
                rows[stem],
                heap=heap,
                registry=registry,
                on_change=changed,
            )
        )
    body["window_end"] = time.time()
    if sampler is not None:
        sampler.phase("stop")
    dicts = [r.as_dict() for r in records]
    with open(out_dir / "positions.jsonl", "w", encoding="utf-8") as fh:
        for d in dicts:
            fh.write(json.dumps(d) + "\n")
    body["unclosed_sessions"] = [u for r in records for u in r.unclosed]
    body["summary"] = summarize(dicts)
    body["outcome"] = "ok"
    return body


def parse_budgets(text: str) -> Dict[float, int]:
    """``5:50,30:10`` -> {5.0: 50, 30.0: 10}."""
    out: Dict[float, int] = {}
    for part in filter(None, (p.strip() for p in text.split(","))):
        b, n = part.split(":")
        out[float(b)] = int(n)
    return out


async def amain(args: argparse.Namespace) -> int:
    """One T5 W = 1 cell under the shared harness."""
    cfg = load_config(args.config)
    t5 = dict(cfg["t5"])
    if args.local_smoke:
        files = load_paths(cfg["local_smoke"]["files"])
        heap = cfg["local_smoke"]["field"]
        t5.update(t5.get("local_smoke") or {})
    else:
        files = load_corpus(
            cfg["corpus"], args.files.split(",") if args.files else None
        )
        heap = cfg["session"]["field"]
    if args.budgets:
        t5["budgets"] = parse_budgets(args.budgets)
    if args.seeds_per_family:
        t5["seeds_per_family"] = args.seeds_per_family
    census = load_census(args.census)
    out_dir = Path(args.out) / "t5" / "w1" / f"rep{args.rep}"
    seed = int(cfg["seed"]) + args.rep

    async def work(client: Any, sampler: Any, body: Dict[str, Any], ckpt: Any) -> None:
        body.update(
            {
                "census_path": args.census,
                "ips_env": cfg["ips"].get("env", {}),
                "local_smoke": bool(args.local_smoke),
                "t5_config": t5,
            }
        )
        await run_cell(
            client,
            t5,
            files,
            census,
            heap=heap,
            seed=seed,
            out_dir=out_dir,
            sampler=sampler,
            body=body,
            checkpoint=ckpt,
        )

    body = await run_with_server(
        cfg,
        heap=heap,
        out_dir=out_dir,
        head={"experiment": "t5", "workers": 1, "rep": args.rep},
        work=work,
        url=args.url,
        external=bool(args.ips_external),
    )
    print(
        f"t5 W=1 rep={args.rep}: {body['outcome']}"
        + (f" ({body.get('error')})" if body.get("error") else "")
        + f" -> {out_dir}",
        flush=True,
    )
    if body.get("summary"):
        print(json.dumps(body["summary"], indent=1), flush=True)
    return 0 if body["outcome"] == "ok" else 1


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """CLI."""
    ap = cell_arg_parser(__doc__)
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--files", default="", help="restrict the corpus to these stems")
    ap.add_argument(
        "--budgets", default="", help="override config t5.budgets, e.g. 5:2,30:1"
    )
    ap.add_argument("--seeds-per-family", type=int, default=0)
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    return asyncio.run(amain(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
