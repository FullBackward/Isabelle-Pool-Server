"""memory-scaling stage 5: the analysis on synthetic cells — per-cell
medians/peaks, the least-squares marginal/fixed costs on PSS and host used,
sessions-per-budget, latencies, the scoped/unscoped refusal, CSV + snippet."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "evaluation" / "memory_scaling")
)

from memscale import analyze  # noqa: E402

MB = 2**20


def _cell(
    root: Path,
    tool,
    topo,
    heap,
    k,
    rep,
    *,
    fixed_mb,
    per_session_mb,
    host_fixed_mb,
    scope=True,
    outcome="ok",
):
    d = root / f"{tool}-{topo}" / heap / f"k{k}" / f"rep{rep}"
    d.mkdir(parents=True)
    sessions = [
        {
            "index": i,
            "start_s": 3.0 + i,
            "load_s": 5.0,
            "recheck_s": 2.0,
            "warm_hit": False,
        }
        for i in range(k)
    ]
    (d / "manifest.json").write_text(
        json.dumps(
            {
                "tool": tool,
                "topology": topo,
                "heap": heap,
                "k": k,
                "rep": rep,
                "outcome": outcome,
                "environment": {"scope_used": scope, "cgroup_dir": "/x"},
                "sessions": sessions,
            }
        )
    )
    rows = []
    t = 1000.0
    for phase, polys, bump in (
        ("P0", 0, 0),
        ("P1", k, 0),
        ("P2", k, 0),
        ("P3", k, 100),
        ("P4", k, 100),
        ("P5", k, 400),
        ("P6", 0, 0),
    ):
        for _ in range(5):
            t += 1
            jvm = fixed_mb * MB
            procs = [
                {
                    "pid": 1,
                    "comm": "java",
                    "role": "jvm",
                    "pss": jvm,
                    "rss": jvm,
                    "uss": jvm,
                    "shared": 0,
                }
            ]
            for i in range(polys):
                p = (per_session_mb + bump) * MB
                procs.append(
                    {
                        "pid": 10 + i,
                        "comm": "poly",
                        "role": "poly",
                        "pss": p,
                        "rss": p,
                        "uss": p,
                        "shared": 0,
                    }
                )
                procs.append(
                    {
                        "pid": 50 + i,
                        "comm": "poly",
                        "role": "poly",
                        "pss": 20 * MB,
                        "rss": 20 * MB,
                        "uss": 20 * MB,
                        "shared": 0,
                    }
                )
            total = sum(p["pss"] for p in procs)
            by_role = {}
            for p in procs:
                by_role.setdefault(
                    p["role"], {"pss": 0, "rss": 0, "uss": 0, "count": 0}
                )
                by_role[p["role"]]["pss"] += p["pss"]
                by_role[p["role"]]["count"] += 1
            rows.append(
                {
                    "t": t,
                    "phase": phase,
                    "sum": {
                        "total": {
                            "pss": total,
                            "rss": total,
                            "uss": total,
                            "count": len(procs),
                        },
                        **by_role,
                    },
                    "procs": procs,
                    "host": {
                        "used": (host_fixed_mb + polys * (per_session_mb + bump)) * MB
                    },
                    "cgroup": {"peak": None},
                }
            )
    (d / "samples.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_cells_series_and_outputs(tmp_path):
    root = tmp_path / "results"
    for k in (1, 2, 4):
        _cell(
            root,
            "ips",
            "pool",
            "HOL",
            k,
            0,
            fixed_mb=500,
            per_session_mb=800,
            host_fixed_mb=2000,
        )
        _cell(
            root,
            "pide-b",
            "B",
            "HOL",
            k,
            0,
            fixed_mb=200 * k,
            per_session_mb=900,
            host_fixed_mb=1500,
        )
    _cell(
        root,
        "ips",
        "pool",
        "HOL",
        8,
        0,
        fixed_mb=500,
        per_session_mb=800,
        host_fixed_mb=2000,
        outcome="error",
    )
    cells = analyze.load_cells(root)
    assert len(cells) == 7
    rows = [analyze.summarize_cell(c) for c in cells]
    r = next(x for x in rows if x["tool"] == "ips" and x["k"] == 2)
    assert (
        r["pss_idle_mb"] == 500 + 2 * 800 + 2 * 20
        and r["pss_settled_mb"] == 500 + 2 * 900 + 40
    )
    assert (
        r["pss_peak_mb"] == 500 + 2 * 1200 + 40
        and r["session_polys_settled"] == 2
        and r["jvm_settled_mb"] == 500
    )
    assert (
        r["host_settled_mb"] == 2000 + 2 * 900
        and r["start_s_p50"] == 3.5
        and r["load_s_p95"] == 5.0
    )
    series = analyze.series_table(rows, budget_gb=100)
    ips = next(s for s in series if s["tool"] == "ips")
    assert ips["ks"] == [1, 2, 4]  # the error cell is left out
    assert (
        abs(ips["pss_settled_marginal_mb"] - 920) < 0.5
        and abs(ips["pss_settled_fixed_mb"] - 500) < 0.5
    )
    assert (  # host fit on the DELTA over the cell's P0: fixed ≈ 0 for a synthetic host
        abs(ips["host_settled_marginal_mb"] - 900) < 0.5
        and abs(ips["host_settled_fixed_mb"]) < 0.5
    )
    assert r["host_delta_settled_mb"] == 2 * 900 and r["host_delta_idle_mb"] == 2 * 800
    assert r["cgroup_cell_peak_mb"] == 0.0 and r["cgroup_peak_rose"] is None
    assert ips["sessions_per_100gb"] == int((100 * 1024 - 0) // 900)
    pide = next(s for s in series if s["tool"] == "pide-b")
    assert (
        abs(pide["pss_settled_marginal_mb"] - (200 + 1000 + 20)) < 0.5
    )  # one JVM per session (200) + its poly (900+100) + the monitor (20)
    assert pide["jvm_settled_mb_by_k"] == {"1": 200.0, "2": 400.0, "4": 800.0}
    out = tmp_path / "tables"
    out.mkdir()
    analyze.write_csv(out / "cells.csv", rows)
    analyze.write_csv(out / "series.csv", series)
    assert (out / "cells.csv").read_text().count(
        "\n"
    ) == 8 and "sessions_per_100gb" in (out / "series.csv").read_text()
    text = analyze.snippet(series, rows, 100)
    assert "| ips/pool | HOL | 1,2,4 |" in text and "pide-b/B" in text
    assert analyze.main(["--results", str(root), "--out", str(out)]) == 0
    assert (root / "REPORT_SNIPPET.md").exists()


def test_external_server_cells_are_smoke_only(tmp_path):
    """Review c17ec04 S1: a series run against a long-lived external server
    carries that server's history in its deltas — no fit, no capacity number."""
    root = tmp_path / "results"
    for k in (1, 2):
        _cell(
            root,
            "ips",
            "pool",
            "HOL",
            k,
            0,
            fixed_mb=500,
            per_session_mb=800,
            host_fixed_mb=2000,
        )
        m = root / "ips-pool" / "HOL" / f"k{k}" / "rep0" / "manifest.json"
        d = json.loads(m.read_text())
        d["driver"] = {"external": True}
        m.write_text(json.dumps(d))
    rows = [analyze.summarize_cell(c) for c in analyze.load_cells(root)]
    assert all(r["external"] for r in rows)
    (s,) = analyze.series_table(rows)
    assert (
        s["smoke_only"] is True
        and s["host_settled_marginal_mb"] is None
        and s["sessions_per_100gb"] is None
    )
    assert "smoke only (external server)" in analyze.snippet([s], rows, 100)


