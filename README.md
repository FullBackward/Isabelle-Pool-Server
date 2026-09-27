# IsabelleGym Server

A server-side Isabelle proof verification system for training and evaluating LLM-based
provers. It wraps the Isabelle theorem prover behind a FastAPI HTTP server supporting
**small-step** (stepwise REPL execution with checkpoints/rollback), **chunk**
(`verify_chunk`: a whole proof chunk in one PIDE edit with per-command status), and
**big-step** (whole `.thy` file verification via `isabelle build`) workflows, plus an
**MCP server** that exposes it all to LLM agents.

Based on IsabelleGym 1.0 by Tom Milan (University of Cambridge) and IsabelleGym 2.0 by
Zijing Li (University of Edinburgh); this server iteration is implemented by Xuanwei Ren
(University of Edinburgh).

Components:

| Path | What it is |
|---|---|
| `server/` | FastAPI service: session pool, leases, memory admission, metrics (`server/app/api/v1/routes/` holds the endpoints) |
| `server/repl/` | Scala/ML Isabelle REPL backend (PIDE sessions, one shared gateway JVM); launched only by the server |
| `client/` | Async Python HTTP client (`IsabelleGymAsyncClient`); its own package (`pip install -e ./client`), httpx only, never imports server code |
| `mcp_servers/` | MCP servers for LLM agents: `lsp/` (file-sync, the one the humanize harness uses), `stepwise/` (chunk-centric), `common/` shared bits. `mcp_lsp_server/` and `mcp_stepwise_server/` are deprecated launch/import shims |
| `deploy/` | Dockerfiles, `setup.sh`, RC0 image scripts, Prometheus/Grafana/cAdvisor configs (`docker-compose.yml` stays at the root) |
| `evaluation/` | Benchmark CLIs, `results/benchmark_runs.json` (consolidated runs), `MCP-comparison/` harness |
| `examples/` | Demo notebook, figures, heap demo project |
| `tests/` | Unit tests (no Isabelle needed); run the full suite inside the container |
| `docs/` | `DESIGN_CHOICES.md`, `ISSUES.md` (bug log + work log), `devnote.md` (experiment notes) |
| `archive/` | Read-only history: IsabelleGym 1.0 sources, the 2.0 in-process gym and its baseline scripts, the unmaintained `install.sh` |

Design rationale for the architecture lives in [DESIGN_CHOICES.md](docs/DESIGN_CHOICES.md);
the living bug log is [ISSUES.md](docs/ISSUES.md).

---

## Installing the server on an Ubuntu server (command line)

Works on x86-64 and ARM64 Ubuntu (20.04+). The Docker build auto-selects the matching
Isabelle distribution for your CPU architecture. Budget ~30 GB disk for the image + heaps
and ideally 16 GB+ RAM (the compose file caps the container at 14 GB; adjust for smaller
machines — see step 5).

The fastest path is the setup script, which does configuration, build, start, and a
health check in one go:

```bash
git clone https://github.com/FullBackward/IsabelleGym.git
cd IsabelleGym
./deploy/setup.sh            # add --verify for a smoke test, --build-heaps "HOL-Library" to prebuild heaps
```

The manual equivalent is documented below (the script does exactly these steps).

### 1. Prerequisites

The server itself runs entirely inside Docker — nothing else (no Python, no JDK, no
Isabelle) needs to be installed on the host. The host needs only the components below.
**Check each one first and skip it if it is already installed on your server.**

| Component | Check with | Needed for |
|---|---|---|
| `git` | `git --version` | cloning the repository |
| `curl`, `ca-certificates` | `curl --version` | fetching the Docker apt key; health checks |
| Docker Engine (20.10+) | `docker --version` | running the container |
| Docker Compose plugin (v2) | `docker compose version` | building/starting the stack |

