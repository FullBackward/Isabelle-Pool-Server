"""The MCP-side registration list (plan §2.C, D4): what the MCP holds, with the
session id and lease kept private and a display-only ``ref`` for the agent."""

from __future__ import annotations

from mcp_servers.common.registry import (
    KIND_BINDING,
    KIND_SCRATCH,
    KIND_THEORY,
    SessionRegistry,
)


def test_register_touch_unregister_and_owner_scoping():
    reg = SessionRegistry()
    a = reg.register(
        "sid-a", "LEASE-A", KIND_THEORY, "conn1", label="theory Foo", imports=["Main"]
    )
    b = reg.register(
        "sid-b", "LEASE-B", KIND_BINDING, "/tmp/Bar.thy", label="file Bar.thy"
    )
    reg.touch("sid-a", now=a.last_used_at + 50)
    assert a.idle_s(now=a.last_used_at + 10) == 10.0
    assert [e.session_id for e in reg.owned("conn1")] == ["sid-a"]
    assert len(reg.owned(None)) == 2 and reg.ids() == {"sid-a", "sid-b"}
    assert reg.unregister("sid-b") is b and reg.unregister("sid-b") is None
    assert reg.get("sid-a") is a and reg.all() == [a]


def test_agent_view_hides_ids_and_lease():
    reg = SessionRegistry()
    e = reg.register(
        "11111111-2222",
        "LEASE-SECRET",
        KIND_SCRATCH,
        "scratch:default:plain",
        label="scratch default/plain",
        task_group="default",
    )
    view = e.to_agent()
    assert set(view) == {
        "ref",
        "kind",
        "for",
        "task_group",
        "heap_session",
        "idle_s",
        "expires_in_s",
        "rss_mb",
        "heap_mb",
        "state",
    }
    assert "LEASE-SECRET" not in repr(view) and "11111111-2222" not in repr(view)
    assert len(e.ref) == 8 and e.ref == reg.get("11111111-2222").ref  # stable


def test_merge_and_mark_missing_from_server_rows():
    reg = SessionRegistry()
    reg.register("s1", "L1", KIND_THEORY, "c")
    reg.register("s2", "L2", KIND_THEORY, "c")
    reg.merge(
        [
            {
                "session_id": "s1",
                "rss_mb": 850.0,
                "heap_mb": 497.0,
                "expires_in_s": 42.0,
                "idle_s": 7.0,
                "lease_id": "MUST-NOT-COPY",
            },
            {"session_id": "other", "rss_mb": 1.0},
        ]
    )
    e1 = reg.get("s1")
    assert (e1.rss_mb, e1.heap_mb, e1.expires_in_s, e1.server_idle_s) == (
        850.0,
        497.0,
        42.0,
        7.0,
    )
    assert e1.lease_id == "L1"  # never overwritten from server rows
    gone = reg.mark_missing([{"session_id": "s1"}])
    assert [e.session_id for e in gone] == ["s2"] and reg.get("s2").state == "gone"
    reg.touch("s2")
    assert reg.get("s2").state == "active"


def test_idle_filter_and_memory_failure_counter():
    reg = SessionRegistry()
    a = reg.register("a", None, KIND_SCRATCH, "x")
    b = reg.register("b", None, KIND_BINDING, "y")
    now = a.last_used_at
    reg.touch("b", now=now)  # b fresh
    assert [e.session_id for e in reg.idle(min_idle_s=100, now=now + 150)] == ["a", "b"]
    assert [
        e.session_id
        for e in reg.idle(min_idle_s=100, now=now + 150, kinds=(KIND_BINDING,))
    ] == ["b"]
    assert reg.note_memory_failure("c1") == 1 and reg.note_memory_failure("c1") == 2
    assert reg.memory_failures("c1") == 2 and reg.memory_failures("c2") == 0
    reg.clear_memory_failures("c1")
    assert reg.memory_failures("c1") == 0
