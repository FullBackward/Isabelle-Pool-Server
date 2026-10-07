# server/repl — the Isabelle REPL backend

This directory is an **Isabelle system component** (registered by `Admin/init`, which the
container entrypoint runs on every start) containing the Scala/ML layer that the FastAPI
server drives. It is launched only by the server — there is no user-facing CLI.

```
server/app (Python)  ──Py4J──▶  one shared gateway JVM (repl_backend_gateway.scala)
                                  └─ one ReplBackend per session ──▶ Isabelle/PIDE session
                                        └─ Query_Operations in src/ml/REPL.ML (overlays)
```

| Path | What it is |
|---|---|
| `src/main/scala/repl/` | The backend. `repl_backend_gateway.scala` is the Py4J entry point (factories for backends); `repl_backend.scala` + `backend_{lifecycle,chunk_ops,file_ops,probes}.scala` are the per-session operations; `repl_session.scala` owns the PIDE document (edits, checkpoints); `document_utils.scala` has `overlay_query` and the wall-bounded `settled_node_snapshot`; `edit_utils.scala` maps spliff diffs to sequential PIDE edits; `thy_*.scala` parse and track theory status; `session_manager.scala` starts Isabelle sessions (reads `ISABELLE_PARALLEL_PROOFS`). |
| `src/ml/REPL.ML` | ML-side `Query_Operation` registrations (`isabelle_pool_server_goals` / `in_proof` / `local_facts` / `global_facts` / `state` / `sledgehammer`) that run as PIDE overlays on the document's last command — no document edits, no ML→Scala channels. Registrations are at the top level, outside the `Repl` struct, on purpose. |
| `src/python/` | `repl_backend_gateway.py` spawns the JVM (`isabelle scala`), owns the Py4J bridge, redirects JVM output to `logs/gateway-jvm.log`; `thy_init.py` generates wrapper theories for import sets; `isabelle_client.py` / `isabelle_repl.py` / `operation.py` are the lower-level wrappers. |
| `thys/` | `IsabelleREPL.thy` (base theory for default sessions) and generated wrapper `.thy` files. |
| `Admin/` | `init` (component registration — the fix for ISSUES.md Bug 7; a fast no-op once done), `ensure_settings.sh` (ML heap cap + JVM GC logging in the user settings; shared with `deploy/native_setup.sh`), `container_entrypoint.sh` (init → settings → `exec`s the API server), `etc/`, admin Scala tools. |
| `etc/` | Isabelle component settings/options for this directory. |
| `etc/build.props` | Isabelle component build description: `isabelle scala_build` compiles `src/main/scala/repl/` into `lib/repl.jar` (Py4J/spliff come from the registered contribs). `build.gradle` / `gradlew` are kept for IDE import (Metals/IntelliJ) only. |

## Build

```bash
isabelle scala_build     # compiles lib/repl.jar with Isabelle's bundled JDK; no Gradle, no system JDK
```

`isabelle scala` runs `scala_build` implicitly, so the gateway start rebuilds a stale jar on
its own; running it by hand just surfaces compile errors earlier. Restart the server after
Scala changes (the gateway JVM loads the jar at spawn).

## Where the behaviour is documented

- Design rationale (why overlays, why one JVM, why wall budgets): `docs/DESIGN_CHOICES.md` §1.
- Bugs and their regression tests: `docs/ISSUES.md` (Bugs 9, 11, 14, 15, 18, 22 are backend-side).
- Environment knobs read on the Scala side: `ISABELLE_PARALLEL_PROOFS`, `ISABELLE_REPL_SETTLE_TIMEOUT`,
  `ISABELLE_REPL_SUBGOALS_TIMEOUT`, `ISABELLE_REPL_LOCAL_FACTS_TIMEOUT`, `ISABELLE_REPL_GLOBAL_FACTS_TIMEOUT_MINUTES`
  (see `AGENTS.md` "Important environment variables").
