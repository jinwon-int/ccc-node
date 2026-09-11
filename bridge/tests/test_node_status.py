"""node_status collector tests (#1694): one call aggregating the existing
read-only sources, per-section unknown on failure, body-free parsing, ssh
aggregation scoping, and the node policy gate.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from telegram_bot.core.node_status import (
    NodeStatusError,
    _parse_bridge_status,
    collect_local,
    collect_remote,
    node_status,
)

LOCATE_JSON = json.dumps(
    {
        "running": True,
        "multi": False,
        "bridges": [
            {"pid": 123, "checkout": "/opt/x", "projectPath": "/root", "head": "abc1234", "branch": "main", "dirty": 0}
        ],
        "restartCmd": "/opt/x/bridge/start.sh --path /root --restart -d",
    }
)

BRIDGE_STATUS_TEXT = """🤖 Claude Telegram Bot Bridge
================================
📂 Project path: /root
🟢 Bot status: available
   Process: alive (PID: 123)
   Service: available
   Turn occupancy: occupied (1 active turn; oldest started at 2026-09-12T00:00:00Z)
   Dead-session wakeup: disabled
   Telegram: healthy
   Piri: healthy
"""

SCHEDULER_JSON = json.dumps(
    {
        "ok": True,
        "mode": "status-read-only",
        "at": "2026-09-12T00:00:00Z",
        "summary": {"total": 2, "healthy": 1, "due": 1, "failed": 0, "locked": 0},
        "tasks": [],
        "errors": [],
    }
)

FLEET_ENV = {"CCC_NODE_ISOLATION_PROFILE": "fleet"}


def _completed(cmd: list[str], stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=cmd, returncode=returncode, stdout=stdout.encode(), stderr=stderr.encode()
    )


def _fake_runner(outputs: dict[str, Any]) -> Any:
    """Dispatch on the collector script name embedded in the command."""

    def runner(cmd: list[str], timeout: float):
        joined = " ".join(cmd)
        for needle, value in outputs.items():
            if needle in joined:
                if isinstance(value, Exception):
                    raise value
                if callable(value):
                    return value(cmd, timeout)
                return _completed(cmd, stdout=value)
        raise AssertionError(f"unexpected collector command: {joined}")

    return runner


def test_collect_local_aggregates_all_sections() -> None:
    runner = _fake_runner(
        {"ccc-bridge-locate": LOCATE_JSON, "--status": BRIDGE_STATUS_TEXT, "agent-cron": SCHEDULER_JSON}
    )
    result = collect_local(env=FLEET_ENV, runner=runner)
    assert result["status"] == "ok"
    assert result["observed_at"].endswith("Z")
    sections = result["sections"]
    assert sections["source"]["head"] == "abc1234"
    assert sections["source"]["dirty"] == 0
    assert sections["bridge_process"]["running"] is True
    assert sections["bridge_process"]["pid"] == 123
    assert sections["service"]["healthy"] is True
    assert sections["service"]["telegram"] == "healthy"
    assert sections["service"]["turn_occupancy"].startswith("occupied")
    assert sections["scheduler"]["summary"]["due"] == 1
    assert all(section["status"] == "ok" for name, section in sections.items() if name != "model")
    # No structured model source yet (#1694): unknown, never guessed.
    assert sections["model"] == {"status": "unknown", "reason": "no structured source"}


def test_collect_local_marks_partial_failures_unknown() -> None:
    def scheduler_fail(cmd: list[str], timeout: float):
        return _completed(cmd, returncode=3, stderr="boom")

    runner = _fake_runner(
        {
            "ccc-bridge-locate": subprocess.TimeoutExpired(cmd=["x"], timeout=10),
            "--status": BRIDGE_STATUS_TEXT,
            "agent-cron": scheduler_fail,
        }
    )
    result = collect_local(env=FLEET_ENV, runner=runner)
    assert result["status"] == "unknown"
    assert result["sections"]["source"]["status"] == "unknown"
    assert result["sections"]["source"]["error"] == "timeout"
    assert result["sections"]["source"]["head"] == "unknown"
    assert result["sections"]["service"]["healthy"] is True
    assert result["sections"]["scheduler"]["status"] == "unknown"
    assert result["sections"]["scheduler"]["error"] == "exit 3"


def test_collect_local_survives_unparseable_scheduler_output() -> None:
    runner = _fake_runner(
        {"ccc-bridge-locate": LOCATE_JSON, "--status": BRIDGE_STATUS_TEXT, "agent-cron": "not json"}
    )
    result = collect_local(env=FLEET_ENV, runner=runner)
    assert result["sections"]["scheduler"]["status"] == "unknown"
    assert result["status"] == "unknown"


def test_bridge_status_parser_handles_degraded_node() -> None:
    parsed = _parse_bridge_status("Bot status: degraded\n   Service: unavailable\n   Telegram: healthy\n")
    assert parsed["healthy"] is False
    assert parsed["bot_status"] == "degraded"
    assert parsed["piri"] == "unknown"  # absent line -> unknown, never invented


def test_policy_denied() -> None:
    with pytest.raises(NodeStatusError) as exc:
        node_status(env={"CCC_NODE_ISOLATION_PROFILE": "external"})
    assert exc.value.code == "policy_denied"


def test_remote_collection_wraps_and_scopes_failures() -> None:
    ok_runner = _fake_runner({"ssh": json.dumps({"status": "ok", "observed_at": "2026-09-12T00:00:00Z"})})
    result = collect_remote("peer-a", env=FLEET_ENV, runner=ok_runner)
    assert result["node"] == "peer-a"
    assert result["status"] == "ok"
    assert result["result"]["status"] == "ok"

    fail_runner = _fake_runner({"ssh": (lambda cmd, timeout: _completed(cmd, returncode=255, stderr="connection refused"))})
    result = collect_remote("peer-b", env=FLEET_ENV, runner=fail_runner)
    assert result["status"] == "unknown"
    assert result["error"] == "exit 255"
    assert "connection refused" in result["stderr_tail"]

    with pytest.raises(NodeStatusError) as exc:
        collect_remote("peer x; rm -rf /", env=FLEET_ENV)
    assert exc.value.code == "invalid_node"


def test_aggregate_over_nodes_reports_unknown_for_partial() -> None:
    def runner(cmd: list[str], timeout: float):
        joined = " ".join(cmd)
        if "peer-ok" in joined:
            return _completed(cmd, json.dumps({"status": "ok"}))
        return _completed(cmd, returncode=255, stderr="down")

    result = node_status(["peer-ok", "peer-down"], env=FLEET_ENV, runner=runner)
    assert result["status"] == "unknown"
    by_node = {item["node"]: item for item in result["nodes"]}
    assert by_node["peer-ok"]["status"] == "ok"
    assert by_node["peer-down"]["status"] == "unknown"
