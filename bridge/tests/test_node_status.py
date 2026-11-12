"""node_status collector tests (#1694): one call aggregating the existing
read-only sources, per-section unknown on failure, body-free parsing, ssh
aggregation scoping, and the node policy gate.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
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


# -- frontend list (#2342) on a Telegram-retired node (#2771) ----------------

RETIRED_STATUS_TEXT = """🔴 Bot status: unavailable
   Process: dead (no PID file)
   Service: unavailable (process not running)
   Telegram: unavailable (process not running)
"""

REVIVED_STATUS_TEXT = BRIDGE_STATUS_TEXT


def _write_health(home: Path, name: str, *, service: str = "available", age: int = 5) -> Path:
    directory = home / f".ccc-{name}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "health.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "service": {"state": service, "reason": "body text must not leak"},
                "claude": {"state": "healthy", "last_error": "secret-ish body"},
                "agent": {"state": "healthy"},
                "process": {"pid": 4242, "mode": "daemon"},
            }
        ),
        encoding="utf-8",
    )
    stamp = path.stat().st_mtime - age
    os.utime(path, (stamp, stamp))
    return path


def _retired_collect(tmp_path: Path, status_text: str, frontends: str = "hande\n") -> dict[str, Any]:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(exist_ok=True)
    (claude_dir / "frontends").write_text(frontends, encoding="utf-8")
    runner = _fake_runner(
        {"ccc-bridge-locate": LOCATE_JSON, "--status": status_text, "agent-cron": SCHEDULER_JSON}
    )
    env = {**FLEET_ENV, "HOME": str(tmp_path)}
    return collect_local(env=env, runner=runner, project_path=str(tmp_path))


def test_retired_telegram_node_is_healthy_from_declared_frontend(tmp_path: Path) -> None:
    _write_health(tmp_path, "hande")
    frontends = "# telegram retired 2026-10-16\n# matrix  # retired\nhande\n"
    service = _retired_collect(tmp_path, RETIRED_STATUS_TEXT, frontends)["sections"]["service"]
    assert service["healthy"] is True
    assert service["telegram"] == "retired (frontend list)"
    assert "telegram_drift" not in service
    assert set(service["frontends"]) == {"hande"}  # commented-out matrix is not declared
    block = service["frontends"]["hande"]
    assert block == {
        "status": "ok",
        "service": "available",
        "claude": "healthy",
        "agent": "healthy",
        "pid": 4242,
        "age_seconds": block["age_seconds"],
        "stale": False,
        "healthy": True,
    }
    # Body-free: free-text reason/last_error fields never reach the output.
    assert "body text" not in json.dumps(service)
    assert "secret-ish" not in json.dumps(service)


def test_retired_telegram_revived_bridge_is_drift(tmp_path: Path) -> None:
    _write_health(tmp_path, "hande")
    service = _retired_collect(tmp_path, REVIVED_STATUS_TEXT)["sections"]["service"]
    assert service["telegram"] == "retired (frontend list)"
    assert service["telegram_drift"] == "retired but running"


def test_retired_telegram_stale_or_down_frontend_is_unhealthy(tmp_path: Path) -> None:
    _write_health(tmp_path, "hande", age=301)
    service = _retired_collect(tmp_path, RETIRED_STATUS_TEXT)["sections"]["service"]
    assert service["frontends"]["hande"]["stale"] is True
    assert service["healthy"] is False

    _write_health(tmp_path, "hande", service="unavailable")
    service = _retired_collect(tmp_path, RETIRED_STATUS_TEXT)["sections"]["service"]
    assert service["healthy"] is False


def test_retired_telegram_missing_or_garbled_health_is_unknown(tmp_path: Path) -> None:
    service = _retired_collect(tmp_path, RETIRED_STATUS_TEXT)["sections"]["service"]
    assert service["frontends"]["hande"] == {"status": "unknown", "error": "health.json missing"}
    assert service["healthy"] == "unknown"

    (tmp_path / ".ccc-hande").mkdir(exist_ok=True)
    (tmp_path / ".ccc-hande" / "health.json").write_text("{not json", encoding="utf-8")
    service = _retired_collect(tmp_path, RETIRED_STATUS_TEXT)["sections"]["service"]
    assert service["frontends"]["hande"]["error"] == "unparseable health.json"
    assert service["healthy"] == "unknown"

    # An explicitly unhealthy frontend wins over an unknown one.
    _write_health(tmp_path, "matrix", service="unavailable")
    service = _retired_collect(tmp_path, RETIRED_STATUS_TEXT, "hande matrix\n")["sections"]["service"]
    assert service["healthy"] is False


def test_ccc_frontends_env_wins_and_undeclared_keeps_legacy(tmp_path: Path) -> None:
    _write_health(tmp_path, "matrix")
    runner = _fake_runner(
        {"ccc-bridge-locate": LOCATE_JSON, "--status": RETIRED_STATUS_TEXT, "agent-cron": SCHEDULER_JSON}
    )
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "frontends").write_text("telegram\n", encoding="utf-8")
    env = {**FLEET_ENV, "HOME": str(tmp_path), "CCC_FRONTENDS": "matrix"}
    service = collect_local(env=env, runner=runner, project_path=str(tmp_path))["sections"]["service"]
    assert set(service["frontends"]) == {"matrix"}
    assert service["healthy"] is True

    # Telegram still declared -> legacy start.sh verdict, untouched.
    env = {**FLEET_ENV, "HOME": str(tmp_path)}
    service = collect_local(env=env, runner=runner, project_path=str(tmp_path))["sections"]["service"]
    assert "frontends" not in service
    assert service["telegram"] == "unavailable (process not running)"
    assert service["healthy"] is False

    # No declaration at all -> legacy.
    (tmp_path / ".claude" / "frontends").unlink()
    service = collect_local(env=env, runner=runner, project_path=str(tmp_path))["sections"]["service"]
    assert "frontends" not in service and service["healthy"] is False


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


def test_remote_command_has_no_cli_flags_and_probes_fleet_checkouts() -> None:
    """The peer's CLI takes only --node; the old `--local` made every remote
    call exit 2, and one fixed /opt path never fit Termux peers (#1990)."""
    from telegram_bot.core.node_status import remote_command

    ok_runner = _fake_runner({"ssh": json.dumps({"status": "ok"})})
    calls: list[list[str]] = []

    def runner(cmd: list[str], timeout: float):
        calls.append(cmd)
        return ok_runner(cmd, timeout)

    collect_remote("peer-a", env=FLEET_ENV, runner=runner)
    remote = calls[0][-1]
    assert "--local" not in remote
    assert remote == remote_command(None)
    for root in ("/opt/ccc-node", "/root/ccc-node"):
        assert f"{root}/scripts/ccc-node-status.py" in remote
    assert '"$HOME"/ccc-node/scripts/ccc-node-status.py' in remote

    override = {**FLEET_ENV, "CCC_NODE_STATUS_REMOTE_PATH": "/srv/x y/scripts/ccc-node-status.py"}
    calls.clear()
    collect_remote("peer-a", env=override, runner=runner)
    assert calls[0][-1].startswith("for s in '/srv/x y/scripts/ccc-node-status.py' ")


def test_remote_command_execs_the_first_checkout_found(tmp_path: Path, monkeypatch) -> None:
    """Run the snippet in a real shell: a Termux-style $HOME checkout is found
    and executed; nothing found reports where it looked and exits 127."""
    import shutil
    from telegram_bot.core.node_status import remote_command

    # Fleet hosts actually have /opt/ccc-node. Isolate the candidate filesystem
    # while exercising the real generated shell, including the missing case.
    candidates = (str(tmp_path / "opt/ccc-node"), str(tmp_path / "root/ccc-node"), "$HOME/ccc-node")
    monkeypatch.setattr("telegram_bot.core.node_status.REMOTE_CHECKOUT_CANDIDATES", candidates)

    home = tmp_path / "home"
    script = home / "ccc-node" / "scripts" / "ccc-node-status.py"
    script.parent.mkdir(parents=True)
    script.write_text("import json, sys; print(json.dumps({'status': 'ok', 'argv': sys.argv[1:]}))\n")
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    sh = shutil.which("sh")
    assert sh, "a POSIX shell is required"

    found = subprocess.run([sh, "-c", remote_command(None)], env=env, capture_output=True, text=True, timeout=30)
    assert found.returncode == 0, found.stderr
    assert json.loads(found.stdout) == {"status": "ok", "argv": []}

    empty = subprocess.run([sh, "-c", remote_command(None)], env={**env, "HOME": str(tmp_path / "nowhere")}, capture_output=True, text=True, timeout=30)
    assert empty.returncode == 127
    assert "not found under " + ", ".join(candidates) in empty.stderr
