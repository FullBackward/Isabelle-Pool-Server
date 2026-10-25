from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional



THEORY_RE = re.compile(r'(?ms)^[ \t]*theory\s+(?:"([^"\n]+)"|([A-Za-z0-9_\'.-]+))')
HEADER_RE = re.compile(r"(?s)\btheory\b.*?\bbegin\b")
END_RE = re.compile(r"\bend\s*$")
IMPORTS_RE = re.compile(r"(?s)\bimports\b(.*?)\bbegin\b")
IMPORT_TOKEN_RE = re.compile(r'"[^"]+"|[A-Za-z_][A-Za-z0-9_./-]*')


COMMAND_STARTERS = tuple(
    sorted(
        {
            # document / structure
            "chapter", "section", "subsection", "subsubsection", "subsubsubsection",
            "paragraph", "subparagraph", "text", "text_raw",
            "context", "locale", "interpretation", "sublocale", "experiment",
            "bundle", "unbundle", "include", "including", "notepad", "named_theorems",
            # theory / declarations
            "lemma", "theorem", "corollary", "proposition", "schematic_goal",
            "definition", "abbreviation", "lemmas", "fun", "function", "primrec",
            "inductive", "inductive_set", "coinductive", "datatype", "codatatype",
            "record", "typedef", "class", "instantiation", "instance",
            "lift_definition", "termination", "consts", "axiomatization",
            "notation", "no_notation", "adhoc_overloading", "no_adhoc_overloading",
            "declare", "syntax", "no_syntax", "translations", "no_translations",
            "typed_print_translation", "print_translation", "parse_translation",
            "print_ast_translation", "parse_ast_translation",
            "hide_const", "hide_fact", "hide_type", "hide_class",
            "term", "typ", "thm", "prop", "value", "values",
            "print_statement", "find_theorems", "print_theorems", "print_record",
            "ML", "ML_file", "SML_export", "setup", "method_setup", "oracle",
            # proof commands
            "proof", "qed", "by", "done", "oops", "sorry", "next",
            "fix", "assume", "presume", "case", "note", "let", "write",
            "have", "show", "thus", "hence", "obtain", "guess", "define",
            "then", "from", "with", "using", "unfolding", "supply",
            "moreover", "ultimately", "also", "finally",
            "apply", "apply_end", "subgoal", "defer", "prefer", "back",
            # structural terminator inside nested contexts
            "end",
        },
        key=lambda s: (-len(s), s),
    )
)


TOP_LEVEL_KEYWORDS = (
    "lemma", "theorem", "corollary", "proposition", "schematic_goal",
    "definition", "fun", "function", "primrec", "inductive", "inductive_set",
    "coinductive", "abbreviation", "notation", "no_notation", "declare",
    "context", "locale", "interpretation", "instantiation", "lift_definition",
    "datatype", "codatatype", "record", "typedef", "class", "instance",
    "text", "text_raw", "ML", "ML_file", "SML_export", "setup",
    "method_setup", "termination", "end",
)
PROOF_OPENERS = ("proof", "proof -", "proof (")
PROOF_CLOSERS = ("qed", "by", "done", "oops", "sorry")


def extract_theory_name(text: str) -> Optional[str]:
    m = THEORY_RE.search(text)
    if not m:
        return None
    return m.group(1) or m.group(2)


def determine_theory_name(thy_file: Path, text: str) -> str:
    return extract_theory_name(text) or thy_file.stem


def split_theory(text: str) -> tuple[str, str, str]:
    stripped = text.strip()
    header_match = HEADER_RE.search(stripped)
    end_match = END_RE.search(stripped)
    if not header_match or not end_match:
        raise ValueError("Could not split theory into header/body/end")
    return (
        stripped[:header_match.end()].strip() + "\n",
        stripped[header_match.end():end_match.start()].strip(),
        stripped[end_match.start():end_match.end()].strip(),
    )

def extract_imports(text: str) -> list[str]:
    m = IMPORTS_RE.search(text)
    if not m:
        return ["Main"]
    out: list[str] = []
    for token in IMPORT_TOKEN_RE.findall(m.group(1)):
        token = token.strip().strip('"')
        if token and token not in {"imports", "begin", "theory", "keywords"}:
            out.append(token)
    # header order kept (duplicates dropped): the later import wins a shared short name
    return list(dict.fromkeys(out)) or ["Main"]


@dataclass
class LexState:
    comment_depth: int = 0
    in_string: bool = False
    cartouche_depth: int = 0
    unicode_cartouche_depth: int = 0

    def clear(self) -> bool:
        return (
            self.comment_depth == 0
            and not self.in_string
            and self.cartouche_depth == 0
            and self.unicode_cartouche_depth == 0
        )


def _escaped_quote(text: str, idx: int) -> bool:
    backslashes = 0
    j = idx - 1
    while j >= 0 and text[j] == "\\":
        backslashes += 1
        j -= 1
    return (backslashes % 2) == 1


