"""T4 part B, the mutation suite (design §T4, plan stage 4): each sampled
theorem block in four variants, one mutation per build (design review S7).

- ``unchanged``: the block as written.
- ``sorry``: the statement, then ``sorry`` (in-session: kept with
  ``used_sorry``; cold: rejected, the build has no ``quick_and_dirty``); a
  document command the splitter attached after the proof is dropped with it
  (review ab492af note: harmless, but only ``truncated`` keeps it).
- ``truncated`` (plan review Q4): the final ``qed`` / ``done`` of the proof
  dropped (with its method, ``qed auto``), or for a proof that ends with a
  terminal method (``by …``, ``.``, ``..``) that method dropped; never a cut
  inside a nested block, because only the proof's last top-level command is
  removed; a trailing document command stays (in-session: ``proof_open``;
  cold: rejected).
- ``wrong_fact``: the first fact name of the proof replaced by one that does
  not exist (in-session: failed; cold: rejected).

A variant the block does not admit (no fact name, no terminal command) is
None and reported as not applicable."""

from __future__ import annotations

import random
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .corpus import CorpusFile
from .seeds import split_statement, top_level_words

VARIANTS = ("unchanged", "sorry", "truncated", "wrong_fact")
NO_SUCH_FACT = "t4_no_such_fact"
TERMINAL = ("by", ".", "..")
CLOSERS = ("qed", "done")
ISAR = set(
    "have show thus hence from with using unfolding note then also finally moreover "
    "ultimately obtain where and case next qed done fix assume presume let define proof "
    "by apply sorry oops is for if supply including subgoal back defer prefer".split()
)
# a name, symbols such as \<phi> included (review ab492af B1: ``ac_\<phi>``)
NAME_RE = re.compile(r"[A-Za-z](?:[A-Za-z0-9_'.]|\\<[^>]*>)*")
VAR_INTRO = ("fix", "obtain", "define", "let", "case", "for")  # variables follow


def _is_binding(words: List[Tuple[int, str]], k: int) -> bool:
    """Is word k a name being DEFINED (a label ``name:`` or
    ``name [attr, attr]:``, the left side of ``note name = …``), not a
    reference (``name [of x]`` is one)? Review ab492af B1: renaming a label
    that is never used is no mutation."""
    word = words[k][1]
    if word.endswith(":") and not word.endswith("::"):
        return True
    j = k + 1
    if j < len(words) and words[j][1].startswith("["):  # skip the attribute list
        while j < len(words) and "]" not in words[j][1]:
            j += 1
        if (
            j < len(words)
            and words[j][1].endswith(":")
            and not words[j][1].endswith("::")
        ):
            return True
        j += 1
    nxt = words[j][1] if j < len(words) else ""
    return nxt == "=" or (nxt.startswith(":") and not nxt.startswith("::"))


def statement_and_proof(block: str) -> Optional[Tuple[str, str, int]]:
    """(statement, proof, the proof's offset in the raw ``block``) of a
    theorem block with a proof (:func:`seeds.split_statement` on the stripped
    block, its offset mapped back)."""
    split = split_statement(block)
    if split is None:
        return None
    stmt, proof = split
    lead = len(block) - len(block.lstrip())
    return stmt, proof, lead + len(block.strip()) - len(proof)


def with_sorry(block: str) -> Optional[str]:
    """The statement followed by ``sorry``."""
    sp = statement_and_proof(block)
    return None if sp is None else f"{sp[0]}\n  sorry"


DOC = (
    "chapter",
    "section",
    "subsection",
    "subsubsection",
    "paragraph",
    "text",
    "text_raw",
)


