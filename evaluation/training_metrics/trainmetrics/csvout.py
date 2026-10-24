"""Input and output shared by the analysis modules: the finished cells of a
results tree and the CSV writer (moved out of analyze.py in stage 3c-2 so
the per-experiment modules can use them without an import cycle)."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    """A CSV with the union of the rows' keys (nothing for no rows)."""
    if not rows:
        return
    keys: List[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    k: json.dumps(v) if isinstance(v, (dict, list)) else v
                    for k, v in r.items()
                }
            )


def ok_cells(results: Path, pattern: str) -> List[Tuple[Path, Dict[str, Any]]]:
    """(cell directory, manifest) of every finished (ok) cell whose manifest
    matches ``pattern`` under ``results`` (e.g. ``t4/rep*/manifest.json``)."""
    out: List[Tuple[Path, Dict[str, Any]]] = []
    for mpath in sorted(results.glob(pattern)):
        try:
            m = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if m.get("outcome") == "ok":
            out.append((mpath.parent, dict(m)))
    return out
