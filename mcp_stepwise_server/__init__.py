"""DEPRECATED shim — the chunk-centric MCP server moved to ``mcp_servers.stepwise`` (2026-09-27).

``python -m mcp_stepwise_server.app`` and ``from mcp_stepwise_server.pool import
SessionPool`` keep working for one release via the re-exports in this package;
switch to ``mcp_servers.stepwise.app`` / ``mcp_servers.stepwise.pool``.
(The even older name ``mcp_server`` is gone.)
"""
