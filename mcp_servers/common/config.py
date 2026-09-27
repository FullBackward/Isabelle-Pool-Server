"""Env-var helpers shared by the MCP server configs.

Each server keeps its own prefix (``ISABELLE_MCP_`` for stepwise,
``ISABELLE_MCP_LSP_`` for the LSP server) so the two can run side by side with
different gym URLs / ports; the reading and casting is shared here so a knob
behaves identically in both.
"""
from __future__ import annotations

import os

_TRUE = {"1", "true", "yes", "on"}


def env_str(prefix: str, name: str, default: str) -> str:
    return os.environ.get(prefix + name, default)


def env_int(prefix: str, name: str, default: int) -> int:
    return int(os.environ.get(prefix + name, str(default)))


def env_float(prefix: str, name: str, default: float) -> float:
    return float(os.environ.get(prefix + name, str(default)))


def env_bool(prefix: str, name: str, default: bool = False) -> bool:
    raw = os.environ.get(prefix + name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE
