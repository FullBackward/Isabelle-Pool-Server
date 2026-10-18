"""MCP-side advice from server facts (plan §2.C, D4) and the tool guard."""

from __future__ import annotations

import asyncio
import json

import httpx

from mcp_servers.common.guard import make_guard
from mcp_servers.common.memory_advice import (  # pylint: disable=protected-access
    ToolNames,
    _others_text,
    advise,
    others_summary,
    parse_http_error,
    reminder,
    translate_http_error,
    with_reminder,
)
from mcp_servers.common.registry import (
    KIND_BINDING,
    KIND_SCRATCH,
    KIND_THEORY,
    SessionRegistry,
)

TOOLS = ToolNames(
    release_idle="isabelle_release_idle()",
    close="isabelle_close(f, destroy=true)",
    list_sessions="isabelle_list_sessions()",
    memory_status="isabelle_memory_status()",
    reopen="reopen it",
)


def _facts(**over):
    base = {
        "error": "memory_pressure",
        "state": "critical",
        "retry_after_s": 7,
        "memory": {
            "used_mb": 12600.0,
            "limit_mb": 14336.0,
            "available_mb": 1200.0,
            "pressure_pct": 88.0,
        },
        "evictable": [],
        "leased": [],
        "builds_in_flight": {"bigstep": 0, "heap": 0},
        "sledgehammer_inflight": 0,
        "external_pressure": None,
    }
    base.update(over)
    return base


def _err(status, body=None, headers=None):
    resp = httpx.Response(
        status,
        json=body if body is not None else {},
        headers=headers or {},
        request=httpx.Request("POST", "http://gym/x"),
    )
    return httpx.HTTPStatusError(f"{status}", request=resp.request, response=resp)


# ------------------------------------------------------------------ parsing


def test_parse_http_error_reads_body_and_retry_header():
    info = parse_http_error(
        _err(503, {"error": "pool_full", "detail": "full"}, {"Retry-After": "9"})
    )
    assert (info["status"], info["error"], info["detail"], info["retry_after_s"]) == (
        503,
        "pool_full",
        "full",
        9,
    )
    info = parse_http_error(_err(503, _facts()))
    assert info["retry_after_s"] == 7  # body wins
    assert parse_http_error(RuntimeError("x"))["status"] is None


# ------------------------------------------------------------------ advice


def test_rule1_release_what_you_hold_idle():
    reg = SessionRegistry()
    s = reg.register(
        "s", "L", KIND_SCRATCH, "scratch:default:plain", label="scratch default/plain"
    )
    b = reg.register("b", "L", KIND_BINDING, "/f/Foo.thy", label="file Foo.thy")
    now = s.last_used_at + 1  # binding fresh, scratch always releasable
    adv = advise(_facts(), [s, b], tools=TOOLS, idle_s=120, now=now)
    assert (
        adv[0].startswith("Release the 1 session(s)")
        and "isabelle_release_idle()" in adv[0]
    )
    assert "retry in 7 s" in adv[0]
    assert len(adv) == 1  # nothing to escalate yet


def test_rule2_nothing_idle_but_server_can_reclaim_others():
    reg = SessionRegistry()
    b = reg.register("b", "L", KIND_BINDING, "/f/Foo.thy", label="file Foo.thy")
    facts = _facts(
        evictable=[{"session_id": "o1", "rss_mb": 900.0, "task_group": "default"}]
    )
    adv = advise(facts, [b], tools=TOOLS, idle_s=120, now=b.last_used_at)
    assert "reclaim 1 idle session(s) of other clients" in adv[0]


def test_rule3_escalate_to_the_user():
    reg = SessionRegistry()
    b = reg.register("b", "L", KIND_BINDING, "/f/Foo.thy", label="file Foo.thy")
    facts = _facts(
        leased=[
            {"session_id": "b", "rss_mb": 500.0, "task_group": "default"},
            {"session_id": "x1", "rss_mb": 3000.0, "task_group": "teamB"},
            {"session_id": "x2", "rss_mb": 2500.0, "task_group": "teamB"},
        ],
        builds_in_flight={"bigstep": 1, "heap": 0},
    )
    adv = advise(facts, [b], tools=TOOLS, idle_s=120, now=b.last_used_at)
    text = " ".join(adv)
    assert "stop and tell the user" in text
    assert (
        "2 session(s) of other clients in task group(s) teamB hold ~5500.0 MB" in text
    )
    assert "1 theory build(s) are running" in text
    assert "cannot free that memory" in text
    # repeated failures escalate even with nothing else going on
    adv2 = advise(_facts(), [], tools=TOOLS, failures=2)
    assert any("stop and tell the user" in a for a in adv2)
    # external pressure escalates
    adv3 = advise(_facts(external_pressure=True), [], tools=TOOLS)
    assert any("outside the prover server" in a for a in adv3)


