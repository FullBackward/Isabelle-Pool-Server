"""MemoryMonitor `used` sources (batch 1 of the memory-awareness plan, item 1.1).

Before this fix, a host without cgroup memory files (native Linux installs,
cgroup v2 service slices, macOS) made ``_read_used()`` return 0, so the
admission gate silently never fired while the docs promised one. The monitor
now falls back to ``/proc/meminfo`` (MemTotal - MemAvailable) and reports where
the figure came from in ``MemorySnapshot.source``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.app.services import memory_monitor as mm

GIB = 1024**3


def _absent_all(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        "_CG_V2_CURRENT",
        "_CG_V2_MAX",
        "_CG_V2_STAT",
        "_CG_V1_USAGE",
        "_CG_V1_LIMIT",
        "_CG_V1_STAT",
        "_PROC_MEMINFO",
    ):
        monkeypatch.setattr(mm, name, str(tmp_path / f"absent-{name}"))


def _meminfo(tmp_path: Path, total_kb: int, available_kb: int) -> Path:
    path = tmp_path / "meminfo"
    path.write_text(
        f"MemTotal:       {total_kb} kB\n"
        f"MemFree:        {available_kb // 2} kB\n"
        f"MemAvailable:   {available_kb} kB\n"
        "Buffers:        1 kB\n"
    )
    return path


def test_cgroup_v2_source(tmp_path, monkeypatch):
    _absent_all(monkeypatch, tmp_path)
    (tmp_path / "current").write_text(str(4 * GIB))
    (tmp_path / "max").write_text(str(16 * GIB))
    monkeypatch.setattr(mm, "_CG_V2_CURRENT", str(tmp_path / "current"))
    monkeypatch.setattr(mm, "_CG_V2_MAX", str(tmp_path / "max"))
    snap = mm.MemoryMonitor().read()
    assert snap.used_bytes == 4 * GIB
    assert snap.limit_bytes == 16 * GIB
    assert snap.source == mm.SOURCE_CGROUP_V2


def test_cgroup_v1_source(tmp_path, monkeypatch):
    _absent_all(monkeypatch, tmp_path)
    (tmp_path / "usage").write_text(str(3 * GIB))
    (tmp_path / "limit").write_text(str(12 * GIB))
    monkeypatch.setattr(mm, "_CG_V1_USAGE", str(tmp_path / "usage"))
    monkeypatch.setattr(mm, "_CG_V1_LIMIT", str(tmp_path / "limit"))
    snap = mm.MemoryMonitor().read()
    assert snap.used_bytes == 3 * GIB
    assert snap.limit_bytes == 12 * GIB
    assert snap.source == mm.SOURCE_CGROUP_V1


def test_host_meminfo_fallback_when_no_cgroup(tmp_path, monkeypatch):
    """No cgroup files: used = MemTotal - MemAvailable, limit = MemTotal."""
    _absent_all(monkeypatch, tmp_path)
    total_kb, avail_kb = 32 * 1024 * 1024, 20 * 1024 * 1024  # 32 GiB / 20 GiB
    monkeypatch.setattr(
        mm, "_PROC_MEMINFO", str(_meminfo(tmp_path, total_kb, avail_kb))
    )
    snap = mm.MemoryMonitor().read()
    assert snap.used_bytes == (total_kb - avail_kb) * 1024
    assert snap.limit_bytes == total_kb * 1024
    assert snap.source == mm.SOURCE_HOST
    assert 0 < snap.pressure_pct < 100


def test_host_fallback_can_trip_the_gate(tmp_path, monkeypatch):
    """The whole point: a nearly-full native host must NOT admit."""
    _absent_all(monkeypatch, tmp_path)
    total_kb = 16 * 1024 * 1024
    monkeypatch.setattr(
        mm, "_PROC_MEMINFO", str(_meminfo(tmp_path, total_kb, 100 * 1024))
    )  # 100 MiB available
    monitor = mm.MemoryMonitor()
    assert monitor.can_admit() is False
    assert monitor.last_source == mm.SOURCE_HOST


def test_nothing_readable_degrades_to_fallback(tmp_path, monkeypatch):
    _absent_all(monkeypatch, tmp_path)
    monitor = mm.MemoryMonitor()
    snap = monitor.read()
    assert snap.used_bytes == 0
    assert snap.limit_bytes == monitor.fallback_limit_bytes
    assert snap.source == mm.SOURCE_FALLBACK
    assert monitor.can_admit(snap) is True  # inert gate (startup warns about it)


def test_meminfo_missing_available_field_is_not_host_source(tmp_path, monkeypatch):
    _absent_all(monkeypatch, tmp_path)
    path = tmp_path / "meminfo"
    path.write_text(
        "MemTotal:  1000 kB\nMemFree:   10 kB\n"
    )  # old kernels: no MemAvailable
    monkeypatch.setattr(mm, "_PROC_MEMINFO", str(path))
    snap = mm.MemoryMonitor().read()
    assert snap.source == mm.SOURCE_FALLBACK
    assert snap.used_bytes == 0
    assert snap.limit_bytes == 1000 * 1024  # MemTotal is still a usable limit


def test_status_dict_reports_source(tmp_path, monkeypatch):
    _absent_all(monkeypatch, tmp_path)
    status = mm.MemoryMonitor().status_dict()
    assert status["memory_source"] == mm.SOURCE_FALLBACK
    assert set(status) >= {
        "memory_used_mb",
        "memory_limit_mb",
        "memory_available_mb",
        "memory_pressure_pct",
    }
