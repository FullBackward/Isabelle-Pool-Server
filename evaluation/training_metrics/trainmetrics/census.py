"""The census (design B2): one replay of the corpus at W = 1 with the maximum
budget. The server's per-command reports give each block's command count;
``census.json`` holds them and every later table is keyed on its id. Each
finished file goes to ``files.jsonl`` at once; a rerun continues after them
(``census_journal``; ``--fresh`` starts again).

    python -m trainmetrics.census [--config config.yaml] [--files A,B] [--out results/census] [--fresh]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

from . import EXPERIMENT_DIR, REPO_ROOT
from .census_journal import (
    JOURNAL,
    append_journal,
    block_progress,
    journal_key,
    kept_in_order,
    load_journal,
    start_journal,
)
from .corpus import CorpusFile, corpus_sha, load_corpus, load_paths, sha256_text
from .replay import BUDGET_MAX_S, ReplayRecord, replay_file_fresh

CENSUS_VERSION = 1


def load_config(path: str) -> Dict[str, Any]:
    """config.yaml as a dict."""
    with open(path, "r", encoding="utf-8") as fh:
        return dict(yaml.safe_load(fh) or {})


def census_id(files: Sequence[Dict[str, Any]]) -> str:
    """sha256 over (stem, block sha, command count) in order: two censuses with
    the same id count the same commands for the same blocks."""
    lines = [
        f"{f['stem']} {b['sha']} {b['n_commands']}"
        for f in files
        for b in f.get("blocks", [])
    ]
    return sha256_text("\n".join(lines))


def summarize(records: Sequence[ReplayRecord]) -> List[Dict[str, Any]]:
    """census.json's ``files`` list from replay records."""
    out: List[Dict[str, Any]] = []
    for rr in records:
        out.append(
            {
                "stem": rr.stem,
                "theory_name": rr.theory_name,
                "acquire_s": rr.acquire_s,
                "acquire_error": rr.acquire_error,
                "enter_s": rr.enter_s,
                "enter_error": rr.enter_error,
                "blocks": [
                    {
                        "index": b.index,
                        "sha": b.block_sha,
                        "n_commands": b.n_commands,
                        # a timed-out block's report is partial: its count is a lower
                        # bound; the budget rule gives it the maximum and T2's size
                        # classes leave it out (review 86afb15 S3)
                        "count_partial": b.timed_out,
                        "proved": b.proved,
                        "success": b.success,
                        "timed_out": b.timed_out,
                        "execution_time": b.execution_time,
                        "wall_s": b.wall_s,
                        "statuses": b.statuses,
                        "error": b.error,
                        "error_kind": b.error_kind,
                        # why a block is not proved: its first error message and
                        # the block line of its first failed command (2026-10-25);
                        # not in the census id
                        "first_error": b.first_error,
                        "first_failed_line": b.first_failed_line,
                        # stage 3d: the commands' spans (T6b), not in the census id
                        "spans": b.spans,
                        "spans_error": b.spans_error,
                    }
                    for b in rr.blocks
                ],
            }
        )
    return out


