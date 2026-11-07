"""Append text to the Claude Code system prompt without replacing it (#2712).

The Claude Agent SDK maps a plain-string ``system_prompt`` to
``--system-prompt <text>``, which the CLI treats as a *full replacement* of the
Claude Code default prompt (verified 2026-11-07 on CLI 2.1.x: a string prompt
answered "REPLACED"; ``--append-system-prompt`` kept the default and appended).
Routing hints and the node's output style are additions, so every caller goes
through :func:`append_system_prompt`, which keeps the ``claude_code`` preset and
accumulates text in its ``append`` field.
"""
from __future__ import annotations

from typing import Any

SEPARATOR = "\n\n"


def append_system_prompt(options: Any, text: str | None) -> None:
    """Add ``text`` after the default Claude Code prompt (and anything appended before)."""

    if not text:
        return
    current = getattr(options, "system_prompt", None)
    if isinstance(current, dict):
        if current.get("type") != "preset":
            return  # a custom/file prompt was chosen on purpose; leave it alone
        previous = str(current.get("append") or "")
        options.system_prompt = {**current, "append": previous + SEPARATOR + text if previous else text}
        return
    previous = current if isinstance(current, str) else ""
    options.system_prompt = {
        "type": "preset",
        "preset": "claude_code",
        "append": previous + SEPARATOR + text if previous else text,
    }


def appended_text(options: Any) -> str:
    """The text appended so far ('' when none) — for tests and diagnostics."""

    current = getattr(options, "system_prompt", None)
    if isinstance(current, dict):
        return str(current.get("append") or "")
    return current if isinstance(current, str) else ""
