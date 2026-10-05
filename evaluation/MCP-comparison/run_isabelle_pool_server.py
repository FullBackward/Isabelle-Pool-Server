#!/usr/bin/env python3
"""Isabelle Pool Server MCP comparison runner."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time as time_mod
from pathlib import Path

# Allow importing from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.arbiter import check
from common.config import load
from common.mcp_client import call_tool, list_tools, mcp_session
from common.metrics import AttemptResult, Timer, TokenAggregator, append_result
from common.model import ModelClient, no_tool_call_action
from common.problems import derive_session, load_problems, sanitize_for_isabelle
from common.session_logger import SessionLogger

# ── Four prompt variants (as 1st user-message bodies) ──────────────────

def _theory_note(problem) -> str:
    # Starting-state parity with the file-based MCPs (fairness): the harness
    # pre-submits the target statement, so the agent starts from an OPEN proof
    # exactly like I/Q / Isabelle-MCP agents start from a sorry-holed file —
    # no rounds are spent transcribing the theorem.
    field = derive_session(problem.imports)
    return (
        f"You are an expert interactive theorem prover assistant for Isabelle/HOL. Your job is to construct a complete, correct Isar proof of the target theorem, using the tools provided by the Isabelle MCP server you are connected to."
        f"The theory '{problem.name}' is already entered with imports "
        f"{', '.join(problem.imports)}, and the TARGET THEOREM STATEMENT has "
        f"already been submitted — the proof is OPEN at its goal.  Do NOT "
        f"re-declare the theorem; continue the proof from the open goal.  If "
        f"you ever need to restart, use the EXACT call:\n"
        f"enter_theory(name='{problem.name}', "
        f"imports=[{', '.join(repr(i) for i in problem.imports)}], "
        f"field='{field}')\n"
        f"(field names the prebuilt parent session — omitting it makes the "
        f"session re-process imports from source or fail to find them.)\n"
        f"— the statement will be re-submitted for you automatically.\n\n"
        f"Target theorem (already submitted, do not re-declare):\n"
        f"{problem.statement}\n\n"
    )


def _general_prompt_body() -> str:
    return """\
CRITICAL RULES:
----------
1. SOLVER RULE — NEVER write external-solver invocations (smt, metis, cvc5,
   vampire, z3, verit, e, spass, etc.) directly in your proof text.  When you
   reach a subgoal that simp/linarith/argo/auto/presburger cannot close:
     a. Submit your proof UP TO that subgoal.
     b. Verify.  If proof_open=True, call sledgehammer() on the open goal.
     c. Use sledgehammer's EXACT output to close the goal.  Do NOT write your
        own solver invocation.  If sledgehammer answers "No proof found" or
        "Timed out", that IS the answer — split the goal into smaller have
        steps and sledgehammer those, or change strategy.
     d. WHERE to call it: sledgehammer needs a REAL open goal — the seeded
        statement (before you write `proof -`) or a `have` subgoal.  Do NOT
        call it right after a bare `proof -` (it fails with a 'state mode'
        error) and NEVER when pending_qed=True (no goal left — submit `qed`).
     e. ESCALATION — after the SAME subgoal has failed twice, or any chunk
        times out on it, you MUST call sledgehammer() before trying another
        manual method.  A looping blast/auto burning the chunk timeout is
        exactly the failure sledgehammer replaces.

2. AUTO-ROLLBACK — When verify_chunk reports success=False (any command
   failed), those failed commands are AUTOMATICALLY rolled back.  The source
   stays at the last successful state.  Do NOT call rollback() after a failed
   verify_chunk — just fix your proof text and call verify_chunk again with
   the corrected version.  TIMEOUT DISCIPLINE — pass a small timeout
   (timeout=30-60) for exploratory chunks.  A timeout IS a failure: the
   stuck line names the looping method — replace it or sledgehammer it,
   NEVER resubmit a near-identical command.

3. DONE CRITERIA — The theorem is proved ONLY when verify_chunk reports ALL
   of: success=True AND proof_open=False AND used_sorry=False for the TARGET
   theorem (auxiliary lemmas do NOT count).  If proof_open=True with
   pending_qed=True, the goal is discharged but the block lacks its `qed` —
   submit a bare `qed` chunk; do NOT reply DONE yet.  When your latest
   verify_chunk already shows this, reply with the single word DONE
   immediately — no summary, no further confirmation calls are required.

