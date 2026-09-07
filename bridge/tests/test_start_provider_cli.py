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


def _run(
    tmp_path: Path,
    *,
    provider: str,
    codex_cli: str = "codex",
    crush_cli: str = "crush",
    piri_cli: str = "piri",
    danso_cli: str = "danso",
    process_provider: str | None = None,
    process_danso_cli: str | None = None,
) -> subprocess.CompletedProcess[str]:
    program = "\n".join(
        [
            "set -eu",
            'read_env_with_fallback() { case "$1" in '
            'CCC_AGENT_PROVIDER) printf "%s" "$TEST_PROVIDER" ;; '
            'CCC_CODEX_CLI_PATH) printf "%s" "$TEST_CODEX_CLI" ;; '
            'CCC_CRUSH_CLI_PATH) printf "%s" "$TEST_CRUSH_CLI" ;; '
            'CCC_DANSO_CLI_PATH) printf "%s" "$TEST_DANSO_CLI" ;; '
            'CCC_PIRI_CLI_PATH) printf "%s" "$TEST_PIRI_CLI" ;; esac; }',
            _function_source("maybe_setup_agent_cli"),
            "maybe_setup_agent_cli",
        ]
    )
    env = {
        **{k: v for k, v in os.environ.items() if k not in {"CCC_AGENT_PROVIDER", "CCC_DANSO_CLI_PATH"}},
        "TEST_PROVIDER": provider,
        "TEST_CODEX_CLI": codex_cli,
        "TEST_CRUSH_CLI": crush_cli,
        "TEST_PIRI_CLI": piri_cli,
        "TEST_DANSO_CLI": danso_cli,
        "CLAUDE_CLI_PATH": "",
        "PATH": f"{tmp_path}:/usr/bin:/bin",
    }
    if process_provider is not None:
        env["CCC_AGENT_PROVIDER"] = process_provider
    if process_danso_cli is not None:
        env["CCC_DANSO_CLI_PATH"] = process_danso_cli
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


def test_crush_provider_uses_crush_cli(tmp_path: Path) -> None:
    crush = tmp_path / "crush"
    crush.write_text("#!/bin/sh\nexit 0\n")
    crush.chmod(0o700)

    result = _run(tmp_path, provider="crush", crush_cli=str(crush))

    assert result.returncode == 0
    assert "crush provider CLI is available" in result.stdout


def test_crush_provider_fails_closed_when_cli_is_missing(tmp_path: Path) -> None:
    result = _run(tmp_path, provider="crush", crush_cli=str(tmp_path / "missing"))

    assert result.returncode == 1
    assert "configured crush CLI is not executable" in result.stdout
    assert str(tmp_path) not in result.stdout


def test_piri_provider_does_not_require_claude(tmp_path: Path) -> None:
    piri = tmp_path / "piri"
    piri.write_text("#!/bin/sh\nexit 0\n")
    piri.chmod(0o700)

    result = _run(tmp_path, provider="piri", piri_cli=str(piri))

    assert result.returncode == 0
    assert "Piri provider CLI is available" in result.stdout
    assert "claude command not found" not in result.stdout


def test_piri_provider_fails_closed_when_cli_is_missing(tmp_path: Path) -> None:
    result = _run(tmp_path, provider="piri", piri_cli=str(tmp_path / "missing"))

    assert result.returncode == 1
    assert "configured Piri CLI is not executable" in result.stdout
    assert str(tmp_path) not in result.stdout


def test_unknown_provider_fails_closed(tmp_path: Path) -> None:
    result = _run(tmp_path, provider="unknown")

    assert result.returncode == 1
    assert "unsupported CCC_AGENT_PROVIDER" in result.stdout


def test_danso_startup_checks_its_cli_without_claude(tmp_path):
    for name in ("danso", "bwrap"):
        path = tmp_path / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o700)
    result = _run(tmp_path, provider="danso", danso_cli=str(tmp_path / "danso"))
    assert result.returncode == 0, result.stderr
    assert "Danso provider CLI is available" in result.stdout
    failed = _run(tmp_path, provider="danso", danso_cli=str(tmp_path / "missing"))
    assert failed.returncode == 1
    assert "Danso CLI unavailable" in failed.stdout


def test_exported_danso_provider_and_cli_override_file_settings(tmp_path):
    for name in ("danso", "bwrap"):
        path = tmp_path / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o700)
    result = _run(tmp_path, provider="claude", danso_cli="/missing/from-file",
                  process_provider="danso", process_danso_cli=str(tmp_path / "danso"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Danso provider CLI is available" in result.stdout
    assert "Claude" not in result.stdout