def test_mixed_scopes_form_separate_series(tmp_path):
    """Scoped and unscoped IPS cells are different cgroups: two series rows,
    the unscoped one labelled, never a refusal that blocks the tables."""
    root = tmp_path / "results"
    _cell(
        root,
        "ips",
        "pool",
        "HOL",
        1,
        0,
        fixed_mb=500,
        per_session_mb=800,
        host_fixed_mb=2000,
        scope=True,
    )
    _cell(
        root,
        "ips",
        "pool",
        "HOL",
        2,
        0,
        fixed_mb=500,
        per_session_mb=800,
        host_fixed_mb=2000,
        scope=True,
    )
    _cell(
        root,
        "ips",
        "pool",
        "HOL",
        4,
        0,
        fixed_mb=500,
        per_session_mb=800,
        host_fixed_mb=2000,
        scope=False,
    )
    rows = [analyze.summarize_cell(c) for c in analyze.load_cells(root)]
    series = analyze.series_table(rows)
    assert sorted(s["topology"] for s in series) == ["pool", "pool-unscoped"]
    assert next(s for s in series if s["topology"] == "pool")["ks"] == [1, 2]
    assert next(s for s in series if s["topology"] == "pool-unscoped")["ks"] == [4]


def test_experiment_outputs_are_not_cells(tmp_path):
    """A pressure/warm/kill manifest under results/ (with its own samples)
    must not be read as a cell (it made the server analysis see an
    'unscoped' ips/HOL-Analysis member)."""
    root = tmp_path / "results"
    _cell(
        root,
        "ips",
        "pool",
        "HOL",
        1,
        0,
        fixed_mb=500,
        per_session_mb=800,
        host_fixed_mb=2000,
    )
    d = root / "pressure-ips-HOL"
    d.mkdir()
    (d / "manifest.json").write_text(
        json.dumps(
            {
                "experiment": "pressure",
                "tool": "ips",
                "topology": "pool",
                "heap": "HOL",
                "outcome": "ok",
            }
        )
    )
    (d / "samples.jsonl").write_text("")
    cells = analyze.load_cells(root)
    assert len(cells) == 1 and cells[0]["k"] == 1