def advance_lex_state(state: LexState, text: str, start: int = 0) -> LexState:
    i = start
    n = len(text)
    while i < n:
        if state.comment_depth > 0:
            if text.startswith("(*", i):
                state.comment_depth += 1
                i += 2
                continue
            if text.startswith("*)", i):
                state.comment_depth -= 1
                i += 2
                continue
            i += 1
            continue

        if state.in_string:
            if text[i] == '"' and not _escaped_quote(text, i):
                state.in_string = False
            i += 1
            continue

        if state.cartouche_depth > 0:
            if text.startswith(r"\<open>", i):
                state.cartouche_depth += 1
                i += len(r"\<open>")
                continue
            if text.startswith(r"\<close>", i):
                state.cartouche_depth -= 1
                i += len(r"\<close>")
                continue
            i += 1
            continue

        if state.unicode_cartouche_depth > 0:
            if text[i] == "\u2039":
                state.unicode_cartouche_depth += 1
            elif text[i] == "\u203a":
                state.unicode_cartouche_depth -= 1
            i += 1
            continue

        if text.startswith("(*", i):
            state.comment_depth += 1
            i += 2
            continue
        if text.startswith(r"\<open>", i):
            state.cartouche_depth += 1
            i += len(r"\<open>")
            continue
        if text[i] == "\u2039":
            state.unicode_cartouche_depth += 1
            i += 1
            continue
        if text[i] == '"':
            state.in_string = True
            i += 1
            continue
        i += 1
    return state


def _line_starter_keyword(stripped: str) -> Optional[str]:
    if stripped in {".", "..", "..."}:
        return stripped
    for kw in COMMAND_STARTERS:
        if re.match(rf"^{re.escape(kw)}(\b|\s|\(|$)", stripped):
            return kw
    return None


def split_commands(body_text: str) -> list[str]:
    text = body_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    lines = text.split("\n")
    state = LexState()
    commands: list[str] = []
    current: list[str] = []

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        safe_line_start = state.clear()
        starter = _line_starter_keyword(stripped) if (safe_line_start and stripped) else None
        if current and starter is not None:
            block = "\n".join(current).strip()
            if block:
                commands.append(block)
            current = []

        current.append(line)
        advance_lex_state(state, raw_line)
        advance_lex_state(state, "\n")

    block = "\n".join(current).strip()
    if block:
        commands.append(block)
    return commands


def _starts_with_keyword(line: str, keywords: tuple[str, ...]) -> bool:
    stripped = line.strip()
    return any(re.match(rf"^{re.escape(keyword)}(\b|\s|\(|$)", stripped) for keyword in keywords)


def _strip_comments(text: str) -> str:
    """``text`` without its (nested) ``(* ... *)`` comments."""
    out: list[str] = []
    depth = 0
    i = 0
    while i < len(text):
        if text.startswith("(*", i):
            depth += 1
            i += 2
            continue
        if depth and text.startswith("*)", i):
            depth -= 1
            i += 2
            continue
        if not depth:
            out.append(text[i])
        i += 1
    return "".join(out)


def _comment_only(lines: list[str]) -> bool:
    """The lines hold nothing but comments and blank space."""
    return not _strip_comments("\n".join(lines)).strip()


def split_top_level_blocks(body_text: str) -> list[str]:
    """The body cut before each top-level command. A cut is only made, and
    ``proof`` / ``qed`` only counted, on a line that starts outside any
    comment, string or cartouche (``LexState``, as ``split_commands``): a block
    comment with blank lines and keywords inside stays in one block. A block of
    comments only joins the next block (the last one joins the previous), so
    no block is empty of commands. Lines keep their trailing whitespace: every
    block is the body's exact text, in order (T4's ``mutate.splice`` finds
    each block in the file)."""
    text = body_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    # each block as its lines, so a merged block stays the file's exact text
    blocks: list[list[str]] = []
    current: list[str] = []
    proof_depth = 0
    state = LexState()
    for line in text.split("\n"):  # kept as is: each block is the body's exact text
        stripped = line.strip()
        at_top = state.clear() and stripped != ""
        if (
            current
            and proof_depth == 0
            and at_top
            and _starts_with_keyword(stripped, TOP_LEVEL_KEYWORDS)
            and not _comment_only(current)
        ):
            blocks.append(current)
            current = []
        current.append(line)
        if at_top and _starts_with_keyword(stripped, PROOF_OPENERS):
            proof_depth += 1
        elif at_top and _starts_with_keyword(stripped, PROOF_CLOSERS):
            proof_depth = max(0, proof_depth - 1)
        advance_lex_state(state, line)
        advance_lex_state(state, "\n")
    if blocks and _comment_only(current):
        blocks[-1].extend(current)
    else:
        blocks.append(current)
    return [b for b in ("\n".join(lines).strip() for lines in blocks) if b]
