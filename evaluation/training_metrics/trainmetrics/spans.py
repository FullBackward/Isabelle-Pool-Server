"""Command spans for the census (plan stage 3d; design v0.4 T6b): where each
command of a block starts and ends, as Isabelle split it, so T6b can replay a
proof command by command. The chunk report gives every command's ``kind`` and
``range``: node-absolute lines, 1-based columns counted in the node text's
characters (a symbol such as ``\\<inter>`` is its eight characters; seen live),
end exclusive. The block's first line in the node is the first command's
``node_line`` minus its chunk-relative ``line`` plus one (review plan_v1.6 S7).

:func:`check_spans` is the census's own assertion (S7): the spans must be in
order and in bounds, each must start with its command keyword, and the text
between them may hold only whitespace and comments — a column-unit mismatch
(e.g. characters outside the Basic Multilingual Plane, counted twice in
UTF-16) fails it."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

COMMENT_RE = re.compile(r"\(\*.*?\*\)", re.S)


def command_spans(
    text: str, commands: List[Dict[str, Any]]
) -> Optional[List[Dict[str, Any]]]:
    """[{kind, start, end}] offsets into ``text`` for every command with a
    range; None when the report has no range to anchor on."""
    anchored = [
        c for c in commands if c.get("range") and c.get("node_line") and c.get("line")
    ]
    if not anchored:
        return None if commands else []
    first = anchored[0]
    line0 = int(first["node_line"]) - (
        int(first["line"]) - 1
    )  # node line of chunk line 1
    starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]

    def offset(pos: Dict[str, Any]) -> int:
        ln = int(pos["line"]) - line0
        if not 0 <= ln < len(starts):
            return -1
        return starts[ln] + int(pos["col"]) - 1

    return [
        {
            "kind": str(c.get("kind") or ""),
            "start": offset(c["range"]["start"]),
            "end": offset(c["range"]["end"]),
        }
        for c in commands
        if c.get("range")
    ]


def check_spans(text: str, spans: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """None when the spans re-assemble ``text`` (see the module docstring),
    else what is wrong."""
    if spans is None:
        return "no ranges in the report"
    pos = 0
    for n, s in enumerate(spans):
        a, b = int(s["start"]), int(s["end"])
        if not 0 <= a <= b <= len(text) or a < pos:
            return f"command {n} ({s['kind']}) out of order or bounds: {a}..{b}"
        if s["kind"] and not text[a:b].startswith(s["kind"]):
            return f"command {n} does not start with {s['kind']!r}: {text[a:a + 20]!r}"
        gap = COMMENT_RE.sub("", text[pos:a])
        if gap.strip():
            return f"text between commands before command {n}: {gap.strip()[:40]!r}"
        pos = b
    tail = COMMENT_RE.sub("", text[pos:])
    return (
        None
        if not tail.strip()
        else f"text after the last command: {tail.strip()[:40]!r}"
    )
