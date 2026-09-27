# MCP comparison harness

This directory compares Isabelle MCP servers on the same set of `.thy` problems:

1. **IsabelleGym MCP** (`run_isabellegym.py`) — this repo (chunk-centric MCP)
2. **IsabelleGym LSP MCP** (`run_isabellegym_lsp.py`) — this repo (file-sync LSP-like MCP)
3. **Isabelle-MCP** (`run_isabelle_mcp.py`) — `~/GitHub/Isabelle-MCP`
4. **AutoCorrode I/Q** (`run_autocorrode_iq.py`) — `~/GitHub/AutoCorrode`

The harness uses a **shared OpenAI-compatible chat client** (`common/model.py` — DeepSeek,
Kimi, or any compatible endpoint) and the same agent-loop structure for all three systems.
Only the MCP-specific tool dispatch differs.

---

## Directory layout

```
MCP-comparison/
├── README.md                    # this file
├── config.yaml                  # default configuration
├── config.local.yaml            # (optional, untracked) local overrides
├── common/                      # shared utilities
│   ├── config.py                # load config + env vars
│   ├── model.py                 # OpenAI-compatible chat client + no-tool-call policy
│   ├── mcp_client.py            # MCP stdio client (captures vendor instructions)
│   ├── problems.py              # parse .thy files
│   ├── metrics.py               # result schema, JSONL, timing
│   └── arbiter.py               # neutral isabelle build checker
├── problems/                    # benchmark .thy files (theorem … sorry)
├── run_isabellegym.py           # IsabelleGym runner
├── run_isabellegym_lsp.py       # IsabelleGym LSP-MCP runner (file-sync workflow)
├── run_isabelle_mcp.py          # Isabelle-MCP runner
├── run_autocorrode_iq.py        # AutoCorrode I/Q runner
├── analyze.py                   # print summary tables
└── runs/                        # results + final .thy artifacts (per system)
```

---

## Prerequisites

### Python dependencies

From the repo root:

```bash
pip install -r mcp_servers/requirements.txt   # mcp + httpx
pip install openai pyyaml
```

### Environment variables

Model provider key (pick one matching `model.provider` / `model.api_key_env`):

```bash
export DEEPSEEK_API_KEY="your-deepseek-key"   # current default provider
# or: export KIMI_API_KEY="your-moonshot-key"
```

Optional, for I/Q:

```bash
export IQ_AUTH_TOKEN="eval-secret-token"      # or paste into MCP-comparison/iq_token.txt
export IQ_MCP_ALLOWED_ROOTS="/abs/path/to/MCP-comparison/runs/autocorrode/work"
```

### Backends

| System | Required backend |
|---|---|
| IsabelleGym | IsabelleGym HTTP server running on `http://localhost:8000` |
| Isabelle-MCP | A running Docker container built from `Isabelle-MCP/container/` (or a native patched Isabelle with `isabelle-mcp` on PATH) |
| AutoCorrode I/Q | Isabelle/jEdit running with the I/Q plugin listening on `127.0.0.1:8765` |

The **arbiter** (used by all three runners after every attempt) also needs the IsabelleGym
server on `http://localhost:8000`.

---

## Configuration

Edit `MCP-comparison/config.yaml`, or create `MCP-comparison/config.local.yaml` to override
without touching versioned defaults (the local file is not tracked by git).

Current key knobs (see the files for the full set):

```yaml
model:
  provider: deepseek
  base_url: https://api.deepseek.com/v1
  api_key_env: DEEPSEEK_API_KEY
  model_id: deepseek-v4-pro
  temperature: 0.3
  max_tokens: 32768        # keep high: reasoning models burn hidden tokens first

budgets:
  max_rounds: 100
  problem_wall_cap_seconds: 2400    # 40 min per attempt
  tool_timeout_seconds: 300         # 5 min per MCP tool call
  repeats: 5

mcp_servers:
  isabellegym:
    command: [python, -m, mcp_servers.stepwise.app]
  isabelle_mcp:
    command: [docker, exec, -i, isabelle-eval, isabelle-mcp]   # container mode
  autocorrode_iq:
    command: [python, C:/Users/winst/GitHub/AutoCorrode/iq/iq_bridge.py]
    env:
      IQ_MCP_BRIDGE_PORT: "8765"
      PYTHONUTF8: "1"                   # MCP stdio is UTF-8; defeats locale (GBK) mojibake
```

---

## Prepare problems

Each problem is a single `.thy` file containing exactly one `theorem … sorry`:

```isabelle
theory Putnam_1988_B1
  imports Main
begin

theorem putnam_1988_b1:
  fixes ...
  shows "..."
  sorry

end
```

