"""The corpus: theory files split into header, imports and top-level blocks
(``evaluation/scripts/theory_splitter``), checksums, and the seeded assignment
of files to workers (plan D2: every W sees the same multiset of files)."""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import REPO_ROOT  # noqa: F401  (sets sys.path for the splitter)

from evaluation.scripts.theory_splitter import (  # isort: skip  # pylint: disable=wrong-import-order
    extract_imports,
    split_theory,
    split_top_level_blocks,
)


def sha256_text(text: str) -> str:
    """Hex sha256 of ``text`` (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CorpusFile:
    """One theory of the corpus, split for replay."""

    stem: str
    path: str
    imports: List[str]
    blocks: List[str]
    sha256: str
    block_sha: List[str] = field(default_factory=list)

    def as_manifest(self) -> Dict[str, object]:
        """Compact description for manifests."""
        return {
            "stem": self.stem,
            "imports": self.imports,
            "blocks": len(self.blocks),
            "sha256": self.sha256,
        }


def load_file(path: Path) -> Optional[CorpusFile]:
    """Split one ``.thy``; None when it has no header/end or no blocks."""
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        _header, body, _end = split_theory(text)
    except ValueError:
        return None
    blocks = split_top_level_blocks(body)
    if not blocks:
        return None
    return CorpusFile(
        stem=path.stem,
        path=str(path),
        imports=extract_imports(text),
        blocks=blocks,
        sha256=sha256_text(text),
        block_sha=[sha256_text(b) for b in blocks],
    )


def load_corpus(
    corpus_dir: str, only: Optional[Sequence[str]] = None
) -> List[CorpusFile]:
    """Every splittable ``*.thy`` of ``corpus_dir`` in name order; ``only``
    restricts to the given stems (smoke)."""
    root = Path(corpus_dir)
    if not root.is_absolute():
        root = REPO_ROOT / root
    wanted = set(only or ())
    out: List[CorpusFile] = []
    for path in sorted(root.glob("*.thy")):
        if wanted and path.stem not in wanted:
            continue
        cf = load_file(path)
        if cf is not None:
            out.append(cf)
    return out


def load_paths(paths: Sequence[str]) -> List[CorpusFile]:
    """Explicit theory files (relative to the repository root), in the given
    order; used by the local smoke corpus."""
    out: List[CorpusFile] = []
    for p in paths:
        path = Path(p)
        if not path.is_absolute():
            path = REPO_ROOT / path
        cf = load_file(path)
        if cf is not None:
            out.append(cf)
    return out


def corpus_sha(files: Sequence[CorpusFile]) -> str:
    """One checksum for the corpus as used (stems and file checksums)."""
    return sha256_text("\n".join(f"{f.stem} {f.sha256}" for f in files))


def assignment(
    stems: Sequence[str], seed: int, workers: int, quota: int
) -> List[List[str]]:
    """W lists of ``quota`` stems each. One seeded sequence is built from
    shuffled copies of the corpus (each copy a full permutation, so every file
    appears once per 94 positions); a run of total N = W × quota takes its
    first N stems and worker ``w`` the positions ``w, w + W, ...``. The files of
    a smaller run are therefore a prefix of a larger run's, every file appears
    ⌊N/94⌋ or ⌈N/94⌉ times, and a repeat replays the same assignment (plan D2,
    plan review S2)."""
    if workers < 1 or quota < 1:
        raise ValueError("workers and quota must be >= 1")
    base = sorted(stems)
    if not base:
        raise ValueError("empty corpus")
    need = workers * quota
    rng = random.Random(seed)
    pool: List[str] = []
    while len(pool) < need:
        rnd = list(base)
        rng.shuffle(rnd)
        pool.extend(rnd)
    pool = pool[:need]
    return [pool[w::workers] for w in range(workers)]
