"""Training metrics stage 3a: T5 seeds — theorem statements split from their
proofs, fact names, the two seed families, the budget plan."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import corpus, seeds  # noqa: E402

THY = """theory Seeded
  imports Main
begin

definition double :: "nat \\<Rightarrow> nat" where "double n = n + n"

lemma double_add [simp]: "double (a + b) = double a + double b"
  by (simp add: double_def)

lemma
  fixes x :: nat
  assumes "x > 0"
  shows "x + 1 > 1"
  using assms by simp

lemma no_plus: "rev (rev xs) = xs" (* by hand: no *)
  by simp

lemma (in order) loc_fact: "x \\<le> x"
  by simp

theorem plus_cart: \\<open>(a::nat) + 0 = a\\<close> by simp

end
"""


def test_split_statement_handles_the_usual_shapes():
    assert seeds.split_statement('lemma foo: "a + b = b + a" by simp') == (
        'lemma foo: "a + b = b + a"',
        "by simp",
    )
    stmt, proof = seeds.split_statement(
        'lemma\n  fixes x :: nat\n  assumes "x > 0"\n  shows "x + 1 > 1"\n  using assms by simp'
    )
    assert stmt.endswith('shows "x + 1 > 1"') and proof == "using assms by simp"
    # "by" inside a string, a cartouche or a comment is not the proof
    assert (
        seeds.split_statement('lemma "f by x = y" (* by hand *) by(simp)')[1]
        == "by(simp)"
    )
    assert (
        seeds.split_statement("lemma ‹a by b› apply auto done")[1] == "apply auto done"
    )
    assert seeds.split_statement('definition f where "f = 0"') is None
    assert seeds.split_statement('lemma "P"') is None  # no proof: not a candidate


def test_names_renaming_and_additive_conclusions():
    assert seeds.lemma_name('lemma double_add [simp]: "x"\n by simp') == "double_add"
    assert seeds.lemma_name('lemma "x" by simp') is None
    assert seeds.lemma_name('lemma (in order) loc_fact: "x" by simp') is None
    assert seeds.lemma_name('lemma\n fixes x :: nat\n shows "x = x" by simp') is None
    assert (
        seeds.rename('lemma double_add [simp]: "a + b = b + a"', "t5_seed_0")
        == 'lemma t5_seed_0: "a + b = b + a"'
    )
    assert seeds.rename('lemma (in order) "x"', "s") == 'lemma (in order) s: "x"'
    assert seeds.additive_conclusion('lemma "a + b = c"')
    assert not seeds.additive_conclusion('lemma assumes "a + b = c" shows "c = c"')
    assert seeds.additive_conclusion('lemma assumes "c = c" shows "a + b = c"')


def _census(cf, proved=None):
    proved = proved if proved is not None else [True] * len(cf.blocks)
    return {
        "files": [
            {
                "stem": cf.stem,
                "blocks": [{"index": i, "proved": p} for i, p in enumerate(proved)],
            }
        ]
    }


def test_make_seeds_places_both_families_at_distinct_proved_theorems(tmp_path):
    path = tmp_path / "Seeded.thy"
    path.write_text(THY)
    cf = corpus.load_file(path)
    assert cf is not None and len(cf.blocks) == 6
    census = _census(cf)
    out = seeds.make_seeds([cf], census, per_family=2, rng_seed=1, max_files=5)
    subst = [s for s in out if s.family == "subst"]
    metis = [s for s in out if s.family == "metis"]
    assert len(subst) == 2 and len(metis) == 2
    assert len({s.position for s in out}) == 4 and all(s.position >= 1 for s in out)
    # the additive corpus statements come first (blocks 1, 2 and 5 have a + in the conclusion)
    assert {s.goal_source for s in subst} == {"corpus"}
    assert all(s.text.endswith(seeds.SUBST_TACTIC) for s in subst)
    assert all("t5_seed_" in s.text.split("\n")[0] for s in subst)
    for s in metis:
        assert s.text.startswith(f"lemma {s.name}: ") and "by (metis " in s.text
        assert set(seeds.ALGEBRA_FACTS) <= set(s.facts)
        earlier = {seeds.lemma_name(b) for b in cf.blocks[: s.position]} - {None}
        assert set(s.facts) - set(seeds.ALGEBRA_FACTS) <= earlier
    assert out[0].text_for(5.0) != out[0].text_for(30.0)
    assert f"{out[0].name}_b5:" in out[0].text_for(5.0)


def test_synthetic_goals_when_no_additive_statement_and_unproved_blocks_skipped(
    tmp_path,
):
    path = tmp_path / "Seeded.thy"
    path.write_text(THY)
    cf = corpus.load_file(path)
    proved = [True, False, True, True, True, False]  # 1 and 5 not proved in the census
    out = seeds.make_seeds(
        [cf], _census(cf, proved), per_family=2, rng_seed=3, max_files=1
    )
    assert all(s.position in (2, 3, 4) for s in out)
    subst = [s for s in out if s.family == "subst"]
    srcs = sorted(s.goal_source for s in subst)
    assert srcs == ["corpus", "synthetic"]  # only block 2 is additive
    synth = next(s for s in subst if s.goal_source == "synthetic")
    assert seeds.SYNTHETIC_ADDITIVE in synth.text
    assert len([s for s in out if s.family == "metis"]) == 1  # three positions in all


def _ladder_seeds(n):
    return [
        seeds.Seed(f"{fam}-{k}", fam, "S", k, f"t5_seed_{k}", "x", "g", "synthetic")
        for fam in seeds.FAMILIES
        for k in range(n)
    ]


def test_the_ladder_climbs_one_budget_at_a_time_and_stops_when_a_seed_ends():
    """Review beac25c S4."""
    ladder = seeds.BudgetLadder({5: 2, 30: 1, 120: 1})  # no ranks: every seed eligible
    assert ladder.first("subst") == 5.0
    assert ladder.next_budget("subst-0", "subst", 5.0, timed_out=True) == 30.0
    # it ended at 30 s: not tried at 120 s, and the 120 s slot passes on
    assert ladder.next_budget("subst-0", "subst", 30.0, timed_out=False) is None
    assert ladder.left[("subst", 120.0)] == 1 and ladder.missed[("subst", 120.0)] == 1
    assert ladder.first("subst") == 5.0
    assert ladder.next_budget("subst-1", "subst", 5.0, timed_out=True) == 120.0
    assert ladder.first("subst") is None  # two attempts at 5 s per family
    assert ladder.first("metis") == 5.0  # families count apart


def test_the_larger_budgets_go_to_seeded_ranks_not_to_run_order():
    """Review beac25c S3: with 10 seeds in run order, the one 120 s attempt goes
    to the seed of rank 0, wherever it sits; a ranked seed that does not loop
    passes its slot to the next rank."""
    sd = _ladder_seeds(10)
    ladder = seeds.BudgetLadder({5: 10, 120: 1}, sd, rng_seed=7)
    order = [s for s in sd if s.family == "subst"]
    ranked = sorted(order, key=lambda s: ladder.rank[s.seed_id])
    got = []
    for s in order:
        assert ladder.first("subst") == 5.0
        b = ladder.next_budget(s.seed_id, "subst", 5.0, timed_out=True)
        if b is not None:
            got.append(s.seed_id)
    assert got == [ranked[0].seed_id]
    assert (
        ranked[0].seed_id != order[0].seed_id or ranked[1].seed_id != order[1].seed_id
    )
    # the rank-0 seed does not loop: rank 1 inherits the slot
    ladder = seeds.BudgetLadder({5: 10, 120: 1}, sd, rng_seed=7)
    got = []
    for s in sorted(order, key=lambda s: ladder.rank[s.seed_id]):
        ladder.first("subst")
        loops = ladder.rank[s.seed_id] != 0
        if ladder.next_budget(s.seed_id, "subst", 5.0, timed_out=loops) is not None:
            got.append(ladder.rank[s.seed_id])
    assert got == [1]
