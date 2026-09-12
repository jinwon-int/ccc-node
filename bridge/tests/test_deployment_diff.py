"""deployment_diff collector tests (#1694 item 4): source reuse (locate,
doctor, installed marker, backups), git history with bounded fetch, dep-file
diffs, section-scoped unknowns, and the node policy gate.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from telegram_bot.core.deployment_diff import DeploymentDiffError, collect

FLEET_ENV = {"CCC_NODE_ISOLATION_PROFILE": "fleet"}

HEAD_SHA = "aa11bb33" * 5
TARGET_SHA = "cc22dd44" * 5
MARKER_SHA = "ee33ff55" * 5

LOCATE_JSON = json.dumps(
    {
        "running": True,
        "bridges": [
            {"pid": 7, "checkout": "/opt/ccc-node", "projectPath": "/opt/ccc-node", "head": HEAD_SHA, "branch": "main", "dirty": 0}
        ],
    }
)
DOCTOR_JSON = json.dumps(
    {
        "rows": [
            {"action": "none", "class": "정상", "item": "settings.json", "status": "installed"},
            {"action": "install", "class": "교정가능", "item": "hooks/foo.sh", "status": "drifted"},
            {"action": "install", "class": "교정가능", "item": "skills/bar/SKILL.md", "status": "missing"},
        ]
    }
)


def _completed(cmd: list[str], stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=cmd, returncode=returncode, stdout=stdout.encode(), stderr=stderr.encode()
    )


class FakeGit:
    """Dispatch a runner by the git subcommand shape."""

    def __init__(
        self,
        *,
        head: str = HEAD_SHA,
        target: str = TARGET_SHA,
        behind: int = 2,
        ahead: int = 0,
        dirty: int = 1,
        dep_files: list[str] | None = None,
        fetch_fails: bool = False,
        no_git: bool = False,
        doctor_json: str = DOCTOR_JSON,
        locate_json: str = LOCATE_JSON,
        doctor_exit: int = 1,
    ) -> None:
        self.head = head
        self.target = target
        self.behind = behind
        self.ahead = ahead
        self.dirty = dirty
        self.dep_files = dep_files if dep_files is not None else ["bridge/requirements.lock.txt"]
        self.fetch_fails = fetch_fails
        self.no_git = no_git
        self.doctor_json = doctor_json
        self.locate_json = locate_json
        self.doctor_exit = doctor_exit
        self.commands: list[list[str]] = []

    def __call__(self, cmd: list[str], timeout: float):
        self.commands.append(list(cmd))
        joined = " ".join(cmd)
        if "ccc-bridge-locate.sh" in joined:
            return _completed(cmd, self.locate_json)
        if "ccc_doctor.py" in joined:
            return _completed(cmd, self.doctor_json, returncode=self.doctor_exit)
        if "ls-remote" in joined:
            if self.no_git:
                return _completed(cmd, returncode=128, stderr="fatal: not a git repository")
            return _completed(cmd, f"{self.target}\trefs/heads/main\n")
        if "fetch" in joined:
            if self.fetch_fails or self.no_git:
                return _completed(cmd, returncode=128, stderr="fetch failed")
            return _completed(cmd, "")
        if "rev-parse" in joined:
            if self.no_git:
                return _completed(cmd, returncode=128, stderr="fatal: not a git repository")
            return _completed(cmd, f"{self.head}\n")
        if "rev-list" in joined:
            if self.no_git:
                return _completed(cmd, returncode=128, stderr="fatal")
            if any(arg.startswith("origin/main..") for arg in cmd):
                return _completed(cmd, f"{self.ahead}\n")
            return _completed(cmd, f"{self.behind}\n")
        if "diff" in joined:
            return _completed(cmd, "".join(f"{name}\n" for name in self.dep_files))
        if "status" in joined:
            return _completed(cmd, "\n".join(f"?? file{i}" for i in range(self.dirty)) + ("\n" if self.dirty else ""))
        return _completed(cmd, returncode=1, stderr=f"unhandled: {joined}")


def _make_node(tmp_path: Path, *, marker_sha: str | None = MARKER_SHA, backups: int = 2) -> dict[str, str]:
    claude = tmp_path / "claude-home"
    state = claude / "state"
    state.mkdir(parents=True)
    if marker_sha is not None:
        (state / "self-update.installed-sha").write_text(marker_sha + "\n", encoding="utf-8")
    backup_dir = claude / "backups"
    backup_dir.mkdir()
    for i in range(backups):
        (backup_dir / f"ccc-node-setup-2026091{i}-044503.tar.gz").write_bytes(b"x" * (100 + i))
    return {
        **FLEET_ENV,
        "CCC_CLAUDE_DIR": str(claude),
        "CCC_DEPLOYMENT_DIFF_MARKER_FILE": str(state / "self-update.installed-sha"),
    }


def _clean_doctor_json() -> str:
    return json.dumps(
        {"rows": [{"action": "none", "class": "정상", "item": "settings.json", "status": "installed"}]}
    )


def test_collect_aggregates_behind_checkout(tmp_path: Path) -> None:
    env = _make_node(tmp_path)
    result = collect(env=env, runner=FakeGit())
    assert result["status"] == "ok"
    assert result["observed_at"].endswith("Z")
    sections = result["sections"]
    assert sections["checkout"]["head"] == HEAD_SHA
    assert sections["target"]["sha"] == TARGET_SHA
    assert sections["history"]["behind"] == 2
    assert sections["history"]["ahead"] == 0
    assert sections["history"]["fetched"] is True
    assert sections["installed"]["marker"]["present"] is True
    assert sections["installed"]["marker"]["sha"] == MARKER_SHA
    assert sections["installed"]["doctor"]["drift_total"] == 2
    assert sections["installed"]["doctor"]["exit_code"] == 1  # drift = exit 1, JSON still valid
    assert sorted(sections["installed"]["doctor"]["drift_items"]) == ["hooks/foo.sh", "skills/bar/SKILL.md"]
    assert sections["dependencies"]["changed_files"] == ["bridge/requirements.lock.txt"]
    assert sections["recovery"]["total"] == 2
    assert sections["recovery"]["latest"]["name"].endswith(".tar.gz")
    verdict = result["deployment"]
    assert verdict["informational_only"] is True
    for reason in ("checkout_behind:2", "checkout_dirty:1", "installed_drift:2", "installed_marker_differs"):
        assert reason in verdict["reasons"]
    assert verdict["verdict"] == "restart_recommended"
    # bridge is running the same head as the local checkout: no old-code reason
    assert "bridge_running_old_code" not in verdict["reasons"]


def test_up_to_date_checkout_has_clean_verdict(tmp_path: Path) -> None:
    env = _make_node(tmp_path, marker_sha=HEAD_SHA)
    git = FakeGit(behind=0, dirty=0, dep_files=[], doctor_json=_clean_doctor_json())
    result = collect(env=env, runner=git)
    assert result["status"] == "ok"
    sections = result["sections"]
    assert sections["dependencies"]["changed_files"] == []
    assert sections["dependencies"]["reason"] == "checkout is at origin/main"
    assert result["deployment"]["verdict"] == "up_to_date"
    assert result["deployment"]["reasons"] == []


def test_bridge_running_old_code_is_flagged_only_for_same_checkout(tmp_path: Path) -> None:
    env = _make_node(tmp_path)
    locate = json.loads(LOCATE_JSON)
    locate["bridges"][0]["head"] = "old" * 10 + "aaaa"
    result = collect(env=env, runner=FakeGit())
    # fake locate path (/opt/ccc-node) differs from the real repo root: unknown, not flagged
    assert "bridge_running_old_code" not in result["deployment"]["reasons"]


def test_fetch_failure_degrades_history_but_keeps_target(tmp_path: Path) -> None:
    env = _make_node(tmp_path)
    result = collect(env=env, runner=FakeGit(fetch_fails=True))
    sections = result["sections"]
    assert sections["target"]["sha"] == TARGET_SHA
    assert sections["history"]["fetched"] is False
    assert sections["history"]["behind"] is None
    assert "unknown (fetch disabled or failed)" in sections["history"]["behind_note"]
    assert sections["dependencies"]["changed_files"] == []
    assert "history_unknown" not in result["deployment"]["reasons"]  # section still ok
    assert "checkout_behind:2" not in result["deployment"]["reasons"]


def test_fetch_disabled_env(tmp_path: Path) -> None:
    env = {**_make_node(tmp_path), "CCC_DEPLOYMENT_DIFF_FETCH": "0"}
    git = FakeGit()
    result = collect(env=env, runner=git)
    assert result["sections"]["history"]["fetched"] is False
    assert not any("fetch" in " ".join(cmd) for cmd in git.commands)


def test_missing_marker_and_no_backups(tmp_path: Path) -> None:
    env = _make_node(tmp_path, marker_sha=None, backups=0)
    result = collect(env=env, runner=FakeGit())
    sections = result["sections"]
    assert sections["installed"]["marker"]["present"] is False
    assert sections["installed"]["status"] == "ok"  # doctor still ok: section stays ok
    assert "installed_marker_missing" in result["deployment"]["reasons"]
    assert sections["recovery"]["latest"] is None
    assert sections["recovery"]["total"] == 0


def test_no_git_checkout_is_section_scoped_unknown(tmp_path: Path) -> None:
    env = _make_node(tmp_path)
    result = collect(env=env, runner=FakeGit(no_git=True))
    sections = result["sections"]
    assert sections["target"]["status"] == "unknown"
    assert sections["history"]["status"] == "unknown"
    assert result["status"] == "unknown"
    assert result["deployment"]["verdict"] == "restart_recommended"
    assert "history_unknown" in result["deployment"]["reasons"]


def test_git_command_env_override(tmp_path: Path) -> None:
    fake = tmp_path / "fake-git.sh"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '  "ls-remote origin") printf \'%s\\trefs/heads/main\\n\' "' + TARGET_SHA + '";;\n'
        '  "rev-parse HEAD") echo "' + HEAD_SHA + '";;\n'
        '  "fetch origin"*) exit 0;;\n'
        '  "rev-list"*) echo 0;;\n'
        '  "status --porcelain") :;;\n'
        '  *) exit 1;;\n'
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = {**_make_node(tmp_path), "CCC_DEPLOYMENT_DIFF_GIT": f"bash {fake}"}
    result = collect(env=env)
    assert result["sections"]["history"]["behind"] == 0
    assert result["deployment"]["verdict"] == "restart_recommended"  # marker still differs


def test_locate_failure_is_section_scoped(tmp_path: Path) -> None:
    env = {
        **_make_node(tmp_path),
        "CCC_DEPLOYMENT_DIFF_LOCATE": "bash /nonexistent/locate.sh",
    }
    result = collect(env=env, runner=FakeGit())
    assert result["sections"]["checkout"]["status"] == "unknown"
    assert result["status"] == "unknown"
    assert "checkout_unknown" in result["deployment"]["reasons"]


def test_doctor_failure_keeps_marker_verdict(tmp_path: Path) -> None:
    env = {
        **_make_node(tmp_path),
        "CCC_DEPLOYMENT_DIFF_DOCTOR": "bash /nonexistent/doctor.sh",
    }
    result = collect(env=env, runner=FakeGit())
    installed = result["sections"]["installed"]
    assert installed["doctor"]["status"] == "unknown"
    assert installed["status"] == "ok"  # marker present, doctor unknown → section-scoped
    assert "installed_drift:2" not in result["deployment"]["reasons"]
    assert "installed_unknown" not in result["deployment"]["reasons"]


def test_policy_denied() -> None:
    with pytest.raises(DeploymentDiffError) as exc:
        collect(env={"CCC_NODE_ISOLATION_PROFILE": "external"})
    assert exc.value.code == "policy_denied"
    with pytest.raises(DeploymentDiffError) as exc:
        collect(env={"CCC_MEMORY_AUDIENCE": "shared"})
    assert exc.value.code == "policy_denied"
