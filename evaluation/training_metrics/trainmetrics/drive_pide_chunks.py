"""The PIDE MCP row of T1 (design D3, plan B2, stage 8a): the same assignment
and census as T1, replayed block by block on PIDE MCP (Kappelmann's
``isabelle pide_mcp``, topology A: one process, a session per worker), as
edit-and-settle — a throughput row for the comparison, not a verdict call.

A worker starts its session (before the window, reported apart), then per
assigned file creates a scratch theory, writes the header, and appends one
block per ``edit`` (mode replace, the whole text: PIDE checks only the new
block). After each edit it polls ``get_state`` every ``poll_s`` (0.1 s)
until the command count has grown and nothing is running or unprocessed:
PIDE parses the text inside the ``edit`` call, so the count is final at the
first reading (stage 8 spike; each block's count is recorded to check it).
The block's time is the wall from the edit to that reading: 0.1 s resolution, the same as IPS's
``verify_chunk``, which re-reads the node every ``session.output_delay``
(0.1 s). PIDE's ``total_timing_ms`` is recorded beside it but is not a clock
(stage 8 spike: it summed per-command timings and once fell by 9 ms between
two blocks). PIDE counts the spans between commands too (about twice the
census count), so a block's ``n_commands`` is the census count, IPS's unit,
and PIDE's own delta is kept beside it. A block is *proved* when it settled
and added no failed, bad or error command; one not settled within the census budget is *timed out* and
ends its file (PIDE MCP cannot cancel a running command).

The sessions run with ``pide.session_options`` (D12: ``parallel_proofs=2``,
IPS's ``ISABELLE_PARALLEL_PROOFS``), recorded for uniformity: both tools run
interactive PIDE sessions, where any ``parallel_proofs > 0`` is Isabelle's
interactive level 3 (Pure/PIDE/interactive.ML), so the rows check proofs
alike either way (review 04339cf S1).

Records are written in T1's format (``replays.jsonl``), so the T1 analysis
computes the row exactly as for IPS.

    python -m trainmetrics.drive_pide_chunks --workers 8 --rep 0 [--local-smoke]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from memscale.drive_pide import state_args

from .census import load_census, load_config
from .corpus import CorpusFile, assignment, corpus_sha, load_corpus, load_paths
from .harness import open_registry, run_cli_cell, worker_arg_parser
from .replay import BlockRecord, ReplayRecord, budget_for
from .run_t1 import usable_stems

BAD = ("commands_failed", "commands_bad", "errors")
BUSY = ("commands_unprocessed", "commands_still_running_possibly_nonterminating")


def counts(state: Dict[str, Any]) -> Dict[str, int]:
    """The command counts of a ``get_state`` answer (``commands.count`` and
    the per-status counts, ints or objects with a ``count``)."""
    out: Dict[str, int] = {}
    for key, val in state.items():
        if isinstance(val, dict) and isinstance(val.get("count"), int):
            out[key] = int(val["count"])
        elif isinstance(val, int) and not isinstance(val, bool):
            out[key] = val
    return out


def cell_dir(out: Path, workers: int, rep: int) -> Path:
    """``<out>/pide/w<W>/rep<r>``."""
    return out / "pide" / f"w{workers}" / f"rep{rep}"


class Scratch:  # pylint: disable=too-many-instance-attributes
    """One worker's PIDE session: a scratch theory per file, blocks appended."""

    def __init__(self, pide: Any, worker: int, poll_s: float) -> None:
        self.pide, self.worker, self.poll_s = pide, worker, poll_s
        self.mcp = pide._mcp(worker)  # pylint: disable=protected-access
        self.sid = pide.session_ids[worker]
        self.path = ""
        self.text = ""
        self.files = 0
        self.now: Dict[str, int] = {}  # the counts after the last append

    def _args(self) -> Dict[str, Any]:
        return state_args(self.sid, self.path)

    async def new_file(self, imports: Sequence[str]) -> None:
        """A scratch theory with the file's imports (its name is the scratch's)."""
        self.files += 1
        sc = await self.mcp.call(
            "create_scratch",
            {
                "session": self.sid,
                "name_suffix": f"tm_w{self.worker}_f{self.files}",
                "extension": ".thy",
            },
            timeout_s=120.0,
        )
        self.path = str(sc["path"])
        stem = os.path.splitext(os.path.basename(self.path))[0]
        self.text = f"theory {stem}\n  imports {' '.join(imports)}\nbegin\n"
        self.now = {}

    async def append(
        self, block: str, budget_s: float
    ) -> Tuple[Dict[str, int], Dict[str, int], bool, int]:
        """Append ``block`` ("" writes the header alone), wait until it
        settled; (counts before, after, settled, polls)."""
        before = self.now
        self.text = self.text + "\n" + block + "\n" if block else self.text
        await self.mcp.call(
            "edit",
            {
                "session": self.sid,
                "origin": self.path,
                "mode": "replace",
                "old_text": "",
                "text": self.text + "end\n",
            },
            timeout_s=budget_s + 60.0,
        )
        deadline = time.monotonic() + budget_s
        polls = 0
        while True:
            self.now = counts(
                await self.mcp.call("get_state", self._args(), timeout_s=60.0)
            )
            polls += 1
            grown = self.now.get("commands", 0) > before.get("commands", 0)
            if grown and sum(self.now.get(k, 0) for k in BUSY) == 0:
                return before, self.now, True, polls
            if time.monotonic() > deadline:
                return before, self.now, False, polls
            await asyncio.sleep(self.poll_s)


