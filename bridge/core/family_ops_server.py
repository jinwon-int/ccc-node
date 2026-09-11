#!/usr/bin/env python3
"""``family-ops`` — read-only fleet/node operations MCP server (#1694).

First tool: ``node_status`` — one call aggregating serving-checkout state,
bridge service/transport health and scheduler occupancy for this node, or a
peer node over ssh.  Shares the stdio scaffolding with ``family-skills``;
stdlib-only, runs under any python3:

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
    from telegram_bot.core.node_status import NodeStatusError, node_status  # noqa: E402
    from telegram_bot.core.skill_lookup import policy_denial  # noqa: E402
except ImportError:  # pragma: no cover - worktree aliasing only
    from mcp_stdio import (  # noqa: E402  # type: ignore[no-redef]
        ToolError,
        handle_message as _shared_handle_message,
        run_tools_server,
        tool_result,
    )
    from node_status import NodeStatusError, node_status  # noqa: E402  # type: ignore[no-redef]
    from skill_lookup import policy_denial  # noqa: E402  # type: ignore[no-redef]

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

_TOOLS = [
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


def _dispatch(name: str, arguments: Any) -> dict[str, Any]:
    denial = policy_denial()
    if denial is not None:
        raise ToolError(
            "policy_denied", "node status denied by node policy", reason=denial
        )
    if name != "node_status":
        raise ToolError("unknown_tool", f"unknown tool: {name}")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolError("invalid_arguments", "arguments must be an object")
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
