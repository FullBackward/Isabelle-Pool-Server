"""T5 seeds (design §T5, plan D5): bad steps placed right before a corpus
block that the census proved, so the block checked after the seed tells
whether the session came back intact.

- ``subst``: ``apply (subst add.commute)+`` on an additive goal. It loops
  (every rewrite succeeds and leaves a ``+``); live-checked on the container.
  The goal is the corpus block's own statement when its conclusion has a ``+``
  (``goal_source`` "corpus"), else a synthetic one ("synthetic"; the local
  smoke corpus has no additive statement).
- ``metis``: ``by (metis ...)`` with an oversized fact set on a false goal:
  the names of the lemmas the census proved earlier in the file plus 16
  algebra facts (stage 3a probe: still running at 130 s; plan review Q3).

Every seed lemma has a fresh name, so one that is kept (it should never be)
cannot clash with the corpus block after it.
"""

from __future__ import annotations

import random
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .corpus import CorpusFile

FAMILIES = ("subst", "metis")
THEOREM_RE = re.compile(
    r"(?P<kw>lemma|theorem|corollary|proposition)\b(?P<loc>\s*\(in\s+[^)]*\))?\s*"
    r"(?P<named>(?P<name>[^\s\"\[:‹\\(]+)?\s*(?:\[[^\]]*\])?\s*:(?!:))?"
)
PROOF_START = (
    "by",
    "proof",
    "apply",
    "using",
    "unfolding",
    "including",
    "supply",
    "sorry",
    "oops",
    ".",
    "..",
)
SUBST_TACTIC = "apply (subst add.commute)+"
SYNTHETIC_ADDITIVE = '"(t5a::nat) + t5b = t5b + t5a"'
FALSE_GOALS = (
    '"(t5x::nat) + t5y = t5x + t5y + 1"',
    '"(t5x::nat) * (t5y + t5z) = t5x * t5y + t5z"',
)
ALGEBRA_FACTS = (
    "add.commute add.assoc add.left_commute mult.commute mult.assoc mult.left_commute distrib_left "
    "distrib_right add_mult_distrib add_mult_distrib2 mult_Suc mult_Suc_right add_Suc add_Suc_right "
    "power_add power_mult"
).split()
OPENERS = {'"': '"', "‹": "›", "\\<open>": "\\<close>", "(*": "*)"}


def top_level_words(text: str) -> List[Tuple[int, str]]:
    """(offset, word) of the whitespace-separated words outside strings,
    cartouches (‹› and \\<open>\\<close>, nested) and comments ``(* *)``."""
    out: List[Tuple[int, str]] = []
    i, n, start = 0, len(text), -1
    stack: List[str] = []
    while i < n:
        if stack:
            close = stack[-1]
            if text.startswith(close, i) and not (close == '"' and text[i - 1] == "\\"):
                stack.pop()
                i += len(close)
                continue
            opener = next(
                (o for o in OPENERS if o != '"' and text.startswith(o, i)), None
            )
            if opener and close != '"':
                stack.append(OPENERS[opener])
                i += len(opener)
                continue
            i += 1
            continue
        opener = next((o for o in OPENERS if text.startswith(o, i)), None)
        if opener:
            if start >= 0:
                out.append((start, text[start:i]))
                start = -1
            stack.append(OPENERS[opener])
            i += len(opener)
            continue
        if text[i].isspace():
            if start >= 0:
                out.append((start, text[start:i]))
                start = -1
        elif start < 0:
            start = i
        i += 1
    if start >= 0:
        out.append((start, text[start:]))
    return out


def split_statement(block: str) -> Optional[Tuple[str, str]]:
    """(statement, proof) of a theorem-like block, None for anything else."""
    text = block.strip()
    if not THEOREM_RE.match(text):
        return None
    for off, word in top_level_words(text)[1:]:
        if word in PROOF_START or word.split("(", 1)[0] in ("by", "apply"):
            stmt = text[:off].rstrip()
            return (stmt, text[off:]) if stmt else None
    return None


