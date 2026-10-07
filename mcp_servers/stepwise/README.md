# Isabelle Pool Server MCP server

Exposes the running Isabelle Pool Server to LLM agents via the **Model Context Protocol**.
It is a thin layer over `client.async_client.PoolAsyncClient` (no core edits beyond the
client wrappers added in Phase 0); it does **not** modify or patch Isabelle.

## What it offers

- **Auto-managed, per-connection sessions** — the agent never handles `session_id`/leases;
  each MCP connection gets its own isolated Isabelle session.
- **Parallelism, both levels**:
  - intra-proof — `verify_chunk` checks a whole chunk with `parallel_proofs=2` and returns
    per-command status (`ok/failed/running/unprocessed`) under a **single** wall budget,
    naming the `stuck_line` on timeout (no opaque timeouts);
  - inter-session — `verify_batch` fans out many independent chunks **concurrently** across
    the server's session pool in one call (bounded by `max_parallel`).

### Tools
`enter_theory`, `verify_chunk` (the one execution tool — one command or a whole proof),
`diagnostic` (read-only queries — `thm`/`term`/`find_theorems`/`print_*`, output that
`verify_chunk` discards), `proof_state`, `source`, `sledgehammer`, `checkpoint`, `restore`,
`rollback`, `close_theory`, `verify_batch`.
### Resources
`isabelle-pool-server://health`, `isabelle-pool-server://sessions`.
### Prompts
`prove_theorem`.

## Run

Prereq: a running Isabelle Pool Server (default `http://localhost:8000`).

```bash
pip install -e ".[mcp]" -e ./client           # MCP SDK (mcp<2) + the async client
export PYTHONPATH=$PWD                        # so `client` imports

# local (stdio) — for Claude Desktop/Code, Cursor:
python -m mcp_servers.stepwise.app

# remote (Streamable HTTP):
ISABELLE_MCP_TRANSPORT=streamable-http ISABELLE_MCP_PORT=8848 python -m mcp_servers.stepwise.app
```

### Register (stdio, e.g. Claude Code / Desktop `mcp` config)
```json
{
  "mcpServers": {
    "isabelle-pool-server": {
      "command": "python",
      "args": ["-m", "mcp_servers.stepwise.app"],
      "env": { "PYTHONPATH": "/path/to/Isabelle-Pool-Server", "ISABELLE_MCP_GYM_URL": "http://localhost:8000" }
    }
  }
}
```

## Config (env)
`ISABELLE_MCP_GYM_URL` (default `http://localhost:8000`), `ISABELLE_MCP_FIELD` (`HOL`),
`ISABELLE_MCP_MAX_PARALLEL` (`4`), `ISABELLE_MCP_CHUNK_TIMEOUT` (`180`),
`ISABELLE_MCP_HTTP_TIMEOUT` (`600`), `ISABELLE_MCP_TRANSPORT` (`stdio`|`streamable-http`),
`ISABELLE_MCP_HOST`, `ISABELLE_MCP_PORT` (`8848`).

## Run an agent proof (Claude in the loop)

`claude-work/impl-mcp-server/bench_mcp_agent.py` drives Claude through these MCP tools to
prove theorems end-to-end and records **success rate, token usage, and latency** per theorem.
It spawns the MCP server itself over stdio, lists its tools, and runs an agentic loop against
the Anthropic API (Claude calls the tools; the harness executes them via MCP and feeds the
results back). Token usage is the only metric that requires the model in the loop.

**Prerequisites**
- A running Isabelle Pool Server (default `http://localhost:8000`) — the MCP layer wraps it.
- `ANTHROPIC_API_KEY` exported in the environment (the model must be in the loop).
- Host deps: `anthropic`, `mcp`, plus the repo's client deps (`httpx`).
- `PYTHONPATH` set to the repo root so `mcp_servers` / `client` import (the harness forwards it
  to the spawned MCP server as the subprocess's `PYTHONPATH`).

**Commands**
```bash
# the two default theorems (rev_rev, gauss_sum), one pass:
ANTHROPIC_API_KEY=... PYTHONPATH=. python claude-work/impl-mcp-server/bench_mcp_agent.py

# stream Claude's reasoning + each tool call/result:
... python claude-work/impl-mcp-server/bench_mcp_agent.py --verbose

# prove one inline statement directly:
... python claude-work/impl-mcp-server/bench_mcp_agent.py \
    --theorem 'theorem foo: "rev (rev xs) = xs"' --name foo --imports Main

# repeat 3x for averages, with Opus:
... python claude-work/impl-mcp-server/bench_mcp_agent.py --repeats 3 --model claude-opus-4-8

# over a miniF2F problem set (and save each proof as a .thy):
... python claude-work/impl-mcp-server/bench_mcp_agent.py \
    --minif2f-glob "evaluation/miniF2F/test/*.json" --limit 5 --save-proofs proofs/
```

Key flags: `--model` (default `claude-sonnet-4-6`), `--max-rounds` (12), `--repeats`,
`--gym-url`, `--problems <json>` / `--minif2f-glob` / `--theorem`, `--select <substr>`,
`--list`, `--limit`, `--output <json>` (default `mcp_bench_results.json`), `--save-proofs <dir>`.
A run counts as proved only when `verify_chunk` reports `success=True` **and** `proof_open=False`
with no `sorry`/`oops` and the chunk contains the target goal.
