"""fill_sorries stage 1b (plan 2026-10-18 §1.2–1.4): target extraction, the patch
builder (review B5/B6), the deterministic ETA, the tier-aware slots (review B3)
and the job runner with a fake session. No Isabelle."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from server.app.errors import MemoryPressure
from server.app.services import fill_sorries as fs
from server.app.services.session_memory import SessionMemoryMixin
from server.app.services.sledgehammer_slots import SledgehammerSlots


def _cmd(i, kind, line, node_line=None, col=1, end_col=6):
    return {
        "i": i,
        "kind": kind,
        "line": line,
        "node_line": node_line or line + 1,
        "range": {
            "start": {"line": node_line or line + 1, "col": col},
            "end": {"line": node_line or line + 1, "col": end_col},
        },
    }


# ------------------------------------------------------------------ targets


def test_extract_targets_hosts_on_previous_command():
    cmds = [
        _cmd(2, "lemma", 1),
        _cmd(4, "sorry", 1, col=31),
        _cmd(6, "lemma", 2),
        _cmd(8, "proof", 3),
        _cmd(10, "fix", 4),
        _cmd(12, "show", 4),
        _cmd(14, "sorry", 5, col=5),
        _cmd(16, "qed", 6),
        _cmd(18, "oops", 7),
    ]
    t = fs.extract_targets(cmds)
    assert [(x.index, x.host_index, x.host_kind, x.line, x.col) for x in t] == [
        (4, 2, "lemma", 1, 31),
        (14, 12, "show", 5, 5),
    ]


def test_extract_targets_skips_a_leading_sorry():
    assert (
        fs.extract_targets(
            [_cmd(0, "sorry", 1), _cmd(2, "lemma", 2), _cmd(4, "sorry", 2)]
        )[0].index
        == 4
    )


# -------------------------------------------------------------------- patch


@pytest.mark.parametrize(
    "n,methods,expected",
    [
        (1, ["by auto"], "by auto"),
        (1, ["using assms by metis"], "using assms by metis"),
        (1, ["by (simp add: foo)"], "by (simp add: foo)"),
        (1, ["apply satx"], "apply satx\ndone"),  # apply-style answer: chain + done
        (2, ["apply satx", "apply presburger"], "apply satx\napply presburger\ndone"),
        (
            2,
            ["by simp", "using assms by blast"],
            "apply simp\nusing assms apply blast\ndone",
        ),
        (
            2,
            ["by (simp add: x) (auto)", "by simp"],
            None,
        ),  # two-method form: unpatchable
        (2, ["by simp"], None),  # not every subgoal proved
        (1, ["by (metis a) (simp)"], None),
    ],
)
def test_replacement_for(n, methods, expected):
    assert fs.replacement_for(n, methods) == expected


def _target(index, line, col, host=0):
    return fs.SorryTarget(
        index=index,
        host_index=host,
        line=line,
        node_line=line + 1,
        col=col,
        end_col=col + 5,
        host_kind="lemma",
    )


def test_build_patch_ascii_and_indent():
    text = 'lemma a1: "rev (rev xs) = xs" sorry\nlemma a4: "P"\n  apply (rule conjI)\n  sorry\n'
    t1, t2 = _target(4, 1, 31), _target(22, 4, 3)
    out, bad = fs.build_patch(
        text, [t1, t2], {4: "by auto", 22: "apply satx\napply presburger\ndone"}
    )
    assert bad == []
    assert (
        out
        == 'lemma a1: "rev (rev xs) = xs" by auto\nlemma a4: "P"\n  apply (rule conjI)\n  apply satx\n  apply presburger\n  done\n'
    )


def test_build_patch_symbol_lines_fall_back_to_token_search():
    # the reported column is in Line.Document units: off by the symbol width on these lines
    for line in ('lemma a2: "\\<forall>x. x = x" sorry', 'lemma a2: "∀x. x = x" sorry'):
        t = _target(4, 1, len(line) - 1)  # wrong column on purpose
        out, bad = fs.build_patch(line + "\n", [t], {4: "by simp"})
        assert bad == [] and out.endswith(" by simp\n") and "sorry" not in out


def test_build_patch_unpatchable_when_no_sorry_on_the_line():
    out, bad = fs.build_patch(
        "lemma x: True by simp\n", [_target(4, 1, 20)], {4: "by auto"}
    )
    assert bad == [4] and out == "lemma x: True by simp\n"


def test_build_patch_leaves_unsolved_sorries():
    text = "lemma a: A sorry\nlemma b: B sorry\n"
    out, _ = fs.build_patch(
        text, [_target(4, 1, 12), _target(8, 2, 12)], {8: "by blast"}
    )
    assert out == "lemma a: A sorry\nlemma b: B by blast\n"


# ---------------------------------------------------------------------- ETA


def test_eta_and_remaining_steps():
    job = fs.FillJob(
        session_id="s",
        text="",
        mode="chunk",
        timeout_s=30,
        max_total_s=600,
        provers="",
        targets=[_target(4, 1, 1), _target(8, 2, 1)],
    )
    job.status = "running"
    assert job.eta(10.0) == {
        "worst": 130.0,
        "typical": 22.0,
    }  # 2 steps × (30 + 30) + 10 / 2 × 6 + 10
    job.results[0].subgoals = 2
    assert job.eta(0.0)["worst"] == 180.0
    job.parked_reason = "critical"
    assert job.eta(0.0) is None
    job.status = "done"
    assert job.eta(0.0) == {"worst": 0.0, "typical": 0.0}


# -------------------------------------------------------------------- slots


class _Mon:
    def __init__(self, state="ok", external=False):
        self.last_state = state
        self.last_snapshot = type("S", (), {"external_pressure": external})()


class _Mgr:
    def __init__(self, state="ok", external=False, enabled=True):
        self.memory = _Mon(state, external)
        self.memory_management_enabled = enabled

    def memory_facts(self, snap=None):
        return {"state": self.memory.last_state, "retry_after_s": 5}


def test_slots_capacity_table():
    assert SledgehammerSlots(_Mgr("ok"), 2).capacity_now() == 2
    assert SledgehammerSlots(_Mgr("unknown"), 2).capacity_now() == 2
    assert SledgehammerSlots(_Mgr("warning"), 2).capacity_now() == 1
    assert SledgehammerSlots(_Mgr("critical"), 2).capacity_now() == 0
    assert SledgehammerSlots(_Mgr("ok", external=True), 2).capacity_now() == 0
    assert SledgehammerSlots(_Mgr("critical", enabled=False), 2).capacity_now() == 2


def test_slots_interactive_refused_at_critical_and_parked_job_resumes(monkeypatch):
    from server.app.core.config import Memory

    monkeypatch.setattr(Memory, "FILL_PARK_POLL_S", 0.05)
    mgr = _Mgr("critical")
    slots = SledgehammerSlots(mgr, 2)

    async def run():
        with pytest.raises(MemoryPressure):
            await slots.acquire(parkable=False)
        seen = []
        task = asyncio.create_task(
            slots.acquire(
                parkable=True, on_wait=lambda pos, why: seen.append((pos, why))
            )
        )
        await asyncio.sleep(0.12)
        assert not task.done() and seen and seen[0] == (0, "critical")
        mgr.memory.last_state = "warning"  # tier drops: the parked waiter resumes
        tok = await asyncio.wait_for(task, 1.0)
        assert slots.running == 1 and slots.queue_position(tok) == -1
        # warning = 1 slot: a second fill waits (position 0), an interactive call also waits (not refused)
        t2 = asyncio.create_task(slots.acquire(parkable=True))
        await asyncio.sleep(0.12)
        assert not t2.done() and slots.waiters() == 1
        await slots.release()
        await asyncio.wait_for(t2, 1.0)
        assert slots.running == 1
        await slots.release()
        assert slots.running == 0

    asyncio.run(run())


# ---------------------------------------------------------------------- job


class _Session(SessionMemoryMixin):
    """Fake memory-aware session: canned `sledgehammer_at_command` replies keyed
    by (host, subgoal); `delay` keeps a step running so cancellation is observable."""

    def __init__(self, replies, delay=0.0, dead=False):
        self.session_id = uuid.uuid4()
        self.replies, self.calls, self.activity = replies, [], 0
        self.delay, self.dead = delay, dead
        self.status = "active"
        self._init_memory()
        self.mem_sampled_at = float(
            "inf"
        )  # never due: the fake has no backend to probe

    def sledgehammer_at_command(self, index, subgoal, timeout_s, max_proofs, provers):
        self.calls.append((index, subgoal, timeout_s, max_proofs, provers))
        if self.delay:
            time.sleep(self.delay)
        return self.replies[(index, subgoal)]

    def update_activity(self):
        self.activity += 1

    def prover_alive(self):
        return None

    def check_prover_dead(self, timeout=5.0):
        return self.dead


class _JobMgr(fs.FillJobMixin):
    def __init__(self):
        self.memory = _Mon("ok")
        self.memory_management_enabled = True
        self.slots = SledgehammerSlots(self, 2)

    def memory_facts(self, snap=None):
        return {"state": "ok", "retry_after_s": 5}

    def discard_dead(self, sid, reason="prover_died"):
        return True


def _run_job(session, commands, text, **kw):
    mgr = _JobMgr()

    async def go():
        job = mgr.start_fill(
            session,
            text=text,
            mode="chunk",
            commands=commands,
            timeout_s=kw.get("timeout_s", 20),
            max_total_s=kw.get("max_total_s", 600),
            provers="",
        )
        await asyncio.wait_for(job.task, 5.0)
        return mgr, job

    return asyncio.run(go())


def test_job_fills_one_and_two_subgoals_and_skips_after_failure():
    text = 'lemma a1: "A" sorry\nlemma a4: "B"\n  apply (rule conjI)\n  sorry\nlemma a2: "C" sorry\n'
    cmds = [
        _cmd(2, "lemma", 1),
        _cmd(4, "sorry", 1, col=15),
        _cmd(6, "lemma", 2),
        _cmd(8, "apply", 3),
        _cmd(10, "sorry", 4, col=3),
        _cmd(12, "lemma", 5),
        _cmd(14, "sorry", 5, col=15),
    ]
    replies = {
        (2, 1): {
            "found": True,
            "subgoals": 1,
            "results": ["auto: Try this: by auto (0.3 ms)"],
        },
        (8, 1): {
            "found": True,
            "subgoals": 2,
            "results": ["satx: Try this: apply satx (0.8 ms)"],
        },
        (8, 2): {
            "found": True,
            "subgoals": 2,
            "results": ["Try this: apply presburger (1 ms)"],
        },
        (12, 1): {
            "found": True,
            "subgoals": 1,
            "results": ["No proof found", "Timed out"],
        },
    }
    s = _Session(replies)
    mgr, job = _run_job(s, cmds, text)
    body = job.to_dict()
    assert body["status"] == "done" and body["filled"] == 2 and body["n"] == 3
    assert [r["outcome"] for r in body["results"]] == ["proved", "proved", "none"]
    assert body["results"][1]["method"] == "apply satx\napply presburger\ndone"
    assert (
        body["patched_text"]
        == 'lemma a1: "A" by auto\nlemma a4: "B"\n  apply (rule conjI)\n  apply satx\n  apply presburger\n  done\nlemma a2: "C" sorry\n'
    )
    assert s.calls[0][3] == 1  # max_proofs=1
    assert mgr.slots.running == 0 and body["eta_s"] == {"worst": 0.0, "typical": 0.0}


def test_job_skips_later_subgoals_after_a_failure_and_reports_partial():
    cmds = [_cmd(2, "apply", 1), _cmd(4, "sorry", 2, col=3)]
    replies = {
        (2, 1): {
            "found": True,
            "subgoals": 3,
            "results": ["Try this: apply simp (1 ms)"],
        },
        (2, 2): {"found": True, "subgoals": 3, "results": ["No proof found"]},
    }
    s = _Session(replies)
    _, job = _run_job(s, cmds, "apply x\n  sorry\n")
    r = job.to_dict()["results"][0]
    assert r["outcome"] == "partial" and [p["outcome"] for p in r["per_subgoal"]] == [
        "proved",
        "none",
        "skipped",
    ]
    assert len(s.calls) == 2  # subgoal 3 never run


def test_job_cancel_between_steps_and_max_total():
    cmds = [
        _cmd(2, "lemma", 1),
        _cmd(4, "sorry", 1, col=12),
        _cmd(6, "lemma", 2),
        _cmd(8, "sorry", 2, col=12),
    ]
    replies = {
        (2, 1): {"found": True, "subgoals": 1, "results": ["Try this: by simp (1 ms)"]},
        (6, 1): {"found": True, "subgoals": 1, "results": ["Try this: by auto (1 ms)"]},
    }
    s = _Session(replies, delay=0.3)
    mgr = _JobMgr()

    async def go():
        job = mgr.start_fill(
            s,
            text="lemma a: A sorry\nlemma b: B sorry\n",
            mode="chunk",
            commands=cmds,
            timeout_s=20,
            max_total_s=600,
            provers="",
        )
        await asyncio.sleep(0.05)
        with pytest.raises(ValueError):  # a second job cannot start while one runs
            mgr.start_fill(
                s,
                text="x",
                mode="chunk",
                commands=cmds,
                timeout_s=1,
                max_total_s=1,
                provers="",
            )
        t = time.time()
        assert await mgr.cancel_job(s.session_id) is True
        assert time.time() - t >= 0.2  # waited for the in-flight 0.3 s step (B2)
        assert job.inflight is None and len(s.calls) == 1
        assert (
            job.status == "cancelled"
            and mgr.fill_status(s.session_id)["status"] == "cancelled"
        )
        assert await mgr.cancel_job(s.session_id) is False
        assert mgr.slots.running == 0
        j3 = mgr.start_fill(
            s,
            text="lemma a: A sorry\nlemma b: B sorry\n",
            mode="chunk",
            commands=cmds,
            timeout_s=20,
            max_total_s=0.0,
            provers="",
        )  # budget already spent
        await asyncio.wait_for(j3.task, 2.0)
        return j3

    j3 = asyncio.run(go())
    d = j3.to_dict()
    assert (
        d["status"] == "done"
        and d["filled"] == 0
        and all(r["outcome"] == "skipped" for r in d["results"])
    )


def test_job_prover_death_marks_error():
    cmds = [_cmd(2, "lemma", 1), _cmd(4, "sorry", 1, col=12)]
    s = _Session(
        {
            (2, 1): {
                "found": True,
                "subgoals": 1,
                "results": ["Try this: by simp (1 ms)"],
            }
        }
    )
    s.check_prover_dead = lambda: True  # the hook sees a corpse after the call
    _, job = _run_job(s, cmds, "lemma a: A sorry\n")
    d = job.to_dict()
    assert d["results"][0]["outcome"] == "error" and job.error == "prover_died"
