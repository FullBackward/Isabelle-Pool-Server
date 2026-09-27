# mcp_lsp_server — LSP-like MCP server (file-sync workflow)

An MCP server for agent-driven Isabelle development over *files*, in the spirit
of lean-lsp-mcp: tools take a `file_path` (never a session id), the file is
re-synced from disk before every query, and execution happens on warm scratch
sessions — never on the file's session. Built strictly on top of
`IsabelleGymAsyncClient` (no server-core edits); needs the IsabelleGym HTTP
server running (`docker compose up -d`, `python -m server.app.main` in the
container, port 8000).

**Positions everywhere are 1-based line/column, UTF-16 columns** (editor/LSP
convention, matching the server's report `range` fields).

## Sync model (copied buffer)

The MCP never writes files. Per open file it keeps a text cache; **every
file-scoped tool call first re-reads the file from disk**; if the text changed
since the last sync, the new text is pushed via `load_document(report=true)`
(the lean-lsp-mcp `reload_from_disk` analog). If the server evicted the session
(404), the binding is recreated transparently and the file reloaded.

**Security**: like the HTTP API it wraps, this server has NO authentication —
the streamable-http transport binds 127.0.0.1 by default; deploy behind a
firewall or SSH tunnel, never expose the port publicly.

## Tools

Lifecycle:
| Tool | What it does |
|---|---|
| `isabelle_open(file_path, task_group?, heap_session?, label?)` | Bind a file to a session (auto-acquired on first use with defaults if not called; released same-dependency-key sessions are reused warm — every sync resets via load_document, so dirty reuse is safe); heap context chosen here. The acquire passes the file's own header imports as `theories`, so imports beyond `Main` (Complex_Main, `"HOL-Analysis.Derivative"`, …) resolve — see the theories-at-acquire fix |
| `isabelle_close(file_path, destroy?)` | Release the session and unbind. With `destroy=true` (or `ISABELLE_MCP_LSP_CLOSE_DESTROYS=true`) the session is torn down immediately instead of released warm — the sanctioned way to free a multi-GB session; reopening the file rebinds transparently |
| `isabelle_sync(file_path)` | Force a disk→session sync check (normally implicit) |

Read-only (each syncs first):
| Tool | What it does |
|---|---|
| `isabelle_diagnostic_messages(file_path, severity?)` | Per-command errors/warnings with line/col ranges + status (severity filter: `error`/`warning`) |
| `isabelle_goal(file_path, line)` | Goal state before/after the command at `line` |
| `isabelle_command_at_line(file_path, line)` | The command containing `line` (+ range) |
| `isabelle_proof_state(file_path)` | Tip subgoals / proof_finished / pending_qed |
| `isabelle_source(file_path)` | The source as the prover sees it |
| `isabelle_query(file_path, command)` | One read-only query command — `thm`, `term`, `prop`, `typ`, `prf`, `find_theorems`, `find_consts`, `find_*`, any `print_*` (guarded server-side; ML/IO rejected) |
| `isabelle_local_facts(file_path)` / `isabelle_global_facts(file_path, limit?)` | Proof-context vs theory-level facts |
| `isabelle_hover_info(file_path, line, column)` | Entity kind + type/statement |
| `isabelle_definition(file_path, line, column)` | Def positions: file targets (heap/distribution sources) or in-node line ranges (entities defined in the file itself) |
| `isabelle_sledgehammer(file_path, line?, subgoal?, timeout_s?)` | Tip goal (no `line`) or positioned at `line` (+subgoal) |
| `isabelle_checkpoint(file_path)` / `isabelle_restore(file_path, checkpoint_id)` / `isabelle_rollback(file_path)` | Snapshot machinery on the file session |
| `isabelle_history(file_path)` / `isabelle_last_report(file_path)` | Command history / retained last report |

Scratch execution (never touch the file session; warm pool keyed by
task_group/heap/imports/field, `ISABELLE_MCP_LSP_SCRATCH_POOL_SIZE`):
| Tool | What it does |
|---|---|
| `isabelle_multi_attempt(file_path, line, candidates, timeout?)` | File prefix before `line` + each candidate, verified concurrently on scratch sessions (bounded by `ISABELLE_MCP_LSP_MAX_PARALLEL`); per-candidate success/proof_open/failed commands (candidate-relative lines) |
| `isabelle_run_code(chunk, imports?, task_group?, heap_session?, timeout?)` | Independent snippet on a scratch session (own `theory` header or body of a scratch theory importing `imports`, default Main) |

Heap pool:
| Tool | What it does |
|---|---|
| `isabelle_build_heap(task_group, project, session_name?)` | `isabelle build -b` the project's top-level .thy files into a verified heap (long-running) |
| `isabelle_heap_status(task_group?, project?)` | With both args: the full manifest (file hashes, ROOT text, fingerprint, log tail). Otherwise: pool listing **plus `available_heaps`** — every base session image on disk (HOL-Analysis, distribution heaps, pool-built), so you can see what sessions can start from before naming `heap_session`/`field` anywhere |

Server endpoints the agent may also want (REST, same server the MCP wraps):
`GET /api/v1/heaps/available` (the raw `available_heaps` listing) and
`POST /api/v1/parse_theory_header {text}` → `{theory_name, imports,
suggested_field}` — the server's canonical header parse (comment-stripped,
header-anchored); use it instead of a local regex so all consumers share one
parser (client wrapper: `async_client.parse_theory_header`).

## Configuration (`ISABELLE_MCP_LSP_*` env vars)

| Var | Default | Meaning |
|---|---|---|
| `..._GYM_URL` | `http://localhost:8000` | IsabelleGym HTTP server |
| `..._FIELD` / `..._TASK_GROUP` | `HOL` / `default` | session field / default heap task group |
| `..._HTTP_TIMEOUT` | 600 | httpx timeout (s) |
| `..._LOAD_TIMEOUT` | 120 | load_document sync budget (s) |
| `..._ATTEMPT_TIMEOUT` | 180 | per-candidate/run_code budget (s) |
| `..._MAX_PARALLEL` | 4 | multi_attempt fan-out cap |
| `..._SCRATCH_POOL_SIZE` | 4 | warm scratch sessions per context |
| `..._CLOSE_DESTROYS` | false | `isabelle_close` destroys (teardown) instead of warm-releasing |
| `..._TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `..._HOST` / `..._PORT` | `127.0.0.1` / `8849` | streamable-http bind |

## Run

```bash
pip install mcp httpx        # plus the repo's requirement.txt deps
PYTHONPATH=. python -m mcp_servers.lsp.app                                   # stdio
ISABELLE_MCP_LSP_TRANSPORT=streamable-http PYTHONPATH=. python -m mcp_servers.lsp.app   # HTTP on :8849
```

## Notes / deferrals (v1)

- Header imports must be qualified heap names for heap-backed files
  (`imports "MySession.Bar"`); bare project-relative names fail the header gate
  (server-side normalization is a documented deferral).
- Scratch sessions are held leased for reuse; they are reclaimed by the
  server's abandoned-lease reaper if the MCP dies.
- No completions / code actions / widgets yet.
