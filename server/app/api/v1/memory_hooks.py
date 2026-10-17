"""Request-path memory hooks for the execution routes (plan §2.B part 3).

``run_with_memory_hooks`` wraps a backend call IN THE SAME WORKER THREAD
(never on the event loop — plan-review f1fb31a B2): the call, then the
dead-prover check (``/proc`` + start time when the pid is known, else
Isabelle's ``Session.phase`` — B1), then the memory probe when it is due (at
most once per ISABELLE_MEMORY_PROBE_MIN_INTERVAL_S, skipped on a corpse,
where it would burn its whole budget). A dead prover can answer a step with
stale output, so its result is overridden to a failure with
``error_kind="prover_died"`` and the session is discarded (next call 404).
Error texts are labelled with ``classify_error_kind`` so a client can tell
"out of memory" from a proof error.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Tuple

from server.app.core.logging import get_logger
from server.app.services.session_memory import check_prover_dead, probe_memory
from server.app.services.success_checker import (
    ERROR_KIND_PROVER_DIED,
    classify_error_kind,
)

logger = get_logger(__name__)

PROVER_DIED_TEXT = (
    "prover process died (the session's ML process is gone — out of memory "
    "or killed); the session has been discarded, open a new one"
)


def run_with_memory_hooks(
    fn: Callable[[], Any], session: Any, session_manager: Any
) -> Tuple[Any, Optional[str]]:
    """Run ``fn`` (a blocking backend call), then the hooks. Returns
    ``(result, override)`` where ``override`` is ``"prover_died"`` or None.
    Call via ``asyncio.to_thread``."""
    result = fn()
    try:
        if check_prover_dead(session):
            logger.error("prover died session_id=%s; discarding", session.session_id)
            try:
                session_manager.discard_dead(session.session_id)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("discard_dead failed for %s", session.session_id)
            return result, ERROR_KIND_PROVER_DIED
        probe_memory(session)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("memory hook failed (ignored)")
    return result, None


def kind_for(
    error_text: Optional[str],
    session_manager: Any,
    *,
    timed_out: bool = False,
    override: Optional[str] = None,
) -> Optional[str]:
    """``error_kind`` for a result: the hook's override wins, else the text
    classifier with the monitor's cached tier (no I/O)."""
    if override:
        return override
    try:
        state = str(session_manager.memory.last_state)
    except Exception:  # pylint: disable=broad-exception-caught
        state = "unknown"
    return classify_error_kind(error_text, memory_state=state, timed_out=timed_out)


def first_error_text(commands: Any) -> Optional[str]:
    """The first error message of a chunk report's commands, for classification."""
    for c in commands or []:
        for m in c.get("messages") or []:
            if m.get("sev") == "error" and m.get("text"):
                return str(m["text"])
    return None
