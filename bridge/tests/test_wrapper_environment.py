"""#1771: wrapper-only keys from the .env files reach provider child processes.

The ccc-piri / ccc-codex launcher wrappers read ``CCC_PIRI_REAL_CLI_PATH`` and
friends only from their own process environment, while ``Config.load`` merges
the project ``.env`` and the package fallback ``.env`` without exporting them.
These tests pin the explicit, allowlisted hand-off that closes that gap for
both frontends (the Matrix unit never runs ``start.sh``).
"""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from telegram_bot.utils.wrapper_environment import (
    WRAPPER_ENV_KEYS,
    missing_wrapper_environment,
    select_wrapper_environment,
    with_wrapper_environment,
    wrapper_environment,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHONPATH_SHIM = REPO_ROOT / ".github" / "pythonpath"


def _settings(values):
    return SimpleNamespace(wrapper_environment=lambda: dict(values))


def test_allowlist_is_wrapper_only_and_carries_no_secret_names():
    assert set(WRAPPER_ENV_KEYS) == {
        "CCC_PIRI_REAL_CLI_PATH",
        "CCC_PIRI_MEMORY_MATERIALIZER_PATH",
        "CCC_PIRI_MEMORY_HOME",
        "CCC_PIRI_MEMORY_SKIP",
        "CCC_CODEX_REAL_CLI_PATH",
        "CCC_CODEX_MEMORY_MATERIALIZER_PATH",
    }
    for name in WRAPPER_ENV_KEYS:
        assert not any(marker in name for marker in ("TOKEN", "KEY", "SECRET", "PASSWORD"))


def test_select_keeps_only_allowlisted_non_empty_values():
    selected = select_wrapper_environment(
        {
            "CCC_PIRI_REAL_CLI_PATH": "/opt/piri/real",
            "CCC_CODEX_REAL_CLI_PATH": "   ",
            "CCC_PIRI_MEMORY_HOME": "bad\x00value",
            "CCC_PIRI_MEMORY_SKIP": None,
            "TELEGRAM_BOT_TOKEN": "123456:secret",
            "ANTHROPIC_API_KEY": "secret",
            "CLAUDE_CODE_OAUTH_TOKEN": "secret",
        }
    )
    assert selected == {"CCC_PIRI_REAL_CLI_PATH": "/opt/piri/real"}


def test_existing_child_environment_always_wins():
    settings = _settings(
        {
            "CCC_PIRI_REAL_CLI_PATH": "/from/env-file",
            "CCC_CODEX_REAL_CLI_PATH": "/from/env-file/codex",
        }
    )
    base = {"PATH": "/usr/bin", "CCC_PIRI_REAL_CLI_PATH": "/explicit"}

    merged = with_wrapper_environment(base, settings)

    assert merged == {
        "PATH": "/usr/bin",
        "CCC_PIRI_REAL_CLI_PATH": "/explicit",
        "CCC_CODEX_REAL_CLI_PATH": "/from/env-file/codex",
    }
    assert base == {"PATH": "/usr/bin", "CCC_PIRI_REAL_CLI_PATH": "/explicit"}
    assert missing_wrapper_environment(settings, base) == {
        "CCC_CODEX_REAL_CLI_PATH": "/from/env-file/codex"
    }


def test_settings_without_a_capture_contribute_nothing():
    assert wrapper_environment(SimpleNamespace()) == {}
    assert with_wrapper_environment({"A": "1"}, SimpleNamespace()) == {"A": "1"}


def test_config_load_captures_wrapper_keys_from_both_env_files(tmp_path, monkeypatch):
    from telegram_bot.utils.config import Settings

    project_root = tmp_path / "project"
    project_env = project_root / ".telegram_bot" / ".env"
    project_env.parent.mkdir(parents=True)
    project_env.write_text(
        "TELEGRAM_BOT_TOKEN=123456:project\n"
        "CCC_PIRI_REAL_CLI_PATH=/project/real-piri\n"
        "CCC_PIRI_MEMORY_HOME=/project/agent\n"
        "ANTHROPIC_API_KEY=project-secret\n",
        encoding="utf-8",
    )
    fallback_env = tmp_path / "bridge.env"
    fallback_env.write_text(
        "CCC_PIRI_REAL_CLI_PATH=/bridge/real-piri\n"
        "CCC_CODEX_REAL_CLI_PATH=/bridge/real-codex\n"
        "CLAUDE_CODE_OAUTH_TOKEN=bridge-secret\n",
        encoding="utf-8",
    )
    before = dict(os.environ)

    settings = Settings.load(
        project_root=project_root,
        environ={"CCC_PIRI_MEMORY_HOME": "/process/agent"},
        bot_env_file=fallback_env,
    )

    # Precedence matches every other setting: process > project > fallback.
    assert settings.wrapper_environment() == {
        "CCC_PIRI_REAL_CLI_PATH": "/project/real-piri",
        "CCC_PIRI_MEMORY_HOME": "/process/agent",
        "CCC_CODEX_REAL_CLI_PATH": "/bridge/real-codex",
    }
    # Loading still never exports anything.
    assert dict(os.environ) == before
    # A caller cannot mutate the captured mapping through the accessor.
    settings.wrapper_environment()["CCC_PIRI_REAL_CLI_PATH"] = "/tampered"
    assert settings.wrapper_environment()["CCC_PIRI_REAL_CLI_PATH"] == "/project/real-piri"


def _run_composition_probe(tmp_path: Path, provider: str, channel: str):
    project_root = tmp_path / "project"
    project_env = project_root / ".telegram_bot" / ".env"
    project_env.parent.mkdir(parents=True)
    project_env.write_text(
        "TELEGRAM_BOT_TOKEN=123456:test\n"
        "ALLOWED_USER_IDS=1\n"
        f"CCC_AGENT_PROVIDER={provider}\n"
        f"CCC_CHANNEL={channel}\n"
        f"CCC_MATRIX_CONFIG_PATH={tmp_path / 'matrix.json'}\n"
        "CCC_PIRI_REAL_CLI_PATH=/project/real-piri\n",
        encoding="utf-8",
    )
    fallback_env = tmp_path / "bridge.env"
    fallback_env.write_text(
        "CCC_CODEX_REAL_CLI_PATH=/bridge/real-codex\n", encoding="utf-8"
    )
    script = r"""
import os
from telegram_bot.__main__ import build_context, load_runtime_settings

settings = load_runtime_settings()
assert "CCC_PIRI_REAL_CLI_PATH" not in os.environ
context = build_context(settings)
runtime = context.agent_runtime
if settings.agent_provider == "piri":
    environment = runtime._process_environment
else:
    pool_factory = getattr(runtime, "_runtime_factory", None)
    if pool_factory is not None:
        runtime = pool_factory(dict(runtime._shared_environment))
    environment = runtime._process_environment
assert environment["CCC_PIRI_REAL_CLI_PATH"] == "/project/real-piri", environment.get("CCC_PIRI_REAL_CLI_PATH")
assert environment["CCC_CODEX_REAL_CLI_PATH"] == "/bridge/real-codex"
assert "CCC_PIRI_REAL_CLI_PATH" not in os.environ
print("WRAPPER-ENV-COMPOSED-OK", settings.channel, settings.agent_provider)
"""
    home = tmp_path / "home"
    home.mkdir()
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={
            "HOME": str(home),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(PYTHONPATH_SHIM),
            "PROJECT_ROOT": str(project_root),
            "CCC_BOT_ENV_FILE": str(fallback_env),
        },
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )


def test_matrix_piri_runtime_receives_wrapper_keys_from_env_files(tmp_path):
    result = _run_composition_probe(tmp_path, "piri", "matrix")
    assert result.returncode == 0, result.stderr
    assert "WRAPPER-ENV-COMPOSED-OK matrix piri" in result.stdout


def test_telegram_codex_runtime_receives_wrapper_keys_from_env_files(tmp_path):
    result = _run_composition_probe(tmp_path, "codex", "telegram")
    assert result.returncode == 0, result.stderr
    assert "WRAPPER-ENV-COMPOSED-OK telegram codex" in result.stdout


def test_piri_distill_backend_receives_wrapper_keys():
    from telegram_bot.memory.distill_backend_factory import build_distill_backend

    settings = SimpleNamespace(
        memory_distill_model="provider-default",
        memory_distill_timeout_seconds=120.0,
        codex_distill_model="provider-default",
        codex_distill_timeout_seconds=120.0,
        codex_cli_path="codex",
        claude_cli_path=None,
        piri_cli_path="piri",
        wrapper_environment=lambda: {"CCC_PIRI_REAL_CLI_PATH": "/env-file/real-piri"},
    )
    original = os.environ.pop("CCC_PIRI_REAL_CLI_PATH", None)
    try:
        backend = build_distill_backend(settings, provider="piri", wiki_enabled=False)
    finally:
        if original is not None:
            os.environ["CCC_PIRI_REAL_CLI_PATH"] = original
    assert backend._environment["CCC_PIRI_REAL_CLI_PATH"] == "/env-file/real-piri"
