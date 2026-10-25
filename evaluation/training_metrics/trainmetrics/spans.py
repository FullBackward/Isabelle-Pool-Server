"""Command spans for the census (plan stage 3d; design v0.4 T6b): where each
command of a block starts and ends, as Isabelle split it, so T6b can replay a
proof command by command. The chunk report gives every command's ``kind`` and
``range``: node-absolute lines, 1-based columns counted in the node text's
characters (a symbol such as ``\\<inter>`` is its eight characters; seen live),
end exclusive. The block's first line in the node is the first command's
``node_line`` minus its chunk-relative ``line`` plus one (review plan_v1.6 S7).

:func:`check_spans` is the census's own assertion (S7): the spans must be in
order and in bounds, each must start with its command keyword (after a
``private`` / ``qualified`` modifier, which Isabelle counts as part of the
command, D16), and the text between them may hold only whitespace and
comments (nested ``(* *)`` and formal comments; review c6aa372 S1) — a
column-unit mismatch (e.g. characters outside the Basic Multilingual Plane,
counted twice in UTF-16) fails it."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# the census of 2026-10-25: `private lemma` is one command whose span starts at
# `private` (10 blocks of 5 HOL-Analysis files)
MODIFIER_RE = re.compile(r"(?:private|qualified)\s+")
FORMAL = ("\\<comment>", "\\<^cancel>", "\\<^marker>")
CARTOUCHE = (("\\<open>", "\\<close>"), ("\u2039", "\u203a"))  # \<open>..\<close>, ‹..›


def _skip_cartouche(text: str, i: int) -> int:
    """The index after the (nested) cartouche starting at ``i``, or ``i``."""
    for op, cl in CARTOUCHE:
        if text.startswith(op, i):
            depth, j = 0, i
            while j < len(text):
                if text.startswith(op, j):
                    depth, j = depth + 1, j + len(op)
                elif text.startswith(cl, j):
                    depth, j = depth - 1, j + len(cl)
                    if depth == 0:
                        return j
                else:
                    j += 1
            return len(text)
    return i


def strip_comments(text: str) -> str:
    """``text`` without its comments: ``(* … *)`` nested to any depth, and
    formal comments (``\\<comment>``, ``\\<^cancel>``, ``\\<^marker>`` with
    their cartouche) — review c6aa372 S1."""
    out: List[str] = []
    i, depth = 0, 0
    while i < len(text):
        if text.startswith("(*", i):
            depth, i = depth + 1, i + 2
        elif depth and text.startswith("*)", i):
            depth, i = depth - 1, i + 2
        elif depth:
            i += 1
        else:
            kw = next((k for k in FORMAL if text.startswith(k, i)), None)
            if kw is None:
                out.append(text[i])
                i += 1
                continue
            j = i + len(kw)
            while j < len(text) and text[j].isspace():
                j += 1
            i = _skip_cartouche(text, j)
    return "".join(out)


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
        mod = MODIFIER_RE.match(text, a, b)
        if s["kind"] and not text[mod.end() if mod else a : b].startswith(s["kind"]):
            return f"command {n} does not start with {s['kind']!r}: {text[a:a + 20]!r}"
        gap = strip_comments(text[pos:a])
        if gap.strip():
            return f"text between commands before command {n}: {gap.strip()[:40]!r}"
        pos = b
    tail = strip_comments(text[pos:])
    return (
        None
        if not tail.strip()
        else f"text after the last command: {tail.strip()[:40]!r}"
    )
