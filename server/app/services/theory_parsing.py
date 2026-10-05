"""Canonical Isabelle theory-header parsing (one implementation, all consumers).

History (isabellegym-header-imports-issue.md): three independent regex parsers
(mcp_servers/lsp/pool.py, core/config.py::RegularExp.IMPORT_RE, external
harnesses) all matched the FIRST `imports` keyword anywhere in the file —
including inside leading `(* TASK: ... imports=... *)` comments — producing
garbage theory names and HTTP 500 at session creation. This module is the
single correct implementation: comments (nested) are stripped first, then the
`theory <name> imports <...> begin` header is anchored at line start.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from server.app.core.config import RegularExp


def extract_theory_name(text: str) -> Optional[str]:
    m = RegularExp.THEORY_RE.search(text)
    if not m:
        return None
    return m.group(1) or m.group(2)


# ---------------------------------------------------------------------------
# Canonical header parse

_IMPORT_TOKEN_RE = re.compile(r'"[^"]+"|[A-Za-z_][A-Za-z0-9_./-]*')
_THEORY_LINE_RE = re.compile(
    r'(?m)^[ \t]*theory\s+(?:"([^"\n]+)"|([A-Za-z0-9_\'.-]+))'
)
#: Words that are header syntax, not import names.
_NOISE = {"imports", "begin", "theory", "keywords"}


def strip_theory_comments(text: str) -> str:
    """Remove `(* ... *)` comments, tracking nesting depth (Isabelle comments
    nest). Newlines inside comments are preserved so line numbers do not shift;
    all other comment bytes become nothing."""
    out: List[str] = []
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        if depth == 0:
            if text.startswith("(*", i):
                depth = 1
                i += 2
                continue
            out.append(text[i])
            i += 1
            continue
        # inside a comment
        if text.startswith("(*", i):
            depth += 1
            i += 2
            continue
        if text.startswith("*)", i):
            depth -= 1
            i += 2
            continue
        if text[i] == "\n":
            out.append("\n")
        i += 1
    return "".join(out)


def parse_theory_header(text: str) -> Tuple[Optional[str], List[str]]:
    """The canonical parse: (theory_name, imports).

    Comments are stripped (nested) before matching, so a leading
    `(* TASK: ... imports=... *)`-style comment can never pollute the import
    list. The imports run from the `imports` keyword after the theory name to
    `begin` (or EOF), tokenized as quoted or bare names; header syntax words
    (`keywords`, `begin`, …) are excluded.
    """
    stripped = strip_theory_comments(text)
    m = _THEORY_LINE_RE.search(stripped)
    if m is None:
        return None, []
    name = m.group(1) or m.group(2)
    rest = stripped[m.end():]
    im = re.search(r"\bimports\b", rest)
    if im is None:
        return name, []
    after = rest[im.end():]
    bm = re.search(r"\bbegin\b", after)
    raw = after[: bm.start()] if bm is not None else after
    imports = [
        tok
        for tok in (t.strip('"') for t in _IMPORT_TOKEN_RE.findall(raw))
        if tok and tok not in _NOISE
    ]
    return name, imports


def suggested_field(imports: List[str]) -> Optional[str]:
    """The session field a dotted import implies: `HOL-Analysis.Derivative` →
    `HOL-Analysis`. None for plain (undotted) imports like Main."""
    for name in imports:
        if "." in name:
            return name.split(".", 1)[0]
    return None
