"""Build a narrow Claude flag-settings layer for bridge memory lifecycle hooks.

Non-owner execution profiles deliberately suppress all filesystem settings.
Operators may explicitly enable this module to restore only ccc-node's bounded
memory/distill lifecycle without loading arbitrary user/project/local settings.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any


MEMORY_MODE_OFF = "off"
MEMORY_MODE_CURATED = "curated"


def _command(hook_dir: Path, relative: str, *args: str, background: bool = False) -> str:
    command = " ".join(
        ["bash", shlex.quote(str(hook_dir / relative)), *(shlex.quote(arg) for arg in args)]
    )
    if background:
        return f"{command} >/dev/null 2>&1 &"
    return command


def build_curated_memory_settings(settings: Any) -> str | None:
    """Return a deterministic JSON ``--settings`` value, or ``None`` when off."""

    if getattr(settings, "bridge_memory_mode", MEMORY_MODE_OFF) != MEMORY_MODE_CURATED:
        return None
    hook_dir = Path(settings.claude_settings_path).expanduser().parent / "hooks"
    policy_env = dict(settings.hook_policy_environment())
    payload = {
        "env": policy_env,
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": _command(hook_dir, "load-memory.sh", "SessionStart"),
                            "timeout": 15,
                        },
                        {
                            "type": "command",
                            "command": (
                                "CLAUDE_DISTILL_INFLIGHT=1 "
                                + _command(
                                    hook_dir,
                                    "distill/queue-drain.sh",
                                    background=True,
                                )
                            ),
                            "timeout": 5,
                        },
                        {
                            "type": "command",
                            "command": _command(
                                hook_dir,
                                "distill/pending-drain.sh",
                                background=True,
                            ),
                            "timeout": 5,
                        },
                    ]
                }
            ],
            "PreCompact": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": _command(hook_dir, "checkpoint.sh", "PreCompact"),
                            "timeout": 10,
                        },
                        {
                            "type": "command",
                            "command": _command(hook_dir, "distill.sh", "precompact"),
                            "timeout": 10,
                        },
                    ]
                }
            ],
            "PostCompact": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": _command(hook_dir, "load-memory.sh", "PostCompact"),
                            "timeout": 15,
                        },
                        {
                            "type": "command",
                            "command": _command(hook_dir, "checkpoint.sh", "PostCompact"),
                            "timeout": 10,
                        },
                    ]
                }
            ],
            "SessionEnd": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": _command(hook_dir, "distill.sh", "sessionend"),
                            "timeout": 10,
                        }
                    ]
                }
            ],
        },
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