def totals(files: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Counts over the census."""
    blocks = [b for f in files for b in f["blocks"]]
    return {
        "files": len(files),
        "files_entered": sum(
            1 for f in files if not f["enter_error"] and not f.get("acquire_error")
        ),
        "blocks": len(blocks),
        "commands": sum(int(b["n_commands"] or 0) for b in blocks),
        "proved_blocks": sum(1 for b in blocks if b["proved"]),
        "timed_out_blocks": sum(1 for b in blocks if b["timed_out"]),
        "partial_count_blocks": sum(1 for b in blocks if b.get("count_partial")),
        "error_blocks": sum(1 for b in blocks if b["error"]),
        "spans_failed_blocks": len(spans_failures(files)),
    }


def spans_failures(files: Sequence[Dict[str, Any]]) -> List[str]:
    """``stem#index: why`` for every proved block whose spans do not
    re-assemble it (stage 3d; plan_v1.6 S7: the census checks the server
    corpus too). Blocks that did not prove are not judged (T6b uses proved
    theorems only)."""
    return [
        f"{f['stem']}#{b['index']}: {b.get('spans_error')}"
        for f in files
        for b in f["blocks"]
        if b.get("proved") and b.get("spans_error")
    ]


def proved_gate(tot: Dict[str, Any], need: float) -> Optional[str]:
    """None when the census proved at least ``need`` of its blocks, else the
    failure line (review afd3de9 B1: a census run without the files' imports
    proved 13 of 383 blocks and still said ok)."""
    blocks = int(tot.get("blocks") or 0)
    proved = int(tot.get("proved_blocks") or 0)
    frac = proved / blocks if blocks else 0.0
    if blocks and frac < need:
        return (
            f"CENSUS FAILED THE GATE: {proved} of {blocks} blocks proved ({frac:.1%}) "
            f"< {need:.0%} (config census_min_proved_fraction)"
        )
    return None


def counts_by_stem(census: Dict[str, Any]) -> Dict[str, List[Optional[int]]]:
    """stem -> per-block command counts (the budget rule's input; a partial
    count is returned as recorded, see :func:`partial_by_stem`)."""
    return {f["stem"]: [b["n_commands"] for b in f["blocks"]] for f in census["files"]}


def partial_by_stem(census: Dict[str, Any]) -> Dict[str, List[bool]]:
    """stem -> per block, whether its census count is partial (timed out)."""
    return {
        f["stem"]: [bool(b.get("count_partial")) for b in f["blocks"]]
        for f in census["files"]
    }


def corpus_manifest(corpus_dir: str) -> Optional[Dict[str, Any]]:
    """The prepared corpus's MANIFEST.json facts (beside ``processed/``): which
    Isabelle the theories came from and their checksum; None when absent."""
    root = Path(corpus_dir)
    if not root.is_absolute():
        root = REPO_ROOT / root
    path = root.parent / "MANIFEST.json"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            m = dict(json.load(fh))
    except (OSError, ValueError):
        return None
    return {
        k: m.get(k)
        for k in (
            "isabelle_version",
            "isabelle_id",
            "source_dir",
            "corpus_sha256",
            "missing",
        )
    }


def load_census(path: str) -> Dict[str, Any]:
    """A census.json."""
    with open(path, "r", encoding="utf-8") as fh:
        return dict(json.load(fh))


async def run_census(
    client: Any,
    files: Sequence[CorpusFile],
    *,
    field: str,
    unclosed: Optional[List[Dict[str, Any]]] = None,
    done: Optional[Dict[str, ReplayRecord]] = None,
    journal: Optional[Path] = None,
) -> List[ReplayRecord]:
    """Replay every file once at the maximum budget, each on a fresh session
    acquired with the file's own imports (D6, review afd3de9 B1) — the same
    condition as every T1 worker; sessions that stay open are listed. A file
    in ``done`` (an earlier run's record) is not run again; each new record is
    appended to ``journal``."""
    records: List[ReplayRecord] = []
    for n, cf in enumerate(files, 1):
        if done and cf.stem in done:
            records.append(done[cf.stem])
            continue
        rr = await replay_file_fresh(
            client,
            cf,
            heap=field,
            budget_s=BUDGET_MAX_S,
            label="trainmetrics-census",
            keep_spans=True,  # stage 3d: the step split for T6b
            on_block=block_progress(n, len(files), cf),
        )
        records.append(rr)
        if journal is not None:
            append_journal(journal, rr)
        if unclosed is not None:
            unclosed.extend(rr.unclosed)
        proved = sum(1 for b in rr.blocks if b.proved)
        err = rr.acquire_error or rr.enter_error
        print(
            f"[census {n}/{len(files)}] {cf.stem}: {len(rr.blocks)} blocks, "
            f"{proved} proved, acquire {rr.acquire_s:.1f}s, enter {rr.enter_s:.1f}s"
            + (f", ERROR {err}" if err else ""),
            flush=True,
        )
    return records


def write_census(
    out_dir: Path,
    files: Sequence[CorpusFile],
    records: Sequence[ReplayRecord],
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    """census.json (+ the full replay records beside it)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    flist = summarize(records)
    body: Dict[str, Any] = {
        "version": CENSUS_VERSION,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "corpus_sha": corpus_sha(files),
        "files": flist,
        "totals": totals(flist),
        **meta,
    }
    body["census_id"] = census_id(flist)
    with open(out_dir / "census.json", "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=1)
    with open(out_dir / "replays.jsonl", "w", encoding="utf-8") as fh:
        for rr in records:
            fh.write(json.dumps(rr.as_dict()) + "\n")
    return body


async def _main_async(  # pylint: disable=too-many-locals
    args: argparse.Namespace,
) -> int:
    # pylint: disable=import-outside-toplevel
    from memscale.phases import environment_block

    from client.async_client import PoolAsyncClient

    from .serve import isabelle_cmd, make_driver

    cfg = load_config(args.config)
    only = [s for s in args.files.split(",") if s] if args.files else None
    field = cfg["session"]["field"]
    if args.local_smoke:  # HOL theories on a HOL heap (no HOL-Analysis heap locally)
        files = load_paths(cfg["local_smoke"]["files"])
        field = cfg["local_smoke"]["field"]
    else:
        files = load_corpus(cfg["corpus"], only)
    if not files:
        print("no corpus files")
        return 2
    journal = Path(args.out) / JOURNAL
    key = journal_key(files, cfg, field, BUDGET_MAX_S, CENSUS_VERSION)
    done, why = ({}, "--fresh") if args.fresh else load_journal(journal, key)
    print(
        (
            f"census: starting at the first file ({why})"
            if why
            else f"census: continuing, {len(done)} of {len(files)} files done earlier"
        ),
        flush=True,
    )
    start_journal(journal, key, kept_in_order(files, done))
    driver = make_driver(cfg, field, args.url, bool(args.ips_external))
    t0 = time.time()
    try:
        try:
            await driver.setup()  # a fresh server unless external (as the T1 cells)
        except (RuntimeError, TimeoutError) as exc:  # busy port, dead or wrong server
            print(f"census not started: {exc}", flush=True)
            return 2
        async with PoolAsyncClient(driver.url, timeout=BUDGET_MAX_S + 120.0) as client:
            try:
                report = await client.memory_status()  # limits and state
            except Exception as exc:  # pylint: disable=broad-exception-caught
                report = {"error": f"{type(exc).__name__}: {exc}"[:200]}
            unclosed: List[Dict[str, Any]] = []
            records = await run_census(
                client,
                files,
                field=field,
                unclosed=unclosed,
                done=done,
                journal=journal,
            )
        driver_info = driver.describe()
    finally:
        await driver.stop_all()
    url = driver.url
    body = write_census(
        Path(args.out),
        files,
        records,
        {
            "url": url,
            "subset": only,
            "local_smoke": bool(args.local_smoke),
            "field": field,
            "wall_s": round(time.time() - t0, 1),  # this run's (resumed files apart)
            "resumed_files": sum(1 for cf in files if cf.stem in done),
            "budget_s": BUDGET_MAX_S,
            "session_per_file": True,
            "corpus_manifest": (
                corpus_manifest(cfg["corpus"]) if not args.local_smoke else None
            ),
            "unclosed_sessions": unclosed,
            "server_report": report,
            "driver": driver_info,
            "environment": environment_block(cfg.get("isabelle_home"), isabelle_cmd()),
        },
    )
    print(json.dumps({"census_id": body["census_id"], **body["totals"]}))
    # the gate that would have caught the import bug (review afd3de9 B1): a
    # census that proves too little measured the wrong thing
    msg = proved_gate(body["totals"], float(cfg.get("census_min_proved_fraction", 0.0)))
    if msg:
        print(
            f"{msg}; inspect {Path(args.out) / 'census.json'} before running any cell"
        )
        return 3
    bad = spans_failures(body["files"])
    if bad:  # plan_v1.6 S7: fail loudly, the T6b step split would be wrong
        print(
            f"CENSUS SPANS FAILED for {len(bad)} proved blocks (first: {bad[0]}); "
            f"inspect {Path(args.out) / 'census.json'} before running T6b. "
            "The census itself is written and valid: T1-T5 can proceed, only T6b's "
            "step split needs the spans (review c6aa372 S1)"
        )
        return 4
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", default=str(EXPERIMENT_DIR / "config.yaml"))
    ap.add_argument("--files", default="", help="comma-separated stems (smoke)")
    ap.add_argument("--url", default=None)
    ap.add_argument(
        "--ips-external",
        action="store_true",
        help="use the server at --url / the config url instead of starting one",
    )
    ap.add_argument(
        "--local-smoke",
        action="store_true",
        help="the config's local_smoke files on its field (HOL), for the container",
    )
    ap.add_argument("--out", default=str(EXPERIMENT_DIR / "results" / "census"))
    ap.add_argument(
        "--fresh",
        action="store_true",
        help="ignore the files of an earlier, interrupted run (files.jsonl)",
    )
    return asyncio.run(_main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
