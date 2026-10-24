"""Training metrics stage 4a: T4's mutation suite — sorry, truncated (plan
review Q4: the proof's last top-level command only), wrong fact; the sample;
the splice that changes one block of the file."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import corpus, mutate  # noqa: E402

SINGLE_BY = 'lemma foo: "x = x" by simp'
USING_BY = 'lemma foo: "P"\n  using bar_baz by (simp add: qux.def)'
STRUCTURED = (
    'lemma foo: "P"\nproof -\n  have "Q" by (rule a_b)\n'
    "  then show ?thesis\n  proof\n    show ?thesis by simp\n  qed\nqed"
)
APPLY = 'lemma foo [simp]: "P"\n  apply (rule x_y)\n  apply simp\n  done'


def test_sorry_keeps_the_statement():
    assert mutate.with_sorry(SINGLE_BY) == 'lemma foo: "x = x"\n  sorry'
    assert mutate.with_sorry(APPLY) == 'lemma foo [simp]: "P"\n  sorry'
    assert mutate.with_sorry('definition f where "f = 0"') is None


def test_truncation_drops_the_proofs_last_top_level_command_only():
    assert mutate.truncated(SINGLE_BY) == 'lemma foo: "x = x"'  # Q4: the method removed
    assert mutate.truncated(USING_BY) == 'lemma foo: "P"\n  using bar_baz'
    assert mutate.truncated(APPLY).endswith("apply simp")  # done dropped
    # the outer qed only; the nested proof stays whole
    assert (
        mutate.truncated(STRUCTURED) == STRUCTURED[: STRUCTURED.rindex("qed")].rstrip()
    )
    assert mutate.truncated('lemma foo: "P" sorry') is None  # nothing to truncate
    assert mutate.truncated('lemma foo: "P" .') == 'lemma foo: "P"'
    # seen in HOL-Analysis: a closer with a method, and a section the splitter attached
    assert mutate.truncated('lemma foo: "P"\nproof\n  show P by simp\nqed auto') == (
        'lemma foo: "P"\nproof\n  show P by simp'
    )
    trail = 'lemma foo: "P"\n  by (force simp: a_b)\n\nsection \\<open>Next\\<close>'
    assert mutate.truncated(trail) == 'lemma foo: "P"\n\nsection \\<open>Next\\<close>'


def test_a_wrong_fact_replaces_the_first_fact_name_not_a_method():
    assert mutate.wrong_fact(USING_BY) == USING_BY.replace(
        "bar_baz", mutate.NO_SUCH_FACT
    )
    assert mutate.wrong_fact(APPLY) == APPLY.replace("x_y", mutate.NO_SUCH_FACT)
    assert mutate.wrong_fact(STRUCTURED) == STRUCTURED.replace(
        "a_b", mutate.NO_SUCH_FACT
    )
    assert mutate.wrong_fact(SINGLE_BY) is None  # no fact name in "by simp"
    by_method = 'lemma foo: "P" by simp_all'  # the word after "by" is the method
    assert mutate.wrong_fact(by_method) is None
    quoted = 'lemma foo: "P" using "a_b" by auto'  # inside a string: a term
    assert mutate.fact_names(quoted) == []


def test_variants_are_deterministic_and_the_splice_changes_one_block(tmp_path):
    text = (
        "theory M\n  imports Main\nbegin\n\n"
        + SINGLE_BY
        + "\n\n"
        + USING_BY
        + "\n\n"
        + APPLY
        + "\n\nend\n"
    )
    p = tmp_path / "M.thy"
    p.write_text(text)
    cf = corpus.load_file(p)
    assert cf is not None and len(cf.blocks) == 3
    v = mutate.variants(cf.blocks[1])
    assert v == mutate.variants(cf.blocks[1]) and set(v) == set(mutate.VARIANTS)
    out = mutate.splice(text, cf.blocks, 1, v["truncated"])
    assert (
        out.count(USING_BY) == 0 and out.count(SINGLE_BY) == 1 and out.count(APPLY) == 1
    )
    assert out.replace(v["truncated"], USING_BY) == text
    census = {
        "files": [
            {
                "stem": "M",
                "blocks": [
                    {"index": 0, "proved": True},
                    {"index": 1, "proved": False},
                    {"index": 2, "proved": True},
                ],
            }
        ]
    }
    picked = mutate.sample_blocks([cf], census, 5, rng_seed=1)
    assert picked == [("M", 0), ("M", 2)]  # proved theorem blocks only, in order
    assert mutate.sample_blocks([cf], census, 1, rng_seed=1) == mutate.sample_blocks(
        [cf], census, 1, rng_seed=1
    )
