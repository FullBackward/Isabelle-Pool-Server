# IsabelleGym Server — Issue Investigation & Fix Plan

**Date:** 2026-06-04 (revised)
**Scope:** Server layer (`server/`), REPL layer (`repl/`)
**Status:** Sledgehammer is implemented and verified — its section has been removed from this file (see `claude-work/impl-sledgehammer/` for the test/demonstration artifacts and notes). What remains below is the memory-management / session-closing review, re-checked against the current code.

---

## Table of Contents

1. [Memory Management / Session Closing](#memory-management--session-closing)
   - [Bug 1: Race Condition in `ThreadedBackend.close()` — RESOLVED](#bug-1-race-condition-in-threadedbackendclose--resolved)
   - [Bug 2: Join Timeout Too Short — RESOLVED](#bug-2-join-timeout-too-short--resolved)
   - [Bug 3: Leased Sessions Never Idle-Evicted — RESOLVED](#bug-3-leased-sessions-never-idle-evicted--open)
   - [Bug 4: TOCTOU on `in_use` Check — RESOLVED](#bug-4-toctou-on-in_use-check--open)
   - [Bug 5: Isabelle Processes Persist After Close — RESOLVED](#bug-5-isabelle-processes-persist-after-close--open)
2. [Bug 6: Gateway OOM Under Concurrent Sledgehammer — RESOLVED](#bug-6-gateway-oom-under-concurrent-sledgehammer--resolved)
3. [Bug 7: Stale `isabelle_user_data` Volume Shadows Component Registration — RESOLVED (workaround)](#bug-7-stale-isabelle_user_data-volume-shadows-component-registration-after-image-rebuild--resolved-workaround)
4. [Bug 8: `close()` Rejects Its Own `exit` Job — Sessions Never Torn Down — RESOLVED](#bug-8-close-rejects-its-own-exit-job--sessions-never-torn-down--resolved)
5. [Claude Work Log (dated)](#claude-work-log-dated)

---

## Memory Management / Session Closing

The full close call chain (current code) is:

```
HTTP DELETE /sessions/{id}
  → asyncio.to_thread(session_manager.close_session(...))   [session_manager.py:497]
  → session.close()                                         [session.py:562]
  → threaded_backend.close()                                [threaded_backend.py:62]
  → submit(self._backend.exit) on the worker thread         [threaded_backend.py:64]
  → ReplBackend.exit()   ← Py4J call to Scala               [repl_backend.scala:157]
  → Repl_ML_Communication.clear_channel(channel_id)
  → session_manager_instance.shutdown()                     [session_manager.scala:400]
  → remove_session_async(...) per running session  +  Server_Utils.stop_server(...)
```

**Status summary:** Bugs 1–6 are resolved in the current code. Bug 5 (Isabelle OS processes lingering after close) was re-checked on 2026-06-04 and is **not reproducible** — clean reaping verified for normal close and close-during-sledgehammer; the accumulation-to-OOM it described is explained by the now-fixed gateway-orphan-on-shutdown (`killpg`, `claude-work/fix-shutdown/`) and gateway-OOM-under-concurrency (Bug 6) issues. A narrow optional hardening item remains (force-kill fallback for a genuinely wedged ML process). See the [Claude Work Log](#claude-work-log-dated) for the dated history.

---

### Bug 1: Race Condition in `ThreadedBackend.close()` — RESOLVED

**File:** `server/app/services/threaded_backend.py`
**Severity:** High
**Status:** ✅ Resolved — current code matches the recommended fix.

#### Original root cause

The old `close()` called `self._backend.exit()` directly from the calling thread, bypassing the single-worker serialisation. Because the Py4J gateway uses one socket connection and the Scala `ReplBackend` is not thread-safe, that could collide with an in-flight job on the worker thread (`Py4JNetworkError` / corrupted Scala state).

#### Verification (current code)

`exit()` is now submitted through the job queue and awaited, so it is serialised after any pending job — exactly the recommended fix:

```python
def close(self) -> None:
    logger.info("closing threaded backend worker=%s", self._name)
    exit_fut = self.submit(self._backend.exit)   # queued, not called directly
    try:
        exit_fut.result(timeout=self.EXIT_TIMEOUT)
    except Exception:
        logger.exception("backend exit raised during close worker=%s", self._name)
    finally:
        self._stop.set()
        self._t.join(timeout=self.JOIN_TIMEOUT)
        logger.info("threaded backend closed worker=%s", self._name)
```

No further action needed.

---

### Bug 2: Join Timeout Too Short — RESOLVED

**File:** `server/app/services/threaded_backend.py`
**Severity:** High
**Status:** ✅ Resolved — timeouts are now configurable and generous.

#### Original root cause

The old code used `self._t.join(timeout=2.0)`. The Scala `shutdown()` (remove sessions, join async removals, stop the Isabelle server) routinely takes 5–30 s for any non-trivial theory, so the join timed out and `close()` returned while the worker (and Isabelle subprocess) was still alive.

#### Verification (current code)

The class reads its timeouts from config (`server/app/core/config.py`), defaulting to values long enough for the Scala shutdown:

```python
class ThreadedBackend:
    EXIT_TIMEOUT: float = Repl.BACKEND_EXIT_TIMEOUT   # ISABELLE_BACKEND_EXIT_TIMEOUT, default 60.0
    JOIN_TIMEOUT: float = Repl.BACKEND_JOIN_TIMEOUT   # ISABELLE_BACKEND_JOIN_TIMEOUT, default 5.0
    QUEUE_POLL_TIMEOUT: float = Repl.BACKEND_QUEUE_POLL  # default 0.1
```

`exit_fut.result(timeout=EXIT_TIMEOUT)` waits up to 60 s for the Scala shutdown, and the subsequent join is for a loop that has already stopped. No further action needed.

> Caveat: this guarantees the *Python* call waits for `exit()` to *return*. It does not by itself guarantee the Isabelle OS process actually died — see Bug 5.

---

### Bug 3: Leased Sessions Never Idle-Evicted — RESOLVED

**File:** `server/app/services/session_manager.py`, `cleanup_idle_sessions()`
**Severity:** Medium
**Status:** ✅ Resolved — current code force-evicts abandoned leased sessions older than `self.max_lease_age` (`Server.MAX_LEASE_AGE`, env `ISABELLE_MAX_LEASE_AGE`), exactly as in the fix below.

#### Original root cause

```python
async def cleanup_idle_sessions(self) -> None:
    while True:
        await asyncio.sleep(self.cleanup_interval)
        ...
        for sid, session in list(self._lru.items()):
            if session.leased:
                continue  # never evict a leased session   <-- unconditional
```

If a client acquires a lease and then disconnects (crash, network failure, killed process), the session is never reclaimed — a steady leak for ML training loops that lease many sessions. This is a likely contributor to pool exhaustion (`PoolExhausted` / HTTP 503 on `acquire`).

#### Fix

Force-close leases that have been idle longer than `idle_timeout * 2`. The helpers used below (`session.is_idle`, `session.last_activity`, `session.lease_id`, `session.status`, `close_session(require_lease=False)`) all exist in the current code.

```python
async def cleanup_idle_sessions(self) -> None:
    max_lease_age = self.idle_timeout * 2  # abandoned leases reclaimed after this

    while True:
        await asyncio.sleep(self.cleanup_interval)
        now = time.time()
        to_close: List[uuid.UUID] = []

        with self._lock:
            for sid, session in list(self._lru.items()):
                if session.status == SessionStatus.CLOSED:
                    continue
                if session.leased:
                    if session.is_idle(max_lease_age, now=now):
                        logger.warning(
                            "force-closing abandoned leased session "
                            "session_id=%s lease_id=%s idle_for=%.0fs",
                            sid, session.lease_id, now - session.last_activity,
                        )
                        to_close.append(sid)
                    continue
                if session.is_idle(self.idle_timeout, now=now):
                    to_close.append(sid)

        for sid in to_close:
            logger.info("closing idle session session_id=%s", sid)
            try:
                self.close_session(sid, require_lease=False)
            except Exception:
                logger.exception("failed to close idle session session_id=%s", sid)
```

Optionally make `max_lease_age` configurable via `ISABELLE_MAX_LEASE_AGE_SECONDS` in `server/app/core/config.py`.

> Note: also confirm `start_cleanup_task()` is actually invoked at startup — the cleanup coroutine only runs if `start_cleanup_task()` is called (e.g. from the FastAPI lifespan). If it is never started, *no* idle eviction happens at all (leased or not).

---

### Bug 4: TOCTOU on `in_use` Check — OPEN

**File:** `server/app/services/session_manager.py`, `close_session()`
**Severity:** Low
**Status:** ✅ Resolved — current code re-checks `in_use` after `pop()` and restores + raises `SessionBusyError` ("became busy between in_use check and pop"), as in the fix below.

#### Original root cause

```python
with self._lock:
    session = self._lru.get(sid)
    ...
    if session.in_use:
        raise SessionBusyError(...)
    self._lru.pop(sid, None)        # no re-check after pop
    if session.leased:
        session.release_lease()
```

`in_use` reads `_active_requests` under `_active_requests_lock`, not under `_lock`, so a concurrent handler could call `_acquire_request()` between the check and the `pop()`. The window is microseconds and unlikely in a single-server deployment, but it is a logical correctness issue.

#### Fix

Re-check `in_use` after `pop()`; if a request slipped through, restore the session and raise:

```python
with self._lock:
    session = self._lru.get(sid)
    if session is None:
        raise SessionNotFound(f"Session {sid} not found")
    if session.status == SessionStatus.CLOSED:
        raise SessionNotFound(f"Session {sid} is closed")
    if require_lease:
        session.require_lease(lease_id)
    if session.in_use:
        raise SessionBusyError(f"Session {sid} is busy and cannot be closed")
    self._lru.pop(sid, None)
    if session.in_use:                       # re-check after removing from LRU
        self._lru[sid] = session             # restore
        self._lru.move_to_end(sid)
        raise SessionBusyError(
            f"Session {sid} became busy between in_use check and pop"
        )
    if session.leased:
        session.release_lease()
```

Once `pop()` succeeds, no new `get_session()` can return this session, so no new `_acquire_request()` can start; only a request already past `get_session()` but not yet at `_acquire_request()` remains a (tiny) residual risk.

---

### Bug 5: Isabelle Processes Persist After Close — RESOLVED (not reproducible)

**Files:** `repl/src/main/scala/repl/session_manager.scala` (`shutdown`, `remove_session_async`), `repl/src/main/scala/repl/server_utils.scala` (`stop_server`, `stop_session`)
**Severity:** High (operational — causes OOM over time)
**Status:** ✅ Resolved — re-checked 2026-06-04 and **not reproducible** in current code; the accumulation-to-OOM symptom is explained by two *other* issues fixed since (gateway orphan on shutdown → `killpg`, `claude-work/fix-shutdown/`; gateway OOM under concurrency → Bug 6). See `claude-work/bug5-session-close-leak/NOTES.md`.

#### Verification (2026-06-04, container `isabelle-gym`)

Each `ReplBackend` owns its own Scala `Session_Manager` + Isabelle server, so a close
(`session.close() → ReplBackend.exit() → session_manager_instance.shutdown() → stop_server`)
tears down that backend's whole Isabelle server and its `poly` process. Measured:

- **Normal create → work → close:** poly 2 → 4 (after create) → **2 within ~2 s** of `DELETE`, stable.
- **Close WHILE sledgehammer runs (lead #3):** (poly, provers) = (2,0) baseline → (4,3) mid-run → **(2,0) within 1 s** of `DELETE`, stable for 20 s. The forked sledgehammer thread and its ATP subprocesses (`eprover`/`z3`/`cvc5`/`vampire`) are reaped together with the session.

**Residual (separate hardening, not the OOM symptom):** a session whose ML process is
*genuinely wedged* (a non-terminating tactic that ignores interrupts) may not respond to
`Server.exit`, and there is no OS-level force-kill fallback (`kill -9` on the tracked
server PID). Tracked as an optional follow-up below.

#### Original leads (investigated; symptom no longer present)

1. **Async removals may outrace server stop.** `Session_Manager.shutdown()` forks `remove_session_async` per running session, then joins `pending_removals`, then calls `Server_Utils.stop_server`. Each backend has its **own** `Session_Manager` and therefore its **own** Isabelle server process (`Server.init` in `start_server`). Confirm every `remove_session_async` future is actually in `pending_removals` before the join (it is added synchronously today) and that `stop_session` (`Server_Commands.Session_Stop`) returns a success `return_code` rather than erroring out silently.

2. **`Server.exit(name)` may not kill the OS process.** `stop_server` calls `Server.exit(server_info.name)`, which asks the Isabelle server to shut down over its socket. If the server process (or a child `poly`/ML process) is mid-computation or wedged, it may not terminate. Verify with `ps` inside the container before/after a close that the specific server PID actually exits.

3. **A forked sledgehammer thread can hold the ML process open.** The standalone sledgehammer channel forks an `Isabelle_Thread` (see `claude-work/impl-sledgehammer/`). If a session is closed while that thread is still running a prover, the ML process may refuse to exit until the thread (or its external ATP subprocesses) finishes. Reproduce by closing a session immediately after firing sledgehammer and checking for leftover prover/`poly` processes.

4. **No OS-level reaping.** There is no fallback that force-kills a server PID if graceful `Server.exit` fails. Consider tracking the spawned server PID and `kill`-ing it (and orphaned ATP children) as a last resort during `shutdown()`.

#### Optional follow-up (hardening only)

The poly-probe check above (`claude-work/bug5-session-close-leak/`) confirmed normal and
sledgehammer-interrupt closes reap cleanly. The only remaining gap is a last-resort
**force-kill fallback** in `stop_server`/`shutdown` that `kill -9`s the tracked Isabelle
server PID (and orphaned ATP children) if graceful `Server.exit` does not return within a
timeout — to cover a genuinely wedged ML process. Low priority; not the OOM symptom.

---

### Bug 6: Gateway OOM Under Concurrent Sledgehammer — RESOLVED

**Files:** `server/app/core/config.py`, `server/app/services/session_manager.py`, `server/app/api/v1/router.py`
**Severity:** High (operational — bricked the whole server)
**Status:** ✅ Resolved — fixed and verified 2026-06-04 (see `claude-work/impl-sledgehammer/SCALING_NOTES.md`).

#### Root cause

Found by the scale harness `claude-work/impl-sledgehammer/test_sledgehammer_scaling.py`. At ~16 concurrent `sledgehammer` calls, the single shared Py4J **gateway JVM** was OOM-killed by the kernel:

```
scala: line 68: 15680 Killed   ".../java" -Xmx4g ... repl_backend_gateway.scala
py4j.java_gateway: An error occurred while trying to connect to the Java server (127.0.0.1:39143)
```

Each `sledgehammer` is itself a multi-prover parallel job (balloons its `poly` heap + forks several ATP processes). A burst of them spikes memory; the OOM killer takes the gateway JVM; and because there was **no gateway recovery**, every subsequent request returned HTTP 500 — the server stayed bricked until manual restart. The Python cgroup admission gate (Bug-fix from 2026-06-03, see Work Log) did not prevent this: it gates memory at session-*create* time, but the spike is from *running* a heavy op on already-admitted idle sessions.

#### Fix (two parts)

1. **Concurrency semaphore** — bound in-flight sledgehammers server-wide.
   - `config.py`: `Server.MAX_CONCURRENT_SLEDGEHAMMER` (env `ISABELLE_MAX_CONCURRENT_SLEDGEHAMMER`, default `~cores/8`, i.e. 4 on a 32-core box — tracks the empirical throughput knee).
   - `session_manager.py`: `self.sledgehammer_sem = asyncio.Semaphore(...)`.
   - `router.py`: the sledgehammer endpoint runs under `async with session_manager.sledgehammer_sem:`; excess requests queue (backpressure) instead of oversubscribing.
2. **Gateway health-check + auto-restart** — `session_manager.py`:
   - `gateway_alive()` (via `ReplBackendGatewayProcess.has_terminated()`).
   - `_ensure_gateway()` now detects a dead gateway and calls `_recover_gateway_locked()` — purges the now-invalid sessions and rebuilds the gateway — so the next request recovers instead of 500-ing.
   - the background cleanup loop also recovers a dead gateway proactively.
   - `GET /` reports `gateway_alive` (status `degraded` when false) and `max_concurrent_sledgehammer`.

#### Verification (2026-06-04, container `isabelle-gym`, 32 cores)

- **Semaphore:** re-running the harness at W=16 (which previously OOM-killed the gateway) now completes **32/32** with memory flat at ~3.2 GB; no 500s, isolation still PASS.
- **Auto-restart:** `claude-work/impl-sledgehammer/test_gateway_recovery.sh` SIGKILLs the gateway process group → `GET /` shows `status=degraded, gateway_alive=false` → `POST /sessions` returns **200** (auto-recovered) → `GET /` shows `status=healthy, gateway_alive=true`. Previously this was 500-forever.

### Bug 7: Stale `isabelle_user_data` Volume Shadows Component Registration After Image Rebuild — RESOLVED

**Files:** `Dockerfile`, `docker-compose.yml`, `repl/Admin/init`, `repl/Admin/container_init.sh`
**Severity:** Medium (operational — server cannot start after an image rebuild when the old volume exists)
**Status:** ✅ Fixed permanently 2026-08-08 — the container entrypoint `repl/Admin/container_init.sh` re-runs the idempotent `./repl/Admin/init` on every container start (wired via `command:` in `docker-compose.yml`), so a stale volume can no longer shadow build-time registration. (Previously worked around 2026-06-10 by manually re-running `./repl/Admin/init` inside the container.)

#### Symptom

After rebuilding the image (`docker compose build isabelle-gym`) and starting a fresh container, `python -m server.app.main` dies during startup with:

```
-- [E006] Not Found Error: .../repl_backend_gateway.scala:4:7
4 |import py4j.GatewayServer
  |       Not found: py4j
...
ValueError: invalid literal for int() with base 10: ''   # gateway printed no port
server.app.errors.GatewayUnavailable: ... failed to start REPL gateway
```

#### Root cause

The Dockerfile runs `./repl/Admin/init` at build time, which registers the `repl` component (and downloads `py4j`/`spliff` contribs) under `/root/.isabelle`. But docker-compose mounts the named volume `isabelle_user_data` over `/root/.isabelle`, and Docker only copies image content into a named volume **when the volume is empty**. Any pre-existing volume (from a previous image) therefore shadows the build-time registration: the new container sees the *old* volume's state, `isabelle scala` finds no `py4j`/`repl.jar` on its classpath, the gateway script fails to compile, prints no port, and the server exits.

This bites every time the image is rebuilt while the old volume survives — exactly the standard upgrade path.

#### Workaround (verified 2026-06-10)

```bash
docker compose exec isabelle-gym ./repl/Admin/init   # re-registers into the live volume
# then start the server as usual
```

#### Proposed permanent fix

Run a cheap idempotent check at container start (entrypoint or server startup): if `$ISABELLE_HOME_USER/etc/components` does not list `/app/repl`, run `repl/Admin/init` before launching the gateway. Alternatively, drop the named volume from compose (heaps would rebuild per fresh container) or version the volume name with the image.

Related hardening (2026-07-15): `repl/Admin/init` downloads the `py4j`/`spliff` component
tarballs from the Isabelle component servers, and the official site is sometimes unstable —
a rebuild on a fresh volume can fail transiently. Consider vendoring the two tarballs into the
image (or a local component repository) so builds never depend on the network; until then, the
recovery is simply re-running `docker compose exec isabelle-gym ./repl/Admin/init` once the
site is reachable (the volume caches the download afterward).

---

### Bug 8: `close()` Rejects Its Own `exit` Job — Sessions Never Torn Down — RESOLVED

**File:** `server/app/services/threaded_backend.py`
**Severity:** Critical (P0 — every session close leaked a `poly` process + in-JVM Isabelle server)
**Introduced:** commit `e6c3869` (2026-07-13, "reject new job and emit running job for ThreadedBackend")
**Status:** ✅ Resolved 2026-07-15 (`_submit_unchecked` bypass + regression tests)

#### Root cause

`e6c3869` added a `_shutting_down` guard to `submit()` so no stale jobs reach a disconnecting
Py4J gateway. But `close()` sets the flag **first** and then calls
`self.submit(self._backend.exit)` — the guard rejected the backend's own exit job (the returned
future was pre-failed with `RuntimeError("Backend … is shutting down")`, caught and only
logged). `ReplBackend.exit()` therefore never reached the JVM: no `Session_Stop`, no
`stop_server` — the session's `poly` ML process and its per-backend in-JVM Isabelle server
leaked on **every** close. Under the MCP-comparison per-problem create/close cycling, container
memory climbed monotonically until the admission gate refused new sessions (503
`PoolExhausted: memory pressure too high`). Full analysis:
`claude-work/2026-7-15(1)-research-session-memory-release/FINDINGS.md`.

#### Fix

`close()` now delivers the exit job through a private `_submit_unchecked()` that bypasses the
shutdown guard (external `submit()` calls are still rejected, and the queue is still drained
first). Regression tests in `tests/test_threaded_backend.py` — verified to FAIL on the pre-fix
code (2 of 4 tests) and pass on the fixed code:

- `test_close_delivers_exit_to_backend` — the e6c3869 regression guard
- `test_submit_rejected_after_close`
- `test_pending_jobs_cancelled_but_exit_still_runs`
- `test_worker_thread_stops_after_close`

#### Residual (pre-existing, separate)

Even when delivered, `exit()` has historically blocked >60 s (`TimeoutError` at
`EXIT_TIMEOUT` in `logs/server.log`, 2026-06-10) — the Bug 5 "wedged ML process / no OS-level
force-kill fallback" hardening item still applies and is tracked in the Phase-3 plan
(`claude-work/2026-7-15(2)-research-server-code-audit/FINDINGS.md`).

### Bug 9: Gateway JVM `Event_Timer` Cancelled — Server Wedges, Recovery Blind — RESOLVED

**Files:** gateway JVM lifecycle (`repl/src/python/repl_backend_gateway.py`,
`server/app/services/session_manager*.py`)
**Severity:** High (once hit, every session create/reset 500s until container restart)
**Found:** 2026-08-15, during the `mcp_lsp_server` smoke (Stage 4 work-log entry)
**Status:** ✅ **Resolved 2026-09-08, two independent layers.**

1. **Root cause fixed upstream (Isabelle2026-RC0).** In 2025-2, `Event_Timer.request`
   schedules a bare `TimerTask`; one throwing task kills the JVM-global
   `java.util.Timer` thread, and every later `schedule()` throws
   `IllegalStateException("Timer already cancelled")`. Commit `88acf2619921`
   (isabelle-release, 2026-05-17) wraps every task in try/catch, so the wedge
   class is impossible. Verified on RC0 (`isabellegym-isabelle-gym:2026rc0`,
   port 8001): 12/12 churn rounds + concurrent HOL-Analysis build with
   `gateway_alive=true`, zero `Timer already cancelled` in the server log;
   load degrades to graceful memory-gate 503s instead of 500-forever.
2. **Detection + recovery hardened server-side** (works on 2025-2 too):
   `ReplBackendGateway.alive()` (Scala) schedules a no-op on `Event_Timer` and
   returns false on `IllegalStateException`; `ReplBackendGatewayProcess.is_alive()`
   (Python, 5 s-bounded) probes it; `SessionManager._ensure_gateway` recovers on
   *any* failed probe (dead **or** wedged), and `gateway_alive()` is a functional
   probe with a 5 s cache. Tests: `tests/test_gateway_wedge.py`.

#### Symptom

Mid-smoke, under scratch-session churn + a heap build (twice), every subsequent session
create/reset started failing with HTTP 500:

```
java.lang.IllegalStateException: Timer already cancelled
  at ... Headless$Session.use_theories ← Event_Timer.request
```

`Event_Timer` is JVM-global, so one cancellation wedges the whole gateway. The gateway
liveness check (`gateway_alive` = process liveness only) does NOT detect this state, so
gateway crash recovery never fires; only a container restart unwedges the server.

#### Status / next steps

Root cause not yet isolated. Simple close→create and reset→create cycles were ruled out
(both clean). Suspected: an eviction/teardown interleave cancelling the shared
`java.util.Timer`. Two work directions: (1) find the cancellation source (audit
`Event_Timer` users on the teardown path — session stop / server stop inside
`Server_Utils` / `Session_Manager`); (2) harden detection regardless — the gateway health
check should exercise a real round-trip (e.g. a cheap `use_theories` probe or a JVM-level
"timer alive" check) so this class of wedge triggers crash recovery instead of 500-forever.

### Bug 10: `GET /api/v1/sessions` Leaks `lease_id`s — Destroy Authorization Bypassable — RESOLVED

**Files:** `server/app/services/session_manager_helpers.py` (`list_sessions`),
`server/app/api/v1/router.py`, `server/app/main.py`, `server/app/static/admin.html`,
`mcp_lsp_server/{app,pool,config}.py`
**Severity:** High (any client could destroy any tenant's session)
**Found:** 2026-09-09, handoff `isabellegym-lease-leak-issue.md` (agents in the
Putnam runs hand-rolled DELETEs after reading the listing)
**Status:** ✅ Resolved 2026-09-09.

#### Root cause

The `X-Lease-Id` token is the only ownership proof on mutation paths
(`DELETE`, release), but the pool listing returned `"lease_id"` for **every**
session with no lease required. Three curls — list, steal, delete — killed any
session (JVM + poly torn down), bypassing the lease check entirely. The
absence of a sanctioned destroy path (release frees nothing) is what pushed
agents to improvise it.

#### Fix (four parts)

1. `list_sessions` never includes `lease_id`; the full listing moved behind
   `GET /api/v1/admin/sessions`, gated by `X-Admin-Token` against
   `ISABELLE_ADMIN_TOKEN` (empty = disabled). The admin console gets the token
   injected at serve time; without it, force-close buttons render `locked`.
2. Audit: every DELETE logs a `warning` (session id, lease prefix, request id)
   and increments `isabellegym_sessions_force_closed_total` (alertable).
3. Sanctioned destroy in the LSP MCP: `isabelle_close(file_path, destroy?)` +
   `ISABELLE_MCP_LSP_CLOSE_DESTROYS` — teardown via the binding's own lease,
   rebind-on-404 recovers afterwards (tested: recovery happens in
   `pool.call`'s 404 wrapper; `sync` short-circuits on unchanged text).
4. Tests: `tests/test_lease_security.py` (listing split, admin gate, destroy
   paths, rebind-after-destroy). Rejected as non-solutions, documented:
   task-group scoping without credentials (theater), rotating leases (breaks
   long-lived flows).

#### Verification

Live on the RC0 track: public listing clean with a live session; admin
endpoint 403/403/200 (no/wrong/correct token); bogus-lease DELETE → 403;
owner-lease DELETE → 200 and the session is gone; 8/8 in-container tests.

---

### Bug 11: `ISABELLE_SCALA_JAVA_OPTIONS` Is Dead Config — JVM Options Never Reach the Gateway — RESOLVED

**Files:** `.env` / `.env.example` (false claim), `repl/src/python/repl_backend_gateway.py`,
`repl/Admin/container_entrypoint.sh`
**Severity:** Medium (silent: a documented memory mitigation never took effect; GC behavior
invisible during the 2026-09-10 incident)
**Found:** 2026-09-13, while deploying JVM GC logging (issue 5 rec 1)
**Status:** ✅ Resolved 2026-09-13 (GC logging now wired via the user settings file).

#### Root cause

`.env` carries `ISABELLE_SCALA_JAVA_OPTIONS="-Dpolybank.heap.percent=20
-XX:MaxHeapFreeRatio=30 ..."` with a comment claiming it lets the gateway JVM
shrink its heap. **Nothing consumes this variable** — not the `isabelle scala`
toolchain, not the Scala REPL sources. The gateway JVM always ran with the
scala-launcher defaults (`-Xmx4g`, ZGC), so the heap-shrinking mitigation
never existed. Two other injection channels were verified dead at the same
time: `JAVA_TOOL_OPTIONS` is filtered out by Isabelle's environment handling,
and `ISABELLE_TOOL_JAVA_OPTIONS` set in the process env is **clobbered** by
the settings evaluation (same trap as `ML_OPTIONS`, cf. the heap-cap work).

**The only reliable channel for JVM/ML options is the user settings file**
(`$ISABELLE_HOME_USER/etc/settings`, evaluated last): `ML_OPTIONS` for poly
processes, `ISABELLE_TOOL_JAVA_OPTIONS` for Isabelle-launched JVMs.

#### Consequence discovered at the same time

The gateway JVM runs **ZGC with `-Xmx4g`** (launcher defaults). With two
heavy Analysis-scale sessions in one 4 GB heap, ZGC allocation stalls are the
prime suspect for the 2026-09-10 64-second accept freeze and the 95-minute
4–5× slowdown window (issue 5). Next occurrence will be directly visible in
the GC log instead of guesswork.

#### Fix

- `container_entrypoint.sh` appends
  `ISABELLE_TOOL_JAVA_OPTIONS="$ISABELLE_TOOL_JAVA_OPTIONS -Xlog:gc*:file=/app/logs/isabelle-jvm-gc-%p.log:...:filecount=3,filesize=10M"`
  to the user settings (idempotent), so every Isabelle-launched JVM writes a
  rotated, per-PID GC log. Verified live: `isabelle-jvm-gc-<pid>.log` written
  from gateway start.
- JVM stdout/stderr are also durably captured (`logs/gateway-jvm.log`) —
  previously the stdout pipe was closed after the port line and stderr went
  to the ephemeral server stdout, which is why 06:21 was undiagnosable.
- `.env.example` corrected: the `ISABELLE_SCALA_JAVA_OPTIONS` block is
  labelled known-dead instead of claiming it shrinks the gateway heap.

---

### Bug 12: Unauthenticated ML Execution Through Every Text Endpoint — RESOLVED

**Files:** `server/app/core/input_guards.py` (new), `server/app/api/v1/schemas/API_models.py`,
`server/app/services/heap_pool.py`, `server/app/core/config.py`
**Severity:** Critical (remote code execution in the container by any client)
**Found:** 2026-09-21 code audit (`claude-work/2026-9-21-research-code-audit/FINDINGS.md` SEC-2)
**Status:** ✅ Resolved 2026-09-22.

#### Root cause

`diagnostic_guard.py` protected only `POST /diagnostic`. `POST /sessions/{id}/commands`,
`.../verify_chunk`, `PUT .../document` and the **lease-free** `POST /api/v1/sessions/bigstep`
accepted arbitrary Isar including `ML ‹OS.Process.system "…"›`; bigstep's only filter was a
regex stripping `ML_val` lines (a build-compat hack). The heap pool ran `isabelle build -d
<caller-chosen dir>`, executing whatever theories were there. The MCP tools
`isabelle_run_code` / `isabelle_sync` / `isabelle_multi_attempt` reach the same endpoints.
Additionally `_build_theory_header` quoted import names without escaping, so an import
name containing `"` could break out of the header and inject commands.

#### Fix

- `input_guards.reject_code_execution(text)`: reduce the text to its *command skeleton*
  (nested comments removed, string/cartouche/alt-string bodies blanked) and scan for the
  denylist shared with `diagnostic_guard._DANGEROUS_RE` at token boundaries. Wired as
  Pydantic validators on `CommandRequest.command`, `ChunkVerifyRequest.chunk`,
  `DocumentLoadRequest.text`, `BigStepTheoryRequest.theory` → HTTP 422 naming the keyword.
  `HeapPool._scan_for_code_execution` applies it recursively to project `.thy` files before
  `isabelle build`. Policy switch `ISABELLE_ALLOW_ML_COMMANDS` (default false).
- `validate_import_names` on `theories` / `imports` / `dependencies` fields (no quotes,
  whitespace or control characters).
- Tests: `tests/test_input_guards.py` (detection matrix, literal/comment false-positive
  matrix, every model → 422, policy switch, header injection).

Server-side probe `ML_val` inserts (`Backend_Probes`) never pass through the request models
and are unaffected. Legacy documents carrying a leaked probe `ML_val` (REPL-1 in the audit)
will now be rejected by bigstep instead of silently stripped — fix REPL-1 to stop the leak.

### Bug 13: Heap-Pool Path Traversal and Unauthenticated Destructive Heap Endpoints — RESOLVED

**Files:** `server/app/services/heap_pool.py`, `server/app/api/v1/router.py`,
`server/app/api/v1/schemas/API_models.py`, `server/app/core/config.py`
**Severity:** Critical (arbitrary file write/delete as the container user)
**Found:** 2026-09-21 code audit (FINDINGS SEC-3)
**Status:** ✅ Resolved 2026-09-22.

#### Root cause

`task_group` from the request body was used unvalidated as a directory component for the
manifest write (`state_dir / task_group / …` + `mkdir(parents=True)`); `DELETE
/api/v1/heaps/images/{session}?platform=` globbed raw `platform`/`session` into
`shutil.rmtree`/`unlink`; `project` was any absolute path handed to `isabelle build -d`;
none of the "Admin:" heap deletes checked a token.

#### Fix

- `validate_safe_name` (`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`, fullmatch) on `task_group`,
  `session_name`, `heap_session`, image `session`/`platform` — request models and path
  parameters (422). `validate_project_path`: absolute and resolved under
  `ISABELLE_HEAP_ALLOWED_ROOTS` (default `/app:/root/.isabelle`; `HeapPool(allowed_roots=…)`
  for tests). `assert_within` containment on manifest paths and image paths before any
  write/delete; `HeapRejected` (422) error class.
- Owner decision: `DELETE heap`, `DELETE heap image`, `DELETE heap group` require
  `X-Admin-Token` (`router._require_admin_token`, mismatches logged); `POST /heaps/build`
  stays open (the LSP MCP's `isabelle_build_heap` keeps working) but is path-restricted.
- Tests: `tests/test_input_guards.py` (traversal payloads for every input, containment,
  admin gate 403/422, build stays open); `tests/test_heap_pool.py` adjusted to pass
  `allowed_roots=[tmp_path]` and an allowed project path.

#### Deferred (owner decision 2026-09-22)

FINDINGS SEC-1 — `GET /admin` inlines the admin token into an unauthenticated page — is
**not** fixed: a real admin login is future work; keep the port firewalled.

### [To-do]Issue 1: How Isabelle do parallel
When have parallel "have x" statements, can we do this in step. And how do we retrive information when one line is stucked in loop. That is, we need error retrieval for a proof chunk, the server should not just return a timeout error, it should tell, when we build the MCP server, the agent what part of that proof chunk just went wrong.


---

## Summary of Changes

| File | Change | Status |
|---|---|---|
| `server/app/services/threaded_backend.py` | Queue `exit()` + configurable `EXIT_TIMEOUT`/`JOIN_TIMEOUT` | ✅ Done (Bug 1, Bug 2) |
| `server/app/services/session_manager.py` | `cleanup_idle_sessions()`: add `max_lease_age` force-eviction path | ✅ Done (Bug 3) |
| `server/app/services/session_manager.py` | `close_session()`: re-check `in_use` after `pop()` | ✅ Done (Bug 4) |
| (none — verification only) | Isabelle server/session OS processes confirmed to terminate on close (incl. mid-sledgehammer) | ✅ Verified not-reproducible (Bug 5); optional force-kill fallback remains |
| `server/app/core/config.py`, `session_manager.py`, `router.py` | Sledgehammer concurrency semaphore + gateway auto-restart | ✅ Done (Bug 6) |

---

## Agent Work Log (dated)

Test/demonstration artifacts live under `claude-work/<task>/` (each with a `NOTES.md`).
**Note:** `claude-work/` is gitignored (local-only artifacts) and was pruned locally in
2026-09 — several pre-2026-09 entries below reference paths that no longer exist on
disk; the entries are kept as the historical record. Summary of work completed:

| Date | Task | What was done | Artifacts |
|---|---|---|---|
| pre-2026-06 (legacy) | **Early development — previously unlogged** | Foundational work that predates this log; one-liners from each folder's NOTES.md. `research-mcp-interaction`: MCP interaction design research (Lean/Isabelle SOTA → token-efficient design). `research-parallel-proof`: Isabelle parallel proof checking → parallel `have` chunks + granular error retrieval. `impl-verify-chunk`: `verify_chunk` — single-timeout, per-command status for a proof chunk. `transactional-verify-chunk`: auto-rollback on failure/timeout. `impl-parallel-sessions`: per-session parallelism + OOM testing. `impl-diagnostic-endpoint`: diagnostic-commands endpoint. `impl-mcp-server`: the (stepwise) MCP server over IsabelleGym. `modularize-server`: split server files to ~500-line modules. `compare-mcps`: early cross-MCP comparison planning (COMPARISON_PLAN.md). `rearb`: arbitration spike scratch (ROOT + numbertheory problem). Scratch files `2026-1223.pdf`, `build_devnote_section.py`, `repro_sledgehammer_500.py`, `trash-imo-rep6-9/` are unarchived scratch, not logged work. | `claude-work/` (folders above) |
| 2026-06-03 | **Shutdown fix** | Two bugs: (1) `SessionManager.shutdown()` let `asyncio.CancelledError` escape (it's a `BaseException`, not caught by `except Exception`) → uvicorn "Application shutdown failed" + teardown skipped; (2) `ReplBackendGatewayProcess.terminate()` signalled only the launcher shell, leaving the JVM orphaned (4 GB-heap leak → container OOM `Exited 137`). Fixed: catch `CancelledError`; `killpg` the gateway process group. | `claude-work/fix-shutdown/` |
| 2026-06-03 | **Memory-mgmt investigation** | Found the Scala memory management was dead three ways: never called by the server, wrong layer (per-backend, 1 session each), wrong metric (JVM heap, not the `poly` processes). | `claude-work/investigate-memory-management/` |
| 2026-06-03 | **Memory mgmt → Python** | Moved memory management into the Python `SessionManager`, measuring real container memory via cgroup v2 (`MemoryMonitor`). Under pressure: evict idle LRU sessions, then 503 (never kill busy/leased). Removed the dead Scala code (rebuilt `repl.jar`) and synced the Py4J layer. Also fixed `PoolExhausted` being mis-mapped to 500. | `claude-work/impl-python-memory-mgmt/` |
| 2026-06-04 | **Sledgehammer concurrency at scale** | Built an N-way isolation + throughput-scaling harness. Result: isolation PASS at N=6 (no crossed channels); throughput knee ≈ 4 on 32 cores; **surfaced Bug 6** (gateway OOM at W=16). | `claude-work/impl-sledgehammer/SCALING_NOTES.md`, `test_sledgehammer_scaling.py` |
| 2026-06-04 | **Bug 6 fix** | Sledgehammer concurrency semaphore + gateway health-check/auto-restart (see Bug 6 above). Verified: W=16 no longer OOMs; killed gateway auto-recovers on next request. | `claude-work/impl-sledgehammer/test_gateway_recovery.sh` |
| 2026-06-04 | **Bug 5 re-check** | Measured poly/prover process counts across create→close and close-during-sledgehammer. Both reap cleanly (≤2 s), so Bug 5 is not reproducible; marked resolved. Optional force-kill fallback for wedged ML processes noted. | `claude-work/bug5-session-close-leak/` |
| 2026-06-04 | **Phase 0 monitoring** | Added a Prometheus `/metrics` endpoint (HTTP histograms via instrumentator + `isabellegym_*` counters/gauges/histogram in `server/app/core/metrics.py`, fed by `get_lru_info()`/`MemoryMonitor`/gateway-recovery), `/healthz`+`/readyz` probes, and a Prometheus+Grafana+cAdvisor stack in `docker-compose.yml` with `mem_limit: 12g` and a starter dashboard. Verified end-to-end: all 3 Prometheus targets UP, Grafana dashboard provisioned, domain counters move, `memory_limit_mb` reflects the 12g cgroup. NB: image must be rebuilt (`docker compose build isabelle-gym`) to bake in the two new pip deps. | `claude-work/impl-monitoring/`, `monitoring/` |
| 2026-07-15 | **MCP-comparison harness fixes** | Fixed the harness bugs from `claude-work/2026-7-15(3)-research-mcp-comparison-audit/`. Headline: the DeepSeek "empty response" runs were `max_tokens: 4096` truncation of a reasoning model (output_tokens == 4096 exactly; hidden `reasoning_content` ate the budget), NOT a content filter — raised to 32768, `RoundResult` now carries `finish_reason`/`reasoning_text`, truncated rounds are counted and honestly labelled, and a no-tool-call round gets up to 2 nudges instead of silently ending the attempt. Also: removed the first-green-chunk early termination in the IsabelleGym runner (it cut attempts at the first proved *helper* lemma — the devnote "fooled arbiter" rows); unparsable tool JSON no longer poisons the message history; `call_tool` stops swallowing `BaseException`; `isabelle_launch` gets a derived session name instead of a theory; I/Q auth token no longer routed through the model; `analyze.py` fixed (crash on zero-solved, wrong default dir, per-experiment subfolders) + error-class table. Tests: `tests/test_mcp_comparison_fixes.py`; full suite 20 passed. Verified offline only (no live model runs, per request). | `claude-work/2026-7-15(6)-fix-mcp-comparison/`, `claude-work/2026-7-15(3)-research-mcp-comparison-audit/` |
| 2026-07-15 | **P1/P2 audit fixes (Phases 2–3)** | Fixed the P1/P2 findings from `claude-work/2026-7-15(2)-research-server-code-audit/`: A2 `_create_session` no longer closes a session while holding the manager lock; A3 base `SessionError` handler (500s now carry the real message, e.g. backend timeouts); A4 sledgehammer marks the session busy + refreshes activity; A5 client HTTP timeouts get grace beyond the server budget (`execute_command`/`diagnostic` +30 s, sledgehammer +120 s for semaphore queueing); B1 MCP `_begin_theory` closes the leased session on `enter_theory` failure; A6 memory gate subtracts `inactive_file` page cache, settles 2 s between evictions, and retries admission 3×2 s before 503; A7 a concurrently-closed session surfaces as 404 not 500; A8 empty `verify_chunk` chunks are 422 and the backend `error` (e.g. "theory not begun") is passed through; B2 MCP connection state keyed weakly by the session object (no id-recycling, auto-cleanup); B3 `close_theory` docstring says destroy, not release. Tests: `tests/test_phase2_phase3_fixes.py` (8) + existing (4), all green; in-container probes in `claude-work/2026-7-15(5)-fix-p1-p2-bugs/probe_fixes.py`. Also hardened the Dockerfile (Isabelle download layer before COPY, wget retries + Clarkson mirror fallback) after two silently-failed image builds traced to the unstable TUM dist server. | `claude-work/2026-7-15(5)-fix-p1-p2-bugs/`, `claude-work/2026-7-15(2)-research-server-code-audit/` |
| 2026-07-15 | **Bug 8 fix (session teardown regression)** | Research into "deleted session doesn't release memory" (MCP-comparison failures) found `ThreadedBackend.close()` rejecting its own `exit` job since `e6c3869` — no session was ever torn down on the JVM side. Fixed via `_submit_unchecked()` bypass; added `tests/test_threaded_backend.py` (verified red on pre-fix code). Also produced full audits of the server/MCP layers and the comparison harness (incl. the DeepSeek "empty response" root cause: `max_tokens=4096` reasoning-truncation, not a content filter). | `claude-work/2026-7-15(1)-research-session-memory-release/`, `claude-work/2026-7-15(2)-research-server-code-audit/`, `claude-work/2026-7-15(3)-research-mcp-comparison-audit/` |
| 2026-06-10 | **Image rebuild + Bug 7** | Rebuilt the image with trimmed deps (removed `torch` — sole source of the multi-GB `nvidia-*-cu12` CUDA wheels, only imported by archival `previous works/` code — and unused `expecttest`, from `requirement.txt` + `pyproject.toml`; image 24.9 GB → ~2 GB). First server start after the rebuild failed with `Not found: py4j` in the gateway — surfaced **Bug 7** (pre-existing `isabelle_user_data` volume shadows the build-time `repl/Admin/init` registration). Worked around by re-running `./repl/Admin/init` in the container. | — |
| 2026-08-08 | **Server prep for dual MCP support + legacy cleanup** | Prepared the server for the planned LSP-like read-only MCP (research: `claude-work/2026-8-8-research-lsp-readonly-mode/`). New server surface: `GET .../facts/local` + `GET .../facts/global?limit=` (transient read-only probes; the Scala/ML chain already existed but had zero callers), `PUT .../document` (whole-document replace: backend `reset()` + re-enter theory + one edit — the file-sync primitive), `GET .../last_report` (retained report of the most recent `verify_chunk`; cleared by `load_document`, not by rollback/restore), and an optional observability `label` on session create echoed in session info/listings. Client methods in `async_client.py` for all four. Legacy cleanup: **Bug 7 permanent fix** (`repl/Admin/container_init.sh` entrypoint re-runs idempotent `./repl/Admin/init` on every container start); fixed `REPL.ML` `pretty_local_facts` returning only the first fact (`List.hd`); removed the dead `run_diagnostic` Protocol alias (Scala side renamed to `probe_transient` in `e6c3869`); dropped never-populated bigstep `diagnostics`/`failure_location` fields from API + internal models; deleted dead `server/app/api/v1/ws.py` and `repl/src/python/session_manager.py`; removed stale `--cov=gym` pytest addopts and `gym*` packaging refs from `pyproject.toml`. Tests: `tests/test_readonly_mode_server_prep.py` (8 new, all green in-container; the two `render_chunk` + `test_mcp_comparison_fixes.py` failures are pre-existing missing `mcp`/`openai` packages in the image, unrelated). **Known limitation surfaced during smoke-testing:** channel probes (`facts/local`, `facts/global`, `open_subgoals`, sledgehammer, `diagnostic`) time out when the document tip is past a trailing `end` — a probe command appended after `end` never executes, so the ML channel never answers (pre-existing backend behavior, also affects `probe_transient`; file-synced clients should be aware). Follow-up same day: closed the file-sync diagnostics gap — `PUT .../document` now accepts `report: bool`; with `report=true` the text runs through a new backend method `step_chunk_report` (Scala, mirrors `verify_chunk`'s report but NEVER rolls back ordinary failures, LSP-style; still discards on budget timeout to cancel runaway commands) and stores the report in `last_chunk_report`, so `last_report`/`last_diagnostics` works uniformly for both MCP flavors. `proof_open` probing is skipped when the text ends with theory `end` (the limitation above). Tests: 4 more in `tests/test_readonly_mode_server_prep.py` (13 total, green). Endpoint↔MCP-tool mapping: `claude-work/2026-8-8-research-lsp-readonly-mode/RESEARCH.md` appendix. | `claude-work/2026-8-8-research-lsp-readonly-mode/` |
| 2026-08-12 | **Docs consolidation + Stage 1: Scala backend reorganization** | Housekeeping: moved `DESIGN_CHOICES.md`/`devnote.md`/`ISSUES.md` into new `docs/` (entry points README/AGENTS/CLAUDE stay at root; all live references updated). Stage 1 of the LSP-mode plan: split the 340-line Py4J facade `repl_backend.scala` into self-typed traits, one file per consuming workflow — `backend_lifecycle.scala`, `backend_probes.scala` (transient read-only probes, both MCPs), `backend_chunk_ops.scala` (chunk-centric MCP surface), `backend_file_ops.scala` (LSP-like file-sync surface) — `repl_backend.scala` is now a 61-line class with the shared probe plumbing. Zero behavior change (27 public methods moved verbatim, verified by grep + full smoke: verify_chunk / load_document(report) / probes / lifecycle all work through Py4J). Compiler-forced details: trait-visible members widened to `protected`; new files must be listed in `repl/etc/build.props` (Isabelle component build compiles an explicit source list). Also: file-header comments on all 13 Scala files + section markers in REPL.ML stating which workflow each serves; Scaladoc gaps filled; Protocol regrouped by concern and the missing `in_proof` declaration added. Next: Stage 2 (line+col diagnostics, goals_at_line, probe-before-end) per `claude-work/2026-8-8-research-lsp-readonly-mode/`. | `claude-work/2026-8-8-research-lsp-readonly-mode/` |
| 2026-08-12 | **Stage 2: Phase-2 read-only features (lean-lsp-mcp symmetry)** | Three backend upgrades, all smoke-verified live in the container. **2.1 line+col diagnostics**: `node_status_report` now emits per-command `range {start{line,col},end{line,col}}` (1-based, UTF-16 cols) via `node.command_iterator` + `Line.Document`; message-level offsets provably don't exist for DRAFT nodes (PIDE spans are position-less), so diagnostics carry their owning command's range — jEdit's granularity. Flows through `verify_chunk`/`last_report`/`load_document(report=true)`; additive `CommandRange`/`Position` schema models. **2.2 `goals_at_line` + `command_at_line`**: read-only jEdit-style line queries (`snapshot.current_command` + retained per-command STATE_MESSAGE results — no ML probes); `goals_before`/`goals_after` symmetry with lean_goal. Required flipping `ISABELLE_SHOW_STATES` default to true (config comment notes the trade-off; `.env` had it pinned false — flipped). **2.3 probes on finished theories**: root cause of the post-`end` probe hang (from the 2026-08-08 entry) confirmed empirically — past `end`, probe commands parse against the Pure bootstrap keyword table ("missing theory context"), the ML payload never runs. Fixed: `open_subgoals`/`in_proof` short-circuit to `[]`/`false` when the document ends with `end` (definitional); `sledgehammer`/`get_proof_state` fail fast instead of 2× channel timeout; `local_facts`/`global_facts`/`probe_transient` insert the probe BEFORE the `end` command via a new `Repl_Session.with_probe_before_end` bracket (mid-document `Text.Edit.insert`, bypassing Thy_Info's append-only bookkeeping, always removed in `finally`; document verified byte-identical after probing); `step_chunk_report` hardcodes `proof_open=false` after an `ok` `end`. Smoke: `thm foo`/`find_theorems` on a finished theory answer in ~0.2s (previously 2× timeout → 500); normal-path regression clean. Tests: 3 new (range mapping + goals parsing); 31 passed in-container (2 pre-existing `mcp`-package failures). | `claude-work/2026-8-8-research-lsp-readonly-mode/` |
| 2026-08-14 | **Design decisions + checkpoint verification** | Design work for the LSP-like mode: chose **static verified heaps in a task-group-scoped heap pool** for imports over live draft-node syncing (recorded as DESIGN_CHOICES.md §1.13; both options researched — `claude-work/2026-8-12(1/2)-*`, `claude-work/2026-8-14(1)-research-style4-feasibility/` — overlays validated but deferred). Task-group tenancy + heap manifests + `/admin` page folded into the plan (`claude-work/2026-8-8-research-lsp-readonly-mode/IMPORT_SYNC_PLAN.md`). Read Tom Milan's IsabelleGym 1.0 thesis for precedent (imports as freeze/reuse boundary; editor-style access explicitly rejected for agents — supports the static-import call). **Legacy checkpoint machinery verified live** (`save_state`/`restore_state`): basic restore, multi-checkpoint history-tree restores (out-of-order A→B), branching after restore, and restore-undoes-kept-`verify_chunk` interplay — all pass; snapshots therefore planned for the new MCP surface. Housekeeping: claude-work reorganized into date-prefixed task folders (convention recorded in AGENTS/CLAUDE); Scala canned JSON responses centralized into `json_reports.scala`; `use_theories` spike (`2026-8-12(1)`): file-backed nodes load fine but reject interactive probe edits and lack the wrapper ML env — entry documents stay string-fed. | `claude-work/2026-8-12(1)-research-use-theories-spike/`, `claude-work/2026-8-12(2)-research-heap-pool/`, `claude-work/2026-8-14(1)-research-style4-feasibility/` |
| 2026-08-15 | **Stage 3: heap-pool import system implemented** | Static verified imports per DESIGN_CHOICES.md §1.13 and `claude-work/2026-8-8-research-lsp-readonly-mode/IMPORT_SYNC_PLAN.md`. Scala: `Server_Utils.start_session` + `Session_Manager` thread `dirs` into `Session_Build.Args` (REPL sessions start on user heaps; heap sessions bypass the theories-only session cache; `load_document` resets preserve field+dirs). Python: new `services/heap_pool.py` — registry keyed by `(task_group, project)`, `isabelle build -b` orchestration (per-key lock + `ISABELLE_MAX_CONCURRENT_BUILDS` semaphore), persisted per-heap JSON manifests (ROOT text, per-file sha256/mtime, fingerprint, status) under `ISABELLE_HEAP_POOL_DIR` (in the `isabelle_user_data` volume; registry rebuilt at startup, interrupted builds come back failed). Endpoints: `POST /api/v1/heaps/build`, `GET /api/v1/heaps`, `GET/DELETE /api/v1/heaps/{group}/{project:path}` (full manifest), `GET/DELETE /api/v1/heap_groups...` (admin). Session tenancy: `task_group` (+`heap_session`/`project`) on create/acquire, 403 cross-group (namespace isolation, not security), staleness gate (source re-hash vs fingerprint → 422 with rebuild instructions), `dependency_key` gains group+fingerprint, wrapper states the qualified heap theories. Client methods for all of it. Tests: `tests/test_heap_pool.py` (10, fake-subprocess). Container smoke — ALL GREEN: build 2-theory heap → manifest with hashes → session proves with qualified import (`thm baz_lemma` unqualified works) → second session shares → beta group 403 → edit source → 422 stale → rebuild → new facts live; manifests survive server restart. Two bugs caught by the smoke: missing `HeapGroupInfo` router import (500 on /heap_groups), and rebuild without `session_name` re-derived the name from the project dir (renamed the heap) — now keeps the existing name, with a regression test. Follow-ups same day: **admin console + heap GC + metrics** — `GET /admin` serves a static heap-pool console (`server/app/static/admin.html`: groups, build/rebuild/delete, manifest viewer with per-file hashes and log tail); delete now GC's the on-disk heap image + build logs once no pool entry references the session name (`ISABELLE_HEAP_GC_IMAGES`, default true — caught that Poly/ML images are single FILES, not dirs); Prometheus gauges `isabellegym_heap_pool_heaps{task_group,status}` + `isabellegym_heap_build_seconds` via a per-scrape collector. 43 tests pass in-container (2 pre-existing mcp-package failures). | `claude-work/2026-8-8-research-lsp-readonly-mode/`, `claude-work/2026-8-12(2)-research-heap-pool/` |
| 2026-08-15 | **Hover / go-to-definition / position-explicit sledgehammer** | Implemented per `claude-work/2026-8-15-research-hover-definition/FINDINGS.md` (spike-validated). **Hover + definition** (`GET .../hover?line=&col=`, `GET .../definition?line=&col=`): pure snapshot + `Rendering` reads on the DRAFT node (no evaluation, no edits, no overlays) — `hover_at` returns the tooltip contents (entity kind + type, e.g. `constant "List.list.hd" :: nat list ⇒ nat`); `definition_at` resolves entity markup def positions — heap/source entities to file positions (`~~/` expanded, symbol ranges decoded to 1-based UTF-16 line/col against the target file's text), the entry document's own entities to in-node line ranges via `snapshot.find_command_position`. **Position-explicit sledgehammer** (`POST .../sledgehammer_at` {line, subgoal?, timeout_s?}): first production use of the overlay machinery — new `Document_Utils.overlay_query` (attach print fn to an existing command with a full-perspective edit, poll for the instance's `finished` status marker, collect instance-tagged results, always remove) driving the new `isabellegym_sledgehammer` query op registered in REPL.ML (args [timeout_s, subgoal_i]). Key fix found by the pre-implementation spike: post-initial-method proof states are Forward mode, which `run_sledgehammer` rejects (`Proof.assert_backward`) — the op calls `Proof.enter_backward` first (read-only, safe), so any in-proof line works and subgoal selection (i=1/2 → distinct suggestions) holds. Shares the server-wide sledgehammer semaphore. Schemas (`HoverResponse`/`DefinitionResponse`/`DefinitionTarget`/`SledgehammerAt*`), Session wrappers (junk-tolerant json pass-through), client methods, Protocol synced. Tests: 7 new in test_readonly_mode_server_prep.py; 48 passed in-container (2 pre-existing mcp-package failures). Smoke: hover/def/sledgehammer_at pairs below the fold; tip sledgehammer regression clean. **Demo validation surfaced two more items** (see demo.ipynb new section): (1) KNOWN WART — the legacy tip `sledgehammer` (channel probe) fails with "Illegal application of proof command in state mode" when the tip state follows an initial-method `proof (...)` command (Forward-mode state); the overlay op's `Proof.enter_backward` normalization is the known fix if we want parity there — not applied yet (behavior change to a production path; pending decision). (2) Cosmetic: overlay sledgehammer result text concatenates writeln chunks without newlines (content complete; formatting polish if the MCP wants prettier lines). | `claude-work/2026-8-15-research-hover-definition/` |
| 2026-08-15 | **Stage 4: LSP-like MCP server (`mcp_lsp_server/`)** | New package implementing the file-sync MCP per `claude-work/2026-8-15-research-lsp-mcp-design/DESIGN.md` + user amendments (lean-lsp-mcp tool naming, warm scratch pool, 1-based UTF-16 positions, positioned sledgehammer). 23 tools: lifecycle (`isabelle_open`/`close`/`sync`), read-only file tools (diagnostic_messages with severity filter, goal, command_at_line, proof_state, source, query, local/global_facts, hover_info, definition, sledgehammer (tip or line+subgoal), checkpoint/restore/rollback/history/last_report), scratch execution (`isabelle_multi_attempt` — file prefix + candidates fanned out bounded-parallel on warm scratch sessions; `isabelle_run_code`), heap tools (`isabelle_build_heap`, `isabelle_heap_status`). `pool.py`: bindings keyed by canonical file path, disk re-read + `load_document(report=true)` on change before every call (copied-buffer; MCP never writes files), 404 → transparent rebind, scratch pool keyed by (task_group, heap, imports, field) with cap `ISABELLE_MCP_LSP_SCRATCH_POOL_SIZE`. Config `ISABELLE_MCP_LSP_*` (streamable-http on :8849). Tests: `tests/test_mcp_lsp_server.py` (10: sync-cache, rebind-on-404, scratch keying/reuse/drop, prefix truncation, header parsing). Container `pip install "mcp<2"` (1.29.0 — mcp 2.0 removed `mcp.server.fastmcp`; with it installed the 2 pre-existing render_chunk test failures also resolve: full suite **60 passed**). Live smoke via a real streamable-http MCP client (`claude-work/mcp_lsp_smoke_client.py`): all 17 steps green on a fresh JVM (open with deliberate errors → per-line/col diagnostics; goal at a proof line; hover/def on a HOL constant (file position into `/opt/isabelle/src/HOL/List.thy`) and on an own lemma (in-node range); sledgehammer subgoal=1/2 distinct suggestions; multi_attempt per-candidate verdicts; run_code; checkpoint/restore; disk-edit → next query reflects it; build_heap → open with heap_session proves with heap facts; close). **NEW BUG surfaced (unfixed, needs its own investigation)**: the gateway JVM's shared `Event_Timer` got cancelled mid-smoke (twice, under scratch/session churn + a heap build) — every subsequent session create/reset then fails 500 with `java.lang.IllegalStateException: Timer already cancelled` at `Headless$Session.use_theories ← Event_Timer.request`, and the gateway liveness check (`gateway_alive` = process liveness) doesn't see it, so crash recovery never fires; only a container restart unwedges. Ruled out as triggers: simple close→create and reset→create cycles (both clean). Likely an eviction/teardown interleave cancelling the JVM-global `java.util.Timer`. | `claude-work/2026-8-15-research-lsp-mcp-design/DESIGN.md` |
| 2026-08-15 | **Eval Level 0: LSP-MCP runner in the comparison harness** | New `MCP-comparison/run_isabellegym_lsp.py` driving `mcp_lsp_server` through the same OpenAI-compatible agent loop + neutral arbiter as the other runners (`claude-work/2026-8-15-research-mcp-evaluation/EVAL.md` Level 0). Shape difference: the LSP MCP is read-only by design, so the runner adds LOCAL sandboxed `read_file`/`write_file` tools over a per-attempt workdir copy of the problem (`_safe_workdir_path` rejects escapes); setup is `isabelle_open(file_path)` + best-effort warmup (positioned sledgehammer on the sorry-holed theorem's statement line — a prove-mode state even with the sorry below); the DONE gate uses `isabelle_proof_state`/`isabelle_source`; verdict = arbiter on the final workdir file. System key `isabellegym_lsp`; `config.yaml` gains the stdio spawn entry, `analyze.py` the system + its sledgehammer log pattern. Runner keeps `mcp`/`openai` imports lazy so unit tests import without them. Tests: `tests/test_lsp_runner.py` (5: sandbox escape rejection, read/write roundtrip + validation, tool surface, and a mock-LLM smoke — scripted write_file(good proof)→DONE through the real stdio MCP + arbiter: `arbiter_solved=True`). Caveat surfaced: the mock smoke exposed that the server I had started via `bash -lc` lost the Docker ENV PATH (login shells reset it) so bigstep's bare `isabelle` spawn 500'd — server must be started with the container ENV PATH intact (`bash -c`); noted for the ops docs. Test env note: container now has `mcp` 1.29.0 + `openai` installed (mcp 2.0 dropped `mcp.server.fastmcp`); full suite: 84 passed, 4 failed — the 4 `test_phase2_phase3_fixes.py` failures are `ModuleNotFoundError: mcp_server` from the in-flight `mcp_server`→`mcp_stepwise_server` rename (host working tree), NOT this work; before the rename the suite was 83 passed 0 failed. | `claude-work/2026-8-15-research-mcp-evaluation/EVAL.md` |
| 2026-09-04 | **Heaps discoverability + LSP imports fix** | `GET /api/v1/heaps/available` (base images from distribution + user heaps dirs with origin/size/mtime), admin console base-images section with image delete, `isabelle_heap_status` exposes the same to agents. Fixed the LSP MCP acquiring sessions without the file's own imports (theories-at-acquire): sessions are now acquired with the header imports parsed from the file, so non-`Main` parents resolve without the ~130 s doomed attempt. Full heap tutorial written (isabelle-humanize `isabellegym-heaps-tutorial(temp).md`). | commits on `2026-RC0`; `tests/test_mcp_lsp_heaps.py` |
| 2026-09-05 | **Bug 9 fix + Isabelle 2026-RC0 track** | RC0 migration (isabelle-humanize `.m0/rc0-migration/`): REPL Scala backend ported to Isabelle2026-RC0 (`a9e1683`), RC0 Dockerfile (`af14618`). Bug 9 (Event_Timer wedge): root-caused to upstream, fixed in RC0 (`88acf2619921`); our hardening (`d865f31`): bounded 5 s `alive()` probe (Event_Timer request + Console_Logger), 5 s-cached functional `gateway_alive()`, wedge recovery in `_ensure_gateway`. 12/12 stress rounds green. | `tests/test_gateway_wedge.py`; `Dockerfile.rc0` |
| 2026-09-08 | **Admin console: session management + base heaps** | Force-close from the console (lease-aware), theories column with full hover text, base-heap listing + delete. Follow-up gap fixed same week: released/unleased sessions were uncloseable — DELETE now accepts `X-Admin-Token` for unleased sessions (leased still require the matching lease). | `server/app/static/admin.html`, router |
| 2026-09-09 | **Bug 10 (lease-leak) + theory-parsing endpoint** | Bug 10: public `GET /api/v1/sessions` no longer returns `lease_id`; new token-gated `GET /api/v1/admin/sessions`; DELETE audit logging + `isabellegym_sessions_force_closed_total`; `isabelle_close(destroy?)` MCP tool. Header parsing: canonical `parse_theory_header` (nested-comment stripping + header anchoring) backing MCP `header_imports` and `build_verify.extract_imports`; endpoint `POST /api/v1/parse_theory_header`. Verified live on the Putnam q4 problem (4362 commands, no comment pollution). | `e2f89ae`, `0ae5e04`; `tests/test_lease_security.py` (8), `tests/test_theory_parsing.py` (10) |
| 2026-09-10 | **ML heap cap (issue 4)** | Container-level per-process ML memory bound: `ML_OPTIONS="--minheap 500 --enablegcsharing --maxheap 9216"` (9 GB) in the Isabelle user settings — the only working injection point, since the polyml component's `etc/settings` clobbers env/`.env` values. Builds capped immediately, sessions after next gateway restart. Owner decision: stay 32-bit `x86_64_32-linux` (no heap rebuild). | volume `etc/settings`; tutorial §5 |
| 2026-09-11 | **Packaging: one-command setup + turnkey export image** | `setup.sh` (env bootstrap with generated admin token → build → up → healthz wait → `--verify`/`--build-heaps`), annotated `.env.example`, `container_entrypoint.sh` (component re-register + idempotent ML-cap write + foreground API), compose now auto-starts the server. Turnkey RC0 image for reproduction: clean rebuild via `build_rc0_image.sh` (scripted assembly — the declarative BuildKit path is blocked by the JAVA_HOME quirk), lean bake (heaps+etc only, contribs verified byte-identical), cold-tested with no volume (healthz, HOL-Library acquire, small-step proof, strict bigstep), exported as 9.8 GB tar.gz with recipient runbook. Two-track branch model established: `main` = 2025-2 + features, `2026-RC0` = main + compat delta; feature backports by cherry-pick. | `setup.sh`, `repl/Admin/container_entrypoint.sh`, `.env.example`, `build_rc0_image.sh`, `Isabelle2026-RC0_version_docker_image_instruction.md` |
| 2026-09-11 | **Repo restructure + main backports** | `ba61e6c` restructure (MCP-comparison → evaluation/, bench JSONs → evaluation/, PDF → previous works/, deletions) applied to main minus the EXPORT.md rename (main never carries RC0 image instructions); test sys.paths + doc references fixed on both branches. | `760d514`, `c17ff81` on main |
| 2026-09-13 | **Issue 5 log dig + resilience recs 1–3 (Bug 11)** | Full server-log dig of the 2026-09-10 incident rewrote the handoff narrative: the 06:21 "JVM death" was a ~64 s accept stall with NO gateway restart (the IndexError deque cluster was misattributed from the 03:39 incident); the real gateway death hung an acquire 57 min because Py4J reads are unbounded and the cleanup loop's probe never fired; the 95-min slowdown was two heavy sessions contending — recovery the instant the lease reaper closed one (08:00:18). Implemented: durable JVM logs (`logs/gateway-jvm.log` + per-PID rotated GC logs `logs/isabelle-jvm-gc-<pid>.log` via user-settings `ISABELLE_TOOL_JAVA_OPTIONS`), Py4J `read_timeout` (`ISABELLE_PY4J_READ_TIMEOUT`, 900 s), acquire wall timeout (`ISABELLE_TIMEOUT_SESSION_CREATE`, 600 s → 503), probe-exception logging. **Bug 11** discovered and documented: `ISABELLE_SCALA_JAVA_OPTIONS` is dead config (nothing consumes it); gateway JVM runs ZGC at `-Xmx4g` — allocation stall is the prime suspect for the slow window. | `tests/test_gateway_resilience.py` (6); ISSUES.md Bug 11 |
| 2026-09-15 | **B1: incremental PIDE document edits (Option B)** | New channel aside the step path: `Repl_Session.replace_document` (spliff diff → `Text.Edit` list → `session.update`; PIDE re-processes only from the first changed command), Py4J `sync_document`, `load_document` fast path (same-theory `report=True` loads; header/theory-name change → reset fallback; checkpoints invalidated both sides; absolute line semantics). Key audit insight: append-only was a repo-side invariant, not PIDE — checkpoint restore already pushed mid-document spliff diffs through the same funnel. Benchmark on the 1059-line IMO file: **full load 137.6 s → tip edit 0.1 s (~1000×)**, mid-file edit 32.3 s, no-op 0.1 s, all success=True. Backported to main (`c86f79a`), merged both ways (`f32c6e7`); clean image rebuilt from the committed branch and the container recreated with entrypoint auto-start + `--restart unless-stopped` (Docker Desktop restarts no longer take the gym down). | `735e171` (RC0), `c86f79a` (main); `tests/test_document_sync.py` (12) |

| 2026-09-21 | **Full code audit + roadmap** | Read-only audit of server/, repl/, both MCP servers, client, docs/config, the `isabelle-humanize` harness's documented pain points, and a web survey of the Sept-2026 Lean/Isabelle MCP frontier. Ranked findings (3 critical: `/admin` token disclosure, unauthenticated ML execution, heap path traversal; 10 high incl. REPL probe double-insert, unbounded `stable_node_snapshot`, event-loop-blocking cleanup, LSP scratch-slot leak/hang, checkpoint soundness, dead `mcp_server` rename, `mcp<2` pin), 20-row frontier gap matrix, humanize asks with status, P0/P1/P2 roadmap. | `claude-work/2026-9-21-research-code-audit/` (FINDINGS.md + appendices A–E) |
| 2026-09-22 | **Bug 12 + Bug 13 fixes (audit SEC-2/SEC-3)** | New `server/app/core/input_guards.py`: code-execution guard on every text endpoint + heap sources (`ISABELLE_ALLOW_ML_COMMANDS`), import-name validation (header injection), safe-name validation for heap identifiers, `project` under `ISABELLE_HEAP_ALLOWED_ROOTS`, containment checks on manifest/image paths, admin token on destructive heap endpoints (build stays open). SEC-1 (`/admin` token) deferred by owner. Host tests: 128 passed (7 FastAPI-dependent skipped on host; container run pending Docker). | `claude-work/2026-9-22-fix-ml-exec-and-heap-traversal/`, `tests/test_input_guards.py` |

| 2026-09-27 | **Refactor: router.py → route package** | The 1,067-line `server/app/api/v1/router.py` split into `routes/{health,sessions,execution,inspection,positional,automation,checkpoints,heaps}.py` + `deps.py` (`LeasedSession` dependency replaces 25 copies of the lease/get_session/logging preamble; `sledgehammer_slot` shared by both sledgehammer endpoints) + `serializers.py`; `router.py` is an 88-line aggregate + compat facade. Largest file now 267 lines. Behaviour-preserving: OpenAPI diff 39/39 paths identical (tags aside). New `tests/test_source_limits.py` gate (600 lines, ratcheting allow-list for session.py/heap_pool.py/async_client.py). In-container: 235 passed; 1 pre-existing unrelated failure (`test_gateway_resilience` caplog vs non-propagating logger). Discovered: container `/app` is a WSL staging copy, not the working tree — uncommitted code must be `docker cp`'d in. | `claude-work/2026-9-27-refactor-router-package/` |

| 2026-09-27 | **Remove `server_gym/`** | `server_gym/success_checker.py` (a live server dependency mislabelled "legacy") moved to `server/app/services/success_checker.py`; dead `server_gym/isabelle_gym.py` deleted; importers repointed (`session.py`, `session_bigstep.py`, `eval_smallstep_isabellegym.py`); `pyproject.toml` now packages `server*` instead of `server_gym*`. Server/client/MCP packages have no `local_gym` dependency (verified). Also: `.gitignore` now ignores `.vscode/` and `.clinerules/` unconditionally (the old exception rules kept re-admitting editor files). | `claude-work/2026-9-27-remove-server-gym/` |

| 2026-09-27 | **Consolidate benchmark runs** | 42 raw run files (22 MB) + 4 orphaned `mcp_bench_results*.json` + derived `analysis_exports/` replaced by `evaluation/results/benchmark_runs.json` (2.3 MB, 46 runs, 1,525 theory rows, per-step timing vectors kept, command text dropped) + `.csv` + README; generator `evaluation/scripts/consolidate_runs.py --check` (157 recorded values recomputed, drift 7.6e-8); `runs_analysis.ipynb` repointed at the summary. Inspection outcome: server families are still regenerable; local-gym and qIsabelle baselines are not (archived in the summary only); the MCP bench script no longer exists. | `claude-work/2026-9-27-consolidate-benchmark-runs/` |

| 2026-09-27 | **Repo restructure (steps 1–4)** | `archive/` (previous-works, the 2.0 in-process gym + its baseline scripts, install.sh); `deploy/` (Dockerfiles, setup.sh, RC0 scripts, monitoring; compose stays at root with `dockerfile: deploy/Dockerfile`); `demo/` → `examples/`; deleted stale `repl/python/`, the committed `server.log.1`, `repl/temp.txt`. Client independence: `client/pyproject.toml` (httpx only), LSP MCP header parsing now via `POST /parse_theory_header` instead of importing server code, `tests/test_dependency_rules.py` import-direction contract. `.gitignore` typo `ClAUDE.md` fixed and `AGENTS.md`/`CLAUDE.md` tracked again; `*.log.*` ignored. Root loose files 17 → 12. | `claude-work/2026-9-27-repo-restructure/` |

*Last updated: 2026-09-27.*
