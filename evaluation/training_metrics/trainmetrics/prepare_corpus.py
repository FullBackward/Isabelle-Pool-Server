"""Prepare the D1 corpus from the Isabelle under test (owner decision
2026-10-22, ``ESCALATION_corpus.md``): the theory names listed in
``corpus_stems.txt`` are taken from ``$ISABELLE_HOME/src/HOL/Analysis`` and
rewritten by ``evaluation/scripts/process.py`` (imports qualified as
``HOL-Analysis.<Theory>``, LaTeX and cite antiquotations removed) into
``results/corpus/processed/``. ``MANIFEST.json`` records the Isabelle id, the
source directory, the sha256 of every source and processed file, and any name
the source lacks. The snapshot under ``evaluation/HOL_corpus`` is not tracked
in git and comes from an older release (22 of its 94 files match RC2), so the
corpus is built where the experiment runs.

    python -m trainmetrics.prepare_corpus [--src DIR] [--out results/corpus]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import EXPERIMENT_DIR
from .serve import isabelle_cmd

from evaluation.scripts.process import (  # isort: skip  # pylint: disable=wrong-import-order
    build_analysis_theory_set,
    rewrite_theory_text,
)

STEMS_FILE = EXPERIMENT_DIR / "corpus_stems.txt"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_stems(path: Path = STEMS_FILE) -> List[str]:
    """The corpus definition: one theory name per line (# comments allowed)."""
    out: List[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _isabelle(args: List[str]) -> Optional[str]:
    cmd = (isabelle_cmd() or ["isabelle"]) + args
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout.strip() or None


def default_src() -> Optional[Path]:
    """``$ISABELLE_HOME/src/HOL/Analysis`` of the ``isabelle`` on PATH."""
    home = _isabelle(["getenv", "-b", "ISABELLE_HOME"])
    return Path(home) / "src" / "HOL" / "Analysis" if home else None


def prepare(
    src: Path, stems: Sequence[str], out: Path, meta: Dict[str, Any]
) -> Dict[str, Any]:
    """Rewrite every listed theory of ``src`` into ``out/processed`` and write
    ``out/MANIFEST.json``; returns the manifest."""
    theories = build_analysis_theory_set(src)
    proc_dir = out / "processed"
    proc_dir.mkdir(parents=True, exist_ok=True)
    for old in proc_dir.glob("*.thy"):  # a re-run never keeps stale files
        old.unlink()
    files: List[Dict[str, Any]] = []
    missing: List[str] = []
    for stem in stems:
        path = src / f"{stem}.thy"
        if not path.exists():
            missing.append(stem)
            continue
        raw = path.read_bytes()
        text = rewrite_theory_text(raw.decode("utf-8"), theories)
        (proc_dir / f"{stem}.thy").write_text(text, encoding="utf-8")
        files.append(
            {
                "stem": stem,
                "source_sha256": _sha(raw),
                "processed_sha256": _sha(text.encode("utf-8")),
            }
        )
    manifest: Dict[str, Any] = {
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_dir": str(src),
        "stems_file_sha256": (
            _sha(STEMS_FILE.read_bytes()) if STEMS_FILE.exists() else None
        ),
        "files": files,
        "missing": missing,
        **meta,
    }
    manifest["corpus_sha256"] = _sha(
        "\n".join(f"{f['stem']} {f['processed_sha256']}" for f in files).encode("utf-8")
    )
    with open(out / "MANIFEST.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
    return manifest


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--src", default=None, help="default: $ISABELLE_HOME/src/HOL/Analysis"
    )
    ap.add_argument("--out", default=str(EXPERIMENT_DIR / "results" / "corpus"))
    ap.add_argument("--stems", default=str(STEMS_FILE))
    args = ap.parse_args(argv)
    src = Path(args.src) if args.src else default_src()
    if src is None or not src.is_dir():
        print(f"no HOL-Analysis sources at {src}; pass --src or put isabelle on PATH")
        return 2
    stems = read_stems(Path(args.stems))
    m = prepare(
        src,
        stems,
        Path(args.out),
        {
            "isabelle_version": _isabelle(["version"]),
            "isabelle_id": _isabelle(["version", "-i"]),
        },
    )
    print(
        f"corpus: {len(m['files'])} of {len(stems)} theories from {src} "
        f"(Isabelle {m['isabelle_version']} {m['isabelle_id']}), "
        f"missing {m['missing'] or 'none'}, corpus sha {m['corpus_sha256'][:12]} "
        f"-> {Path(args.out) / 'processed'}"
    )
    return 0 if not m["missing"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
