"""Prepare the D1 corpus from the Isabelle under test (owner decision
2026-10-22, ``ESCALATION_corpus.md``): the theory names listed in
``corpus_stems.txt`` are taken from ``$ISABELLE_HOME/src/HOL/Analysis`` and
rewritten by ``evaluation/scripts/process.py`` (imports qualified as
``HOL-Analysis.<Theory>``, LaTeX and cite antiquotations removed) into
``results/corpus/processed/``. A load command's relative path
(``ML_file ‹measurable.ML›``) is anchored at the source directory, as
``~~/src/HOL/Analysis/…`` (D14): IPS enters a theory as ``Draft.<name>`` with
no folder, so the prover would look for the file in its working directory.
``MANIFEST.json`` records the Isabelle id, the source directory, the sha256 of
every source and processed file, the anchored paths, and any name the source
lacks. The snapshot under ``evaluation/HOL_corpus`` is not tracked
in git and comes from an older release (22 of its 94 files match RC2), so the
corpus is built where the experiment runs.

    python -m trainmetrics.prepare_corpus [--src DIR] [--out results/corpus]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import EXPERIMENT_DIR
from .serve import isabelle_cmd

from evaluation.scripts.process import (  # isort: skip  # pylint: disable=wrong-import-order
    build_analysis_theory_set,
    rewrite_theory_text,
)

STEMS_FILE = EXPERIMENT_DIR / "corpus_stems.txt"

# a load command and its path argument (cartouche, symbol cartouche or string)
LOAD_RE = re.compile(
    r"(?<![A-Za-z0-9_'])(?P<cmd>S?ML_file(?:_debug|_no_debug)?)(?P<sp>\s*)"
    r"(?:\\<open>(?P<a>[^\\]*?)\\<close>|‹(?P<b>[^›]*)›|\"(?P<c>[^\"]*)\")"
)


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


def anchor_load_paths(
    text: str, src: Path, home: Optional[Path]
) -> Tuple[str, List[str]]:
    """``text`` with each load command's relative path made ``~~/…`` (or
    absolute when ``src`` is not under ``home``); the new paths."""
    anchored: List[str] = []

    def repl(m: "re.Match[str]") -> str:
        rel = next(g for g in (m["a"], m["b"], m["c"]) if g is not None)
        if rel.startswith(("/", "~", "$")):
            return m.group(0)
        target = (src / rel).resolve()
        try:
            new = "~~/" + target.relative_to(home.resolve()).as_posix() if home else ""
        except ValueError:
            new = ""
        new = new or target.as_posix()
        anchored.append(new)
        return f"{m['cmd']}{m['sp']}\\<open>{new}\\<close>"

    return LOAD_RE.sub(repl, text), anchored


def isabelle_home() -> Optional[Path]:
    """``$ISABELLE_HOME`` of the ``isabelle`` on PATH."""
    home = _isabelle(["getenv", "-b", "ISABELLE_HOME"])
    return Path(home) if home else None


def default_src() -> Optional[Path]:
    """``$ISABELLE_HOME/src/HOL/Analysis`` of the ``isabelle`` on PATH."""
    home = isabelle_home()
    return home / "src" / "HOL" / "Analysis" if home else None


def prepare(
    src: Path,
    stems: Sequence[str],
    out: Path,
    meta: Dict[str, Any],
    home: Optional[Path] = None,
) -> Dict[str, Any]:
    """Rewrite every listed theory of ``src`` into ``out/processed`` and write
    ``out/MANIFEST.json``; returns the manifest. ``home`` is
    ``$ISABELLE_HOME`` (load paths under it are written ``~~/…``)."""
    theories = build_analysis_theory_set(src)
    proc_dir = out / "processed"
    proc_dir.mkdir(parents=True, exist_ok=True)
    for old in proc_dir.glob("*.thy"):  # a re-run never keeps stale files
        old.unlink()
    files: List[Dict[str, Any]] = []
    missing: List[str] = []
    anchored: Dict[str, List[str]] = {}
    for stem in stems:
        path = src / f"{stem}.thy"
        if not path.exists():
            missing.append(stem)
            continue
        raw = path.read_bytes()
        text, paths = anchor_load_paths(
            rewrite_theory_text(raw.decode("utf-8"), theories), src, home
        )
        if paths:
            anchored[stem] = paths
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
        "anchored_load_paths": anchored,
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
        isabelle_home(),
    )
    print(
        f"corpus: {len(m['files'])} of {len(stems)} theories from {src} "
        f"(Isabelle {m['isabelle_version']} {m['isabelle_id']}), "
        f"missing {m['missing'] or 'none'}, anchored load paths "
        f"{sum(len(v) for v in m['anchored_load_paths'].values())}, "
        f"corpus sha {m['corpus_sha256'][:12]} "
        f"-> {Path(args.out) / 'processed'}"
    )
    return 0 if not m["missing"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
