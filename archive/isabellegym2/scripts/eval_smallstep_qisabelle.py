from __future__ import annotations

import argparse
import json
import platform
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests

from evaluation.scripts.eval_stats import (
    preview,
    safe_mean,
    safe_median,
    summarize_metric,
)
from evaluation.scripts.theory_splitter import (
    HEADER_RE,
    END_RE,
    determine_theory_name,
    extract_imports,
    split_commands,
    split_theory,
)

PREBEGIN_KEYWORDS_RE = re.compile(r"(?ms)^\s*keywords\b")


@dataclass
class StepResult:
    step_kind: str
    command: str
    preview: str
    accepted: bool
    elapsed_sec: float
    proof_done: Optional[bool]
    error: Optional[str] = None
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
    startup_error: Optional[str] = None
    steps: list[StepResult] = field(default_factory=list)


class QIsabelleServerError(RuntimeError):
    pass


class SimpleQIsabelleSession:
    def __init__(
        self,
        *,
        session_name: str,
        session_roots: list[str],
        port: int,
        per_transition_timeout: float,
        execute_timeout: float,
        debug: bool,
        working_dir: str = "/home/isabelle/",
    ) -> None:
        self.port = port
        self.debug = debug
        self.base_url = f"http://localhost:{port}"
        self.sledgehammer_ready: Optional[bool] = None
        t0 = time.perf_counter()
        result = self._post(
            "/openIsabelleSession",
            {
                "sessionName": session_name,
                "sessionRoots": session_roots,
                "workingDir": working_dir,
                "perTransitionTimeoutSeconds": per_transition_timeout,
                "executeTimeoutSeconds": execute_timeout,
            },
        )
        self.open_elapsed_sec = time.perf_counter() - t0
        if result.get("success") != "success":
            raise QIsabelleServerError(f"Unexpected openIsabelleSession response: {result}")
        self.sledgehammer_ready = result.get("sledgehammerReady")

    def _post(self, path: str, json_data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if self.debug:
            print(f"Request to {self.base_url}{path} with {json_data}")
        response = requests.post(f"{self.base_url}{path}", json=json_data or {})
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise RuntimeError(f"Expected JSON object from {path}, got: {result!r}")
        if "error" in result:
            msg = str(result["error"])
            tb = result.get("traceback")
            if tb:
                msg += "\nTraceback:\n" + str(tb)
            raise QIsabelleServerError(msg)
        return result

    def close(self) -> None:
        result = self._post("/closeIsabelleSession")
        if result.get("success") != "Closed":
            raise QIsabelleServerError(f"Unexpected closeIsabelleSession response: {result}")

    def new_theory(
        self,
        *,
        theory_name: str,
        new_state_name: str,
        imports: list[str],
        master_dir: str,
        only_import_from_session_heap: bool,
    ) -> None:
        result = self._post(
            "/newTheory",
            {
                "theoryName": theory_name,
                "newStateName": new_state_name,
                "imports": imports,
                "masterDir": master_dir,
                "onlyImportFromSessionHeap": only_import_from_session_heap,
            },
        )
        if result.get("success") != "success":
            raise QIsabelleServerError(f"Unexpected newTheory response: {result}")

    def execute(self, state_name: str, isar_code: str, new_state_name: str) -> tuple[bool, str]:
        result = self._post(
            "/execute",
            {"stateName": state_name, "isarCode": isar_code, "newStateName": new_state_name},
        )
        return bool(result["proofDone"]), str(result["proofGoals"])



def header_requires_features_not_supported_by_new_theory(header: str) -> Optional[str]:
    if PREBEGIN_KEYWORDS_RE.search(header):
        return "qIsabelle newTheory() cannot reproduce header `keywords` declarations before `begin`"
    return None


def run_theory(
    session: SimpleQIsabelleSession,
    thy_file: Path,
    *,
    startup_sec: float,
    master_dir: str,
    only_import_from_session_heap: bool,
    print_steps: bool,
) -> TheoryResult:
    theory_wall_t0 = time.perf_counter()
    text = thy_file.read_text(encoding="utf-8")
    theory_name = determine_theory_name(thy_file, text)
    imports = extract_imports(text)
    step_results: list[StepResult] = []
    startup_error: Optional[str] = None
    completed_all_commands = False
    reached_end_command = False
    warnings_ignored = 0

    try:
        header, body, end_kw = split_theory(text)
        unsupported = header_requires_features_not_supported_by_new_theory(header)
        if unsupported:
            raise ValueError(unsupported)
        commands = split_commands(body)
        command_stream = [("body", c + "\n") for c in commands] + [("end", end_kw)]

        session.new_theory(
            theory_name=theory_name,
            new_state_name=f"{theory_name}_0",
            imports=imports,
            master_dir=master_dir,
            only_import_from_session_heap=only_import_from_session_heap,
        )

        current_state = f"{theory_name}_0"
        next_idx = 1
        for idx, (kind, command) in enumerate(command_stream):
            if kind == "end":
                reached_end_command = True
            next_state = f"{theory_name}_{next_idx}"
            next_idx += 1
            t1 = time.perf_counter()
            status_code = 200
            err = None
            proof_done: Optional[bool] = None
            exc_cls = None
            try:
                proof_done, _goals = session.execute(current_state, command, next_state)
                accepted = True
                current_state = next_state
            except requests.HTTPError as exc:
                accepted = False
                exc_cls = exc.__class__.__name__
                status_code = exc.response.status_code if exc.response is not None else None
                try:
                    err = exc.response.text
                except Exception:
                    err = str(exc)
            except Exception as exc:  # noqa: BLE001
                accepted = False
                err = str(exc)
                exc_cls = exc.__class__.__name__
                status_code = None

            elapsed = time.perf_counter() - t1
            step_results.append(
                StepResult(
                    step_kind=kind,
                    command=command.rstrip("\n"),
                    preview=preview(command),
                    accepted=accepted,
                    elapsed_sec=elapsed,
                    proof_done=proof_done,
                    error=err,
                    response_status_code=status_code,
                    exception_class=exc_cls,
                    step_index=idx,
                )
            )
            if print_steps:
                msg = f"[{theory_name}] step={idx} kind={kind} accepted={accepted} preview={preview(command)}"
                print(msg)
                if err:
                    print(f"  command_error={err.splitlines()[0]}")
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
                    proof_done=None,
                    error=str(exc),
                    response_status_code=None,
                    exception_class=exc.__class__.__name__,
                    step_index=-1,
                )
            )

    ok = completed_all_commands and all(s.accepted for s in step_results)
    theory_wall = time.perf_counter() - theory_wall_t0
    last = step_results[-1] if step_results else None

    return TheoryResult(
        file=str(thy_file),
        theory_name=theory_name,
        imports=imports,
        startup_sec=startup_sec,
        wall_time_sec=theory_wall,
        api_execution_time_sec=None,
        client_overhead_sec=None,
        ok=ok,
        total_steps=len(step_results),
        accepted_steps=sum(1 for s in step_results if s.accepted),
        expected_steps=(len(split_commands(split_theory(text)[1])) + 1) if HEADER_RE.search(text.strip()) and END_RE.search(text.strip()) else 0,
        completed_all_commands=completed_all_commands,
        reached_end_command=reached_end_command,
        warnings_ignored=warnings_ignored,
        last_recorded_step_kind=(last.step_kind if last else None),
        last_recorded_preview=(last.preview if last else None),
        startup_error=startup_error,
        steps=step_results,
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Stepwise qIsabelle benchmark with lexical command splitting and server-client-like stats."
    )
    ap.add_argument("--qisabelle-root", required=False, type=Path, default=None, help="Unused; kept for CLI compatibility")
    ap.add_argument("--corpus", required=True, type=Path)
    ap.add_argument("--session-name", default="HOL")
    ap.add_argument("--session-root", action="append", default=[])
    ap.add_argument("--master-dir", default="/home/isabelle/", help="Server-visible absolute Linux path")
    ap.add_argument("--port", type=int, default=17000)
    ap.add_argument("--per-transition-timeout", type=float, default=10.0)
    ap.add_argument("--execute-timeout", type=float, default=0.0)
    ap.add_argument(
        "--only-import-from-session-heap",
        action="store_true",
        help="Restrict imports to the opened heap only. Leave off for HOL-Examples.",
    )
    ap.add_argument("--print-steps", action="store_true")
    ap.add_argument("--debug-http", action="store_true")
    ap.add_argument("--output", type=Path, default=Path("smallstep_qisabelle_results.json"))
    args = ap.parse_args()

    files = sorted(args.corpus.glob("*.thy"))
    if not files:
        raise FileNotFoundError(f"No .thy files found in {args.corpus}")

    session_roots = [str(Path(p)) for p in args.session_root]
    master_dir = str(args.master_dir)

    batch_wall_t0 = time.perf_counter()
    batch_python_cpu_t0 = time.process_time()

    session = SimpleQIsabelleSession(
        session_name=args.session_name,
        session_roots=session_roots,
        port=args.port,
        per_transition_timeout=args.per_transition_timeout,
        execute_timeout=args.execute_timeout,
        debug=args.debug_http,
        working_dir="/home/isabelle/",
    )
    startup_sec = session.open_elapsed_sec

    results: list[TheoryResult] = []
    try:
        for thy_file in files:
            results.append(
                run_theory(
                    session,
                    thy_file,
                    startup_sec=startup_sec,
                    master_dir=master_dir,
                    only_import_from_session_heap=args.only_import_from_session_heap,
                    print_steps=args.print_steps,
                )
            )
    finally:
        session.close()

    total_wall_time_sec = time.perf_counter() - batch_wall_t0
    total_python_cpu_time_sec = time.process_time() - batch_python_cpu_t0

    startup_times = [r.startup_sec for r in results]
    theory_wall_times = [r.wall_time_sec for r in results]
    step_elapsed_times = [s.elapsed_sec for r in results for s in r.steps]
    steps_per_theory = [float(r.total_steps) for r in results]
    accepted_steps_per_theory = [float(r.accepted_steps) for r in results]
    api_exec_times = [r.api_execution_time_sec for r in results if r.api_execution_time_sec is not None]
    client_overheads = [r.client_overhead_sec for r in results if r.client_overhead_sec is not None]

    summary = {
        "tool": "qisabelle",
        "benchmark_kind": "local_qisabelle_smallstep_verification_benchmark_stepwise_single_session",
        "server": f"http://localhost:{args.port}",
        "session_name": args.session_name,
        "session_roots": session_roots,
        "master_dir": master_dir,
        "only_import_from_session_heap": args.only_import_from_session_heap,
        "per_transition_timeout_sec": args.per_transition_timeout,
        "execute_timeout_sec": args.execute_timeout,
        "sledgehammer_ready": session.sledgehammer_ready,
        "corpus": str(args.corpus),
        "total_files": len(results),
        "successes": sum(1 for r in results if r.ok),
        "failures": sum(1 for r in results if not r.ok),
        "theories_attempted": len(results),
        "theories_completed": sum(1 for r in results if r.ok),
        "total_wall_time_sec": total_wall_time_sec,
        "total_python_cpu_time_sec": total_python_cpu_time_sec,
        "total_startup_time_sec": sum(startup_times),
        "qisabelle_session_open_time_sec": startup_sec,
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
            "client": "SimpleQIsabelleSession(requests)",
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
    main()
