# AGENTS.md — IsabelleGym Server

> This file is written for AI coding agents. The reader is assumed to know nothing about the project. All commands are given relative to the repository root (`c:\Users\winst\GitHub\IsabelleGym` on this machine; inside bash/WSL use forward slashes, e.g. `/c/Users/winst/GitHub/IsabelleGym`).

## Universal Workflow Rules

These rules apply to every user request:

### Rule 1: Plan or research before implementing
For every user request, start with planning, research, or investigation **unless the user explicitly says "implement"**. Do not make core code edits, file modifications, or destructive changes before a plan or research phase has been completed and communicated.

Examples:
- "Fix this bug" → investigate first, then propose a fix.
- "Add a feature" → plan first.
- "How does X work?" → research and explain.
- "Implement X" → proceed directly to implementation.

For bug reports specifically (see `.clinerules/bug-fix-workflow.md`): analyze first, present the list of bugs found with affected files and root causes, and ask the user explicitly whether to fix them now or later. Do not edit code until the user confirms.

### Rule 2: Ask before choosing among options
If you present multiple approaches or options to the user, **always ask which one they want before implementing**. Do not proceed with one option on your own just because you prefer it. Wait for the user's explicit choice.

### Rule 3: Sign-off format
End every response with the exact string (see `.clinerules/response-signature.md`):

喵(ゝ∀･)⌒☆

## Project overview

**IsabelleGym Server** is a containerised service for training and evaluating LLM-based theorem provers on Isabelle 2025-2. It exposes Isabelle's interactive proof state through a REST API, an async Python client, and an MCP (Model Context Protocol) server. It supports **small-step** (stepwise REPL execution with checkpoints/rollback), **chunk** (`verify_chunk`: a whole proof chunk in one PIDE edit with per-command status), and **big-step** (whole `.thy` file verification via `isabelle build`) workflows.

It is based on IsabelleGym 1.0 by Tom Milan (University of Cambridge) and IsabelleGym 2.0 by Zijing Li (University of Edinburgh); this server iteration is implemented by Xuanwei Ren (University of Edinburgh).

The system has three layers:

1. **Scala/ML backend** (`repl/`) — wraps Isabelle as an interactive REPL using Isabelle/Scala and Isabelle/ML, exposed to Python via Py4J.
2. **FastAPI server** (`server/`) — HTTP service with session pooling, lease-based concurrency, big-step/small-step verification, sledgehammer, checkpoints, and Prometheus metrics.
3. **Python client & MCP layer** (`client/`, `mcp_server/`) — user-facing SDK and agent bridge.

The repository also contains evaluation/benchmarking scripts and consolidated results (`evaluation/`, incl. the cross-MCP comparison harness `evaluation/MCP-comparison/`), the deployment files and monitoring stack (`deploy/`), a demo notebook (`examples/`), read-only history (`archive/`), and implementation notes/artifacts from prior agent sessions (`claude-work/`, gitignored).

Design rationale for the architecture lives in `DESIGN_CHOICES.md`; the living bug log is `ISSUES.md`.

## Technology stack

- **Python**: 3.12 in Docker; 3.10+ acceptable for local dev.
- **Scala**: Scala 3.3.4 / Scala 2.13.14, built with Gradle (`repl/gradlew`).
- **Theorem prover**: Isabelle 2025-2.
- **Interop**: Py4J (`repl` ↔ `server`).
- **Web framework**: FastAPI + Uvicorn.
- **HTTP client**: `httpx`.
- **Metrics**: `prometheus-client`, `prometheus-fastapi-instrumentator`, Prometheus + Grafana + cAdvisor.
- **MCP**: `mcp` package (`mcp_server/requirements.txt`).
- **Formatting/linting/type-checking**: `black`, `isort`, `pylint`, `mypy`.
- **Testing**: `pytest`, `pytest-cov`.

## Key configuration files