def lemma_name(block: str) -> Optional[str]:
    """The fact name a top-level theorem block introduces (None for an
    anonymous one or one ``(in locale)``, whose name is not visible here)."""
    m = THEOREM_RE.match(block.strip())
    if not m or m.group("loc") or not m.group("name"):
        return None
    return m.group("name")


def rename(statement: str, name: str) -> str:
    """The statement under a new fact name, without its attributes."""
    m = THEOREM_RE.match(statement)
    if not m:
        raise ValueError("not a theorem statement")
    head = m.group("kw") + (m.group("loc") or "")
    return f"{head} {name}: {statement[m.end():].lstrip()}"


def additive_conclusion(statement: str) -> bool:
    """A ``+`` in the conclusion: after the last top-level ``shows``, else in
    the whole proposition."""
    m = THEOREM_RE.match(statement)
    shows = [off for off, w in top_level_words(statement) if w == "shows"]
    start = shows[-1] if shows else (m.end() if m else 0)
    return "+" in statement[start:]


def statement_of(cf: CorpusFile, i: int) -> str:
    """Block ``i``'s statement ("" for a block that is not a theorem)."""
    split = split_statement(cf.blocks[i])
    return split[0] if split else ""


@dataclass(frozen=True)
class Seed:  # pylint: disable=too-many-instance-attributes
    """One bad step, checked right before corpus block ``position``."""

    seed_id: str
    family: str
    stem: str
    position: int
    name: str
    text: str
    goal: str
    goal_source: str  # corpus | synthetic
    facts: Tuple[str, ...] = field(default_factory=tuple)

    def text_for(self, budget_s: float) -> str:
        """The seed under a name unique to the attempt (budget)."""
        return self.text.replace(f"{self.name}:", f"{self.name}_b{int(budget_s)}:", 1)

    def as_dict(self) -> Dict[str, Any]:
        """JSON form."""
        d = asdict(self)
        d["facts"] = list(self.facts)
        return d


def _proved(census_file: Dict[str, Any], i: int) -> bool:
    blocks = census_file.get("blocks") or []
    return i < len(blocks) and bool(blocks[i].get("proved"))


def candidates(cf: CorpusFile, census_file: Dict[str, Any]) -> List[int]:
    """Positions i >= 1 whose block is a theorem the census proved."""
    return [
        i
        for i in range(1, len(cf.blocks))
        if _proved(census_file, i) and split_statement(cf.blocks[i])
    ]


def metis_facts(
    cf: CorpusFile, census_file: Dict[str, Any], position: int, cap: int
) -> Tuple[str, ...]:
    """Up to ``cap`` names of lemmas proved before ``position`` (nearest first)
    plus the algebra facts."""
    names: List[str] = []
    for i in range(position - 1, -1, -1):
        name = lemma_name(cf.blocks[i])
        if name and _proved(census_file, i) and name not in names:
            names.append(name)
        if len(names) >= cap:
            break
    return tuple(names) + tuple(f for f in ALGEBRA_FACTS if f not in names)