**a. git + curl** (skip if present):

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates
```

**b. Docker Engine + Compose plugin** (skip if both checks above pass; note that the
legacy `docker-compose` v1 binary also works — substitute `docker-compose` for
`docker compose` in every command below):

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

If Docker Engine is already installed but `docker compose version` fails, you only need
the plugin: `sudo apt-get install -y docker-compose-plugin`.

**c. Run docker without sudo** (skip if `docker ps` already works for your user;
re-login after this for the group change to take effect):

```bash
sudo usermod -aG docker "$USER"
```

### 2. Clone and configure

```bash
git clone https://github.com/FullBackward/IsabelleGym.git
cd IsabelleGym
```

Runtime configuration lives in `.env` at the repo root (loaded into the container via
`env_file`). `./deploy/setup.sh` creates it from the annotated **`.env.example`** (which
documents every knob) and generates a random `ISABELLE_ADMIN_TOKEN` for the admin
console. To do it by hand: `cp .env.example .env` and review. The knobs you most
likely want to check:

```bash
ISABELLE_POOL_SIZE=3              # max concurrent Isabelle sessions (each ~1.5-4 GB)
ISABELLE_INITIAL_SESSIONS=0       # sessions pre-warmed at startup (0 = fast startup)
ISABELLE_MEMORY_PRESSURE_THRESHOLD=80.0   # refuse new sessions above this used-%
ISABELLE_ML_MAXHEAP_MB=9216       # hard per-process ML heap cap (Poly/ML --maxheap)
ISABELLE_ADMIN_TOKEN=...          # admin console / admin endpoints credential
```

### 3. Build the image

```bash
docker compose build isabelle-gym
```

This downloads the Isabelle 2025-2 distribution (~1.2 GB). The official server
(`isabelle.in.tum.de`) is sometimes down; the build automatically falls back through
mirrors (Clarkson → Cambridge → Proofcraft). The build takes 10–30 minutes (Isabelle
download + Scala backend build).

### 4. Start the server

The container entrypoint registers the Isabelle components, writes the ML heap cap
into the user settings, and **starts the API server automatically**:

```bash
docker compose up -d isabelle-gym

# wait a minute or two (gateway JVM spawn), then check:
curl http://localhost:8000/healthz     # {"status":"alive"}
curl http://localhost:8000/            # full health: gateway_alive, pool, memory
```

With `ISABELLE_INITIAL_SESSIONS=0`, the first session request pays the session-creation
cost (~1 min). Optional but recommended if your workload imports heavy sessions (e.g.
`HOL-Computational_Algebra`): prebuild their heaps once so session creation and big-step
verification start from a cached image (`./deploy/setup.sh --build-heaps "..."` wraps this):

```bash
docker compose exec isabelle-gym isabelle build -b HOL-Computational_Algebra
```

### 5. Operating notes

- **Logs:** the server runs in the container foreground, so the canonical live log is
  `docker compose logs -f isabelle-gym`. The rotating file log (10 MB × 5) is also at
  `logs/server.log` in the repo (the repo is volume-mounted at `/app`):

  ```bash
  docker compose logs -f isabelle-gym                        # stdout of the server
  tail -f logs/server.log                                    # rotating file log, from the host
  ```
- **Admin console:** `http://localhost:8000/admin` — pool contents, base heap images,
  force-close. Token-gated operations use `ISABELLE_ADMIN_TOKEN` from `.env` (injected
  into the page automatically when set; without it, destructive actions render locked).
- **ML heap cap:** the entrypoint writes `ML_OPTIONS="--minheap 500 --enablegcsharing
  --maxheap $ISABELLE_ML_MAXHEAP_MB"` into the Isabelle user settings (default 9 GB).
  Any single poly process exceeding it fails gracefully instead of OOM-killing the
  container. It must live in the user settings file — the polyml component clobbers
  env-var values. Tune via `ISABELLE_ML_MAXHEAP_MB` in `.env`.
- **Metrics:** Prometheus metrics at `/metrics`; a full Prometheus+Grafana+cAdvisor stack
  is included — `docker compose up -d` starts everything, Grafana on `:3000`.
- **Memory limit:** `mem_limit: 14g` in `docker-compose.yml` — must stay BELOW the Docker
  VM's own memory or the cgroup-aware gate goes blind. On smaller machines lower it
  AND lower `ISABELLE_POOL_SIZE`; the admission gate refuses new sessions near the limit
  instead of letting the OOM killer take the JVM.
- **Changing `.env`:** requires recreating the container, not just restarting it:
  `docker compose up -d --force-recreate isabelle-gym`.
- **After an image rebuild**, if the server fails with `Not found: py4j`: a pre-existing
  named volume shadows the component registration. The container entrypoint
  (`server/server/repl/Admin/container_entrypoint.sh`) re-registers automatically on every start; the
  manual fix is `docker compose exec isabelle-gym ./server/repl/Admin/init`
  (docs/ISSUES.md Bug 7).
