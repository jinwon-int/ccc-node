"""Fail-closed tool exposure policy for the Telegram bridge.

``PROJECT_ROOT`` is an approval/working-directory boundary, not a shell
sandbox. Bash is therefore absent by default. The only source-level opt-in is
``approve-each``, which still requires the permission callback to approve every
individual Bash call.
"""

from __future__ import annotations

import os
from typing import List, Optional

BASH_POLICY_ENV = "CCC_BRIDGE_BASH_POLICY"
BASH_DISABLED = "disabled"
BASH_APPROVE_EACH = "approve-each"

STRUCTURED_ALLOWED_TOOLS = (
    "Read",
    "Edit",
    "Write",
    "MultiEdit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "NotebookEdit",
    "TodoWrite",
)


def resolve_bash_policy(raw: Optional[str] = None) -> str:
    """Return a validated policy, failing closed on missing/unknown values."""

    value = os.getenv(BASH_POLICY_ENV, BASH_DISABLED) if raw is None else raw
    normalized = str(value).strip().lower().replace("_", "-")
    if normalized == BASH_APPROVE_EACH:
        return BASH_APPROVE_EACH
    return BASH_DISABLED


def allowed_tools(bash_policy: Optional[str] = None) -> List[str]:
    """Build the SDK allowlist for the validated Bash policy."""

    policy = resolve_bash_policy(bash_policy)
    tools: List[str] = list(STRUCTURED_ALLOWED_TOOLS)
    if policy == BASH_APPROVE_EACH:
        tools.append("Bash")
    return tools


def disallowed_tools(bash_policy: Optional[str] = None) -> List[str]:
    """Build the SDK hard-deny list for interactive and disabled tools."""

    policy = resolve_bash_policy(bash_policy)
    tools = ["AskUserQuestion"]
    if policy == BASH_DISABLED:
        tools.append("Bash")
    return tools


def missing_callback_requires_denial(
    tool_name: str, bash_policy: Optional[str] = None
) -> bool:
    """Keep Bash fail-closed if stream/request callback state is unavailable."""

    del bash_policy  # Reserved for future sandboxed policies; Bash always fails closed.
    return tool_name == "Bash"
