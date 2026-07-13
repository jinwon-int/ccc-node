"""Execution-boundary and Bash approval policy for the Telegram bridge.

``CCC_BRIDGE_EXECUTION_PROFILE`` selects the SDK boundary independently from
``CCC_BRIDGE_BASH_POLICY`` approval UX. ``strict-project`` keeps the fail-closed
OS sandbox, ``owner-operator`` intentionally restores normal host-capable Claude
Code execution for exactly one allowlisted owner, and ``disabled`` removes Bash.
Unknown or unsafe profile values fail closed to ``disabled``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import claude_agent_sdk
from claude_agent_sdk import HookMatcher
from claude_agent_sdk.types import HookContext, HookInput, HookJSONOutput

BASH_POLICY_ENV = "CCC_BRIDGE_BASH_POLICY"
BASH_DISABLED = "disabled"
BASH_APPROVE_EACH = "approve-each"
BASH_AUTO_APPROVE = "auto-approve"
BASH_AUTO_REVIEW = "auto-review"

EXECUTION_PROFILE_ENV = "CCC_BRIDGE_EXECUTION_PROFILE"
EXECUTION_OWNER_OPERATOR = "owner-operator"
EXECUTION_STRICT_PROJECT = "strict-project"
EXECUTION_DISABLED = "disabled"


def owner_operator_access_is_safe(
    allowed_user_ids: Optional[Sequence[int]], require_allowlist: bool
) -> bool:
    """Return whether host-capable execution is bound to one explicit owner."""

    owners = {int(user_id) for user_id in (allowed_user_ids or [])}
    return bool(require_allowlist) and len(owners) == 1


def resolve_execution_profile(
    raw: Optional[str] = None,
    *,
    allowed_user_ids: Optional[Sequence[int]] = None,
    require_allowlist: bool = True,
) -> str:
    """Resolve the SDK execution boundary, failing closed on unsafe inputs.

    ``strict-project`` remains the package default. ``owner-operator`` is valid
    only when Telegram access is constrained to one explicit owner; otherwise
    it degrades to ``disabled`` so Bash cannot accidentally gain host scope.
    """

    value = os.getenv(EXECUTION_PROFILE_ENV, EXECUTION_STRICT_PROJECT) if raw is None else raw
    normalized = str(value).strip().lower().replace("_", "-")
    if normalized == EXECUTION_OWNER_OPERATOR:
        if owner_operator_access_is_safe(allowed_user_ids, require_allowlist):
            return EXECUTION_OWNER_OPERATOR
        return EXECUTION_DISABLED
    if normalized == EXECUTION_STRICT_PROJECT:
        return EXECUTION_STRICT_PROJECT
    if normalized == EXECUTION_DISABLED:
        return EXECUTION_DISABLED
    return EXECUTION_DISABLED


# Bash needs a small read-only host runtime to start interpreters and resolve
# libraries. Everything else is hidden by denyRead=["/"]. The project root is
# added dynamically; the Claude Code sandbox separately grants its per-session
# temporary directory. Do not add user-controlled or credential-bearing roots.
_SANDBOX_RUNTIME_READ_PATHS = (
    "/bin",
    "/sbin",
    "/usr",
    "/lib",
    "/lib64",
    "/etc/ld.so.cache",
    "/etc/localtime",
    "/etc/hosts",
    "/etc/resolv.conf",
    "/etc/nsswitch.conf",
    "/etc/ssl/certs",
)


def _sandbox_runtime_read_paths(configured_cli_path: Optional[str] = None) -> List[str]:
    """Return trusted SDK/CLI roots needed by the sandbox bootstrap itself."""

    candidates: List[Path] = []
    sdk_file = getattr(claude_agent_sdk, "__file__", None)
    if sdk_file:
        candidates.append(Path(sdk_file).resolve().parent)

    cli_path = configured_cli_path or shutil.which("claude")
    if cli_path:
        cli = Path(cli_path).expanduser()
        # npm launchers commonly resolve into the package directory that also
        # owns sandbox helper binaries; expose that resolved package only.
        candidates.append(cli.resolve().parent)

    seen: set[str] = set()
    result: List[str] = []
    for path in candidates:
        value = str(path)
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def strict_bash_sandbox_settings(
    project_root: Path, configured_cli_path: Optional[str] = None
) -> Dict[str, Any]:
    """Return the non-widenable OS sandbox contract for Bash.

    The command text is deliberately absent from this function. Variable
    expansion, interpreter-mediated I/O, ``cd ..`` and symlink traversal are
    all confined by the SDK's OS sandbox instead of a shell-token parser.

    ``filesystem`` and ``failIfUnavailable`` are forwarded by current Claude
    Agent SDK releases even though older ``SandboxSettings`` annotations did
    not declare every settings.json key.
    """

    root = Path(project_root).resolve()
    allow_read = [
        str(root),
        *_SANDBOX_RUNTIME_READ_PATHS,
        *_sandbox_runtime_read_paths(configured_cli_path),
    ]
    return {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "excludedCommands": [],
        "enableWeakerNestedSandbox": False,
        "ignoreViolations": {"file": [], "network": []},
        "filesystem": {
            "denyRead": ["/"],
            "allowRead": allow_read,
            # Sandbox Runtime uses an allow-only write model: an explicit root
            # list means sibling, parent and host paths remain read-only/hidden.
            # denyWrite must stay empty because deny rules take precedence over
            # allowWrite and denyWrite=["/"] would also block the project root.
            "allowWrite": [str(root)],
            "denyWrite": [],
        },
    }


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
    if normalized in (BASH_AUTO_APPROVE, BASH_APPROVE_EACH, BASH_AUTO_REVIEW):
        return normalized
    return BASH_DISABLED


def effective_bash_policy(bash_policy: Optional[str], execution_profile: str) -> str:
    """Apply the execution boundary without coupling it to approval UX."""

    if execution_profile not in (EXECUTION_OWNER_OPERATOR, EXECUTION_STRICT_PROJECT):
        return BASH_DISABLED
    return resolve_bash_policy(bash_policy)


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
    """Force Bash through ``can_use_tool`` under interactive policies.

    A PreToolUse ``ask`` decision takes precedence over allow rules, including
    broad ``Bash(*)`` rules inherited from settings.json. ``auto-approve``
    deliberately installs no ask hook. ``auto-review`` uses this conservative
    path for Claude, which has no Codex reviewer agent.
    """

    if resolve_bash_policy(bash_policy) not in (
        BASH_APPROVE_EACH,
        BASH_AUTO_REVIEW,
    ):
        return {}

    async def require_per_call_approval(
        _input_data: HookInput,
        _tool_use_id: Optional[str],
        _context: HookContext,
    ) -> HookJSONOutput:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": ("Bash requires explicit per-call Telegram approval."),
            }
        }

    return {"PreToolUse": [HookMatcher(matcher="Bash", hooks=[require_per_call_approval])]}


def sdk_permission_options(bash_policy: Optional[str] = None) -> Dict[str, Any]:
    """Build one internally consistent SDK permission bundle."""

    policy = resolve_bash_policy(bash_policy)
    return {
        "allowed_tools": allowed_tools(policy),
        "disallowed_tools": disallowed_tools(policy),
        "hooks": bash_permission_hooks(policy),
    }


def missing_callback_requires_denial(tool_name: str, bash_policy: Optional[str] = None) -> bool:
    """Require callback state unless the operator selected auto-approval."""

    if tool_name != "Bash":
        return False
    return resolve_bash_policy(bash_policy) != BASH_AUTO_APPROVE