- **Remote access:** the API listens on `0.0.0.0:8000` with no authentication — keep it
  firewalled (`sudo ufw allow from <your-ip> to any port 8000`) or tunnel over SSH.

### 6. Isabelle 2026-RC0 track

`main` tracks Isabelle 2025-2. The `2026-RC0` branch carries the same server features
plus the compatibility patch set for the Isabelle 2026 release candidate (its own
`deploy/Dockerfile.rc0`, Scala API adjustments). Everything above applies identically — check
out that branch and run the same commands. A pre-built turnkey image (heaps included,
for reproducing published results) is distributed separately; see
`deploy/RC0-image-instructions.md`.

---

## Connecting the MCP server to an agent

The MCP server is a thin agent-facing layer over the HTTP API:

```
LLM agent  ⇄  MCP server (stdio or streamable-HTTP)  ⇄  IsabelleGym HTTP server  ⇄  Isabelle
```

Sessions and leases are managed automatically per MCP connection — the agent never sees a
`session_id`. Each connection gets an isolated Isabelle session; a fresh `enter_theory`
always starts from a clean document.

### Prerequisites (on the machine that runs the agent/MCP client)

```bash
cd IsabelleGym
pip install -r mcp_servers/requirements.txt httpx
```

The MCP server needs two things at runtime: `PYTHONPATH` pointing at the repo root (so
`client` and `mcp_servers` import), and the gym server URL (default
`http://localhost:8000`). The gym server must be running (previous section).

### Option A — stdio (local agents: Claude Code, Claude Desktop, Cursor)

The client spawns the MCP server as a subprocess; one process per connection.

**Claude Code** (either command works):

```bash
claude mcp add isabellegym \
  --env PYTHONPATH=/absolute/path/to/IsabelleGym \
  --env ISABELLE_MCP_GYM_URL=http://localhost:8000 \
  -- python -m mcp_servers.stepwise.app
```

or drop a `.mcp.json` in your project:

```json
{
  "mcpServers": {
    "isabellegym": {
      "command": "python",
      "args": ["-m", "mcp_servers.stepwise.app"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/IsabelleGym",
        "ISABELLE_MCP_GYM_URL": "http://localhost:8000"
      }
    }
  }
}
```

**Claude Desktop:** add the same JSON block under `mcpServers` in
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows).

**Cursor:** same block in `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global).

**Any other MCP client / your own agent loop:** spawn
`python -m mcp_servers.stepwise.app` over stdio with those two env vars. If your agent framework
uses the `mcp` Python SDK, `evaluation/MCP-comparison/common/mcp_client.py` is a minimal working
example (spawn → `initialize` → `tools/list` → `tools/call`).

### Option B — streamable-HTTP (remote server, multiple agents)

Run the MCP server as a standalone service next to the gym server:

```bash
cd IsabelleGym
PYTHONPATH=. ISABELLE_MCP_TRANSPORT=streamable-http \
  ISABELLE_MCP_HOST=0.0.0.0 ISABELLE_MCP_PORT=8848 \
  python -m mcp_servers.stepwise.app
```

Point HTTP-capable MCP clients at `http://<server>:8848/mcp`. Concurrent connections are
isolated from each other (per-connection sessions). Like the gym API, there is no built-in
auth — firewall the port or tunnel:

```bash
# from the agent machine:
ssh -N -L 8848:localhost:8848 user@your-server
```

### What the agent gets

| Tool | Purpose |
|---|---|
| `enter_theory(name, imports, field)` | Start (or restart) a proof session; begins the theory header for you |
| `verify_chunk(text, timeout, detail)` | **The one execution tool.** Submit one command or a whole proof; returns per-command status (`ok/failed/running/unprocessed`), names the stuck line on timeout, auto-rolls-back failures |
| `proof_state()` | Current open subgoals |
| `source()` | The theory source as the prover sees it |
| `diagnostic(command)` | Read-only queries (`thm`, `term`, `find_theorems`, `print_*`) — transient, never touches the proof |
| `sledgehammer(timeout_s)` | Automated proof search on the open goal; returns ready-to-paste methods |
| `checkpoint()` / `restore(id)` / `rollback()` | State management |
| `verify_batch(items, max_parallel)` | Check many independent chunks concurrently across the session pool |
| `close_theory()` | Dispose this connection's session |

