"""The node's Claude Code output style for lanes without a settings chain (#2712).

Owner lanes that drop the host settings chain (``CCC_BRIDGE_CLAUDE_UNRESTRICTED``
and audience-scoped memory set ``setting_sources=[]``) never read
``settings.json``'s ``outputStyle`` nor ``output-styles/``. This reads the same
style the governed path would load and returns it as text to append to the
default system prompt. Missing, unsafe, or oversized files disable it quietly:
an optional prose policy must never block an owner turn.
"""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

MAX_STYLE_BYTES = 32 * 1024
MAX_SETTINGS_BYTES = 256 * 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def _read_regular(path: Path, limit: int) -> str | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
                or info.st_mode & 0o022 or info.st_size > limit):
            return None
        data = os.read(descriptor, limit + 1)
        return data.decode("utf-8") if len(data) <= limit else None
    except (OSError, UnicodeError):
        return None
    finally:
        os.close(descriptor)


def _strip_front_matter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            newline = text.find("\n", end + 4)
            return text[newline + 1:] if newline != -1 else ""
    return text


def read_output_style(claude_dir: Path) -> str:
    """``# Output Style: <name>`` + the style body, or '' when none is configured or readable."""

    raw = _read_regular(claude_dir / "settings.json", MAX_SETTINGS_BYTES)
    if raw is None:
        return ""
    try:
        name = json.loads(raw).get("outputStyle")
    except (ValueError, AttributeError):
        return ""
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name.lower() == "default":
        return ""
    body = _read_regular(claude_dir / "output-styles" / f"{name}.md", MAX_STYLE_BYTES)
    if body is None:
        return ""
    body = _strip_front_matter(body).strip()
    return f"# Output Style: {name}\n{body}" if body else ""
