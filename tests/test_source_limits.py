"""Source-size gate (owner decision 2026-09-27: 600 lines).

No Python source file under the server/client/MCP packages may exceed
MAX_LINES, except the files in ALLOWED_OVER — the pre-existing offenders,
each pinned at its size when the gate was introduced so it can only shrink
(the "ratchet"). Splitting one of them: delete its entry.
"""
from __future__ import annotations

from pathlib import Path

import pytest

MAX_LINES = 600
ROOT = Path(__file__).resolve().parent.parent
SCANNED = ("server", "client", "mcp_servers", "mcp_lsp_server", "mcp_stepwise_server")

# path (posix, repo-relative) -> ceiling. Ratchet down as they are split.
ALLOWED_OVER = {
    "server/app/services/session.py": 899,
    "server/app/services/heap_pool.py": 673,
}


def _line_count(path: Path) -> int:
    with path.open("rb") as f:
        return sum(1 for _ in f)


def _sources():
    for top in SCANNED:
        for p in sorted((ROOT / top).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            yield p


@pytest.mark.parametrize("path", list(_sources()), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_source_file_size(path: Path):
    rel = path.relative_to(ROOT).as_posix()
    n = _line_count(path)
    ceiling = ALLOWED_OVER.get(rel, MAX_LINES)
    assert n <= ceiling, (
        f"{rel} is {n} lines (limit {ceiling}). Split it along its section "
        f"markers (see claude-work/2026-9-27-refactor-router-package/NOTES.md) "
        f"instead of raising the limit."
    )


def test_allowlist_entries_still_exist_and_still_need_it():
    for rel, ceiling in ALLOWED_OVER.items():
        p = ROOT / rel
        assert p.exists(), f"{rel} in ALLOWED_OVER no longer exists — remove the entry"
        assert _line_count(p) > MAX_LINES, (
            f"{rel} is now under {MAX_LINES} lines — remove it from ALLOWED_OVER"
        )