def test_others_summary_excludes_own_sessions():
    facts = _facts(
        leased=[
            {"session_id": "mine", "rss_mb": 1.0, "task_group": "a"},
            {"session_id": "theirs", "rss_mb": 2.0, "task_group": "b"},
        ],
        evictable=[{"session_id": "idle", "rss_mb": 3.0, "task_group": "b"}],
        builds_in_flight={"bigstep": 2, "heap": 1},
        external_pressure=False,
    )
    o = others_summary(facts, {"mine"})
    assert o == {
        "sessions": 2,
        "rss_mb": 5.0,
        "unmeasured": 0,
        "sledgehammer_mb": 0.0,
        "task_groups": ["b"],
        "builds_in_flight": 3,
        "external_pressure": False,
    }


# -------------------------------------------------------------- translation


def test_translate_memory_pressure_payload():
    reg = SessionRegistry()
    t = reg.register("t", "LEASE", KIND_THEORY, "conn", label="theory Foo")
    facts = _facts(
        leased=[
            {"session_id": "t", "rss_mb": 777.0, "idle_s": 3.0, "task_group": "default"}
        ]
    )
    out = translate_http_error(_err(503, facts), reg, "conn", tools=TOOLS)
    assert out["error"] == "memory_pressure" and out["state"] == "critical"
    assert out["memory"] == {
        "pressure_pct": 88.0,
        "available_mb": 1200.0,
        "limit_mb": 14336.0,
    }
    assert out["your_sessions"][0]["rss_mb"] == 777.0  # merged from the facts
    assert "LEASE" not in json.dumps(out) and "session_id" not in json.dumps(
        out["your_sessions"]
    )
    assert out["retry_after_s"] == 7 and out["failures_in_a_row"] == 1
    assert isinstance(out["advice"], list) and out["advice"]


def test_translate_other_statuses():
    reg = SessionRegistry()
    assert (
        translate_http_error(
            _err(503, {"error": "gateway_unavailable"}, {"Retry-After": "10"}),
            reg,
            None,
            tools=TOOLS,
        )["error"]
        == "server_recovering"
    )
    assert (
        translate_http_error(_err(503, {"error": "pool_full"}), reg, None, tools=TOOLS)[
            "error"
        ]
        == "pool_full"
    )
    assert (
        translate_http_error(_err(409, {"detail": "busy"}), reg, None, tools=TOOLS)[
            "error"
        ]
        == "busy"
    )
    gone = translate_http_error(
        _err(404, {"detail": "Session x not found"}), reg, None, tools=TOOLS
    )
    assert gone["error"] == "session_gone" and "reopen it" in gone["advice"][0]
    assert (
        translate_http_error(_err(422, {"detail": "bad"}), reg, None, tools=TOOLS)[
            "error"
        ]
        == "http_422"
    )


# --------------------------------------------------------------- reminder


def test_reminder_only_at_warning_or_critical():
    reg = SessionRegistry()
    s = reg.register("s", "L", KIND_SCRATCH, "scratch:d:plain", label="scratch d/plain")
    assert reminder("ok", [s], tools=TOOLS) is None
    assert reminder("unknown", [s], tools=TOOLS) is None
    note = reminder("warning", [s], tools=TOOLS, pressure_pct=74.0)
    assert note.startswith(
        "MEMORY REMINDER (warning, 74.0% used): you hold 1 idle session(s)"
    )
    assert "isabelle_release_idle()" in note and "expect memory failures" not in note
    crit = reminder("critical", [], tools=TOOLS)
    assert "you hold no sessions" in crit and "expect memory failures" in crit


def test_with_reminder_attaches_to_json_objects_and_text():
    out = with_reminder(json.dumps({"a": 1}), "NOTE")
    assert json.loads(out) == {"a": 1, "memory_reminder": "NOTE"}
    assert with_reminder("[1, 2]", "NOTE").endswith("\n\nNOTE")
    assert with_reminder("plain text", "NOTE") == "plain text\n\nNOTE"
    assert with_reminder(5, "NOTE") == 5


# ------------------------------------------------------------------- guard


