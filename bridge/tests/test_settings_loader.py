"""Import and explicit-load contracts for bridge settings."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHONPATH_SHIM = REPO_ROOT / ".github" / "pythonpath"


def test_config_module_import_is_pure_without_runtime_environment(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    probe = r"""
import os
before = dict(os.environ)
from telegram_bot.utils import config
assert dict(os.environ) == before
assert config.Config.__name__ == "Config"
assert config.Settings is config.Config
print("pure-import-ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env={
            "HOME": str(home),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(PYTHONPATH_SHIM),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "pure-import-ok"
    assert list(home.iterdir()) == []


def test_explicit_load_preserves_process_project_fallback_precedence(tmp_path: Path):
    project_root = tmp_path / "project"
    project_env = project_root / ".telegram_bot" / ".env"
    project_env.parent.mkdir(parents=True)
    project_env.write_text(
        "TELEGRAM_BOT_TOKEN=123456:project\n"
        "CCC_BRIDGE_EXECUTION_PROFILE=strict-project\n"
        "CCC_TELEGRAM_STREAMING=true\n",
        encoding="utf-8",
    )
    fallback_env = tmp_path / "package.env"
    fallback_env.write_text(
        "TELEGRAM_BOT_TOKEN=123456:fallback\n"
        "CCC_BRIDGE_EXECUTION_PROFILE=owner-operator\n"
        "CCC_TELEGRAM_MAX_BUBBLE_CHARS=2222\n",
        encoding="utf-8",
    )
    process_values = {
        "HOME": str(tmp_path / "home"),
        "CCC_BRIDGE_EXECUTION_PROFILE": "disabled",
    }
    probe = r"""
import json
import os
from pathlib import Path
from telegram_bot.utils.config import Config

before = dict(os.environ)
settings = Config.load(
    project_root=Path(os.environ["PROBE_PROJECT_ROOT"]),
    environ=json.loads(os.environ["PROBE_PROCESS_VALUES"]),
    bot_env_file=Path(os.environ["PROBE_FALLBACK_ENV"]),
)
assert dict(os.environ) == before
print(json.dumps({
    "token": settings.telegram_bot_token,
    "profile": settings.execution_profile,
    "streaming": settings.enable_streaming,
    "bubble_chars": settings.telegram_max_bubble_chars,
    "bot_data_dir": str(settings.bot_data_dir),
    "logs_dir": str(settings.logs_dir),
    "session_store_path": str(settings.session_store_path),
}, sort_keys=True))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path / "ambient-home"),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(PYTHONPATH_SHIM),
            "PROBE_PROJECT_ROOT": str(project_root),
            "PROBE_PROCESS_VALUES": json.dumps(process_values),
            "PROBE_FALLBACK_ENV": str(fallback_env),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "token": "123456:project",
        "profile": "disabled",
        "streaming": True,
        "bubble_chars": 2222,
        "bot_data_dir": str(project_root / ".telegram_bot"),
        "logs_dir": str(project_root / ".telegram_bot" / "logs"),
        "session_store_path": str(project_root / ".telegram_bot" / "sessions.json"),
    }


def test_cli_bootstrap_binds_explicit_settings_before_runtime_imports(tmp_path: Path):
    project_root = tmp_path / "project"
    env_file = project_root / ".telegram_bot" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=123456:project\n"
        "ALLOWED_USER_IDS=42\n"
        "CCC_REQUIRE_ALLOWLIST=true\n"
        "CCC_BRIDGE_EXECUTION_PROFILE=owner-operator\n",
        encoding="utf-8",
    )
    probe = r"""
import os
from pathlib import Path
from telegram_bot.__main__ import load_runtime_settings
from telegram_bot.utils.config import config

settings = load_runtime_settings(
    project_root=Path(os.environ["PROBE_PROJECT_ROOT"]),
    environ={"HOME": os.environ["HOME"]},
    bot_env_file=Path(os.environ["PROBE_FALLBACK_ENV"]),
)
assert config.telegram_bot_token == settings.telegram_bot_token
assert config.allowed_user_ids == [42]
assert config.execution_profile == "owner-operator"
print("cli-bootstrap-ok")
"""
    fallback_env = tmp_path / "empty-fallback.env"
    fallback_env.write_text("", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(PYTHONPATH_SHIM),
            "PROBE_PROJECT_ROOT": str(project_root),
            "PROBE_FALLBACK_ENV": str(fallback_env),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "cli-bootstrap-ok"


def test_placeholder_tokens_preserve_package_fallback_compatibility(tmp_path: Path):
    project_root = tmp_path / "project"
    project_env = project_root / ".telegram_bot" / ".env"
    project_env.parent.mkdir(parents=True)
    fallback_env = tmp_path / "package.env"
    fallback_env.write_text("TELEGRAM_BOT_TOKEN=123456:fallback\n", encoding="utf-8")
    probe = r"""
import json
import os
from pathlib import Path
from telegram_bot.utils.config import Settings

root = Path(os.environ["PROBE_PROJECT_ROOT"])
fallback = Path(os.environ["PROBE_FALLBACK_ENV"])
empty_fallback = Path(os.environ["PROBE_EMPTY_FALLBACK_ENV"])
project_env = root / ".telegram_bot" / ".env"
project_env.write_text("TELEGRAM_BOT_TOKEN=your_bot_token_here\n", encoding="utf-8")
project_placeholder = Settings.load(
    project_root=root,
    environ={"HOME": os.environ["HOME"]},
    bot_env_file=fallback,
)
project_env.write_text("TELEGRAM_BOT_TOKEN=123456:project\n", encoding="utf-8")
process_placeholder = Settings.load(
    project_root=root,
    environ={
        "HOME": os.environ["HOME"],
        "TELEGRAM_BOT_TOKEN": "your_bot_token_here",
    },
    bot_env_file=fallback,
)
process_placeholder_without_package_token = Settings.load(
    project_root=root,
    environ={
        "HOME": os.environ["HOME"],
        "TELEGRAM_BOT_TOKEN": "your_bot_token_here",
    },
    bot_env_file=empty_fallback,
)
print(json.dumps([
    project_placeholder.telegram_bot_token,
    process_placeholder.telegram_bot_token,
    process_placeholder_without_package_token.telegram_bot_token,
]))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(PYTHONPATH_SHIM),
            "PROBE_PROJECT_ROOT": str(project_root),
            "PROBE_FALLBACK_ENV": str(fallback_env),
            "PROBE_EMPTY_FALLBACK_ENV": str(tmp_path / "empty-package.env"),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "123456:fallback",
        "123456:fallback",
        "123456:project",
    ]


@pytest.mark.parametrize("root_value", ["", " ", "\t"])
def test_explicit_load_rejects_empty_project_root(tmp_path: Path, root_value: str):
    probe = r"""
import os
from pathlib import Path
from telegram_bot.utils.config import Settings

try:
    Settings.load(
        environ={
            "HOME": os.environ["HOME"],
            "PROJECT_ROOT": os.environ["PROBE_ROOT_VALUE"],
            "TELEGRAM_BOT_TOKEN": "123456:test",
        },
        bot_env_file=Path(os.environ["PROBE_FALLBACK_ENV"]),
    )
except ValueError as exc:
    assert "PROJECT_ROOT must be non-empty" in str(exc)
    print("root-rejected")
else:
    raise AssertionError("empty PROJECT_ROOT was accepted")
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(PYTHONPATH_SHIM),
            "PROBE_ROOT_VALUE": root_value,
            "PROBE_FALLBACK_ENV": str(tmp_path / "missing-package.env"),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "root-rejected"


def test_malformed_require_allowlist_is_rejected(tmp_path: Path):
    probe = r"""
import os
from pathlib import Path
from pydantic import ValidationError
from telegram_bot.utils.config import Settings

try:
    Settings.load(
        project_root=Path(os.environ["PROBE_PROJECT_ROOT"]),
        environ={
            "HOME": os.environ["HOME"],
            "TELEGRAM_BOT_TOKEN": "123456:test",
            "ALLOWED_USER_IDS": "",
            "CCC_REQUIRE_ALLOWLIST": "treu",
        },
        bot_env_file=Path(os.environ["PROBE_FALLBACK_ENV"]),
    )
except ValidationError as exc:
    assert "CCC_REQUIRE_ALLOWLIST" in str(exc)
    print("bool-rejected")
else:
    raise AssertionError("malformed CCC_REQUIRE_ALLOWLIST was accepted")
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(PYTHONPATH_SHIM),
            "PROBE_PROJECT_ROOT": str(tmp_path / "project"),
            "PROBE_FALLBACK_ENV": str(tmp_path / "missing-package.env"),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bool-rejected"


def test_timeout_invariant_is_rejected_during_settings_load(tmp_path: Path):
    from pydantic import ValidationError
    from telegram_bot.utils.config import Settings

    with pytest.raises(
        ValidationError,
        match="CCC_DELEGATED_TASK_STALL_SECONDS must be lower than CLAUDE_PROCESS_TIMEOUT",
    ):
        Settings.load(
            project_root=tmp_path / "project",
            environ={
                "HOME": str(tmp_path / "home"),
                "TELEGRAM_BOT_TOKEN": "123456:test",
                "CLAUDE_PROCESS_TIMEOUT": "3600",
            },
            bot_env_file=tmp_path / "missing-package.env",
        )


