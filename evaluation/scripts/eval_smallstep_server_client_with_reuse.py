from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import httpx
from client.async_client import PoolAsyncClient
from evaluation.scripts.eval_stats import (
    is_warning_message,
    normalize_execution_time,
    preview,
    safe_mean,
    safe_median,
    summarize_metric,
)
from evaluation.scripts.theory_splitter import (
    determine_theory_name,
    extract_imports,
    split_commands,
    split_theory,
)

DEFAULT_NUM_WORKERS = 8


@dataclass
class StepResult:
    step_kind: str
    command: str
    preview: str
    accepted: bool
    elapsed_sec: float
    execution_time_sec: Optional[float]
    open_subgoals: int
    subgoals: list[str] = field(default_factory=list)
    error: Optional[str] = None
    warning: Optional[str] = None
    warning_ignored: bool = False
    response_status_code: Optional[int] = None
    exception_class: Optional[str] = None
    step_index: int = -1


@dataclass
class TheoryResult:
    file: str
    theory_name: str
    imports: list[str]
    startup_sec: float
    wall_time_sec: float
    api_execution_time_sec: Optional[float]
    client_overhead_sec: Optional[float]
    ok: bool
    total_steps: int
    accepted_steps: int
    expected_steps: int
    completed_all_commands: bool
    reached_end_command: bool
    warnings_ignored: int
    last_recorded_step_kind: Optional[str]
    last_recorded_preview: Optional[str]
    worker_id: int = -1
    reused_session: bool = False
    startup_error: Optional[str] = None
    steps: list[StepResult] = field(default_factory=list)


async def run_theory(
    server_url: str,
    thy_file: Path,
    *,
    field: str,
    timeout: float,
    print_steps: bool,
    worker_id: int,
) -> TheoryResult:
    """Run a single theory file using its own client and an acquired session."""
    text = thy_file.read_text(encoding="utf-8")
    theory_name = determine_theory_name(thy_file, text)
    imports = extract_imports(text)
    header, body, end_kw = split_theory(text)
    commands = split_commands(body)
    command_stream = [("header", header)] + [("body", c + "\n") for c in commands] + [("end", end_kw)]

    theory_wall_t0 = time.perf_counter()
    step_results: list[StepResult] = []
    completed_all_commands = False
    reached_end_command = False
    warnings_ignored = 0
    startup = 0.0
    startup_error = None
    api_execution_total = 0.0
    saw_api_execution = False
    session_id: Optional[str] = None
    reused_session = False

    async with PoolAsyncClient(base_url=server_url, timeout=timeout) as client:
        try:
            t0 = time.perf_counter()
            session_payload = await client.acquire_session(
                theories=imports,
                field=field,
                reuse_dirty=True,
            )
            session_id = session_payload["session_id"]
            reused_session = session_payload.get("reused", False)
            lease_id = session_payload.get("lease_id")
            await client.enter_theory(session_id, theory_name, lease_id=lease_id)
            startup = time.perf_counter() - t0

            if print_steps:
                print(
                    f"[worker={worker_id}] [{theory_name}] session={session_id} "
                    f"reused={reused_session}"
                )

            for idx, (kind, command) in enumerate(command_stream):
                if kind == "end":
                    reached_end_command = True

                t1 = time.perf_counter()
                status_code = 200
                warning = None
                warning_ignored = False

                try:
                    data = await client.execute_command(session_id, command, timeout=timeout, lease_id=lease_id)
                    elapsed = time.perf_counter() - t1

                    execution_time = normalize_execution_time(data.get("execution_time"))
                    if execution_time is not None:
                        api_execution_total += execution_time
                        saw_api_execution = True

                    accepted = bool(data.get("success", False))
                    raw_error = data.get("error")
                    subgoals = list(data.get("subgoals", []) or [])

                    if not accepted and is_warning_message(raw_error):
                        accepted = True
                        warning = raw_error
                        warning_ignored = True
                        raw_error = None
                        warnings_ignored += 1

                    err = raw_error
                    exc_cls = None

                except httpx.HTTPStatusError as exc:
                    elapsed = time.perf_counter() - t1
                    accepted = False
                    err = exc.response.text
                    subgoals = []
                    execution_time = None
                    exc_cls = exc.__class__.__name__
                    status_code = exc.response.status_code
                except Exception as exc:  # noqa: BLE001
                    elapsed = time.perf_counter() - t1
                    accepted = False
                    err = str(exc)
                    subgoals = []
                    execution_time = None
                    exc_cls = exc.__class__.__name__
                    status_code = None

                step_results.append(
                    StepResult(
                        step_kind=kind,
                        command=command.rstrip("\n"),
                        preview=preview(command),
                        accepted=accepted,
                        elapsed_sec=elapsed,
                        execution_time_sec=execution_time,
                        open_subgoals=len(subgoals),
                        subgoals=subgoals,
                        error=err,
                        warning=warning,
                        warning_ignored=warning_ignored,
                        response_status_code=status_code,
                        exception_class=exc_cls,
                        step_index=idx,
                    )
                )

                if print_steps:
                    msg = (
                        f"[worker={worker_id}] [{theory_name}] step={idx} kind={kind} "
                        f"accepted={accepted} preview={preview(command)}"
                    )
                    if warning_ignored:
                        msg += " [warning ignored]"
                    print(msg)
                    if err:
                        print(f"  command_error={err.splitlines()[0]}")
                    if warning:
                        print(f"  warning={warning.splitlines()[0]}")
                    if subgoals:
                        print(f"  open_subgoals={len(subgoals)}")

                if not accepted:
                    break
                else:
                    completed_all_commands = True

        except Exception as exc:  # noqa: BLE001
            startup_error = str(exc)
            if not step_results:
                step_results.append(
                    StepResult(
                        step_kind="startup",
                        command=theory_name,
                        preview=preview(theory_name),
                        accepted=False,
                        elapsed_sec=0.0,
                        execution_time_sec=None,
                        open_subgoals=0,
                        subgoals=[],
                        error=str(exc),
                        warning=None,
                        warning_ignored=False,
                        response_status_code=None,
                        exception_class=exc.__class__.__name__,
                        step_index=-1,
                    )
                )
        finally:
            if session_id is not None:
                try:
                    await client.release_session(session_id, lease_id=lease_id)
                except Exception:
                    pass

    ok = completed_all_commands and all(s.accepted for s in step_results)
    theory_wall = time.perf_counter() - theory_wall_t0
    api_execution_time_sec = api_execution_total if saw_api_execution else None
    client_overhead_sec = (
        max(0.0, theory_wall - api_execution_total)
        if saw_api_execution
        else None
    )
    last = step_results[-1] if step_results else None

    return TheoryResult(
        file=str(thy_file),
        theory_name=theory_name,
        imports=imports,
        startup_sec=startup,
        wall_time_sec=theory_wall,
        api_execution_time_sec=api_execution_time_sec,
        client_overhead_sec=client_overhead_sec,
        ok=ok,
        total_steps=len(step_results),
        accepted_steps=sum(1 for s in step_results if s.accepted),
        expected_steps=len(command_stream),
        completed_all_commands=completed_all_commands,
        reached_end_command=reached_end_command,
        warnings_ignored=warnings_ignored,
        last_recorded_step_kind=(last.step_kind if last else None),
        last_recorded_preview=(last.preview if last else None),
        worker_id=worker_id,
        reused_session=reused_session,
        startup_error=startup_error,
        steps=step_results,
    )


