"""T4 agreement (design §T4, plan stage 4b): does "proved" in-session mean
proved by a cold build?

- Part A, the corpus as written: the in-session verdict per file comes from
  the census (a W = 1 replay at the maximum budget, a session per file); the
  cold verdict from ``verify_bigstep_text`` of the file as written.
- Part B, the mutation suite (:mod:`trainmetrics.mutate`): the sampled
  theorem blocks in four variants. In-session, one replay per file: at a
  sampled block each mutant is checked under the original block's census
  budget (plan review S3) and a kept one is rolled back, then the original
  block (the unchanged variant) advances the file. Cold, one build per block
  and mutant, the mutant spliced into the file (one mutation per build,
  design review S7); the unchanged variant's build is part A's.

Cold builds run ``build_concurrency`` at a time; the server's cap
(``ISABELLE_MAX_CONCURRENT_BIGSTEP_BUILDS``) is not reported, so the cell polls
the builds in flight and records the most it saw (plan review S4). A refused
build (503 memory_pressure / pool_full) waits Retry-After and is retried.

    python -m trainmetrics.run_t4 [--local-smoke] [--ips-external]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from . import mutate
from .census import load_census, load_config
from .contention import tier_poll, tier_summary
from .corpus import CorpusFile, corpus_sha, load_corpus, load_paths
from .harness import cell_arg_parser, run_cli_cell
from .metrics_t4 import summarize_t4
from .metrics_t5 import load_samples
from .replay import RETRYABLE, SessionRef, _acquire_retrying, close_all, verify_in_file
from .run_t1 import usable_stems
from .spans import strip_comments
from .t5_core import DEAD, classify

BUILD_RETRIES = 12
# the header: "theory NAME" at the start of a line, then "imports" (review
# 51f1be2 S1: a markup command before it may contain the word "theory")
THEORY_RE = re.compile(r'^\s*theory\s+"?([^\s"]+)"?\s+imports\b', re.M)


def declared_name(file_text: str) -> str:
    """The theory name the file's header declares (the cold build requires
    it; a file need not be named after its theory, e.g.
    examples/mcp_lsp_demo.thy declares McpLspDemo). Comments are stripped
    first; a markup command before the header (``section \\<open>The theory
    of …\\<close>``) is skipped because the match is anchored on a line
    start and needs ``imports``."""
    m = THEORY_RE.search(strip_comments(file_text))
    return m.group(1) if m else ""


@dataclass
class VariantRecord:  # pylint: disable=too-many-instance-attributes
    """One block in one variant: its in-session and its cold verdict."""

    stem: str
    index: int
    variant: str
    applicable: bool
    in_session: Optional[Dict[str, Any]] = None
    cold: Optional[Dict[str, Any]] = None
    stopped: Optional[str] = None  # the file's session died before this variant


def _session_view(rec: Any, rolled_back: bool) -> Dict[str, Any]:
    return {
        "outcome": classify(rec),
        "proved": rec.proved,
        "success": rec.success,
        "proof_open": rec.proof_open,
        "used_sorry": rec.used_sorry,
        "timed_out": rec.timed_out,
        "error_kind": rec.error_kind,
        "first_error": rec.first_error,
        "wall_s": rec.wall_s,
        "rolled_back": rolled_back,
    }


async def _mutants_at(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    client: Any,
    sess: SessionRef,
    cf: CorpusFile,
    i: int,
    row: Dict[str, Any],
    out: List[VariantRecord],
) -> Optional[str]:
    """The three mutants of block i, each under the original block's budget,
    a kept one rolled back; returns why the session is gone, if it is."""
    variants = mutate.variants(cf.blocks[i])
    for name in ("sorry", "truncated", "wrong_fact"):
        text = variants[name]
        if text is None:
            out.append(VariantRecord(cf.stem, i, name, False))
            continue
        rec = await verify_in_file(
            client, sess, cf, i, row, text=text, block_sha=f"t4:{name}"
        )
        kept = classify(rec) == "kept"
        if kept:
            await client.rollback(sess.session_id, lease_id=sess.lease_id)
        out.append(
            VariantRecord(cf.stem, i, name, True, in_session=_session_view(rec, kept))
        )
        if rec.error_kind in DEAD:
            return rec.error_kind
    return None


async def in_session_file(  # pylint: disable=too-many-arguments,too-many-locals
    client: Any,
    cf: CorpusFile,
    picks: Set[int],
    census_file: Dict[str, Any],
    *,
    heap: str,
    registry: Dict[str, str],
    on_change: Callable[[Dict[str, str]], Any],
) -> List[VariantRecord]:
    """One replay of ``cf`` up to its last sampled block with every variant
    checked at the sampled blocks; a session per file (D6)."""
    out: List[VariantRecord] = []
    rows = census_file.get("blocks") or []
    try:
        acq, _r, _w = await _acquire_retrying(client, cf, heap, "trainmetrics-t4")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        why = f"acquire: {type(exc).__name__}: {exc}"[:200]
        return [
            VariantRecord(cf.stem, i, v, True, stopped=why)
            for i in sorted(picks)
            for v in mutate.VARIANTS
        ]
    sess = SessionRef(str(acq["session_id"]), str(acq["lease_id"]))
    registry[sess.session_id] = sess.lease_id
    on_change(registry)
    stopped: Optional[str] = None
    try:
        await client.enter_theory(
            sess.session_id,
            sess.new_name(cf.stem),
            imports=list(cf.imports),
            lease_id=sess.lease_id,
        )
        for i in range(max(picks) + 1):
            row = rows[i] if i < len(rows) else {}
            if i in picks:
                stopped = await _mutants_at(client, sess, cf, i, row, out)
            if stopped:
                break
            blk = await verify_in_file(client, sess, cf, i, row)
            if i in picks:
                out.append(
                    VariantRecord(
                        cf.stem,
                        i,
                        "unchanged",
                        True,
                        in_session=_session_view(blk, False),
                    )
                )
            stopped = blk.error_kind if blk.error_kind in DEAD else None
            if stopped:
                break
    except Exception as exc:  # pylint: disable=broad-exception-caught
        stopped = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        unclosed = await close_all(client, [sess])
        if not unclosed:
            registry.pop(sess.session_id, None)
        on_change(registry)
    have = {(r.index, r.variant) for r in out}
    out += [
        VariantRecord(cf.stem, i, v, True, stopped=stopped or "not reached")
        for i in sorted(picks)
        for v in mutate.VARIANTS
        if (i, v) not in have
    ]
    return out


async def cold_build(  # pylint: disable=too-many-arguments
    client: Any,
    stem: str,
    text: str,
    *,
    heap: str,
    timeout_s: float,
    sem: asyncio.Semaphore,
) -> Dict[str, Any]:
    """The cold verdict of ``text`` (theory ``stem``); a refusal waits
    Retry-After and is retried."""
    retries = 0
    async with sem:
        t0 = time.monotonic()
        while True:
            try:
                r = await client.verify_bigstep_text(
                    stem, text, field=heap, timeout=timeout_s
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                return {
                    "accepted": None,
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                    "wall_s": round(time.monotonic() - t0, 3),
                    "retries": retries,
                }
            try:
                body = r.json() if r.content else {}
            except ValueError:
                body = {}
            if (
                r.status_code == 503
                and body.get("error") in RETRYABLE
                and retries < BUILD_RETRIES
            ):
                retries += 1
                await asyncio.sleep(max(1.0, float(r.headers.get("Retry-After", 5))))
                continue
            ok = r.status_code == 200
            wall = round(time.monotonic() - t0, 3)
            return {
                "accepted": bool(body.get("success")) if ok else None,
                "http": r.status_code,
                "error": (
                    None
                    if body.get("success")
                    else str(body.get("error") or body.get("detail") or "")[-300:]
                ),
                "server_s": body.get("execution_time"),
                # the server keeps successful builds in a per-process cache and
                # answers a hit with execution_time 0.0 (review a0ddeaa S1)
                "cached": bool(
                    ok
                    and body.get("success")
                    and body.get("execution_time") == 0.0
                    and wall < 0.5
                ),
                "wall_s": wall,
                "retries": retries,
            }


def part_a_row(cf: CorpusFile, census_file: Dict[str, Any]) -> Dict[str, Any]:
    """A file's in-session verdict from the census: every block proved."""
    blocks = census_file.get("blocks") or []
    bad = next((b for b in blocks if not b.get("proved")), None)
    return {
        "stem": cf.stem,
        "blocks": len(blocks),
        "in_session_proved": bad is None and len(blocks) == len(cf.blocks),
        "first_unproved": (
            None
            if bad is None
            else {
                "index": bad["index"],
                "error": bad.get("error"),
                "timed_out": bad.get("timed_out"),
            }
        ),
    }


