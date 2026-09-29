"""Startup required-env check + shared unit EnvironmentFile (#1771, after #2065).

#2065 hands wrapper paths from the merged config to the wrapper child. The
rest of the owner decision: secrets reach the provider only through the
process environment (the shared owner-only systemd EnvironmentFile), both unit
templates read that one file, and a provider whose requirements cannot be
resolved is reported at startup by key NAME only.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import telegram_bot.__main__ as main_module
from telegram_bot.core.bot_lifecycle import BotLifecycleMixin
from telegram_bot.utils.config import Config
from telegram_bot.utils.provider_requirements import (
    missing_provider_environment,
    provider_child_environment,
    provider_environment_problem,
)

BRIDGE_DIR = Path(__file__).resolve().parents[1]
WRAPPER = BRIDGE_DIR.parent / "scripts" / "ccc-piri"
SECRET = "sk-ant-oat01-SECRET-VALUE-must-never-be-logged"
SHARED_ENV_SUFFIX = "/.config/ccc-node/bridge.env"
CLAUDE_AUTH = "CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN"


def _system_path() -> str:
    bash_dir = str(Path(shutil.which("bash") or "/bin/bash").parent)
    return ":".join(dict.fromkeys([bash_dir, "/usr/bin", "/bin"]))


def _no_real_piri() -> str:
    path = _system_path()
    if shutil.which("piri", path=path):
        pytest.skip("a real `piri` on the system PATH satisfies the wrapper default")
    return path


def _stub_cli(path: Path) -> Path:
    # Absolute interpreter shebang: `#!/usr/bin/env` does not resolve on Termux.
    path.write_text(f"#!{sys.executable}\nprint('stub')\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _wrapper_copy(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "ccc-piri"
    shutil.copy2(WRAPPER, target)
    target.chmod(0o755)
    return target


def _load(tmp_path: Path, project_env: str, **process: str) -> Config:
    root = tmp_path / "project"
    (root / ".telegram_bot").mkdir(parents=True, exist_ok=True)
    (root / ".telegram_bot" / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=123456:test\n" + project_env, encoding="utf-8"
    )
    fallback = tmp_path / "package.env"
    fallback.write_text("", encoding="utf-8")
    environ = {"HOME": str(tmp_path / "home"), "PATH": _system_path(), **process}
    return Config.load(project_root=root, environ=environ, bot_env_file=fallback)


# --- requirement resolution -------------------------------------------------


def test_piri_wrapper_without_real_cli_names_the_missing_key(tmp_path: Path) -> None:
    wrapper = _wrapper_copy(tmp_path / "hooks")
    environ = {"HOME": str(tmp_path), "PATH": _no_real_piri()}
    assert missing_provider_environment("piri", environ, cli_path=str(wrapper)) == (
        "CCC_PIRI_REAL_CLI_PATH",
    )
    real = _stub_cli(tmp_path / "real-piri")
    assert missing_provider_environment(
        "piri", {**environ, "CCC_PIRI_REAL_CLI_PATH": str(real)}, cli_path=str(wrapper)
    ) == ()
    # A non-wrapper CLI has no real-CLI requirement; an unresolvable CLI names
    # its own setting.
    assert missing_provider_environment("piri", environ, cli_path=str(real)) == ()
    assert missing_provider_environment("codex", environ, cli_path=str(tmp_path / "nope")) == (
        "CCC_CODEX_CLI_PATH",
    )


def test_wrapper_key_from_project_env_satisfies_the_check_via_2065(tmp_path: Path) -> None:
    """The check sees what the child gets: process env + #2065 wrapper keys."""

    wrapper = _wrapper_copy(tmp_path / "hooks")
    path = _no_real_piri()
    real = _stub_cli(tmp_path / "real-piri")
    base = {"HOME": str(tmp_path), "PATH": path}
    with_key = _load(
        tmp_path,
        f"CCC_AGENT_PROVIDER=piri\nCCC_PIRI_CLI_PATH={wrapper}\nCCC_PIRI_REAL_CLI_PATH={real}\n",
    )
    assert provider_environment_problem(with_key, provider_child_environment(with_key, base)) is None
    without_key = _load(tmp_path, f"CCC_AGENT_PROVIDER=piri\nCCC_PIRI_CLI_PATH={wrapper}\n")
    assert provider_environment_problem(
        without_key, provider_child_environment(without_key, base)
    ) == "required provider environment missing: provider=piri missing=CCC_PIRI_REAL_CLI_PATH"


def test_secrets_are_not_injected_from_env_files(tmp_path: Path) -> None:
    """A token only in a .env file never reaches the child env; only the
    process env (shared EnvironmentFile) counts, and the check says so."""

    home = tmp_path / "home"
    home.mkdir()
    settings = _load(tmp_path, f"CLAUDE_CODE_OAUTH_TOKEN={SECRET}\n")
    base = {"HOME": str(home), "PATH": "/usr/bin"}
    child = provider_child_environment(settings, base)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in child
    problem = provider_environment_problem(settings, child, platform="linux")
    assert problem == f"required provider environment missing: provider=claude missing={CLAUDE_AUTH}"
    assert SECRET not in problem
    # The same token delivered through the process env (EnvironmentFile) is enough.
    assert provider_environment_problem(
        settings, {**child, "CLAUDE_CODE_OAUTH_TOKEN": SECRET}, platform="linux"
    ) is None


