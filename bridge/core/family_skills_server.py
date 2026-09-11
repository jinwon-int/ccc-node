#!/usr/bin/env python3
"""``family-skills`` — read-only stdio MCP server over the skill lookup.

Exposes exactly two tools (``skill_search``, ``skill_read``) backed by
``skill_lookup`` so Claude CLI, the CCC Claude bridge and any other MCP
client resolve skills through the same fail-closed logic as the JSON CLI.

Framing and protocol handling live in the shared ``mcp_stdio`` scaffolding.
The implementation is stdlib-only and runs under any python3 — no bridge
virtualenv required:

    python3 <repo>/bridge/core/family_skills_server.py

Access policy is enforced per ``tools/call`` from this process environment
(``CCC_NODE_ISOLATION_PROFILE`` / ``CCC_MEMORY_AUDIENCE``), never from tool
arguments, so a user-scope registration or a direct connection cannot bypass
the node's isolation profile (#1678).  Diagnostics never contain queries,
skill bodies or other content — only tool names, ids and counts.
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

# Canonical import inside a normal checkout; the sibling-module fallback covers
# worktrees whose ``telegram_bot`` alias resolves to a different checkout than
# the one this server file lives in. Both paths load the same stdlib-only file.
try:
    from telegram_bot.core.skill_lookup import (  # noqa: E402
        MAX_LIMIT,
        MAX_QUERY_CHARS,
        RUNTIMES,
        SkillLookupError,
        policy_denial,
        read,
        search,
    )
except ImportError:  # pragma: no cover - worktree aliasing only
    from skill_lookup import (  # noqa: E402  # type: ignore[no-redef]
        MAX_LIMIT,
        MAX_QUERY_CHARS,
        RUNTIMES,
        SkillLookupError,
        policy_denial,
        read,
        search,
    )

try:
    from telegram_bot.core.mcp_stdio import (  # noqa: E402
        ToolError,
        handle_message as _shared_handle_message,
        run_tools_server,
        tool_result,
    )
except ImportError:  # pragma: no cover - worktree aliasing only
    from mcp_stdio import (  # noqa: E402  # type: ignore[no-redef]
        ToolError,
        handle_message as _shared_handle_message,
        run_tools_server,
        tool_result,
    )

SERVER_NAME = "family-skills"
SERVER_VERSION = "1.0.0"

_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_QUERY_CHARS,
            "description": "Space-separated tokens matched against skill names and descriptions.",
        },
        "runtime": {
            "type": "string",
            "enum": list(RUNTIMES),
            "description": "Optional origin filter: repo registry or an installed runtime root.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_LIMIT,
            "description": f"Maximum results (default 10, max {MAX_LIMIT}).",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "skill_id": {
            "type": "string",
            "pattern": "^(repo:[a-z][a-z0-9_./-]*|(claude|codex|piri):[a-z0-9][a-z0-9-]{0,63})$",
            "description": "Exact id from a skill_search result (repo source path or '<runtime>:<name>').",
        },
        "revision": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
            "description": "Optional expected revision from the search result; mismatch returns stale_revision.",
        },
    },
    "required": ["skill_id"],
    "additionalProperties": False,
}

_TOOLS = [
    {
        "name": "skill_search",
        "description": (
            "Search approved node/fleet skills (repo registry + installed roots) "
            "by name/description tokens. Returns bounded, deterministically "
            "ordered entries with skill_id and revision."
        ),
        "inputSchema": _SEARCH_SCHEMA,
    },
    {
        "name": "skill_read",
        "description": (
            "Read one validated SKILL.md by exact skill_id. Re-verifies path, "
            "symlink, ownership, permission, size and UTF-8 constraints; "
            "detects content drift against the advertised revision."
        ),
        "inputSchema": _READ_SCHEMA,
    },
]


def _dispatch(name: str, arguments: Any) -> dict[str, Any]:
    denial = policy_denial()
    if denial is not None:
        raise ToolError(
            "policy_denied", "skill lookup denied by node policy", reason=denial
        )
    try:
        return _lookup(name, arguments)
    except SkillLookupError as error:
        raise ToolError(error.code, str(error), **error.details) from error


def _lookup(name: str, arguments: Any) -> dict[str, Any]:
    if name == "skill_search":
        if not isinstance(arguments, dict):
            raise SkillLookupError("invalid_query", "arguments must be an object")
        query = arguments.get("query")
        if not isinstance(query, str):
            raise SkillLookupError("invalid_query", "query must be a string")
        result = search(query, arguments.get("runtime"), arguments.get("limit"))
        _diag(f"call skill_search ok results={len(result['results'])}")
        return tool_result(result)
    if name == "skill_read":
        if not isinstance(arguments, dict):
            raise SkillLookupError("invalid_skill_id", "arguments must be an object")
        skill_id = arguments.get("skill_id")
        if not isinstance(skill_id, str):
            raise SkillLookupError("invalid_skill_id", "skill_id must be a string")
        revision = arguments.get("revision")
        result = read(skill_id, revision if isinstance(revision, str) else None)
        _diag(f"call skill_read ok skill_id={result['skill_id']} bytes={result['bytes']}")
        return tool_result(result)
    raise SkillLookupError("unknown_tool", f"unknown tool: {name}")


def _diag(line: str) -> None:
    print(f"{SERVER_NAME}: {line}", file=sys.stderr, flush=True)


def handle_message(message: Any) -> dict[str, Any] | None:
    """Unit-test/compat wrapper over the shared stdio handler."""

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