async def _in_session_pass(  # pylint: disable=too-many-arguments
    client: Any,
    usable: Sequence[CorpusFile],
    picks: Sequence[Any],
    rows: Dict[str, Dict[str, Any]],
    *,
    heap: str,
    workers: int,
    registry: Dict[str, str],
    on_change: Callable[[Dict[str, str]], Any],
) -> List[VariantRecord]:
    """Part B in-session: the files with sampled blocks, ``workers`` at a time."""
    by_file: Dict[str, Set[int]] = {}
    for stem, i in picks:
        by_file.setdefault(stem, set()).add(i)
    queue = [cf for cf in usable if cf.stem in by_file]
    records: List[VariantRecord] = []

    async def worker() -> None:
        while queue:
            cf = queue.pop(0)
            records.extend(
                await in_session_file(
                    client,
                    cf,
                    by_file[cf.stem],
                    rows[cf.stem],
                    heap=heap,
                    registry=registry,
                    on_change=on_change,
                )
            )

    await asyncio.gather(*(worker() for _ in range(workers)))
    return records


def _mutant_text(cf: CorpusFile, file_text: str, v: VariantRecord) -> str:
    """The file with block ``v.index`` replaced by its ``v.variant`` mutant."""
    block = mutate.variants(cf.blocks[v.index])[v.variant] or ""
    return mutate.splice(file_text, cf.blocks, v.index, block)


