"""Success evaluation helper functions with explicit warning separation."""
from typing import List, Optional, Tuple

from repl.src.python.repl_backend_gateway import ReplResult


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
