"""The census (design B2): one replay of the corpus at W = 1 with the maximum
budget. The server's per-command reports give each block's command count;
``census.json`` holds them and every later table is keyed on its id.

    python -m trainmetrics.census [--config config.yaml] [--files A,B] [--out results/census]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

from . import EXPERIMENT_DIR
from .corpus import CorpusFile, corpus_sha, load_corpus, load_paths, sha256_text
from .replay import BUDGET_MAX_S, ReplayRecord, SessionRef, replay_file

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
        "files_entered": sum(1 for f in files if not f["enter_error"]),
        "blocks": len(blocks),
        "commands": sum(int(b["n_commands"] or 0) for b in blocks),
        "proved_blocks": sum(1 for b in blocks if b["proved"]),
        "timed_out_blocks": sum(1 for b in blocks if b["timed_out"]),
        "partial_count_blocks": sum(1 for b in blocks if b.get("count_partial")),
        "error_blocks": sum(1 for b in blocks if b["error"]),
    }


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


def load_census(path: str) -> Dict[str, Any]:
    """A census.json."""
    with open(path, "r", encoding="utf-8") as fh:
        return dict(json.load(fh))


async def run_census(  # pylint: disable=too-many-locals
    client: Any,
    files: Sequence[CorpusFile],
    *,
    field: str,
    session_theories: List[str],
) -> List[ReplayRecord]:
    """Replay every file once on one session at the maximum budget."""
    acq = await client.acquire_session(
        theories=session_theories,
        field=field,
        reuse_dirty=False,
        label="trainmetrics-census",
    )
    sess = SessionRef(str(acq["session_id"]), str(acq["lease_id"]), worker=0)
    records: List[ReplayRecord] = []
    try:
        for n, cf in enumerate(files, 1):
            rr = await replay_file(client, sess, cf, budget_s=BUDGET_MAX_S)
            records.append(rr)
            proved = sum(1 for b in rr.blocks if b.proved)
            print(
                f"[census {n}/{len(files)}] {cf.stem}: {len(rr.blocks)} blocks, "
                f"{proved} proved, enter {rr.enter_s:.1f}s"
                + (f", ENTER ERROR {rr.enter_error}" if rr.enter_error else ""),
                flush=True,
            )
    finally:
        try:
            await client.close_session(sess.session_id, lease_id=sess.lease_id)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
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


async def _main_async(args: argparse.Namespace) -> int:
    from memscale.phases import (  # pylint: disable=import-outside-toplevel
        environment_block,
    )

    from client.async_client import (  # pylint: disable=import-outside-toplevel
        PoolAsyncClient,
    )

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
    url = args.url or cfg["ips"]["url"]
    async with PoolAsyncClient(url) as client:
        try:
            report = await client.memory_status()  # limits and state, for the manifest
        except Exception as exc:  # pylint: disable=broad-exception-caught
            report = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        t0 = time.time()
        records = await run_census(
            client,
            files,
            field=field,
            session_theories=list(cfg["session"]["theories"]),
        )
    body = write_census(
        Path(args.out),
        files,
        records,
        {
            "url": url,
            "subset": only,
            "local_smoke": bool(args.local_smoke),
            "field": field,
            "wall_s": round(time.time() - t0, 1),
            "budget_s": BUDGET_MAX_S,
            "server_report": report,
            "environment": environment_block(cfg.get("isabelle_home")),
        },
    )
    print(json.dumps({"census_id": body["census_id"], **body["totals"]}))
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
        "--local-smoke",
        action="store_true",
        help="the config's local_smoke files on its field (HOL), for the container",
    )
    ap.add_argument("--out", default=str(EXPERIMENT_DIR / "results" / "census"))
    return asyncio.run(_main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