def _assign(
    part_a: List[Dict[str, Any]],
    mutants: List[VariantRecord],
    records: List[VariantRecord],
    results: Sequence[Dict[str, Any]],
) -> None:
    """The cold verdicts into their records; an unchanged variant takes part
    A's build of its file (the file as written)."""
    for a, cold in zip(part_a, results[: len(part_a)]):
        a["cold"] = cold
    for v, cold in zip(mutants, results[len(part_a) :]):
        v.cold = cold
    a_cold = {a["stem"]: a["cold"] for a in part_a}
    for v in records:
        if v.variant == "unchanged":
            v.cold = a_cold.get(v.stem)


async def _cold_pass(  # pylint: disable=too-many-arguments
    client: Any,
    t4: Dict[str, Any],
    usable: Sequence[CorpusFile],
    part_a: List[Dict[str, Any]],
    records: List[VariantRecord],
    *,
    heap: str,
) -> None:
    """Every cold build (part A's files, part B's mutants), ``build_concurrency``
    at a time; fills ``part_a[*]["cold"]`` and ``records[*].cold``."""
    sem = asyncio.Semaphore(int(t4.get("build_concurrency", 1)))
    kw: Dict[str, Any] = {
        "heap": heap,
        "timeout_s": float(t4.get("build_timeout_s", 1200)),
        "sem": sem,
    }
    texts = {cf.stem: Path(cf.path).read_text(encoding="utf-8") for cf in usable}
    by_stem = {cf.stem: cf for cf in usable}
    mutants = [v for v in records if v.applicable and v.variant != "unchanged"]
    name = {stem: declared_name(text) for stem, text in texts.items()}
    jobs = [cold_build(client, name[a["stem"]], texts[a["stem"]], **kw) for a in part_a]
    jobs += [
        cold_build(
            client, name[v.stem], _mutant_text(by_stem[v.stem], texts[v.stem], v), **kw
        )
        for v in mutants
    ]
    _assign(part_a, mutants, records, await asyncio.gather(*jobs))


