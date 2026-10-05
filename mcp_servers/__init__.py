"""Isabelle Pool Server MCP servers (agent-facing layers over the HTTP client only).

- ``mcp_servers.lsp``      file-sync (LSP-style) workflow: ``python -m mcp_servers.lsp.app``
- ``mcp_servers.stepwise`` chunk-centric workflow:       ``python -m mcp_servers.stepwise.app``
- ``mcp_servers.common``   shared config/env helpers and the gym-client mixin

Both servers depend on ``client`` only — never on ``server`` or ``repl``
(enforced by tests/test_dependency_rules.py). The former top-level packages
``mcp_lsp_server`` / ``mcp_stepwise_server`` (re-export shims kept for one
release after the 2026-09-27 merge) were removed on 2026-10-05.

The package is deliberately NOT named ``mcp``: that name is the MCP Python SDK
both servers import (``mcp.server.fastmcp``).
"""