4. NEVER use `sorry` or `oops` — they invalidate your proof.
!!! WARNING You have to recheck every rules when you generated a proof !!!
"""


def _stepwise_prompt_body() -> str:
    return """\
CRITICAL RULES (read carefully — violating any of these will fail the proof)
----------

1. INCREMENTAL SUBMISSION — NEVER submit the entire proof at once.  Start by
   submitting only the structural skeleton up to the first open subgoal — do
   NOT write `sorry` to get there; simply stop before the closing method and
   leave the goal open.  Inspect the open subgoals (verify_chunk shows them
   when proof_open=True), understand what needs to be proved, THEN decide how
   to close each subgoal one at a time.

2. SOLVER RULE — NEVER call external solvers (smt, metis, cvc5, vampire,
   z3, verit, e, spass, etc.) directly under any circumstances.  Even if you
   believe the entire proof is correct and uses a solver, you must instead:
     a. Push the proof only up to the point just BEFORE the solver line.
     b. Verify that chunk.  If proof_open=True, call sledgehammer() on the
        open goal.
     c. Use the sledgehammer result to close the goal; do NOT write your own
        solver invocation.  If sledgehammer answers "No proof found" or
        "Timed out", that IS the answer — do NOT write smt/metis yourself;
        split the goal into smaller have steps and sledgehammer those, or
        change strategy.
     d. WHERE to call it: sledgehammer needs a REAL open goal — the seeded
        statement (before you write `proof -`) or a `have` subgoal.  Do NOT
        call it right after a bare `proof -` (it fails with a 'state mode'
        error) and NEVER when pending_qed=True (no goal left — submit `qed`).
     e. ESCALATION — after the SAME subgoal has failed twice, or any chunk
        times out on it, you MUST call sledgehammer() before trying another
        manual method.  A looping blast/auto burning the chunk timeout is
        exactly the failure sledgehammer replaces.

3. AUTO-ROLLBACK — When verify_chunk reports success=False (any command
   failed), those failed commands are AUTOMATICALLY rolled back.  The source
   stays at the last successful state.  Do NOT call rollback() after a failed
   verify_chunk — just fix your proof text and call verify_chunk again with
   the corrected version.

4. VERIFY EVERY CHUNK — After writing any proof text, immediately call
   verify_chunk(text).  Read the per-command status report.  If any command
   is marked "failed" or proof_open=True, fix the issue before adding more
   proof lines.  Never stack multiple unverified chunks.  TIMEOUT DISCIPLINE —
   pass a small timeout (timeout=30-60) for exploratory chunks.  A timeout IS
   a failure: the stuck line names the looping method — replace it or
   sledgehammer it, NEVER resubmit a near-identical command.

5. DONE CRITERIA — The theorem is proved ONLY when verify_chunk reports ALL
   of: success=True AND proof_open=False AND used_sorry=False for the TARGET
   theorem (auxiliary lemmas do NOT count).  If proof_open=True with
   pending_qed=True, the goal is discharged but the block lacks its `qed` —
   submit a bare `qed` chunk; do NOT reply DONE yet.  When your latest
   verify_chunk already shows this, reply with the single word DONE
   immediately — no summary, no further confirmation calls are required.

!!! WARNING You have to recheck every rules when you generated a proof !!!   

LAYERED INCREMENTAL WORKFLOW — HOW TO BUILD A PROOF STEP BY STEP
----------

Each verify_chunk(text) APPENDS commands to the theory source.  Build the
proof in layers — NEVER submit the entire proof at once.  NEVER include
`sorry` or `oops` in any verify_chunk (they mark the proof as invalid and
auto-rollback will leave you with nothing to inspect).

