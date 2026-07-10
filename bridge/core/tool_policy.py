"""Approval-gated Bash exposure policy for the Telegram bridge.

``PROJECT_ROOT`` is an approval/working-directory boundary, not a shell
sandbox. Bash is exposed by default under ``approve-each``, which requires the
permission callback to approve every individual Bash call. Operators can still
hard-disable Bash explicitly; unknown policy values fail closed.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from claude_agent_sdk import HookMatcher

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
    """Default missing values to approve-each and fail closed on unknown values."""

    value = os.getenv(BASH_POLICY_ENV, BASH_APPROVE_EACH) if raw is None else raw
    normalized = str(value).strip().lower().replace("_", "-")
    if normalized == BASH_APPROVE_EACH:
        return BASH_APPROVE_EACH
    return BASH_DISABLED


def allowed_tools(bash_policy: Optional[str] = None) -> List[str]:
    """Build the SDK allowlist without auto-approving Bash."""

    del bash_policy
    # An unlisted tool remains visible and falls through to permission
    # evaluation. A bare Bash entry would auto-approve every invocation before
    # can_use_tool is consulted, so Bash must never be added here.
    return list(STRUCTURED_ALLOWED_TOOLS)


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
    """Force every exposed Bash call through can_use_tool.

    A PreToolUse ``ask`` decision takes precedence over allow rules, including
    broad ``Bash(*)`` rules inherited from settings.json.
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
    """Keep Bash fail-closed if stream/request callback state is unavailable."""

    del bash_policy  # Reserved for future sandboxed policies; Bash always fails closed.
    return tool_name == "Bash"