def truncated(block: str) -> Optional[str]:
    """The block without its proof's last top-level command when that is a
    closer (``qed`` / ``done`` / ``.`` / ``..``, with a method after ``qed``
    if any) or a terminal ``by …``. A document command the splitter attached
    after the proof (``section …``) is kept: only the proof changes."""
    sp = statement_and_proof(block)
    if sp is None:
        return None
    words = [(o, w) for o, w in top_level_words(block) if o >= sp[2]]
    ends = [
        k
        for k, (_o, w) in enumerate(words)
        if w in CLOSERS or w in (".", "..") or w == "by" or w.startswith("by(")
    ]
    if not ends:
        return None
    k = ends[-1]
    doc = next((o for o, w in words[k + 1 :] if w in DOC), None)
    head = block[: words[k][0]].rstrip()
    return head if doc is None else f"{head}\n\n{block[doc:]}"


def fact_names(block: str) -> List[Tuple[int, str]]:
    """(offset, name) of the fact names the proof REFERS to: outside strings
    and cartouches, not a method name (the word after ``by`` / ``apply`` or one
    starting with "("), not an Isar keyword, with a ``_`` or ``.``
    (``imp_def``, ``add.commute``), and not a binding (review ab492af B1): a
    label (``have run_after:``), the left side of ``note … =``, the variables
    after ``fix`` / ``obtain`` / ``define`` / ``let`` / ``case`` / ``for``, a
    schematic ``?name``."""
    sp = statement_and_proof(block)
    if sp is None:
        return []
    words = [(o, w) for o, w in top_level_words(block) if o >= sp[2]]
    out: List[Tuple[int, str]] = []
    prev, in_vars = "", False
    for k, (off, word) in enumerate(words):
        if word in VAR_INTRO:
            in_vars = True
        elif word in ISAR or word.startswith(('"', "\\<open>", "\u2039")):
            in_vars = word in VAR_INTRO
        skip = (
            prev in ("by", "apply")
            or word.startswith(("(", "?"))
            or in_vars
            or _is_binding(words, k)
        )
        prev = word
        if skip:
            continue
        for m in NAME_RE.finditer(word):
            name = m.group(0).rstrip(".")
            if name in ISAR or not ("_" in name or "." in name):
                continue
            out.append((off + m.start(), name))
    return out


def wrong_fact(block: str) -> Optional[str]:
    """The first fact name of the proof replaced by :data:`NO_SUCH_FACT`."""
    names = fact_names(block)
    if not names:
        return None
    off, name = names[0]
    return block[:off] + NO_SUCH_FACT + block[off + len(name) :]


def variants(block: str) -> Dict[str, Optional[str]]:
    """The four variants of a block (None: not applicable)."""
    return {
        "unchanged": block,
        "sorry": with_sorry(block),
        "truncated": truncated(block),
        "wrong_fact": wrong_fact(block),
    }


def sample_blocks(
    files: Sequence[CorpusFile], census: Dict[str, Any], n: int, rng_seed: int
) -> List[Tuple[str, int]]:
    """``n`` (stem, block index) pairs, seeded, among the theorem blocks the
    census proved, in file / block order."""
    rows = {f["stem"]: f for f in census.get("files", [])}
    pool = [
        (cf.stem, i)
        for cf in files
        if cf.stem in rows
        for i, b in enumerate(rows[cf.stem].get("blocks") or [])
        if b.get("proved") and i < len(cf.blocks) and statement_and_proof(cf.blocks[i])
    ]
    rng = random.Random(rng_seed)
    picked = rng.sample(pool, min(n, len(pool)))
    order = {cf.stem: k for k, cf in enumerate(files)}
    return sorted(picked, key=lambda p: (order[p[0]], p[1]))


def splice(text: str, blocks: Sequence[str], index: int, new_block: str) -> str:
    """``text`` (the file) with its ``index``-th block replaced; the blocks are
    exact substrings of the file, in order."""
    pos = 0
    for i, b in enumerate(blocks):
        at = text.find(b, pos)
        if at < 0:
            raise ValueError(f"block {i} not found in the file text")
        if i == index:
            return text[:at] + new_block + text[at + len(b) :]
        pos = at + len(b)
    raise IndexError(index)