class _Client:
    last_memory_state = "ok"


def _guarded(reg, client, on_reminder=None):
    async def get_client():
        return client

    return make_guard(
        registry=reg,
        tools=TOOLS,
        get_client=get_client,
        owner_of=lambda a, k: None,
        idle_s=120,
        on_reminder=on_reminder,
    )


def test_guard_translates_http_errors_and_keeps_metadata():
    reg = SessionRegistry()
    client = _Client()

    @_guarded(reg, client)
    async def tool(x: int) -> str:
        """Doc."""
        raise _err(503, _facts())

    assert tool.__doc__ == "Doc." and tool.__name__ == "tool"
    out = json.loads(asyncio.run(tool(1)))
    assert out["error"] == "memory_pressure" and out["advice"]


def test_guard_attaches_reminder_and_runs_hook_only_when_tight():
    reg = SessionRegistry()
    reg.register("s", "L", KIND_SCRATCH, "scratch:d:plain", label="scratch d/plain")
    client = _Client()
    hooks = []

    async def hook(state):
        hooks.append(state)

    @_guarded(reg, client, on_reminder=hook)
    async def tool() -> str:
        return json.dumps({"ok": True})

    assert json.loads(asyncio.run(tool())) == {
        "ok": True
    }  # state ok: untouched, no hook
    client.last_memory_state = "critical"
    out = json.loads(asyncio.run(tool()))
    assert out["ok"] is True and out["memory_reminder"].startswith(
        "MEMORY REMINDER (critical"
    )
    assert hooks == ["critical"]


# --------------------------------------------- review d2bc679 S2 / S5


def test_theory_session_is_never_releasable_but_is_mentioned():
    reg = SessionRegistry()
    th = reg.register("t", "L", KIND_THEORY, "conn", label="theory Foo")
    now = th.last_used_at + 10_000  # idle for hours: still the live proof state
    adv = advise(_facts(), [th], tools=TOOLS, idle_s=120, now=now)
    assert not any(a.startswith("Release") for a in adv)
    assert any("Your current theory session is yours to keep" in a for a in adv)
    assert any("isabelle_close(f, destroy=true)" in a for a in adv)
    note = reminder("warning", [th], tools=TOOLS, idle_s=120, now=now)
    assert "none releasable" in note and "Release what you no longer need" not in note


def test_pool_full_carries_the_server_detail():
    reg = SessionRegistry()
    out = translate_http_error(
        _err(503, {"error": "pool_full", "detail": "Session pool is full (6)"}),
        reg,
        None,
        tools=TOOLS,
    )
    assert out["error"] == "pool_full" and out["detail"] == "Session pool is full (6)"


def test_unmeasured_holders_are_not_reported_as_zero_mb():
    facts = _facts(leased=[{"session_id": "t1", "rss_mb": None, "task_group": "teamB"}])
    o = others_summary(facts, set())
    assert o["sessions"] == 1 and o["unmeasured"] == 1 and o["rss_mb"] == 0.0
    text = " ".join(advise(facts, [], tools=TOOLS))
    assert "hold memory not yet measured" in text and "0.0 MB" not in text
    facts = _facts(
        leased=[
            {"session_id": "t1", "rss_mb": None, "task_group": "teamB"},
            {"session_id": "t2", "rss_mb": 400.0, "task_group": "teamB"},
        ]
    )
    text = " ".join(advise(facts, [], tools=TOOLS))
    assert ">= 400.0 MB measured, 1 not yet measured" in text


def test_others_summary_counts_running_sledgehammers():
    """Review 260d78d S2: the ATP memory of others' running sledgehammers is a
    separate field (`rss_mb` is the poly tree) and the text names it."""
    facts = _facts(
        leased=[
            {
                "session_id": "theirs",
                "rss_mb": 2.0,
                "sledgehammer_mb": 1300.5,
                "task_group": "b",
            },
            {
                "session_id": "more",
                "rss_mb": 1.0,
                "sledgehammer_mb": 0.0,
                "task_group": "b",
            },
        ],
        evictable=[],
        builds_in_flight={},
        external_pressure=False,
    )
    o = others_summary(facts, set())
    assert o["rss_mb"] == 3.0 and o["sledgehammer_mb"] == 1300.5
    assert "plus ~1300.5 MB of running sledgehammers" in _others_text(o)
    quiet = {**o, "sledgehammer_mb": 0.0}
    assert "sledgehammer" not in _others_text(quiet)
