"""Regression tests for the body-free bridge runtime-config preflight."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from telegram_bot.runtime_config_check import check_runtime_config


CHECKER = Path(__file__).resolve().parents[1] / "runtime_config_check.py"


def test_legacy_process_timeout_conflicts_with_delegated_default(tmp_path: Path) -> None:
    bridge_env = tmp_path / "bridge.env"
    bridge_env.write_text("CLAUDE_PROCESS_TIMEOUT=3600\n", encoding="utf-8")

    result = check_runtime_config(
        project_root=tmp_path / "project",
        bridge_env=bridge_env,
        environ={},
    )

    assert not result.ok
    assert result.code == "delegated-task-stall-not-lower-than-process-timeout"


def test_explicit_lower_delegated_timeout_is_valid(tmp_path: Path) -> None:
    bridge_env = tmp_path / "bridge.env"
    bridge_env.write_text(
        "CLAUDE_PROCESS_TIMEOUT=3600\n"
        "CCC_DELEGATED_TASK_STALL_SECONDS=1800\n",
        encoding="utf-8",
    )

    result = check_runtime_config(
        project_root=tmp_path / "project",
        bridge_env=bridge_env,
        environ={},
    )

    assert result.ok
    assert result.code == "ok"


def test_process_environment_keeps_runtime_precedence(tmp_path: Path) -> None:
    project_env = tmp_path / "project/.telegram_bot/.env"
    project_env.parent.mkdir(parents=True)
    project_env.write_text("CLAUDE_PROCESS_TIMEOUT=3600\n", encoding="utf-8")
    bridge_env = tmp_path / "bridge.env"
    bridge_env.write_text("CLAUDE_PROCESS_TIMEOUT=1800\n", encoding="utf-8")

    result = check_runtime_config(
        project_root=tmp_path / "project",
        bridge_env=bridge_env,
        environ={"CLAUDE_PROCESS_TIMEOUT": "21600"},
    )

    assert result.ok


def test_cli_json_is_body_free_on_failure(tmp_path: Path) -> None:
    bridge_env = tmp_path / "bridge.env"
    secret_marker = "SENSITIVE_TOKEN_MUST_NOT_APPEAR"
    bridge_env.write_text(
        f"TELEGRAM_BOT_TOKEN={secret_marker}\n"
        "CLAUDE_PROCESS_TIMEOUT=3600\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--project-root",
            str(tmp_path / "project"),
            "--bridge-env",
            str(bridge_env),
            "--json",
        ],
        env={"PATH": os.environ.get("PATH", "")},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert secret_marker not in completed.stdout
    assert secret_marker not in completed.stderr
    assert json.loads(completed.stdout) == {
        "code": "delegated-task-stall-not-lower-than-process-timeout",
        "ok": False,
    }
