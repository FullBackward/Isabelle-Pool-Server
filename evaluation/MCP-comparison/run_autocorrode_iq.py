#!/usr/bin/env python3
"""AutoCorrode I/Q comparison runner."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time as time_mod
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.arbiter import check
from common.config import load
from common.mcp_client import call_tool, list_tools, mcp_session
from common.metrics import AttemptResult, Timer, TokenAggregator, append_result
from common.model import ModelClient, no_tool_call_action
from common.problems import load_problems, sanitize_for_isabelle
from common.session_logger import SessionLogger

# ── IQ prompt variants ─────────────────────────────────────────────────

def _general_prompt_body(thy_path: Path) -> str:
    return (
        f"You are an expert interactive theorem prover assistant for Isabelle/HOL. Your job is to construct a complete, correct Isar proof of the target theorem, using the tools provided by the Isabelle MCP server you are connected to."
        f"Discharge every `sorry` in {thy_path.resolve()} — replace the `sorry` keyword "
        f"with a complete proof block.  Write your proof using write_file. You should complete "
        f"the proof before replying DONE. If you cannot give the reason of why.\n\n"
        f"After each edit, check the per-command results and file_summary that write_file "
        f"returns (call get_diagnostics only when you need more detail).  Fix errors before "
        f"moving on.\n\n"
        f"SOLVER RULE (read first, it overrides your habits): NEVER use external solvers "
        f"(smt, metis, cvc5, vampire, eprover, z3, spass, verit, zipperposition) directly "
        f"in your proof text.  You MUST call explore(query=\"sledgehammer\") on the current "
        f"goal first.  If sledgehammer times out, retry once on a smaller sub-goal — do "
        f"NOT fall back to writing smt/metis calls yourself.  simp, auto, blast, force, "
        f"linarith and presburger are always allowed; external ATPs are not.  If "
        f"sledgehammer cannot find a proof, the current approach is probably wrong — "
        f"change strategy instead of trying more solver calls manually.\n"
        f"ESCALATION — after the SAME goal has failed twice, or any call times out on "
        f"it, you MUST call explore(query=\"sledgehammer\") before trying another manual "
        f"method.  A looping attempt is exactly the failure sledgehammer replaces.\n"
        f"TIMEOUT DISCIPLINE — a timed-out call IS a failure: never resubmit a "
        f"near-identical edit; change the method or sledgehammer the goal instead.\n\n"
        f"The theorem is proved ONLY when there are zero errors and zero sorries.  When your "
        f"latest edit's returned results already show this, reply with the single word "
        f"DONE immediately — no summary, no further confirmation calls are required.  DONE also requires the document to be "
        f"fully processed: get_document_info must show is_processed: true with 0 running "
        f"and 0 unprocessed commands.  Running or unprocessed lines are NEVER 'background "
        f"processing' and errors are NEVER 'PIDE artifacts' — DONE is checked and rejected "
        f"otherwise.\n\n"
        f"IMPORTANT: ALL non-ASCII mathematical symbols MUST be written using Isabelle's\n"
        f"\\<name> escape notation — NEVER use raw Unicode characters.  Common escapes:\n"
        f"  \\<forall> = ∀    \\<exists> = ∃    \\<Rightarrow> = ⇒    \\<and> = ∧\n"
        f"  \\<or> = ∨       \\<not> = ¬    \\<equiv> = ≡    \\<noteq> = ≠\n"
        f"  \\<le> = ≤       \\<ge> = ≥    \\<in> = ∈      \\<subseteq> = ⊆\n"
        f"  \\<union> = ∪    \\<inter> = ∩   \\<forall>x. = ∀x.\n"
        f"For any other symbol, use \\<name> where name is its ASCII identifier.\n"
        f"Unicode characters will be REJECTED by Isabelle/save — always use \\<...>.\n"
        f"WARNING!!!! Everytime you generated a proof, recheck if it contains illegal UTF symbols!!!!\n\n"
        f"Note: I/R is not installed, do not use it.\n"
        f"Note: the MCP session is ALREADY authenticated for you — never call authenticate.\n\n"
    )


# Vendor guidance shipped with AutoCorrode I/Q (iq/iq_guidance.md) — the
# "guided" variant appends it verbatim (minus the fs_read/fs_write line, which
# references tools not present here) so the I/Q agent gets the same class of
# playbook that Isabelle Pool Server's stepwise/segment prompts provide.
_IQ_GUIDANCE = """\
VENDOR GUIDANCE (from the I/Q project):
You are a formal proof engineer working with Isabelle/jEdit. Your work
is surgical, clearly structured, and well-documented. You regularly step
back to reflect on the quality of your work, and ask yourself: Could my
proofs be cleaned up, accelerated or simplified? Could they be broken
up into smaller lemmas?

