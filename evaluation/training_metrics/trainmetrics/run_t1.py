"""One T1/T2 cell: W workers, each on its own fresh session, replay their
assigned files concurrently (plan v1.3 §3). The clock starts after all W
sessions are acquired and before the first ``enter_theory`` (review S2); the
sampler records memory and CPU for the whole cell.

    python -m trainmetrics.run_t1 --workers 8 --rep 0 [--local-smoke] [--ips-external]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import EXPERIMENT_DIR
from .census import counts_by_stem, load_census, load_config, partial_by_stem
from .corpus import CorpusFile, assignment, corpus_sha, load_corpus, load_paths
from .replay import BUDGET_MAX_S, ReplayRecord, replay_file_fresh
from .serve import isabelle_cmd, make_driver


@dataclass
class CellSpec:  # pylint: disable=too-many-instance-attributes
    """What one cell runs."""

    workers: int
    rep: int
    seed: int
    quota: int
    heap: str  # the session's heap (Isabelle session name)
    out_dir: Path
    local_smoke: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


def cell_dir(out: Path, workers: int, rep: int) -> Path:
    """``<out>/t1/w<W>/rep<r>``."""
    return out / "t1" / f"w{workers}" / f"rep{rep}"


def usable_stems(files: Sequence[CorpusFile], census: Dict[str, Any]) -> List[str]:
    """Stems in the corpus AND acquired and entered without error in the census
    (a file whose session or header fails cannot be replayed; it is excluded
    and listed)."""
    entered = {
        f["stem"]
        for f in census["files"]
        if not f.get("enter_error") and not f.get("acquire_error")
    }
    return sorted(cf.stem for cf in files if cf.stem in entered)


async def _worker(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    client: Any,
    spec: CellSpec,
    worker: int,
    stems: List[str],
    files: Dict[str, CorpusFile],
    census: Tuple[Dict[str, List[Optional[int]]], Dict[str, List[bool]]],
    sink: List[ReplayRecord],
    registry: Dict[str, str],
    on_change: Optional[Callable[[Dict[str, str]], Any]],
) -> None:
    counts, partial = census
    for stem in stems:  # a session per file, with the file's imports (D6)
        sink.append(
            await replay_file_fresh(
                client,
                files[stem],
                heap=spec.heap,
                worker=worker,
                census_counts=counts.get(stem),
                census_partial=partial.get(stem),
                label=f"trainmetrics-t1-w{worker}",
                registry=registry,
                on_change=on_change,
            )
        )


async def run_cell(  # pylint: disable=too-many-locals,too-many-arguments
    client: Any,
    spec: CellSpec,
    files: Sequence[CorpusFile],
    census: Dict[str, Any],
    sampler: Any = None,
    *,
    body: Optional[Dict[str, Any]] = None,
    checkpoint: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    """W workers replay their assigned files concurrently, each file on a
    fresh session acquired with its own imports (D6, review afd3de9 B1). The
    window opens when the workers start and so includes the per-file
    acquisitions (reported per replay as ``acquire_s``). Fills ``body`` (the
    caller's manifest dict, so a cancelled cell keeps what it had) and returns
    it; ``body["open_sessions"]`` (session id -> lease) is kept current and
    ``checkpoint(body)`` called on every change, so a killed cell's leases are
    on disk; writes ``replays.jsonl`` into ``spec.out_dir``."""
    by_stem = {cf.stem: cf for cf in files}
    stems = usable_stems(files, census)
    excluded = sorted(set(by_stem) - set(stems))
    plan = assignment(stems, spec.seed, spec.workers, spec.quota)
    body = body if body is not None else {}
    body.update(
        {
            "experiment": "t1",
            "workers": spec.workers,
            "rep": spec.rep,
            "seed": spec.seed,
            "quota": spec.quota,
            "heap": spec.heap,
            "local_smoke": spec.local_smoke,
            "census_id": census.get("census_id"),
            "corpus_sha": corpus_sha(files),
            "assignment": plan,
            "excluded_stems": excluded,
            "session_per_file": True,
            "outcome": "running",
            **spec.extra,
        }
    )
    registry: Dict[str, str] = {}
    body["open_sessions"] = registry

    def changed(_reg: Dict[str, str]) -> None:
        if checkpoint is not None:
            checkpoint(body)

    records: List[ReplayRecord] = []
    if sampler is not None:
        sampler.phase("run")
    body["window_start"] = time.time()  # the workers start; acquisitions are inside
    await asyncio.gather(
        *(
            _worker(
                client,
                spec,
                w,
                plan[w],
                by_stem,
                (counts_by_stem(census), partial_by_stem(census)),
                records,
                registry,
                changed,
            )
            for w in range(spec.workers)
        )
    )
    body["window_end"] = time.time()
    if sampler is not None:
        sampler.phase("stop")
    body["unclosed_sessions"] = [u for r in records for u in r.unclosed]
    body["replays"] = len(records)
    body["acquire_errors"] = sum(1 for r in records if r.acquire_error)
    body["enter_errors"] = sum(1 for r in records if r.enter_error)
    body["block_errors"] = sum(1 for r in records for b in r.blocks if b.error)
    spec.out_dir.mkdir(parents=True, exist_ok=True)
    with open(spec.out_dir / "replays.jsonl", "w", encoding="utf-8") as fh:
        for rr in sorted(records, key=lambda x: (x.worker, x.t_start)):
            fh.write(json.dumps(rr.as_dict()) + "\n")
    body["outcome"] = "ok"
    return body


def write_manifest(out_dir: Path, body: Dict[str, Any]) -> Path:
    """``manifest.json`` (written at start and at the end, as in memscale)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "manifest.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=1, default=str)
    return path


async def amain(args: argparse.Namespace) -> int:  # pylint: disable=too-many-locals
    """Build everything for one cell and run it."""
    # pylint: disable=import-outside-toplevel
    from memscale.phases import environment_block
    from memscale.sampler import Sampler

    from client.async_client import PoolAsyncClient

    cfg = load_config(args.config)
    ips = cfg["ips"]
    if args.local_smoke:
        files = load_paths(cfg["local_smoke"]["files"])
        field_name = cfg["local_smoke"]["field"]
    else:
        files = load_corpus(
            cfg["corpus"], args.files.split(",") if args.files else None
        )
        field_name = cfg["session"]["field"]
    census = load_census(args.census)
    out_dir = cell_dir(Path(args.out), args.workers, args.rep)
    spec = CellSpec(
        workers=args.workers,
        rep=args.rep,
        seed=int(cfg["seed"]) + args.rep,  # seed per repeat (review 86afb15 S1)
        quota=args.quota or int(cfg["files_per_worker"]),
        heap=field_name,
        out_dir=out_dir,
        local_smoke=bool(args.local_smoke),
        extra={"census_path": args.census, "ips_env": ips.get("env", {})},
    )
    driver = make_driver(cfg, field_name, args.url, bool(args.ips_external))
    head: Dict[str, Any] = {
        "experiment": "t1",
        "workers": spec.workers,
        "rep": spec.rep,
        "outcome": "running",
        "started_at": time.time(),
        "environment": environment_block(cfg.get("isabelle_home"), isabelle_cmd()),
    }
    write_manifest(out_dir, head)
    body: Dict[str, Any] = dict(head)
    main_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if main_task is not None:  # the matrix runner's timeout: stop the server first
        loop.add_signal_handler(signal.SIGTERM, main_task.cancel)
    try:
        await driver.setup()
        # the server's pid/process group and cgroup at once, so the runner can
        # kill it if this cell is killed before its finally (review 830f9ac B1, S2)
        body["server"] = {
            "pid": driver.proc.pid if driver.proc is not None else None,
            "pgid": driver.proc.pid if driver.proc is not None else None,
            "cgroup_dir": driver.cgroup_dir,
            "external": driver.external,
            "started_at": driver.server_started_at,
        }
        write_manifest(out_dir, body)
        patterns = list(ips.get("root_patterns", [])) if driver.external else []
        with Sampler(
            str(out_dir / "samples.jsonl"),
            root_pids=driver.root_pids(),
            patterns=patterns,
            interval_s=float(cfg.get("sample_interval_s", 1.0)),
            cgroup_dir=driver.cgroup_dir,
        ) as sampler:
            async with PoolAsyncClient(
                driver.url, timeout=BUDGET_MAX_S + 120.0
            ) as client:
                await run_cell(
                    client,
                    spec,
                    files,
                    census,
                    sampler,
                    body=body,
                    checkpoint=lambda b: write_manifest(out_dir, b),
                )
                try:
                    body["server_memory_after"] = await client.memory_status()
                except Exception:  # pylint: disable=broad-exception-caught
                    body["server_memory_after"] = None
        body["driver"] = driver.describe()
    except asyncio.CancelledError:
        body["outcome"] = "terminated"
        body["error"] = "SIGTERM (the matrix runner's cell timeout)"
    except Exception as exc:  # pylint: disable=broad-exception-caught
        body["outcome"] = "error"
        body["error"] = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        if main_task is not None:
            loop.remove_signal_handler(signal.SIGTERM)  # stop_all must not be cancelled
        await driver.stop_all()
        body["finished_at"] = time.time()
        write_manifest(out_dir, body)
    print(
        f"t1 W={spec.workers} rep={spec.rep}: {body['outcome']}"
        + (f" ({body.get('error')})" if body.get("error") else "")
        + f" -> {out_dir}",
        flush=True,
    )
    return 0 if body["outcome"] == "ok" else 1


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """CLI."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", default=str(EXPERIMENT_DIR / "config.yaml"))
    ap.add_argument("--workers", type=int, required=True)
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--quota", type=int, default=0, help="files per worker (config)")
    ap.add_argument("--files", default="", help="restrict the corpus to these stems")
    ap.add_argument(
        "--census", default=str(EXPERIMENT_DIR / "results/census/census.json")
    )
    ap.add_argument("--out", default=str(EXPERIMENT_DIR / "results"))
    ap.add_argument("--url", default=None)
    ap.add_argument("--ips-external", action="store_true")
    ap.add_argument("--local-smoke", action="store_true")
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    return asyncio.run(amain(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