def make_seeds(  # pylint: disable=too-many-arguments,too-many-locals
    files: Sequence[CorpusFile],
    census: Dict[str, Any],
    *,
    per_family: int,
    rng_seed: int,
    max_files: int,
    facts_cap: int = 40,
) -> List[Seed]:
    """``per_family`` seeds of each family at distinct census-proved theorem
    positions in at most ``max_files`` files (seeded choice; fewer files keep
    the prefix replays short). ``subst`` takes additive corpus statements
    first, then synthetic goals; ``metis`` takes the remaining positions."""
    rows = {
        f["stem"]: f
        for f in census.get("files", [])
        if not f.get("acquire_error") and not f.get("enter_error")
    }
    rng = random.Random(rng_seed)
    stems = sorted(cf.stem for cf in files if cf.stem in rows)
    rng.shuffle(stems)
    by_stem = {cf.stem: cf for cf in files}
    pos: List[Tuple[str, int]] = []
    for stem in stems:
        found = [(stem, i) for i in candidates(by_stem[stem], rows[stem])]
        if found:
            pos.extend(found)
            max_files -= 1
        if max_files <= 0:
            break
    rng.shuffle(pos)
    additive = [
        p for p in pos if additive_conclusion(statement_of(by_stem[p[0]], p[1]))
    ]
    subst_pos = (additive + [p for p in pos if p not in additive])[:per_family]
    metis_pos = [p for p in pos if p not in subst_pos][:per_family]
    seeds: List[Seed] = []
    for k, (stem, i) in enumerate(subst_pos):
        name = f"t5_seed_{k}"
        if (stem, i) in additive:
            goal, src = rename(statement_of(by_stem[stem], i), name), "corpus"
        else:
            goal, src = f"lemma {name}: {SYNTHETIC_ADDITIVE}", "synthetic"
        seeds.append(
            Seed(
                f"subst-{k}",
                "subst",
                stem,
                i,
                name,
                f"{goal}\n  {SUBST_TACTIC}",
                goal,
                src,
            )
        )
    for k, (stem, i) in enumerate(metis_pos, start=len(subst_pos)):
        name = f"t5_seed_{k}"
        goal = FALSE_GOALS[k % len(FALSE_GOALS)]
        facts = metis_facts(by_stem[stem], rows[stem], i, facts_cap)
        text = f"lemma {name}: {goal} by (metis {' '.join(facts)})"
        seeds.append(
            Seed(f"metis-{k}", "metis", stem, i, name, text, goal, "synthetic", facts)
        )
    return sorted(seeds, key=lambda s: (s.stem, s.position, s.seed_id))


class BudgetLadder:
    """Which budgets a seed is tried at (design T5, about 1 h; reviews 7bedac5
    S1, beac25c S3/S4). ``counts[b]`` is the number of attempts per family at
    budget b. Every seed gets the smallest budget while its family's count
    lasts. The larger budgets go to seeds that loop, chosen by a seeded rank
    (not by run order, so they spread over the files): a seed is eligible for
    b when its rank is below ``counts[b]`` plus the eligible seeds that did not
    loop so far. A seed climbs one budget at a time and stops at the first
    attempt that does not time out; the slots it leaves pass on. The counts are
    maxima (review 1676d29 S2): a slot passed to a rank whose seed has already
    run is lost, so a cell can run fewer larger-budget attempts; the T5 table
    reports the attempts that ran."""

    def __init__(
        self, counts: Dict[float, int], seeds: Sequence[Seed] = (), rng_seed: int = 0
    ) -> None:
        if not counts:
            raise ValueError("no budgets")
        self.counts = {float(b): int(n) for b, n in counts.items()}
        self.smallest = min(self.counts)
        self.larger = sorted(b for b in self.counts if b != self.smallest)
        self.left = {(fam, b): n for fam in FAMILIES for b, n in self.counts.items()}
        self.missed = {(fam, b): 0 for fam in FAMILIES for b in self.larger}
        rng = random.Random(rng_seed)
        self.rank: Dict[str, int] = {}
        for fam in FAMILIES:
            ids = sorted(s.seed_id for s in seeds if s.family == fam)
            rng.shuffle(ids)
            self.rank.update({sid: r for r, sid in enumerate(ids)})

    def _take(self, family: str, budget: float) -> bool:
        if self.left.get((family, budget), 0) <= 0:
            return False
        self.left[(family, budget)] -= 1
        return True

    def _eligible(self, seed_id: str, family: str, budget: float) -> bool:
        return (
            self.rank.get(seed_id, 0)
            < self.counts[budget] + self.missed[(family, budget)]
        )

    def first(self, family: str) -> Optional[float]:
        """The smallest budget, or None when the family's count is used up."""
        return self.smallest if self._take(family, self.smallest) else None

    def next_budget(
        self, seed_id: str, family: str, after: float, timed_out: bool
    ) -> Optional[float]:
        """The next larger budget for a seed whose attempt at ``after`` ended
        (``timed_out``: it still loops), or None. A seed that stops passes
        the larger slots it was eligible for on to the next seeds."""
        later = [b for b in self.larger if b > after]
        if not timed_out:
            for b in later:
                if self._eligible(seed_id, family, b) and self.left[(family, b)] > 0:
                    self.missed[(family, b)] += 1
            return None
        for b in later:
            if self._eligible(seed_id, family, b) and self._take(family, b):
                return b
        return None
