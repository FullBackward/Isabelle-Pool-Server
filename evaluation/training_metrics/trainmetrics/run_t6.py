"""T6 rollout rate (design §T6, plan stages 6-7): how many verified rollouts
per hour one IPS host delivers at W workers when the client runs the training
loop of the report's rollout figure (checkpoint, verify under a budget,
restore or continue). No model: the policy is a script, so the rate is the
server's share of a training loop.

One cell: the T1 assignment (seed + rep, ``files_per_worker`` files per
worker) over the census-usable files; each worker takes its files in order,
each on a session acquired with the file's imports (D6), and runs a group at
each problem (:mod:`trainmetrics.t6_core`). The window opens when the workers
start, so acquisition and prefix replay are inside it (the setup share). A
side task records the memory tier every ``tier_poll_s``; the sampler records
CPU and PSS as for T1. Style ``t6a``: the D9 candidates at each of the first
``problems_per_file`` census-proved theorems.

    python -m trainmetrics.run_t6 --style t6a --workers 8 --rep 0 [--local-smoke] [--ips-external]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .census import load_census, load_config
from .contention import executor_threads, tier_poll, tier_summary
from .corpus import CorpusFile, assignment, corpus_sha, load_corpus, load_paths
from .harness import cell_arg_parser, cli_extra, open_registry, run_cli_cell
from .metrics_t5 import load_samples
from .metrics_t6 import summarize_t6
from .run_t1 import usable_stems
from .samples import window_summary
from .t6_core import CellCtx, FileRecord, Group, group_t6a, run_file

STYLES = ("t6a",)


def cell_dir(out: Path, style: str, workers: int, rep: int) -> Path:
    """``<out>/<style>/w<W>/rep<r>``."""
    return out / style / f"w{workers}" / f"rep{rep}"


def group_for(style: str, t6: Dict[str, Any]) -> Group:
    """The style's group function and its settings."""
    if style == "t6a":
        return group_t6a(float(t6["t6a"]["candidate_budget_s"]))
    raise ValueError(f"unknown style {style}")


async def run_cell(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    client: Any,
    t6: Dict[str, Any],
    files: Sequence[CorpusFile],
    census: Dict[str, Any],
    style: str,
    workers: int,
    *,
    heap: str,
    seed: int,
    out_dir: Path,
    sampler: Any = None,
    body: Optional[Dict[str, Any]] = None,
    checkpoint: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    """W workers over their assigned files; fills and returns the manifest body."""
    by_stem = {cf.stem: cf for cf in files}
    rows = {f["stem"]: f for f in census.get("files", [])}
    stems = usable_stems(files, census)
    quota = int(t6["files_per_worker"])
    plan = assignment(stems, seed, workers, quota)
    integrity_p = 1.0 if workers == 1 else float(t6.get("integrity_fraction", 0.25))
    body = body if body is not None else {}
    body.update(
        {
            "experiment": style,
            "workers": workers,
            "seed": seed,
            "heap": heap,
            "t6": t6,
            "census_id": census.get("census_id"),
            "corpus_sha": corpus_sha(files),
            "assignment": plan,
            # the W = 1 cell's files, a prefix of every larger cell's (S3)
            "matched_stems": assignment(stems, seed, 1, quota)[0],
            "integrity_p": integrity_p,
            "server_executor_threads": executor_threads(),
            "outcome": "running",
        }
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    registry, changed = open_registry(body, checkpoint)
    events: List[Dict[str, Any]] = []
    cell = CellCtx(
        client=client,
        group=group_for(style, t6),
        heap=heap,
        n_problems=int(t6[style]["problems_per_file"]),
        seed=seed,
        integrity_p=integrity_p,
        events=events,
        registry=registry,
        on_change=changed,
    )
    records: List[FileRecord] = []

    async def worker(w: int) -> None:
        for stem in plan[w]:
            records.append(await run_file(cell, by_stem[stem], rows[stem], w))

    if sampler is not None:
        sampler.phase("run")
    body["window_start"] = time.time()
    async with tier_poll(
        client, out_dir / "tiers.jsonl", float(t6.get("tier_poll_s", 5))
    ):
        await asyncio.gather(*(worker(w) for w in range(workers)))
    body["window_end"] = time.time()
    if sampler is not None:
        sampler.phase("stop")
    files_out = [
        asdict(r) for r in sorted(records, key=lambda r: (r.worker, r.t_start))
    ]
    for name, data in (("events.jsonl", events), ("files.jsonl", files_out)):
        with open(out_dir / name, "w", encoding="utf-8") as fh:
            for d in data:
                fh.write(json.dumps(d) + "\n")
    body["summary"] = {
        **summarize_t6(events, files_out, body),
        "tiers": tier_summary(load_samples(out_dir / "tiers.jsonl")),
        "host": window_summary(
            load_samples(out_dir / "samples.jsonl"),
            body["window_start"],
            body["window_end"],
        ),
    }
    body["unclosed_sessions"] = [u for r in records for u in r.unclosed]
    ran = [r for r in records if r.groups_done]
    body["outcome"] = "ok" if ran else "empty"
    return body


async def amain(args: argparse.Namespace) -> int:
    """One T6 cell under the shared harness."""
    cfg = load_config(args.config)
    t6 = json.loads(json.dumps(cfg["t6"]))
    if args.local_smoke:
        files = load_paths(cfg["local_smoke"]["files"])
        heap = cfg["local_smoke"]["field"]
        for k, v in (t6.get("local_smoke") or {}).items():
            t6[k] = {**t6[k], **v} if isinstance(v, dict) else v
    else:
        files = load_corpus(
            cfg["corpus"], args.files.split(",") if args.files else None
        )
        heap = cfg["session"]["field"]
    if args.files_per_worker:
        t6["files_per_worker"] = args.files_per_worker
    return await run_cli_cell(
        cfg,
        run_cell,
        (t6, files, load_census(args.census), args.style, args.workers),
        heap=heap,
        seed=int(cfg["seed"]) + args.rep,
        out_dir=cell_dir(Path(args.out), args.style, args.workers, args.rep),
        head={"experiment": args.style, "workers": args.workers, "rep": args.rep},
        extra=cli_extra(cfg, args),
        url=args.url,
        external=bool(args.ips_external),
        label=f"{args.style} W={args.workers} rep={args.rep}",
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """CLI."""
    ap = cell_arg_parser(__doc__)
    ap.add_argument("--style", choices=STYLES, required=True)
    ap.add_argument("--workers", type=int, required=True)
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--files", default="", help="restrict the corpus to these stems")
    ap.add_argument(
        "--files-per-worker", type=int, default=0, help="override t6.files_per_worker"
    )
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    return asyncio.run(amain(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
