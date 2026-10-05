#!/usr/bin/env python3
"""Isabelle Pool Server LSP-MCP comparison runner (file-sync workflow).

Drives the LSP-like MCP server (mcp_servers.lsp) through the same
OpenAI-compatible agent loop, problems, and neutral arbiter as the other
runners — the cleanest possible A/B against the chunk-centric MCP.

Difference in shape: the LSP MCP has NO file-editing tools by design — the
agent edits the workdir copy of the problem .thy on disk with the LOCAL file
tools below (read_file/write_file, sandboxed to the attempt workdir), and the
MCP observes the edit via its disk→session sync on the next query. The verdict
is the arbiter on the final file state, as for every runner.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import time as time_mod
from pathlib import Path

# Allow importing from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.arbiter import check
from common.config import load
from common.metrics import AttemptResult, Timer, TokenAggregator, append_result
from common.problems import load_problems, sanitize_for_isabelle
from common.session_logger import SessionLogger

# NOTE: common.mcp_client (needs the `mcp` package) and common.model (needs
# `openai`) are imported LAZILY inside the functions that use them, so this
# module stays importable without either (unit tests exercise the local
# file-tool logic only).

SYSTEM = "isabelle_pool_server_lsp"

# ── Local file tools (runner-side; the MCP is read-only by design) ──────

FILE_TOOLS = [
    {
        "name": "read_file",
        "description": "Read the current content of the problem .thy file in your workspace.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "write_file",
        "description": (
            "Replace the ENTIRE content of the problem .thy file in your workspace "
            "(full-file write). The Isabelle MCP observes the edit automatically — "
            "check the result with isabelle_diagnostic_messages after every write."
        ),
        "parameters": {
            "type": "object",
            "properties": {"content": {"type": "string", "description": "the complete new file content"}},
            "required": ["content"],
        },
    },
]
_LOCAL_TOOL_NAMES = {t["name"] for t in FILE_TOOLS}


def _safe_workdir_path(workdir: Path, path: str | None) -> Path:
    """Resolve a (possibly agent-supplied) path INSIDE the workdir; escapes are
    rejected (the agent may only touch its attempt workdir)."""
    candidate = Path(path) if path else workdir
    if not candidate.is_absolute():
        candidate = workdir / candidate
    resolved = Path(candidate).resolve()
    if resolved != workdir.resolve() and workdir.resolve() not in resolved.parents:
        raise ValueError(f"path escapes the attempt workdir: {path}")
    return resolved


def _target_file(workdir: Path) -> Path:
    """The single .thy in the attempt workdir."""
    thys = sorted(workdir.glob("*.thy"))
    if not thys:
        raise FileNotFoundError(f"no .thy file in {workdir}")
    return thys[0]


def handle_local_tool(workdir: Path, name: str, args: dict) -> str:
    """Execute a local file tool. Returns the tool output string."""
    target = _target_file(workdir)
    if name == "read_file":
        return target.read_text(encoding="utf-8")
    if name == "write_file":
        content = args.get("content")
        if not isinstance(content, str) or not content.strip():
            return "ERROR: write_file needs a non-empty 'content' string (the FULL file)."
        _safe_workdir_path(workdir, args.get("path"))  # path arg optional but sandboxed
        target.write_text(content, encoding="utf-8")
        return (f"wrote {len(content)} chars to {target.name} — the MCP re-syncs on the "
                f"next isabelle_* call; check isabelle_diagnostic_messages now.")
    return f"ERROR: unknown local tool {name}"


# ── Prompt (file-workflow variant of the shared critical rules) ──────────

_DONE_SORRY_RE = re.compile(r"\b(sorry|oops)\b")

# Max times an attempt is sent back when the DONE gate rejects its claim
DONE_NUDGE_LIMIT = 2


def _lsp_prompt_body(problem, file_path: Path) -> str:
    return f"""\
You are an expert interactive theorem prover assistant for Isabelle/HOL. Your job is to construct a complete, correct Isar proof of the target theorem.

WORKFLOW (file-based):
- The problem file is on disk at: {file_path}
- Read it with read_file(); modify it with write_file(content) — ALWAYS the
  COMPLETE new file content. The Isabelle MCP observes your edit automatically.
- The file contains the target theorem with a `sorry`. Replace the sorry with a
  real proof. NEVER leave `sorry` or `oops` in the file.
- After EVERY write_file, call isabelle_diagnostic_messages(file_path) and fix
  every error it reports (it gives line/column, command kind, and the message).