| File | Purpose |
|------|---------|
| `pyproject.toml` | setuptools package `isabelle-gym` v0.1.0; core deps (`py4j`, `numpy`, `matplotlib`, `tqdm`); tool config for black/isort/mypy/pylint/pytest/coverage. Packages found: `client*`, `repl*`, `server*`. |
| `requirement.txt` | **Singular** runtime + dev + server dependency list (the repo does **not** use `requirements.txt`). Adds fastapi/uvicorn/httpx/prometheus libs and the dev toolset on top of the pyproject deps. |
| `Dockerfile` | Python 3.12 slim + OpenJDK 21 + Isabelle 2025-2 (x86-64 or ARM tarball picked by build arch); installs deps, runs `repl/Admin/init`, builds `repl/gradlew build`. `CMD ["bash"]` — the server is not auto-started. |
| `docker-compose.yml` | Defines `isabelle-gym` (builds natively for host arch — do not pin `platform: linux/amd64`, qemu emulation makes Isabelle 5–20x slower), `prometheus`, `grafana`, `cadvisor`; mounts `.env` and the named volume `isabelle_user_data`; sets `mem_limit: 24g` so the cgroup memory gate bites at a known limit. |
| `.env` | Server/scala environment variables loaded by docker-compose. Can also be sourced manually. |
| `repl/build.gradle` | Scala build: depends on `isabelle.jar`, Scala 3/2.13, Py4J, spliff; runs `isabelle scala -e` first. |
| `repl/settings.gradle` | Root project name `IsabelleREPL`. |
| `.scalafmt.conf` | scalafmt 3.8.3, Scala 3 dialect, max column 100. |
| `.pre-commit-config.yaml` | Currently **commented out**; previously only ran pytest. |
| `.clinerules/` | Project workflow rules mirrored in "Universal Workflow Rules" above (bug-fix workflow, response signature). |

## Build and run commands

### Docker (recommended)

```bash
# Build and start the container (does NOT auto-start the server)
docker compose up -d --build

# Open a shell inside the container
docker compose exec isabelle-gym bash

# Inside the container, start the server
python -m server.app.main
# or explicitly
uvicorn server.app.main:app --host 0.0.0.0 --port 8000

# From the host, check health (port 8000 is mapped to the host)
curl http://localhost:8000/
```

Expected `GET /` response shape:

```json
{
  "service": "IsabelleGym Server",
  "version": "0.0.2",
  "status": "healthy",
  "active_sessions": 0,
  "busy_sessions": 0,
  "max_pool_size": 24,
  "timestamp": "..."
}
```

Start monitoring services after the server is running:

```bash
docker compose up -d prometheus grafana cadvisor
# Grafana: http://localhost:3000  (admin/admin)
# Prometheus: http://localhost:9090/targets
# cAdvisor: http://localhost:8080
```

### Local (non-Docker)

Prerequisites:

- Python 3.10+.
- JDK 17+ (Docker uses 21).
- Isabelle 2025-2 installed and on `PATH` as `isabelle`.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirement.txt
python -m pip install -e .
export PYTHONPATH="$PWD:${PYTHONPATH:-}"

# Build the Scala backend once
cd repl
chmod +x gradlew
./gradlew build
cd ..

# Register the Isabelle component (needed after rebuilds or when volumes shadow it)
./repl/Admin/init