def _block_record(  # pylint: disable=too-many-arguments
    cf: CorpusFile,
    i: int,
    row: Dict[str, Any],
    *,
    t_start: float,
    wall: float,
    result: Tuple[Dict[str, int], Dict[str, int], bool, int],
) -> BlockRecord:
    before, after, settled, polls = result
    bad = sum(after.get(k, 0) - before.get(k, 0) for k in BAD)
    rec = BlockRecord(
        stem=cf.stem,
        index=i,
        block_sha=cf.block_sha[i],
        n_commands=row.get("n_commands"),  # IPS's unit (module docstring)
        census_commands=row.get("n_commands"),
        budget_s=budget_for(row.get("n_commands"), bool(row.get("count_partial"))),
        budget_source="census",
        t_start=t_start,
        wall_s=round(wall, 4),
    )
    rec.success = settled and bad == 0
    rec.proved = rec.success
    rec.timed_out = not settled
    rec.execution_time = None
    rec.statuses = {
        "polls": polls,
        "bad": bad,
        "pide_commands_delta": after.get("commands", 0) - before.get("commands", 0),
        "timing_ms_delta": after.get("total_timing_ms", 0)
        - before.get("total_timing_ms", 0),
    }
    return rec


async def _open(scratch: Scratch, cf: CorpusFile, rr: ReplayRecord) -> None:
    """A scratch theory for ``cf`` and its header, settled (``enter_s``)."""
    t0 = time.monotonic()
    try:
        await scratch.new_file(cf.imports)
        rr.theory_name = os.path.splitext(os.path.basename(scratch.path))[0]
        _b, after, settled, _p = await scratch.append("", 120.0)
        bad = sum(int(after.get(k, 0)) for k in BAD)
        if not settled or bad > 0:
            rr.enter_error = f"header did not settle cleanly: {after}"[:300]
    except Exception as exc:  # pylint: disable=broad-exception-caught
        rr.enter_error = f"{type(exc).__name__}: {exc}"[:300]
    rr.enter_s = round(time.monotonic() - t0, 4)


async def _block(
    scratch: Scratch, cf: CorpusFile, i: int, row: Dict[str, Any]
) -> BlockRecord:
    """Block ``i`` appended and settled under its census budget."""
    budget = budget_for(row.get("n_commands"), bool(row.get("count_partial")))
    t_start, t0 = time.time(), time.monotonic()
    try:
        result = await scratch.append(cf.blocks[i], budget)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        return BlockRecord(
            stem=cf.stem,
            index=i,
            block_sha=cf.block_sha[i],
            n_commands=None,
            census_commands=row.get("n_commands"),
            budget_s=budget,
            budget_source="census",
            t_start=t_start,
            wall_s=round(time.monotonic() - t0, 4),
            error=f"{type(exc).__name__}: {exc}"[:300],
        )
    return _block_record(
        cf, i, row, t_start=t_start, wall=time.monotonic() - t0, result=result
    )


async def replay(
    scratch: Scratch, cf: CorpusFile, rows: List[Dict[str, Any]]
) -> ReplayRecord:
    """One file on the worker's session, block by block (T1's record); a
    block that errors or does not settle ends the file (PIDE MCP cannot
    cancel a running command)."""
    rr = ReplayRecord(
        stem=cf.stem,
        theory_name="",
        session_id=scratch.sid,
        worker=scratch.worker,
        nodes_on_session=scratch.files + 1,
        t_start=time.time(),
    )
    await _open(scratch, cf, rr)
    for i in range(len(cf.blocks) if not rr.enter_error else 0):
        rr.blocks.append(await _block(scratch, cf, i, rows[i] if i < len(rows) else {}))
        if rr.blocks[-1].error or rr.blocks[-1].timed_out:
            break
    rr.t_end = time.time()
    return rr


