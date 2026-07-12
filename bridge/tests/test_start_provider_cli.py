"""Regression tests for provider-specific CLI startup gating."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


START_SH = Path(__file__).resolve().parents[1] / "start.sh"


def _function_source(name: str) -> str:
    lines = START_SH.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line == f"{name}() {{")
    depth = 0
    selected: list[str] = []
    for line in lines[start:]:
        selected.append(line)
        depth += line.count("{") - line.count("}")
        if depth == 0:
            break
    return "\n".join(selected)


def _run(tmp_path: Path, *, provider: str, codex_cli: str = "codex") -> subprocess.CompletedProcess[str]:
    program = "\n".join(
        [
            "set -eu",
            'read_env_with_fallback() { case "$1" in CCC_AGENT_PROVIDER) printf "%s" "$TEST_PROVIDER" ;; CCC_CODEX_CLI_PATH) printf "%s" "$TEST_CODEX_CLI" ;; esac; }',
            _function_source("maybe_setup_agent_cli"),
            "maybe_setup_agent_cli",
        ]
    )
    env = {
        **os.environ,
        "TEST_PROVIDER": provider,
        "TEST_CODEX_CLI": codex_cli,
        "CLAUDE_CLI_PATH": "",
        "PATH": f"{tmp_path}:/usr/bin:/bin",
    }
    return subprocess.run(["bash", "-c", program], text=True, capture_output=True, env=env, check=False)


def test_codex_provider_does_not_require_claude(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n")
    codex.chmod(0o700)

    result = _run(tmp_path, provider="codex", codex_cli=str(codex))

    assert result.returncode == 0
    assert "Codex provider CLI is available" in result.stdout
    assert "claude command not found" not in result.stdout


def test_codex_provider_fails_closed_when_cli_is_missing(tmp_path: Path) -> None:
    result = _run(tmp_path, provider="codex", codex_cli=str(tmp_path / "missing"))

    assert result.returncode == 1
    assert "configured Codex CLI is not executable" in result.stdout
    assert str(tmp_path) not in result.stdout


def test_unknown_provider_fails_closed(tmp_path: Path) -> None:
    result = _run(tmp_path, provider="unknown")

    assert result.returncode == 1
    assert "unsupported CCC_AGENT_PROVIDER" in result.stdout
