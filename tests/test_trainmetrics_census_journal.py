"""Training metrics: the census journal (owner, 2026-10-25) — an interrupted
census keeps the files it finished and a rerun with the same inputs continues
after them; slow and timed-out blocks print a progress line."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_trainmetrics_census import (  # noqa: E402  # pylint: disable=wrong-import-position
    THY_A,
    THY_B,
    FakeClient,
)
from trainmetrics import census, census_journal, corpus  # noqa: E402

KEY = {"census_version": 1, "corpus_sha": "c", "commit": "x", "ips_env": {}}


def _files(tmp_path):
    (tmp_path / "A.thy").write_text(THY_A)
    (tmp_path / "B.thy").write_text(THY_B)
    return corpus.load_corpus(str(tmp_path))


def test_a_rerun_continues_after_the_finished_files(tmp_path):
    files = _files(tmp_path)
    journal = tmp_path / "out" / census_journal.JOURNAL
    census_journal.start_journal(journal, KEY, [])
    full = asyncio.run(
        census.run_census(FakeClient(), files, field="HOL", journal=journal)
    )
    lines = journal.read_text().splitlines()
    assert json.loads(lines[0]) == {"key": KEY} and len(lines) == 3
    # interrupted during B: only A was written
    journal.write_text("\n".join(lines[:2]) + "\n")
    done, why = census_journal.load_journal(journal, KEY)
    assert why is None and list(done) == ["A"]
    census_journal.start_journal(
        journal, KEY, census_journal.kept_in_order(files, done)
    )
    cl = FakeClient()
    again = asyncio.run(
        census.run_census(cl, files, field="HOL", done=done, journal=journal)
    )
    entered = [c[1] for c in cl.calls if c[0] == "enter"]
    assert entered == ["B"]  # A is not run again
    assert [r.stem for r in again] == ["A", "B"]
    assert census.census_id(census.summarize(again)) == census.census_id(
        census.summarize(full)
    )
    assert len(journal.read_text().splitlines()) == 3


def test_changed_inputs_start_again_and_keep_the_old_journal(tmp_path):
    journal = tmp_path / census_journal.JOURNAL
    census_journal.start_journal(journal, KEY, [])
    done, why = census_journal.load_journal(journal, {**KEY, "commit": "y"})
    assert done == {} and why == "the inputs changed: commit"
    census_journal.start_journal(journal, {**KEY, "commit": "y"}, [])
    assert (tmp_path / (census_journal.JOURNAL + ".prev")).exists()
    assert census_journal.load_journal(tmp_path / "none.jsonl", KEY) == (
        {},
        "no earlier run",
    )
    journal.write_text("not json\n")
    assert census_journal.load_journal(journal, KEY)[1].startswith("unreadable")


def test_a_file_that_did_not_enter_is_run_again(tmp_path):
    files = _files(tmp_path)
    journal = tmp_path / census_journal.JOURNAL
    census_journal.start_journal(journal, KEY, [])
    asyncio.run(
        census.run_census(
            FakeClient(header_error="Bad header"), files, field="HOL", journal=journal
        )
    )
    done, why = census_journal.load_journal(journal, KEY)
    assert why is None and done == {}


def test_records_survive_the_journal(tmp_path):
    files = _files(tmp_path)
    (rr,) = asyncio.run(census.run_census(FakeClient(), files[:1], field="HOL"))
    back = census_journal.record_from_dict(json.loads(json.dumps(rr.as_dict())))
    assert back.as_dict() == rr.as_dict()


def test_slow_and_timed_out_blocks_print_a_line(tmp_path, capsys):
    files = _files(tmp_path)
    asyncio.run(census.run_census(FakeClient(fail_block="a2"), files, field="HOL"))
    out = capsys.readouterr().out
    assert "  [census 1/2] A block 2/3: timed out after" in out
    assert '(lemma a2: "x = x")' in out and "block 1/3" not in out
    show = census_journal.block_progress(2, 2, files[1], slow_s=0.0)
    rec = census_journal.BlockRecord(
        stem="B",
        index=0,
        block_sha="s",
        n_commands=1,
        census_commands=None,
        budget_s=1.0,
        budget_source="none",
        proved=True,
        wall_s=0.0,
    )
    show(rec)
    assert (
        "[census 2/2] B block 1/1: proved after 0s (lemma b1" in capsys.readouterr().out
    )