Place them in `MCP-comparison/problems/` (or any directory passed with `--thy-dir`).

---

## Prompt variants

All runners accept `--prompt` (default differs per runner):

| variant | systems | content |
|---|---|---|
| `general` | all three | interface-level rules only: solver rule (+ fallback, escalation, timeout discipline), DONE criteria, unicode rule (file-based systems), no strategy coaching |
| `stepwise` | isabellegym | + layered incremental proving (REPL-style, small layers) |
| `segment` | isabellegym | + chunked segment submission with recovery examples (the most efficient IsabelleGym playbook) |
| `guided` | autocorrode I/Q | general + AutoCorrode's vendor playbook (`iq_guidance.md`) |
| `guided` | isabelle_mcp | general + the server's own `instructions.py`, captured live from the MCP initialize handshake |

Use `general` × 3 for the bare-interface comparison; the guided/stepwise/segment variants
for the product-playbook comparison.

---

## Run the comparison

### 1. IsabelleGym

Start the IsabelleGym server first:

```bash
python -m server.app.main
```

Then run:

```bash
python MCP-comparison/run_isabellegym.py --thy-dir MCP-comparison/problems --prompt segment --repeats 10
```

### 1b. IsabelleGym LSP (file-sync workflow)

The LSP-like MCP (`mcp_lsp_server/`) is READ-ONLY by design — it observes files.
So this runner differs in shape: the agent edits a per-attempt workdir copy of
the problem with LOCAL `read_file`/`write_file` tools (sandboxed to the workdir),
and the MCP sees each edit via its disk→session sync on the next query. Setup
per attempt is `isabelle_open(file_path)`; the verdict is the same neutral
arbiter on the final file state.

```bash
python -m server.app.main   # the LSP MCP talks to the same HTTP server
python MCP-comparison/run_isabellegym_lsp.py --thy-dir MCP-comparison/problems --repeats 10
```

Results land in `runs/isabellegym_lsp/` (same results.jsonl schema;
`analyze.py` picks it up as a fourth system row).

### 2. Isabelle-MCP

#### Option A — native (patched Isabelle on host)

Ensure patched Isabelle and `isabelle-mcp` are on PATH, set
`mcp_servers.isabelle_mcp.command: [isabelle-mcp]`, then:

```bash
python MCP-comparison/run_isabelle_mcp.py --thy-dir MCP-comparison/problems
```

#### Option B — Docker container (recommended, host-safe)

Build and start the container from `~/GitHub/Isabelle-MCP/container/`. Build with the
heaps your problems need — the default `SESSIONS="HOL"` is not enough for problems that
import `HOL-*` sessions (e.g. `HOL-Computational_Algebra`); `isabelle_launch` fails fast
otherwise. A pre-downloaded tarball at `container/isabelle/Isabelle2025-2_linux.tar.gz`
is used automatically if present.

```bash
cd ~/GitHub/Isabelle-MCP/container
mkdir -p work
docker build -t isabelle-eval --build-arg SESSIONS="HOL HOL-Computational_Algebra" .
docker run -d --name isabelle-eval -v "$PWD/work:/work" isabelle-eval sleep infinity
```

On Windows, mount with an absolute path:

```bash
docker run -d --name isabelle-eval -v "C:/Users/winst/GitHub/Isabelle-MCP/container/work:/work" isabelle-eval sleep infinity
```

Smoke test (optional):

```bash
docker run --rm isabelle-eval bash -lc "my-better-isabelle status && isabelle-mcp --version"
```

Configure `config.local.yaml` for container mode (already set up on this machine):

```yaml
mcp_servers:
  isabelle_mcp:
    command: [docker, exec, -i, isabelle-eval, isabelle-mcp]

isabelle_mcp_container:
  container_name: isabelle-eval
  host_work_dir: C:/Users/winst/GitHub/Isabelle-MCP/container/work
  container_work_dir: /work
```

Run:

```bash
python MCP-comparison/run_isabelle_mcp.py --thy-dir MCP-comparison/problems --repeats 10
```

The harness writes `.thy` files to `host_work_dir` and translates paths to `/work/...`
for the in-container server. Per attempt it spawns a fresh `docker exec` MCP process and
calls `isabelle_terminate` at teardown.

### 3. AutoCorrode I/Q

Start jEdit with the I/Q plugin (it must be rebuilt after `iq/src` changes and jEdit
restarted), then:

```bash
export IQ_AUTH_TOKEN="eval-secret-token"     # or paste into iq_token.txt (re-read each attempt)
export IQ_MCP_ALLOWED_ROOTS="C:/Users/winst/GitHub/IsabelleGym/MCP-comparison/runs/autocorrode/work"
```

Run:

```bash
python MCP-comparison/run_autocorrode_iq.py --thy-dir MCP-comparison/problems --repeats 10
```

### Run a subset / override repeats

```bash
python MCP-comparison/run_isabellegym.py --thy-dir MCP-comparison/problems --select putnam_1988
python MCP-comparison/run_isabellegym.py --thy-dir MCP-comparison/problems --repeats 20
```

---

## Arbiter

Each runner calls the neutral arbiter automatically after every attempt (it runs
`isabelle build` on the final file via the IsabelleGym server's bigstep endpoint; the
first call for a heavy parent session can take minutes — `ARBITER_BUILD_TIMEOUT_S`,
default 900 s). You can also run it manually:

```bash
python -m common.arbiter MCP-comparison/problems/Putnam_1988_B1.thy \
                         MCP-comparison/runs/isabellegym/Putnam_1988_B1_rep0.thy
```

The arbiter checks:
1. No `sorry`/`oops` in the final file.
2. The target theorem name is present.
3. `isabelle build` succeeds on a throwaway session importing the problem's imports.

---

## Analyze results

```bash
python MCP-comparison/analyze.py
```

Prints per-system summary tables (attempts, pass@1, mean rounds / productive rounds /
wall_s / setup_s / first_tool_s / tokens, truncated & nudge rounds), a per-repeat wall_s
table (warm/cold drift control), per-problem pass@1, and error classes.

---

## Output schema

Each runner appends one JSON line per `(system, problem, repeat)` to its `results.jsonl`:

```json
{
  "system": "isabellegym",
  "problem": "mathd_algebra_276",
  "repeat": 0,
  "rounds": 10,
  "n_tool_calls": 9,
  "n_truncated_rounds": 0,
  "n_nudge_rounds": 0,
  "wall_s": 179.8,
  "setup_s": 13.3,
  "first_tool_s": 0.13,
  "prover_s": 31.9,
  "model_s": 147.9,
  "round_latencies": [3.8, 42.1, 21.3],
  "input_tokens": 109274,
  "output_tokens": 10681,
  "total_tokens": 119955,
  "cached_tokens": 98000,
  "agent_claimed_solved": true,
  "arbiter_solved": true,
  "final_thy_path": "MCP-comparison/runs/isabellegym/mathd_algebra_276_rep0.thy",
  "error": null
}
```

Headline numbers use `arbiter_solved`. Field notes:

- `wall_s` — agent phase only (starts after setup); `setup_s` is recorded separately and
  is **not** a comparison metric (the three systems have fundamentally different setup
  models: warm jEdit vs fresh session vs LSP launch).
- `prover_s` — summed MCP tool time; `model_s ≈ wall_s − prover_s`.
- `n_nudge_rounds` — harness nudges (text-only rounds, DONE-gate rejections);
  productive rounds = `rounds − n_nudge_rounds`.
- Competitive metrics: **rounds, tokens, wall_s** (plus pass@1 at scale).

---

## Harness safeguards (affects how to read results)

- **DONE gate (all runners).** An agent's DONE is verified before acceptance: settled
  document, no errors, no sorries, closed proof (`pending_qed` on IsabelleGym). A false
  DONE costs a nudge round (max 2), then the arbiter judges.
- **Setup guards (I/Q).** Buffer reset + sorry-presence poll prevent phantom solves from
  stale jEdit buffers.
- **Auto seeding (IsabelleGym).** The theorem statement is pre-submitted so agents start
  from an open goal, matching the file-based systems' starting state.
- **Truncated rounds.** `finish_reason=length` rounds are nudged, not killed
  (`n_truncated_rounds` is informational).

---

## Notes and caveats

- **Sequential only.** Each runner processes problems one at a time; Isabelle-MCP and
  I/Q are single-session by design.
- **Fresh session per attempt.** Each repeat starts fresh (I/Q reuses the persistent
  jEdit editor by design — its setup resets the buffer instead).
- **Model provider.** Any OpenAI-compatible endpoint works; set `model.provider`,
  `base_url`, `api_key_env` in config. DeepSeek reasoning models consume hidden
  reasoning tokens against `max_tokens` — keep it high.
- **Tool schemas.** The model is given the raw tool list returned by each MCP server's
  `tools/list`.
- **Timeouts.** Hard problems (e.g. Putnam) will hit `max_rounds` / the wall cap; those
  attempts are recorded as distinct outcome classes in `analyze.py`, not plain failures.
- **Windows locale.** MCP stdio is forced to UTF-8 (`PYTHONUTF8=1`); the I/Q plugin
  socket is pinned to UTF-8 in `IQServer.scala`. Without both, non-English system
  locales (GBK) mangle Isabelle symbols.
