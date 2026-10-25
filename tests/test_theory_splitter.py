"""evaluation/scripts/theory_splitter.split_top_level_blocks: cuts only at
lines that start outside comments, strings and cartouches; comment-only
blocks join a neighbour; every block is the body's exact text, in order
(T4's splice relies on it). The case behind the server census of 2026-10-25:
HOL-Analysis' Abstract_Metric_Spaces wraps HOL Light leftovers in a
`(** ... **)` comment with blank lines and `lemma ... oops` inside, which the
old splitter cut into three failing blocks (and 12 more failed after them)."""

from __future__ import annotations

from evaluation.scripts.theory_splitter import split_top_level_blocks

AMS = """lemma metrizable_imp_Hausdorff_space:
   "metrizable_space X \\<Longrightarrow> Hausdorff_space X"
  by (metis Metric_space.Hausdorff_space_mtopology metrizable_space_def)

(**
lemma metrizable_imp_kc_space:
   "metrizable_space X \\<Longrightarrow> kc_space X"
oops
  MESON_TAC[METRIZABLE_IMP_HAUSDORFF_SPACE; HAUSDORFF_IMP_KC_SPACE]);;

lemma kc_space_mtopology:
   "kc_space mtopology"
oops
  REWRITE_TAC[GSYM FORALL_METRIZABLE_SPACE; METRIZABLE_IMP_KC_SPACE]);;
**)

lemma metrizable_imp_t1_space:
   "metrizable_space X \\<Longrightarrow> t1_space X"
  by (simp add: Hausdorff_imp_t1_space metrizable_imp_Hausdorff_space)"""


def _in_order(body, blocks):
    pos = 0
    for b in blocks:
        at = body.find(b, pos)
        assert at >= 0, b[:40]
        pos = at + len(b)


def test_a_block_comment_with_keywords_inside_stays_whole():
    blocks = split_top_level_blocks(AMS)
    assert len(blocks) == 2
    assert blocks[0].startswith("lemma metrizable_imp_Hausdorff_space")
    assert blocks[0].rstrip().endswith("**)")  # the whole comment rides with it
    assert blocks[1].startswith("lemma metrizable_imp_t1_space")
    _in_order(AMS, blocks)


def test_nested_comments_and_lookalikes():
    body = (
        'lemma a: "x = x"\n  by simp\n\n'
        '(* outer (* inner\n\nlemma not_a_block: "y" *) still outer *)\n\n'
        'lemma b: "(*) 2 = (*) 2"\n  by simp\n\n'
        'lemma c: "P"\nproof\n  assume *: "Q"\n  thus False by (subst (asm) *) auto\nqed\n\n'
        'lemma d: "z = z"\n  by simp'
    )
    blocks = split_top_level_blocks(body)
    assert [b.split(":")[0] for b in blocks] == [
        "lemma a",
        "lemma b",
        "lemma c",
        "lemma d",
    ]
    assert "not_a_block" in blocks[0]  # inside the nested comment
    _in_order(body, blocks)


def test_a_cartouche_with_a_keyword_line_stays_whole():
    body = 'text \\<open>A note.\n\nlemma inside the text\n\\<close>\n\nlemma e: "w = w"\n  by simp'
    blocks = split_top_level_blocks(body)
    assert len(blocks) == 2 and "lemma inside the text" in blocks[0]


def test_comment_only_blocks_join_a_neighbour():
    body = (
        "(*Avoid a clash with the existing metric_space locale*)\n\n"
        "locale Metric_space = fixes M d\n\n"
        'lemma f: "v = v"\n  by simp\n\n'
        "(* a closing remark *)"
    )
    blocks = split_top_level_blocks(body)
    assert len(blocks) == 2
    assert blocks[0].startswith("(*Avoid") and "locale Metric_space" in blocks[0]
    assert blocks[1].startswith("lemma f") and blocks[1].endswith(
        "(* a closing remark *)"
    )
    _in_order(body, blocks)


def test_plain_theories_split_as_before():
    body = 'definition g where "g = 1"\n\nlemma h: "g = 1"\n  by (simp add: g_def)\n\ntext \\<open>Done.\\<close>'
    assert split_top_level_blocks(body) == [
        'definition g where "g = 1"',
        'lemma h: "g = 1"\n  by (simp add: g_def)',
        "text \\<open>Done.\\<close>",
    ]


def test_trailing_whitespace_is_kept_so_blocks_are_the_exact_text():
    """2 361 corpus lines end in spaces; stripping them broke T4's splice."""
    body = 'lemma i: "u = u"   \n  by simp  \n\nlemma j: "t = t"\n  by simp'
    blocks = split_top_level_blocks(body)
    assert blocks == ['lemma i: "u = u"   \n  by simp', 'lemma j: "t = t"\n  by simp']
    _in_order(body, blocks)


def test_a_text_inside_an_instance_proof_stays_in_the_block():
    """D16, the census of 2026-10-25: Function_Metric's `instance proof` was not
    counted as a proof, so a `text` inside it cut the proof in three."""
    body = (
        "instance proof\n"
        '  fix x :: nat show "x = x"\n'
        "  proof\n"
        "    show ?thesis by simp\n"
        "  qed\n"
        "next\n"
        "text \\<open>The second goal.\\<close>\n"
        '  show "True" by simp\n'
        "qed\n\n"
        "end"
    )
    blocks = split_top_level_blocks(body)
    assert len(blocks) == 2 and blocks[0].endswith("qed") and blocks[1] == "end"


def test_by_inside_a_proof_does_not_close_it():
    """D16: Function_Topology and Urysohn: a `by` line closed the open `proof`,
    and a `text` further down cut it."""
    body = (
        'lemma k: "P"\n'
        "proof -\n"
        '  have "Q"\n'
        "    by metis\n"
        "  text \\<open>A remark.\\<close>\n"
        "  show ?thesis\n"
        "    by simp\n"
        "qed\n\n"
        'lemma l: "R" by simp'
    )
    blocks = split_top_level_blocks(body)
    assert [b.split(":")[0] for b in blocks] == ["lemma k", "lemma l"]
    assert blocks[0].endswith("qed")


def test_proof_and_qed_on_one_line_close_the_block():
    """D16: `proof qed auto` (Measure_Space) kept the next lemmas in its block."""
    body = (
        'lemma m: "S"\n  proof qed auto\n\n'
        'lemma n: "T"\n  by simp\n\n'
        'lemma o: "U"\nproof\n  show U sorry\noops\n\n'
        'lemma p: "V" by simp'
    )
    blocks = split_top_level_blocks(body)
    assert [b.split(":")[0] for b in blocks] == [
        "lemma m",
        "lemma n",
        "lemma o",
        "lemma p",
    ]


def test_a_private_lemma_starts_a_block():
    """D16: `private lemma` (10 blocks of 5 files) joined the block before it."""
    body = (
        "context\nbegin\n\n"
        'private lemma q: "W" by simp\n\n'
        'qualified definition r where "r = 1"\n\n'
        'lemma s: "W" by (rule q)\n\n'
        "end"
    )
    blocks = split_top_level_blocks(body)
    assert [b.split(":")[0].split(" where")[0] for b in blocks] == [
        "context\nbegin",
        "private lemma q",
        "qualified definition r",
        "lemma s",
        "end",
    ]
    _in_order(body, blocks)
