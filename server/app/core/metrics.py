"""Prometheus metrics for the Isabelle Pool Server.

Event metrics (counters / histogram / inflight gauge) are module-level and
incremented at the relevant code points. Current-state gauges (pool counts,
memory, gateway) are produced on each scrape by ``SessionPoolCollector``, which
reads the data the server already computes in ``SessionManager.get_lru_info()``
— so there is no separate state to keep in sync.

All metrics live in the default registry, which the ``/metrics`` endpoint
(wired via prometheus-fastapi-instrumentator in main.py) serves.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable

from prometheus_client import REGISTRY, Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from server.app.core.logging import get_logger

logger = get_logger(__name__)

# --- event metrics (incremented in code) ---------------------------------

sessions_created = Counter(
    "isabelle_pool_server_sessions_created_total", "Isabelle sessions created"
)
sessions_evicted = Counter(
    "isabelle_pool_server_sessions_evicted_total",
    "Sessions evicted/closed by the manager",
    ["reason"],  # memory | lru | idle
)
pool_exhausted = Counter(
    "isabelle_pool_server_pool_exhausted_total",
    "Session creations refused with 503",
    ["reason"],  # memory | all_busy
)
sessions_force_closed = Counter(
    "isabelle_pool_server_sessions_force_closed_total",
    "Sessions destroyed via DELETE (audit counter for the alarm path)",
)
gateway_restarts = Counter(
    "isabelle_pool_server_gateway_restarts_total", "Dead REPL gateway recoveries"
)
sledgehammer_total = Counter(
    "isabelle_pool_server_sledgehammer_total",
    "Sledgehammer calls by outcome",
    ["result"],  # success | failure
)
sledgehammer_seconds = Histogram(
    "isabelle_pool_server_sledgehammer_seconds",
    "Sledgehammer wall-clock duration",
    buckets=(1, 2, 5, 10, 20, 30, 45, 60, 90, 120),
)
sledgehammer_inflight = Gauge(
    "isabelle_pool_server_sledgehammer_inflight", "Sledgehammer calls currently executing"
)
heap_build_seconds = Histogram(
    "isabelle_pool_server_heap_build_seconds",
    "Heap build wall-clock duration",
    ["task_group"],
    buckets=(5, 15, 30, 60, 120, 300, 600, 1800, 3600),
)


# --- current-state gauges (produced per scrape) --------------------------

class SessionPoolCollector(Collector):
    """Yield pool/memory/gateway gauges from ``get_lru_info()`` on each scrape."""

    def __init__(self, get_info: Callable[[], Dict[str, Any]]):
        self._get_info = get_info

    def collect(self) -> Iterable[GaugeMetricFamily]:
        try:
            info = self._get_info()
        except Exception:  # never let a scrape break the endpoint
            logger.exception("SessionPoolCollector failed to read pool info")
            return

        def g(name: str, doc: str, key: str, default: float = 0.0):
            fam = GaugeMetricFamily(name, doc)
            val = info.get(key, default)
            fam.add_metric([], float(val if val is not None else default))
            return fam

        def g_bytes(name: str, doc: str, mb_key: str):
            fam = GaugeMetricFamily(name, doc)
            mb = info.get(mb_key) or 0.0
            fam.add_metric([], float(mb) * 1024.0 * 1024.0)  # MB -> bytes
            return fam

        yield g("isabelle_pool_server_sessions_active", "Active sessions in pool", "active_sessions")
        yield g("isabelle_pool_server_sessions_busy", "Sessions processing a request", "busy_sessions")
        yield g("isabelle_pool_server_sessions_leased", "Leased sessions", "leased_sessions")
        yield g("isabelle_pool_server_pool_size", "Configured max pool size", "max_pool_size")
        yield g("isabelle_pool_server_max_concurrent_sledgehammer",
                "Concurrent sledgehammer limit", "max_concurrent_sledgehammer")
        yield g_bytes("isabelle_pool_server_memory_used_bytes", "Container memory used (cgroup)", "memory_used_mb")
        yield g_bytes("isabelle_pool_server_memory_limit_bytes", "Container memory limit (cgroup)", "memory_limit_mb")
        yield g("isabelle_pool_server_memory_pressure_pct", "Container memory pressure %", "memory_pressure_pct")

        gw = GaugeMetricFamily("isabelle_pool_server_gateway_up", "1 if the REPL gateway is alive, else 0")
        gw.add_metric([], 1.0 if info.get("gateway_alive") else 0.0)
        yield gw


_pool_collector_registered = False


def register_pool_collector(get_info: Callable[[], Dict[str, Any]]) -> None:
    """Register the pool collector once (idempotent; safe under --reload)."""
    global _pool_collector_registered
    if _pool_collector_registered:
        return
    REGISTRY.register(SessionPoolCollector(get_info))
    _pool_collector_registered = True


class HeapPoolCollector(Collector):
    """Yield heap-pool gauges from the pool's entry list on each scrape."""

    def __init__(self, list_entries: Callable[[], list]):
        self._list_entries = list_entries

    def collect(self) -> Iterable[GaugeMetricFamily]:
        try:
            entries = self._list_entries()
        except Exception:  # never let a scrape break the endpoint
            logger.exception("HeapPoolCollector failed to read heap pool")
            return
        fam = GaugeMetricFamily(
            "isabelle_pool_server_heap_pool_heaps",
            "Heap-pool entries by task group and status",
            labels=["task_group", "status"],
        )
        counts: Dict[tuple, int] = {}
        for e in entries:
            key = (e.get("task_group", "?"), e.get("status", "unknown"))
            counts[key] = counts.get(key, 0) + 1
        for (group, status), n in sorted(counts.items()):
            fam.add_metric([group, status], float(n))
        yield fam


_heap_collector_registered = False


def register_heap_collector(list_entries: Callable[[], list]) -> None:
    """Register the heap-pool collector once (idempotent)."""
    global _heap_collector_registered
    if _heap_collector_registered:
        return
    REGISTRY.register(HeapPoolCollector(list_entries))
    _heap_collector_registered = True
    logger.info("registered SessionPoolCollector for /metrics")
