"""Do a session's theories cover the imports a client asks ``enter_theory`` for?

IPS fixes a session's theory context when the session is acquired: a node's
context comes from the wrapper theory that imports the session's theories,
and the imports in the node's own header only gate loading (Scala
``Edit_Utils.get_thy_header_edits``). A request for imports outside that
context used to be accepted silently, and the theory was then checked
without them (training-metrics review afd3de9: a corpus replay on ``Main``
sessions; the July HOL-Analysis small-step run, 2 of 93 files). The backend
answers from Isabelle's own import graph (``Repl_Session.uncovered_imports``);
the route turns a non-empty answer into a 409.
"""

from __future__ import annotations

from typing import Any, List, Sequence

COVERAGE_TIMEOUT_S = 30.0


def uncovered_imports(session: Any, imports: Sequence[str]) -> List[str]:
    """The requested imports the session's theories do not cover (empty when
    all are covered, or when the backend cannot judge them: project theories,
    unqualified local names, a session theory outside the heap's graph)."""
    theories = [str(t) for t in (getattr(session, "theories", None) or [])]
    wanted = [str(i) for i in imports if str(i).strip()]
    if not theories or not wanted:
        return []
    raw = session.backend.raw
    fut = session.backend.submit(
        lambda: raw.uncovered_imports("\n".join(theories), "\n".join(wanted))
    )
    out = fut.result(timeout=COVERAGE_TIMEOUT_S)
    return [line for line in str(out or "").split("\n") if line.strip()]
