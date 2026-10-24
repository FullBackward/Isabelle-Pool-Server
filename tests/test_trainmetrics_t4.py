"""Training metrics stage 4b: the T4 cell on a fake server — part A (files
as written: census verdict against the cold build), part B (sampled blocks
x variants in-session, kept mutants rolled back, one cold build per mutant),
a refused build retried, the summary (review ab492af S1: a mutant both
checkers accept is ineffective, not a miss)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "training_metrics")
)

from trainmetrics import corpus, metrics_t4, mutate, run_t4  # noqa: E402

THY = """theory {name}
  imports Main
begin

lemma {name}_a: "x = x"
  using refl_fact by simp

lemma {name}_b: "y = y"
proof -
  show ?thesis by (rule refl_x)
qed

end
"""


class _Resp:
    def __init__(self, status, body, retry_after="1"):
        self.status_code = status
        self._body = body
        self.content = b"x"
        self.headers = {"Retry-After": retry_after}

    def json(self):
        return self._body


class Fake:
    """In-session: the original block proves; a sorry is kept with used_sorry;
    a wrong fact fails; any other mutant (truncated) is kept with an open
    proof. Cold: only the files as written build; the first build is refused
    once (503 memory_pressure)."""

    def __init__(self, originals, files_text):
        self.originals = set(originals)
        self.files_text = set(files_text)
        self.docs = {}
        self.rollbacks = 0
        self.builds = []
        self.refused = False
        self.n = 0

    async def acquire_session(self, **kw):
        self.n += 1
        self.docs[f"s{self.n}"] = []
        return {"session_id": f"s{self.n}", "lease_id": "l"}

    async def enter_theory(self, sid, name, *, imports=None, lease_id=None):
        return {"success": True}

    async def verify_chunk(self, sid, chunk, timeout=None, *, lease_id=None):
        ok = [{"status": "ok"}]
        if chunk in self.originals:
            self.docs[sid].append(chunk)
            return {"success": True, "commands": ok}
        if mutate.NO_SUCH_FACT in chunk:
            msg = [
                {
                    "status": "failed",
                    "messages": [{"sev": "error", "text": "Undefined fact"}],
                }
            ]
            return {"success": False, "commands": msg}
        self.docs[sid].append(chunk)
        if chunk.rstrip().endswith("sorry"):
            return {"success": True, "used_sorry": True, "commands": ok}
        return {"success": True, "proof_open": True, "commands": ok}

    async def rollback(self, sid, lease_id=None):
        self.rollbacks += 1
        self.docs[sid].pop()

    async def close_session(self, sid, lease_id=None):
        return {}

    async def memory_status(self):
        return {
            "state": "ok",
            "memory": {"used_mb": 1.0},
            "builds_in_flight": {"bigstep": 1},
        }

    async def verify_bigstep_text(self, stem, text, *, field=None, timeout=None):
        await asyncio.sleep(0)
        if not self.refused:
            self.refused = True
            return _Resp(503, {"error": "memory_pressure"})
        self.builds.append(stem)
        ok = text in self.files_text
        return _Resp(
            200,
            {
                "success": ok,
                "error": None if ok else "*** Failed",
                "execution_time": 1.0,
            },
        )


def _setup(tmp_path):
    files, texts = [], []
    for n in ("Ta", "Tb"):
        p = tmp_path / f"{n}.thy"
        p.write_text(THY.format(name=n))
        files.append(corpus.load_file(p))
        texts.append(p.read_text())
    census = {
        "census_id": "cid",
        "files": [
            {
                "stem": cf.stem,
                "blocks": [
                    {"index": i, "proved": True, "n_commands": 3, "wall_s": 0.2}
                    for i in range(len(cf.blocks))
                ],
            }
            for cf in files
        ],
    }
    originals = [b for cf in files for b in cf.blocks]
    return files, texts, census, originals


def test_a_t4_cell_on_a_fake_server(tmp_path, monkeypatch):
    monkeypatch.setattr(run_t4.asyncio, "sleep", _no_sleep)
    files, texts, census, originals = _setup(tmp_path)
    srv = Fake(originals, texts)
    t4 = {"mutation_blocks": 4, "workers": 2, "build_concurrency": 2, "poll_s": 0.01}
    body = asyncio.run(
        run_t4.run_cell(
            srv, t4, files, census, heap="HOL", seed=1, out_dir=tmp_path / "c"
        )
    )
    assert body["outcome"] == "ok" and body["unclosed_sessions"] == []
    assert body["builds_bigstep_max_seen"] == 1
    a = [
        json.loads(x)
        for x in (tmp_path / "c" / "part_a.jsonl").read_text().splitlines()
    ]
    b = [
        json.loads(x)
        for x in (tmp_path / "c" / "part_b.jsonl").read_text().splitlines()
    ]
    assert [r["stem"] for r in a] == ["Ta", "Tb"] and all(
        r["cold"]["accepted"] for r in a
    )
    mutants = [
        r for r in b if r["variant"] != "unchanged"
    ]  # unchanged rows reuse part A's build
    retries = [r["cold"]["retries"] for r in a] + [
        r["cold"]["retries"] for r in mutants
    ]
    assert sum(retries) == 1  # the refused first build, retried once
    assert len(b) == 4 * 4  # 4 blocks x 4 variants, all applicable here
    s = body["summary"]
    assert s["part_a"]["agree"] == 2 and s["part_a"]["disagreements"] == []
    pb = s["part_b"]
    assert pb["unchanged"]["both_accept"] == 4
    for v in ("sorry", "truncated", "wrong_fact"):
        assert (
            pb[v]["both_reject"] == 4
            and pb[v]["safety_findings"] == []
            and pb[v]["ineffective"] == []
        )
        assert (
            pb[v]["in_session_reject_rate"] == 1.0 and pb[v]["cold_reject_rate"] == 1.0
        )
    assert srv.rollbacks == 8  # the kept mutants: sorry and truncated, 4 blocks each
    assert len(srv.builds) == 2 + 4 * 3  # part A's files, then one build per mutant


async def _no_sleep(_s):
    return None


def _row(variant, in_proved, cold, outcome="failed", applicable=True):
    ins = None if in_proved is None else {"proved": in_proved, "outcome": outcome}
    return {
        "stem": "A",
        "index": 1,
        "variant": variant,
        "applicable": applicable,
        "in_session": ins,
        "cold": None if cold is None else {"accepted": cold},
    }


def test_the_t4_summary_lists_safety_findings_and_ineffective_mutants():
    part_b = [
        _row("unchanged", True, True, outcome="ok"),  # block A#1's baseline holds
        _row("sorry", False, False),
        _row("sorry", True, True, outcome="kept"),  # ineffective: both accept
        _row("sorry", True, False, outcome="kept"),  # the safety finding
        _row("sorry", False, None),  # the build did not answer: apart
        _row("sorry", None, None, applicable=False),
        _row("wrong_fact", False, False, outcome="memory"),  # apart
    ]
    pb = metrics_t4.summarize_t4([], part_b)["part_b"]
    s = pb["sorry"]
    assert s["both_reject"] == 1 and s["both_accept"] == 1 and s["in_session_only"] == 1
    assert s["ineffective"] == [("A", 1)] and s["safety_findings"] == [("A", 1)]
    assert s["apart"] == 1 and s["not_applicable"] == 1
    assert (
        s["in_session_reject_rate"] == 0.5
    )  # 1 of the 2 effective mutants rejected in-session
    assert pb["wrong_fact"]["apart"] == 1
    a = metrics_t4.summarize_t4(
        [
            {
                "stem": "X",
                "in_session_proved": True,
                "cold": {"accepted": False, "error": "cite"},
            },
            {"stem": "Y", "in_session_proved": True, "cold": {"accepted": True}},
        ],
        [],
    )["part_a"]
    assert (
        a["agree"] == 1
        and a["in_session_only"] == 1
        and a["disagreements"][0]["stem"] == "X"
    )


def test_a_mutant_of_a_file_that_fails_cold_as_written_is_not_judged():
    """The baseline: CTL fails cold as written (its citations), so its mutants'
    cold rejections say nothing about the mutations."""
    rows = [
        {**_row("unchanged", True, False, outcome="ok"), "stem": "CTL"},
        {**_row("sorry", False, False), "stem": "CTL"},
        _row("unchanged", True, True, outcome="ok"),
        _row("sorry", False, False),
    ]
    pb = metrics_t4.summarize_t4([], rows)["part_b"]
    assert pb["sorry"]["both_reject"] == 1 and pb["sorry"]["baseline_failed"] == 1
    assert pb["sorry"]["apart"] == 1 and "safety_findings" not in pb["unchanged"]
    assert pb["unchanged"]["in_session_only"] == 1  # the baseline row reports it
