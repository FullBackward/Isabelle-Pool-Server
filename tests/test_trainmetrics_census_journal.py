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


def _finished_run(tmp_path, monkeypatch):
    """A finished census of A and B under KEY, B's journal record as if B had
    been split differently, and ``replay_change`` finding nothing."""
    files = _files(tmp_path)
    journal = tmp_path / "out" / census_journal.JOURNAL
    census_journal.start_journal(journal, KEY, [])
    asyncio.run(census.run_census(FakeClient(), files, field="HOL", journal=journal))
    head, a, b = journal.read_text().splitlines()
    rec = json.loads(b)
    rec["blocks"][0]["block_sha"] = "split-before-the-fix"
    journal.write_text("\n".join([head, a, json.dumps(rec)]) + "\n")
    monkeypatch.setattr(census_journal, "replay_change", lambda old, new: None)
    return files, journal


def test_a_new_commit_keeps_the_files_whose_blocks_did_not_change(
    tmp_path, monkeypatch
):
    """D16: a splitter fix reruns only the files it splits differently."""
    files, journal = _finished_run(tmp_path, monkeypatch)
    key = {**KEY, "commit": "y"}
    assert census_journal.load_journal(journal, key) == (
        {},
        "the inputs changed: commit",
    )
    kept, note = census_journal.load_unchanged(journal, key, files)
    assert list(kept) == ["A"]
    assert note == (
        "commit x -> y changes nothing a replay runs; "
        "keeping the 1 files whose blocks did not change"
    )
    census_journal.start_journal(
        journal, key, census_journal.kept_in_order(files, kept)
    )
    prev = journal.with_name(census_journal.JOURNAL + ".prev")
    assert len(prev.read_text().splitlines()) == 3  # the old run is kept aside
    assert json.loads(journal.read_text().splitlines()[0]) == {"key": key}
    kept["A"].blocks[0].spans_error = "stale"
    cl = FakeClient()
    again = asyncio.run(
        census.run_census(cl, files, field="HOL", done=kept, journal=journal)
    )
    assert [c[1] for c in cl.calls if c[0] == "enter"] == ["B"]
    a0 = again[0].blocks[0]
    assert a0.spans_error == census.check_spans(files[0].blocks[0], a0.spans)


def test_no_file_is_kept_when_a_replay_could_change(tmp_path, monkeypatch):
    files, journal = _finished_run(tmp_path, monkeypatch)
    monkeypatch.setattr(
        census_journal, "replay_change", lambda old, new: "server/app/x.py changed"
    )
    assert census_journal.load_unchanged(journal, {**KEY, "commit": "y"}, files) == (
        {},
        "no file kept from commit x: server/app/x.py changed",
    )
    # more than the commit changed: nothing to keep, nothing to say
    other = {**KEY, "commit": "y", "corpus_sha": "d"}
    assert census_journal.load_unchanged(journal, other, files) == ({}, None)
    assert census_journal.load_unchanged(tmp_path / "none", KEY, files) == ({}, None)


def test_replay_change_reads_the_definitions_not_their_layout():
    a = 'X_RE = 1\n\n\ndef f(t):\n    """doc"""\n    return t  # note\n\ndef g():\n    pass\n'
    b = 'X_RE = 1\ndef f(t):\n    """doc"""\n    return (t)\n\ndef g():\n    return 2\n'
    assert census_journal._defs(a, ("X_RE", "f")) == census_journal._defs(
        b, ("X_RE", "f")
    )
    assert census_journal._defs(a, ("g",)) != census_journal._defs(b, ("g",))
    head = census_journal.git_commit()
    if head is None:  # no git (the container's /app)
        return
    assert census_journal.replay_change(head, head) is None
    assert census_journal.replay_change("0" * 40, head).startswith("git cannot compare")
    assert census_journal.replay_change(None, head) == "no commit to compare"


def test_resume_point(tmp_path, monkeypatch):
    files, journal = _finished_run(tmp_path, monkeypatch)
    assert census_journal.resume_point(journal, KEY, files, True) == (
        {},
        "--fresh",
        None,
    )
    done, why, note = census_journal.resume_point(journal, KEY, files, False)
    assert sorted(done) == ["A", "B"] and why is None and note is None
    done, why, note = census_journal.resume_point(
        journal, {**KEY, "commit": "y"}, files, False
    )
    assert list(done) == ["A"] and why is None and note.startswith("commit x -> y")
    done, why, note = census_journal.resume_point(
        journal, {**KEY, "corpus_sha": "d"}, files, False
    )
    assert done == {} and why == "the inputs changed: corpus_sha" and note is None