The one rule agents must respect (it is spelled out in the tool outputs too):
**a theorem is proved only when `verify_chunk` reports `success=True` AND
`proof_open=False` AND `used_sorry=False`.** `success=True` alone means "no command
errored" — an open or `sorry`-closed proof is NOT a result.

A typical agent flow:

```
enter_theory(name="Scratch", imports=["Main"])
verify_chunk("theorem foo: \"rev (rev xs) = xs\"\n  by (induct xs) auto")
  → success=True proof_open=False used_sorry=False   ✓ proved
# or, when stuck:
verify_chunk("theorem bar: ...\nproof -\n  have step1: ... by simp")
  → success=True proof_open=True                     (open goal kept)
sledgehammer()                                        → "by (metis ...)"
verify_chunk("  show ?thesis by (metis ...)\nqed")
```

### MCP configuration reference (env vars)

| Variable | Default | Meaning |
|---|---|---|
| `ISABELLE_MCP_GYM_URL` | `http://localhost:8000` | The gym HTTP server |
| `ISABELLE_MCP_FIELD` | `HOL` | Default Isabelle session for new theories |
| `ISABELLE_MCP_CHUNK_TIMEOUT` | `180` | Default wall budget (s) per `verify_chunk` |
| `ISABELLE_MCP_HTTP_TIMEOUT` | `600` | httpx timeout (must exceed chunk timeout) |
| `ISABELLE_MCP_MAX_PARALLEL` | `4` | Cap for `verify_batch` fan-out |
| `ISABELLE_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `ISABELLE_MCP_HOST` / `ISABELLE_MCP_PORT` | `127.0.0.1` / `8848` | HTTP transport bind |

### Smoke test

With the gym server running, verify the MCP layer end-to-end without any agent:

```bash
cd IsabelleGym
PYTHONPATH=. python - <<'EOF'
import asyncio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

async def main():
    params = StdioServerParameters(command="python", args=["-m", "mcp_servers.stepwise.app"],
                                   env={"PYTHONPATH": ".", "ISABELLE_MCP_GYM_URL": "http://localhost:8000"})
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("tools:", [t.name for t in (await s.list_tools()).tools])
            await s.call_tool("enter_theory", {"name": "Smoke", "imports": ["Main"]})
            out = await s.call_tool("verify_chunk",
                {"text": 'theorem t: "rev (rev xs) = xs" by (induct xs) auto'})
            print(out.content[0].text)
            await s.call_tool("close_theory", {})

asyncio.run(main())
EOF
```

Expected: the tool list, then `success=True proof_open=False used_sorry=False ...`.
(The first run pays one-time session creation, ~1 minute.)

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `McpError: Connection closed` immediately | The MCP subprocess died on startup — almost always missing `PYTHONPATH` or missing pip deps. Run `PYTHONPATH=. python -m mcp_servers.stepwise.app` manually to see the traceback. |
| `enter_theory` hangs then errors | Gym server not running / wrong `ISABELLE_MCP_GYM_URL`; or the first session for a heavy import set is building its heap — prebuild it (install step 4). |
| HTTP 503 "memory pressure" from tools | The admission gate is protecting the container — lower `ISABELLE_POOL_SIZE`, raise `mem_limit`, or wait for idle sessions to be evicted. |
| `success=True` but the agent isn't done | Working as intended: check `proof_open` / `used_sorry`. |

---

## Beyond the basics

- **HTTP API directly** (no MCP): see the endpoint modules in `server/app/api/v1/routes/`
  and the client in `client/async_client.py`; API reference PDFs are in the repo root.
- **MCP comparison harness** (this MCP vs Isabelle-MCP vs AutoCorrode I/Q):
  [evaluation/MCP-comparison/README.md](evaluation/MCP-comparison/README.md).
- **Evaluation scripts** for small-step/big-step benchmarking: `evaluation/scripts/`
  (each runs as `python -m evaluation.scripts.<name>`).
- **Developer docs:** [CLAUDE.md](CLAUDE.md) (architecture + conventions),
  [DESIGN_CHOICES.md](docs/DESIGN_CHOICES.md) (rationale), [ISSUES.md](docs/ISSUES.md) (bug log).
