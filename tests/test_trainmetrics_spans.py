"""Training metrics stage 3d: the census records each command's span (T6b's
step split), and checks that the spans re-assemble the block (plan_v1.6 S7)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import census, replay, spans  # noqa: E402

# the live probe's block (CTL block 2), node line 19, three commands on one line
CTL2 = 'lemma [intro!]: "p \\<inter> p \\<rightarrow> q \\<subseteq> q" unfolding imp_def by auto'


def _cmd(kind, line, node_line, a, b):
    return {
        "kind": kind,
        "line": line,
        "node_line": node_line,
        "status": "ok",
        "range": {
            "start": {"line": a[0], "col": a[1]},
            "end": {"line": b[0], "col": b[1]},
        },
    }


def test_spans_of_the_live_ctl_block_reassemble_it():
    cmds = [
        _cmd(
            "lemma", 1, 19, (19, 1), (19, 61)
        ),  # columns count \<inter> as 8 characters
        _cmd("unfolding", 1, 19, (19, 62), (19, 79)),
        _cmd("by", 1, 19, (19, 80), (19, 87)),
    ]
    sp = spans.command_spans(CTL2, cmds)
    assert [(s["kind"], CTL2[s["start"] : s["end"]]) for s in sp] == [
        ("lemma", CTL2[:60]),
        ("unfolding", "unfolding imp_def"),
        ("by", "by auto"),
    ]
    assert spans.check_spans(CTL2, sp) is None


def test_a_multi_line_block_with_a_comment_and_a_non_ascii_name():
    text = '(* note *)\nlemma fooé:\n  "x = x"\n  by simp'
    cmds = [  # the chunk starts at node line 40; the lemma is on its line 2
        _cmd("lemma", 2, 41, (41, 1), (42, 10)),
        _cmd("by", 4, 43, (43, 3), (43, 10)),
    ]
    sp = spans.command_spans(text, cmds)
    assert [text[s["start"] : s["end"]] for s in sp] == [
        'lemma fooé:\n  "x = x"',
        "by simp",
    ]
    assert spans.check_spans(text, sp) is None


def test_a_character_outside_the_bmp_breaks_the_columns_and_the_check_says_so():
    """UTF-16 counts it twice: the server's columns run one ahead of Python's."""
    text = 'lemma "\U0001d538 = \U0001d538" by simp'
    # 13 characters in Python, 15 UTF-16 units: the server ends the lemma at column
    # 16 and starts "by" at 17
    cmds = [_cmd("lemma", 1, 5, (5, 1), (5, 16)), _cmd("by", 1, 5, (5, 17), (5, 24))]
    err = spans.check_spans(text, spans.command_spans(text, cmds))
    assert err is not None and "out of order or bounds" in err  # "by" runs past the end


def test_text_between_commands_or_a_bad_kind_fails():
    text = "lemma x: True by simp"
    ok = [_cmd("lemma", 1, 1, (1, 1), (1, 14)), _cmd("by", 1, 1, (1, 15), (1, 22))]
    assert spans.check_spans(text, spans.command_spans(text, ok)) is None
    shifted = [_cmd("lemma", 1, 1, (1, 1), (1, 10)), _cmd("by", 1, 1, (1, 15), (1, 22))]
    assert "between commands" in spans.check_spans(
        text, spans.command_spans(text, shifted)
    )
    wrong = [_cmd("theorem", 1, 1, (1, 1), (1, 14)), _cmd("by", 1, 1, (1, 15), (1, 22))]
    assert "does not start with" in spans.check_spans(
        text, spans.command_spans(text, wrong)
    )
    assert spans.check_spans(text, None) == "no ranges in the report"


def test_verify_block_keeps_spans_only_when_asked_and_the_census_flags_failures():
    class C:
        async def verify_chunk(self, sid, chunk, timeout=None, *, lease_id=None):
            return {
                "success": True,
                "commands": [
                    _cmd("lemma", 1, 19, (19, 1), (19, 61)),
                    _cmd("unfolding", 1, 19, (19, 62), (19, 79)),
                    _cmd("by", 1, 19, (19, 80), (19, 87)),
                ],
            }

    sess = replay.SessionRef("s", "l")
    kw = {"stem": "CTL", "index": 2, "block_sha": "h", "census_commands": 3}
    plain = asyncio.run(replay.verify_block(C(), sess, CTL2, **kw))
    assert plain.spans is None and plain.spans_error is None
    rec = asyncio.run(replay.verify_block(C(), sess, CTL2, keep_spans=True, **kw))
    assert len(rec.spans) == 3 and rec.spans_error is None
    files = [
        {
            "stem": "CTL",
            "blocks": [
                {"index": 2, "proved": True, "spans_error": None},
                {"index": 3, "proved": True, "spans_error": "text between commands"},
                {
                    "index": 4,
                    "proved": False,
                    "spans_error": "no ranges in the report",
                },  # not judged
            ],
        }
    ]
    assert census.spans_failures(files) == ["CTL#3: text between commands"]


def test_nested_and_formal_comments_between_commands_pass():
    """Review c6aa372 S1: Isabelle comments nest, and formal comments
    (\\<comment> with a cartouche) are not commands either."""
    for lead in (
        "(* outer (* inner *) still outer *)\n",
        "\\<comment> \\<open>a \\<open>nested\\<close> note\\<close>\n",
        "\\<^cancel>\u2039gone\u203a (* and (* this *) *)\n",
    ):
        text = lead + "lemma x: True by simp"
        n = len(lead)
        sp = [
            {"kind": "lemma", "start": n, "end": n + 13},
            {"kind": "by", "start": n + 14, "end": n + 21},
        ]
        assert spans.check_spans(text, sp) is None, lead
    assert spans.strip_comments("a (* b (* c *) d *) e") == "a  e"
    bad = "(* closed *) stray lemma x: True by simp"
    sp = [
        {"kind": "lemma", "start": 19, "end": 32},
        {"kind": "by", "start": 33, "end": 40},
    ]
    assert "between commands" in spans.check_spans(bad, sp)
