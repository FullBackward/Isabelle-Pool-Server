"""Neutral solved check via Isabelle Pool Server bigstep endpoint."""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

# Repo root must be importable BEFORE the client import (this previously sat
# below it, so `python -m common.arbiter` always died with ModuleNotFoundError).
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from client.async_client import PoolAsyncClient

from .problems import Problem

_SORRY_RE = re.compile(r"\b(sorry|oops)\b")


def _contains_sorry_parsed(text: str) -> bool:
    return bool(_SORRY_RE.search(text))


async def check(
    problem: Problem,
    final_thy_path: Path,
    gym_url: str,
    field: str = "HOL",
) -> dict[str, object]:
    """Return {"solved": bool, "error": str|None, "build_log": str}.

    Submits the theory to the Isabelle Pool Server's bigstep endpoint
    (POST /api/v1/sessions/bigstep).  The server must be running with
    a warm session pool.
    """
    final_thy_path = Path(final_thy_path)
    if not final_thy_path.exists():
        return {"solved": False, "error": f"file not found: {final_thy_path}", "build_log": ""}

    final_text = final_thy_path.read_text(encoding="utf-8")

    # 1. No sorry/oops
    if _contains_sorry_parsed(final_text):
        return {"solved": False, "error": "file still contains sorry/oops", "build_log": ""}

    # 2. Target theorem present
    if f"theorem {problem.theorem_name}" not in final_text:
        return {
            "solved": False,
            "error": f"target theorem {problem.theorem_name} not found",
            "build_log": "",
        }

    # 3. Derive parent session (field) and dependencies from imports
    # that live in non-HOL sessions (only imports with a dot like
    # "HOL-Computational_Algebra.Computational_Algebra").
    deps: list[str] = []
    effective_field = field
    for imp in problem.imports:
        if "." in imp:
            parent = imp.split(".")[0]
            if parent and parent not in deps and parent != "Main":
                deps.append(parent)
    # If there are external session imports, use the first as parent (field).
    # Complex_Main and other HOL-internal imports stay with field="HOL".
    if deps:
        effective_field = deps[0]

    # 4. Send to the Isabelle Pool Server via async client.
    # Budget note: the FIRST arbiter call for a problem importing a heavy session
    # (e.g. HOL-Computational_Algebra) also builds that parent session; 300 s was
    # not enough and produced false "unsolved" verdicts with an empty ReadTimeout.
    # Later calls reuse the cached heap and are fast. Pre-building once
    # (`isabelle build -b HOL-Computational_Algebra` in the container) avoids
    # paying this during scoring at all.
    build_budget = float(os.environ.get("ARBITER_BUILD_TIMEOUT_S", "900"))
    try:
        async with PoolAsyncClient(base_url=gym_url, timeout=build_budget + 60.0) as client:
            resp = await client.verify_bigstep_text(
                theory_name=problem.name,
                theory_text=final_text,
                field=effective_field,
                timeout=build_budget,
            )
            resp.raise_for_status()
            data = resp.json()
            solved = bool(data.get("theory_verified", False))
            return {
                "solved": solved,
                "error": None if solved else data.get("error", "bigstep verification failed"),
                "build_log": str(data),
            }
    except Exception as e:
        # Include the exception TYPE: httpx.ReadTimeout etc. often have an empty
        # str(), which used to yield an undiagnosable "gym arbiter: " error.
        return {"solved": False, "error": f"gym arbiter: {type(e).__name__}: {e}", "build_log": ""}


if __name__ == "__main__":
    from .config import load
    from .problems import parse_thy

    if len(sys.argv) != 3:
        print("Usage: python -m common.arbiter <original.thy> <final.thy>")
        sys.exit(1)
    cfg = load()
    problem = parse_thy(Path(sys.argv[1]))

    async def _main():
        verdict = await check(
            problem,
            Path(sys.argv[2]),
            gym_url=cfg.arbiter_gym_url or "http://localhost:8000",
        )
        print(f"solved={verdict['solved']}")
        if verdict["error"]:
            print(f"error={verdict['error']}")

    asyncio.run(_main())