_CHANNEL_TOKEN_PROBE = r"""
import json
import os
from pathlib import Path
from pydantic import ValidationError
from telegram_bot.utils.config import Settings

environ = {"HOME": os.environ["HOME"], **json.loads(os.environ["PROBE_PROCESS_VALUES"])}
try:
    settings = Settings.load(
        project_root=Path(os.environ["PROBE_PROJECT_ROOT"]),
        environ=environ,
        bot_env_file=Path(os.environ["PROBE_FALLBACK_ENV"]),
    )
except ValidationError as exc:
    print(json.dumps({"error": str(exc)}))
else:
    print(json.dumps({"channel": settings.channel, "token": settings.telegram_bot_token}))
"""


def _load_channel_settings(
    tmp_path: Path,
    process_values: dict[str, str],
    *,
    project_env: str = "",
    fallback_env: str = "",
) -> dict:
    project_root = tmp_path / "project"
    env_file = project_root / ".telegram_bot" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(project_env, encoding="utf-8")
    fallback = tmp_path / "package.env"
    fallback.write_text(fallback_env, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", _CHANNEL_TOKEN_PROBE],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(PYTHONPATH_SHIM),
            "PROBE_PROJECT_ROOT": str(project_root),
            "PROBE_PROCESS_VALUES": json.dumps(process_values),
            "PROBE_FALLBACK_ENV": str(fallback),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_FRONTEND_CHANNELS = {
    "matrix": {"CCC_CHANNEL": "matrix", "CCC_MATRIX_CONFIG_PATH": "/nonexistent/matrix.json"},
    "hande": {"CCC_CHANNEL": "hande", "CCC_HANDE_CONFIG_PATH": "/nonexistent/hande.json"},
}


@pytest.mark.parametrize("channel", sorted(_FRONTEND_CHANNELS))
def test_non_telegram_channel_loads_without_token(tmp_path: Path, channel: str):
    # #2411: Matrix/hande never talk to the Bot API, so a node that retired
    # its Telegram bridge may delete TELEGRAM_BOT_TOKEN from every env file.
    assert _load_channel_settings(tmp_path, _FRONTEND_CHANNELS[channel]) == {
        "channel": channel,
        "token": None,
    }


@pytest.mark.parametrize("placeholder", ["your_bot_token_here", "", "   "])
@pytest.mark.parametrize("channel", sorted(_FRONTEND_CHANNELS))
def test_non_telegram_channel_treats_placeholder_token_as_absent(
    tmp_path: Path, channel: str, placeholder: str
):
    process_values = {**_FRONTEND_CHANNELS[channel], "TELEGRAM_BOT_TOKEN": placeholder}
    loaded = _load_channel_settings(
        tmp_path,
        process_values,
        project_env=f"TELEGRAM_BOT_TOKEN={placeholder}\n",
    )
    assert loaded == {"channel": channel, "token": None}


def test_non_telegram_channel_keeps_a_configured_token(tmp_path: Path):
    # The Grok-on-Matrix route still binds its journal to the token's bot id.
    loaded = _load_channel_settings(
        tmp_path, _FRONTEND_CHANNELS["matrix"], fallback_env="TELEGRAM_BOT_TOKEN= 123456:fallback \n"
    )
    assert loaded == {"channel": "matrix", "token": "123456:fallback"}


@pytest.mark.parametrize(
    ("process_values", "project_env"),
    [
        ({}, ""),
        ({"CCC_CHANNEL": "telegram"}, ""),
        # Blank selection resolves the default channel (#2177).
        ({"CCC_CHANNEL": ""}, ""),
        ({}, "TELEGRAM_BOT_TOKEN=your_bot_token_here\n"),
        ({"TELEGRAM_BOT_TOKEN": "  "}, ""),
    ],
)
def test_telegram_channel_still_requires_token(
    tmp_path: Path, process_values: dict[str, str], project_env: str
):
    loaded = _load_channel_settings(tmp_path, process_values, project_env=project_env)
    assert "TELEGRAM_BOT_TOKEN is not configured" in loaded["error"]


@pytest.mark.parametrize("process_values", [{}, {"CCC_CHANNEL": "telegram"}])
def test_telegram_channel_loads_with_token(tmp_path: Path, process_values: dict[str, str]):
    loaded = _load_channel_settings(
        tmp_path, process_values, project_env="TELEGRAM_BOT_TOKEN=123456:project\n"
    )
    assert loaded == {"channel": "telegram", "token": "123456:project"}
