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

from telegram_bot.core.memory_audience import MemoryAudience
from telegram_bot.utils.memory_policy import (
    MEMORY_MODE_AUDIENCE_SCOPED,
    MEMORY_MODE_CURATED,
    MEMORY_MODE_OFF,
    assert_memory_scope_safe,
    owner_global_memory_gate,
)


def owner_nunchi_injection_allowed(settings: Any, audience: MemoryAudience | None) -> bool:
    """May this curated session read the node-global nunchi snapshot? (#2844)

    The node-global nunchi store is the owner's own memory, and the curated
    layer is shared by non-owner profiles, so the hook is added only when the
    write-side privacy gate (``owner_global_memory_gate``) passes, no route
    audience is attached (audience-scoped memory has its own scoped
    snapshot), and the node is not on the ``external`` isolation profile.
    """

    if audience is not None:
        return False
    if getattr(settings, "bridge_memory_mode", MEMORY_MODE_OFF) != MEMORY_MODE_CURATED:
        return False
    if str(getattr(settings, "node_isolation_profile", "fleet") or "fleet") != "fleet":
        return False
    allowed, _profile, _scope = owner_global_memory_gate(settings)
    return allowed


def _command(hook_dir: Path, relative: str, *args: str, background: bool = False) -> str:
    command = " ".join(
        ["bash", shlex.quote(str(hook_dir / relative)), *(shlex.quote(arg) for arg in args)]
    )
    if background:
        return f"{command} >/dev/null 2>&1 &"
    return command


def build_curated_memory_settings(
    settings: Any, *, audience: MemoryAudience | None = None
) -> str | None:
    """Return a deterministic JSON ``--settings`` value, or ``None`` when off."""

    mode = getattr(settings, "bridge_memory_mode", MEMORY_MODE_OFF)
    if mode not in {MEMORY_MODE_CURATED, MEMORY_MODE_AUDIENCE_SCOPED}:
        return None
    assert_memory_scope_safe(
        mode,
        getattr(settings, "telegram_session_scope", "per-user-chat"),
        unsafe_shared_all_override=bool(
            getattr(settings, "bridge_unsafe_shared_all_memory", False)
        ),
    )
    if mode == MEMORY_MODE_AUDIENCE_SCOPED and audience is None:
        raise ValueError("audience-scoped memory requires a resolved route audience")
    hook_dir = Path(settings.claude_settings_path).expanduser().parent / "hooks"
    policy_env = dict(settings.hook_policy_environment())
    if audience is not None:
        policy_env.update(audience.hook_environment(settings))
    session_start: list[dict[str, Any]] = [
        {
            "type": "command",
            "command": _command(hook_dir, "load-memory.sh", "SessionStart"),
            "timeout": 15,
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
    if owner_nunchi_injection_allowed(settings, audience):
        # The installer registers this hook in settings.local.json, which a
        # setting_sources=[] session never reads (#2844). The hook exits 0
        # when nunchi is not installed/enabled or the session is scoped.
        session_start.append(
            {
                "type": "command",
                "command": _command(hook_dir, "nunchi/sessionstart.sh"),
                "timeout": 15,
            }
        )
    payload = {
        "env": policy_env,
        "hooks": {
            "SessionStart": [{"hooks": session_start}],
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
