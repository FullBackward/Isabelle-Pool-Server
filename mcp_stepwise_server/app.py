"""DEPRECATED launch shim: use ``python -m mcp_servers.stepwise.app``."""
from mcp_servers.stepwise.app import _render_chunk, main, mcp, pool  # noqa: F401  (re-exports)

if __name__ == "__main__":
    main()
