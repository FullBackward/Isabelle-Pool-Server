"""Configuration for the Isabelle Pool Server chunk-centric MCP server (all via env, prefix ISABELLE_MCP_)."""
from __future__ import annotations

from ..common.config import env_float, env_int, env_str

_P = "ISABELLE_MCP_"


class Config:
    # Where the running Isabelle Pool Server lives.
    GYM_URL: str = env_str(_P, "GYM_URL", "http://localhost:8000")
    DEFAULT_FIELD: str = env_str(_P, "FIELD", "HOL")

    # Concurrency cap for verify_batch fan-out. Kept conservative to respect the server's
    # memory admission gate. Never exceeds the server pool size in practice.
    MAX_PARALLEL: int = env_int(_P, "MAX_PARALLEL", 4)

    # Default single wall budget (s) for verify_chunk / each verify_batch item.
    CHUNK_TIMEOUT: float = env_float(_P, "CHUNK_TIMEOUT", 180.0)
    # httpx timeout for the underlying client (must exceed CHUNK_TIMEOUT + server grace).
    HTTP_TIMEOUT: float = env_float(_P, "HTTP_TIMEOUT", 600.0)

    # Transport: "stdio" (local) or "streamable-http" (remote).
    TRANSPORT: str = env_str(_P, "TRANSPORT", "stdio")
    HOST: str = env_str(_P, "HOST", "127.0.0.1")
    PORT: int = env_int(_P, "PORT", 8848)