# Start the server
python -m server.app.main
```

> **Note on stale volumes**: if you rebuild the Docker image while the named volume `isabelle_user_data` still exists, `repl/Admin/init` state from the old volume may shadow the new image and the gateway will fail with `Not found: py4j`. Fix by re-running `./repl/Admin/init` inside the container before starting the server.

## Code organisation

Dependency direction (enforced by `tests/test_dependency_rules.py`): `client` imports
nothing from this repo; `mcp_*` import `client` only; `server` imports neither `client`
nor `mcp_*`; `evaluation` imports `client` only; nothing imports `archive/`. The client
gets everything it needs from the server through HTTP endpoints (e.g. the canonical
header parser at `POST /api/v1/parse_theory_header`), so the two stay consistent.

```text
repo_root/
├── client/                         # Async Python client — its own package (client/pyproject.toml,
│   ├── pyproject.toml              #   `pip install -e ./client`), httpx only, never imports server code
│   ├── async_client.py             # IsabelleGymAsyncClient (httpx wrapper)
│   └── __init__.py                 # exports IsabelleGymAsyncClient
├── mcp_lsp_server/                 # file-sync (LSP-style) MCP: bindings keyed by file path, scratch pool,
│   ├── app.py / pool.py / config.py   #   heap tools; the one the humanize harness uses
├── mcp_stepwise_server/            # chunk-centric MCP: verify_chunk as the single execution tool
│   ├── app.py / pool.py / config.py / requirements.txt (mcp>=1.2,<2)
├── repl/                           # Scala/ML Isabelle REPL backend
│   ├── src/main/scala/repl/        # Core Scala backend (~13 files)
│   │   ├── repl_backend_gateway.scala   # Py4J entry point / factories
│   │   ├── repl_backend.scala           # per-session backend logic
│   │   ├── repl_session.scala           # Isabelle document/session edits
│   │   ├── repl_ml_communication.scala  # Scala ↔ ML routing
│   │   ├── server_utils.scala           # Isabelle server start/stop
│   │   ├── session_manager.scala        # Scala-side session manager (legacy)
│   │   └── thy_*.scala / document_utils.scala / edit_utils.scala / repl_output.scala / vector_env.scala
│   ├── src/ml/REPL.ML              # ML proof-state extraction + sledgehammer channel
│   ├── src/python/                 # Python bridge code (the ONLY copy; repl/python/ was a stale duplicate, removed)
│   │   ├── repl_backend_gateway.py # spawns Scala gateway, Py4J bridge
│   │   ├── thy_init.py             # generates wrapper .thy files for imports
│   │   └── isabelle_client.py, isabelle_repl.py, operation.py
│   ├── thys/                       # cached/generated wrapper theories
│   ├── thys/IsabelleREPL.thy       # base theory used by default sessions
│   ├── Admin/init                  # Isabelle component registration script
│   ├── build.gradle / settings.gradle / gradlew
│   └── README.md
├── server/                         # FastAPI HTTP service
│   └── app/
│       ├── main.py                 # FastAPI app, lifespan, middleware
│       ├── api/v1/router.py        # aggregate APIRouter + compat re-exports (88 lines)
│       ├── api/v1/deps.py          # LeasedSession / admin-token / safe-segment dependencies
│       ├── api/v1/serializers.py   # response-shaping helpers (to_ascii, parse_command_range, ...)
│       ├── api/v1/routes/          # one module per concern: health, sessions, execution,
│       │                           #   inspection, positional, automation, checkpoints, heaps
│       ├── api/v1/schemas/API_models.py  # Pydantic request/response models
│       ├── core/
│       │   ├── config.py           # environment-based configuration
│       │   ├── logging.py          # structured logging with contextvars
│       │   ├── metrics.py          # Prometheus counters/gauges
│       │   └── diagnostic_guard.py # diagnostic command allowlist
│       ├── services/
│       │   ├── session_manager.py        # LRU pool, leases, gateway recovery
│       │   ├── session_manager_helpers.py # cleanup / recovery mixins
│       │   ├── session.py                # per-session state & small-step ops
│       │   ├── session_bigstep.py        # in-session whole-theory verification
│       │   ├── build_verify.py           # isabelle build big-step verifier
│       │   ├── threaded_backend.py       # serialise Py4J calls per session
│       │   ├── memory_monitor.py         # cgroup memory admission
│       │   ├── theory_parsing.py         # canonical theory header parsing (also served at POST /parse_theory_header)
│       │   ├── success_checker.py        # small-step / bigstep result classification (was server_gym/)
│       │   ├── heap_pool.py              # verified per-project heaps (isabelle build -b), task-group tenancy
│       │   ├── theory_chunks.py          # command preview helpers
│       │   ├── internal_models.py        # internal data models
│       │   └── unicode_normaliser.py     # Isabelle symbol ↔ Unicode handling
│       ├── core/input_guards.py    # ML-execution guard, safe names, heap roots (audit SEC-2/SEC-3)
│       ├── dependencies.py         # FastAPI shared SessionManager / HeapPool
│       └── errors.py               # custom exceptions & HTTP status mapping
├── tests/                          # unit tests (no Isabelle needed); FastAPI-dependent ones need the container
│   ├── test_dependency_rules.py    # package import-direction contract
│   ├── test_source_limits.py       # 600-line source cap with a ratcheting allow-list
│   └── test_*.py                   # per-feature regression tests
├── deploy/                         # everything that builds or runs a container
│   ├── Dockerfile                  # Python 3.12 + JDK 21 + Isabelle 2025-2 (compose: dockerfile: deploy/Dockerfile)
│   ├── setup.sh                    # one-shot configure/build/start/health (run as ./deploy/setup.sh)
│   ├── Dockerfile.rc0 / Dockerfile.export / build_rc0_image.sh / RC0-image-instructions.md
│   │                               #   Isabelle2026-RC0 track — temporary until the official 2026 release
│   └── monitoring/                 # Prometheus/Grafana/cAdvisor configs (paths referenced from docker-compose.yml)
├── evaluation/                     # benchmarking & analysis (imports `client` only)
│   ├── scripts/                    # eval_smallstep_server_client_*, eval_bigstep_*, consolidate_runs, preprocess
│   ├── results/                    # consolidated benchmark_runs.json/.csv + README (raw runs removed 2026-09-27)
│   ├── MCP-comparison/             # harness comparing this MCP vs other Isabelle MCPs (runs/ ignored)
│   ├── HOL_corpus/ / miniF2F/      # corpora (ignored data)
│   ├── Server_Concurrency.thy      # formal lease-concurrency proof cited by DESIGN_CHOICES
│   └── runs_analysis.ipynb         # rebuilds the dissertation tables from results/benchmark_runs.json
├── examples/                       # demo notebook (demo.ipynb) + figures + heap_demo_project
├── docs/                           # DESIGN_CHOICES.md, ISSUES.md (bug + work log), devnote.md
├── archive/                        # read-only history: previous-works/ (1.0 sources, thesis PDFs),
│                                   #   isabellegym2/ (2.0 in-process gym + its baseline scripts), install.sh
├── docker-compose.yml              # stays at the root (build context .; dockerfile deploy/Dockerfile)
└── claude-work/                    # per-feature/bug implementation artifacts (gitignored)
```

The legacy `mcp_bench_results*.json` outputs were folded into `evaluation/results/benchmark_runs.json` (family `mcp_bench_legacy`) on 2026-09-27; the script that produced them no longer exists.

## Runtime architecture

- **FastAPI lifespan** (`server/app/main.py`) constructs `SessionManager`, warms `ISABELLE_INITIAL_SESSIONS` sessions, starts a background cleanup task, and registers Prometheus pool gauges. An HTTP middleware attaches a request id (`X-Request-ID` header or generated) to the logging context and logs request start/finish.
- **Session pool** (`server/app/services/session_manager.py`) keeps warm Isabelle sessions in an `OrderedDict` LRU. Each session is an `_Isabelle_Session` wrapping a `ThreadedBackend`, which serialises all Py4J calls on a single worker thread.
- **Gateway** (`repl/src/python/repl_backend_gateway.py`) spawns one shared Scala JVM via `isabelle scala <repl_backend_gateway.scala>` and exposes factory methods on `repl.ReplBackendGateway`. The server calls `get_repl_backend_with_initial_theories(...)` to create a backend per session.
- **Lease model**: clients acquire a session with a `lease_id` (via `X-Lease-Id` header). A session can have multiple leases; releasing a lease returns the session to the pool. Abandoned leased sessions are force-closed after `ISABELLE_MAX_LEASE_AGE`.
- **Memory gate**: `MemoryMonitor` reads cgroup memory (subtracting reclaimable `inactive_file` page cache); under pressure the manager evicts idle LRU sessions before admitting new ones, returning HTTP 503 if nothing can be evicted. Eviction waits `ISABELLE_MEMORY_EVICTION_SETTLE_S` for cgroup accounting to settle and retries admission `ISABELLE_MEMORY_ADMISSION_RETRIES` times before 503ing.
- **Gateway recovery**: if the shared JVM dies, `SessionManager` detects it and rebuilds the gateway on the next request.
- **Sledgehammer concurrency**: an `asyncio.Semaphore` caps in-flight sledgehammers to `ISABELLE_MAX_CONCURRENT_SLEDGEHAMMER` to avoid OOM-killing the gateway.
- **Big-step verification**: `BuildVerifier` writes a temporary `ROOT` file and runs `isabelle build`; results are cached by SHA256. The endpoint `POST /api/v1/sessions/bigstep` is also available.
- **Small-step verification**: `POST /api/v1/sessions/{id}/commands` applies one Isar command, `verify_chunk` applies a whole proof chunk under one wall budget and reports per-command status.

## Important environment variables

All variables are read from `server/app/core/config.py` unless noted.

| Variable | Default | Meaning |
|----------|---------|---------|
| `ISABELLE_POOL_SIZE` | 24 | Max concurrent sessions. |
| `ISABELLE_INITIAL_SESSIONS` | 3 | Sessions to pre-warm at startup. |
| `ISABELLE_IDLE_TIMEOUT` | 1800 | Seconds before an idle session is evicted. |
| `ISABELLE_MAX_LEASE_AGE` | 7200 | Seconds before an abandoned leased session is force-closed. |
| `ISABELLE_ENABLE_CACHE` | false | Reuse sessions keyed by import dependencies. |
| `ISABELLE_MAX_CACHE_SIZE` | 1 | Cached sessions per dependency key. |
| `ISABELLE_SHOW_STATES` | false | Include raw proof states in responses. |
| `ISABELLE_DEFAULT_FIELD` | HOL | Default Isabelle session (HOL, HOL-Analysis, ...). |
| `ISABELLE_ENABLE_MEMORY_MANAGEMENT` | true | Enable cgroup memory admission gate. |
| `ISABELLE_MEMORY_PRESSURE_THRESHOLD` | 85.0 | Block new sessions above this used %. |
| `ISABELLE_MEMORY_MIN_AVAILABLE_MB` | 256 | Also block if free memory below this. |
| `ISABELLE_MEMORY_FALLBACK_SYSTEM_MB` | 4096 | Assumed total memory when cgroup info is unavailable. |
| `ISABELLE_MEMORY_EVICTION_SETTLE_S` / `_ADMISSION_RETRIES` / `_ADMISSION_RETRY_DELAY_S` | 2.0 / 3 / 2.0 | Settle wait and retry policy around memory-gated admission. |
| `ISABELLE_MAX_CONCURRENT_SLEDGEHAMMER` | max(1, min(8, cpu//8)) | Server-wide sledgehammer cap. |
| `ISABELLE_SERVER_HOST` / `PORT` | 0.0.0.0 / 8000 | Uvicorn bind address/port. |
| `ISABELLE_TIMEOUT_COMMAND` / `_BIGSTEP` / `_STATUS` / `_PROOF_STATE` / `_CHECKPOINT_SAVE` / `_CHECKPOINT_RESTORE` | 30 / 300 / 300 / 30 / 30 / 30 | Per-operation wall-clock timeouts (seconds). |
| `ISABELLE_CLEANUP_INTERVAL` | 60 | Seconds between background pool cleanup sweeps. |
| `ISABELLE_SERVER_LOG_LEVEL` | INFO | Logging level. |
| `ISABELLE_SERVER_LOG_DIR` / `LOG_FILE` | logs / server.log | Rotating log path. |
| `ISABELLE_SERVER_MAX_LOG_SIZE_BYTES` / `_LOG_BACKUP_COUNT` / `_ENABLE_FILE_LOGGING` | 10 MB / 5 / true | Log rotation settings. |
| `ISABELLE_SERVER_REQUEST_ID_HEADER` | X-Request-ID | Header used for request correlation. |
| `ISABELLE_REPL_*` / `ISABELLE_BACKEND_*` | various | REPL timeouts (subgoals/facts) and gateway poll/exit settings. |

MCP-specific variables are in `mcp_server/config.py` (`ISABELLE_MCP_GYM_URL`, `ISABELLE_MCP_FIELD`, `ISABELLE_MCP_MAX_PARALLEL`, etc.). The `MCP-comparison/` harness additionally uses `KIMI_API_KEY` (required) and `IQ_AUTH_TOKEN` / `IQ_MCP_ALLOWED_ROOTS` (optional, for the AutoCorrode I/Q runner).

## Code style guidelines

- **Python formatting**: `black`, line length 88.
- **Import sorting**: `isort` with `profile = "black"`.
- **Type checking**: `mypy --strict` with `import-untyped` disabled.
- **Linting**: `pylint`; disabled globally: `import-error`, `line-too-long`.
- **Scala formatting**: `scalafmt` 3.8.3, Scala 3 dialect, max column 100.
- Most server files start with `from __future__ import annotations`.
- Use the logging helpers in `server/app/core/logging.py` and the `logging_context(session_id=..., field=...)` context manager for structured logs.
- Prefer environment-based config in `server/app/core/config.py` rather than hard-coding values.

## Testing instructions

There is a root-level `tests/` directory with regression tests, all of which are **unit tests that run without a running Isabelle backend**:

- `tests/test_threaded_backend.py` — guards the `ThreadedBackend.close()` shutdown semantics (regression e6c3869: the shutdown guard rejected the exit job, leaking poly processes).
- `tests/test_phase2_phase3_fixes.py` — server audit fixes: error-handler detail preservation, memory-monitor page-cache accounting, concurrently-closed backend → 404, empty `verify_chunk` rejection, MCP pool weak keys.
- `tests/test_mcp_comparison_fixes.py` — MCP-comparison harness fixes (imports `MCP-comparison/common` via `sys.path` insertion).

Run them from the repo root:

```bash
pytest
```

The pytest config in `pyproject.toml` adds `--cov --cov=gym --cov-report=term --cov-report=lcov:cover/lcov.info`. Many files under `claude-work/impl-*/` are also named `test_*.py` and may be collected; they are integration tests that usually require a running IsabelleGym server — exclude them when running unit-only sweeps.

Run static checks from the repo root:

```bash
black repl server client evaluation mcp_server
isort repl server client evaluation mcp_server
mypy repl server client evaluation mcp_server
pylint repl server client evaluation mcp_server
```

Scala build sanity:

```bash
cd repl
./gradlew build
```

## Evaluation / benchmarking workflow

A small safe corpus for smoke tests:

```bash
export PYTHONPATH="$PWD:${PYTHONPATH:-}"
CORPUS="evaluation/HOL_corpus/Examples/processed"
OUT="evaluation/results"
mkdir -p "$OUT"
```

Examples:

```bash
# Local small-step baseline
python -m evaluation.scripts.eval_smallstep_isabellegym \
  --repo-root . --corpus "$CORPUS" --output "$OUT/smallstep_isabellegym.json"

