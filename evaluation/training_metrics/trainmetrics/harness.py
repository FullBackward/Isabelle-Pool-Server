"""What every measured cell does around its work (T1 and T5): start (or
attach to) the server, record its process group and cgroup at once, run the
memscale sampler for the whole cell, open the client, stop on SIGTERM (the
matrix runner's timeout) by cancelling the work, and always stop the server
and write the manifest. Moved out of ``run_t1.amain`` for T5 (stage 3b)."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from . import EXPERIMENT_DIR
from .replay import BUDGET_MAX_S
from .serve import isabelle_cmd, make_driver

# work(client, sampler, body, checkpoint): fills body; raising ends the cell as "error"
Work = Callable[
    [Any, Any, Dict[str, Any], Callable[[Dict[str, Any]], Any]], Awaitable[Any]
]


def cell_arg_parser(doc: Optional[str]) -> argparse.ArgumentParser:
    """The options every runner shares: config, census, results tree, server."""
    ap = argparse.ArgumentParser(
        description=doc, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--config", default=str(EXPERIMENT_DIR / "config.yaml"))
    ap.add_argument(
        "--census", default=str(EXPERIMENT_DIR / "results/census/census.json")
    )
    ap.add_argument("--out", default=str(EXPERIMENT_DIR / "results"))
    ap.add_argument("--url", default=None)
    ap.add_argument("--ips-external", action="store_true")
    ap.add_argument("--local-smoke", action="store_true")
    return ap


def write_manifest(out_dir: Path, body: Dict[str, Any]) -> Path:
    """``manifest.json`` (written at start and at the end, as in memscale)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "manifest.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=1, default=str)
    return path


async def run_with_server(  # pylint: disable=too-many-arguments,too-many-locals
    cfg: Dict[str, Any],
    *,
    heap: str,
    out_dir: Path,
    head: Dict[str, Any],
    work: Work,
    url: Optional[str] = None,
    external: bool = False,
) -> Dict[str, Any]:
    """Run ``work`` against a server for ``heap`` and return the manifest body
    (``outcome``: ok, error, terminated; ``work`` sets ok)."""
    # pylint: disable=import-outside-toplevel
    from memscale.phases import environment_block
    from memscale.sampler import Sampler

    from client.async_client import PoolAsyncClient

    ips = cfg["ips"]
    driver = make_driver(cfg, heap, url, external)
    head = {
        **head,
        "outcome": "running",
        "started_at": time.time(),
        "environment": environment_block(cfg.get("isabelle_home"), isabelle_cmd()),
    }
    write_manifest(out_dir, head)
    body: Dict[str, Any] = dict(head)
    main_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if main_task is not None:  # the matrix runner's timeout: stop the server first
        loop.add_signal_handler(signal.SIGTERM, main_task.cancel)
    try:
        await driver.setup()
        # the server's pid/process group and cgroup at once, so the runner can
        # kill it if this cell is killed before its finally (review 830f9ac B1, S2)
        body["server"] = {
            "pid": driver.proc.pid if driver.proc is not None else None,
            "pgid": driver.proc.pid if driver.proc is not None else None,
            "cgroup_dir": driver.cgroup_dir,
            "external": driver.external,
            "started_at": driver.server_started_at,
        }
        write_manifest(out_dir, body)
        patterns = list(ips.get("root_patterns", [])) if driver.external else []
        with Sampler(
            str(out_dir / "samples.jsonl"),
            root_pids=driver.root_pids(),
            patterns=patterns,
            interval_s=float(cfg.get("sample_interval_s", 1.0)),
            cgroup_dir=driver.cgroup_dir,
        ) as sampler:
            async with PoolAsyncClient(
                driver.url, timeout=BUDGET_MAX_S + 120.0
            ) as client:
                await work(client, sampler, body, lambda b: write_manifest(out_dir, b))
                try:
                    body["server_memory_after"] = await client.memory_status()
                except Exception:  # pylint: disable=broad-exception-caught
                    body["server_memory_after"] = None
        body["driver"] = driver.describe()
    except asyncio.CancelledError:
        body["outcome"] = "terminated"
        body["error"] = "SIGTERM (the matrix runner's cell timeout)"
    except Exception as exc:  # pylint: disable=broad-exception-caught
        body["outcome"] = "error"
        body["error"] = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        if main_task is not None:
            loop.remove_signal_handler(signal.SIGTERM)  # stop_all must not be cancelled
        await driver.stop_all()
        body["finished_at"] = time.time()
        write_manifest(out_dir, body)
    return body
