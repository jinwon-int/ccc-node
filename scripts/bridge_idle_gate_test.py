"""bridge-idle-gate.py verdicts against a synthetic /proc (#2567)."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "bridge_idle_gate", Path(__file__).with_name("bridge-idle-gate.py")
)
assert _SPEC and _SPEC.loader
gate = importlib.util.module_from_spec(_SPEC)
sys.modules["bridge_idle_gate"] = gate
_SPEC.loader.exec_module(gate)

HZ = os.sysconf("SC_CLK_TCK")
NOW = 1_800_000_000.0
MAIN = 100


class FakeProc:
    def __init__(self, root: Path, uptime: float = 10_000.0) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root / "uptime").write_text(f"{uptime} 0.0\n")

    def add(self, pid: int, ppid: int, argv: list[str], *, started: float = 9_000.0) -> None:
        d = self.root / str(pid)
        d.mkdir()
        comm = os.path.basename(argv[0])[:15]
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(int(started * HZ))] + ["0"] * 5
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (d / "comm").write_text(comm + "\n")


def _health(path: Path, **overrides: object) -> Path:
    data: dict[str, object] = {
        "updated_at": datetime.fromtimestamp(NOW - 5, timezone.utc).isoformat(),
        "process": {"pid": MAIN},
        "workload": {"active_requests": 0},
        "requests": {"delegated_tasks_active": 0},
    }
    data.update(overrides)
    path.write_text(json.dumps(data))
    return path


def _bridge(proc: FakeProc) -> None:
    proc.add(MAIN, 1, ["/opt/ccc-node/bridge/venv/bin/python", "-m", "telegram_bot"])
    proc.add(200, MAIN, ["/usr/bin/claude", "--output-format", "stream-json"])
    # MCP servers: direct children of the CLI, no shell-snapshot wrapper.
    proc.add(201, 200, ["/usr/bin/python3", "/opt/ccc-node/bridge/core/family_ops_server.py"])
    proc.add(202, 200, ["node", "/usr/lib/node_modules/mcp-searxng/dist/cli.js"])
    proc.add(203, 200, ["ssh", "-o", "BatchMode=yes", "--", "host", "browser-mcp"])


def test_resident_session_with_mcp_servers_only_is_idle(tmp_path: Path) -> None:
    proc = FakeProc(tmp_path / "proc")
    _bridge(proc)
    report = gate.evaluate(proc.root, MAIN, _health(tmp_path / "h.json"), now=NOW)
    assert report["verdict"] == "idle" and report["tool_commands"] == []


def test_background_tool_command_after_the_turn_is_busy(tmp_path: Path) -> None:
    proc = FakeProc(tmp_path / "proc")
    _bridge(proc)
    snapshot = "source /root/.claude/shell-snapshots/snapshot-bash-1.sh && eval 'secret-cmd'"
    proc.add(300, 200, ["/usr/bin/bash", "-c", snapshot], started=9_500.0)
    proc.add(301, 300, ["bash", "/root/.claude/skills/gh-pr-flow/merge-queue-watch.sh"])
    proc.add(302, 301, ["sleep", "45"])
    report = gate.evaluate(proc.root, MAIN, _health(tmp_path / "h.json"), now=NOW)
    assert report["verdict"] == "busy" and report["reasons"] == ["tool-commands"]
    [command] = report["tool_commands"]  # nested shells fold into the outermost one
    assert command["pid"] == 300 and command["age_s"] == 500.0
    assert command["processes"] == ["bash", "sleep"]
    assert "secret-cmd" not in json.dumps(report)  # body-free: never argv


def test_other_provider_shell_under_its_cli_counts(tmp_path: Path) -> None:
    proc = FakeProc(tmp_path / "proc")
    proc.add(MAIN, 1, ["python", "-m", "telegram_bot"])
    proc.add(200, MAIN, ["/usr/local/bin/codex", "app-server"])
    proc.add(300, 200, ["/bin/bash", "-lc", "pytest -q"])
    report = gate.evaluate(proc.root, MAIN, _health(tmp_path / "h.json"), now=NOW)
    assert report["verdict"] == "busy" and report["tool_commands"][0]["pid"] == 300


def test_shell_outside_the_bridge_tree_is_ignored(tmp_path: Path) -> None:
    proc = FakeProc(tmp_path / "proc")
    _bridge(proc)
    proc.add(900, 1, ["/usr/bin/claude"])
    proc.add(901, 900, ["/usr/bin/bash", "-c", "source /x/shell-snapshots/snapshot-1"])
    report = gate.evaluate(proc.root, MAIN, _health(tmp_path / "h.json"), now=NOW)
    assert report["verdict"] == "idle"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"workload": {"active_requests": 2}}, "active-requests"),
        ({"requests": {"delegated_tasks_active": 1}}, "delegated-tasks"),
    ],
)
def test_health_counters_make_it_busy(tmp_path: Path, overrides: dict, reason: str) -> None:
    proc = FakeProc(tmp_path / "proc")
    _bridge(proc)
    report = gate.evaluate(proc.root, MAIN, _health(tmp_path / "h.json", **overrides), now=NOW)
    assert report["verdict"] == "busy" and report["reasons"] == [reason]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"updated_at": datetime.fromtimestamp(NOW - 900, timezone.utc).isoformat()}, "health-stale"),
        ({"updated_at": "not-a-time"}, "health-stale"),
        ({"process": {"pid": 999}}, "health-pid-mismatch"),
    ],
)
def test_untrustworthy_health_is_undecidable(tmp_path: Path, overrides: dict, reason: str) -> None:
    proc = FakeProc(tmp_path / "proc")
    _bridge(proc)
    report = gate.evaluate(proc.root, MAIN, _health(tmp_path / "h.json", **overrides), now=NOW)
    assert report["verdict"] == "unknown" and report["reasons"] == [reason]


def test_missing_process_or_health_is_undecidable(tmp_path: Path) -> None:
    proc = FakeProc(tmp_path / "proc")
    assert gate.evaluate(proc.root, MAIN, tmp_path / "h.json", now=NOW)["reasons"] == [
        "main-pid-not-running"
    ]
    _bridge(proc)
    assert gate.evaluate(proc.root, MAIN, tmp_path / "missing.json", now=NOW)["reasons"] == [
        "health-unreadable"
    ]


def test_cli_exit_codes_and_environ_health_lookup(tmp_path: Path, capsys) -> None:
    proc = FakeProc(tmp_path / "proc")
    _bridge(proc)
    data = tmp_path / "data"
    data.mkdir()
    _health(data / "health.json", updated_at=datetime.now(timezone.utc).isoformat())
    (proc.root / str(MAIN) / "environ").write_bytes(f"BOT_DATA_DIR={data}\0".encode())
    assert gate.main(["--main-pid", str(MAIN), "--proc", str(proc.root)]) == 0
    assert capsys.readouterr().out.startswith("idle:")
    proc.add(300, 200, ["bash", "-c", "source /r/.claude/shell-snapshots/snapshot-2"])
    assert gate.main(["--main-pid", str(MAIN), "--proc", str(proc.root), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "busy"
    assert gate.main(["--main-pid", "4242", "--proc", str(proc.root)]) == 2
