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
    sandbox: str = "host",
    hide_bwrap: bool = False,
    process_sandbox: str | None = None,
    process_provider: str | None = None,
    process_danso_cli: str | None = None,
    grok_transport: str = "",
) -> subprocess.CompletedProcess[str]:
    program = "\n".join(
        [
            "set -eu",
            'read_env_with_fallback() { case "$1" in '
            'CCC_AGENT_PROVIDER) printf "%s" "$TEST_PROVIDER" ;; '
            'CCC_CODEX_CLI_PATH) printf "%s" "$TEST_CODEX_CLI" ;; '
            'CCC_CRUSH_CLI_PATH) printf "%s" "$TEST_CRUSH_CLI" ;; '
            'CCC_DANSO_SANDBOX) printf "%s" "$TEST_DANSO_SANDBOX" ;; '
            'CCC_DANSO_CLI_PATH) printf "%s" "$TEST_DANSO_CLI" ;; '
            'CCC_PIRI_CLI_PATH) printf "%s" "$TEST_PIRI_CLI" ;; '
            'CCC_GROK_TRANSPORT) printf "%s" "$TEST_GROK_TRANSPORT" ;; esac; }',
            _function_source("maybe_setup_agent_cli"),
            'command() { if [ "${1:-}" = "-v" ] && [ "${2:-}" = "bwrap" ]; then return 1; fi; builtin command "$@"; }' if hide_bwrap else ":",
            "maybe_setup_agent_cli",
        ]
    )
    env = {
        **{k: v for k, v in os.environ.items() if k not in {"CCC_AGENT_PROVIDER", "CCC_DANSO_CLI_PATH", "CCC_DANSO_SANDBOX", "CCC_GROK_TRANSPORT"}},
        "TEST_PROVIDER": provider,
        "TEST_CODEX_CLI": codex_cli,
        "TEST_CRUSH_CLI": crush_cli,
        "TEST_PIRI_CLI": piri_cli,
        "TEST_DANSO_CLI": danso_cli,
        "TEST_DANSO_SANDBOX": sandbox,
        "TEST_GROK_TRANSPORT": grok_transport,
        "CLAUDE_CLI_PATH": "",
        "PATH": f"{tmp_path}:/usr/bin:/bin",
    }
    if process_sandbox is not None:
        env["CCC_DANSO_SANDBOX"] = process_sandbox
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


def test_grok_provider_defaults_to_local_transport(tmp_path: Path) -> None:
    result = _run(tmp_path, provider="grok")
    assert result.returncode == 0
    assert "Grok local transport" in result.stdout
    assert "claude command not found" not in result.stdout


def test_grok_ssh_transport_needs_ssh_client(tmp_path: Path) -> None:
    result = _run(tmp_path, provider="grok", grok_transport="ssh")
    assert result.returncode == 0
    assert "Grok SSH client available" in result.stdout


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

def test_danso_host_without_bwrap_and_explicit_backend_gate(tmp_path):
    binary = tmp_path / "danso"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    for sandbox, expected in (("host",0), ("bubblewrap",1), ("invalid",1)):
        result = _run(tmp_path, provider="danso", danso_cli=str(binary), sandbox=sandbox, hide_bwrap=True)
        assert result.returncode == expected, result.stdout + result.stderr
    override = _run(tmp_path, provider="danso", danso_cli=str(binary), sandbox="bubblewrap",
                    process_sandbox="host", hide_bwrap=True)
    assert override.returncode == 0, override.stdout + override.stderr


