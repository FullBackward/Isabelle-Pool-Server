"""Workload theories: one fixed ``.thy`` per heap from the repository corpus,
identified by name and sha256 in every cell manifest, plus the trailing lemma
the concurrent phase (P5) appends/re-checks in every session."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

_THEORY_RE = re.compile(r"^\s*theory\s+(\S+)", re.M)
_IMPORTS_RE = re.compile(r"\bimports\b(.*?)\bbegin\b", re.S)


@dataclass(frozen=True)
class Workload:
    """One heap's fixed workload theory, identified by sha256."""

    heap: str
    path: Path
    text: str
    sha256: str
    theory_name: str
    imports: Tuple[str, ...]
    trailing_lemma: str

    def as_manifest(self) -> Dict[str, object]:
        """The workload block of a cell manifest."""
        return {
            "heap": self.heap,
            "file": str(self.path),
            "sha256": self.sha256,
            "theory": self.theory_name,
            "imports": list(self.imports),
            "lines": self.text.count("\n") + 1,
            "trailing_lemma": self.trailing_lemma,
        }


def parse_header(text: str) -> Tuple[str, Tuple[str, ...]]:
    """``(theory_name, imports)`` from a theory header (comments stripped)."""
    stripped = re.sub(r"\(\*.*?\*\)", " ", text, flags=re.S)
    m = _THEORY_RE.search(stripped)
    name = m.group(1) if m else ""
    im = _IMPORTS_RE.search(stripped)
    imports = tuple(tok.strip('"') for tok in im.group(1).split()) if im else ()
    return name, imports


def load_workload(
    heap: str, file_path: str, trailing_lemma: str, repo_root: Optional[Path] = None
) -> Workload:
    """Read the theory file and its header; ``file_path`` may be repo-relative."""
    path = Path(file_path)
    if not path.is_absolute() and repo_root is not None:
        path = repo_root / path
    text = path.read_text(encoding="utf-8")
    name, imports = parse_header(text)
    return Workload(
        heap=heap,
        path=path,
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        theory_name=name,
        imports=imports,
        trailing_lemma=trailing_lemma,
    )


def with_theory_name(text: str, new_name: str) -> str:
    """The same theory text under another name (one scratch per session)."""
    return _THEORY_RE.sub(f"theory {new_name}", text, count=1)


def body_before_end(text: str) -> str:
    """Text up to (not including) the final ``end``; where P5's lemma goes."""
    idx = text.rstrip().rfind("\nend")
    return text if idx < 0 else text[: idx + 1]
