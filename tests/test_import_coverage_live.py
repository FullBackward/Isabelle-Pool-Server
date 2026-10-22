"""Live regression test for the import check (docs/ISSUES.md Bug 25; review
5a4ff3a S2): the Scala side (the wrapper theory's ancestry, name resolution)
is covered only against a running server, so this matrix is kept as an opt-in
test. It needs a server with the HOL heap:

    ISABELLE_LIVE_TESTS=1 pytest tests/test_import_coverage_live.py
    (ISABELLE_LIVE_URL, default http://localhost:8000)

One session per theory set, closed after its cases.
"""

from __future__ import annotations

import asyncio
import os

import pytest

URL = os.environ.get("ISABELLE_LIVE_URL", "http://localhost:8000")


def _server_up() -> bool:
    try:
        import httpx  # pylint: disable=import-outside-toplevel

        return httpx.get(f"{URL}/readyz", timeout=5).status_code == 200
    except Exception:  # pylint: disable=broad-exception-caught
        return False


pytestmark = pytest.mark.skipif(
    os.environ.get("ISABELLE_LIVE_TESTS") != "1" or not _server_up(),
    reason="live test: set ISABELLE_LIVE_TESTS=1 with an Isabelle Pool Server (HOL heap)",
)

# session theories -> [(requested imports, status, uncovered or None)]
MATRIX = [
    (
        ["Main"],
        [
            (["HOL.Real"], 409, ["HOL.Real"]),
            (["Complex_Main"], 409, ["Complex_Main"]),
            (["HOL.Complex_Main"], 409, ["HOL.Complex_Main"]),  # review 83baa0a B1
            (["HOL-Library.Infinite_Set"], 409, ["HOL-Library.Infinite_Set"]),
            (["Main"], 200, None),
            (["HOL.Main"], 200, None),
            (["Some_Project_Theory"], 422, None),  # review 83baa0a S1: header_failed
        ],
    ),
    (
        ["Complex_Main"],
        [
            (["Main"], 200, None),
            (["HOL.Real"], 200, None),
            (["HOL.Complex_Main"], 200, None),
        ],
    ),
    (
        ["HOL.Main"],  # a qualified session theory (review 83baa0a B1)
        [
            (["HOL.Real"], 409, ["HOL.Real"]),
            (["Complex_Main"], 409, ["Complex_Main"]),
            (["Main"], 200, None),
        ],
    ),
    (
        ["HOL.Real"],
        [
            (["HOL.Real"], 200, None),
            (["HOL.Real", "HOL.Transcendental"], 409, ["HOL.Transcendental"]),
        ],
    ),
    (["HOL.Transcendental"], [(["HOL.Real"], 200, None)]),
    (
        [
            "HOL-Library.Infinite_Set"
        ],  # ancestry through a document node, outside the heap
        [
            (["HOL-Library.Infinite_Set"], 200, None),
            (["Main"], 200, None),
            (["HOL.Real"], 409, ["HOL.Real"]),
        ],
    ),
]


async def _run_group(theories, cases):
    import httpx  # pylint: disable=import-outside-toplevel

    from client.async_client import (  # pylint: disable=import-outside-toplevel
        PoolAsyncClient,
    )

    results = []
    async with PoolAsyncClient(URL, timeout=900) as c:
        s = await c.acquire_session(
            theories=theories, field="HOL", reuse_dirty=False, label="live-imports"
        )
        try:
            for n, (imports, _want, _unc) in enumerate(cases):
                try:
                    await c.enter_theory(
                        s["session_id"],
                        f"Live_Imports_{n}",
                        imports=imports,
                        lease_id=s["lease_id"],
                    )
                    results.append((200, None))
                except httpx.HTTPStatusError as e:
                    results.append((e.response.status_code, e.response.json()))
            if theories == [
                "Complex_Main"
            ]:  # the accepted context really holds the import
                r = await c.execute_command(
                    s["session_id"], "typ real", lease_id=s["lease_id"]
                )
                results.append(("typ real", r.get("success")))
        finally:
            await c.close_session(s["session_id"], lease_id=s["lease_id"])
    return results


@pytest.mark.parametrize("theories,cases", MATRIX, ids=["+".join(t) for t, _ in MATRIX])
def test_enter_theory_import_matrix(theories, cases):
    results = asyncio.run(_run_group(theories, cases))
    for (imports, want, want_unc), (code, body) in zip(cases, results):
        assert code == want, (theories, imports, code, body)
        if want == 409:
            assert (
                body["error"] == "imports_not_covered" and body["uncovered"] == want_unc
            )
        if want == 422:
            assert body["error"] == "header_failed"
    if theories == ["Complex_Main"]:
        assert results[-1] == ("typ real", True)