REMEMBER: When you embark on a proof, you ask yourself: Is this proof likely
short and simple, or not? If it is, try a `by ...` or an apply-style Isar
proof. If it is not, try a structured Isar proof.
- When you work on apply-style proofs, proceed incrementally. Try 1-2 tactics
  at a time, inspect their results, and proceed. DO NOT repeatedly
  replace entire proof scripts.
- When you work on an Isar proof, work top-down: First, establish the rough
  structure, using `sorry` to temporarily axiomatize core steps. Then, fill
  in those `sorry``s one at a time; if they are complex, hoist them out as
  separate lemmas or state subproofs via `proof -`.

NOTE on scaffolding: a temporary `sorry` is allowed MID-proof as described
above, but every scaffold must be discharged before you reply DONE — the
final file must be completely sorry-free or it does not count as proved.
"""


def _guided_prompt_body(thy_path: Path) -> str:
    return _general_prompt_body(thy_path) + _IQ_GUIDANCE


_PROMPTS = {
    "general":    _general_prompt_body,
    "guided":     _guided_prompt_body,
}

# ── I/Q auth token + setup helpers ──────────────────────────────────────
#
# The I/Q plugin MINTS ITS OWN token (a short-lived JWT shown in the jEdit I/Q
# panel) — there is no shared secret, and the token changes between jEdit/I/Q
# launches. Priority: IQ_AUTH_TOKEN env > IQ_AUTH_TOKEN_FILE env >
# MCP-comparison/iq_token.txt. Resolved PER ATTEMPT so you can paste a fresh
# token into the file while a batch is running.

_TOKEN_FILE_DEFAULT = Path(__file__).resolve().parent / "iq_token.txt"


def resolve_iq_token() -> tuple[str | None, str]:
    """Return (token, source-description); token is None if nothing is configured."""
    env_tok = os.environ.get("IQ_AUTH_TOKEN", "").strip()
    if env_tok:
        return env_tok, "env IQ_AUTH_TOKEN"
    candidates = []
    file_env = os.environ.get("IQ_AUTH_TOKEN_FILE", "").strip()
    if file_env:
        candidates.append(Path(file_env))
    candidates.append(_TOKEN_FILE_DEFAULT)
    for p in candidates:
        try:
            if p.is_file():
                tok = p.read_text(encoding="utf-8").strip()
                if tok:
                    return tok, str(p)
        except OSError:
            continue
    return None, "not configured"


def tool_output_failed(output: str) -> bool:
    """True if a call_tool() return string reports an error (call_tool never raises)."""
    return output.startswith("MCP tool error") or "McpError" in output


_LINE_PREFIX_RE = re.compile(r"^[ \t]*\d+:", re.MULTILINE)


async def setup_call(session, name: str, args: dict, timeout: float) -> str:
    """call_tool with a hard timeout for SETUP-phase calls.

    The agent loop wraps its tool calls in wait_for(tool_timeout), but setup
    calls had no bound: one buffer-reset write hung for the bridge's internal
    7200 s Isabelle-server timeout — twice — wasting 4 h before failing.
    """
    try:
        return await asyncio.wait_for(call_tool(session, name, args), timeout=timeout)
    except asyncio.TimeoutError:
        return f"MCP tool error ({name}): setup call timed out after {timeout:.0f}s"


async def read_iq_buffer(session, path: str, timeout: float) -> tuple[str, int]:
    """Return (buffer text, line count) from I/Q's in-memory view of `path`.

    read_file mode=Line returns numbered lines ('  1:theory ...'); strip the
    prefixes. This is the BUFFER, not the disk file — the distinction is the
    whole point of the reset logic below.
    """
    raw = await setup_call(session, "read_file", {"path": path, "mode": "Line"}, timeout)
    if tool_output_failed(raw):
        raise RuntimeError(f"I/Q read_file failed during setup: {raw.strip()}")
    data = json.loads(raw)
    content = data.get("content", "") if isinstance(data, dict) else str(data)
    return _LINE_PREFIX_RE.sub("", content), len(content.splitlines())


def theory_ends_with_end(text: str) -> bool:
    """True if the last non-empty line of the theory is the closing `end` keyword.

    I/Q's file_summary counts only per-command errors, so a buffer missing its
    trailing `end` still reports 0 errors — run1/rep2: an agent's line-replace
    spanning the buffer tail deleted `end`, the agent claimed DONE, and the
    arbiter's isabelle build failed with "Malformed theory" at EOF.
    """
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped == "end"
    return False


# Max times an attempt is sent back when the DONE gate rejects its claim
# (missing `end`, commands still running, errors or sorries present). After
# that the DONE is accepted and the arbiter judges the file as-is.
DONE_NUDGE_LIMIT = 2


def parse_sorry_count(output: str) -> int:
    """Parse a get_sorry_positions reply; -1 when the count can't be read."""
    try:
        return int(json.loads(output).get("count", -1))
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return -1


def parse_document_status(output: str) -> tuple[int, int, bool, int] | None:
    """Parse a get_document_info reply into (running, unprocessed, is_processed,
    error_count); None when the reply can't be read."""
    try:
        info = json.loads(output)
        status = info.get("status", {})
        running = int(status.get("running", 0) or 0)
        unprocessed = int(status.get("unprocessed", 0) or 0)
        is_processed = bool(status.get("is_processed", True))
        error_count = int(info.get("error_count", status.get("errors", 0)) or 0)
        return running, unprocessed, is_processed, error_count
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return None


# get_sorry_positions reads the PROCESSED PIDE document, which lags open_file:
# right after open it legitimately reports 0 sorries even for a fresh template
# (run1/rep0+rep1 aborted ~20ms after open; the count flipped to 1 within
# ~450ms once the document was processed). Poll for this long before
# concluding the buffer really is stale.
SORRY_CHECK_TIMEOUT_S = 30.0
SORRY_CHECK_POLL_S = 1.0

# DONE gate: how long to wait for PIDE to finish processing before rejecting
# DONE over still-running/unprocessed commands (usually transient lag of a
# few seconds after the last edit).
DONE_SETTLE_TIMEOUT_S = 60.0
DONE_SETTLE_POLL_S = 3.0


async def check_done_readiness(session, path: str, timeout: float) -> tuple[bool, str]:
    """Objective completion check before accepting an agent's DONE.

    Agents have claimed DONE with commands still running ("that's just
    background processing") and even with errors present ("PIDE artifacts") —
    2026-07-25 run reps 1/2/4. Returns (ready, reason); (True, "") when the
    document state can't be read, in which case the arbiter judges instead.
    """
    deadline = time_mod.monotonic() + DONE_SETTLE_TIMEOUT_S
    while True:
        out = await setup_call(session, "get_document_info", {"path": path, "include_errors": True}, timeout)
        if tool_output_failed(out):
            return True, ""  # unverifiable — let the arbiter judge
        parsed = parse_document_status(out)
        if parsed is None:
            return True, ""
        running, unprocessed, is_processed, error_count = parsed
        if running == 0 and unprocessed == 0 and is_processed:
            break
        if time_mod.monotonic() >= deadline:
            return False, (f"{running} command(s) still running and {unprocessed} "
                           f"unprocessed after {DONE_SETTLE_TIMEOUT_S:.0f}s — running or "
                           f"unprocessed lines are NEVER 'background processing'")
        await asyncio.sleep(DONE_SETTLE_POLL_S)
    if error_count > 0:
        return False, (f"{error_count} error(s) in the document — errors are NEVER "
                       f"'PIDE artifacts'; fix them")
    sorries = parse_sorry_count(
        await setup_call(session, "get_sorry_positions", {"path": path}, timeout))
    if sorries > 0:
        return False, f"{sorries} sorry/sorries remaining"
    try:
        buffer_text, _ = await read_iq_buffer(session, path, timeout)
    except RuntimeError:
        buffer_text = ""
    if buffer_text and not theory_ends_with_end(buffer_text):
        return False, ("the file does not end with the closing `end` keyword — one of "
                       "your edits deleted it; append `end` after the final `qed`")
    return True, ""


async def reset_iq_buffer(session, path: str, fresh_text: str, logger, timeout: float) -> None:
    """Force I/Q's in-memory buffer to hold `fresh_text`.

    I/Q's write_file implements ONLY line/str_replace/insert — the previous
    reset used a nonexistent 'write' command and silently no-oped for every
    run (server replied 'command write not implemented'), which is how a
    finished proof survived in the buffer between attempts. Here: if the
    buffer differs, replace its whole line range via command='line'.
    Retries cover the 'not opened in jEdit' race right after open_file.
    """
    last_err = ""
    for attempt in range(3):
        buffer_text, n_lines = await read_iq_buffer(session, path, timeout)
        if buffer_text.strip() == fresh_text.strip():
            logger.log_text("SETUP buffer_reset", f"buffer already fresh ({n_lines} lines)")
            return
        out = await setup_call(session, "write_file", {
            "path": path,
            "command": "line",
            "start_line": 1,
            "end_line": max(1, n_lines),
            "new_str": fresh_text,
        }, timeout)
        logger.log_text("SETUP buffer_reset(line-replace)", out[:500])
        if not tool_output_failed(out):
            return
        last_err = out.strip()
        await asyncio.sleep(1.0)  # 'not opened in jEdit' race — let the buffer model attach
    raise RuntimeError(f"I/Q buffer reset failed after 3 tries: {last_err}")


_TOKEN_HELP = (
    "The I/Q plugin displays its current token in the jEdit I/Q panel; it is a "
    "short-lived JWT that changes between launches. Copy it and either "
    "`export IQ_AUTH_TOKEN=<token>` or paste it into "
    f"{_TOKEN_FILE_DEFAULT} (re-read every attempt, no restart needed)."
)

# ═════════════════════════════════════════════════════════════════════════

async def run_attempt(problem, repeat: int, results_path: Path, prompt_name: str = "general") -> None:
    cfg = load()
    client = ModelClient(cfg)
    system_prompt = cfg.system_prompt

    # I/Q reads/writes files inside allowed roots; place the starting file there.
    work_dir = Path(os.environ.get("IQ_MCP_ALLOWED_ROOTS", cfg.paths.runs_dir / "autocorrode" / "work"))
    work_dir.mkdir(parents=True, exist_ok=True)
    thy_path = work_dir / f"{problem.name}.thy"
    # Write the problem to disk ONLY if the file doesn't exist yet. jEdit keeps
    # an open buffer for this path across attempts; deleting/rewriting the file
    # on disk behind its back triggers the modal "file has been modified on
    # disk by another program" dialog, which blocks jEdit's event thread and
    # stalls every I/Q call. The buffer reset below (through I/Q itself) is the
    # authoritative refresh and keeps buffer and disk in sync.
    if not thy_path.exists():
        thy_path.write_text(problem.full_text, encoding="utf-8")

    token, token_source = resolve_iq_token()
    body_fn = _PROMPTS.get(prompt_name, _general_prompt_body)
    # The prompt bodies already open with the expert-role sentence, and the
    # harness authenticates itself below — never route the token through the
    # model (one run was lost to the model mutating a hex digit of it).
    content = body_fn(thy_path)
    messages = [{"role": "user", "content": content}]

    logger = SessionLogger("autocorrode", problem.name, repeat, cfg.paths.runs_dir)
    if system_prompt:
        logger.log_text("SYSTEM_PROMPT", system_prompt)
    logger.log_message(messages[0])

    result = AttemptResult(
        system="autocorrode",
        problem=problem.name,
        repeat=repeat,
        model_id=cfg.model.model_id,
        model_provider=cfg.model.provider,
        model_temperature=cfg.model.temperature,
    )
    timer = Timer()
    attempt_t0 = time_mod.time()  # setup_s reference: attempt start → timer start
    tokens = TokenAggregator()
    tool_times: list[float] = []
    round_latencies: list[float] = []
    final_thy_path = cfg.paths.runs_dir / "autocorrode" / f"{problem.name}_rep{repeat}.thy"

    try:
        async with mcp_session(cfg.mcp_servers["autocorrode_iq"]) as session:
            mcp_tools = await list_tools(session)

            # ── fail-fast setup ─────────────────────────────────────────
            # call_tool() never raises; it returns error STRINGS. Previously all
            # three setup results were ignored, so an expired/wrong token meant
            # the buffer reset silently failed and the agent then "discovered"
            # the PREVIOUS attempt's finished proof in IQ's in-memory buffer and
            # claimed DONE in seconds (phantom solve — see the 1-2/rep1 run).
            if token is None:
                raise RuntimeError(f"No I/Q auth token configured. {_TOKEN_HELP}")
            logger.log_text("IQ_TOKEN_SOURCE", token_source)
            setup_timeout = cfg.budgets.tool_timeout_seconds
            out = await setup_call(session, "authenticate", {"token": token}, setup_timeout)
            logger.log_text("SETUP authenticate", out)
            if tool_output_failed(out):
                raise RuntimeError(
                    f"I/Q authentication failed (token from {token_source}): "
                    f"{out.strip()}\n{_TOKEN_HELP}")
            out = await setup_call(session, "open_file", {"path": str(thy_path.resolve())}, setup_timeout)
            logger.log_text("SETUP open_file", out)
            if tool_output_failed(out):
                raise RuntimeError(f"I/Q open_file failed: {out.strip()}")
            # Force IQ's in-memory buffer to match the fresh file on disk —
            # open_file reuses an existing buffer (which may still hold the
            # previous attempt's finished proof). See reset_iq_buffer.
            await reset_iq_buffer(session, str(thy_path.resolve()), problem.full_text, logger, setup_timeout)
            # Verify the buffer REALLY holds the fresh problem: it must contain
            # the original `sorry`. count=0 immediately after open_file is
            # usually just the document not being processed yet (see
            # SORRY_CHECK_TIMEOUT_S) — poll until the sorry appears; only a
            # PERSISTENT 0 means the reset didn't take (stale buffer from a
            # previous attempt). Each call's timeout is capped by the remaining
            # budget, so the whole check is bounded by SORRY_CHECK_TIMEOUT_S
            # even when a call hangs (setup_timeout is 300s by default).
            sorry_count = -1
            sorry_deadline = time_mod.monotonic() + SORRY_CHECK_TIMEOUT_S
            while True:
                remaining = sorry_deadline - time_mod.monotonic()
                if remaining <= 0:
                    break
                out = await setup_call(session, "get_sorry_positions", {"path": str(thy_path.resolve())}, min(setup_timeout, remaining))
                logger.log_text("SETUP get_sorry_positions", out)
                sorry_count = parse_sorry_count(out)
                if sorry_count != 0:
                    break
                await asyncio.sleep(SORRY_CHECK_POLL_S)
            if sorry_count == 0:
                raise RuntimeError(
                    f"I/Q buffer reset verification failed: work file still reports 0 "
                    f"sorries {SORRY_CHECK_TIMEOUT_S:.0f}s after open — IQ's buffer holds "
                    f"a previous attempt's proof. Aborting to avoid a phantom solve.")

            result.setup_s = round(time_mod.time() - attempt_t0, 2)
            timer.start()
            round_start: float = 0.0
            nudges_used = 0
            done_nudges_used = 0
            for _round in range(cfg.budgets.max_rounds):
                # Enforce per-problem wall cap
                elapsed = timer.elapsed()
                if elapsed >= cfg.budgets.problem_wall_cap_seconds:
                    result.error = f"problem wall cap exceeded ({cfg.budgets.problem_wall_cap_seconds}s)"
                    break

                round_result = await client.chat(messages, tools=mcp_tools, system_prompt=system_prompt)
                tokens.add(round_result.usage)
                result.rounds += 1
                if round_result.finish_reason == "length":
                    result.n_truncated_rounds += 1
                now = timer.elapsed()
                round_latencies.append(round(now - round_start, 2))
                round_start = now

                logger.log_message({
                    "role": "assistant",
                    "content": round_result.assistant_text or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in (round_result.tool_calls or [])
                    ],
                })
                if round_result.reasoning_text:
                    logger.log_text("REASONING", round_result.reasoning_text[:2000])

                if not round_result.tool_calls:
                    action, payload = no_tool_call_action(round_result, nudges_used)
                    if action == "done":
                        # Objective completion check before accepting DONE —
                        # agents have claimed DONE with commands still running
                        # ("just background processing") and with errors present
                        # ("PIDE artifacts"). See check_done_readiness.
                        if done_nudges_used < DONE_NUDGE_LIMIT:
                            ready, reason = await check_done_readiness(
                                session, str(thy_path.resolve()), setup_timeout)
                            if not ready:
                                done_nudges_used += 1
                                result.n_nudge_rounds += 1
                                payload = (f"[DONE not accepted: {reason}. Fix this and "
                                           f"re-check with get_document_info (it must show "
                                           f"is_processed: true, 0 running, 0 unprocessed, "
                                           f"0 errors) and get_sorry_positions (count 0), "
                                           f"then reply DONE.]")
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
                    if name == "authenticate":
                        # The harness authenticates the connection at setup; the model
                        # does not know the token (deliberately — see token-mutation
                        # incident) and its guessed tokens fail. Short-circuit instead
                        # of forwarding, so no rounds are wasted on auth errors.
                        output = ("Already authenticated by the harness — you do not need to "
                                  "call authenticate. Proceed with the other tools.")
                        tool_outputs.append({"tool_call_id": tc.id, "role": "tool", "name": name, "content": output})
                        logger.log_tool_result(tc.id, name, output)
                        continue
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError as e:
                        # Keep history consistent: every tool_call needs a tool reply (H3).
                        err = (f"ERROR: tool arguments were not valid JSON ({e}). "
                               f"Re-issue the call with complete, valid JSON.")
                        tool_outputs.append({"tool_call_id": tc.id, "role": "tool", "name": name, "content": err})
                        logger.log_tool_result(tc.id, name, err)
                        continue
                    # Sanitize string args — DeepSeek may emit lone surrogates.
                    # The filter only strips invalid UTF-16 halves; legitimate
                    # \<name> escapes pass through untouched.
                    for k, v in list(args.items()):
                        if isinstance(v, str):
                            args[k] = sanitize_for_isabelle(v)
                    # Force path to our scoped work file
                    if "path" in args and "file" not in args:
                        args["path"] = str(thy_path.resolve())
                    if "file" in args:
                        args["file"] = str(thy_path.resolve())
                    t0 = time_mod.time()
                    try:
                        output = await asyncio.wait_for(
                            call_tool(session, name, args),
                            timeout=cfg.budgets.tool_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        output = f"Tool call timed out after {cfg.budgets.tool_timeout_seconds}s"
                    tool_times.append(time_mod.time() - t0)

                    logger.log_tool_result(tc.id, name, output)

                    tool_outputs.append({
                        "tool_call_id": tc.id,
                        "role": "tool",
                        "name": name,
                        "content": output,
                    })

                messages.append({
                    "role": "assistant",
                    "content": round_result.assistant_text or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in round_result.tool_calls
                    ],
                })
                messages.extend(tool_outputs)

            result.wall_s = round(timer.stop(), 2)
            result.input_tokens = tokens.input_tokens
            result.output_tokens = tokens.output_tokens
            result.cached_tokens = tokens.cached_tokens
            result.prover_s = round(sum(tool_times), 2) if tool_times else None
            result.first_tool_s = round(tool_times[0], 2) if tool_times else None
            result.model_s = round(result.wall_s - (result.prover_s or 0), 2) if result.prover_s else None
            result.round_latencies = round_latencies

            # IQ server works in-memory; read the edited content before session
            # is torn down (which reverts the file on disk).  save_file is a
            # no-op on IQ, so we read back the current content and write it
            # ourselves.
            try:
                raw = await call_tool(session, "read_file", {
                    "path": str(thy_path.resolve()),
                    "mode": "Line",
                })
                # The IQ read_file Line mode returns JSON: {"content": " 1:...\n  2:..."}
                data = json.loads(raw)
                content = data.get("content", raw) if isinstance(data, dict) else raw
                # Strip line-number prefixes added by IQ's Line mode:
                #   "  1:theory ..." → "theory ..."
                import re as _re
                content = _re.sub(r"^[ \t]*\d+:", "", content, flags=_re.MULTILINE)
            except Exception:
                content = thy_path.read_text(encoding="utf-8")
            final_thy_path.write_text(content, encoding="utf-8")
            result.final_thy_path = str(final_thy_path)
    except Exception as e:
        # Recursively unwrap nested ExceptionGroups to find the root cause
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
        # Try to preserve the final theory file
        try:
            if thy_path.exists():
                shutil.copy(thy_path, final_thy_path)
                result.final_thy_path = str(final_thy_path)
        except Exception:
            pass
    finally:
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

        # Deliberately DO NOT delete the work file: jEdit's open buffer would
        # detect the disk change and pop the modal "file modified on disk"
        # dialog, blocking I/Q on the next attempt. The next attempt's buffer
        # reset (via I/Q) restores the fresh problem text instead.
        append_result(results_path, result)
        logger.close()
        print(f"{problem.name} rep{repeat}: rounds={result.rounds} wall={result.wall_s}s "
              f"tok={result.total_tokens} arbiter={result.arbiter_solved}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run AutoCorrode I/Q comparison")
    parser.add_argument("--thy-dir", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=None)
    parser.add_argument("--select")
    parser.add_argument("--prompt", choices=list(_PROMPTS), default="general",
                        help="Which prompt variant to use (default: general)")
    args = parser.parse_args()

    cfg = load()
    repeats = args.repeats or cfg.budgets.repeats
    problems = load_problems(args.thy_dir)
    if args.select:
        problems = [p for p in problems if args.select in p.name]

    token, token_source = resolve_iq_token()
    if token is None:
        print(f"WARNING: no I/Q auth token configured — every attempt will abort at setup.\n{_TOKEN_HELP}")
    else:
        print(f"I/Q auth token: {token_source} (re-read each attempt)")

    results_path = cfg.paths.runs_dir / "autocorrode" / "results.jsonl"
    results_path.parent.mkdir(parents=True, exist_ok=True)

    for problem in problems:
        for repeat in range(repeats):
            try:
                await run_attempt(problem, repeat, results_path, prompt_name=args.prompt)
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"FAILED {problem.name} rep{repeat}: {e}")
                res = AttemptResult(
                    system="autocorrode",
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