"""task_status collector tests (#1694 item 2): bounded checkpoint/resume
reads, external-wait classification, partial-failure unknowns, and the node
policy gate.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from telegram_bot.core.task_status import TaskStatusError, collect

FLEET_ENV = {
    "CCC_NODE_ISOLATION_PROFILE": "fleet",
    "CCC_STATE_DIR": "",  # exercise the home fallback explicitly
}


def _completed(cmd: list[str], stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=cmd, returncode=returncode, stdout=stdout.encode(), stderr=stderr.encode()
    )


def _waits_json(*waits: dict) -> str:
    return json.dumps({"ok": True, "waits": list(waits)})


PENDING = {"wait_id": "w1", "repo": "jinwon-int/ccc-node", "pr": 1699, "head_sha": "abc", "state": "pending", "summary": "watch PR 1699"}
DROPPED = {"wait_id": "w2", "repo": "jinwon-int/x", "pr": 5, "head_sha": "def", "state": "success", "resumed": False, "skip_reason": "session_moved", "summary": "dropped promise"}
RESUMED_TERMINAL = {"wait_id": "w3", "repo": "jinwon-int/x", "pr": 6, "head_sha": "ghi", "state": "failure", "resumed": True, "skip_reason": None, "summary": "handled"}


def _write_notes(home: Path, working_state: str | None = "## Working State\n\n- 목표: 테스트", resume: str | None = "- resume note") -> None:
    state = home / ".claude" / "state"
    state.mkdir(parents=True, exist_ok=True)
    if working_state is not None:
        (state / "working-state.md").write_text(working_state, encoding="utf-8")
    if resume is not None:
        (state / "resume.md").write_text(resume, encoding="utf-8")


def test_collect_aggregates_notes_and_waits(tmp_path: Path) -> None:
    _write_notes(tmp_path)
    env = {**FLEET_ENV, "CCC_SKILL_LOOKUP_HOME": str(tmp_path)}
    runner_cmd_seen = []

    def runner(cmd: list[str], timeout: float):
        runner_cmd_seen.append(list(cmd))
        return _completed(cmd, _waits_json(PENDING, RESUMED_TERMINAL, DROPPED))

    result = collect(env=env, runner=runner)
    assert result["status"] == "ok"
    assert result["observed_at"].endswith("Z")
    sections = result["sections"]
    assert sections["working_state"]["present"] is True
    assert "목표: 테스트" in sections["working_state"]["content"]
    assert sections["working_state"]["truncated"] is False
    assert sections["resume"]["present"] is True
    waits = sections["waits"]
    assert waits["status"] == "ok"
    assert [w["wait_id"] for w in waits["active"]] == ["w1"]
    assert [w["wait_id"] for w in waits["dropped"]] == ["w2"]
    assert waits["dropped"][0]["skip_reason"] == "session_moved"
    assert waits["total"] == 3  # resumed terminal waits are counted but not listed
    assert runner_cmd_seen and "-m" in runner_cmd_seen[0]


def test_missing_notes_are_ok_not_unknown(tmp_path: Path) -> None:
    env = {**FLEET_ENV, "CCC_SKILL_LOOKUP_HOME": str(tmp_path)}
    result = collect(env=env, runner=lambda cmd, timeout: _completed(cmd, _waits_json()))
    sections = result["sections"]
    assert sections["working_state"] == {"status": "ok", "present": False}
    assert sections["resume"]["present"] is False
    assert result["status"] == "ok"


def test_checkpoint_content_is_bounded_and_flagged(tmp_path: Path) -> None:
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    big = "x" * 20_000
    (state / "working-state.md").write_text(big, encoding="utf-8")
    env = {**FLEET_ENV, "CCC_SKILL_LOOKUP_HOME": str(tmp_path)}
    result = collect(env=env, runner=lambda cmd, timeout: _completed(cmd, _waits_json()))
    section = result["sections"]["working_state"]
    assert section["truncated"] is True
    assert len(section["content"]) <= 8 * 1024
    assert section["size_bytes"] == 20_000


def test_wait_collector_failure_is_section_scoped_unknown(tmp_path: Path) -> None:
    _write_notes(tmp_path)
    env = {**FLEET_ENV, "CCC_SKILL_LOOKUP_HOME": str(tmp_path)}

    def runner(cmd: list[str], timeout: float):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    result = collect(env=env, runner=runner)
    assert result["sections"]["waits"]["status"] == "unknown"
    assert result["sections"]["waits"]["error"] == "timeout"
    assert result["sections"]["working_state"]["status"] == "ok"
    assert result["status"] == "unknown"


def test_waits_command_env_override(tmp_path: Path) -> None:
    env = {
        **FLEET_ENV,
        "CCC_SKILL_LOOKUP_HOME": str(tmp_path),
        "CCC_TASK_STATUS_WAITS_CMD": "echo " + _waits_json(DROPPED),
    }
    result = collect(env=env)
    waits = result["sections"]["waits"]
    assert waits["status"] == "ok"
    assert [w["wait_id"] for w in waits["dropped"]] == ["w2"]
    assert waits["active"] == []


def test_policy_denied() -> None:
    with pytest.raises(TaskStatusError) as exc:
        collect(env={"CCC_NODE_ISOLATION_PROFILE": "external"})
    assert exc.value.code == "policy_denied"
    with pytest.raises(TaskStatusError) as exc:
        collect(env={"CCC_MEMORY_AUDIENCE": "shared"})
    assert exc.value.code == "policy_denied"
