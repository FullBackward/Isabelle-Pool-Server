"""The IPS server a run talks to: a fresh server started by memscale's
``IPSDriver`` (in its own systemd scope when available), or an external one
(``--ips-external``, e.g. the local container). Shared by the census and the
T1 cells so both start the server the same way."""

from __future__ import annotations

import os
import shlex
from typing import Any, Dict, List, Optional

from . import EXPERIMENT_DIR, REPO_ROOT
from .replay import BUDGET_MAX_S


def server_env(env: Dict[str, Any]) -> Dict[str, str]:
    """The server's environment from the config, with a relative
    ``ISABELLE_SERVER_LOG_DIR`` made absolute under the experiment directory
    (the server runs from the repository root, the runner from here)."""
    out = {k: str(v) for k, v in env.items()}
    log_dir = out.get("ISABELLE_SERVER_LOG_DIR")
    if log_dir and not os.path.isabs(log_dir):
        out["ISABELLE_SERVER_LOG_DIR"] = str(EXPERIMENT_DIR / log_dir)
    return out


def isabelle_cmd() -> Optional[List[str]]:
    """``ISABELLE=`` from the environment as a command prefix (review 77fc8fc S2)."""
    raw = os.environ.get("ISABELLE", "").strip()
    return shlex.split(raw) if raw else None


def make_driver(
    cfg: Dict[str, Any],
    heap: str,
    url: Optional[str] = None,
    external: bool = False,
) -> Any:
    """memscale's IPSDriver configured from ``cfg['ips']``."""
    from memscale.drive_ips import (  # pylint: disable=import-outside-toplevel
        IPSDriver,
    )

    ips = cfg["ips"]
    return IPSDriver(
        heap=heap,
        imports=[],
        url=url or ips["url"],
        external=bool(external or ips.get("external", False)),
        start_cmd=list(ips.get("start_cmd", ["python", "-m", "server.app.main"])),
        env=server_env(ips.get("env") or {}),
        ready_timeout_s=float(ips.get("ready_timeout_s", 300)),
        load_timeout_s=BUDGET_MAX_S,
        repo_root=str(REPO_ROOT),
        scope=str(ips.get("scope", "auto")),
    )
