"""DEPRECATED launch shim: use ``python -m mcp_servers.lsp.app``."""
from mcp_servers.lsp.app import main, mcp, pool  # noqa: F401  (re-exports)

if __name__ == "__main__":
    main()
