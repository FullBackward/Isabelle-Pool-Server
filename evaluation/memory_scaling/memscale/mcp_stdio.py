"""A minimal MCP client over stdio (newline-delimited JSON-RPC 2.0), used
to drive `isabelle pide_mcp`. The official SDK hides the child process; this
client owns it, so the driver knows the pid (the sampler's root) and can
kill the tree. Only what the experiments need: initialize, tools/list,
tools/call with a timeout; notifications (progress, logs) are ignored."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = "2025-11-25"
# asyncio's default readline limit is 64 KiB; a tool result is ONE line and a
# PIDE state can be far larger — the reader died on it and every pending call
# timed out (stage-3 smoke).
STREAM_LIMIT = 256 * 1024 * 1024


class MCPError(RuntimeError):
    """A tool returned isError or the server answered with a JSON-RPC error."""


class StdioMCP:  # pylint: disable=too-many-instance-attributes
    """``async with StdioMCP(cmd) as m: await m.call("start_session", {...})``"""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        command: List[str],
        *,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        stderr_path: Optional[str] = None,
        name: str = "memscale",
    ) -> None:
        self.command = list(command)
        self.env = env
        self.cwd = cwd
        self.stderr_path = stderr_path
        self.name = name
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 1
        self._pending: Dict[int, asyncio.Future[Any]] = {}
        self._reader: Optional[asyncio.Task[None]] = None
        self.notifications: List[Dict[str, Any]] = []
        self.started_at: Optional[float] = None
        self.server_info: Dict[str, Any] = {}

    @property
    def pid(self) -> Optional[int]:
        """The spawned process (bash wrapper of `isabelle`, parent of the JVM)."""
        return self.proc.pid if self.proc is not None else None

    # ---------------------------------------------------------- lifecycle
    async def start(self, init_timeout_s: float = 300.0) -> "StdioMCP":
        """Spawn the server and complete the initialize handshake."""
        err = None
        if self.stderr_path:  # lives as long as the process; closed with it
            err = open(self.stderr_path, "ab")  # pylint: disable=consider-using-with
        self.started_at = time.time()
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=STREAM_LIMIT,  # one JSON line can be megabytes (get_state)
            stderr=err if err is not None else asyncio.subprocess.DEVNULL,
            env=self.env,
            cwd=self.cwd,
            start_new_session=True,
        )
        self._reader = asyncio.create_task(self._read_loop())
        result = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": self.name, "version": "0"},
            },
            timeout_s=init_timeout_s,
        )
        self.server_info = dict(result.get("serverInfo") or {})
        await self.notify("notifications/initialized", {})
        return self

    async def __aenter__(self) -> "StdioMCP":
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self, grace_s: float = 15.0) -> None:
        """Terminate the server's process group (the JVM and its provers)."""
        proc = self.proc
        if proc is None:
            return
        if self._reader is not None:
            self._reader.cancel()
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), timeout=grace_s)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.proc = None

    # ------------------------------------------------------------ protocol
    def _fail_pending(self, why: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MCPError(why))
        self._pending.clear()

    async def _read_loop(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        while True:
            try:
                line = await self.proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError) as exc:
                self._fail_pending(f"server line too long for the reader: {exc}")
                return
            if not line:
                self._fail_pending("server closed its stdout")
                return
            try:
                msg = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                fut = self._pending.pop(int(msg["id"]), None)
                if fut is not None and not fut.done():
                    if "error" in msg:
                        fut.set_exception(MCPError(json.dumps(msg["error"])[:500]))
                    else:
                        fut.set_result(msg.get("result"))
            else:
                self.notifications.append(msg)

    async def _send(self, msg: Dict[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def notify(self, method: str, params: Dict[str, Any]) -> None:
        """A JSON-RPC notification (no reply)."""
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def request(
        self, method: str, params: Dict[str, Any], timeout_s: float = 600.0
    ) -> Any:
        """A JSON-RPC request; raises MCPError on an error reply or timeout."""
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        )
        try:
            return await asyncio.wait_for(fut, timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            self._pending.pop(rid, None)
            raise MCPError(f"{method} timed out after {timeout_s}s") from exc

    async def list_tools(self) -> List[Dict[str, Any]]:
        """tools/list → the tool descriptors."""
        res = await self.request("tools/list", {}, timeout_s=60.0)
        return list(res.get("tools") or [])

    async def call(
        self, name: str, arguments: Dict[str, Any], timeout_s: float = 600.0
    ) -> Dict[str, Any]:
        """tools/call → the structured content (or the parsed text content);
        raises MCPError when the result is flagged isError."""
        res = await self.request(
            "tools/call", {"name": name, "arguments": arguments}, timeout_s=timeout_s
        )
        if res.get("isError"):
            raise MCPError(f"{name}: {_text_of(res)[:500]}")
        structured = res.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        text = _text_of(res)
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except ValueError:
            return {"text": text}


def _text_of(res: Dict[str, Any]) -> str:
    parts = []
    for c in res.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(str(c.get("text", "")))
    return "\n".join(parts)