def test_real_startup_merge_preserves_runtime_and_backend_precedence(tmp_path):
    cases = [("host", None, "bubblewrap", "host"),
             ("bubblewrap", None, "host", "bubblewrap"),
             (None, "host", "bubblewrap", "host"),
             (None, "bubblewrap", "host", "bubblewrap"),
             (None, None, "host", "host"),
             (None, None, "bubblewrap", "bubblewrap")]
    for index, (process, project, fallback, expected) in enumerate(cases):
        root = tmp_path / str(index)
        root.mkdir()
        global_dir = root / "global"
        global_dir.mkdir()
        project_file = root / "project.env"
        project_file.write_text(f"CCC_DANSO_SANDBOX={project}\n" if project else "")
        (global_dir / ".env").write_text(
            f"CCC_DANSO_SANDBOX={fallback}\nCCC_AGENT_PROVIDER=piri\nCCC_DANSO_CLI_PATH=/missing/fallback\n")
        for name in ("danso", "bwrap"):
            binary = root / name
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o700)
        program = '\n'.join([
            'source "$START_SH" --path "$TEST_ROOT"',
            'SCRIPT_DIR="$TEST_GLOBAL"; ENV_FILE="$TEST_PROJECT_ENV"',
            'merge_env_files',
            'printf "effective=%s\\n" "${CCC_DANSO_SANDBOX:-$(read_env_with_fallback CCC_DANSO_SANDBOX)}"',
            _function_source("maybe_setup_agent_cli"),
            'maybe_setup_agent_cli',
        ])
        env = {'PATH': f'{root}:/usr/bin:/bin', 'HOME': str(root),
               'CCC_START_SH_LIB_ONLY': '1', 'START_SH': str(START_SH),
               'TEST_ROOT': str(root), 'TEST_GLOBAL': str(global_dir),
               'TEST_PROJECT_ENV': str(project_file), 'CCC_AGENT_PROVIDER': 'danso',
               'CCC_DANSO_CLI_PATH': str(root / 'danso')}
        if process is not None:
            env['CCC_DANSO_SANDBOX'] = process
        result = subprocess.run(['bash', '-c', program], env=env, text=True,
                                capture_output=True, timeout=10, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert f'effective={expected}\n' in result.stdout
        assert 'Danso provider CLI is available' in result.stdout


def test_runtime_subscription_selection_survives_fallback_merge(tmp_path):
    project = tmp_path / "project.env"
    project.write_text("")
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / ".env").write_text("CCC_DANSO_AUTH_MODE=api-key\nDANSO_CHATGPT_AUTH_FILE=/wrong/auth.json\nDANSO_CHATGPT_BASE_URL=http://127.0.0.1:1/wrong\n")
    program = '\n'.join([
        'source "$START_SH" --path "$TEST_ROOT"',
        'SCRIPT_DIR="$TEST_GLOBAL"; ENV_FILE="$TEST_PROJECT_ENV"',
        'merge_env_files',
        'printf "auth=%s file=%s base=%s\\n" "$CCC_DANSO_AUTH_MODE" "$DANSO_CHATGPT_AUTH_FILE" "$DANSO_CHATGPT_BASE_URL"',
    ])
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CCC_START_SH_LIB_ONLY": "1",
           "START_SH": str(START_SH), "TEST_ROOT": str(tmp_path), "TEST_GLOBAL": str(global_dir),
           "TEST_PROJECT_ENV": str(project), "CCC_DANSO_AUTH_MODE": "chatgpt",
           "DANSO_CHATGPT_AUTH_FILE": "/private/danso-auth.json",
           "DANSO_CHATGPT_BASE_URL": "https://chatgpt.com/backend-api/codex"}
    result = subprocess.run(["bash", "-c", program], env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "auth=chatgpt file=/private/danso-auth.json base=https://chatgpt.com/backend-api/codex" in result.stdout


def test_runtime_platform_credentials_survive_fallback_merge(tmp_path):
    project = tmp_path / "project.env"
    project.write_text("")
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    (global_dir / ".env").write_text("OPENAI_API_KEY=global-key\nDANSO_OPENAI_BASE_URL=https://global.example/v1\n")
    program = '\n'.join([
        'source "$START_SH" --path "$TEST_ROOT"',
        'SCRIPT_DIR="$TEST_GLOBAL"; ENV_FILE="$TEST_PROJECT_ENV"',
        'merge_env_files',
        'test "$OPENAI_API_KEY" = process-key && test "$DANSO_OPENAI_BASE_URL" = https://process.example/v1',
    ])
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "CCC_START_SH_LIB_ONLY": "1",
           "START_SH": str(START_SH), "TEST_ROOT": str(tmp_path), "TEST_GLOBAL": str(global_dir),
           "TEST_PROJECT_ENV": str(project), "OPENAI_API_KEY": "process-key",
           "DANSO_OPENAI_BASE_URL": "https://process.example/v1"}
    result = subprocess.run(["bash", "-c", program], env=env, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
