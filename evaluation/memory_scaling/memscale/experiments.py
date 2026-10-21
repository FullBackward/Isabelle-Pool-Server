"""E2, E3 and E4 (design §5) on top of the drivers:

- ``warm_hit`` (E2): on an IPS server with k loaded sessions, release each
  session and re-acquire with the same imports; the pool should hand the
  same session back warm. Latencies next to the cold starts of P1.
- ``pressure`` (E3): under a memory limit (IPS: its own scope with
  ``MemoryMax``; PIDE B: one process per session under the same scope limit
  set by the caller), start sessions and load the workload one by one until
  the tool refuses (IPS: 503 ``memory_pressure``, admission) or a process is
  killed (``memory.events oom_kill`` grows, a prover disappears, a tool call
  fails); then the time until the next session can be started again.
- ``kill_probe`` (E4): with k loaded sessions, kill the newest session's
  prover (the highest-pid ``poly`` ≥ 64 MB under the tool's tree) and ask
  the other sessions for an answer within 30 s.

Each writes ``<out>/<name>-<tool>-<heap>/manifest.json`` (+ samples.jsonl).
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from . import procs
from .phases import environment_block
from .sampler import Sampler
from .workload import Workload, with_theory_name

SESSION_POLY_MIN = 64 * 2**20


def _write(out: Path, body: Dict[str, Any]) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / "manifest.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=1, default=str)
    return path


# ------------------------------------------------------------------- E2
async def warm_hit(
    driver: Any, workload: Workload, k: int, out: Path, rounds: int = 3
) -> Dict[str, Any]:
    """E2 on IPS: k cold starts + loads, then ``rounds`` × k release/re-acquire
    cycles timed; records cold vs warm latencies and whether the same session
    came back."""
    body: Dict[str, Any] = {
        "experiment": "warm_hit",
        "tool": driver.name,
        "heap": workload.heap,
        "k": k,
        "cold_s": [],
        "warm": [],
        "environment": environment_block(None),
    }
    await driver.setup()
    try:
        for i in range(k):
            t0 = time.monotonic()
            await driver.start_session(i)
            body["cold_s"].append(round(time.monotonic() - t0, 3))
            await driver.load(
                i, with_theory_name(workload.text, f"{workload.theory_name}_{i}")
            )
        for r in range(rounds):
            for i in range(k):
                res = await driver.warm_hit(i)
                res.update({"round": r, "session": i})
                body["warm"].append(res)
    finally:
        await driver.stop_all()
    body["warm_s_p50"] = _median([w["warm_s"] for w in body["warm"]])
    body["cold_s_p50"] = _median(body["cold_s"])
    body["reused_all"] = all(w.get("reused") for w in body["warm"])
    _write(out, body)
    return body


def _median(xs: List[float]) -> Optional[float]:
    xs = sorted(xs)
    return None if not xs else xs[len(xs) // 2]


# ------------------------------------------------------------------- E3
async def pressure(  # pylint: disable=too-many-locals,too-many-statements,too-many-arguments,too-many-branches
    driver: Any,
    workload: Workload,
    out: Path,
    *,
    max_sessions: int = 64,
    patterns: Optional[List[str]] = None,
    cgroup_dir: Optional[str] = None,
    recover_wait_s: float = 300.0,
) -> Dict[str, Any]:
    """E3: add a session and load the workload, again and again, until the
    tool refuses or something dies; then wait for recovery and try once more.
    The sampler records the whole run (``samples.jsonl``) with the limit's
    cgroup when given."""
    out.mkdir(parents=True, exist_ok=True)
    sampler = Sampler(
        str(out / "samples.jsonl"), patterns=patterns or [], cgroup_dir=cgroup_dir
    )
    body: Dict[str, Any] = {
        "experiment": "pressure",
        "tool": driver.name,
        "topology": driver.topology,
        "heap": workload.heap,
        "steps": [],
        "environment": environment_block(None),
        "cgroup_dir": cgroup_dir,
        "outcome": "running",
    }
    events: List[Dict[str, Any]] = []
    body["events"] = events
    with sampler:
        sampler.phase("setup")
        await driver.setup()
        sampler.add_roots(driver.root_pids())
        tool_cg = getattr(driver, "cgroup_dir", None)
        # IPS's own scope (with MemoryMax) beats the caller's cgroup: the server
        # run of 2026-10-21 sampled the login session's cgroup (~98 MB) instead
        cgroup_dir = tool_cg or cgroup_dir or procs.own_cgroup_dir()
        sampler.cgroup_dir = cgroup_dir
        body["cgroup_dir"] = cgroup_dir
        body["driver"] = driver.describe()
        cg0 = procs.cgroup_memory(cgroup_dir)
        body["cgroup_at_start"] = cg0
        stop_reason = None
        i = 0
        task, loop = asyncio.current_task(), asyncio.get_running_loop()

        def _terminated() -> None:
            # systemd stops the WHOLE scope after a kernel OOM kill (OOMPolicy=stop):
            # the experiment got SIGTERM with its victim (server run 2026-10-21,
            # "Terminated", no manifest). Record it and let the cleanup write.
            body["outcome"] = "terminated"
            body["stop_reason"] = body.get("stop_reason") or (
                "SIGTERM: systemd stopped the scope (OOMPolicy=stop; "
                "the script passes OOMPolicy=continue on systemd >= 253)"
            )
            body["sessions_before_stop"] = sum(
                1 for s in body["steps"] if s.get("load_ok")
            )
            if task is not None:
                task.cancel()

        try:
            loop.add_signal_handler(signal.SIGTERM, _terminated)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
        try:
            while i < max_sessions and stop_reason is None:
                sampler.phase(f"add{i}")
                step: Dict[str, Any] = {"i": i, "t": time.time()}
                t0 = time.monotonic()
                try:
                    await driver.start_session(i)
                    step["start_s"] = round(time.monotonic() - t0, 3)
                    sampler.add_roots(driver.root_pids())
                    res = await driver.load(
                        i,
                        with_theory_name(workload.text, f"{workload.theory_name}_{i}"),
                    )
                    step["load_ok"] = bool(res.get("ok"))
                    step["load_error"] = res.get("error")
                    if (
                        res.get("status") is not None
                    ):  # refusal body on the load (facc4c9 S4)
                        step["http"], step["body"] = res.get("status"), res.get("body")
                    if not res.get("ok"):
                        stop_reason = f"load failed: {res.get('error')}"
                except (
                    httpx.HTTPStatusError
                ) as exc:  # IPS admission refusal (503) or a dead session (404)
                    code = (
                        exc.response.status_code if exc.response is not None else None
                    )
                    try:
                        detail = exc.response.json() if exc.response is not None else {}
                    except ValueError:
                        detail = {
                            "text": (
                                exc.response.text[:300]
                                if exc.response is not None
                                else ""
                            )
                        }
                    step["http"] = code
                    step["body"] = detail
                    stop_reason = f"http {code}: {detail.get('error') if isinstance(detail, dict) else detail}"
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    step["error"] = f"{type(exc).__name__}: {exc}"[:300]
                    stop_reason = step["error"]
                row = sampler.sample_once()
                step["host_used_mb"] = round((row["host"].get("used") or 0) / 2**20)
                step["cgroup_current_mb"] = round(
                    (row["cgroup"].get("current") or 0) / 2**20
                )
                step["oom_kill"] = row["cgroup"].get("oom_kill")
                step["session_polys"] = sum(
                    1
                    for p in row["procs"]
                    if p["role"] == "poly" and p["pss"] >= SESSION_POLY_MIN
                )
                body["steps"].append(step)
                cg = row["cgroup"]
                if (cg.get("oom_kill") or 0) > (cg0.get("oom_kill") or 0):
                    stop_reason = stop_reason or "oom_kill"
                    events.append({"t": time.time(), "oom_kill": cg.get("oom_kill")})
                i += 1
            body["stop_reason"] = stop_reason
            body["sessions_before_stop"] = i - 1 if stop_reason else i
            # recovery: how long until one more session can be started (IPS: the gate evicts idle sessions
            # — ours are leased, so release half first; PIDE: nothing frees memory, so only a kill ends it)
            sampler.phase("recover")
            t_stop = time.monotonic()
            release = getattr(driver, "release_half", None)
            if release is not None:
                await release()
            recovered: Optional[float] = None
            deadline = time.monotonic() + recover_wait_s
            while time.monotonic() < deadline:
                try:
                    await driver.start_session(i)
                    recovered = round(time.monotonic() - t_stop, 1)
                    break
                except Exception:  # pylint: disable=broad-exception-caught
                    await asyncio.sleep(5.0)
            body["recovery_s"] = recovered
            body["outcome"] = "ok"
        except asyncio.CancelledError:
            if body.get("outcome") != "terminated":
                raise  # somebody else's cancel
        except Exception as exc:  # pylint: disable=broad-exception-caught
            body["outcome"], body["error"] = (
                "error",
                f"{type(exc).__name__}: {exc}"[:300],
            )
        finally:
            try:
                loop.remove_signal_handler(signal.SIGTERM)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
            sampler.phase("stop")
            try:
                await driver.stop_all()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                body["stop_error"] = f"{type(exc).__name__}: {exc}"[:200]
            body["cgroup_at_end"] = procs.cgroup_memory(cgroup_dir)
            body["dmesg_oom"] = _dmesg_oom()
            _write(out, body)
    return body


def _dmesg_oom() -> List[str]:
    """Kernel OOM lines (needs permission; empty otherwise)."""
    try:
        import subprocess  # pylint: disable=import-outside-toplevel

        txt = subprocess.run(
            ["dmesg", "-T"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
        return [
            ln[:200]
            for ln in txt.splitlines()
            if "Out of memory" in ln or "oom-kill" in ln
        ][-20:]
    except (OSError, subprocess.SubprocessError):
        return []


# ------------------------------------------------------------------- E4
def newest_session_poly(roots: List[int]) -> Optional[int]:
    """The highest-pid session ``poly`` under the tool's tree (the last session started)."""
    rows = procs.snapshot(roots)
    polys = [r.pid for r in rows if r.role == "poly" and r.pss >= SESSION_POLY_MIN]
    return max(polys) if polys else None


