#!/usr/bin/env python3
"""``family-skills`` — read-only stdio MCP server over the skill lookup.

Exposes exactly two tools (``skill_search``, ``skill_read``) backed by
``skill_lookup`` so Claude CLI, the CCC Claude bridge and any other MCP
client resolve skills through the same fail-closed logic as the JSON CLI.

Framing is the MCP stdio transport: one JSON-RPC 2.0 message per line on
stdin, responses on stdout, diagnostics on stderr.  The implementation is
stdlib-only and runs under any python3 — no bridge virtualenv required:

    python3 <repo>/bridge/core/family_skills_server.py

Access policy is enforced per ``tools/call`` from this process environment
(``CCC_NODE_ISOLATION_PROFILE`` / ``CCC_MEMORY_AUDIENCE``), never from tool
arguments, so a user-scope registration or a direct connection cannot bypass
the node's isolation profile (#1678).  Diagnostics never contain queries,
skill bodies or other content — only tool names, ids and counts.
"""

from __future__ import annotations

import json
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
    from skill_lookup import (  # noqa: E402
        MAX_LIMIT,
        MAX_QUERY_CHARS,
        RUNTIMES,
        SkillLookupError,
        policy_denial,
        read,
        search,
    )

SERVER_NAME = "family-skills"
SERVER_VERSION = "1.0.0"
_PROTOCOL_VERSION = "2025-06-18"
_SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", _PROTOCOL_VERSION)
_MAX_LINE_BYTES = 1_000_000

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


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, sort_keys=True)}],
        "isError": is_error,
    }


def _call_tool(name: str, arguments: Any) -> dict[str, Any]:
    denial = policy_denial()
    if denial is not None:
        return _tool_result(
            {"error": {"code": "policy_denied", "message": "skill lookup denied by node policy", "reason": denial}},
            is_error=True,
        )
    if name == "skill_search":
        if not isinstance(arguments, dict):
            raise SkillLookupError("invalid_query", "arguments must be an object")
        query = arguments.get("query")
        if not isinstance(query, str):
            raise SkillLookupError("invalid_query", "query must be a string")
        result = search(query, arguments.get("runtime"), arguments.get("limit"))
        _diag(f"call skill_search ok results={len(result['results'])}")
        return _tool_result(result)
    if name == "skill_read":
        if not isinstance(arguments, dict):
            raise SkillLookupError("invalid_skill_id", "arguments must be an object")
        skill_id = arguments.get("skill_id")
        if not isinstance(skill_id, str):
            raise SkillLookupError("invalid_skill_id", "skill_id must be a string")
        revision = arguments.get("revision")
        result = read(skill_id, revision if isinstance(revision, str) else None)
        _diag(f"call skill_read ok skill_id={result['skill_id']} bytes={result['bytes']}")
        return _tool_result(result)
    raise SkillLookupError("unknown_tool", f"unknown tool: {name}")


def _respond(message: dict[str, Any], response: dict[str, Any]) -> dict[str, Any] | None:
    message_id = message.get("id")
    if message_id is None or isinstance(message_id, (str, int)):
        response["id"] = message_id
        return response
    return None


def handle_message(message: Any) -> dict[str, Any] | None:
    """Handle one decoded JSON-RPC message; None means emit nothing."""

    message_id = message.get("id") if isinstance(message, dict) else None
    if message_id is not None and not isinstance(message_id, (str, int)):
        message_id = None
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {"code": -32600, "message": "not a JSON-RPC 2.0 message"},
        }
    method = message.get("method")
    if not isinstance(method, str):
        if message.get("id") is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "error": {"code": -32600, "message": "method must be a string"},
        }
    if method == "notifications/initialized" or method.startswith("notifications/"):
        return None
    if method == "initialize":
        requested = message.get("params", {}).get("protocolVersion")
        version = requested if requested in _SUPPORTED_PROTOCOLS else _PROTOCOL_VERSION
        return _respond(
            message,
            {
                "jsonrpc": "2.0",
                "result": {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            },
        )
    if method == "ping":
        return _respond(message, {"jsonrpc": "2.0", "result": {}})
    if method == "tools/list":
        return _respond(message, {"jsonrpc": "2.0", "result": {"tools": _TOOLS}})
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _respond(
                message,
                {
                    "jsonrpc": "2.0",
                    "error": {"code": -32602, "message": "params.name must be a string"},
                },
            )
        try:
            result = _call_tool(params["name"], params.get("arguments"))
        except SkillLookupError as error:
            result = _tool_result(error.payload(), is_error=True)
        return _respond(message, {"jsonrpc": "2.0", "result": result})
    return _respond(
        message,
        {"jsonrpc": "2.0", "error": {"code": -32601, "message": f"unknown method: {method}"}},
    )


def _diag(line: str) -> None:
    print(f"{SERVER_NAME}: {line}", file=sys.stderr, flush=True)


def serve(stdin: Any = None, stdout: Any = None) -> int:
    """Read/write loop; returns when stdin closes."""

    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout.buffer if stdout is None else stdout
    _diag("server ready")
    while True:
        line = stdin.readline()
        if not line:
            return 0
        if len(line) > _MAX_LINE_BYTES:
            _diag(f"rejected oversize frame bytes={len(line)}")
            _emit(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "frame exceeds the size bound"}})
            continue
        try:
            message = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            _diag("rejected undecodable frame")
            _emit(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "frame is not valid JSON"}})
            continue
        response = handle_message(message)
        if response is not None:
            _emit(stdout, response)


def _emit(stdout: Any, response: dict[str, Any]) -> None:
    stdout.write((json.dumps(response, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    stdout.flush()


def main() -> int:
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
