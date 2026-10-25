"""The census's journal and progress lines (owner, 2026-10-25): an interrupted
census keeps every file it finished, and a block that runs long shows.

``files.jsonl`` beside ``census.json``: a first line with the run's key, then
one replay record per finished file, written when the file is done. A rerun
with the same key (census version, corpus, code commit, ``ips.env``, field,
budget) continues after them. A key that differs only in the commit keeps each
file whose blocks did not change, when the commits between change nothing a
replay runs (D16, :func:`load_unchanged`; ``--keep-unchanged`` keeps them
whatever the commits change, D17); any other key starts again. A file whose
acquire or header failed carries no blocks and is run again.
"""

from __future__ import annotations

import ast
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
# D16: what a replay runs. A commit that changes none of these paths and none of
# these definitions replays an unchanged file as before (the blocks themselves
# are compared file by file, so the rest of the splitter may change)
REPLAY_PATHS = (
    "server",
    "client",
    "evaluation/memory_scaling/memscale/drive_ips.py",
    "evaluation/training_metrics/config.yaml",
    "evaluation/training_metrics/trainmetrics/replay.py",
    "evaluation/training_metrics/trainmetrics/serve.py",
)
REPLAY_DEFS = {
    "evaluation/scripts/theory_splitter.py": (
        "THEORY_RE",
        "HEADER_RE",
        "END_RE",
        "IMPORTS_RE",
        "IMPORT_TOKEN_RE",
        "split_theory",
        "extract_imports",
        "extract_theory_name",
        "determine_theory_name",
    ),
    "evaluation/training_metrics/trainmetrics/corpus.py": ("load_file",),
    "evaluation/training_metrics/trainmetrics/spans.py": ("command_spans",),
}


def _git(*args: str) -> Optional[str]:
    """The output of a git command in the repository (None when it fails)."""
    try:
        res = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode == 0 else None


def git_commit() -> Optional[str]:
    """HEAD of the repository (None without git)."""
    return (_git("rev-parse", "HEAD") or "").strip() or None


def _defs(source: str, names: Sequence[str]) -> Dict[str, str]:
    """The named top-level definitions of a module (comments and layout
    ignored)."""
    out: Dict[str, str] = {}
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            found = [node.name]
        elif isinstance(node, ast.Assign):
            found = [t.id for t in node.targets if isinstance(t, ast.Name)]
        else:
            found = []
        out.update({n: ast.dump(node) for n in found if n in names})
    return out


def replay_change(old: Optional[str], new: Optional[str]) -> Optional[str]:
    """None when the commits ``old`` .. ``new`` change nothing a replay runs
    (``REPLAY_PATHS``, ``REPLAY_DEFS``), else what changed or why git cannot
    tell."""
    if not old or not new:
        return "no commit to compare"
    paths = _git("diff", "--name-only", old, new, "--", *REPLAY_PATHS)
    if paths is None:
        return f"git cannot compare {old[:7]} with {new[:7]}"
    if paths.split():
        return f"{paths.split()[0]} changed"
    for path, names in REPLAY_DEFS.items():
        before, after = _git("show", f"{old}:{path}"), _git("show", f"{new}:{path}")
        try:
            same = (
                before is not None
                and after is not None
                and _defs(before, names) == _defs(after, names)
            )
        except SyntaxError:
            same = False
        if not same:
            return f"{path} changed ({', '.join(names)})"
    return None


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


def _head_key(lines: Sequence[str]) -> Dict[str, Any]:
    """The key on a journal's first line ({} when there is none)."""
    return dict((json.loads(lines[0]) if lines else {}).get("key") or {})


def _records(lines: Sequence[str]) -> List[ReplayRecord]:
    """The journal's finished files (one whose acquire or header failed is
    left out: it runs again)."""
    out = [record_from_dict(json.loads(ln)) for ln in lines[1:] if ln.strip()]
    return [rr for rr in out if not _redo(rr)]


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
        head = _head_key(lines)
        if head != key:
            changed = sorted(k for k in key if head.get(k) != key[k])
            return {}, f"the inputs changed: {', '.join(changed) or 'key'}"
        done = {rr.stem: rr for rr in _records(lines)}
    except (ValueError, TypeError) as exc:
        return {}, f"unreadable journal ({type(exc).__name__})"
    return done, None


def load_unchanged(
    path: Path,
    key: Dict[str, Any],
    files: Sequence[CorpusFile],
    override: bool = False,
) -> Tuple[Dict[str, ReplayRecord], Optional[str]]:
    """D16 (owner, 2026-10-25: a splitter fix must not replay the whole
    corpus): an earlier run whose key differs only in the commit keeps each
    finished file whose blocks are unchanged, when :func:`replay_change` finds
    nothing between the commits, or whatever it finds with ``override`` (D17,
    owner: a change the owner knows a replay does not run, such as removed
    dead code). (stem -> record, the line that says so); ``({}, None)`` when
    the key differs in more than the commit."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        head = _head_key(lines)
    except (OSError, ValueError, TypeError, AttributeError):
        return {}, None
    if not head or sorted(k for k in key if head.get(k) != key[k]) != ["commit"]:
        return {}, None
    old, new = head.get("commit"), key.get("commit")
    why = replay_change(old, new)
    if why and not override:
        return {}, f"no file kept from commit {str(old)[:7]}: {why}"
    shas = {cf.stem: cf.block_sha for cf in files}
    try:
        done = {
            rr.stem: rr
            for rr in _records(lines)
            if [b.block_sha for b in rr.blocks] == shas.get(rr.stem)
        }
    except (ValueError, TypeError) as exc:
        return {}, f"no file kept: unreadable journal ({type(exc).__name__})"
    commits = f"commit {str(old)[:7]} -> {str(new)[:7]}"
    return done, (
        (
            f"--keep-unchanged: {commits} ({why}) is taken to change nothing "
            "a replay runs"
            if why
            else f"{commits} changes nothing a replay runs"
        )
        + f"; keeping the {len(done)} files whose blocks did not change"
    )


def resume_point(
    path: Path,
    key: Dict[str, Any],
    files: Sequence[CorpusFile],
    fresh: bool,
    keep_unchanged: bool = False,
) -> Tuple[Dict[str, ReplayRecord], Optional[str], Optional[str]]:
    """(the finished files a census keeps, why it starts at the first file
    instead, the D16 line about files kept from an earlier commit).
    ``keep_unchanged`` is ``--keep-unchanged`` (D17); ``--fresh`` wins."""
    if fresh:
        return {}, "--fresh", None
    done, why = load_journal(path, key)
    if not why:
        return done, None, None
    kept, note = load_unchanged(path, key, files, keep_unchanged)
    return (kept, None, note) if kept else ({}, why, note)


def start_journal(
    path: Path, key: Dict[str, Any], kept: Sequence[ReplayRecord]
) -> None:
    """Write the key and the records that are kept. The earlier journal is
    moved to ``files.jsonl.prev`` when it had another key (a fresh start, or
    the files kept after a new commit) or nothing is kept."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            same = bool(kept) and _head_key(
                path.read_text(encoding="utf-8").splitlines()
            ) == dict(key)
        except (OSError, ValueError, TypeError, AttributeError):
            same = False
        if not same:
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
