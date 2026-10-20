"""memory-scaling stage 2: the P0–P6 phase machine with a fake driver and a
fake sampler (no /proc, no server), the manifest, and the P5 text."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "memory_scaling")
)

from memscale import phases, workload  # noqa: E402


class FakeSampler:
    def __init__(self):
        self.phases = []
        self.roots = set()
        self.samples = 0

    def phase(self, name):
        self.phases.append(name)

    def add_roots(self, pids):
        self.roots.update(pids)

    def sample_once(self):
        self.samples += 1
        return {}


class FakeDriver:
    name = "fake"
    topology = "t"

    def __init__(self, fail_load_at=None, raise_at_start=None):
        self.calls = []
        self.fail_load_at = fail_load_at
        self.raise_at_start = raise_at_start
        self.stopped = False

    async def setup(self):
        self.calls.append("setup")

    async def start_session(self, i):
        if self.raise_at_start == i:
            raise RuntimeError("boom")
        self.calls.append(("start", i))

    async def load(self, i, text):
        self.calls.append(("load", i, workload.parse_header(text)[0]))
        ok = i != self.fail_load_at
        return {"ok": ok, "error": None if ok else "2 failed commands"}

    async def recheck(self, i, text):
        await asyncio.sleep(0.01)
        self.calls.append(("recheck", i, workload.parse_header(text)[0]))
        return {"ok": True}

    async def stop_all(self):
        self.stopped = True
        self.calls.append("stop")

    def root_pids(self):
        return [4242]

    def describe(self):
        return {"name": self.name}


def _workload(tmp_path):
    thy = tmp_path / "W.thy"
    thy.write_text("theory W\n  imports Main\nbegin\n\nlemma a: True by simp\n\nend\n")
    return workload.load_workload(
        "HOL", str(thy), 'lemma memscale_p5_done: "True" by simp'
    )


def _cell(tmp_path, k):
    return phases.Cell(
        tool="fake",
        topology="t",
        heap="HOL",
        k=k,
        rep=0,
        out_dir=str(tmp_path),
        workload={},
        config={},
    )


def test_phases_run_in_order_and_manifest_is_written(tmp_path):
    w = _workload(tmp_path)
    drv, smp = FakeDriver(), FakeSampler()
    cell = asyncio.run(
        phases.run_cell(
            _cell(tmp_path, 3),
            drv,
            w,
            smp,
            baseline_s=0.0,
            idle_s=0.0,
            settle_after_stop_s=0,
        )
    )
    assert smp.phases == ["P0", "P1", "P2", "P3", "P4", "P5", "P6"]
    assert cell.outcome == "ok" and cell.error is None and drv.stopped
    assert [c for c in drv.calls if c[0] == "start"] == [
        ("start", 0),
        ("start", 1),
        ("start", 2),
    ]
    assert ("load", 1, "W_1") in drv.calls and ("recheck", 2, "W_2") in drv.calls
    assert smp.roots == {4242} and smp.samples >= 6
    assert all(
        s.start_s is not None and s.load_ok and s.recheck_ok for s in cell.sessions
    )
    m = json.loads((tmp_path / "manifest.json").read_text())
    assert set(m["phases"]) == {"P0", "P1", "P2", "P3", "P4", "P5", "P6"}
    assert all(
        "t0" in p and "t1" in p and p["t1"] >= p["t0"] for p in m["phases"].values()
    )
    assert (
        m["sessions"][0]["index"] == 0
        and m["outcome"] == "ok"
        and m["driver"] == {"name": "fake"}
    )
    assert m["config"]["roots"] == [4242]  # spawned pids recorded (plan review B2/S2)


def test_failed_load_is_partial_and_an_exception_is_error(tmp_path):
    w = _workload(tmp_path)
    cell = asyncio.run(
        phases.run_cell(
            _cell(tmp_path, 2),
            FakeDriver(fail_load_at=1),
            w,
            FakeSampler(),
            baseline_s=0,
            idle_s=0,
            settle_after_stop_s=0,
        )
    )
    assert cell.outcome == "partial" and cell.sessions[1].error == "2 failed commands"
    drv = FakeDriver(raise_at_start=1)
    cell = asyncio.run(
        phases.run_cell(
            _cell(tmp_path, 2),
            drv,
            w,
            FakeSampler(),
            baseline_s=0,
            idle_s=0,
            settle_after_stop_s=0,
        )
    )
    assert cell.outcome == "error" and "boom" in cell.error and drv.stopped
    assert (
        "P1" in cell.phases and "P2" not in cell.phases and cell.finished_at is not None
    )


def test_p5_text_keeps_the_name_and_inserts_the_marker_after_the_first_paragraph(
    tmp_path,
):
    """P5 differs from P3 only by the marker after the first paragraph: an
    edit in the header span or a renamed header made IPS reset the session's
    prover (stage-2 smoke, live probe)."""
    thy = tmp_path / "W.thy"
    thy.write_text(
        "theory W\n  imports Main\nbegin\n\nsection \\<open>one\\<close>\n\nlemma a: True by simp\n\nend\n"
    )
    w = workload.load_workload(
        "HOL", str(thy), 'lemma memscale_p5_done: "True" by simp'
    )
    t = phases.p5_text(w, 4)
    assert t.startswith("theory W_4\n") and t.rstrip().endswith("end")
    assert (
        t.index("section") < t.index(w.trailing_lemma) < t.index("lemma a: True")
        and t.count("\nend") == 1
    )
    assert (
        t.replace(w.trailing_lemma, "").split()
        == workload.with_theory_name(w.text, "W_4").split()
    )


def test_environment_block_has_the_appendix_fields():
    env = phases.environment_block(None)
    assert {
        "hostname",
        "kernel",
        "machine",
        "cpu_count",
        "python",
        "isabelle_version",
        "isabelle_id",
        "ips_commit",
    } <= set(env)


def test_ips_driver_scope_wrapper(monkeypatch):
    """Review 62c7262 S1: the server runs in its own transient scope when
    systemd-run exists (auto), is required with yes, never with no."""
    from memscale import drive_ips

    d = drive_ips.IPSDriver(heap="HOL", imports=["Main"], scope="auto")
    monkeypatch.setattr(drive_ips.shutil, "which", lambda name: "/usr/bin/systemd-run")
    assert (
        d._scoped(["python", "-m", "x"])[:4]
        == ["systemd-run", "--user", "--scope", "--quiet"]
        and d.scope_used
    )
    d2 = drive_ips.IPSDriver(
        heap="HOL", imports=["Main"], scope="auto", memory_max="48G"
    )
    assert "-p" in d2._scoped(["x"]) and "MemoryMax=48G" in d2._scoped(["x"])
    monkeypatch.setattr(drive_ips.shutil, "which", lambda name: None)
    assert drive_ips.IPSDriver(heap="HOL", imports=[], scope="auto")._scoped(["x"]) == [
        "x"
    ]
    assert drive_ips.IPSDriver(heap="HOL", imports=[], scope="no")._scoped(["x"]) == [
        "x"
    ]
    import pytest

    with pytest.raises(RuntimeError):
        drive_ips.IPSDriver(heap="HOL", imports=[], scope="yes")._scoped(["x"])
    assert drive_ips.cgroup_dir_of(10**9) is None  # unreadable pid -> None