def test_contaminated_cell_is_excluded(tmp_path):
    """Two runners wrote into one cell (2026-10-21): phases go backwards and a
    second IPS server pid appears; the cell is flagged and left out of the
    fit and the plots. One late-labelled sample (the sampler's race) is not."""
    root = tmp_path / "results"
    for k in (1, 2, 4):
        _cell(
            root,
            "ips",
            "pool",
            "HOL",
            k,
            0,
            fixed_mb=500,
            per_session_mb=800,
            host_fixed_mb=2000,
        )
    s = root / "ips-pool" / "HOL" / "k4" / "rep0" / "samples.jsonl"
    rows = [json.loads(l) for l in s.read_text().splitlines() if l]
    for r in rows:
        r["roots"] = [1]
    other = [dict(r, phase="P1", roots=[2]) for r in rows[:9]]
    mixed = rows[:20] + other[:3] + rows[20:25] + other[3:6] + rows[25:30]
    mixed += other[6:] + rows[30:]
    s.write_text("\n".join(json.dumps(r) for r in mixed) + "\n")
    s1 = root / "ips-pool" / "HOL" / "k1" / "rep0" / "samples.jsonl"
    rows1 = [json.loads(l) for l in s1.read_text().splitlines() if l]
    late = rows1[:6] + [dict(rows1[4], phase="P0")] + rows1[6:]  # one late P0 label
    s1.write_text("\n".join(json.dumps(r) for r in late) + "\n")
    by_k = {
        r["k"]: r for r in (analyze.summarize_cell(c) for c in analyze.load_cells(root))
    }
    assert by_k[4]["contaminated"] and by_k[4]["root_pids"] == 2
    assert by_k[4]["phase_regressions"] == 9  # every late-labelled row counts
    assert by_k[1]["phase_regressions"] == 1 and not by_k[1]["contaminated"]
    (series,) = analyze.series_table(list(by_k.values()))
    assert series["ks"] == [1, 2]
    text = analyze.snippet([series], list(by_k.values()), 100)
    assert "Excluded as contaminated" in text and "ips/HOL k=4 rep=0" in text


def test_smoke_cells_are_not_matrix_data(tmp_path):
    root = tmp_path / "results"
    _cell(
        root, "ips", "pool", "HOL", 1, 0, fixed_mb=5, per_session_mb=8, host_fixed_mb=20
    )
    _cell(
        root / "smoke",
        "ips",
        "pool",
        "HOL",
        2,
        0,
        fixed_mb=5,
        per_session_mb=8,
        host_fixed_mb=20,
    )
    assert [c["k"] for c in analyze.load_cells(root)] == [1]
    assert len(analyze.load_cells(root, skip_dirs=())) == 2


def test_fit_and_p95():
    assert analyze.fit([(1, 3), (2, 5), (3, 7)]) == (2.0, 1.0)
    assert analyze.fit([(1, 3), (1, 5)]) is None
    assert (
        analyze._p95([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == 10 and analyze._p95([]) is None
    )
