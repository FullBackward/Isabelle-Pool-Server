# Isabelle Pool Server

**Isabelle Pool Server** turns the Isabelle theorem prover into a managed, concurrent
service: a pool of warm proof sessions behind a REST API, with lease-based ownership,
memory-aware admission control, and Prometheus observability. It is the Isabelle
counterpart of [Kimina Lean Server](https://github.com/project-numina/kimina-lean-server)
(FastAPI + a pool of prover REPLs + an import-keyed cache + a Python SDK), extended with
checkpoints and rollback, whole-chunk and whole-theory verification, a heap pool, and two
MCP servers for LLM agents.

On that core it serves three purposes:

- a **training and evaluation environment** for LLM-based provers — **small-step**
  execution (one Isar command at a time, with checkpoints/rollback), **chunk** verification
  (`verify_chunk`: a whole proof chunk in one PIDE edit with per-command status) and
  **big-step** verification (whole `.thy` files via `isabelle build`);
- a **verification service** for single theories or batch workloads — result caching,
  pre-built heaps, pool-level parallelism; the batch loop lives in the client or MCP layer
  (there is no server-side job queue);
- a **platform for many concurrent agents** — the MCP servers give each agent an isolated
  session over a shared prover pool, inside a trusted network (there is no authentication;
  agents are isolated from each other, not coordinated).

Successor to IsabelleGym 1.0 by Tom Milan (University of Cambridge) and IsabelleGym 2.0 by
Zijing Li (University of Edinburgh); this server iteration (IsabelleGym 3.0 until its
renaming in October 2026) is implemented by Xuanwei Ren (University of Edinburgh).

Components:

| Path | What it is |
|---|---|
| `server/` | FastAPI service: session pool, leases, memory admission, metrics (`server/app/api/v1/routes/` holds the endpoints) |
| `server/repl/` | Scala/ML Isabelle REPL backend (PIDE sessions, one shared gateway JVM); launched only by the server |
| `client/` | Async Python HTTP client (`PoolAsyncClient`); its own package (`pip install -e ./client`), httpx only, never imports server code |
| `mcp_servers/` | MCP servers for LLM agents: `stepwise/` (chunk-centric, `verify_chunk` as the one execution tool), `lsp/` (file-sync, LSP-style — tools take a file path), `common/` shared bits |
| `deploy/` | `Dockerfile` (one file, every Isabelle version via `ISABELLE_VERSION`), `setup.sh`, turnkey-image scripts, Prometheus/Grafana/cAdvisor configs (`docker-compose.yml` stays at the root) |
| `evaluation/` | Benchmark CLIs, `results/benchmark_runs.json` (consolidated runs), `MCP-comparison/` harness |
| `examples/` | `demo.ipynb` API walkthrough (HTTP client + both MCP servers), figures, heap demo project |
| `tests/` | Unit tests (no Isabelle needed; ~390 tests) |
| `docs/` | `DESIGN_CHOICES.md` (rationale), `ISSUES.md` (bug log + dated work log), `devnote.md` → `experiments/` (MCP-comparison experiment logs) |
| `archive/` | Read-only history: IsabelleGym 1.0 sources, the 2.0 in-process gym and its baseline scripts, the unmaintained `install.sh` |

Design rationale for the architecture lives in [DESIGN_CHOICES.md](docs/DESIGN_CHOICES.md);
the living bug log is [ISSUES.md](docs/ISSUES.md).

---

## Setup

Pick **one** of the two paths. Both end with the same HTTP API on port 8000, and the
[Usage](#usage) section applies to either.

| | A. Docker (recommended) | B. Native Linux server |
|---|---|---|
| Host needs | Docker Engine + Compose v2 | Ubuntu 22.04/24.04 (or similar), sudo |
| Isabelle, JDK, Python | inside the image | installed by you |
| Memory cap | `mem_limit` in `docker-compose.yml` (cgroup-aware admission gate) | per-process ML heap cap only; admission gate measures whole-host RAM |
| Start on boot | `restart: unless-stopped` | systemd unit (step B8) |
| Time to first `healthz` | 10–30 min build + 1–2 min start | ~20 min + 1–2 min start |

Sizing for both: budget **~30 GB disk** (Isabelle + heaps) and **16 GB+ RAM**. Each Isabelle
session is 1.5–4 GB resident; `ISABELLE_POOL_SIZE × ~4 GB` plus one `isabelle build` must
fit.

Isabelle version: the project targets **Isabelle 2026** (currently the `Isabelle2026-RC2`
release candidate; it becomes `Isabelle2026` when the final release ships). The previous
release, `Isabelle2025-2`, still works from the same code. Heaps are version-locked — never
share a user-data directory/volume between two Isabelle versions.

### A. Docker setup

Works on x86-64 and ARM64 Linux (and Docker Desktop on macOS/Windows for development); the
build picks the Isabelle tarball for the host architecture. Nothing but Docker is installed
on the host.

#### A1. Install Docker (skip what you already have)

```bash
docker --version && docker compose version   # both present? skip to A2
```

```bash
sudo apt-get update && sudo apt-get install -y git curl ca-certificates
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"   # then log out and back in
```

(If only `docker compose version` fails: `sudo apt-get install -y docker-compose-plugin`.
The legacy `docker-compose` v1 binary also works — substitute it in every command.)

#### A2. Clone and run the setup script

```bash
git clone https://github.com/FullBackward/Isabelle-Pool-Server.git
cd Isabelle-Pool-Server
./deploy/setup.sh --verify
```

The script creates `.env` from `.env.example` (generating a random `ISABELLE_ADMIN_TOKEN`),
builds the image, starts the container, waits for `/healthz`, and with `--verify` acquires
a session and proves `lemma True by simp`. First build downloads Isabelle (~1.2 GB) and
compiles the Scala backend: **10–30 minutes**. Add `--build-heaps "HOL-Library"` to prebuild
session heaps (see A5); `--no-build` restarts without rebuilding.

That is the whole install. The steps below are what the script does, for when you want
to do them by hand or something goes wrong.

#### A3. Configure (`.env`)

Runtime configuration lives in `.env` at the repo root (loaded into the container via
`env_file`). `.env.example` documents every knob; the ones to check first:

```bash
ISABELLE_POOL_SIZE=3              # max concurrent Isabelle sessions (each ~1.5-4 GB)
ISABELLE_INITIAL_SESSIONS=0       # sessions pre-warmed at startup (0 = fast startup)
ISABELLE_MEMORY_PRESSURE_THRESHOLD=80.0   # refuse new sessions above this used-%
ISABELLE_ML_MAXHEAP_MB=9216       # hard per-process ML heap cap (Poly/ML --maxheap)
ISABELLE_ADMIN_TOKEN=...          # admin console / admin endpoints credential
```

Changing `.env` later requires recreating the container, not just restarting it:
`docker compose up -d --force-recreate isabelle-pool-server`.

#### A4. Build and start by hand

```bash
cp .env.example .env              # then edit
docker compose build isabelle-pool-server # Isabelle download + Scala build, 10-30 min
docker compose up -d isabelle-pool-server # entrypoint registers components, writes the ML heap cap, starts the server
curl http://localhost:8000/healthz   # {"status":"alive"}  (allow 1-2 min for the gateway JVM)
curl http://localhost:8000/          # full health: version, gateway_alive, pool, memory
```

To build a different Isabelle version:

```bash
docker build -f deploy/Dockerfile --build-arg ISABELLE_VERSION=Isabelle2025-2 \
  -t isabelle-pool-server:2025-2 .
```

The official download server (`isabelle.in.tum.de`) is sometimes down or slow; the build
abandons any mirror under 1 MB/s and falls back through `isabelle.sketis.net` → Cambridge →
Proofcraft → Clarkson.

#### A5. Prebuild heaps (optional, recommended for heavy imports)

With `ISABELLE_INITIAL_SESSIONS=0` the first session request pays session creation (~1 min).
If your theories import heavy sessions (`HOL-Library`, `HOL-Analysis`,
`HOL-Computational_Algebra`, …), build their heaps once so sessions and big-step
verification start from a cached image. Heaps persist in the `isabelle_user_data` volume.

```bash
docker compose exec isabelle-pool-server isabelle build -b HOL-Library
# HOL-Analysis-scale heaps: build with the server idle (they share the 14 GB cgroup cap)
curl http://localhost:8000/api/v1/heaps/available
```

#### A6. Docker operating notes

- **Logs:** `docker compose logs -f isabelle-pool-server` (server stdout); the rotating file log
  (10 MB × 5) is also at `logs/server.log` on the host (the repo is mounted at `/app`);
  gateway JVM output in `logs/gateway-jvm.log`.
- **Memory limit:** `mem_limit: 14g` in `docker-compose.yml` — must stay **below** the Docker
  VM's own memory or the cgroup-aware admission gate goes blind. On smaller machines lower it
  *and* `ISABELLE_POOL_SIZE`; the gate refuses new sessions (HTTP 503) near the limit instead
  of letting the OOM killer take the JVM.
- **ML heap cap:** the entrypoint writes `ML_OPTIONS="--minheap 500 --enablegcsharing
  --maxheap $ISABELLE_ML_MAXHEAP_MB"` into the Isabelle user settings on the volume (default
  9 GB) so a single runaway poly process fails cleanly instead of OOM-killing the container.
- **After an image rebuild**, if the server fails with `Not found: py4j`: the pre-existing
  volume shadows the component registration. The entrypoint re-registers on every start; the
  manual fix is `docker compose exec isabelle-pool-server ./server/repl/Admin/init`
  ([ISSUES.md](docs/ISSUES.md) Bug 7).
- **Monitoring:** `docker compose up -d prometheus grafana cadvisor` — Grafana on `:3000`
  (admin/admin, "Isabelle Pool Server" dashboard), Prometheus on `:9090`, raw metrics at
  `/metrics`.
- **Pre-built turnkey image** (no build, heaps included, for reproducing published results):
  distributed separately as a `docker load`-able tarball; runbook in
  [deploy/RC0-image-instructions.md](deploy/RC0-image-instructions.md).

### B. Native Linux server setup

For a machine where you want Isabelle and the server running directly on the host (no
Docker). Tested shape: Ubuntu 22.04/24.04, a regular user with `sudo`, repo at
`~/Isabelle-Pool-Server`, Isabelle at `/opt/isabelle`. Adjust paths as you like — everything is
driven by `ISABELLE_HOME` and `.env`.

#### B1. System packages

```bash
sudo apt-get update
sudo apt-get install -y git curl tar gzip ca-certificates procps \
     python3 python3-venv python3-pip \
     openjdk-21-jdk-headless \
     fontconfig fonts-dejavu-core
```

- Python **3.10+** (22.04 ships 3.10, 24.04 ships 3.12).
- The JDK (17+) is needed **only by Gradle** to compile the Scala backend. Isabelle 2026
  bundles its own JDK and fails with `Unknown JAVA_HOME` if a system `JAVA_HOME` is exported
  globally — so do **not** put `JAVA_HOME` in your profile; it is scoped to the build step
  in B5.
- `fontconfig` + one font family are **required even though nothing is displayed**:
  Isabelle 2026 initialises the font subsystem on every session start and the JVM dies
  with `Fontconfig head is null` without them.

#### B2. Install Isabelle

```bash
VERSION=Isabelle2026-RC2                                  # or Isabelle2026 / Isabelle2025-2
case "$(uname -m)" in aarch64|arm64) T="${VERSION}_linux_arm.tar.gz";; *) T="${VERSION}_linux.tar.gz";; esac
# release candidates live under website-<version>/dist/, final releases under dist/
curl -fLO "https://isabelle.in.tum.de/website-${VERSION}/dist/${T}" \
  || curl -fLO "https://isabelle.in.tum.de/dist/${T}" \
  || curl -fLO "https://isabelle.sketis.net/website-${VERSION}/dist/${T}"
sudo mkdir -p /opt && sudo tar -xf "$T" -C /opt && sudo mv "/opt/${VERSION}" /opt/isabelle
sudo chown -R "$USER" /opt/isabelle
rm "$T"
```

Make it permanent for your user (the server, the gateway JVM, and the heap pool all
resolve the launcher from `ISABELLE_HOME`):

```bash
cat >> ~/.profile <<'EOF'
export ISABELLE_HOME=/opt/isabelle
export PATH="$ISABELLE_HOME/bin:$PATH"
EOF
source ~/.profile
isabelle version          # sanity check
```

Mirrors if `isabelle.in.tum.de` is down: `https://www.cl.cam.ac.uk/research/hvg/Isabelle/dist/`,
`https://proofcraft.systems/isabelle/dist/`, `https://mirror.clarkson.edu/isabelle/dist/`.

#### B3. Clone and install the Python side

```bash
git clone https://github.com/FullBackward/Isabelle-Pool-Server.git ~/Isabelle-Pool-Server
cd ~/Isabelle-Pool-Server
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirement.txt -r mcp_servers/requirements.txt   # note: requirement.txt, singular
pip install -e . -e ./client
```

#### B4. Register the REPL component with Isabelle

```bash
chmod +x server/repl/Admin/init server/repl/Admin/*.sh server/repl/gradlew
./server/repl/Admin/init
```

This registers `server/repl` as an Isabelle component and fetches the `py4j` and `spliff`
contribs into `~/.isabelle/<version>/contrib/`. Idempotent — re-run it after moving the
repo or upgrading Isabelle.

#### B5. Build the Scala backend

```bash
cd server/repl
JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$(which javac)")")")" ./gradlew build --no-daemon
cd ../..
```

The first run also builds `isabelle.jar` via `isabelle scala -e` (several minutes).
Re-run after any change under `server/repl/src/`, then restart the server.

#### B6. ML heap cap (strongly recommended)

Without a cap, one pathological theory can grow a Poly/ML process until the kernel OOM
killer takes down the whole server. The Docker entrypoint writes this into the Isabelle
user settings; do the same by hand. It **must** live in the user settings file — the
polyml component's own `etc/settings` forces `ML_OPTIONS=""`, so an environment variable
is ignored — and it replaces the platform default, so the defaults are restated:

```bash
SETTINGS="$(isabelle getenv -b ISABELLE_HOME_USER)/etc/settings"
mkdir -p "$(dirname "$SETTINGS")"
cat >> "$SETTINGS" <<'EOF'
# Isabelle Pool Server: hard per-process ML heap cap (MB). Size it to leave room for POOL_SIZE sessions.
ML_OPTIONS="--minheap 500 --enablegcsharing --maxheap 9216"
# Optional: GC logs for every Isabelle-launched JVM (the gateway in particular).
ISABELLE_TOOL_JAVA_OPTIONS="$ISABELLE_TOOL_JAVA_OPTIONS -Xlog:gc*:file=/home/USER/Isabelle-Pool-Server/logs/isabelle-jvm-gc-%p.log:time,uptime,level,tags:filecount=3,filesize=10M"
EOF
sed -i "s#/home/USER#$HOME#" "$SETTINGS"
mkdir -p ~/Isabelle-Pool-Server/logs
```

#### B7. Configure and run

Nothing loads `.env` automatically outside Docker, so export it into the shell (or let
systemd do it, B8). Three defaults are container paths and must be overridden on a host:

```bash
cd ~/Isabelle-Pool-Server
cp .env.example .env
python3 -c 'import secrets; print("ISABELLE_ADMIN_TOKEN=" + secrets.token_hex(16))'   # paste into .env
cat >> .env <<EOF
# --- native-host overrides (the defaults are the container's /app and /root paths) ---
ISABELLE_HEAP_POOL_DIR=$HOME/.isabelle/heap_pool
ISABELLE_HEAP_POOL_ALLOWED_ROOTS=$HOME/Isabelle-Pool-Server:$HOME/.isabelle
ISABELLE_SERVER_LOG_DIR=$HOME/Isabelle-Pool-Server/logs
EOF
```

Later lines override the defaults above them (both for `source` and for systemd's
`EnvironmentFile`). `.env` value lines must not carry inline `# comments` (the integer knobs
are parsed with `int()`). Then:

```bash
set -a; source .env; set +a
source .venv/bin/activate
python -m server.app.main              # foreground; Ctrl-C stops it
```

In another shell:

```bash
curl http://localhost:8000/healthz     # {"status":"alive"}   (1-2 min for the gateway JVM)
curl http://localhost:8000/            # gateway_alive: true, pool, memory
```

Memory note: the admission gate reads the cgroup when it runs in a container; on a bare
host it falls back to the machine's `MemTotal`, so `ISABELLE_MEMORY_PRESSURE_THRESHOLD` is a
percentage of **all** host RAM. Keep `ISABELLE_POOL_SIZE × 4 GB + 9 GB` (one capped build)
under the memory you are willing to give it.

#### B8. Run as a systemd service

```bash
sudo tee /etc/systemd/system/isabelle-pool-server.service > /dev/null <<EOF
[Unit]
Description=Isabelle Pool Server
After=network.target

[Service]
User=$USER
WorkingDirectory=$HOME/Isabelle-Pool-Server
EnvironmentFile=$HOME/Isabelle-Pool-Server/.env
Environment=ISABELLE_HOME=/opt/isabelle
Environment=PATH=/opt/isabelle/bin:$HOME/Isabelle-Pool-Server/.venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$HOME/Isabelle-Pool-Server/.venv/bin/python -m server.app.main
Restart=on-failure
RestartSec=10
TimeoutStopSec=120
KillMode=control-group

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now isabelle-pool-server
systemctl status isabelle-pool-server
journalctl -u isabelle-pool-server -f           # live log (also logs/server.log)
```

`KillMode=control-group` matters: a stop must take the gateway JVM and every `poly`
process with it. `TimeoutStopSec=120` gives the server time to close sessions cleanly.

#### B9. Prebuild heaps (optional)

Same reasoning as A5. Build with the server **stopped or idle** — a `HOL-Analysis` build
needs several GB on its own:

```bash
isabelle build -b HOL-Library
isabelle build -b -j 2 HOL-Analysis
curl http://localhost:8000/api/v1/heaps/available
```

#### B10. Native operating notes

- **Logs:** `logs/server.log` (rotating, 10 MB × 5), `logs/gateway-jvm.log` (JVM
  stdout/stderr), `journalctl -u isabelle-pool-server`.
- **Upgrading the code:** `git pull`, then `pip install -r requirement.txt`, re-run B4+B5 if
  anything under `server/repl/` changed, `sudo systemctl restart isabelle-pool-server`.
- **Upgrading Isabelle:** install the new version at `/opt/isabelle` (B2), re-run B4, B5, B6
  — the user settings and contribs live under `~/.isabelle/<version>/` and are per-version.
  Old heaps are not reusable.
- **Stuck processes:** `pgrep -af "poly|isabelle"` — after `systemctl stop isabelle-pool-server`
  nothing should remain; if it does, the gateway did not get the signal (check `KillMode`).
- **No `Not found: py4j`-style volume problems** exist natively; if the gateway fails to
  start, run `./server/repl/Admin/init` again and check `isabelle components -l`.

---

## Usage

Everything below is identical for both setups. The server listens on `0.0.0.0:8000` with
**no authentication** — keep it firewalled (`sudo ufw allow from <your-ip> to any port 8000`)
or tunnel over SSH (`ssh -N -L 8000:localhost:8000 user@server`).

### 1. Health, docs, admin

```bash
curl http://localhost:8000/healthz     # liveness: {"status":"alive"}
curl http://localhost:8000/readyz      # readiness: 200 once the gateway JVM is up, else 503
curl http://localhost:8000/            # version, gateway_alive, active/busy sessions, memory
```

- Interactive API docs (Swagger UI): `http://localhost:8000/docs`; raw spec at `/openapi.json`.
- Admin console: `http://localhost:8000/admin` — pool contents, heap images, force-close.
  Destructive actions need `ISABELLE_ADMIN_TOKEN` from `.env` (without it they render
  locked). The console page itself is unauthenticated — one more reason to firewall the port.
- Prometheus metrics: `/metrics`.

### 2. The HTTP API in three calls

Sessions are **leased**: `acquire` gives you a `session_id` and a `lease_id`; every mutating
call carries the lease in the `X-Lease-Id` header; `release` hands the session back to the
warm pool for reuse. This is exactly what `deploy/setup.sh --verify` does.

```bash
# 1. acquire a warm session (or create one) on HOL with Main imported
RESP=$(curl -s -X POST localhost:8000/api/v1/sessions/acquire \
  -H 'Content-Type: application/json' -d '{"theories": ["Main"], "field": "HOL"}')
SID=$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["session_id"])')
LEASE=$(echo "$RESP" | python3 -c 'import json,sys; print(json.load(sys.stdin)["lease_id"])')

# 2. verify a whole proof chunk under one wall budget (seconds)
curl -s -X POST "localhost:8000/api/v1/sessions/$SID/verify_chunk" \
  -H 'Content-Type: application/json' -H "X-Lease-Id: $LEASE" \
  -d '{"chunk": "theorem t: \"rev (rev xs) = xs\" by (induct xs) auto", "timeout": 60}'
# → {"success": true, "proof_open": false, "pending_qed": false, "used_sorry": false,
#    "timed_out": false, "stuck_line": null, "execution_time": ...,
#    "commands": [{"index": 0, "line": 1, "kind": "theorem", "status": "ok", ...}]}

# 3. release the lease (the session stays warm for the next caller)
curl -s -X POST "localhost:8000/api/v1/sessions/$SID/release" -H "X-Lease-Id: $LEASE"
```

The first `acquire` for an import set pays session creation (~1 min; longer if a heap has
to be built — see A5/B9). A `timeout` on `verify_chunk`/`commands` is a hard wall budget:
an expired command is rolled back and reported `success=false` with `stuck_line` naming
the still-running command — nothing keeps running in the background.

**The one rule for judging results:** a theorem is proved only when
`success=true` **and** `proof_open=false` **and** `used_sorry=false`. `success` alone means
"no command errored" — an open or `sorry`-closed proof is not a result.

Other endpoints (all under `/api/v1/sessions/{id}`, lease required): `POST commands` (one
Isar command, small-step), `GET state` / `subgoals` / `goals` / `source` / `facts/local` /
`facts/global`, `POST checkpoints` / `checkpoints/{id}/restore` / `rollback`,
`POST diagnostic` (read-only `thm`/`term`/`find_theorems`), `POST sledgehammer`,
`PUT document` (replace the whole theory text, incremental sync), positional queries
(`command_at_line`, `hover`, `definition`, `sledgehammer_at`). Lease-free:
`POST /api/v1/sessions/bigstep` (whole-theory `isabelle build`),
`POST /api/v1/parse_theory_header`, `GET /api/v1/heaps/available`. Browse them at `/docs`.

### 3. The Python client

`client/` is a standalone package (httpx only) — install it where your agent runs:

```bash
pip install -e ./client        # or copy the client/ directory into your project
```

```python
import asyncio
from client import PoolAsyncClient

async def main():
    async with PoolAsyncClient("http://localhost:8000") as gym:
        s = await gym.acquire_session(theories=["Main"], field="HOL")
        sid, lease = s["session_id"], s["lease_id"]
        try:
            r = await gym.verify_chunk(
                sid, 'theorem t: "rev (rev xs) = xs" by (induct xs) auto',
                timeout=60, lease_id=lease)
            proved = r["success"] and not r["proof_open"] and not r["used_sorry"]
            print("proved:", proved)
            if not proved:                      # ask sledgehammer on the open goal
                print(await gym.sledgehammer(sid, lease_id=lease))
        finally:
            await gym.release_session(sid, lease_id=lease)

asyncio.run(main())
```

Same surface as the HTTP API: `execute_command`, `get_proof_state`, `save_checkpoint` /
`restore_checkpoint` / `rollback`, `diagnostic`, `load_document`, `verify_bigstep_text` /
`verify_bigstep_file`. [examples/demo.ipynb](examples/demo.ipynb) walks through all of it
end to end, including both MCP servers.

### 4. Connecting an LLM agent (MCP)

The MCP servers are thin agent-facing layers over the HTTP API:

```
LLM agent  ⇄  MCP server (stdio or streamable-HTTP)  ⇄  Isabelle Pool Server  ⇄  Isabelle
```

There are two, for two agent shapes (rationale: [DESIGN_CHOICES.md](docs/DESIGN_CHOICES.md) §2.9):

| Server | Launch | Shape |
|---|---|---|
| `mcp_servers.stepwise` | `python -m mcp_servers.stepwise.app` | **Chunk-centric.** The agent submits Isar text; `verify_chunk` is the one execution tool, failed text rolls back. Env prefix `ISABELLE_MCP_`, HTTP port 8848. Walkthrough below. |
| `mcp_servers.lsp` | `python -m mcp_servers.lsp.app` | **File-sync (LSP-style).** Tools take a `file_path`; the file is re-read from disk before every query and the broken state is kept for inspection. Env prefix `ISABELLE_MCP_LSP_`, HTTP port 8849. See [mcp_servers/lsp/README.md](mcp_servers/lsp/README.md). |

Sessions and leases are managed automatically per MCP connection — the agent never sees a
`session_id`. Each connection gets an isolated Isabelle session; a fresh `enter_theory`
always starts from a clean document.

#### Prerequisites (on the machine that runs the agent / MCP client)

```bash
cd Isabelle-Pool-Server
pip install -r mcp_servers/requirements.txt httpx
```

The MCP server needs `PYTHONPATH` pointing at the repo root (so `client` and `mcp_servers`
import) and the gym server URL (default `http://localhost:8000`). The gym server must be
running.

#### Option A — stdio (local agents: Claude Code, Claude Desktop, Cursor)

The client spawns the MCP server as a subprocess; one process per connection.

**Claude Code** (either command works):

```bash
claude mcp add isabelle-pool-server \
  --env PYTHONPATH=/absolute/path/to/Isabelle-Pool-Server \
  --env ISABELLE_MCP_GYM_URL=http://localhost:8000 \
  -- python -m mcp_servers.stepwise.app
```

or drop a `.mcp.json` in your project:

```json
{
  "mcpServers": {
    "isabelle-pool-server": {
      "command": "python",
      "args": ["-m", "mcp_servers.stepwise.app"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/Isabelle-Pool-Server",
        "ISABELLE_MCP_GYM_URL": "http://localhost:8000"
      }
    }
  }
}
```

**Claude Desktop:** the same JSON block under `mcpServers` in
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows). **Cursor:** same block in
`.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global). **Your own agent loop:**
spawn `python -m mcp_servers.stepwise.app` over stdio with those two env vars;
`evaluation/MCP-comparison/common/mcp_client.py` is a minimal working example with the
`mcp` Python SDK (spawn → `initialize` → `tools/list` → `tools/call`).

#### Option B — streamable-HTTP (remote server, multiple agents)

Run the MCP server as a standalone service next to the gym server:

```bash
cd Isabelle-Pool-Server
PYTHONPATH=. ISABELLE_MCP_TRANSPORT=streamable-http \
  ISABELLE_MCP_HOST=0.0.0.0 ISABELLE_MCP_PORT=8848 \
  python -m mcp_servers.stepwise.app
```

Point HTTP-capable MCP clients at `http://<server>:8848/mcp`. Concurrent connections are
isolated from each other (per-connection sessions). Like the gym API there is no built-in
auth — firewall the port or tunnel (`ssh -N -L 8848:localhost:8848 user@your-server`).

#### What the agent gets (stepwise server)

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

#### MCP configuration (env vars, stepwise server)

| Variable | Default | Meaning |
|---|---|---|
| `ISABELLE_MCP_GYM_URL` | `http://localhost:8000` | The gym HTTP server |
| `ISABELLE_MCP_FIELD` | `HOL` | Default Isabelle session for new theories |
| `ISABELLE_MCP_CHUNK_TIMEOUT` | `180` | Default wall budget (s) per `verify_chunk` |
| `ISABELLE_MCP_HTTP_TIMEOUT` | `600` | httpx timeout (must exceed chunk timeout) |
| `ISABELLE_MCP_MAX_PARALLEL` | `4` | Cap for `verify_batch` fan-out |
| `ISABELLE_MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `ISABELLE_MCP_HOST` / `ISABELLE_MCP_PORT` | `127.0.0.1` / `8848` | HTTP transport bind |

The lsp server takes the same names with the `ISABELLE_MCP_LSP_` prefix (plus its own
`LOAD_TIMEOUT`, `ATTEMPT_TIMEOUT`, `SCRATCH_POOL_SIZE`, `TASK_GROUP`, `CLOSE_DESTROYS`).

#### MCP smoke test

With the gym server running, verify the MCP layer end-to-end without any agent:

```bash
cd Isabelle-Pool-Server
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

### 5. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `/readyz` stays 503 for minutes | Gateway JVM still starting (1–2 min normal), or it died — check `logs/gateway-jvm.log`; natively also `isabelle components -l` and re-run `server/repl/Admin/init` |
| `Not found: py4j` on start (Docker) | Stale `isabelle_user_data` volume — `docker compose exec isabelle-pool-server ./server/repl/Admin/init` (Bug 7) |
| `Fontconfig head is null` in the JVM log (native) | Install `fontconfig fonts-dejavu-core` (B1) |
| `Unknown JAVA_HOME` from `isabelle` (native) | A global `JAVA_HOME` is exported; remove it, keep it scoped to the Gradle step (B5) |
| HTTP 503 "memory pressure" | The admission gate is protecting the host — lower `ISABELLE_POOL_SIZE`, raise `mem_limit`, or wait for idle sessions to be evicted |
| First `acquire` takes many minutes | A heap for a heavy import set is being built — prebuild it (A5/B9) |
| `verify_chunk` returns `success=false, timed_out=true` | The budget expired; `stuck_line` names the looping command; it was rolled back — retry with a larger `timeout` or a different proof |
| `McpError: Connection closed` immediately | The MCP subprocess died on startup — missing `PYTHONPATH` or pip deps; run `PYTHONPATH=. python -m mcp_servers.stepwise.app` by hand to see the traceback |
| `enter_theory` hangs then errors | Gym server not running / wrong `ISABELLE_MCP_GYM_URL`, or a heap is being built |
| `success=True` but the agent isn't done | Working as intended — check `proof_open` / `used_sorry` |

---

## Beyond the basics

- **MCP comparison harness** (this MCP vs Isabelle-MCP vs AutoCorrode I/Q):
  [evaluation/MCP-comparison/README.md](evaluation/MCP-comparison/README.md); experiment
  write-ups under [docs/experiments/](docs/experiments/).
- **Evaluation scripts** for small-step/big-step benchmarking: `evaluation/scripts/`
  (each runs as `python -m evaluation.scripts.<name>`); consolidated results in
  [evaluation/results/](evaluation/results/README.md).
- **Developer docs:** [CLAUDE.md](CLAUDE.md) / [AGENTS.md](AGENTS.md) (architecture,
  conventions, env reference), [DESIGN_CHOICES.md](docs/DESIGN_CHOICES.md) (rationale),
  [ISSUES.md](docs/ISSUES.md) (bug log). The older API/client reference PDFs and the
  1.0/2.0 reports live in `archive/previous-works/`.
- **Running the tests:** `pytest` from the repo root (~390 unit tests, no Isabelle needed).
