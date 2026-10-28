"""The Claude model a conversation runs on when it has made no explicit /model choice.

The Claude CLI resolves a missing ``--model`` as ``ANTHROPIC_MODEL`` (the
bridge unit's env, set per node by the main-session drop-ins) first and
``~/.claude/settings.json`` ``model`` second.  The bridge used to read only
settings.json here, so on a node whose unit env and settings disagreed the
``/model`` table and the ``/new`` session sync named one model while the
turns ran on another (the hande bridge of one node ran Opus 5.5 from its
unit env while ``/model`` said Fable 5.1 from settings).  Resolving in the
CLI's order keeps what the bridge shows and persists equal to what runs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Union


def env_claude_model() -> Optional[str]:
    """``ANTHROPIC_MODEL`` from the process env, or ``None`` when unset/blank."""

    value = os.environ.get("ANTHROPIC_MODEL", "").strip()
    return value or None


def settings_claude_model(settings_path: Union[str, Path, None]) -> Optional[str]:
    """``model`` from ``settings_path`` (``~/.claude/settings.json``), or ``None``."""

    if not settings_path:
        return None
    try:
        with open(settings_path, "r") as handle:
            value = json.load(handle).get("model")
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def claude_default_model(
    settings_path: Union[str, Path, None], default: Optional[str] = None
) -> Optional[str]:
    """Env-routed model first, then settings.json, then ``default`` — the CLI's order."""

    return env_claude_model() or settings_claude_model(settings_path) or default
