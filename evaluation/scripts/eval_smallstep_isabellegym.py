from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from evaluation.scripts.eval_stats import (
    is_warning_message,
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


@dataclass
class StepResult:
    step_kind: str
    command: str
    preview: str
    accepted: bool
    elapsed_sec: float
    open_subgoals: int
    subgoals: list[str] = field(default_factory=list)
    error: Optional[str] = None
    warning: Optional[str] = None
    warning_ignored: bool = False
    subgoal_error: Optional[str] = None
    exception_class: Optional[str] = None
    step_index: int = -1


@dataclass
class TheoryResult:
    file: str
    theory_name: str
    startup_sec: float
    wall_time_sec: float
    ok: bool
    total_steps: int
    accepted_steps: int
    expected_steps: int
    completed_all_commands: bool
    reached_end_command: bool
    end_command_accepted: Optional[bool]
    had_subgoal_timeout: bool
    warnings_ignored: int
    last_recorded_step_kind: Optional[str]
    last_recorded_preview: Optional[str]
    startup_error: Optional[str] = None
    steps: list[StepResult] = field(default_factory=list)


def safe_open_subgoals(gym) -> tuple[list[str], Optional[str]]:
    try:
        return list(gym.open_subgoals()), None
    except Exception as exc:  # noqa: BLE001
        return [], f"{exc.__class__.__name__}: {exc}"


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Local IsabelleGym evaluator aligned with the server small-step evaluator."
    )
    ap.add_argument("--repo-root", required=True, type=Path)
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--output", type=Path, default=Path("smallstep_isabellegym_aligned.json"))
    ap.add_argument("--print-steps", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, str(args.repo_root))
    from evaluation.local_gym.isabelle_gym import IsabelleGym
    from server.app.services.success_checker import get_error_message, is_syntax_successful

    files = sorted(args.corpus.glob("*.thy"))
    if not files:
        raise FileNotFoundError(f"No .thy files found in {args.corpus}")

    results: list[TheoryResult] = []
    batch_wall_t0 = time.perf_counter()
    batch_python_cpu_t0 = time.process_time()

    for thy_file in files:
        text = thy_file.read_text(encoding="utf-8")
        theory_name = determine_theory_name(thy_file, text)
        header, body, end_kw = split_theory(text)
        commands = split_commands(body)
        command_stream = [("header", header)] + [("body", c + "\n") for c in commands] + [("end", end_kw)]
        imports = extract_imports(text)
        theory_wall_t0 = time.perf_counter()
        step_results: list[StepResult] = []
        completed_all_commands = False
        reached_end_command = False
        end_command_accepted: Optional[bool] = None
        had_subgoal_timeout = False
        warnings_ignored = 0
        gym = None
        startup = 0.0
        startup_error = None

        try:
            t0 = time.perf_counter()
            gym = IsabelleGym(show_states=False, initial_thys = imports)
            gym.enter_thy(theory_name)
            startup = time.perf_counter() - t0

            for idx, (kind, command) in enumerate(command_stream):
                if kind == "end":
                    reached_end_command = True

                t1 = time.perf_counter()
                warning = None
                warning_ignored = False

                try:
                    repl_result = gym.step(command)
                    elapsed = time.perf_counter() - t1
                    accepted = bool(is_syntax_successful(repl_result))
                    raw_error = None if accepted else get_error_message(repl_result)
                    exc_cls = None

                    # Keep warning-only behaviour aligned with the server evaluator.
                    if not accepted and is_warning_message(raw_error):
                        accepted = True
                        warning = raw_error
                        warning_ignored = True
                        raw_error = None
                        warnings_ignored += 1

                    err = raw_error
                except Exception as exc:  # noqa: BLE001
                    elapsed = time.perf_counter() - t1
                    accepted = False
                    err = str(exc)
                    exc_cls = exc.__class__.__name__

                # Keep the local evaluator's "no open_subgoals after end" behaviour,
                # which avoids false negatives after the theory context is closed.
                if kind == "end":
                    subgoals = []
                    subgoal_error = None
                else:
                    subgoals, subgoal_error = safe_open_subgoals(gym) if gym is not None else ([], None)
                    if subgoal_error:
                        had_subgoal_timeout = True

                if kind == "end":
                    end_command_accepted = accepted

                step_results.append(
                    StepResult(
                        step_kind=kind,
                        command=command.rstrip("\n"),
                        preview=preview(command),
                        accepted=accepted,
                        elapsed_sec=elapsed,
                        open_subgoals=len(subgoals),
                        subgoals=subgoals,
                        error=err,
                        warning=warning,
                        warning_ignored=warning_ignored,
                        subgoal_error=subgoal_error,
                        exception_class=exc_cls,
                        step_index=idx,
                    )
                )

                if args.print_steps:
                    msg = f"[{theory_name}] step={idx} kind={kind} accepted={accepted} preview={preview(command)}"
                    if warning_ignored:
                        msg += " [warning ignored]"
                    if kind == "end":
                        msg += " [skipped open_subgoals after end]"
                    elif subgoal_error:
                        msg += " [subgoals unavailable]"
                    print(msg)
                    if err:
                        print(f"  command_error={err.splitlines()[0]}")
                    if warning:
                        print(f"  warning={warning.splitlines()[0]}")
                    if kind != "end" and subgoal_error:
                        print(f"  subgoal_error={subgoal_error.splitlines()[0]}")
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
                        open_subgoals=0,
                        subgoals=[],
                        error=str(exc),
                        warning=None,
                        warning_ignored=False,
                        subgoal_error=None,
                        exception_class=exc.__class__.__name__,
                        step_index=-1,
                    )
                )
        finally:
            try:
                if gym is not None:
                    gym.close()
            except Exception:
                pass

        ok = completed_all_commands and all(s.accepted for s in step_results)
        last = step_results[-1] if step_results else None
        theory_wall = time.perf_counter() - theory_wall_t0

        results.append(
            TheoryResult(
                file=str(thy_file),
                theory_name=theory_name,
                startup_sec=startup,
                wall_time_sec=theory_wall,
                ok=ok,
                total_steps=len(step_results),
                accepted_steps=sum(1 for s in step_results if s.accepted),
                expected_steps=len(command_stream),
                completed_all_commands=completed_all_commands,
                reached_end_command=reached_end_command,
                end_command_accepted=end_command_accepted,
                had_subgoal_timeout=had_subgoal_timeout,
                warnings_ignored=warnings_ignored,
                last_recorded_step_kind=(last.step_kind if last else None),
                last_recorded_preview=(last.preview if last else None),
                startup_error=startup_error,
                steps=step_results,
            )
        )

    total_wall_time_sec = time.perf_counter() - batch_wall_t0
    total_python_cpu_time_sec = time.process_time() - batch_python_cpu_t0

    startup_times = [r.startup_sec for r in results]
    theory_wall_times = [r.wall_time_sec for r in results]
    step_elapsed_times = [s.elapsed_sec for r in results for s in r.steps]
    steps_per_theory = [float(r.total_steps) for r in results]
    accepted_steps_per_theory = [float(r.accepted_steps) for r in results]

    payload = {
        "tool": "local-isabellegym",
        "benchmark_kind": "local_smallstep_verification_benchmark_aligned_server_split_and_acceptance",
        "corpus": str(args.corpus),
        "repo_root": str(args.repo_root),
        "total_files": len(results),
        "successes": sum(1 for r in results if r.ok),
        "failures": sum(1 for r in results if not r.ok),
        "total_wall_time_sec": total_wall_time_sec,
        "total_python_cpu_time_sec": total_python_cpu_time_sec,
        "total_startup_time_sec": sum(startup_times),
        "total_theory_wall_time_sec": sum(theory_wall_times),
        "total_step_elapsed_time_sec": sum(step_elapsed_times),
        "files_per_minute": (len(results) / total_wall_time_sec * 60.0) if total_wall_time_sec > 0 else None,
        "mean_wall_time_sec_per_file": safe_mean(theory_wall_times),
        "median_wall_time_sec_per_file": safe_median(theory_wall_times),
        "mean_startup_sec_per_file": safe_mean(startup_times),
        "median_startup_sec_per_file": safe_median(startup_times),
        "mean_step_elapsed_sec": safe_mean(step_elapsed_times),
        "median_step_elapsed_sec": safe_median(step_elapsed_times),
        "startup_time_stats_sec": summarize_metric(startup_times),
        "theory_wall_time_stats_sec": summarize_metric(theory_wall_times),
        "step_elapsed_time_stats_sec": summarize_metric(step_elapsed_times),
        "steps_per_theory_stats": summarize_metric(steps_per_theory),
        "accepted_steps_per_theory_stats": summarize_metric(accepted_steps_per_theory),
        "environment": {
            "python_version": sys.version,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
        "results": [
            {
                **asdict(r),
                "steps": [asdict(s) for s in r.steps],
            }
            for r in results
        ],
    }

    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "total_files": payload["total_files"],
        "successes": payload["successes"],
        "failures": payload["failures"],
        "theories_with_ignored_warnings": {r.theory_name: r.warnings_ignored for r in results if r.warnings_ignored > 0},
        "theories_with_subgoal_timeouts_before_end": [r.theory_name for r in results if r.had_subgoal_timeout],
        "theories_with_successful_end_command": [r.theory_name for r in results if r.end_command_accepted],
    }, indent=2))


if __name__ == "__main__":
    main()