A concrete illustration (using a TRIVIAL theorem — THIS IS NOT YOUR TASK):

  [Layer 1 — structural outline; the statement is ALREADY submitted, so
   start with the proof opening]
  verify_chunk("
    proof -
      have step1: \\"Suc 0 + 1 = 2\\" by simp
  ")
  → success=True, proof_open=True → call proof_state() to inspect subgoals!

  [Layer 2 — continue reasoning]
  verify_chunk("
      also have \\"Suc 0 + 1 = Suc (0 + 1)\\" by simp
  ")
  → success=True, proof_open=True (still open — more to prove)

  [Layer 3 — close the proof]
  verify_chunk("
      finally show ?thesis by simp
    qed
  ")
  → success=True, proof_open=False, used_sorry=False → DONE!

LAYER RULES:
- Layer 1: proof - + initial reasoning continuing the ALREADY-SUBMITTED
  statement (5-8 lines, no qed)
- Middle layers: derive intermediate facts (5-10 lines each)
- Last layer: close with qed (2-3 lines)
- After EVERY successful layer with proof_open=True, inspect subgoals via proof_state()
- Use simp/linarith/argo/auto/presburger for routine steps
- Call sledgehammer() ONLY when these methods fail on a subgoal — and only on
  a REAL open goal (a `have` subgoal or the seeded statement, not a bare
  `proof -` state, never when pending_qed=True)
- NEVER re-declare the already-submitted theorem statement
- NEVER use `sorry` or `oops` — they invalidate your proof
"""


def _segment_prompt_body() -> str:
    return """\
CRITICAL RULES:
----------
1. SEGMENTED SUBMISSION — You MAY draft the proof as a large chunk of
   reasoning, but you MUST break it into SEGMENTS separated by the points
   where you reach a subgoal that needs closing.  Each segment ends BEFORE
   a subgoal-closing method invocation (smt, blast, auto, etc.).  Submit
   one segment at a time.

2. SOLVER RULE — NEVER write external-solver invocations (smt, metis, cvc5,
   vampire, z3, verit, e, spass, etc.) directly in your proof text.  When you
   reach a subgoal that simp/linarith/argo/auto/presburger cannot close:
     a. Submit the segment UP TO that subgoal (ending BEFORE the solver line).
     b. Verify the segment (verify_chunk).  If proof_open=True, call
        sledgehammer() on the open goal.
     c. Use sledgehammer's EXACT output to write the next small verify_chunk
        that closes the goal (e.g. `by (metis ...)` if sledgehammer says so).
     d. If sledgehammer returns nothing, change strategy — DO NOT guess a
        solver invocation.
     e. ESCALATION — After the SAME subgoal has failed twice, or any chunk
        times out on it, you MUST call sledgehammer() before trying another
        manual method.  A looping blast/auto burning the whole chunk timeout
        is exactly the failure sledgehammer replaces.

3. AUTO-ROLLBACK — When verify_chunk reports success=False (any command
   failed), those failed commands are AUTOMATICALLY rolled back.  The source
   stays at the last successful state.  Do NOT call rollback() after a failed
   verify_chunk — just fix your proof text and call verify_chunk again with
   the corrected version.

4. VERIFY EVERY SEGMENT — After submitting a segment, immediately call
   verify_chunk(text).  Read the per-command status report.  If any command
   is marked "failed", fix the issue before adding more.  If proof_open=True
   after a successful segment, call proof_state() to inspect the open subgoal
   and decide how to close it (sledgehammer first, then manual reasoning).
   TIMEOUT DISCIPLINE — pass a small timeout (timeout=30-60) for exploratory
   chunks.  A timeout IS a failure: the stuck line names the looping method —
   replace it or sledgehammer it, NEVER resubmit a near-identical command.

5. DONE CRITERIA — The theorem is proved ONLY when verify_chunk reports ALL
   of: success=True AND proof_open=False AND used_sorry=False for the TARGET
   theorem (auxiliary lemmas do NOT count).  If proof_open=True with
   pending_qed=True, the goal is discharged but the block lacks its `qed` —
   submit a bare `qed` chunk; do NOT reply DONE yet.  When your latest
   verify_chunk already shows this, reply with the single word DONE
   immediately — no summary, no further confirmation calls are required.

!!! WARNING You have to recheck every rules when you generated a proof !!!   

SEGMENTED PROOF WORKFLOW — HOW TO SUBMIT A PROOF (INCLUDING RECOVERY)
----------

Plan your proof in advance, then submit it in CLEAN SEGMENTS.  This example
uses a TRIVIAL theorem — it is NOT your task — but note how failures are
handled, because most of your work is recovery, not first-try success.

  [Segment 1 — proof opening + reasoning up to the FIRST open subgoal;
   the statement is ALREADY submitted, so start with proof -]
  verify_chunk("
    proof -
      have step1: \"n + m = m + n\" for n m :: nat
        by (simp add: add.commute)
      (* stop here — the next step is uncertain *)
  ", timeout=30)
  → success=True, proof_open=True → call proof_state(), see the open subgoal

  [Segment 2 — a FAILED chunk: read the error, change ONE thing, resubmit]
  verify_chunk("
      have step2: \"n * (m + k) = n * m + n * k\" for n m k :: nat
        using step1 by blast
  ", timeout=30)
  → success=False, line 2 by failed "Failed to apply initial proof method"
  The whole chunk was rolled back automatically — nothing to undo.  This is
  the SECOND failure on this subgoal → ESCALATE: call sledgehammer() NOW.
  sledgehammer() returns e.g.:
    ["metis found a proof...", "Try this: by (metis distrib_left) (12 ms)"]
  Paste its suggestion VERBATIM:
  verify_chunk("
      have step2: \"n * (m + k) = n * m + n * k\" for n m k :: nat
        using step1 by (metis distrib_left)
  ", timeout=30)
  → success=True, proof_open=True

  WHERE to call sledgehammer — it needs a REAL open goal:
  - GOOD: on the seeded statement (before you write `proof -`), or on a
    `have` subgoal like above.
  - BAD: right after a bare `proof -` — it fails with an
    "Illegal application of proof command in state mode" error.
  - NEVER when pending_qed=True — there is no goal left; submit `qed`.

  If sledgehammer answers "No proof found" or "Timed out", that IS the
  answer — do NOT write smt/metis from memory.  Split the goal into smaller
  have steps and sledgehammer those, or change strategy.

  [A TIMEOUT is a failure, not a retry invitation]
  If a chunk returns timed_out=True with stuck_line=N: line N names the
  looping method.  Do NOT resubmit a near-identical command — replace that
  method (or sledgehammer the goal) and/or shrink the segment.

  [Final segment — watch for pending_qed]
  verify_chunk("
      finally show ?thesis by simp
  ", timeout=30)
  → success=True, proof_open=True, pending_qed=True
  pending_qed means: goal discharged, only `qed` missing.  Submit a bare qed:
  verify_chunk("qed", timeout=30)
  → success=True, proof_open=False, used_sorry=False → reply DONE (one word).

SEGMENT RULES:
- Keep segments short (≈10-15 lines).  A failed chunk is rolled back as a
  WHOLE — the larger the segment, the more correct work you must resubmit.
  When a region is uncertain, submit smaller.
- Each segment MUST END before a solver invocation (smt, metis, etc.) or
  before qed.
- NEVER write smt/metis/cvc5/vampire/z3/verit/e/spass in your proof text.
  ALWAYS call sledgehammer() first at each open subgoal and use its output.
- NEVER include `sorry` or `oops` — they invalidate your proof.
- A timed-out chunk means a looping method (see the stuck line): shrink the
  segment AND change the method — do not retry the same command.
"""


_PROMPTS = {
    "general":    _general_prompt_body,
    "stepwise":   _stepwise_prompt_body,
    "segment":    _segment_prompt_body,
}

# ═════════════════════════════════════════════════════════════════════════

async def seed_statement(session, problem, cfg, logger) -> None:
    """Pre-submit the target theorem statement so the agent starts from an
    OPEN proof (fairness: file-based MCPs start with the statement in the
    file). Harness work — not counted as a round or an agent tool call."""
    out = await asyncio.wait_for(
        call_tool(session, "verify_chunk", {"text": problem.statement}),
        timeout=cfg.budgets.tool_timeout_seconds,
    )
    logger.log_text("SETUP seed_statement", out[:300])
    if out.startswith("MCP tool error") or "success=False" in out:
        raise RuntimeError(f"statement seeding failed: {out[:300]}")


async def warmup_session(session, cfg, logger) -> None:
    """Warm the fresh session's ML/ATP state INSIDE setup, before the timer
    starts. Fairness: I/Q's persistent jEdit is always warm; without this, the
    first verify_chunk/sledgehammer of every gym attempt pays cold-start costs
    inside the timed region (old run1/rep3 had five 180-200s sledgehammer
    rounds). Best-effort: never counted as a round or tool call, never fatal.

    Step 1 runs before seeding (a top-level lemma is only valid there); step 2
    must run AFTER seed_statement so sledgehammer has the real open goal."""
    try:
        out = await asyncio.wait_for(
            call_tool(session, "verify_chunk", {"text": "lemma harness_warmup: True by simp"}),
            timeout=cfg.budgets.tool_timeout_seconds,
        )
        logger.log_text("SETUP warmup_chunk", out[:300])
    except Exception as e:  # warm-up is best-effort
        logger.log_text("SETUP warmup_chunk", f"warm-up failed (ignored): {e}")


async def warmup_sledgehammer(session, cfg, logger) -> None:
    """Second warm-up step: one short sledgehammer on the seeded goal to spin
    up ATP processes. Best-effort, result discarded (the agent never sees it)."""
    try:
        out = await asyncio.wait_for(
            call_tool(session, "sledgehammer", {"timeout_s": 15}),
            timeout=cfg.budgets.tool_timeout_seconds,
        )
        logger.log_text("SETUP warmup_sledgehammer", out[:300])
    except Exception as e:  # warm-up is best-effort
        logger.log_text("SETUP warmup_sledgehammer", f"warm-up failed (ignored): {e}")


_DONE_SORRY_RE = re.compile(r"\b(sorry|oops)\b")

# Max times an attempt is sent back when the DONE gate rejects its claim
# (parity with the I/Q runner). After that the DONE is accepted and the
# arbiter judges the file as-is.
DONE_NUDGE_LIMIT = 2


async def check_done_readiness(session) -> tuple[bool, str]:
    """Objective DONE check (parity with the I/Q runner's DONE gate): the proof
    must actually be finished and the source sorry-free. (True, "") when the
    state can't be read — the arbiter judges instead."""
    state_raw = await call_tool(session, "proof_state", {})
    if state_raw.startswith("MCP tool error"):
        return True, ""
    try:
        state = json.loads(state_raw)
    except (TypeError, json.JSONDecodeError):
        return True, ""
    if isinstance(state, dict) and state.get("proof_finished") is not True:
        return False, "proof_state reports proof_finished=false — the goal is still open"
    src_raw = await call_tool(session, "source", {})
    src = src_raw
    try:
        src = json.loads(src_raw).get("source", src_raw)
    except (TypeError, json.JSONDecodeError, AttributeError):
        pass
    if isinstance(src, str) and _DONE_SORRY_RE.search(src):
        return False, "the source still contains sorry/oops"
    return True, ""


async def run_attempt(
    problem,
    repeat: int,
    results_path: Path,
    prompt_name: str = "segment",
) -> None:
    cfg = load()
    client = ModelClient(cfg)
    system_prompt = cfg.system_prompt
    body_fn = _PROMPTS.get(prompt_name, _segment_prompt_body)
    messages = [
        {"role": "user", "content": (
            _theory_note(problem) +
            body_fn() + "\n\n" +
            problem.statement + "\n"
        )},
    ]

    result = AttemptResult(
        system="isabelle_pool_server",
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

    runs_dir = cfg.paths.runs_dir / "isabelle_pool_server"
    runs_dir.mkdir(parents=True, exist_ok=True)
    final_thy_path = runs_dir / f"{problem.name}_rep{repeat}.thy"
    logger = SessionLogger("isabelle_pool_server", problem.name, repeat, cfg.paths.runs_dir)
    if system_prompt:
        logger.log_text("SYSTEM_PROMPT", system_prompt)
    logger.log_message(messages[0])

    try:
        async with mcp_session(cfg.mcp_servers["isabelle_pool_server"]) as session:
            mcp_tools = await list_tools(session)
            # Setup is bounded and fail-fast (mirrors the I/Q runner): an
            # unresponsive server used to hang this call for the full HTTP
            # timeout with nothing recorded. `field` names the prebuilt parent
            # session (e.g. HOL-Computational_Algebra) so the gym session
            # starts from its heap instead of re-processing the imports from
            # source on every attempt.
            try:
                out = await asyncio.wait_for(
                    call_tool(session, "enter_theory", {
                        "name": problem.name,
                        "imports": problem.imports,
                        "field": derive_session(problem.imports),
                    }),
                    timeout=cfg.budgets.tool_timeout_seconds,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"enter_theory setup timed out after {cfg.budgets.tool_timeout_seconds}s "
                    f"(server unresponsive or session creation overloaded)")
            logger.log_text("SETUP enter_theory", out[:300])
            if out.startswith("MCP tool error") or "McpError" in out:
                raise RuntimeError(f"enter_theory failed at setup: {out[:300]}")
            await warmup_session(session, cfg, logger)   # before seed: top-level lemma
            await seed_statement(session, problem, cfg, logger)
            await warmup_sledgehammer(session, cfg, logger)  # after seed: real goal

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

                round_result = await client.chat(messages, tools=mcp_tools, system_prompt=system_prompt)
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
                    # DONE / nudge / stop — shared policy, no more silent deaths on
                    # a truncated or text-only round (audit §2, H2).
                    action, payload = no_tool_call_action(round_result, nudges_used)
                    if action == "done":
                        # Objective DONE check (parity with the I/Q runner's DONE
                        # gate): a false DONE costs a nudge round, not the attempt.
                        if done_nudges_used < DONE_NUDGE_LIMIT:
                            ready, reason = await check_done_readiness(session)
                            if not ready:
                                done_nudges_used += 1
                                result.n_nudge_rounds += 1
                                payload = (f"[DONE not accepted: {reason}. Fix this and "
                                           f"re-check (proof_state must show "
                                           f"proof_finished=true and the source must be "
                                           f"sorry-free), then reply DONE.]")
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
                        # Keep the history consistent: every tool_call gets a tool
                        # reply, else the next chat() is rejected by the API (H3).
                        err = (f"ERROR: tool arguments were not valid JSON ({e}). "
                               f"Re-issue the call with complete, valid JSON.")
                        tool_outputs.append({"tool_call_id": tc.id, "role": "tool", "name": name, "content": err})
                        logger.log_tool_result(tc.id, name, err)
                        continue
                    for k, v in list(args.items()):
                        if isinstance(v, str):
                            args[k] = sanitize_for_isabelle(v)
                    t0 = time_mod.time()
                    try:
                        output = await asyncio.wait_for(
                            call_tool(session, name, args),
                            timeout=cfg.budgets.tool_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        output = f"Tool call timed out after {cfg.budgets.tool_timeout_seconds}s"
                    if name == "enter_theory" and not output.startswith("MCP tool error"):
                        # Agent restart: re-seed the statement so the restarted
                        # session keeps starting-state parity (see seed_statement).
                        try:
                            await seed_statement(session, problem, cfg, logger)
                            output += ("\n[harness] target theorem statement re-submitted — "
                                       "the proof is OPEN; continue from the goal, do not re-declare it.")
                        except Exception as seed_err:  # noqa: BLE001
                            output += f"\n[harness] statement re-seeding FAILED: {seed_err}"
                    tool_times.append(time_mod.time() - t0)
                    tool_outputs.append({"tool_call_id": tc.id, "role": "tool", "name": name, "content": output})
                    logger.log_tool_result(tc.id, name, output)
                    # NOTE: the harness no longer terminates on a fully-green
                    # verify_chunk — that cut attempts off at the first proved
                    # HELPER lemma (audit H1). Only an explicit DONE ends the
                    # attempt; the arbiter remains the sole success judge.

                messages.append(assistant_message)
                messages.extend(tool_outputs)

            result.wall_s = round(timer.stop(), 2)
            result.input_tokens = tokens.input_tokens
            result.output_tokens = tokens.output_tokens
            result.cached_tokens = tokens.cached_tokens
            result.prover_s = round(sum(tool_times), 2) if tool_times else None
            result.first_tool_s = round(tool_times[0], 2) if tool_times else None
            result.round_latencies = round_latencies

            source_json = await call_tool(session, "source", {})
            try:
                src = json.loads(source_json).get("source", source_json)
            except Exception:
                src = source_json
            if not src.rstrip().endswith("end"):
                src = src.rstrip() + "\nend\n"
            final_thy_path.write_text(src, encoding="utf-8")
            result.final_thy_path = str(final_thy_path)
            logger.log_text("FINAL_SOURCE", src)
            await call_tool(session, "close_theory", {})

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
        # Ensure the server session is released even on error
        try:
            await call_tool(session, "close_theory", {})
        except Exception:
            pass
    finally:
        # Always release the server session to prevent idle-session buildup
        try:
            await call_tool(session, "close_theory", {})
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
    parser = argparse.ArgumentParser(description="Run Isabelle Pool Server MCP comparison")
    parser.add_argument("--thy-dir", required=True, type=Path, help="Directory containing .thy problems")
    parser.add_argument("--repeats", type=int, default=None, help="Overrides config repeats")
    parser.add_argument("--select", help="Only run problems whose name contains this substring")
    parser.add_argument("--prompt", choices=list(_PROMPTS), default="segment",
                        help="Which prompt variant to use (default: segment)")
    args = parser.parse_args()

    cfg = load()
    repeats = args.repeats or cfg.budgets.repeats
    problems = load_problems(args.thy_dir)
    if args.select:
        problems = [p for p in problems if args.select in p.name]

    results_path = cfg.paths.runs_dir / "isabelle_pool_server" / "results.jsonl"
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
                    system="isabelle_pool_server",
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