def test_claude_accepts_non_env_logins(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    environ = {"HOME": str(home), "PATH": "/usr/bin"}
    assert missing_provider_environment("claude", environ, platform="linux") == (CLAUDE_AUTH,)
    assert missing_provider_environment("claude", environ, platform="darwin") == ()
    helper = tmp_path / "settings.json"
    helper.write_text('{"apiKeyHelper": "/usr/local/bin/key"}', encoding="utf-8")
    assert missing_provider_environment(
        "claude", environ, claude_settings_path=helper, platform="linux"
    ) == ()
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
    assert missing_provider_environment("claude", environ, platform="linux") == ()
    assert missing_provider_environment("danso", {}, platform="linux") == ()


def test_problem_helper_never_raises() -> None:
    class Exploding:
        @property
        def agent_provider(self) -> str:
            raise RuntimeError("boom")

    assert provider_environment_problem(Exploding(), {}) is None


# --- startup report + readiness surfaces ------------------------------------


def test_startup_report_logs_names_never_values(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    wrapper = _wrapper_copy(tmp_path / "hooks")
    path = _no_real_piri()
    settings = _load(
        tmp_path,
        f"CCC_AGENT_PROVIDER=piri\nCCC_PIRI_CLI_PATH={wrapper}\nCLAUDE_CODE_OAUTH_TOKEN={SECRET}\n",
    )
    environ = {"HOME": str(tmp_path), "PATH": path, "ANTHROPIC_API_KEY": SECRET}
    with caplog.at_level(logging.INFO, logger=main_module.logger.name):
        problem = main_module.report_provider_environment(settings, environ)
    assert problem == (
        "required provider environment missing: provider=piri missing=CCC_PIRI_REAL_CLI_PATH"
    )
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "missing=CCC_PIRI_REAL_CLI_PATH" in errors[0]
    assert all(SECRET not in r.getMessage() for r in caplog.records)
    assert all(str(wrapper) not in r.getMessage() for r in caplog.records)


def test_startup_report_skips_settings_without_a_provider(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=main_module.logger.name):
        assert main_module.report_provider_environment(SimpleNamespace(), {}) is None
    assert caplog.records == []


def test_telegram_readiness_probe_reports_the_missing_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = _wrapper_copy(tmp_path / "hooks")
    monkeypatch.setenv("PATH", _no_real_piri())
    monkeypatch.delenv("CCC_PIRI_REAL_CLI_PATH", raising=False)
    config = SimpleNamespace(agent_provider="piri", piri_cli_path=str(wrapper))
    fake = SimpleNamespace(
        _config=config,
        _wrapper_child_environment=lambda: BotLifecycleMixin._wrapper_child_environment(fake),
        _probe_piri_readiness=lambda: (_ for _ in ()).throw(AssertionError("probe must not run")),
    )
    ready, reason = BotLifecycleMixin._probe_agent_readiness(fake)
    assert not ready
    assert reason == (
        "required provider environment missing: provider=piri missing=CCC_PIRI_REAL_CLI_PATH"
    )


# --- shared EnvironmentFile in both unit templates --------------------------


def _service_lines(text: str) -> list[str]:
    section, lines = "", []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line
        elif section == "[Service]" and line and not line.startswith("#"):
            lines.append(line)
    return lines


def _secret_environment_lines(lines: list[str]) -> list[str]:
    return [
        line for line in lines
        if line.startswith(("Environment=CLAUDE_CODE_OAUTH_TOKEN", "Environment=ANTHROPIC_"))
    ]


def test_matrix_unit_template_reads_the_shared_environment_file() -> None:
    lines = _service_lines(
        (BRIDGE_DIR / "service-systemd-matrix.service.example").read_text(encoding="utf-8")
    )
    assert [line for line in lines if line.startswith("EnvironmentFile=")] == [
        "EnvironmentFile=-/root" + SHARED_ENV_SUFFIX
    ]
    assert _secret_environment_lines(lines) == []


def test_telegram_unit_renderer_reads_the_same_shared_environment_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(f"#!{shutil.which('bash') or '/bin/bash'}\nexit 0\n", encoding="utf-8")
    systemctl.chmod(0o755)
    unit_dir = tmp_path / "units"
    result = subprocess.run(
        ["bash", str(BRIDGE_DIR / "service-systemd.sh"), "install", "--project-root", str(project)],
        env={
            "HOME": str(home),
            "PATH": _system_path(),
            "CCC_SYSTEMD_DIR": str(unit_dir),
            "CCC_SYSTEMCTL": str(systemctl),
            "CCC_BRIDGE_MEMORY_GUARD": "0",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = _service_lines((unit_dir / "ccc-telegram-bridge.service").read_text(encoding="utf-8"))
    assert [line for line in lines if line.startswith("EnvironmentFile=")] == [
        f"EnvironmentFile=-{home}{SHARED_ENV_SUFFIX}"
    ]
    assert _secret_environment_lines(lines) == []
