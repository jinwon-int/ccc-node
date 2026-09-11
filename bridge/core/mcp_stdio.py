"""Shared JSON-RPC 2.0 stdio scaffolding for the family MCP servers.

Implements the MCP stdio transport once (newline-delimited JSON-RPC 2.0,
``initialize`` / ``tools/list`` / ``tools/call`` / ``ping``, frame-size and
decode error handling) so each family server only declares its tools and a
dispatch callback (#1678, #1694).  Domain failures are ``isError: true`` tool
results, never transport errors; diagnostics go to stderr and must stay
body-free.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

_PROTOCOL_VERSION = "2025-06-18"
_SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", _PROTOCOL_VERSION)
_MAX_LINE_BYTES = 1_000_000


class ToolError(ValueError):
    """A structured tool failure surfaced as an isError result."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details

    def payload(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": str(self), **self.details}}


def tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, sort_keys=True)}
        ],
        "isError": is_error,
    }


def handle_message(
    message: Any,
    *,
    server_name: str,
    server_version: str,
    tools: list[dict[str, Any]],
    dispatch: Callable[[str, Any], dict[str, Any]],
) -> dict[str, Any] | None:
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
        if message_id is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {"code": -32600, "message": "method must be a string"},
        }
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        requested = message.get("params", {}).get("protocolVersion")
        version = requested if requested in _SUPPORTED_PROTOCOLS else _PROTOCOL_VERSION
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": server_name, "version": server_version},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": message_id, "result": {"tools": tools}}
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32602, "message": "params.name must be a string"},
            }
        try:
            result = dispatch(params["name"], params.get("arguments"))
        except ToolError as error:
            result = tool_result(error.payload(), is_error=True)
        return {"jsonrpc": "2.0", "id": message_id, "result": result}
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {"code": -32601, "message": f"unknown method: {method}"},
    }


def run_tools_server(
    *,
    server_name: str,
    server_version: str,
    tools: list[dict[str, Any]],
    dispatch: Callable[[str, Any], dict[str, Any]],
    diag: Callable[[str], None] | None = None,
    stdin: Any = None,
    stdout: Any = None,
) -> int:
    """Read/write loop; returns when stdin closes.

    ``dispatch(name, arguments)`` returns the tool result payload and may
    raise :class:`ToolError` for structured failures.  It is called only for
    ``tools/call``; the caller enforces its call-time policy inside it.
    """

    def _diag(line: str) -> None:
        # The caller's diag callback owns the prefix/format; keep it body-free.
        if diag is not None:
            diag(line)

    def _emit(out: Any, response: dict[str, Any]) -> None:
        out.write((json.dumps(response, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        out.flush()

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
        response = handle_message(
            message,
            server_name=server_name,
            server_version=server_version,
            tools=tools,
            dispatch=dispatch,
        )
        if response is not None:
            _emit(stdout, response)

