"""DEPRECATED import shim: use ``mcp_servers.lsp.pool``."""
from mcp_servers.lsp.pool import (  # noqa: F401  (re-exports)
    FileBinding,
    LspPool,
    attempt_prefix,
    canonical_path,
    header_imports,
    is_not_found,
)
