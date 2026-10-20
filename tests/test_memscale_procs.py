"""memory-scaling experiments, stage 1 (plan 2026-10-20): the /proc readers,
the sampler and the workload helpers against a fake /proc tree."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "memory_scaling")
)

from memscale import procs, sampler, workload  # noqa: E402


def _fake_proc(
    tmp_path,
    table,
    mem,
    meminfo=("MemTotal: 134217728 kB", "MemAvailable: 100000000 kB"),
):
    """table: {pid: (ppid, comm, state, cmdline)}; mem: {pid: (rss_kb, pss_kb, priv_kb, shared_kb)}"""
    root = tmp_path / "proc"
    root.mkdir()
    for pid, (ppid, comm, state, cmd) in table.items():
        d = root / str(pid)
        d.mkdir()
        (d / "stat").write_text(
            f"{pid} ({comm}) {state} {ppid} {pid} 1 0 -1 0 0 0 0 0 0 0 0 0\n"
        )
        (d / "cmdline").write_text(cmd.replace(" ", "\0") + "\0")
        if pid in mem:
            rss, pss, priv, shared = mem[pid]
            (d / "smaps_rollup").write_text(
                f"00400000-7fff Rss: {rss} kB\nRss:  {rss} kB\nPss:  {pss} kB\n"
                f"Private_Clean: 0 kB\nPrivate_Dirty: {priv} kB\nShared_Clean: {shared} kB\nShared_Dirty: 0 kB\n"
            )
    (root / "meminfo").write_text("\n".join(meminfo) + "\n")
    (root / "self").mkdir()
    (root / "self" / "cgroup").write_text("0::/user.slice/exp.scope\n")
    cg = tmp_path / "cgroup" / "user.slice" / "exp.scope"
    cg.mkdir(parents=True)
    (cg / "memory.current").write_text("123456\n")
    (cg / "memory.peak").write_text("234567\n")
    (cg / "memory.max").write_text("max\n")
    (cg / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_kill 2\n")
    return root, tmp_path / "cgroup"


TABLE = {
    1: (0, "systemd", "S", "/sbin/init"),
    100: (
        1,
        "python",
        "S",
        "python -m server.app.main",
    ),  # IPS server (root by pattern)
    101: (100, "bash", "S", "bash /opt/isabelle/bin/isabelle scala x"),
    102: (101, "java", "S", "java -cp ... isabelle.Scala"),  # gateway JVM
    103: (102, "bash_process", "S", "bash_process"),
    104: (103, "poly", "S", "poly -q --minheap 500"),  # session poly
    105: (103, "poly", "Z", "poly"),  # a zombie: no memory, skipped
    200: (
        1,
        "bash",
        "S",
        "bash /opt/isabelle/bin/isabelle pide_mcp -l HOL",
    ),  # PIDE MCP (root by pattern)
    201: (200, "java", "S", "java isabelle.pide.mcp.PIDE_MCP"),
    202: (201, "poly", "S", "poly -q"),
    300: (1, "java", "S", "java SomethingElse"),  # a stranger
}
MEM = {
    100: (50_000, 50_000, 50_000, 0),
    101: (3_000, 1_000, 500, 2_500),
    102: (3_000_000, 2_900_000, 2_800_000, 200_000),
    103: (1_000, 500, 200, 800),
    104: (1_200_000, 900_000, 600_000, 600_000),
    200: (3_000, 1_000, 500, 2_500),
    201: (1_500_000, 1_400_000, 1_300_000, 200_000),
    202: (1_100_000, 800_000, 500_000, 600_000),
    300: (999_999, 999_999, 999_999, 0),
}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    root, cg = _fake_proc(tmp_path, TABLE, MEM)
    monkeypatch.setattr(procs, "PROC_ROOT", str(root))
    monkeypatch.setattr(procs, "CGROUP_ROOT", str(cg))
    return root


def test_process_table_and_roots(fake):
    table = procs.process_table()
    assert table[104] == (103, "poly", "S") and table[105][2] == "Z"
    assert procs.find_roots(["server.app.main"], table) == {100}
    assert procs.find_roots(["pide_mcp"], table) == {200}
    assert procs.find_roots([], table) == set()
    assert procs.descendants([100], table) == {100, 101, 102, 103, 104, 105}
    assert procs.descendants([4242], table) == set()


def test_snapshot_sums_pss_by_role_and_skips_zombies(fake):
    rows = procs.snapshot([100])
    assert [r.pid for r in rows] == [100, 101, 102, 103, 104]  # 105 is a zombie
    by = {r.pid: r for r in rows}
    assert by[104].role == "poly" and by[102].role == "jvm" and by[103].role == "other"
    assert (
        by[104].pss == 900_000 * 1024
        and by[104].uss == 600_000 * 1024
        and by[104].shared == 600_000 * 1024
    )
    sums = procs.sum_rows(rows)
    assert sums["total"]["pss"] == (50_000 + 1_000 + 2_900_000 + 500 + 900_000) * 1024
    assert sums["jvm"]["pss"] == 2_900_000 * 1024 and sums["poly"]["count"] == 1
    # the stranger JVM (300) is not under any root
    assert all(r.pid != 300 for r in rows)


def test_host_and_cgroup_memory(fake):
    host = procs.host_memory()
    assert (
        host["total"] == 134217728 * 1024
        and host["used"] == (134217728 - 100000000) * 1024
    )
    cg = procs.cgroup_memory()
    assert cg == {"current": 123456, "peak": 234567, "max": None, "oom_kill": 2}


def test_sampler_writes_rows_with_phase_and_patterns(fake, tmp_path):
    out = tmp_path / "samples.jsonl"
    s = sampler.Sampler(
        str(out), patterns=["server.app.main", "pide_mcp"], interval_s=0.02
    )
    with s:
        s.phase("P1")
        s.add_roots([300])
        s.phase("P2")
    rows = sampler.load_rows(str(out))
    assert rows[0]["phase"] == "P1" and rows[0]["roots"] == [100, 200]
    p2 = [r for r in rows if r["phase"] == "P2"]
    assert p2 and p2[0]["roots"] == [100, 200, 300]
    assert (
        p2[0]["sum"]["total"]["pss"] > rows[0]["sum"]["total"]["pss"]
    )  # the stranger counted now
    assert p2[0]["host"]["used"] > 0 and p2[0]["cgroup"]["oom_kill"] == 2
    assert {p["role"] for p in p2[0]["procs"]} >= {"jvm", "poly", "python"}
    assert s.rows_written == len(rows) >= 2
    json.dumps(rows[0])  # serialisable


def test_workload_helpers(tmp_path):
    thy = tmp_path / "W.thy"
    thy.write_text(
        '(* c *)\ntheory W\n  imports Main "HOL-Library.Multiset"\nbegin\n\nlemma a: True by simp\n\nend\n'
    )
    w = workload.load_workload("HOL-Library", str(thy), 'lemma z: "True" by simp')
    assert w.theory_name == "W" and w.imports == ("Main", "HOL-Library.Multiset")
    assert len(w.sha256) == 64 and w.as_manifest()["lines"] == 9
    renamed = workload.with_theory_name(w.text, "W_3")
    assert "theory W_3\n" in renamed and workload.parse_header(renamed)[0] == "W_3"
    body = workload.body_before_end(w.text)
    assert body.rstrip().endswith("by simp") and "\nend" not in body