async def worker(
    worker_id: int,
    queue: asyncio.Queue[Path],
    server_url: str,
    field: str,
    timeout: float,
    print_steps: bool,
    results: list[TheoryResult],
    results_lock: asyncio.Lock,
) -> None:
    """Worker coroutine that pulls theory files from the queue and processes them."""
    while True:
        try:
            thy_file = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        if print_steps:
            print(f"[worker={worker_id}] Starting {thy_file.name}")

        result = await run_theory(
            server_url,
            thy_file,
            field=field,
            timeout=timeout,
            print_steps=print_steps,
            worker_id=worker_id,
        )

        async with results_lock:
            results.append(result)

        status = "OK" if result.ok else "FAIL"
        print(
            f"[worker={worker_id}] Finished {result.theory_name}: {status} "
            f"({result.accepted_steps}/{result.expected_steps} steps, "
            f"{result.wall_time_sec:.1f}s)"
        )

        queue.task_done()


async def amain() -> None:
    ap = argparse.ArgumentParser(
        description="Stepwise benchmark via the async client with parallel workers using acquire_session."
    )
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--server", default="http://localhost:8000")
    ap.add_argument("--timeout", type=int, default=1200)
    ap.add_argument("--field", default="HOL")
    ap.add_argument("--num-workers", type=int, default=DEFAULT_NUM_WORKERS,
                     help=f"Number of parallel workers (default: {DEFAULT_NUM_WORKERS})")
    ap.add_argument("--output", type=Path, default=Path("smallstep_server_client_stepwise.json"))
    ap.add_argument("--print-steps", action="store_true",
                     help="Print each command, acceptance, and any ignored warning")
    args = ap.parse_args()

    files = sorted(args.corpus.glob("*.thy"))
    if not files:
        raise FileNotFoundError(f"No .thy files found in {args.corpus}")

    num_workers = min(args.num_workers, len(files))
    print(
        f"Running {len(files)} theory files with {num_workers} parallel workers "
        f"against {args.server}"
    )

    # Fill the work queue
    queue: asyncio.Queue[Path] = asyncio.Queue()
    for f in files:
        queue.put_nowait(f)

    results: list[TheoryResult] = []
    results_lock = asyncio.Lock()

    batch_wall_t0 = time.perf_counter()
    batch_python_cpu_t0 = time.process_time()

    # Launch workers
    tasks = [
        asyncio.create_task(
            worker(
                worker_id=i,
                queue=queue,
                server_url=args.server,
                field=args.field,
                timeout=float(args.timeout),
                print_steps=args.print_steps,
                results=results,
                results_lock=results_lock,
            )
        )
        for i in range(num_workers)
    ]

    await asyncio.gather(*tasks)

    total_wall_time_sec = time.perf_counter() - batch_wall_t0
    total_python_cpu_time_sec = time.process_time() - batch_python_cpu_t0

    # Sort results by filename for deterministic output
    results.sort(key=lambda r: r.file)

    startup_times = [r.startup_sec for r in results]
    theory_wall_times = [r.wall_time_sec for r in results]
    step_elapsed_times = [s.elapsed_sec for r in results for s in r.steps]
    steps_per_theory = [float(r.total_steps) for r in results]
    accepted_steps_per_theory = [float(r.accepted_steps) for r in results]
    api_exec_times = [r.api_execution_time_sec for r in results if r.api_execution_time_sec is not None]
    client_overheads = [r.client_overhead_sec for r in results if r.client_overhead_sec is not None]

    summary = {
        "tool": "isabelle_pool_server-async-client-smallstep",
        "benchmark_kind": f"server_smallstep_verification_benchmark_stepwise_client_{num_workers}_workers_acquire",
        "server": args.server,
        "field": args.field,
        "timeout_sec": args.timeout,
        "num_workers": num_workers,
        "corpus": str(args.corpus),
        "total_files": len(results),
        "successes": sum(1 for r in results if r.ok),
        "failures": sum(1 for r in results if not r.ok),
        "total_wall_time_sec": total_wall_time_sec,
        "total_python_cpu_time_sec": total_python_cpu_time_sec,
        "total_startup_time_sec": sum(startup_times),
        "total_theory_wall_time_sec": sum(theory_wall_times),
        "total_step_elapsed_time_sec": sum(step_elapsed_times),
        "total_api_execution_time_sec": sum(api_exec_times) if api_exec_times else None,
        "total_client_overhead_sec": sum(client_overheads) if client_overheads else None,
        "files_per_minute": (len(results) / total_wall_time_sec * 60.0) if total_wall_time_sec > 0 else None,
        "mean_wall_time_sec_per_file": safe_mean(theory_wall_times),
        "median_wall_time_sec_per_file": safe_median(theory_wall_times),
        "mean_startup_sec_per_file": safe_mean(startup_times),
        "median_startup_sec_per_file": safe_median(startup_times),
        "mean_step_elapsed_sec": safe_mean(step_elapsed_times),
        "median_step_elapsed_sec": safe_median(step_elapsed_times),
        "mean_api_execution_time_sec_per_file": safe_mean(api_exec_times),
        "median_api_execution_time_sec_per_file": safe_median(api_exec_times),
        "mean_client_overhead_sec_per_file": safe_mean(client_overheads),
        "median_client_overhead_sec_per_file": safe_median(client_overheads),
        "startup_time_stats_sec": summarize_metric(startup_times),
        "theory_wall_time_stats_sec": summarize_metric(theory_wall_times),
        "step_elapsed_time_stats_sec": summarize_metric(step_elapsed_times),
        "steps_per_theory_stats": summarize_metric(steps_per_theory),
        "accepted_steps_per_theory_stats": summarize_metric(accepted_steps_per_theory),
        "api_execution_time_stats_sec": summarize_metric(api_exec_times),
        "client_overhead_stats_sec": summarize_metric(client_overheads),
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "client": "PoolAsyncClient",
            "num_workers": num_workers,
        },
        "results": [
            {
                **asdict(r),
                "steps": [asdict(s) for s in r.steps],
            }
            for r in results
        ],
    }

    args.output.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    compact = {k: v for k, v in summary.items() if k != "results"}
    compact["theories_with_ignored_warnings"] = {r.theory_name: r.warnings_ignored for r in results if r.warnings_ignored > 0}
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    asyncio.run(amain())
