# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Universal Workflow Rules

These rules apply to every user request:

### Rule 1: Plan or research before implementing
For every user request, start with planning, research, or investigation **unless the user explicitly says "implement"**. Do not make core code edits, file modifications, or destructive changes before a plan or research phase has been completed and communicated.

Examples:
- "Fix this bug" → investigate first, then propose a fix.
- "Add a feature" → plan first.
- "How does X work?" → research and explain.
- "Implement X" → proceed directly to implementation.

### Rule 2: Ask before choosing among options
If you present multiple approaches or options to the user, **always ask which one they want before implementing**. Do not proceed with one option on your own just because you prefer it. Wait for the user's explicit choice.

### Rule 3: Sign-off format
End every response with the exact string:

喵(ゝ∀･)⌒☆

## Claude Work Artifacts (read first)

The **testing and demonstration** artifacts Claude produces to show a task is done — ad-hoc test scripts, notebook runners, and the markdown write-up of what was verified — go under `claude-work/`, in a subfolder named for the task (one per feature or bug fix, e.g. `claude-work/impl-sledgehammer/`). Use `impl-<feature>` for features and a short descriptive kebab-case name for bug fixes; include a brief `NOTES.md` summarizing the change and how it was verified. Keep these for reference rather than deleting them.

This is only for the demonstration/test artifacts — not every file touched during the task. Real source changes (e.g. `server/repl/src/ml/REPL.ML`, `examples/demo.ipynb`, files under `server/`, `client/`) stay in their normal locations.

## Project Overview

**IsabelleGym Server** is a containerized system for machine learning research on formal theorem proving. It provides a RESTful API to interact with Isabelle 2025-2 theorem prover through a three-tier architecture:

1. **Scala/ML backend** (server/repl/) - Isabelle REPL wrapping via Scala/ML, exposing interactive proof state operations
2. **FastAPI server** (server/) - RESTful HTTP service with session pooling, resource management, and lease-based concurrency
3. **Python async client** (client/) - User-facing SDK for small-step (interactive) and big-step (batch) proof verification

**Core purpose**: Enable LLM training and evaluation on formal theorem proving via both interactive step-by-step verification and whole-theory batch verification.

## Quick Start (Docker Recommended)

```bash
# Build and start the containerized stack
docker compose up -d --build

# Open a shell in the running container
docker compose exec isabelle-gym bash

# Inside the container, start the server
python -m server.app.main

# From host, verify server is healthy
curl http://localhost:8000/
```

Expected health response:
```json
{
  "service": "IsabelleGym Server",
  "version": "0.0.1",
  "status": "healthy",
  "active_sessions": 0,
  "busy_sessions": 0,
  "max_pool_size": 24,
  "timestamp": "..."
}
```

## Architecture & Key Components

### Backend Layer: server/repl/ (Scala/ML)

The REPL backend bridges Python ↔ Scala ↔ Isabelle/ML. It uses **Py4J** for Java/Python interop and exposes Isabelle's interactive proof state through a custom Scala gateway.

**Key files:**
- server/repl/src/main/scala/repl/repl_backend_gateway.scala - Entry point for Py4J gateway; exposes ReplBackendGateway object with factory methods for creating REPL backends with various configurations (caching, memory management, field selection)
- server/repl/src/main/scala/repl/repl_backend.scala - Core backend class managing Isabelle sessions, state, and proof operations
- server/repl/src/main/scala/repl/server_utils.scala - Low-level Isabelle server/session creation utilities
- server/repl/src/main/scala/repl/repl_ml_communication.scala - Scala ↔ ML communication for fact extraction, subgoal retrieval, and the sledgehammer channel
- server/repl/src/ml/REPL.ML - ML-side proof-state extraction and the sledgehammer entry point invoked from Scala (NOTE: path is src/ml/, not src/main/ml/)
- server/repl/src/main/scala/repl/thy_*.scala - Theory parsing, status tracking, and checkpoint utilities
- server/repl/src/python/repl_backend_gateway.py - Python wrapper (ReplBackendGatewayProcess) that spawns the Scala gateway as a subprocess and manages the Py4J bridge
- server/repl/build.gradle - Gradle build config; depends on Isabelle JAR (auto-built via isabelle scala -e)

