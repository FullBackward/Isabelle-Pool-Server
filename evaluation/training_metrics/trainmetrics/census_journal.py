"""The census's journal and progress lines (owner, 2026-10-25): an interrupted
census keeps every file it finished, and a block that runs long shows.

``files.jsonl`` beside ``census.json``: a first line with the run's key, then
one replay record per finished file, written when the file is done. A rerun
with the same key (census version, corpus, code commit, ``ips.env``, field,
budget) continues after them; any other key starts again. A file whose
acquire or header failed carries no blocks and is run again.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import REPO_ROOT
from .corpus import CorpusFile, corpus_sha
from .replay import BlockRecord, ReplayRecord

JOURNAL = "files.jsonl"
SLOW_BLOCK_S = 120.0  # a block at least this long gets a progress line


def git_commit() -> Optional[str]:
    """HEAD of the repository (None without git)."""
    try:
        res = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() or None


def record_from_dict(d: Dict[str, Any]) -> ReplayRecord:
    """A replay record from its JSON form (``ReplayRecord.as_dict``)."""
    names = {f.name for f in dataclasses.fields(ReplayRecord)}
    rr = ReplayRecord(**{k: v for k, v in d.items() if k in names and k != "blocks"})
    bnames = {f.name for f in dataclasses.fields(BlockRecord)}
    rr.blocks = [
        BlockRecord(**{k: v for k, v in b.items() if k in bnames})
        for b in d.get("blocks") or []
    ]
    return rr


def _redo(rr: ReplayRecord) -> bool:
    return bool(rr.acquire_error or rr.enter_error)


def load_journal(
    path: Path, key: Dict[str, Any]
) -> Tuple[Dict[str, ReplayRecord], Optional[str]]:
    """The finished files of an earlier run with this ``key`` (stem -> record),
    or ``({}, why)`` when there is nothing to continue."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}, "no earlier run"
    try:
        head = json.loads(lines[0]) if lines else {}
        if head.get("key") != key:
            changed = sorted(k for k in key if (head.get("key") or {}).get(k) != key[k])
            return {}, f"the inputs changed: {', '.join(changed) or 'key'}"
        done = {}
        for line in lines[1:]:
            if line.strip():
                rr = record_from_dict(json.loads(line))
                if not _redo(rr):
                    done[rr.stem] = rr
    except (ValueError, TypeError) as exc:
        return {}, f"unreadable journal ({type(exc).__name__})"
    return done, None


def start_journal(
    path: Path, key: Dict[str, Any], kept: Sequence[ReplayRecord]
) -> None:
    """Write the key and the records that are kept (a fresh start keeps none;
    the earlier journal is moved to ``files.jsonl.prev``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not kept:
        path.replace(path.with_name(JOURNAL + ".prev"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"key": key}) + "\n")
        for rr in kept:
            fh.write(json.dumps(rr.as_dict()) + "\n")


def append_journal(path: Path, rr: ReplayRecord) -> None:
    """One finished file, flushed at once."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rr.as_dict()) + "\n")
        fh.flush()


def block_progress(
    n: int, total: int, cf: CorpusFile, slow_s: float = SLOW_BLOCK_S
) -> Callable[[BlockRecord], None]:
    """A line for each block of ``cf`` that timed out or ran ``slow_s`` or
    longer: its number, outcome, time and first line."""

    def show(b: BlockRecord) -> None:
        if not b.timed_out and b.wall_s < slow_s:
            return
        what = "timed out" if b.timed_out else "proved" if b.proved else "not proved"
        lines = [ln.strip() for ln in cf.blocks[b.index].splitlines() if ln.strip()]
        print(
            f"  [census {n}/{total}] {cf.stem} block {b.index + 1}/{len(cf.blocks)}: "
            f"{what} after {b.wall_s:.0f}s ({(lines or [''])[0][:60]})",
            flush=True,
        )

    return show


def journal_key(
    files: Sequence[CorpusFile],
    cfg: Dict[str, Any],
    field: str,
    budget_s: float,
    version: int,
) -> Dict[str, Any]:
    """What must not change between an interrupted census and its rerun."""
    return {
        "census_version": version,
        "corpus_sha": corpus_sha(files),
        "commit": git_commit(),
        "ips_env": dict((cfg.get("ips") or {}).get("env") or {}),
        "field": field,
        "budget_s": budget_s,
    }


def kept_in_order(
    files: Sequence[CorpusFile], done: Dict[str, ReplayRecord]
) -> List[ReplayRecord]:
    """The kept records in corpus order (files no longer listed are dropped)."""
    return [done[cf.stem] for cf in files if cf.stem in done]