async def run_cell(  # pylint: disable=too-many-arguments,too-many-locals
    client: Any,
    t4: Dict[str, Any],
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
    """Part A and part B; fills and returns the manifest body."""
    rows = {f["stem"]: f for f in census.get("files", [])}
    usable = [cf for cf in files if cf.stem in set(usable_stems(files, census))]
    picks = mutate.sample_blocks(usable, census, int(t4["mutation_blocks"]), seed)
    body = body if body is not None else {}
    body.update(
        {
            "experiment": "t4",
            "seed": seed,
            "heap": heap,
            "t4": t4,
            "census_id": census.get("census_id"),
            "corpus_sha": corpus_sha(files),
            "files": [cf.stem for cf in usable],
            "picks": picks,
            "outcome": "running",
        }
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    registry: Dict[str, str] = {}
    body["open_sessions"] = registry

    def changed(_reg: Dict[str, str]) -> None:
        if checkpoint is not None:
            checkpoint(body)

    part_a = [part_a_row(cf, rows[cf.stem]) for cf in usable]
    if sampler is not None:
        sampler.phase("run")
    body["window_start"] = time.time()
    async with tier_poll(client, out_dir / "tiers.jsonl", float(t4.get("poll_s", 2))):
        records = await _in_session_pass(
            client,
            usable,
            picks,
            rows,
            heap=heap,
            workers=int(t4.get("workers", 1)),
            registry=registry,
            on_change=changed,
        )
        body["in_session_done"] = time.time()
        await _cold_pass(client, t4, usable, part_a, records, heap=heap)
    body["window_end"] = time.time()
    if sampler is not None:
        sampler.phase("stop")
    b_rows = [
        asdict(v)
        for v in sorted(
            records, key=lambda v: (v.stem, v.index, mutate.VARIANTS.index(v.variant))
        )
    ]
    for name, data in (("part_a.jsonl", part_a), ("part_b.jsonl", b_rows)):
        with open(out_dir / name, "w", encoding="utf-8") as fh:
            for d in data:
                fh.write(json.dumps(d) + "\n")
    tiers = tier_summary(load_samples(out_dir / "tiers.jsonl"))
    body["summary"] = {**summarize_t4(part_a, b_rows), "tiers": tiers}
    body["builds_bigstep_max_seen"] = tiers.get("builds_bigstep_max")
    cached = sum(1 for a in part_a if (a.get("cold") or {}).get("cached")) + sum(
        1
        for r in b_rows
        if r["variant"] != "unchanged" and (r.get("cold") or {}).get("cached")
    )
    body["cold_cached"] = cached
    if cached and not (body.get("server") or {}).get("external"):
        body["warning"] = (
            f"{cached} cold verdicts came from the server's build cache on a fresh server "
            "(expected none): inspect before reading build times"
        )
        print(f"WARNING: {body['warning']}", flush=True)
    body["unclosed_sessions"] = [
        {"session_id": k, "lease_id": v} for k, v in registry.items()
    ]
    body["outcome"] = "ok" if part_a else "empty"
    return body


async def amain(args: argparse.Namespace) -> int:
    """One T4 cell under the shared harness."""
    cfg = load_config(args.config)
    t4 = dict(cfg["t4"])
    if args.local_smoke:
        files = load_paths(cfg["local_smoke"]["files"])
        heap = cfg["local_smoke"]["field"]
        t4.update(t4.get("local_smoke") or {})
    else:
        files = load_corpus(
            cfg["corpus"], args.files.split(",") if args.files else None
        )
        heap = cfg["session"]["field"]
    if args.mutation_blocks:
        t4["mutation_blocks"] = args.mutation_blocks
    return await run_cli_cell(
        cfg,
        run_cell,
        (t4, files, load_census(args.census)),
        heap=heap,
        seed=int(cfg["seed"]) + args.rep,
        out_dir=Path(args.out) / "t4" / f"rep{args.rep}",
        head={"experiment": "t4", "rep": args.rep},
        extra={
            "census_path": args.census,
            "ips_env": cfg["ips"].get("env", {}),
            "local_smoke": bool(args.local_smoke),
        },
        url=args.url,
        external=bool(args.ips_external),
        label=f"t4 rep={args.rep}",
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """CLI."""
    ap = cell_arg_parser(__doc__)
    ap.add_argument("--rep", type=int, default=0)
    ap.add_argument("--files", default="", help="restrict the corpus to these stems")
    ap.add_argument(
        "--mutation-blocks",
        type=int,
        default=0,
        help="override config t4.mutation_blocks",
    )
    return ap.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    return asyncio.run(amain(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
