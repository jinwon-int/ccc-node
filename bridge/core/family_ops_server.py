#!/usr/bin/env python3
"""``family-ops`` — read-only fleet/node operations MCP server (#1694).

Tools: ``node_status`` — one call aggregating serving-checkout state,
bridge service/transport health and scheduler occupancy for this node, or a
peer node over ssh; ``task_status`` — checkpoint/resume/wait-promise
recovery aggregation; ``pr_readiness`` — pre-merge lookup snapshot for one
pull request via the node's authenticated gh; ``deployment_diff`` —
pre-deployment diff (checkout/target/installed/deps/recovery) reusing the
self-update check and doctor sources.  Shares the stdio scaffolding
with ``family-skills``; stdlib-only, runs under any python3:

    python3 <repo>/bridge/core/family_ops_server.py

Access policy is identical to ``family-skills``: enforced per ``tools/call``
from this process environment, never from tool arguments.  Results carry
observation timestamps and ``unknown`` markers for partial failures; they
inform operators and never substitute for approvals or live re-verification.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CORE_DIR = Path(__file__).resolve().parents[1]
for _path in (str(_REPO_ROOT), str(_CORE_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

try:
    from telegram_bot.core.mcp_stdio import (  # noqa: E402
        ToolError,
        handle_message as _shared_handle_message,
        run_tools_server,
        tool_result,
    )
    from telegram_bot.core.deployment_diff import (  # noqa: E402
        DeploymentDiffError,
        collect as collect_deployment_diff,
    )
    from telegram_bot.core.node_status import NodeStatusError, node_status  # noqa: E402
    from telegram_bot.core.pr_readiness import PrReadinessError, collect as collect_pr_readiness  # noqa: E402
    from telegram_bot.core.skill_lookup import policy_denial  # noqa: E402
    from telegram_bot.core.task_status import TaskStatusError, collect as collect_task_status  # noqa: E402
except ImportError:  # pragma: no cover - worktree aliasing only
    from mcp_stdio import (  # noqa: E402  # type: ignore[no-redef]
        ToolError,
        handle_message as _shared_handle_message,
        run_tools_server,
        tool_result,
    )
    from deployment_diff import (  # noqa: E402  # type: ignore[no-redef]
        DeploymentDiffError,
        collect as collect_deployment_diff,
    )
    from node_status import NodeStatusError, node_status  # noqa: E402  # type: ignore[no-redef]
    from pr_readiness import PrReadinessError, collect as collect_pr_readiness  # noqa: E402  # type: ignore[no-redef]
    from skill_lookup import policy_denial  # noqa: E402  # type: ignore[no-redef]
    from task_status import TaskStatusError, collect as collect_task_status  # noqa: E402  # type: ignore[no-redef]

SERVER_NAME = "family-ops"
SERVER_VERSION = "1.0.0"

_NODE_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "node": {
            "type": "string",
            "description": (
                "Optional ssh host alias of a peer node; omit for this node. "
                "Requires the peer to run this repo's scripts/ccc-node-status.py."
            ),
        },
    },
    "additionalProperties": False,
}

_TASK_STATUS_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_PR_READINESS_SCHEMA = {
    "type": "object",
    "properties": {
        "repo": {"type": "string", "description": "GitHub repository as OWNER/REPO."},
        "pr": {"type": "integer", "description": "Pull request number."},
    },
    "required": ["repo", "pr"],
    "additionalProperties": False,
}

_DEPLOYMENT_DIFF_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_TOOLS = [
    {
        "name": "deployment_diff",
        "description": (
            "Read-only pre-deployment diff: serving checkout vs origin/main "
            "(ahead/behind/dirty), installed-harness marker and doctor drift "
            "rows, dependency file changes, latest setup backup, and an "
            "informational restart-recommendation verdict. Reuses the "
            "self-update check/doctor sources; never pulls, installs, or "
            "restarts anything."
        ),
        "inputSchema": _DEPLOYMENT_DIFF_SCHEMA,
    },
    {
        "name": "pr_readiness",
        "description": (
            "Read-only pre-merge PR readiness for one OWNER/REPO pull request: "
            "head sha/mergeability, CI rollup counted with the gh-pr-flow relay "
            "gate rule, latest reviews (non-author, head-matched approvals), and "
            "unresolved review threads. Informational snapshot; never a "
            "substitute for approvals or merge-time re-verification."
        ),
        "inputSchema": _PR_READINESS_SCHEMA,
    },
    {
        "name": "task_status",
        "description": (
            "Read-only task recovery status: the agent checkpoint "
            "(working-state), resume note, and external wait promises "
            "(active pending / dropped). Bounded content; observation time; "
            "partial failures reported as unknown."
        ),
        "inputSchema": _TASK_STATUS_SCHEMA,
    },
    {
        "name": "node_status",
        "description": (
            "Read-only node status: source revision/cleanliness, bridge "
            "process and service/transport/provider health, scheduler/task "
            "occupancy, observation time. Partial failures are reported as "
            "unknown; results never substitute for approvals."
        ),
        "inputSchema": _NODE_STATUS_SCHEMA,
    },
]


def _dispatch_node_status(arguments: dict[str, Any]) -> dict[str, Any]:
    node = arguments.get("node")
    try:
        if node is None:
            result = node_status()
        else:
            if not isinstance(node, str):
                raise NodeStatusError("invalid_node", "node must be a string")
            result = node_status([node])
    except NodeStatusError as error:
        raise ToolError(error.code, str(error), **error.details) from error
    _diag(f"call node_status ok node={node or 'local'} status={result.get('status')}")
    return tool_result(result)


def _dispatch_pr_readiness(arguments: dict[str, Any]) -> dict[str, Any]:
    repo = arguments.get("repo")
    pr = arguments.get("pr")
    if not isinstance(repo, str) or not repo.strip():
        raise ToolError("invalid_repo", "repo must be a non-empty OWNER/REPO string")
    if isinstance(pr, bool) or not isinstance(pr, int):
        raise ToolError("invalid_pr", "pr must be an integer")
    try:
        result = collect_pr_readiness(repo, pr)
    except PrReadinessError as error:
        raise ToolError(error.code, str(error), **error.details) from error
    _diag(f"call pr_readiness ok repo={repo} pr={pr} status={result.get('status')}")
    return tool_result(result)


def _dispatch(name: str, arguments: Any) -> dict[str, Any]:
    denial = policy_denial()
    if denial is not None:
        raise ToolError(
            "policy_denied", "family-ops denied by node policy", reason=denial
        )
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolError("invalid_arguments", "arguments must be an object")
    if name == "task_status":
        try:
            result = collect_task_status()
        except TaskStatusError as error:
            raise ToolError(error.code, str(error), **error.details) from error
        _diag(f"call task_status ok status={result.get('status')}")
        return tool_result(result)
    if name == "deployment_diff":
        try:
            result = collect_deployment_diff()
        except DeploymentDiffError as error:
            raise ToolError(error.code, str(error), **error.details) from error
        _diag(f"call deployment_diff ok status={result.get('status')}")
        return tool_result(result)
    if name == "pr_readiness":
        return _dispatch_pr_readiness(arguments)
    if name != "node_status":
        raise ToolError("unknown_tool", f"unknown tool: {name}")
    return _dispatch_node_status(arguments)


def _diag(line: str) -> None:
    print(f"{SERVER_NAME}: {line}", file=sys.stderr, flush=True)


def handle_message(message: Any) -> dict[str, Any] | None:
    """Unit-test wrapper over the shared stdio handler."""

    return _shared_handle_message(
        message,
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        tools=_TOOLS,
        dispatch=_dispatch,
    )


def main() -> int:
    return run_tools_server(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        tools=_TOOLS,
        dispatch=_dispatch,
        diag=_diag,
    )


if __name__ == "__main__":
    raise SystemExit(main())
