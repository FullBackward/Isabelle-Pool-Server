"""Regex gatekeeping for the diagnostic-command passthrough endpoint.

`POST /sessions/{id}/diagnostic` runs an ARBITRARY Isar command transiently
(insert edit -> read its writeln/state output -> discard the edit). Because the
string is processed by the prover, we must refuse anything that could execute
code, touch the filesystem, or smuggle a second (non-diagnostic) command past
the transient contract. Two layers:

  1. Allowlist (positive): the LEADING command keyword must be a known read-only
     diagnostic -- the ``print_*`` / ``find_*`` families (Isabelle's naming
     convention for ``Toplevel.keep`` inspectors) plus an explicit set of
     non-prefixed ones (``thm``, ``term``, ``prop``, ``typ``, ``prf``, ...).
  2. Denylist (defense in depth): NO ML-evaluating / IO / setup keyword may
     appear anywhere as a standalone token. This blocks ``thm foo\\nML ‹...›``
     style injection where a second command would still execute BEFORE the edit
     is discarded -- the only genuine code-execution risk left once the first
     keyword is allowlisted.

Both sets live here so a new Isabelle version / new diagnostic is a one-line
edit. ``validate_diagnostic_command`` raises ``ValueError`` on rejection; the
Pydantic request model surfaces that as HTTP 422.

Trade-off: the denylist is intentionally conservative and token-based, so a
legitimate but unusual query such as ``find_theorems name: ML`` is also refused.
Safety is preferred over that rare convenience; loosen the denylist here if it
proves too aggressive.
"""
from __future__ import annotations

import os
import re
from typing import Final

# Leading-keyword allowlist ---------------------------------------------------
# print_*/find_* are, by Isabelle convention, read-only inspectors. This prefix
# rule covers the whole family across Pure, HOL and the AFP without enumerating
# every command (print_theorems, find_theorems, print_induct_rules, ...).
_ALLOWED_PREFIX_RE: Final = re.compile(r"^(?:print|find)_[A-Za-z0-9_]+$")

# Non-prefixed diagnostics from Pure.thy section "Diagnostic commands" and
# section "Dependencies", plus the common HOL search command unused_thms.
_ALLOWED_EXPLICIT: Final = frozenset(
    {
        "thm", "prf", "full_prf", "prop", "term", "typ",
        "help", "welcome",
        "thy_deps", "class_deps", "locale_deps", "thm_deps", "thm_oracles",
        "unused_thms",
    }
)

# Denylist: code-execution / filesystem / setup keywords, and `axiomatization`
# (report review 2026-10-27: a chunk that postulates an axiom and uses it is
# "proved" in-session and accepted by a cold build alike; a candidate proof has
# no business adding axioms). Rejected if they occur ANYWHERE as a standalone
# token. Word boundaries use Isar identifier chars (letters, digits,
# underscore, prime) so "ML" inside "print_ML_antiquotations" does NOT match
# (it is preceded by '_').
_DANGEROUS_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_'])(?:"
    r"axiomatization|"
    r"ML|ML_val|ML_command|ML_prf|ML_file(?:_debug|_no_debug)?|ML_export|"
    r"SML_file(?:_debug|_no_debug)?|SML_export|SML_import|"
    r"setup|local_setup|declaration|syntax_declaration|"
    r"attribute_setup|method_setup|simproc_setup|oracle|"
    r"parse_translation|print_translation|typed_print_translation|"
    r"parse_ast_translation|print_ast_translation|"
    r"external_file|generate_file|compile_generated_files|"
    r"export_generated_files|scala_build_generated_files"
    r")(?![A-Za-z0-9_'])"
)

# First run of command-name characters (the outer-syntax keyword).
_HEAD_RE: Final = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)")

# Control characters other than tab/newline are never valid in a command.
_CONTROL_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

MAX_LEN: Final = int(os.getenv("ISABELLE_DIAGNOSTIC_MAX_LEN", "4000"))


def validate_diagnostic_command(command: str) -> str:
    """Return the stripped command if it is a safe read-only diagnostic, else
    raise ``ValueError``. See the module docstring for the policy."""
    if not isinstance(command, str):
        raise ValueError("command must be a string")
    cmd = command.strip()
    if not cmd:
        raise ValueError("command must not be empty")
    if len(cmd) > MAX_LEN:
        raise ValueError(f"command too long (> {MAX_LEN} chars)")
    if _CONTROL_RE.search(cmd):
        raise ValueError("command contains disallowed control characters")

    head_match = _HEAD_RE.match(cmd)
    if not head_match:
        raise ValueError("command must start with a diagnostic keyword")
    head = head_match.group(1)
    if not (head in _ALLOWED_EXPLICIT or _ALLOWED_PREFIX_RE.match(head)):
        raise ValueError(
            f"'{head}' is not an allowed diagnostic command "
            "(expected thm/term/prop/typ/prf, or a print_*/find_* inspector)"
        )

    danger = _DANGEROUS_RE.search(cmd)
    if danger:
        raise ValueError(
            f"command contains a disallowed keyword '{danger.group(0)}' "
            "(code-execution / IO commands are not permitted)"
        )
    return cmd
