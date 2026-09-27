"""DEPRECATED shim — the LSP MCP server moved to ``mcp_servers.lsp`` (2026-09-27).

``python -m mcp_lsp_server.app`` and ``from mcp_lsp_server.pool import LspPool``
keep working for one release via the re-exports in this package; switch to
``mcp_servers.lsp.app`` / ``mcp_servers.lsp.pool``.
"""
