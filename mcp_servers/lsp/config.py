"""Configuration for the Isabelle Pool Server LSP-like MCP server (all via env, prefix ISABELLE_MCP_LSP_)."""
from __future__ import annotations

from ..common.config import env_bool, env_float, env_int, env_str

_P = "ISABELLE_MCP_LSP_"


class Config:
    # Where the running Isabelle Pool Server lives.
    GYM_URL: str = env_str(_P, "GYM_URL", "http://localhost:8000")
    DEFAULT_FIELD: str = env_str(_P, "FIELD", "HOL")
    DEFAULT_TASK_GROUP: str = env_str(_P, "TASK_GROUP", "default")

    # httpx timeout for the underlying client (must exceed load/build budgets).
    HTTP_TIMEOUT: float = env_float(_P, "HTTP_TIMEOUT", 600.0)
    # Wall budget (s) for load_document syncs and per-candidate attempt verification.
    LOAD_TIMEOUT: float = env_float(_P, "LOAD_TIMEOUT", 120.0)
    ATTEMPT_TIMEOUT: float = env_float(_P, "ATTEMPT_TIMEOUT", 180.0)

    # Concurrency cap for multi_attempt fan-out (bounded by the server pool).
    MAX_PARALLEL: int = env_int(_P, "MAX_PARALLEL", 4)
    # Warm scratch sessions kept per context (task_group, heap, imports, field);
    # reused across calls — each use is a load_document reset.
    SCRATCH_POOL_SIZE: int = env_int(_P, "SCRATCH_POOL_SIZE", 4)
    # Max seconds a caller waits for a scratch slot once the pool is at its cap
    # (a legitimate wait is one other candidate's verification); expiry raises a
    # clear error instead of hanging forever (Bug 17 / audit MCP-1).
    SCRATCH_WAIT_TIMEOUT: float = env_float(_P, "SCRATCH_WAIT_TIMEOUT", ATTEMPT_TIMEOUT + 60.0)

    # When true, isabelle_close destroys the session (immediate teardown,
    # freeing memory) instead of the default warm release back to the pool.
    # The per-call `destroy` argument on isabelle_close overrides this.
    CLOSE_DESTROYS: bool = env_bool(_P, "CLOSE_DESTROYS", False)

    # Transport: "stdio" (local) or "streamable-http" (remote).
    TRANSPORT: str = env_str(_P, "TRANSPORT", "stdio")
    HOST: str = env_str(_P, "HOST", "127.0.0.1")
    PORT: int = env_int(_P, "PORT", 8849)
