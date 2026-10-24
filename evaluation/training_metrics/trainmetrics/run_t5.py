"""T5 bounded step (design §T5, plan stages 3b, 3c): the W = 1 cell — the
seeds of :mod:`trainmetrics.seeds` checked right before census-proved corpus
blocks of a few files, each file on a session acquired with its own imports
(D6), with the per-file machinery of :mod:`trainmetrics.t5_core` — and the
CLI, which also runs the contention cell (``--contention``,
:mod:`trainmetrics.run_t5w8`).

    python -m trainmetrics.run_t5 [--local-smoke] [--ips-external] [--budgets 5:50,30:10,120:3]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import run_t5w8
from .census import load_census, load_config
from .corpus import CorpusFile, corpus_sha, load_corpus, load_paths
from .harness import cell_arg_parser, run_cli_cell
from .metrics_t5 import annotate, load_samples, summarize
from .seeds import BudgetLadder, make_seeds
from .t5_core import T5FileRecord, run_file


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
    ladder = BudgetLadder(counts, seeds, rng_seed=seed)
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
    if sampler is not None:  # the sampler's rows after the last attempt (S2)
        await asyncio.sleep(2.0)
        annotate(dicts, load_samples(out_dir / "samples.jsonl"))
    with open(out_dir / "positions.jsonl", "w", encoding="utf-8") as fh:
        for d in dicts:
            fh.write(json.dumps(d) + "\n")
    body["unclosed_sessions"] = [u for r in records for u in r.unclosed]
    body["stopped_files"] = {r.stem: r.stopped for r in records if r.stopped}
    body["summary"] = summarize(dicts)
    # no position run is no result (review beac25c note): not "ok", rc 1
    body["outcome"] = "ok" if body["summary"]["positions"] else "empty"
    return body


def parse_budgets(text: str) -> Dict[float, int]:
    """``5:50,30:10`` -> {5.0: 50, 30.0: 10}."""
    out: Dict[float, int] = {}
    for part in filter(None, (p.strip() for p in text.split(","))):
        b, n = part.split(":")
        out[float(b)] = int(n)
    return out


async def amain(args: argparse.Namespace) -> int:
    """One T5 cell under the shared harness: W = 1, or ``--contention`` (the
    W = 8 cell of config t5.w8). A cell whose manifest says ok is skipped
    unless ``--rerun``."""
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
    workers = int(t5["w8"]["workers"]) if args.contention else 1
    out_dir = Path(args.out) / "t5" / f"w{workers}" / f"rep{args.rep}"
    try:
        done = (
            json.loads((out_dir / "manifest.json").read_text()).get("outcome") == "ok"
        )
    except (OSError, ValueError):
        done = False
    if done and not args.rerun:
        print(f"t5 W={workers} rep={args.rep}: already ok, skipped -> {out_dir}")
        return 0
    census = load_census(args.census)
    seed = int(cfg["seed"]) + args.rep
    return await run_cli_cell(
        cfg,
        run_t5w8.run_cell if args.contention else run_cell,
        (t5, files, census),
        heap=heap,
        seed=seed,
        out_dir=out_dir,
        head={"experiment": "t5", "workers": workers, "rep": args.rep},
        extra={
            "census_path": args.census,
            "ips_env": cfg["ips"].get("env", {}),
            "local_smoke": bool(args.local_smoke),
            "t5_config": t5,
        },
        url=args.url,
        external=bool(args.ips_external),
        label=f"t5 W={workers} rep={args.rep}",
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """CLI."""
    ap = cell_arg_parser(__doc__)
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--files", default="", help="restrict the corpus to these stems")
    ap.add_argument(
        "--budgets", default="", help="override config t5.budgets, e.g. 5:2,30:1"
    )
    ap.add_argument("--seeds-per-family", type=int, default=0)
    ap.add_argument(
        "--contention", action="store_true", help="the W = 8 cell of config t5.w8"
    )
    ap.add_argument(
        "--rerun", action="store_true", help="run a cell that is already ok"
    )
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    return asyncio.run(amain(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