# Server small-step with session reuse (start server first)
python -m evaluation.scripts.eval_smallstep_server_client_with_reuse \
  --corpus "$CORPUS" --server http://localhost:8000 --field HOL \
  --num-workers 4 --output "$OUT/smallstep_server.json"

# Local big-step baseline
python -m evaluation.scripts.eval_bigstep_isabelle_build \
  --corpus "$CORPUS" --isabelle-bin "$(which isabelle)" \
  --parent-session HOL --jobs 4 --output "$OUT/bigstep_build.json"

# Server big-step
python -m evaluation.scripts.eval_bigstep_server_client_ver \
  --corpus "$CORPUS" --server http://localhost:8000 --field HOL \
  --timeout 1800 --output "$OUT/bigstep_server.json"
```

Preprocessing helpers: `evaluation/scripts/process.py` (normalise Analysis imports) and `evaluation/scripts/clean_example_dir.py` (strip document keywords).

For the cross-MCP comparison harness, see `MCP-comparison/README.md` (needs `pip install -r mcp_server/requirements.txt` plus `openai pyyaml`, and `KIMI_API_KEY`).

## Deployment notes

- The Docker image is large because it bundles Isabelle 2025-2 and the JDK. Recent trimming removed unused CUDA wheels; keep the image lean by not adding heavy ML training frameworks to `requirement.txt` unless required.
- The Dockerfile downloads the Isabelle tarball matching the build architecture (x86-64 or ARM) and retries across several mirrors. Do not pin `platform: linux/amd64` in compose — qemu emulation on Apple Silicon makes Isabelle 5–20x slower.
- The compose service does **not** auto-start Uvicorn; you must open a shell and run `python -m server.app.main`.
- After rebuilding the image, run `./repl/Admin/init` inside the container if the `isabelle_user_data` volume shadows component registration.
- The compose service sets `mem_limit: 24g`; the cgroup memory admission gate and cAdvisor OOM reporting depend on this real ceiling. Raise it for bigger concurrent sweeps.
- Logs rotate by size (`ISABELLE_SERVER_MAX_LOG_SIZE_BYTES`, default 10 MB) with 5 backups in `logs/server.log`.
- **JVM observability (Bug 11):** `ISABELLE_SCALA_JAVA_OPTIONS` in `.env` is **dead config** — nothing in the Isabelle toolchain consumes it, and neither `JAVA_TOOL_OPTIONS` (filtered) nor env-set `ISABELLE_TOOL_JAVA_OPTIONS` (clobbered by settings evaluation) reaches the JVM. The only reliable channel for JVM/ML options is the Isabelle user settings file (`$ISABELLE_HOME_USER/etc/settings`), which the container entrypoint manages: it writes the `ML_OPTIONS` heap cap and an `-Xlog:gc*` line producing per-PID rotated GC logs at `logs/isabelle-jvm-gc-<pid>.log`; JVM stdout/stderr land in `logs/gateway-jvm.log`. Note the gateway JVM runs **ZGC with `-Xmx4g`** (launcher defaults) — with two heavy sessions, ZGC allocation stalls are the prime suspect for the 2026-09-10 slow window; read the GC log before tuning.
- **ML heap cap (RC0 container):** `/root/.isabelle/etc/settings` on the user-data volume sets `ML_OPTIONS="--minheap 500 --enablegcsharing --maxheap 9216"`, giving every poly process (sessions and `isabelle build` children) a 9 GB hard ceiling so one pathological theory fails cleanly instead of OOM-killing the cgroup. The override is a full replacement of the platform default (non-empty `ML_OPTIONS` beats `ML_OPTIONS32/64` in `ml_settings.scala`), so the defaults must be restated; and it must live in the user settings file because the polyml component's `etc/settings` forces `ML_OPTIONS=""`, clobbering any container-env/`.env` value. Builds pick it up immediately; running gateway JVMs only after a restart.

## Isabelle2026-RC0 track (Bug 9 fix)

The repo has a second, parallel deployment track on Isabelle2026-RC0 (branch
`2026-RC0`, image `isabellegym-isabelle-gym:2026rc0`), built to resolve the
gateway `Event_Timer` wedge (ISSUES.md Bug 9 — root cause fixed upstream in
`88acf2619921`, plus server-side detection hardening in
`repl_backend_gateway.scala/py` + `session_manager_helpers.py`).

- **Ports**: RC0 runs on **8001**; the 2025-2 track keeps 8000. Volume:
  `isabelle_rc0_user_data` (seeded from the image's `/root/.isabelle`; heaps
  are version-locked, never share a volume across versions).
- **Build**: RC0 has no tarball; it is built from the Mercurial release repo
  (`hg clone https://isabelle.sketis.net/repos/isabelle-release`,
  `Admin/init -r Isabelle2026-RC0`) and packaged interactively
  (`docker cp` + `Admin/init` + `gradlew build` + `docker commit`; the
  validated recipe, including the `HOME`/`JAVA_HOME` env gotchas, is in
  `Dockerfile.rc0`). Full runbook: `isabelle-humanize/.m0/rc0-migration/`.