- Inspect goals mid-proof with isabelle_goal(file_path, line) (1-based lines),
  facts with isabelle_query(file_path, "find_theorems ...") / isabelle_local_facts,
  and symbols with isabelle_hover_info / isabelle_definition.
- Check the overall state with isabelle_proof_state(file_path).

SOLVER RULE — NEVER write external-solver invocations (smt, metis, cvc5,
vampire, z3, verit, e, spass, etc.) in your proof text. When a subgoal defeats
simp/linarith/argo/auto/presburger, call
isabelle_sledgehammer(file_path, line=<line of the statement or proof step>,
subgoal=<n>) and paste its "Try this:" suggestion VERBATIM. If it answers "No
proof found"/times out, split the goal into smaller `have` steps and
sledgehammer those. You may test alternatives safely with
isabelle_multi_attempt(file_path, line, candidates) — it never modifies your
file.

DONE CRITERIA — the theorem is proved ONLY when: the file has NO `sorry`/`oops`,
isabelle_diagnostic_messages reports no errors, and isabelle_proof_state shows
proof_finished=true. When all hold, reply with the single word DONE.
"""


async def check_done_readiness(session, file_path: Path) -> tuple[bool, str]:
    """Objective DONE check (parity with the chunk runner's gate): the synced
    file must be proof-finished and sorry-free. (True, "") when unreadable —
    the arbiter judges instead."""
    from common.mcp_client import call_tool

    state_raw = await call_tool(session, "isabelle_proof_state", {"file_path": str(file_path)})
    if state_raw.startswith("MCP tool error"):
        return True, ""
    try:
        state = json.loads(state_raw)
    except (TypeError, json.JSONDecodeError):
        return True, ""
    if isinstance(state, dict) and state.get("proof_finished") is not True:
        return False, "isabelle_proof_state reports proof_finished=false — the goal is still open"
    src_raw = await call_tool(session, "isabelle_source", {"file_path": str(file_path)})
    src = src_raw
    try:
        src = json.loads(src_raw).get("source", src_raw)
    except (TypeError, json.JSONDecodeError, AttributeError):
        pass
    if isinstance(src, str) and _DONE_SORRY_RE.search(src):
        return False, "the source still contains sorry/oops"
    return True, ""


async def warmup_lsp(session, cfg, logger, file_path: Path) -> None:
    """Warm the file session INSIDE setup (best-effort, uncounted): one cheap
    query + a positioned sledgehammer on the sorry-holed theorem statement
    (its line is a prove-mode state even with the sorry below) to spin up ATPs."""
    from common.mcp_client import call_tool

    try:
        text = file_path.read_text(encoding="utf-8")
        theorem_line = next(
            (i + 1 for i, ln in enumerate(text.splitlines()) if ln.lstrip().startswith("theorem ")),
            None,
        )
        await asyncio.wait_for(
            call_tool(session, "isabelle_query",
                      {"file_path": str(file_path), "command": "thm TrueI"}),
            timeout=cfg.budgets.tool_timeout_seconds,
        )
        if theorem_line is not None:
            await asyncio.wait_for(
                call_tool(session, "isabelle_sledgehammer",
                          {"file_path": str(file_path), "line": theorem_line, "timeout_s": 15}),
                timeout=cfg.budgets.tool_timeout_seconds,
            )
        logger.log_text("SETUP warmup_lsp", "ok")
    except Exception as e:  # noqa: BLE001 — warm-up is best-effort
        logger.log_text("SETUP warmup_lsp", f"warm-up failed (ignored): {e}")


async def run_attempt(problem, repeat: int, results_path: Path, client=None, cfg=None) -> None:
    from common.mcp_client import call_tool, list_tools, mcp_session_startup_retry
    from common.model import ModelClient, no_tool_call_action

    cfg = cfg or load()
    client = client or ModelClient(cfg)
    system_prompt = cfg.system_prompt

    result = AttemptResult(
        system=SYSTEM,
        problem=problem.name,
        repeat=repeat,
        model_id=cfg.model.model_id,
        model_provider=cfg.model.provider,
        model_temperature=cfg.model.temperature,
    )
    timer = Timer()
    attempt_t0 = time_mod.time()
    tokens = TokenAggregator()
    tool_times: list[float] = []
    round_latencies: list[float] = []

    runs_dir = cfg.paths.runs_dir / SYSTEM
    workdir = runs_dir / "work" / f"{problem.name}_rep{repeat}"
    runs_dir.mkdir(parents=True, exist_ok=True)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    target_path = workdir / problem.path.name
    target_path.write_text(problem.full_text, encoding="utf-8")
    final_thy_path = runs_dir / f"{problem.name}_rep{repeat}.thy"
    logger = SessionLogger(SYSTEM, problem.name, repeat, cfg.paths.runs_dir)

    messages = [
        {"role": "user", "content": _lsp_prompt_body(problem, target_path)},
    ]
    if system_prompt:
        logger.log_text("SYSTEM_PROMPT", system_prompt)
    logger.log_message(messages[0])

    session = None
    try:
        async with mcp_session_startup_retry(cfg.mcp_servers[SYSTEM]) as session:
            mcp_tools = await list_tools(session)
            tools = mcp_tools + FILE_TOOLS
            try:
                out = await asyncio.wait_for(
                    call_tool(session, "isabelle_open", {"file_path": str(target_path)}),
                    timeout=cfg.budgets.tool_timeout_seconds,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"isabelle_open setup timed out after {cfg.budgets.tool_timeout_seconds}s")
            logger.log_text("SETUP isabelle_open", out[:300])
            if out.startswith("MCP tool error"):
                raise RuntimeError(f"isabelle_open failed at setup: {out[:300]}")
            await warmup_lsp(session, cfg, logger, target_path)

            result.setup_s = round(time_mod.time() - attempt_t0, 2)
            timer.start()
            round_start: float = 0.0
            nudges_used = 0
            done_nudges_used = 0
            for _round in range(cfg.budgets.max_rounds):
                elapsed = timer.elapsed()
                if elapsed >= cfg.budgets.problem_wall_cap_seconds:
                    result.error = f"problem wall cap exceeded ({cfg.budgets.problem_wall_cap_seconds}s)"
                    break

                round_result = await client.chat(messages, tools=tools, system_prompt=system_prompt)
                tokens.add(round_result.usage)
                result.rounds += 1
                if round_result.finish_reason == "length":
                    result.n_truncated_rounds += 1
                now = timer.elapsed()
                round_latencies.append(round(now - round_start, 2))
                round_start = now

                assistant_message = {
                    "role": "assistant",
                    "content": round_result.assistant_text or "",
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in round_result.tool_calls
                    ],
                }
                logger.log_message(assistant_message)
                if round_result.reasoning_text:
                    logger.log_text("REASONING", round_result.reasoning_text[:2000])

                if not round_result.tool_calls:
                    action, payload = no_tool_call_action(round_result, nudges_used)
                    if action == "done":
                        if done_nudges_used < DONE_NUDGE_LIMIT:
                            ready, reason = await check_done_readiness(session, target_path)
                            if not ready:
                                done_nudges_used += 1
                                result.n_nudge_rounds += 1
                                payload = (f"[DONE not accepted: {reason}. Fix this and "
                                           f"re-check, then reply DONE.]")
                                text = (round_result.assistant_text or "").strip()
                                if text:
                                    messages.append({"role": "assistant", "content": text})
                                messages.append({"role": "user", "content": payload})
                                logger.log_message({"role": "user", "content": payload})
                                continue
                        result.agent_claimed_solved = True
                        break
                    if action == "stop":
                        result.error = payload
                        break
                    nudges_used += 1
                    result.n_nudge_rounds += 1
                    text = (round_result.assistant_text or "").strip()
                    if text:
                        messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": payload})
                    logger.log_message({"role": "user", "content": payload})
                    continue

                tool_outputs = []
                for tc in round_result.tool_calls:
                    result.n_tool_calls += 1
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError as e:
                        err = (f"ERROR: tool arguments were not valid JSON ({e}). "
                               f"Re-issue the call with complete, valid JSON.")
                        tool_outputs.append({"tool_call_id": tc.id, "role": "tool", "name": name, "content": err})
                        logger.log_tool_result(tc.id, name, err)
                        continue
                    for k, v in list(args.items()):
                        if isinstance(v, str):
                            args[k] = sanitize_for_isabelle(v)
                    t0 = time_mod.time()
                    if name in _LOCAL_TOOL_NAMES:
                        try:
                            output = handle_local_tool(workdir, name, args)
                        except Exception as e:  # noqa: BLE001
                            output = f"ERROR: {type(e).__name__}: {e}"
                    else:
                        try:
                            output = await asyncio.wait_for(
                                call_tool(session, name, args),
                                timeout=cfg.budgets.tool_timeout_seconds,
                            )
                            tool_times.append(time_mod.time() - t0)
                        except asyncio.TimeoutError:
                            output = f"Tool call timed out after {cfg.budgets.tool_timeout_seconds}s"
                    tool_outputs.append({"tool_call_id": tc.id, "role": "tool", "name": name, "content": output})
                    logger.log_tool_result(tc.id, name, output)
                    # Only an explicit DONE ends the attempt; the arbiter remains
                    # the sole success judge.

                messages.append(assistant_message)
                messages.extend(tool_outputs)

            result.wall_s = round(timer.stop(), 2)
            result.input_tokens = tokens.input_tokens
            result.output_tokens = tokens.output_tokens
            result.cached_tokens = tokens.cached_tokens
            result.prover_s = round(sum(tool_times), 2) if tool_times else None
            result.first_tool_s = round(tool_times[0], 2) if tool_times else None
            result.round_latencies = round_latencies

            # Final file state → the arbiter's input (the file is the artifact).
            src = target_path.read_text(encoding="utf-8")
            if not src.rstrip().endswith("end"):
                src = src.rstrip() + "\nend\n"
            final_thy_path.write_text(src, encoding="utf-8")
            result.final_thy_path = str(final_thy_path)
            logger.log_text("FINAL_SOURCE", src)
            await call_tool(session, "isabelle_close", {"file_path": str(target_path)})

    except Exception as e:
        while hasattr(e, "exceptions") and getattr(e, "exceptions"):
            subs = getattr(e, "exceptions")
            if subs:
                e = subs[0]
            else:
                break
        msg = f"{type(e).__name__}: {e}"
        logger.log_text("ERROR", msg)
        result.error = msg
        result.wall_s = round(timer.stop(), 2) if timer.t0 is not None else 0.0
        result.input_tokens = tokens.input_tokens
        result.output_tokens = tokens.output_tokens
        result.cached_tokens = tokens.cached_tokens
        result.prover_s = round(sum(tool_times), 2) if tool_times else None
        result.first_tool_s = round(tool_times[0], 2) if tool_times else None
        result.round_latencies = round_latencies
        if session is not None:
            try:
                await call_tool(session, "isabelle_close", {"file_path": str(target_path)})
            except Exception:
                pass
    finally:
        if session is not None:
            try:
                await call_tool(session, "isabelle_close", {"file_path": str(target_path)})
            except Exception:
                pass
        # Arbiter (must be before logger.close() to keep the file descriptor open)
        if final_thy_path.exists():
            verdict = await check(problem, final_thy_path, gym_url=cfg.arbiter_gym_url)
            logger.log_text("ARBITER_VERDICT", (
                f"solved={verdict['solved']} "
                f"error={verdict.get('error')} "
                f"build_log={verdict.get('build_log', '')}"
            ))
            result.arbiter_solved = verdict["solved"]
            if not result.arbiter_solved and result.error is None:
                result.error = verdict.get("error")
        else:
            result.arbiter_solved = False
            if result.error is None:
                result.error = "no final theory file available for arbiter"

        append_result(results_path, result)
        logger.close()
        print(f"{problem.name} rep{repeat}: rounds={result.rounds} wall={result.wall_s}s "
              f"tok={result.total_tokens} arbiter={result.arbiter_solved}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run Isabelle Pool Server LSP-MCP comparison")
    parser.add_argument("--thy-dir", required=True, type=Path, help="Directory containing .thy problems")
    parser.add_argument("--repeats", type=int, default=None, help="Overrides config repeats")
    parser.add_argument("--select", help="Only run problems whose name contains this substring")
    args = parser.parse_args()

    cfg = load()
    repeats = args.repeats or cfg.budgets.repeats
    problems = load_problems(args.thy_dir)
    if args.select:
        problems = [p for p in problems if args.select in p.name]

    results_path = cfg.paths.runs_dir / SYSTEM / "results.jsonl"
    results_path.parent.mkdir(parents=True, exist_ok=True)

    for problem in problems:
        for repeat in range(repeats):
            try:
                await run_attempt(problem, repeat, results_path)
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"FAILED {problem.name} rep{repeat}: {e}")
                res = AttemptResult(
                    system=SYSTEM,
                    problem=problem.name,
                    repeat=repeat,
                    model_id=cfg.model.model_id,
                    model_provider=cfg.model.provider,
                    model_temperature=cfg.model.temperature,
                    error=str(e),
                )
                append_result(results_path, res)


if __name__ == "__main__":
    asyncio.run(main())
