"""family-skills stdio MCP server tests (#1678): protocol roundtrip against a
real subprocess (initialize → tools/list → tools/call), call-time policy
denial, framing/error bounds, and no content leakage into diagnostics.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from telegram_bot.core.family_skills_server import handle_message

SERVER = Path(__file__).resolve().parents[1] / "core" / "family_skills_server.py"


@pytest.fixture
def server_env(lookup_env: dict[str, str]) -> dict[str, str]:
    """Fully explicit child env — no ambient variable reaches the server."""

    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/test-user",
        **lookup_env,
    }


def _run_server(env: dict[str, str], *frames: Any) -> tuple[list[dict], str]:
    """Run one server process over the given frames; dict frames are JSON
    encoded, str frames are sent verbatim. Returns (responses, stderr)."""

    encoded = b""
    for frame in frames:
        line = frame if isinstance(frame, str) else json.dumps(frame)
        encoded += line.encode() + b"\n"
    process = subprocess.Popen(
        [sys.executable, str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    stdout, stderr = process.communicate(encoded, timeout=30)
    responses = [json.loads(line) for line in stdout.decode().splitlines() if line]
    return responses, stderr.decode()


def _initialize() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }


def _call(message_id: int, name: str, arguments: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def test_mcp_roundtrip_initialize_tools_list(server_env) -> None:
    responses, _ = _run_server(
        server_env,
        _initialize(),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert responses[0]["result"]["serverInfo"]["name"] == "family-skills"
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    # The notification produced no response; tools/list is the second message.
    assert [tool["name"] for tool in responses[1]["result"]["tools"]] == [
        "skill_search",
        "skill_read",
    ]


def test_tools_call_search_and_read(server_env) -> None:
    responses, _ = _run_server(
        server_env,
        _initialize(),
        _call(2, "skill_search", {"query": "fixture", "runtime": "repo", "limit": 1}),
        _call(3, "skill_read", {"skill_id": "claude:local-one"}),
    )
    search = json.loads(responses[1]["result"]["content"][0]["text"])
    assert search["results"][0]["skill_id"].startswith("repo:")
    assert responses[1]["result"]["isError"] is False
    read = json.loads(responses[2]["result"]["content"][0]["text"])
    assert read["skill_id"] == "claude:local-one"
    assert "Fixture" in read["body"]


def test_call_time_policy_denial_not_protocol_error(server_env) -> None:
    responses, _ = _run_server(
        {**server_env, "CCC_NODE_ISOLATION_PROFILE": "external"},
        _initialize(),
        _call(2, "skill_search", {"query": "fixture"}),
    )
    assert responses[0]["result"]["serverInfo"]["name"] == "family-skills"
    payload = json.loads(responses[1]["result"]["content"][0]["text"])
    assert responses[1]["result"]["isError"] is True
    assert payload["error"]["code"] == "policy_denied"


def test_framing_and_protocol_errors(server_env) -> None:
    responses, _ = _run_server(
        server_env,
        {"jsonrpc": "1.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "no/such-method"},
        _call(3, "no_such_tool", {}),
        "not-json-at-all",
        "b" * 1_000_001,
    )
    assert responses[0]["id"] == 1
    assert responses[0]["error"]["code"] == -32600
    assert responses[1]["error"]["code"] == -32601
    tool_error = json.loads(responses[2]["result"]["content"][0]["text"])
    assert responses[2]["result"]["isError"] is True
    assert tool_error["error"]["code"] == "unknown_tool"
    assert responses[3]["error"]["code"] == -32700
    assert responses[4]["error"]["code"] == -32600  # oversize frame


def test_diagnostics_never_contain_skill_content(server_env) -> None:
    _responses, stderr = _run_server(
        server_env,
        _call(2, "skill_read", {"skill_id": "claude:local-one"}),
    )
    assert "family-skills: call skill_read ok" in stderr
    assert "Fixture" not in stderr


def test_handle_message_unit_edges() -> None:
    assert handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert handle_message({"jsonrpc": "2.0"}) is None  # unanswerable: no id
    assert handle_message({"jsonrpc": "2.0", "id": 5})["error"]["code"] == -32600
    pinged = handle_message({"jsonrpc": "2.0", "id": 6, "method": "ping"})
    assert pinged == {"jsonrpc": "2.0", "id": 6, "result": {}}
    negotiated = handle_message(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "initialize",
            "params": {"protocolVersion": "1999-01-01"},
        }
    )
    assert negotiated["result"]["protocolVersion"] == "2025-06-18"
