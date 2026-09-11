"""family-ops stdio MCP server tests (#1694): protocol roundtrip against a
real subprocess with fake collectors, call-time policy denial, and the
single-tool surface.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from telegram_bot.core.family_ops_server import handle_message

SERVER = Path(__file__).resolve().parents[1] / "core" / "family_ops_server.py"

LOCATE_JSON = json.dumps(
    {"running": True, "bridges": [{"pid": 7, "head": "abc1234", "branch": "main", "dirty": 0, "checkout": "/opt/x"}]}
)
STATUS_TEXT = "🟢 Bot status: available\n   Service: available\n   Telegram: healthy\n"
CRON_JSON = json.dumps({"ok": True, "at": "2026-09-12T00:00:00Z", "summary": {"total": 1, "due": 0}, "errors": []})


@pytest.fixture
def ops_env(tmp_path: Path) -> dict[str, str]:
    """Fake collector scripts + explicit child env (no ambient variables)."""

    bin_dir = tmp_path / "collectors"
    bin_dir.mkdir()
    for name, payload in [
        ("locate.sh", f"echo '{LOCATE_JSON}'"),
        ("bridge-status.sh", f"echo '{STATUS_TEXT}'"),
        ("agent-cron.sh", f"echo '{CRON_JSON}'"),
    ]:
        script = bin_dir / name
        script.write_text(f"#!/bin/sh\n{payload}\n", encoding="utf-8")
        script.chmod(0o755)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/test-user",
        "CCC_NODE_ISOLATION_PROFILE": "fleet",
        "CCC_NODE_STATUS_LOCATE": f"bash {bin_dir / 'locate.sh'}",
        "CCC_NODE_STATUS_BRIDGE_STATUS": f"bash {bin_dir / 'bridge-status.sh'}",
        "CCC_NODE_STATUS_AGENT_CRON": f"bash {bin_dir / 'agent-cron.sh'}",
    }


def _run_server(env: dict[str, str], *frames: Any) -> tuple[list[dict], str]:
    encoded = b""
    for frame in frames:
        encoded += (frame if isinstance(frame, str) else json.dumps(frame)).encode() + b"\n"
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
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
    }


def _call(message_id: int, arguments: Any) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "tools/call",
        "params": {"name": "node_status", "arguments": arguments},
    }


def test_roundtrip_lists_single_tool_and_calls_it(ops_env) -> None:
    responses, _ = _run_server(
        ops_env,
        _initialize(),
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        _call(3, None),
    )
    assert responses[0]["result"]["serverInfo"]["name"] == "family-ops"
    assert [tool["name"] for tool in responses[1]["result"]["tools"]] == ["node_status"]
    payload = json.loads(responses[2]["result"]["content"][0]["text"])
    assert responses[2]["result"]["isError"] is False
    assert payload["status"] == "ok"
    assert payload["sections"]["source"]["head"] == "abc1234"
    assert payload["sections"]["scheduler"]["summary"]["total"] == 1
    assert payload["observed_at"].endswith("Z")


def test_call_time_policy_denial(ops_env) -> None:
    responses, _ = _run_server(
        {**ops_env, "CCC_NODE_ISOLATION_PROFILE": "external"},
        _call(2, None),
    )
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert responses[0]["result"]["isError"] is True
    assert payload["error"]["code"] == "policy_denied"


def test_remote_node_argument_and_unknown_tool(ops_env, tmp_path: Path) -> None:
    """Remote calls wrap the peer result; no real ssh is ever spawned."""

    ssh_fake = tmp_path / "ssh-fake.sh"
    ssh_fake.write_text(
        "#!/bin/sh\necho '{\"status\": \"ok\", \"observed_at\": \"2026-09-12T00:00:00Z\"}'\n",
        encoding="utf-8",
    )
    ssh_fake.chmod(0o755)
    env = {**ops_env, "CCC_NODE_STATUS_SSH": f"bash {ssh_fake}"}
    responses, _ = _run_server(
        env,
        _call(2, {"node": "peer-a"}),
        _call(3, {"node": "bad alias"}),
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "no_such_tool", "arguments": {}}},
    )
    remote = json.loads(responses[0]["result"]["content"][0]["text"])
    assert responses[0]["result"]["isError"] is False
    assert remote["nodes"][0]["node"] == "peer-a"
    assert remote["nodes"][0]["status"] == "ok"
    invalid = json.loads(responses[1]["result"]["content"][0]["text"])
    assert responses[1]["result"]["isError"] is True
    assert invalid["error"]["code"] == "invalid_node"
    unknown = json.loads(responses[2]["result"]["content"][0]["text"])
    assert unknown["error"]["code"] == "unknown_tool"


def test_unit_message_edges() -> None:
    assert handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert handle_message({"jsonrpc": "2.0", "id": 1, "method": "ping"})["result"] == {}
    assert handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/x"})["error"]["code"] == -32601
