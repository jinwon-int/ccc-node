"""Three-state Bash permission policy for the Telegram bridge.

``PROJECT_ROOT`` is a working-directory/structured-tool boundary, not a shell
sandbox. The operator-selected default is ``auto-approve``: Bash is placed in
the SDK bare allowlist and runs without per-call Telegram confirmation.
``approve-each`` remains available for explicit one-time confirmation, while
``disabled`` removes Bash entirely. Unknown policy values fail closed.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from claude_agent_sdk import HookMatcher

BASH_POLICY_ENV = "CCC_BRIDGE_BASH_POLICY"
BASH_DISABLED = "disabled"
BASH_APPROVE_EACH = "approve-each"
BASH_AUTO_APPROVE = "auto-approve"

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
    """Default missing values to auto-approve and fail closed on unknown values."""

    value = os.getenv(BASH_POLICY_ENV, BASH_AUTO_APPROVE) if raw is None else raw
    normalized = str(value).strip().lower().replace("_", "-")
    if normalized in (BASH_AUTO_APPROVE, BASH_APPROVE_EACH):
        return normalized
    return BASH_DISABLED


def allowed_tools(bash_policy: Optional[str] = None) -> List[str]:
    """Build the SDK allowlist for the selected Bash policy.

    A bare ``Bash`` entry is intentionally used only for ``auto-approve``;
    Claude Agent SDK evaluates it as an automatic allow rule before
    ``can_use_tool``.
    """

    policy = resolve_bash_policy(bash_policy)
    tools: List[str] = list(STRUCTURED_ALLOWED_TOOLS)
    if policy == BASH_AUTO_APPROVE:
        tools.append("Bash")
    return tools


def disallowed_tools(bash_policy: Optional[str] = None) -> List[str]:
    """Build the SDK hard-deny list for interactive and disabled tools."""

    policy = resolve_bash_policy(bash_policy)
    tools = ["AskUserQuestion"]
    if policy == BASH_DISABLED:
        tools.append("Bash")
    return tools


def bash_permission_hooks(
    bash_policy: Optional[str] = None,
) -> Dict[str, List[HookMatcher]]:
    """Force Bash through ``can_use_tool`` only under ``approve-each``.

    A PreToolUse ``ask`` decision takes precedence over allow rules, including
    broad ``Bash(*)`` rules inherited from settings.json. ``auto-approve``
    deliberately installs no ask hook.
    """

    if resolve_bash_policy(bash_policy) != BASH_APPROVE_EACH:
        return {}

    async def require_per_call_approval(
        _input_data: Dict[str, Any], _tool_use_id: Optional[str], _context: Any
    ) -> Dict[str, Any]:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": (
                    "Bash requires explicit per-call Telegram approval."
                ),
            }
        }

    return {
        "PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[require_per_call_approval])
        ]
    }


def sdk_permission_options(bash_policy: Optional[str] = None) -> Dict[str, Any]:
    """Build one internally consistent SDK permission bundle."""

    policy = resolve_bash_policy(bash_policy)
    return {
        "allowed_tools": allowed_tools(policy),
        "disallowed_tools": disallowed_tools(policy),
        "hooks": bash_permission_hooks(policy),
    }


def missing_callback_requires_denial(
    tool_name: str, bash_policy: Optional[str] = None
) -> bool:
    """Require callback state unless the operator selected auto-approval."""

    if tool_name != "Bash":
        return False
    return resolve_bash_policy(bash_policy) != BASH_AUTO_APPROVE
