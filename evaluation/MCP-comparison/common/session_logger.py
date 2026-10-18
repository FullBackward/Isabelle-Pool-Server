"""Per-attempt session logging for the MCP comparison harness.

Writes a human-readable transcript of every model message, tool call, and tool result
to runs/<system>/logs/<problem>_rep<repeat>.log, and echoes the same transcript to stdout
so the live conversation is visible while a problem is being solved.
"""
from __future__ import annotations

import json
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any


class SessionLogger:

    def __init__(
        self,
        system: str,
        problem: str,
        repeat: int,
        runs_dir: Path,
        console: bool = True,
        variant: str | None = None,
    ):
        self.console = console
        # a prompt variant keeps its own results/logs folder under the system
        # (runs/<system>/<variant>/…), which analyze.py reports as a variant row
        self.log_dir = (
            runs_dir / system / variant if variant else runs_dir / system
        ) / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.log_dir / f"{problem}_rep{repeat}.log"
        self._fh = open(self.log_path, "w", encoding="utf-8")
        self._write_header(system, problem, repeat)

    def _write_header(self, system: str, problem: str, repeat: int) -> None:
        header = (
            f"Session transcript\n"
            f"system: {system}\n"
            f"problem: {problem}\n"
            f"repeat: {repeat}\n"
            f"started: {datetime.utcnow().isoformat()}Z\n"
            f"{'=' * 72}\n\n"
        )
        self._fh.write(header)
        self._fh.flush()
        if self.console:
            sys.stdout.write(header)
            sys.stdout.flush()

    def _emit(self, text: str) -> None:
        self._fh.write(text)
        self._fh.flush()
        if self.console:
            sys.stdout.write(text)
            sys.stdout.flush()

    def log_message(self, message: dict[str, Any]) -> None:
        role = message.get("role", "unknown")
        lines = [f"[{datetime.utcnow().isoformat()}Z] MESSAGE role={role}\n"]
        content = message.get("content")
        if content:
            lines.append(textwrap.indent(str(content), "    ") + "\n")
        if role == "assistant" and "tool_calls" in message:
            for tc in message["tool_calls"]:
                lines.append(f"    TOOL_CALL {tc.get('id')}: {tc['function']['name']}\n")
                try:
                    args = json.loads(tc["function"]["arguments"])
                    args_json = json.dumps(args, ensure_ascii=False, indent=6)
                except Exception:
                    args_json = tc["function"]["arguments"]
                lines.append(textwrap.indent(args_json, "      ") + "\n")
        lines.append("-" * 72 + "\n\n")
        self._emit("".join(lines))

    def log_tool_result(self, tool_call_id: str, name: str, output: str) -> None:
        text = (
            f"[{datetime.utcnow().isoformat()}Z] TOOL_RESULT {tool_call_id} ({name})\n"
            f"{textwrap.indent(output, '    ')}\n"
            f"{'-' * 72}\n\n"
        )
        self._emit(text)

    def log_text(self, label: str, text: str) -> None:
        wrapped = (
            f"[{datetime.utcnow().isoformat()}Z] {label}\n"
            f"{textwrap.indent(text, '    ')}\n"
            f"{'-' * 72}\n\n"
        )
        self._emit(wrapped)

    def close(self) -> None:
        self._emit(f"[{datetime.utcnow().isoformat()}Z] session closed\n")
        self._fh.close()

    def __enter__(self) -> SessionLogger:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
