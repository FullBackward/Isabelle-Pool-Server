"""CSV output shared by the analysis modules (moved out of analyze.py in
stage 3c-2 so analyze_t5 can use it without an import cycle)."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List


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
