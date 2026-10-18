"""Success evaluation helpers with explicit warning separation (server-side
classifier for small-step / bigstep results; formerly server_gym/, moved into
the server package 2026-09-27 so the server has no dependency outside itself).
"""
import re
from typing import List, Optional, Tuple

from server.app.core.config import Memory
from server.repl.src.python.repl_backend_gateway import ReplResult

# --- sledgehammer outcome (one classifier for both routes and the fill job) -------

SH_PROVED = "proved"
SH_NONE = "none"
SH_TIMEOUT = "timeout"
SH_ERROR = "error"

# "Try this: by (metis foo) (12 ms)" / "(0.4 ms)" / "(1.2 s)": the trailing timing
# is not part of the method text that gets pasted into a proof.
_TRY_THIS_RE = re.compile(r"Try this:\s*(.+?)\s*(?:\(\s*[\d.]+\s*(?:ms|s)\s*\))?\s*$")


def classify_sledgehammer(lines: Optional[List[str]]) -> Tuple[str, Optional[str]]:
    """``(outcome, method)`` of the lines a sledgehammer run printed.

    ``proved`` with the first ``Try this:`` method text (timing suffix cut);
    else ``none`` when sledgehammer said "No proof found"; else ``timeout`` when
    only "Timed out" remains; ``error`` for a line starting with "error=" or an
    empty answer that carried an error. An empty list is ``none``.
    Note: a false goal prints BOTH "No proof found" and "Timed out" — that is
    ``none`` (sledgehammer's own verdict), not ``timeout``.
    """
    rows = [str(l).strip() for l in (lines or []) if str(l).strip()]
    for row in rows:
        m = _TRY_THIS_RE.search(row)
        if m:
            return SH_PROVED, m.group(1).strip()
    lowered = [r.lower() for r in rows]
    if any(r.startswith("error=") for r in rows):
        return SH_ERROR, None
    if any("no proof found" in r for r in lowered):
        return SH_NONE, None
    if any("timed out" in r or "timeout" in r for r in lowered):
        return SH_TIMEOUT, None
    return SH_NONE, None


ERROR_KIND_MEMORY = "memory"
ERROR_KIND_PROVER_DIED = "prover_died"


def classify_error_kind(
    error_text: Optional[str], *, memory_state: str = "unknown", timed_out: bool = False
) -> Optional[str]:
    """Label an error as ``"memory"`` when the ML runtime ran out of memory.

    Matches the configured substrings (Memory.ERROR_PATTERNS, case-insensitive);
    a bare ``Interrupt`` counts only while the server is at ``critical`` AND the
    result is not already a timeout — a JVM wall-budget expiry (Bug 15) also
    ends in an interrupted, rolled-back command and keeps its own
    classification. Returns None for anything else (plain proof errors).
    """
    if not error_text or timed_out:
        return None
    lower = error_text.lower()
    if any(p.lower() in lower for p in Memory.ERROR_PATTERNS):
        return ERROR_KIND_MEMORY
    if "interrupt" in lower and memory_state == "critical":
        return ERROR_KIND_MEMORY
    return None


def get_raw_error_output(result: ReplResult) -> str:
    """Return raw stderr-like output without filtering."""
    try:
        separated = result.separated_output()
        value = separated.error()
        return "" if value is None else value.strip()
    except Exception:
        return ""


def get_raw_output(result: ReplResult) -> str:
    """Return raw stdout-like output."""
    try:
        separated = result.separated_output()
        value = separated.output()
        return "" if value is None else value.strip()
    except Exception:
        return ""


def _normalized_prefix(line: str) -> str:
    stripped = line.strip()
    while stripped.startswith("*"):
        stripped = stripped[1:].lstrip()
    return stripped.lower()


def _is_warning_line(line: str) -> bool:
    prefix = _normalized_prefix(line)
    if prefix == "" or prefix.startswith("###"):
        return True
    warning_prefixes = (
        "warning:",
        "warning -",
        "warning —",
        "ml warning",
        "note:",
        "introduced fixed type variable(s):",
    )
    return any(prefix.startswith(p) for p in warning_prefixes)


def split_error_and_warning_output(result: ReplResult) -> Tuple[str, str]:
    """
    Split stderr-like output into:
      (error_text, warning_text)

    Success checking only consults the error half.
    """
    raw = get_raw_error_output(result)
    if not raw:
        return "", ""

    lines = raw.splitlines()
    significant = [ln for ln in lines if ln.strip() and not ln.strip().startswith("###")]
    if not significant:
        return "", ""

    # If the first significant line is warning-like, treat the whole diagnostic
    # block as a warning. This covers messages like:
    #   *** Introduced fixed type variable(s): ...
    first_prefix = _normalized_prefix(significant[0])
    if (
        first_prefix.startswith("warning:")
        or first_prefix.startswith("warning -")
        or first_prefix.startswith("warning —")
        or first_prefix.startswith("ml warning")
        or first_prefix.startswith("note:")
        or first_prefix.startswith("introduced fixed type variable(s):")
    ):
        return "", raw.strip()

    warning_lines = []
    error_lines = []
    for line in lines:
        if _is_warning_line(line):
            warning_lines.append(line)
        else:
            error_lines.append(line)

    return "\n".join(error_lines).strip(), "\n".join(warning_lines).strip()


def has_error_output(result: ReplResult) -> bool:
    """Check whether ReplResult has real error output (warnings do not count)."""
    try:
        error_output, _warning_output = split_error_and_warning_output(result)
        return len(error_output) > 0
    except Exception:
        return True


def is_syntax_successful(result: ReplResult) -> bool:
    """Check if syntax execution is successful."""
    return not has_error_output(result)


def is_proof_progress(before_subgoals: List[str], after_subgoals: List[str]) -> bool:
    """Check if there is real proof progress."""
    if len(after_subgoals) < len(before_subgoals):
        return True

    if len(after_subgoals) == len(before_subgoals):
        return after_subgoals != before_subgoals

    if len(after_subgoals) > len(before_subgoals):
        return len(before_subgoals) > 0

    return False


def get_error_message(result: ReplResult) -> str:
    """Get error message from ReplResult, excluding warning-only diagnostics."""
    try:
        error_output, _warning_output = split_error_and_warning_output(result)
        return error_output
    except Exception:
        return "Unknown error occurred"


def get_warning_message(result: ReplResult) -> str:
    """Get warning message from ReplResult."""
    try:
        _error_output, warning_output = split_error_and_warning_output(result)
        return warning_output
    except Exception:
        return ""


def get_output_message(result: ReplResult) -> str:
    """Get output message from ReplResult."""
    try:
        separated = result.separated_output()
        value = separated.output()
        return "" if value is None else value.strip()
    except Exception:
        return ""
