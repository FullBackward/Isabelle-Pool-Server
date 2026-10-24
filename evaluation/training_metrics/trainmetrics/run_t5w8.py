"""T5's contention cell (design §T5: "the response time of the other workers'
calls while one session sits in a 120 s loop at W = 8"; plan stage 3c). W
workers take T1's assignment (seed + rep; each worker's files are the first
``files_per_worker`` of its T1 list at the same W, so the corpus blocks match
T1's cell) and replay their files to the end, each file on its own session
(D6), with a share of each worker's blocks seeded (``seed_fraction`` of its
blocks, at most ``max_seeds_per_worker``) at one budget. Every corpus block is
recorded with its times, so the analysis can split them by whether they
overlap another worker's loop; the server's memory tier is polled for the
whole cell (review beac25c S2); each attempt's prover is the file's
session's, found by its lifetime (review 1676d29 S1; metrics_t5.session_pids)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .contention import (
    census_walls,
    executor_threads,
    poll_tiers,
    state_table,
    tier_summary,
)
from .corpus import CorpusFile, assignment, corpus_sha
from .metrics_t5 import annotate, load_samples, summarize
from .run_t1 import usable_stems
from .seeds import FAMILIES, BudgetLadder, Seed, candidates, make_seeds
from .t5_core import T5FileRecord, run_file


def worker_seeds(
    files: Sequence[CorpusFile],
    census: Dict[str, Any],
    w8: Dict[str, Any],
    *,
    worker: int,
    seed: int,
) -> List[Seed]:
    """A worker's seeds: ``seed_fraction`` of its blocks, at most
    ``max_seeds_per_worker``, alternating the families, at census-proved
    theorems of its own files; ids and names carry the worker."""
    rows = {f["stem"]: f for f in census.get("files", [])}
    blocks = sum(len(cf.blocks) for cf in files)
    eligible = sum(
        len(candidates(cf, rows[cf.stem])) for cf in files if cf.stem in rows
    )
    k = min(
        int(w8["max_seeds_per_worker"]),
        math.ceil(float(w8["seed_fraction"]) * blocks),
        eligible,
    )
    if k <= 0:
        return []
    pool = make_seeds(
        files,
        census,
        per_family=math.ceil(k / len(FAMILIES)),
        rng_seed=seed * 1000 + worker,
        max_files=len(files),
    )
    by_fam = {fam: [s for s in pool if s.family == fam] for fam in FAMILIES}
    picked: List[Seed] = []
    while len(picked) < k and any(by_fam.values()):
        for fam in FAMILIES:
            if by_fam[fam] and len(picked) < k:
                picked.append(by_fam[fam].pop(0))
    return [
        dataclasses.replace(s, seed_id=f"w{worker}:{s.seed_id}")
        for s in sorted(picked, key=lambda s: (s.stem, s.position))
    ]


async def _worker(  # pylint: disable=too-many-arguments
    client: Any,
    w: int,
    *,
    stems: List[str],
    by_stem: Dict[str, CorpusFile],
    seeds: List[Seed],
    budget_s: float,
    census: Dict[str, Any],
    heap: str,
    sink: List[T5FileRecord],
    registry: Dict[str, str],
    on_change: Callable[[Dict[str, str]], Any],
) -> None:
    rows = {f["stem"]: f for f in census.get("files", [])}
    ladder = BudgetLadder({budget_s: len(seeds)})  # one budget; every seed takes it
    for stem in stems:
        sink.append(
            await run_file(
                client,
                by_stem[stem],
                [s for s in seeds if s.stem == stem],
                ladder,
                rows[stem],
                heap=heap,
                registry=registry,
                on_change=on_change,
                worker=w,
                to_end=True,
                keep_blocks=True,
            )
        )


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
    """The contention cell; fills and returns the manifest body."""
    w8 = dict(t5["w8"])
    workers = int(w8["workers"])
    by_stem = {cf.stem: cf for cf in files}
    plan = assignment(
        usable_stems(files, census), seed, workers, int(w8["files_per_worker"])
    )
    seeds = {
        w: worker_seeds([by_stem[s] for s in plan[w]], census, w8, worker=w, seed=seed)
        for w in range(workers)
    }
    body = body if body is not None else {}
    body.update(
        {
            "experiment": "t5_contention",
            "workers": workers,
            "seed": seed,
            "heap": heap,
            "w8": w8,
            "assignment": plan,
            "seeds": sum(len(v) for v in seeds.values()),
            "census_id": census.get("census_id"),
            "corpus_sha": corpus_sha(files),
            "server_executor_threads": executor_threads(),
            "outcome": "running",
        }
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "seeds.json", "w", encoding="utf-8") as fh:
        json.dump([s.as_dict() for v in seeds.values() for s in v], fh, indent=1)
    registry: Dict[str, str] = {}
    body["open_sessions"] = registry

    def changed(_reg: Dict[str, str]) -> None:
        if checkpoint is not None:
            checkpoint(body)

    records: List[T5FileRecord] = []
    stop = asyncio.Event()
    poller = asyncio.ensure_future(
        poll_tiers(
            client, out_dir / "tiers.jsonl", stop, float(w8.get("tier_poll_s", 5))
        )
    )
    if sampler is not None:
        sampler.phase("run")
    body["window_start"] = time.time()
    try:
        await asyncio.gather(
            *(
                _worker(
                    client,
                    w,
                    stems=plan[w],
                    by_stem=by_stem,
                    seeds=seeds[w],
                    budget_s=float(w8["budget_s"]),
                    census=census,
                    heap=heap,
                    sink=records,
                    registry=registry,
                    on_change=changed,
                )
                for w in range(workers)
            )
        )
    finally:
        stop.set()
        await asyncio.gather(poller, return_exceptions=True)
    body["window_end"] = time.time()
    if sampler is not None:
        sampler.phase("stop")
        await asyncio.sleep(2.0)
    dicts = [r.as_dict() for r in sorted(records, key=lambda r: (r.worker, r.t_start))]
    annotate(dicts, load_samples(out_dir / "samples.jsonl"), by_session=True)
    with open(out_dir / "positions.jsonl", "w", encoding="utf-8") as fh:
        for d in dicts:
            fh.write(json.dumps(d) + "\n")
    body["unclosed_sessions"] = [u for r in records for u in r.unclosed]
    body["stopped_files"] = {
        f"w{r.worker}:{r.stem}": r.stopped for r in records if r.stopped
    }
    body["summary"] = {
        **summarize(dicts),
        "contention": state_table(dicts, census_walls(census)),
        "tiers": tier_summary(load_samples(out_dir / "tiers.jsonl")),
    }
    blocks = sum(len(d["blocks"]) for d in dicts)
    body["outcome"] = "ok" if blocks else "empty"
    return body
