"""family-ops stdio MCP server tests (#1694): protocol roundtrip against a
real subprocess with fake collectors, call-time policy denial, and the
tool surface.
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


def _call(message_id: int, arguments: Any, name: str = "node_status") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def test_roundtrip_lists_single_tool_and_calls_it(ops_env) -> None:
    responses, _ = _run_server(
        ops_env,
        _initialize(),
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        _call(3, None),
    )
    assert responses[0]["result"]["serverInfo"]["name"] == "family-ops"
    assert [tool["name"] for tool in responses[1]["result"]["tools"]] == [
        "deployment_diff",
        "pr_readiness",
        "task_status",
        "node_status",
    ]
    payload = json.loads(responses[2]["result"]["content"][0]["text"])
    assert responses[2]["result"]["isError"] is False
    assert payload["status"] == "ok"
    assert payload["sections"]["source"]["head"] == "abc1234"
    assert payload["sections"]["scheduler"]["summary"]["total"] == 1
    assert payload["observed_at"].endswith("Z")


PR_VIEW_FIXTURE = json.dumps(
    {
        "number": 7,
        "title": "feat: fixture",
        "url": "https://github.com/example/repo/pull/7",
        "author": {"login": "someone"},
        "baseRefName": "main",
        "state": "OPEN",
        "isDraft": False,
        "headRefName": "feat/x",
        "headRefOid": "aa11bb33aa11bb33aa11bb33aa11bb33aa11bb33",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED",
        "reviewDecision": "REVIEW_REQUIRED",
        "reviewRequests": [{"login": "reviewer"}],
        "statusCheckRollup": [{"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"}],
        "reviews": [],
    }
)
THREADS_FIXTURE = json.dumps(
    {"data": {"repository": {"pullRequest": {"reviewThreads": {"totalCount": 0, "nodes": []}}}}}
)


def test_task_status_tool_roundtrip(tmp_path: Path, ops_env) -> None:
    """task_status returns the checkpoint/resume notes and wait promises."""

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "working-state.md").write_text(
        "## Working State\n\n- 목표: fixture checkpoint", encoding="utf-8"
    )
    env = {
        **ops_env,
        "CCC_STATE_DIR": str(state_dir),
        "CCC_TASK_STATUS_WAITS_CMD": "echo "
            + json.dumps(
                {"ok": True, "waits": [{"wait_id": "w1", "state": "pending", "repo": "r", "pr": 1, "head_sha": "a", "summary": "watch"}]}
            ),
    }
    responses, stderr = _run_server(env, _call(9, None, name="task_status"))
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert responses[0]["result"]["isError"] is False
    assert payload["status"] == "ok"
    sections = payload["sections"]
    assert sections["working_state"]["present"] is True
    assert "fixture checkpoint" in sections["working_state"]["content"]
    assert sections["waits"]["active"][0]["wait_id"] == "w1"
    assert "family-ops: call task_status ok" in stderr


def test_pr_readiness_tool_roundtrip(tmp_path: Path, ops_env) -> None:
    """pr_readiness aggregates the fake gh fixtures into one snapshot."""

    fake_gh = tmp_path / "fake-gh.sh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        "if printf '%s ' \"$@\" | grep -q graphql; then\n"
        f"  echo '{THREADS_FIXTURE}'\n"
        "else\n"
        f"  echo '{PR_VIEW_FIXTURE}'\n"
        "fi\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = {**ops_env, "CCC_PR_READINESS_GH": f"bash {fake_gh}"}
    responses, stderr = _run_server(
        env,
        _call(11, {"repo": "example/repo", "pr": 7}, name="pr_readiness"),
        _call(12, {"pr": 7}, name="pr_readiness"),
    )
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert responses[0]["result"]["isError"] is False
    assert payload["status"] == "ok"
    assert payload["repo"] == "example/repo"
    assert payload["pr"] == 7
    assert payload["sections"]["ci"]["verdict"] == "ok"
    assert "review_decision:REVIEW_REQUIRED" in payload["readiness"]["reasons"]
    assert "no_non_author_head_matched_approval" in payload["readiness"]["reasons"]
    assert payload["readiness"]["informational_only"] is True
    assert "family-ops: call pr_readiness ok repo=example/repo pr=7" in stderr
    invalid = json.loads(responses[1]["result"]["content"][0]["text"])
    assert responses[1]["result"]["isError"] is True
    assert invalid["error"]["code"] == "invalid_repo"


def test_deployment_diff_tool_roundtrip(tmp_path: Path, ops_env) -> None:
    """deployment_diff aggregates the fake locate/git/doctor sources."""

    bin_dir = tmp_path / "deploy-collectors"
    bin_dir.mkdir()
    claude = tmp_path / "claude-home"
    (claude / "state").mkdir(parents=True)
    (claude / "state" / "self-update.installed-sha").write_text("ee" * 20 + "\n", encoding="utf-8")
    locate = bin_dir / "locate.sh"
    locate.write_text(
        "#!/bin/sh\necho '{\"running\": true, \"bridges\": [{\"pid\": 7, \"checkout\": \"/opt/ccc-node\", \"head\": \"aa11bb33aa11bb33aa11bb33aa11bb33aa11bb33\", \"branch\": \"main\", \"dirty\": 0}]}'\n",
        encoding="utf-8",
    )
    doctor = bin_dir / "doctor.sh"
    doctor.write_text(
        "#!/bin/sh\necho '{\"rows\": [{\"item\": \"hooks/foo.sh\", \"status\": \"drifted\"}]}'\n",
        encoding="utf-8",
    )
    gitfake = bin_dir / "git.sh"
    gitfake.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '  "ls-remote origin") printf \'%s\\trefs/heads/main\\n\' "bb22cc44bb22cc44bb22cc44bb22cc44bb22cc44";;\n'
        '  "rev-parse HEAD") echo "aa11bb33aa11bb33aa11bb33aa11bb33aa11bb33";;\n'
        '  "fetch origin"*) exit 0;;\n'
        '  "rev-list"*) echo 1;;\n'
        '  "status --porcelain") :;;\n'
        '  *) exit 0;;\n'
        "esac\n",
        encoding="utf-8",
    )
    for script in (locate, doctor, gitfake):
        script.chmod(0o755)
    env = {
        **ops_env,
        "CCC_CLAUDE_DIR": str(claude),
        "CCC_DEPLOYMENT_DIFF_LOCATE": f"bash {locate}",
        "CCC_DEPLOYMENT_DIFF_DOCTOR": f"bash {doctor}",
        "CCC_DEPLOYMENT_DIFF_GIT": f"bash {gitfake}",
        "CCC_DEPLOYMENT_DIFF_MARKER_FILE": str(claude / "state" / "self-update.installed-sha"),
    }
    responses, stderr = _run_server(env, _call(21, None, name="deployment_diff"))
    payload = json.loads(responses[0]["result"]["content"][0]["text"])
    assert responses[0]["result"]["isError"] is False
    assert payload["status"] == "ok"
    sections = payload["sections"]
    assert sections["target"]["sha"].startswith("bb22cc44")
    assert sections["history"]["behind"] == 1
    assert sections["installed"]["doctor"]["drift_total"] == 1
    assert "installed_drift:1" in payload["deployment"]["reasons"]
    assert "checkout_behind:1" in payload["deployment"]["reasons"]
    assert payload["deployment"]["informational_only"] is True
    assert "family-ops: call deployment_diff ok" in stderr


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
