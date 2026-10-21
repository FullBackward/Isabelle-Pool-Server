"""Tests for the MCP-comparison harness fixes
(MCP-comparison audit, internal working notes, §2, H1–H6).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation" / "MCP-comparison"))

# The harness imports the OpenAI SDK at module level; the server image does not
# ship it. Skip cleanly instead of breaking collection (tracker RC2-1 note).
pytest.importorskip("openai", reason="openai SDK not installed (harness-only dependency)")

from common.model import NUDGE_LIMIT, RoundResult, no_tool_call_action  # noqa: E402
from common.problems import derive_session


def _round(text: str | None, finish_reason: str | None = "stop") -> RoundResult:
    return RoundResult(
        assistant_text=text, tool_calls=[], usage={}, latency_s=0.0,
        finish_reason=finish_reason,
    )


# --------------------------------------------------- §2/H2: no-tool-call policy


def test_done_reply_finishes_attempt():
    action, payload = no_tool_call_action(_round("All verified. DONE"), 0)
    assert action == "done" and payload is None


def test_truncated_round_is_nudged_not_killed():
    action, payload = no_tool_call_action(_round("", finish_reason="length"), 0)
    assert action == "nudge"
    assert "token limit" in payload


def test_truncation_stop_message_names_max_tokens():
    action, payload = no_tool_call_action(_round("", finish_reason="length"), NUDGE_LIMIT)
    assert action == "stop"
    assert "max_tokens" in payload  # not "content filter"


def test_text_only_round_is_nudged_then_stopped():
    action, _ = no_tool_call_action(_round("Let me think about the approach."), 0)
    assert action == "nudge"
    action, payload = no_tool_call_action(_round("Still thinking."), NUDGE_LIMIT)
    assert action == "stop"
    assert "without replying DONE" in payload


def test_empty_round_without_truncation_labelled_empty():
    action, payload = no_tool_call_action(_round(None), NUDGE_LIMIT)
    assert action == "stop"
    assert "empty" in payload


# ------------------------------------------------------- H6: session derivation


def test_derive_session_plain_theories_use_default():
    assert derive_session(["Main"]) == "HOL"
    assert derive_session(["Complex_Main"]) == "HOL"


def test_derive_session_qualified_import_names_its_session():
    assert derive_session(
        ["Complex_Main", "HOL-Computational_Algebra.Computational_Algebra"]
    ) == "HOL-Computational_Algebra"


# ------------------------------------ I/Q token resolution + setup fail-fast


def test_iq_token_env_wins(monkeypatch):
    import run_autocorrode_iq as iq

    monkeypatch.setenv("IQ_AUTH_TOKEN", "  tok-from-env  ")
    token, source = iq.resolve_iq_token()
    assert token == "tok-from-env" and "env" in source


def test_iq_token_file_fallback(monkeypatch, tmp_path):
    import run_autocorrode_iq as iq

    monkeypatch.delenv("IQ_AUTH_TOKEN", raising=False)
    tok_file = tmp_path / "iq_token.txt"
    tok_file.write_text("tok-from-file\n")
    monkeypatch.setenv("IQ_AUTH_TOKEN_FILE", str(tok_file))
    token, source = iq.resolve_iq_token()
    assert token == "tok-from-file" and source == str(tok_file)


def test_iq_token_none_when_unconfigured(monkeypatch):
    import run_autocorrode_iq as iq

    monkeypatch.delenv("IQ_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("IQ_AUTH_TOKEN_FILE", raising=False)
    monkeypatch.setattr(iq, "_TOKEN_FILE_DEFAULT", Path("/nonexistent/iq_token.txt"))
    token, source = iq.resolve_iq_token()
    assert token is None and source == "not configured"


def test_iq_tool_output_failure_detection():
    import run_autocorrode_iq as iq

    assert iq.tool_output_failed("MCP tool error (authenticate): McpError: Invalid authentication token")
    assert iq.tool_output_failed("McpError: Connection closed")
    assert not iq.tool_output_failed("Authenticated successfully")
    assert not iq.tool_output_failed('{"count":1,"positions":[...]}')


# --------------------------------------- schema: old results rows still load


def test_load_results_accepts_rows_without_new_fields(tmp_path):
    from common.metrics import AttemptResult, append_result, load_results

    path = tmp_path / "results.jsonl"
    append_result(path, AttemptResult(system="isabelle_pool_server", problem="p", repeat=0))
    # simulate an OLD row (pre new fields) by dropping them
    import json
    row = json.loads(path.read_text().splitlines()[0])
    del row["n_truncated_rounds"]
    del row["n_nudge_rounds"]
    path.write_text(json.dumps(row) + "\n")
    rows = load_results(path)
    assert rows[0].n_truncated_rounds == 0
    assert rows[0].n_nudge_rounds == 0
    # latency fields added later (setup/warmup asymmetry metrics) default to None
    assert rows[0].setup_s is None
    assert rows[0].first_tool_s is None


# ------------------------- DONE gate: closing `end` must survive (run1/rep2)


def test_theory_ends_with_end_accepts_clean_theory():
    import run_autocorrode_iq as iq

    assert iq.theory_ends_with_end("theory t\nbegin\n  lemma x by simp\n\nend\n")
    assert iq.theory_ends_with_end("proof -\n  show True by simp\nqed\nend")


def test_theory_ends_with_end_ignores_trailing_blank_lines():
    import run_autocorrode_iq as iq

    assert iq.theory_ends_with_end("qed\nend\n\n  \n")


def test_theory_ends_with_end_rejects_missing_end():
    import run_autocorrode_iq as iq

    # run1/rep2: agent's line-replace over the buffer tail deleted `end`;
    # the file ended at `qed` and the arbiter failed with "Malformed theory".
    assert not iq.theory_ends_with_end("theory t\nbegin\n  lemma x by simp\nqed\n")
    assert not iq.theory_ends_with_end("")
    assert not iq.theory_ends_with_end("qed\n\n")


# ------------------- sorry-check guard: count parsing + transient 0 (run1/rep0)


def test_parse_sorry_count_reads_count():
    import run_autocorrode_iq as iq

    assert iq.parse_sorry_count('{"count":1,"positions":[{"line":9}]}') == 1
    assert iq.parse_sorry_count('{"count":0,"positions":[]}') == 0


def test_parse_sorry_count_minus_one_on_garbage():
    import run_autocorrode_iq as iq

    assert iq.parse_sorry_count("MCP tool error (get_sorry_positions): boom") == -1
    assert iq.parse_sorry_count("[1,2]") == -1  # valid JSON, wrong shape
    assert iq.parse_sorry_count('{"positions":[]}') == -1  # count absent


# ------------------- DONE gate: document status parsing (2026-07-25 run reps)


def test_parse_document_status_running_not_settled():
    import run_autocorrode_iq as iq

    # rep1's actual final get_document_info: 1 running, is_processed false.
    out = ('{"node_name":"Draft.t","error_count":0,"status":'
           '{"unprocessed":0,"running":1,"finished":242,"errors":0,"is_processed":false}}')
    running, unprocessed, is_processed, errors = iq.parse_document_status(out)
    assert (running, unprocessed, is_processed, errors) == (1, 0, False, 0)


def test_parse_document_status_settled_with_errors():
    import run_autocorrode_iq as iq

    # rep4 pattern: settled but failed commands present.
    out = ('{"node_name":"Draft.t","error_count":2,"status":'
           '{"unprocessed":0,"running":0,"finished":89,"errors":2,"is_processed":true}}')
    assert iq.parse_document_status(out) == (0, 0, True, 2)


def test_parse_document_status_clean_and_garbage():
    import run_autocorrode_iq as iq

    out = ('{"node_name":"Draft.t","error_count":0,"status":'
           '{"unprocessed":0,"running":0,"finished":50,"errors":0,"is_processed":true}}')
    assert iq.parse_document_status(out) == (0, 0, True, 0)
    assert iq.parse_document_status("MCP tool error: boom") is None
    assert iq.parse_document_status("[1,2]") is None


# ------------------- isabelle_mcp DONE gate: evaluation snapshot parsing


def test_parse_evaluation_snapshot_clean():
    import run_isabelle_mcp as im

    settled, errors = im.parse_evaluation_snapshot(
        "Evaluation finished.\n\nwork/t.thy: clean")
    assert settled and not errors


def test_parse_evaluation_snapshot_in_progress():
    import run_isabelle_mcp as im

    settled, _ = im.parse_evaluation_snapshot(
        "work/t.thy: in progress (2 running so far)")
    assert not settled
    settled, _ = im.parse_evaluation_snapshot("work/t.thy:\n  running: 9\n  pending: 10")
    assert not settled


def test_parse_evaluation_snapshot_errors():
    import run_isabelle_mcp as im

    settled, errors = im.parse_evaluation_snapshot(
        "work/t.thy:\n  errors: 3-5, 7\n  warnings: 1")
    assert settled and errors


# --- fill_sorries stage 3 (plan 2026-10-18 §2, review S4): --prompt-variant ---


def _lsp_runner():
    import importlib

    sys.path.insert(
        0, str(Path(__file__).resolve().parents[1] / "evaluation" / "MCP-comparison")
    )
    return importlib.import_module("run_isabelle_pool_server_lsp")


def test_prompt_variant_baseline_is_the_builtin_prompt(tmp_path):
    """Without --prompt-variant the user prompt is the built-in body, byte for byte."""
    r = _lsp_runner()
    assert r._variant_prompt(None, tmp_path / "P.thy") is None
    body = r._lsp_prompt_body(None, tmp_path / "P.thy")
    assert (
        body.startswith("You are an expert interactive theorem prover assistant")
        and "isabelle_fill_sorries" not in body
    )


def test_prompt_variant_from_config_substitutes_the_path(tmp_path):
    r = _lsp_runner()
    text = r._variant_prompt("fill_sorries", tmp_path / "P.thy")
    assert (
        str(tmp_path / "P.thy") in text and "isabelle_fill_sorries(file_path)" in text
    )
    assert "{file_path}" not in text
    with pytest.raises(SystemExit):
        r._variant_prompt("no_such_variant", tmp_path / "P.thy")


def test_variant_mcp_env_caps_patience_below_the_tool_timeout():
    from common.config import BudgetsConfig, MCPServerConfig

    r = _lsp_runner()

    class Cfg:
        budgets = BudgetsConfig(
            max_rounds=1,
            problem_wall_cap_seconds=1,
            tool_timeout_seconds=300,
            repeats=1,
        )
        mcp_servers = {
            r.SYSTEM: MCPServerConfig(command=["x"], env={"PYTHONPATH": "."})
        }

    assert (
        r._variant_mcp_config(Cfg, None) is Cfg.mcp_servers[r.SYSTEM]
    )  # baseline: untouched
    env = r._variant_mcp_config(Cfg, "fill_sorries").env
    assert env["ISABELLE_MCP_LSP_FILL_PATIENCE_S"] == "240" and env["PYTHONPATH"] == "."
    Cfg.budgets = BudgetsConfig(
        max_rounds=1, problem_wall_cap_seconds=1, tool_timeout_seconds=120, repeats=1
    )
    assert (
        r._variant_mcp_config(Cfg, "fill_sorries").env[
            "ISABELLE_MCP_LSP_FILL_PATIENCE_S"
        ]
        == "60"
    )


def test_variant_rows_and_logs_live_under_the_variant_folder(tmp_path):
    from common.metrics import AttemptResult, append_result, load_results
    from common.session_logger import SessionLogger

    path = tmp_path / "isabelle_pool_server_lsp" / "fill_sorries" / "results.jsonl"
    path.parent.mkdir(parents=True)
    append_result(
        path,
        AttemptResult(
            system="isabelle_pool_server_lsp",
            problem="p",
            repeat=0,
            variant="fill_sorries",
        ),
    )
    (row,) = load_results(path)
    assert row.variant == "fill_sorries"
    assert AttemptResult(system="s", problem="p", repeat=0).variant == "baseline"
    log = SessionLogger(
        "isabelle_pool_server_lsp",
        "p",
        0,
        tmp_path,
        console=False,
        variant="fill_sorries",
    )
    log.close()
    assert (
        log.log_path
        == tmp_path
        / "isabelle_pool_server_lsp"
        / "fill_sorries"
        / "logs"
        / "p_rep0.log"
    )
    log2 = SessionLogger("isabelle_pool_server_lsp", "p", 0, tmp_path, console=False)
    log2.close()
    assert (
        log2.log_path == tmp_path / "isabelle_pool_server_lsp" / "logs" / "p_rep0.log"
    )


def test_arbiter_retries_a_memory_503_until_it_clears(monkeypatch):
    """Review ed2d751 S5: right after the attempt's session is destroyed the
    bigstep admission can refuse; the runner retries the arbiter."""
    import asyncio

    r = _lsp_runner()
    monkeypatch.setattr(r, "ARBITER_RETRY_S", 0.0)
    answers = [
        {
            "solved": False,
            "error": "gym arbiter: HTTPStatusError: Server error '503 Service Unavailable'",
            "build_log": "",
        },
        {"solved": True, "error": None, "build_log": "ok"},
    ]
    calls = []

    async def fake_check(problem, path, gym_url):
        calls.append(gym_url)
        return answers.pop(0)

    monkeypatch.setattr(r, "check", fake_check)

    class Log:
        def log_text(self, *a):
            pass

    out = asyncio.run(r._check_with_headroom(None, Path("x.thy"), "http://x", Log()))
    assert out["solved"] is True and len(calls) == 2
    answers.extend(
        [{"solved": False, "error": "file still contains sorry/oops", "build_log": ""}]
    )
    out = asyncio.run(r._check_with_headroom(None, Path("x.thy"), "http://x", Log()))
    assert out["solved"] is False and len(calls) == 3  # a non-503 verdict is final