async def run_cell(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    pide: Any,
    pcfg: Dict[str, Any],
    files: Sequence[CorpusFile],
    census: Dict[str, Any],
    workers: int,
    quota: int,
    *,
    heap: str,
    seed: int,
    out_dir: Path,
    sampler: Any = None,
    body: Optional[Dict[str, Any]] = None,
    checkpoint: Optional[Callable[[Dict[str, Any]], Any]] = None,
) -> Dict[str, Any]:
    """W workers on one PIDE MCP process; fills and returns the manifest body."""
    by_stem = {cf.stem: cf for cf in files}
    rows = {f["stem"]: f.get("blocks") or [] for f in census.get("files", [])}
    stems = usable_stems(files, census)
    plan = assignment(stems, seed, workers, quota)
    body = body if body is not None else {}
    body.update(
        {
            "heap": heap,
            "experiment": "pide",
            "tool": "pide-a",
            "workers": workers,
            "seed": seed,
            "quota": quota,
            "census_id": census.get("census_id"),
            "corpus_sha": corpus_sha(files),
            "assignment": plan,
            "poll_s": float(pcfg.get("poll_s", 0.1)),
            "session_options": pide.session_options(),
            "outcome": "running",
        }
    )
    open_registry(body, checkpoint)  # nothing is leased: kept for the manifest's shape
    starts: Dict[int, float] = {}
    for w in range(workers):  # before the window, as T1's original S2 clock
        t0 = time.monotonic()
        await pide.start_session(w)
        starts[w] = round(time.monotonic() - t0, 3)
    body["session_start_s"] = starts
    records: List[ReplayRecord] = []

    async def worker(w: int) -> None:
        scratch = Scratch(pide, w, float(pcfg.get("poll_s", 0.1)))
        for stem in plan[w]:
            records.append(await replay(scratch, by_stem[stem], rows.get(stem) or []))

    if sampler is not None:
        sampler.phase("run")
    body["window_start"] = time.time()
    await asyncio.gather(*(worker(w) for w in range(workers)))
    body["window_end"] = time.time()
    if sampler is not None:
        sampler.phase("stop")
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "replays.jsonl", "w", encoding="utf-8") as fh:
        for rr in sorted(records, key=lambda x: (x.worker, x.t_start)):
            fh.write(json.dumps(asdict(rr)) + "\n")
    body["replays"] = len(records)
    body["enter_errors"] = sum(1 for r in records if r.enter_error)
    body["timed_out_blocks"] = sum(1 for r in records for b in r.blocks if b.timed_out)
    body["unclosed_sessions"] = []
    body["outcome"] = "ok" if any(r.blocks for r in records) else "empty"
    return body


async def amain(args: argparse.Namespace) -> int:
    """One PIDE MCP cell under the shared harness (the driver is the server)."""
    # pylint: disable=import-outside-toplevel
    from memscale.drive_pide import PIDEDriver

    cfg = load_config(args.config)
    pcfg = dict(cfg["pide"])
    if args.local_smoke:
        files, heap = (
            load_paths(cfg["local_smoke"]["files"]),
            cfg["local_smoke"]["field"],
        )
    else:
        files = load_corpus(
            cfg["corpus"], args.files.split(",") if args.files else None
        )
        heap = cfg["session"]["field"]
    out_dir = cell_dir(Path(args.out), args.workers, args.rep)
    pide = PIDEDriver(
        heap=heap,
        topology="A",
        k=args.workers,
        command=list(pcfg.get("command", ["isabelle", "pide_mcp"])),
        extra_args=list(pcfg.get("extra_args") or []),
        start_timeout_s=float(pcfg.get("start_timeout_s", 900)),
        no_build=bool(pcfg.get("no_build", True)),
        log_dir=str(out_dir / "pide-logs"),
        session_options=[str(o) for o in pcfg.get("session_options") or []],
    )
    return await run_cli_cell(
        cfg,
        run_cell,
        (
            pcfg,
            files,
            load_census(args.census),
            args.workers,
            args.quota or int(cfg["files_per_worker"]),
        ),
        heap=heap,
        seed=int(cfg["seed"]) + args.rep,
        out_dir=out_dir,
        head={"experiment": "pide", "workers": args.workers, "rep": args.rep},
        extra={"census_path": args.census, "local_smoke": bool(args.local_smoke)},
        url=None,
        external=False,
        label=f"pide W={args.workers} rep={args.rep}",
        driver=pide,
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """CLI (T1's options: --workers, --rep, --quota, --files)."""
    return worker_arg_parser(__doc__).parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    return asyncio.run(amain(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