**Build process:**
```bash
cd server/repl
chmod +x gradlew
./gradlew build  # Compiles Scala, packages into JAR
```

### Server Layer: server/ (FastAPI)

FastAPI application providing HTTP endpoints for session management (create, acquire, release) and proof execution (small-step command execution, big-step theory verification).

**Architecture pattern: Lease-based concurrency**
- Sessions can be held by multiple clients via leases (X-Lease-Id header)
- Automatic idle timeout and cleanup
- LRU eviction when pool is exhausted

**Key files:**
- server/app/main.py - FastAPI app setup, middleware (request logging, error handlers), lifespan (startup/shutdown with session manager)
- server/app/api/v1/routes/*.py - HTTP endpoints, one module per concern (health, sessions, execution, inspection, positional, automation, checkpoints, heaps); server/app/api/v1/router.py aggregates them and re-exports names for compatibility; api/v1/deps.py holds the `LeasedSession` dependency (lease check + session resolution + log context) and the admin-token / path-segment guards; api/v1/serializers.py holds response-shaping helpers. Source files are capped at 600 lines by tests/test_source_limits.py.
- server/app/api/v1/schemas/API_models.py - Pydantic models for requests/responses
- server/app/services/session_manager.py - Session pool lifecycle, LRU eviction, lease management using OrderedDict for O(1) eviction
- server/app/services/session.py - Per-session state, small-step command execution, checkpoints, rollback
- server/app/services/build_verify.py - Big-step verification via isabelle build CLI
- server/app/services/threaded_backend.py - Thread pool wrapper for concurrent backend operations
- server/app/core/config.py - Configuration via environment variables (pool size, timeouts, caching, logging, field selection)

### Client Layer: client/ (Async Python)

Async HTTP client for end-users. Provides high-level workflows for session creation, command execution, and theory verification.

**Key file:**
- client/async_client.py - IsabelleGymAsyncClient class with methods:
  - Session lifecycle: create_session(), acquire_session(), release_session(), close_session()
  - Small-step: execute_command(), enter_theory(), get_proof_state()
  - Automation: sledgehammer() - runs Isabelle's sledgehammer on the current proof goal; returns success/suggestions/raw_output/execution_time
  - Big-step: verify_bigstep_text(), verify_bigstep_file()
  - Checkpointing: checkpoint_session(), rollback_to_checkpoint()

### Evaluation & Benchmarking: evaluation/

Suite of benchmarking scripts comparing server performance against local IsabelleGym 2.0 baseline and qIsabelle.

**Scripts in evaluation/scripts/:**
- process.py - Preprocessing: normalize Analysis-style imports to fully qualified HOL-Analysis.<Theory> imports
- clean_example_dir.py - Remove documentation keywords (text, section, subsection) from theory files
- eval_smallstep_isabellegym.py - Local baseline (IsabelleGym 2.0)
- eval_smallstep_server_client_1_worker_no_reuse.py - Server small-step with fresh session per theory
- eval_smallstep_server_client_with_reuse.py - Server small-step with pooled session reuse and parallel workers
- eval_smallstep_qisabelle.py - qIsabelle comparison
- eval_bigstep_isabelle_build.py - Local isabelle build baseline
- eval_bigstep_server_client_ver.py - Server big-step verification
- eval_stats.py, theory_splitter.py - Helper modules for stats and theory splitting

## Development Workflow

### Environment Setup (Local, non-Docker)

**Local Python lives in the conda environment named `IsabelleGym`.** There is no
bare `python` on PATH on the host — activate it first (`conda activate IsabelleGym`,
or run one-off commands with `conda run -n IsabelleGym python ...`). Inside the
Docker container, Python is the image interpreter at `/usr/local/bin/python`
(invoke with `docker exec isabelle-gym python ...`), not a conda env.

```bash
# Create and activate venv
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows

# Install dependencies (note: singular "requirement.txt")
pip install --upgrade pip
pip install -r requirement.txt
pip install -e .

# Set PYTHONPATH for imports
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
```

**Requirements:**
- Python 3.12+ (3.10+ for local dev, Docker uses 3.12)
- JDK 17+ (Docker uses OpenJDK 21)
- Isabelle 2025-2 installed and on PATH
- Scala 2.13 or 3.3 (managed by Gradle)

### Building & Testing

**Scala/REPL build:**
```bash
cd server/repl
./gradlew build
```

**Python linting & formatting (dev dependencies):**
```bash
pylint repl server client evaluation  # Lint (ignores venv, import-error, line-too-long)
black repl server client evaluation   # Format (line-length=88)
isort repl server client evaluation   # Sort imports (profile=black)
mypy repl server client evaluation    # Type check (strict=true, disable import-untyped)
```

**Tests:**
```bash
pytest --cov --cov=gym --cov-report=term
```
Note: Current test coverage is limited; most validation is through evaluation scripts.

### Running the Server Locally

```bash
# Terminal 1: Start the server
python -m server.app.main
# or explicitly with Uvicorn:
uvicorn server.app.main:app --host 0.0.0.0 --port 8000

# Terminal 2: Run a client example
python -c "
import asyncio
from client.async_client import IsabelleGymAsyncClient

async def main():
    async with IsabelleGymAsyncClient('http://localhost:8000') as client:
        created = await client.create_session(theories=['Main'], field='HOL')
        print(f'Session: {created[\"session_id\"]}')
        await client.close_session(created['session_id'])

asyncio.run(main())
"
```

### Environment Variables for Server Configuration

Set before starting the server:

```bash
# Pool and concurrency
ISABELLE_POOL_SIZE=24              # Max concurrent sessions (default 24)
ISABELLE_INITIAL_SESSIONS=8        # Pre-warm sessions on startup (default 3)
ISABELLE_IDLE_TIMEOUT=1800         # Session idle timeout in seconds (default 1800)
ISABELLE_MAX_CONCURRENT_SLEDGEHAMMER=4  # Cap in-flight sledgehammers (default ~cores/8); prevents gateway OOM

# Per-session parallel proof checking (repl/.../session_manager.scala, read by Scala via sys.env)
ISABELLE_PARALLEL_PROOFS=2         # Isabelle parallel_proofs per session (default 2): 0=sequential,
                                   # 1=fork top-level proofs, 2=also fork nested have/show bodies.
                                   # VERIFIED effective: ~8x on independent structured proofs.
ISABELLE_SESSION_THREADS=4         # Isabelle `threads` per session (default 4). NOTE: currently a
                                   # no-op -- the poly prover farm is sized from hardware at process
                                   # launch, not from this session option (farm stays ~cores). To
                                   # actually cap per-session threads it must be set at the gateway/
                                   # prover launch layer. See claude-work/impl-parallel-sessions/.

# Caching and memory
ISABELLE_ENABLE_CACHE=false        # Enable session caching (default false)
ISABELLE_MAX_CACHE_SIZE=1          # Max cached sessions per key (default 1)
ISABELLE_ENABLE_MEMORY_MANAGEMENT=true  # Enable Python/cgroup memory management (default true)

# Memory management (Python/cgroup-based; gated by ISABELLE_ENABLE_MEMORY_MANAGEMENT).
# The server measures real container memory via the cgroup (memory.current vs
# memory.max, falling back to host MemTotal when unlimited) and, under pressure,
# evicts idle sessions before admitting new ones — refusing with 503 only if
# nothing idle remains. See server/app/services/memory_monitor.py.
ISABELLE_MEMORY_PRESSURE_THRESHOLD=85.0 # Block new sessions above this used% (default 85.0)
ISABELLE_MEMORY_MIN_AVAILABLE_MB=256    # Also block if available memory below this (default 256)
ISABELLE_MEMORY_FALLBACK_SYSTEM_MB=4096 # Limit used when cgroup + MemTotal unreadable (default 4096)

# Proof state and field
ISABELLE_SHOW_STATES=false         # Include raw proof states in responses (default false)
ISABELLE_DEFAULT_FIELD=HOL         # Default field if not specified (default HOL)

# Server and logging
ISABELLE_SERVER_HOST=0.0.0.0       # Bind address (default 0.0.0.0)
ISABELLE_SERVER_PORT=8000          # Listen port (default 8000)
ISABELLE_SERVER_LOG_LEVEL=INFO     # Log level (default INFO)
ISABELLE_SERVER_LOG_DIR=logs       # Log directory (default logs/)
ISABELLE_SERVER_LOG_FILE=server.log # Log file (default server.log)
ISABELLE_SERVER_MAX_LOG_SIZE_BYTES=10485760  # Rotating log size (default 10MB)
ISABELLE_SERVER_LOG_BACKUP_COUNT=5 # Rotating log backups (default 5)
```

Example: start with smaller pool for testing:
```bash
export ISABELLE_POOL_SIZE=4
export ISABELLE_INITIAL_SESSIONS=2
python -m server.app.main
```

### Monitoring (Prometheus / Grafana / cAdvisor)

The server exposes Prometheus metrics and k8s-style health probes (no extra flags):

- `GET /metrics` — Prometheus metrics. HTTP request count/latency histograms (per route
  template, via `prometheus-fastapi-instrumentator`) plus domain metrics defined in
  `server/app/core/metrics.py`: `isabellegym_sessions_created_total`,
  `isabellegym_sessions_evicted_total{reason}`, `isabellegym_pool_exhausted_total{reason}`,
  `isabellegym_gateway_restarts_total`, `isabellegym_sledgehammer_{total{result},seconds,inflight}`,
  and current-state gauges `isabellegym_sessions_{active,busy,leased}`,
  `isabellegym_memory_{used,limit}_bytes`, `isabellegym_memory_pressure_pct`,
  `isabellegym_gateway_up` (from `SessionManager.get_lru_info()`).
- `GET /healthz` — liveness (always 200 if the process serves).
- `GET /readyz` — readiness: 200 when the gateway is alive, else 503.
- `GET /` — human-readable health summary (sessions, memory, `gateway_alive`).

Bring up the monitoring stack (compose services `prometheus`, `grafana`, `cadvisor`) —
start the server first, then:
```bash
docker compose up -d prometheus grafana cadvisor
# Grafana   http://localhost:3000  (admin/admin) — "IsabelleGym Server" dashboard
# Prometheus http://localhost:9090/targets       — isabelle-gym + cadvisor should be UP
# raw        http://localhost:8000/metrics
```
Config lives under `deploy/monitoring/` (prometheus.yml, Grafana datasource + dashboard
provisioning). `docker-compose.yml` also sets `mem_limit: 12g` on `isabelle-gym` so the
memory admission gate has a real cgroup ceiling and cAdvisor can report OOM events.
Note: the compose service runs `bash`; start the server manually (`python -m
server.app.main`) or Prometheus targets show DOWN until it is up.

### Evaluation Workflow

One-time setup:
```bash
cd /path/to/IsabelleGym
source .venv/bin/activate
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
mkdir -p evaluation/results
CORPUS="evaluation/HOL_corpus/Examples/processed"  # Small example corpus
OUT="evaluation/results"
```

Small-step baseline (local):
```bash
python -m evaluation.scripts.eval_smallstep_isabellegym \
  --repo-root . \
  --corpus "$CORPUS" \
  --output "$OUT/smallstep_isabellegym_aligned.json"
```

Small-step server (session reuse, parallel workers):
```bash
# Start server in another terminal first
python -m evaluation.scripts.eval_smallstep_server_client_with_reuse \
  --corpus "$CORPUS" \
  --server http://localhost:8000 \
  --field HOL \
  --num-workers 4 \
  --output "$OUT/smallstep_server_with_reuse.json"
```

Big-step baseline (local isabelle build):
```bash
python -m evaluation.scripts.eval_bigstep_isabelle_build \
  --corpus "$CORPUS" \
  --isabelle-bin "$(which isabelle)" \
  --parent-session HOL \
  --jobs 4 \
  --output "$OUT/bigstep_isabelle_build.json"
```

Big-step server:
```bash
python -m evaluation.scripts.eval_bigstep_server_client_ver \
  --corpus "$CORPUS" \
  --server http://localhost:8000 \
  --timeout 1800 \
  --field HOL \
  --output "$OUT/bigstep_server_client.json"
```

For HOL-Analysis corpus, use --field HOL-Analysis and adjust import paths via process.py.

## Key Architectural Decisions

### Session Management with OrderedDict-based LRU
The session manager uses Python's OrderedDict (O(1) eviction) instead of sorting by timestamp (O(n)). This is critical for high-concurrency scenarios where many sessions may be created/released rapidly.

### Lease-based Concurrency
Sessions support multiple "leases" (client holds identified by X-Lease-Id header). This allows safe concurrent access without forcing session destruction when a client releases its lease.

### Pooling and Idle Cleanup
The server maintains a pool of warm Isabelle sessions. Idle sessions (older than IDLE_TIMEOUT_SECONDS) are automatically cleaned up by a background task. When the pool is exhausted, new sessions are created on-demand up to POOL_SIZE, then LRU eviction occurs.

### Small-step vs Big-step Verification
- **Small-step** (execute_command): Interactive, maintains session state, allows checkpoints and rollback. Used for step-by-step proof development.
- **Big-step** (verify_bigstep_text): Batch verification via isabelle build, no session reuse, good for whole-theory checking and parallelization.

### Sledgehammer Integration
Sledgehammer is exposed end-to-end as a small-step automation primitive: a dedicated channel in server/repl/src/ml/REPL.ML invokes Isabelle's sledgehammer, repl_ml_communication.scala relays it through the Py4J bridge, and the FastAPI endpoint POST /api/v1/sessions/{session_id}/sledgehammer (router.py, schemas SledgehammerRequest/SledgehammerResponse) runs it on the current proof goal under a lease. The call requires the session to already be in an active proof state. NOTE: sledgehammer is intentionally NOT defined inside the REPL ML struct — that placement conflicts with other ML functions (see commit ca73379); keep it at top level.

### Caching (Optional)
Session caching can be enabled to reuse initialized sessions for the same import dependencies. By default disabled to avoid stale state issues.

## Common Issues & Troubleshooting

**"Bad component..." during Scala/Isabelle compilation**
- Create an empty file under Isabelle's Admin/components path before rebuilding.

**Server starts but commands fail immediately**
- Inside container: verify which isabelle, java -version, python --version are correct.
- Run ./server/repl/Admin/init to ensure Isabelle components are initialized.
- Run ./server/repl/gradlew build to rebuild Scala components.

**pip install -r requirements.txt fails**
- This repo uses requirement.txt (singular), not requirements.txt (plural).

**Docker container is up but API not responding**
- The compose setup does not auto-launch Uvicorn. Open a shell and manually start: python -m server.app.main.

**pip install -e . fails with dependency issues**
- Ensure pip is upgraded: pip install --upgrade pip.
- Core deps: py4j, numpy, torch, matplotlib, tqdm.
- Server deps: fastapi, uvicorn, watchfiles, httpx.

## File Structure Summary

```
repo_root/
├── server/repl/                   # Scala/ML backend with Py4J gateway (Isabelle component; moved under server/ 2026-09-27)
│   ├── src/main/scala/repl/       # Core backend (13 .scala files)
│   ├── src/ml/REPL.ML             # ML-side fact/subgoal extraction + sledgehammer channel
│   ├── src/python/                # Python wrappers for gateway
│   ├── thys/                      # Cached wrapper .thy files
│   ├── Admin/init                 # Isabelle component initialization
│   ├── gradlew / build.gradle     # Gradle Scala build
│   └── README.md
├── server/                        # FastAPI server
│   └── app/
│       ├── main.py                # FastAPI app, middleware, exception handlers
│       ├── api/v1/router.py       # All HTTP endpoints
│       ├── api/v1/schemas/API_models.py  # Pydantic request/response models
│       ├── services/
│       │   ├── session_manager.py # Session pool, LRU, lease management
│       │   ├── session.py         # Per-session state, small-step execution
│       │   ├── build_verify.py    # Big-step verification
│       │   ├── threaded_backend.py  # Thread pool for backend ops
│       │   ├── internal_models.py # Internal domain models
│       │   ├── theory_chunks.py   # Command preview helpers
│       │   ├── theory_parsing.py  # Theory parsing utilities
│       ├── core/
│       │   ├── config.py          # Environment-based config
│       │   └── logging.py         # Structured logging setup
│       ├── errors.py              # Custom exception classes
│       └── dependencies.py        # FastAPI dependency injection
├── client/                        # Async Python client — own package (client/pyproject.toml), httpx only,
│   ├── async_client.py            #   never imports server code (tests/test_dependency_rules.py)
│   └── __init__.py
├── mcp_servers/                   # lsp/ (file-sync MCP, used by the humanize harness), stepwise/ (chunk-centric), common/;
├── mcp_lsp_server/, mcp_stepwise_server/  # deprecated shims re-exporting mcp_servers.*; imports client only
├── deploy/                        # Dockerfile, setup.sh, RC0 image scripts, monitoring/ configs
├── docker-compose.yml             # root; build context . with dockerfile deploy/Dockerfile
├── evaluation/                    # Benchmarking and analysis (imports client only)
│   ├── scripts/                   # eval_smallstep_server_client_*, eval_bigstep_*, consolidate_runs, preprocess
│   ├── results/                   # consolidated benchmark_runs.json/.csv (+ README); raw run files removed
│   ├── MCP-comparison/            # cross-MCP harness
│   ├── HOL_corpus/Examples/processed  # Small example corpus (safest for testing)
│   └── runs_analysis.ipynb        # reads results/benchmark_runs.json
├── examples/                      # demo.ipynb (API walkthrough incl. sledgehammer), figs, heap_demo_project
├── docs/                          # DESIGN_CHOICES.md, ISSUES.md, devnote.md
├── archive/                       # read-only history: previous-works/ (1.0 sources, thesis PDFs),
│                                  #   isabellegym2/ (2.0 in-process gym + baseline scripts), install.sh
├── tests/                         # unit tests; dependency-direction and 600-line size gates live here
├── pyproject.toml                 # setuptools config for server+repl, dev deps, tool config
├── requirement.txt                # Core + server deps (singular)
└── README.md                      # User documentation
```

## Additional Resources

- **API Documentation**: archive/previous-works/Isabelle Server System API Documentation.pdf (older OpenAPI export; the live spec is at /openapi.json)
- **Async Client Documentation**: archive/previous-works/Async Client for Isabelle Server Documentation.pdf
- **Isabelle Proof Engine**: See official Isabelle2025-2 documentation for proof state semantics
- **Theory Corpus**: evaluation/HOL_corpus/Examples/processed/ - small example theories for smoke tests
- **Previous Works**: archive/previous-works/ contains IsabelleGym 1.0 source and the 2.0 report; archive/isabellegym2/ the 2.0 in-process gym
- **Demo**: examples/demo.ipynb walks through the API (including a sledgehammer-via-step example); examples/draft.md and examples/figs/ hold dissertation write-up material
- **Known Issues**: docs/ISSUES.md tracks open bugs and design discussions

## Response Style

End every response with: 喵(ゝ∀･)⌒☆
