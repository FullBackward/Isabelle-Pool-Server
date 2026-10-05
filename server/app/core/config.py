import os
from typing import Final
import re


class API:
    VERSION: Final = "0.1.0"


class Server:
    DEFAULT_POOL_SIZE: Final = int(os.getenv("ISABELLE_POOL_SIZE", "24"))
    ENABLE_MEMORY_MANAGEMENT: Final = os.getenv("ISABELLE_ENABLE_MEMORY_MANAGEMENT", "true").lower() in {
        "1", "true", "yes", "on",
    }
    ENABLE_CACHE: Final = os.getenv("ISABELLE_ENABLE_CACHE", "false").lower() in {
        "1", "true", "yes", "on",
    }
    INITIAL_SESSIONS: Final = int(os.getenv("ISABELLE_INITIAL_SESSIONS", "3"))
    MAX_CACHE_SIZE: Final = int(os.getenv("ISABELLE_MAX_CACHE_SIZE", "1"))
    # Default true since Stage 2.2: goals_at_line (GET .../goals) REQUIRES state
    # messages, which only exist with show_states on. Trade-off: one state message
    # per command, so small-step output grows; the chunk-centric MCP is
    # verify_chunk-based (JSON status reports, not state text) so impact is small.
    # Set ISABELLE_SHOW_STATES=false to opt out (goals_at_line then returns empty
    # goal lists).
    SHOW_STATES: Final = os.getenv("ISABELLE_SHOW_STATES", "true").lower() in {
        "1", "true", "yes", "on",
    }
    # Rendered-output normalisation: the PIDE layer decodes \<...> escapes into
    # Unicode codepoints (the jEdit display convention), so goal/state/query
    # output arrives as UTF symbols while document source stays as-written.
    # normalise_for_isabelle maps rendered output back to \<name> ASCII notation
    # using Isabelle's OWN symbol table ($ISABELLE_HOME/etc/symbols), so the
    # mapping always matches the installed Isabelle version. Display-only —
    # document text is never touched. Set false to receive raw Unicode.
    ASCII_OUTPUT: Final = os.getenv("ISABELLE_ASCII_OUTPUT", "true").lower() in {
        "1", "true", "yes", "on",
    }
    DEFAULT_FIELD: Final = os.getenv("ISABELLE_DEFAULT_FIELD", "HOL")
    # Optional admin credential (off when empty): required as X-Admin-Token on
    # /api/v1/admin/* endpoints (full session listing incl. lease ids, used by
    # the admin console's force-close). The first and only credential on an
    # otherwise unauthenticated server — opt-in per the lease-leak fix.
    ADMIN_TOKEN: Final = os.getenv("ISABELLE_ADMIN_TOKEN", "")
    # Code-execution policy for client-supplied Isar text (commands,
    # verify_chunk, PUT /document, bigstep, heap-pool project sources). Default
    # false: `ML`, `ML_file`, `setup`, `*_setup`, `oracle`, `*_file`, ... are
    # rejected with 422 by server.app.core.input_guards (the denylist is shared
    # with the /diagnostic guard). Set true only for deployments whose theories
    # genuinely need ML — any client can then run arbitrary code in the container.
    ALLOW_ML_COMMANDS: Final = os.getenv("ISABELLE_ALLOW_ML_COMMANDS", "false").lower() in {
        "1", "true", "yes", "on",
    }
    HOST: Final = os.getenv("ISABELLE_SERVER_HOST", "0.0.0.0")
    PORT: Final = int(os.getenv("ISABELLE_SERVER_PORT", "8000"))
    MAX_LEASE_AGE: Final = int(os.getenv("ISABELLE_MAX_LEASE_AGE", "7200")) # 2 hours
    # Bound concurrent sledgehammer (and other heavy ML) calls. sledgehammer is
    # itself a multi-prover parallel job; too many at once spikes memory and can
    # OOM-kill the shared gateway JVM. Defaults to ~half the cores (capped), which
    # tracks the empirical throughput knee.
    MAX_CONCURRENT_SLEDGEHAMMER: Final = int(
        os.getenv("ISABELLE_MAX_CONCURRENT_SLEDGEHAMMER", str(max(1, min(8, (os.cpu_count() or 4) // 8))))
    )


def _heap_env(name: str, default: str) -> str:
    """Heap-pool knob: `ISABELLE_HEAP_POOL_<name>`, falling back to the pre-rename
    `ISABELLE_HEAP_<name>` (renamed 2026-10-05 so nothing in our namespace shadows
    Isabelle's own ISABELLE_HEAPS family). Fallback is kept for one release."""
    new, old = f"ISABELLE_HEAP_POOL_{name}", f"ISABELLE_HEAP_{name}"
    if new in os.environ:
        return os.environ[new]
    if old in os.environ:
        import warnings
        warnings.warn(f"{old} is deprecated; use {new}", DeprecationWarning, stacklevel=2)
        return os.environ[old]
    return default


class Heap:
    # Heap pool (Stage 3): verified per-project heaps built with
    # `isabelle build -b`, shareable by every session of the owning task group.
    # State dir lives in the isabelle_user_data volume (/root/.isabelle) so
    # manifests survive container restarts.
    STATE_DIR: Final = os.getenv("ISABELLE_HEAP_POOL_DIR", "/root/.isabelle/heap_pool")
    MAX_CONCURRENT_BUILDS: Final = int(os.getenv("ISABELLE_MAX_CONCURRENT_BUILDS", "1"))
    BUILD_TIMEOUT_S: Final = float(_heap_env("BUILD_TIMEOUT_S", "3600"))
    DEFAULT_TASK_GROUP: Final = "default"
    # Remove the on-disk heap image + build logs when a heap record is deleted
    # and no other pool entry references the same session name.
    GC_IMAGES: Final = _heap_env("GC_IMAGES", "true").lower() == "true"
    # Directories under which heap `project` dirs may live (colon-separated).
    # `isabelle build -d <project>` executes whatever theories/ROOT are there,
    # so the set is operator-chosen; anything else is 422. Default: the repo
    # mount and the Isabelle user-data volume.
    ALLOWED_ROOTS: Final = [
        r for r in _heap_env("ALLOWED_ROOTS", "/app:/root/.isabelle").split(":") if r
    ]

class Repl:
    SUBGOALS_TIMEOUT_S: Final       = int(os.getenv("ISABELLE_REPL_SUBGOALS_TIMEOUT", "20"))
    LOCAL_FACTS_TIMEOUT_S: Final    = int(os.getenv("ISABELLE_REPL_LOCAL_FACTS_TIMEOUT", "20"))
    GLOBAL_FACTS_TIMEOUT_MIN: Final = int(os.getenv("ISABELLE_REPL_GLOBAL_FACTS_TIMEOUT_MINUTES", "5"))

    GATEWAY_POLL_INTERVAL: Final    = float(os.getenv("ISABELLE_REPL_GATEWAY_POLL_INTERVAL", "0.1"))
    GATEWAY_POLL_TIMEOUT: Final     = float(os.getenv("ISABELLE_REPL_GATEWAY_POLL_TIMEOUT", "20.0"))
    GATEWAY_TERMINATE_WAIT: Final   = float(os.getenv("ISABELLE_REPL_GATEWAY_TERMINATE_WAIT", "3.0"))

    BACKEND_EXIT_TIMEOUT: Final     = float(os.getenv("ISABELLE_BACKEND_EXIT_TIMEOUT", "60.0"))
    BACKEND_JOIN_TIMEOUT: Final     = float(os.getenv("ISABELLE_BACKEND_JOIN_TIMEOUT", "5.0"))
    BACKEND_QUEUE_POLL: Final       = float(os.getenv("ISABELLE_BACKEND_QUEUE_POLL", "0.1"))

    # Socket-level read timeout for EVERY Py4J call. Caller-side
    # future.result(timeout=...) abandons a future but leaves the worker
    # thread blocked on the Py4J read — a wedged JVM (alive, accepts, never
    # answers) then pins that thread forever and every later job queues
    # behind it (the 2026-09-10 57-minute acquire hang). A generous bound
    # unblocks the thread with a proper exception. Must exceed the slowest
    # legitimate call (load_document under GC distress was ~500 s).
    PY4J_READ_TIMEOUT: Final        = float(os.getenv("ISABELLE_PY4J_READ_TIMEOUT", "900.0"))


class Memory:
    # Python/cgroup-based memory management (server-side). The pressure %, the
    # available-memory floor, and the host-memory fallback are read from the
    # container cgroup by server.app.services.memory_monitor.MemoryMonitor.
    PRESSURE_THRESHOLD: Final   = float(os.getenv("ISABELLE_MEMORY_PRESSURE_THRESHOLD", "85.0"))
    MIN_AVAILABLE_MB: Final     = int(os.getenv("ISABELLE_MEMORY_MIN_AVAILABLE_MB", "256"))
    FALLBACK_SYSTEM_MB: Final   = int(os.getenv("ISABELLE_MEMORY_FALLBACK_SYSTEM_MB", "4096"))
    SERVER_START_RETRIES: Final = int(os.getenv("ISABELLE_SERVER_START_RETRIES", "5"))
    # Session teardown -> cgroup accounting is not instantaneous (poly exit +
    # kernel reclaim lag). Pause after an eviction before re-reading the
    # snapshot, and retry admission briefly before 503ing a create.
    EVICTION_SETTLE_S: Final    = float(os.getenv("ISABELLE_MEMORY_EVICTION_SETTLE_S", "2.0"))
    ADMISSION_RETRIES: Final    = int(os.getenv("ISABELLE_MEMORY_ADMISSION_RETRIES", "3"))
    ADMISSION_RETRY_DELAY_S: Final = float(os.getenv("ISABELLE_MEMORY_ADMISSION_RETRY_DELAY_S", "2.0"))


class Timeouts:
    COMMAND_DEFAULT: Final      = float(os.getenv("ISABELLE_TIMEOUT_COMMAND", "30.0"))
    # Added ABOVE a JVM-enforced wall budget (step / diagnostic) for the Python-side
    # future timeout, so the backend's own timeout result always arrives first.
    BACKEND_GRACE_S: Final      = float(os.getenv("ISABELLE_TIMEOUT_BACKEND_GRACE", "10.0"))
    BIGSTEP_DEFAULT: Final      = float(os.getenv("ISABELLE_TIMEOUT_BIGSTEP", "300.0"))
    IDLE_DEFAULT: Final       = float(os.getenv("ISABELLE_TIMEOUT_STATUS", "300.0"))
    PROOF_STATE: Final          = float(os.getenv("ISABELLE_TIMEOUT_PROOF_STATE", "30.0"))
    CHECKPOINT_SAVE: Final      = float(os.getenv("ISABELLE_TIMEOUT_CHECKPOINT_SAVE", "30.0"))
    CHECKPOINT_RESTORE: Final   = float(os.getenv("ISABELLE_TIMEOUT_CHECKPOINT_RESTORE", "30.0"))
    CLEANUP_INTERVAL: Final     = int(os.getenv("ISABELLE_CLEANUP_INTERVAL", "60"))
    SESSION_IDLE_TIMEOUT: Final = int(os.getenv("ISABELLE_IDLE_TIMEOUT", "1800"))
    # Wall budget for creating a session's backend (acquire/create). Without
    # it a wedged gateway holds the request for as long as the Py4J read
    # blocks (2026-09-10: 57-minute hung acquire). Must stay below
    # Repl.PY4J_READ_TIMEOUT so the client gets the clean timeout error.
    SESSION_CREATE: Final       = float(os.getenv("ISABELLE_TIMEOUT_SESSION_CREATE", "600.0"))

class RegularExp:
    IMPORT_RE = re.compile(r'(?ms)\bimports\b(?P<imports>.*?)\bbegin\b')
    IMPORT_TOKEN_RE = re.compile(r'"[^"]+"|[A-Za-z_][A-Za-z0-9_./-]*')
    THEORY_RE = re.compile(r'(?ms)^[ \t]*theory\s+(?:"([^"\n]+)"|([A-Za-z0-9_\'.-]+))')
    THEORY_HEADER_RE = re.compile(r'(?ms)\btheory\b.+?\bimports\b.+?\bbegin\b')


class Logging:
    LOG_LEVEL: Final = os.getenv("ISABELLE_SERVER_LOG_LEVEL", "INFO").upper()
    LOG_DIR: Final = os.getenv("ISABELLE_SERVER_LOG_DIR", "logs")
    LOG_FILE: Final = os.getenv("ISABELLE_SERVER_LOG_FILE", "server.log")
    MAX_LOG_SIZE_BYTES: Final = int(
        os.getenv("ISABELLE_SERVER_MAX_LOG_SIZE_BYTES", str(10 * 1024 * 1024))
    )
    BACKUP_COUNT: Final = int(os.getenv("ISABELLE_SERVER_LOG_BACKUP_COUNT", "5"))
    ENABLE_FILE_LOGGING: Final = os.getenv("ISABELLE_SERVER_ENABLE_FILE_LOGGING", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    REQUEST_HEADER_NAME: Final = os.getenv("ISABELLE_SERVER_REQUEST_ID_HEADER", "X-Request-ID")
    COMMAND_PREVIEW_CHARS: Final = int(os.getenv("ISABELLE_SERVER_COMMAND_PREVIEW_CHARS", "120"))
    THEORY_PREVIEW_CHARS: Final = int(os.getenv("ISABELLE_SERVER_THEORY_PREVIEW_CHARS", "160"))