async def kill_probe(
    driver: Any, workload: Workload, k: int, out: Path, *, answer_s: float = 30.0
) -> Dict[str, Any]:
    """E4: k loaded sessions, kill the newest session's prover, then ask every
    other session for an answer (``driver.probe(i)`` → ok/False) within
    ``answer_s``; also whether the tool's processes survived."""
    body: Dict[str, Any] = {
        "experiment": "kill_probe",
        "tool": driver.name,
        "topology": driver.topology,
        "heap": workload.heap,
        "k": k,
        "environment": environment_block(None),
    }
    await driver.setup()
    try:
        for i in range(k):
            await driver.start_session(i)
            await driver.load(
                i, with_theory_name(workload.text, f"{workload.theory_name}_{i}")
            )
        victim = newest_session_poly(driver.root_pids())
        body["victim_pid"] = victim
        if victim is None:
            raise RuntimeError("no session poly found under the tool's tree")
        os.kill(victim, signal.SIGKILL)
        body["killed_at"] = time.time()
        await asyncio.sleep(2.0)
        answers: Dict[str, Any] = {}
        for i in range(k):
            t0 = time.monotonic()
            try:
                ok = await asyncio.wait_for(driver.probe(i), timeout=answer_s)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                ok = f"{type(exc).__name__}: {exc}"[:120]
            answers[str(i)] = {"ok": ok, "s": round(time.monotonic() - t0, 2)}
            detail = getattr(driver, "last_probe", None)
            if ok is not True and detail:  # why: the probe's own load result
                answers[str(i)]["detail"] = detail
        body["answers"] = answers
        body["others_answered"] = (
            sum(1 for i, a in answers.items() if a["ok"] is True) >= k - 1
        )
        body["processes_alive"] = [
            procs.read_stat(p) is not None for p in driver.root_pids()
        ]
        body["outcome"] = "ok"
    except Exception as exc:  # pylint: disable=broad-exception-caught
        body["outcome"], body["error"] = "error", f"{type(exc).__name__}: {exc}"[:300]
    finally:
        await driver.stop_all()
        _write(out, body)
    return body
