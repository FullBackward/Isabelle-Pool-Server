from __future__ import annotations


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def preview_text(text: str, max_len: int = 160) -> str:
    one_line = " ".join(_normalize(text).split())
    if len(one_line) <= max_len:
        return one_line
    return one_line[: max_len - 3] + "..."