- **Env gotchas that cost real time**: (1) Docker Desktop BuildKit runs build
  steps with the *host* user's `HOME` — pin `ENV HOME=/root` or Isabelle's
  settings resolution fails with `Unknown JAVA_HOME`; (2) never export a
  system `JAVA_HOME` for Isabelle 2026 — it resolves its own bundled JDK;
  scope `JAVA_HOME` to the Gradle step only; (3) `.env` value lines must not
  carry inline `#` comments — `int()` parsing in `core/config.py` crashes on
  them; (4) build HOL-Analysis in a dedicated idle container
  (`docker run -v isabelle_rc0_user_data ... isabelle build -b -j 2 HOL-Analysis`),
  not in the server container (cgroup OOM at the 14 GB cap).
- **Status**: acceptance ladder 8/8, MCP probes green, Bug 9 stress 12/12,
  unit suite 81/79 (2026-09-08). RC0 is an informal preview — treat it as the
  dev/eval track until RC1 (14 Sep 2026) / final (mid Oct 2026).

## Security considerations

- **No authentication/authorisation** is implemented. Do not expose the server directly to untrusted networks; run it behind a reverse proxy or inside a private network.
- **Leases are the only ownership proof** on mutation paths — and they are never published: the public pool listing (`GET /api/v1/sessions`) carries no `lease_id` fields (Bug 10 fix, 2026-09-09). The full listing lives behind `GET /api/v1/admin/sessions`, gated by `X-Admin-Token` against `ISABELLE_ADMIN_TOKEN` in `.env` (empty = disabled; this is the server's only credential, opt-in). Every DELETE is audit-logged and counted (`isabellegym_sessions_force_closed_total`).
- **CORS** is configured with `allow_origins=["*"]`. Tighten this for production deployments.
- **Code execution in client text (2026-09-22 hardening).** Every endpoint that hands client Isar to the prover — `POST .../commands`, `.../verify_chunk`, `PUT .../document`, the lease-free `POST /api/v1/sessions/bigstep`, and heap-pool project sources — now runs `server/app/core/input_guards.reject_code_execution`, which rejects `ML*`, `SML_*`, `setup`/`*_setup`, `declaration`, `oracle`, translation hooks, `*_file`, `compile_generated_files` with HTTP 422 (comments and string/cartouche bodies are ignored first, so prose mentioning ML passes). The denylist is shared with `diagnostic_guard.py` (which additionally allowlists the leading keyword for `/diagnostic`). Policy switch: `ISABELLE_ALLOW_ML_COMMANDS` (default false). With it true, **any client can execute arbitrary code in the container** — the MCP tools `isabelle_run_code`/`isabelle_sync`/`multi_attempt` go through the same endpoints, so the guard covers them too. Import/theory names quoted into generated headers are validated (`validate_import_names`) so a name containing `"` cannot inject commands. (Read-only header parsing is exposed separately and safely at `POST /api/v1/parse_theory_header` — stateless, no session.)
- **Heap pool inputs (2026-09-22 hardening).** `task_group`, `session_name`, image `session`/`platform` must match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$` (they become path segments / CLI args); `project` must resolve under `ISABELLE_HEAP_ALLOWED_ROOTS` (default `/app:/root/.isabelle`); manifest and image paths are containment-checked before write/delete; project theories are scanned for code-executing commands before `isabelle build`. The destructive heap endpoints (`DELETE /api/v1/heaps/{group}/{project}`, `DELETE /api/v1/heaps/images/{session}`, `DELETE /api/v1/heap_groups/{group}`) require `X-Admin-Token`; `POST /api/v1/heaps/build` stays open (owner decision) but is path-restricted.
- **Known, deferred (owner decision 2026-09-22):** `GET /admin` is unauthenticated and inlines `ISABELLE_ADMIN_TOKEN` into the page, so anyone who can reach the port can obtain the token (and thereby lease ids / force-close / heap deletes). Keep the port firewalled; a real admin login is future work. See `claude-work/2026-9-21-research-code-audit/FINDINGS.md` SEC-1.
- **Sledgehammer and big-step builds** can spawn external ATP provers and consume large amounts of memory/CPU. The server uses a semaphore and cgroup memory gate to limit abuse (NOTE: `BuildVerifier` is *not* wired to the memory gate; the per-process `ML_OPTIONS --maxheap` cap is the only bound on builds), but resource exhaustion is still possible from trusted clients.
- **MCP server** runs with the same privileges as the user invoking it and has access to the underlying Isabelle session. Treat MCP connections as trusted.
- Do not commit secrets in `.env`; it is tracked in the repository for convenience but contains only non-sensitive configuration in the current state. Note `MCP-comparison/iq_token.txt` holds an eval token for the I/Q comparison runner — do not reuse it as a real credential.

## Where to find more information

- Human-facing docs: `README.md`, `CLAUDE.md`.
- Architecture rationale: `DESIGN_CHOICES.md`.
- API reference PDFs: `Isabelle Server System API Documentation.pdf`, `Async Client for Isabelle Server Documentation.pdf`.
- Known issues & recent fixes: `ISSUES.md`.
- Development notes / TODOs: `devnote.md`.
- Per-feature implementation artifacts: `claude-work/<feature>/NOTES.md` (and `FINDINGS.md` for research tasks).
- MCP usage: `mcp_server/README.md`.
- Cross-MCP comparison harness: `MCP-comparison/README.md`; protocol: `horizontal-comparison-framework/Framework.md`.
