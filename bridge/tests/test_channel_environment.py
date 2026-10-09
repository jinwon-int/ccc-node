"""#2177: provider children never inherit their frontend's channel selection.

The Matrix frontend runs with CCC_CHANNEL=matrix / CCC_MATRIX_CONFIG_PATH in
its process environment. Its agent's tool shell inherited both, so a Telegram
lifecycle command typed there resolved the Matrix frontend (a Termux node,
2026-10-08). These tests pin that every provider child spawn path withholds
the selection keys while BOT_DATA_DIR -- the memory hooks' data location --
still reaches the child.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from telegram_bot.core.agent_runtime import SessionRequest
from telegram_bot.core.claude_runtime import ClaudeRuntime
from telegram_bot.core.crush_runtime import CrushServerClient
from telegram_bot.utils.channel_environment import (
    CHANNEL_SELECTION_KEYS,
    channel_selection_blank_overlay,
    drop_blank_channel_selection,
    without_channel_selection,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHONPATH_SHIM = REPO_ROOT / ".github" / "pythonpath"

MATRIX_FRONTEND_ENV = {
    "CCC_CHANNEL": "matrix",
    "CCC_MATRIX_CONFIG_PATH": "/nonexistent/family-matrix/config.json",
    "BOT_DATA_DIR": "/nonexistent/.ccc-matrix",
}


@pytest.fixture()
def matrix_frontend_env(monkeypatch):
    for name in CHANNEL_SELECTION_KEYS:
        monkeypatch.delenv(name, raising=False)
    for name, value in MATRIX_FRONTEND_ENV.items():
        monkeypatch.setenv(name, value)


def test_selection_keys_never_include_a_data_location():
    # The hooks find this frontend's journals through BOT_DATA_DIR; dropping
    # it would file Matrix memory under the Telegram data directory.
    assert "BOT_DATA_DIR" not in CHANNEL_SELECTION_KEYS
    assert "LOGS_DIR" not in CHANNEL_SELECTION_KEYS
    assert {"CCC_CHANNEL", "CCC_MATRIX_CONFIG_PATH"} <= set(CHANNEL_SELECTION_KEYS)


def test_without_channel_selection_keeps_everything_else():
    env = {**MATRIX_FRONTEND_ENV, "SESSION_STORE_PATH": "/s", "PATH": "/bin"}
    assert without_channel_selection(env) == {
        "BOT_DATA_DIR": "/nonexistent/.ccc-matrix",
        "PATH": "/bin",
    }


def test_blank_overlay_names_only_present_keys():
    assert channel_selection_blank_overlay({"PATH": "/bin"}) == {}
    assert channel_selection_blank_overlay(MATRIX_FRONTEND_ENV) == {
        "CCC_CHANNEL": "",
        "CCC_MATRIX_CONFIG_PATH": "",
    }


def test_drop_blank_only_touches_blank_selection_keys():
    values = {"CCC_CHANNEL": " ", "CCC_MATRIX_CONFIG_PATH": "/c", "OTHER": ""}
    assert drop_blank_channel_selection(values) == {
        "CCC_MATRIX_CONFIG_PATH": "/c",
        "OTHER": "",
    }


def _load(tmp_path: Path, **environ: str):
    from telegram_bot.utils.config import Settings

    base = {
        "PROJECT_ROOT": str(tmp_path),
        "HOME": str(tmp_path),
        "TELEGRAM_BOT_TOKEN": "123456:test",
        "ALLOWED_USER_IDS": "1",
    }
    return Settings.load(
        project_root=tmp_path,
        environ={**base, **environ},
        bot_env_file=tmp_path / "missing.env",
    )


def test_config_load_reads_a_blanked_child_environment_as_telegram(tmp_path):
    # The environment a Claude/Codex child of the Matrix frontend sees.
    child = {**MATRIX_FRONTEND_ENV, **channel_selection_blank_overlay(MATRIX_FRONTEND_ENV)}
    settings = _load(tmp_path, **child)
    assert settings.channel == "telegram"
    assert settings.matrix_config_path is None
    # The data location is untouched.
    assert settings.bot_data_dir == Path("/nonexistent/.ccc-matrix")


def test_config_load_still_honours_a_real_matrix_frontend(tmp_path):
    settings = _load(tmp_path, **MATRIX_FRONTEND_ENV)
    assert settings.channel == "matrix"
    assert settings.matrix_config_path == Path("/nonexistent/family-matrix/config.json")


def test_claude_child_blanks_selection_and_keeps_data_dir(tmp_path, matrix_frontend_env):
    options = ClaudeRuntime()._build_options(
        SessionRequest(working_directory=str(tmp_path)),
        lambda *_: None,  # never invoked while building options
    )
    assert options.env["CCC_CHANNEL"] == ""
    assert options.env["CCC_MATRIX_CONFIG_PATH"] == ""
    # The SDK transport merges os.environ under options.env.
    child = {**os.environ, **options.env}
    assert child["BOT_DATA_DIR"] == "/nonexistent/.ccc-matrix"
    assert not child["CCC_CHANNEL"]


def test_claude_child_of_telegram_frontend_gets_no_overlay(tmp_path, monkeypatch):
    for name in CHANNEL_SELECTION_KEYS:
        monkeypatch.delenv(name, raising=False)
    options = ClaudeRuntime()._build_options(
        SessionRequest(working_directory=str(tmp_path)), lambda *_: None
    )
    assert not set(options.env) & set(CHANNEL_SELECTION_KEYS)


def test_crush_inherited_environment_drops_selection(matrix_frontend_env):
    client = CrushServerClient(executable="crush")
    try:
        assert "CCC_CHANNEL" not in client._env
        assert "CCC_MATRIX_CONFIG_PATH" not in client._env
        assert client._env["BOT_DATA_DIR"] == "/nonexistent/.ccc-matrix"
    finally:
        if client._config_dir is not None:
            client._config_dir.cleanup()


def _composed_child_environment(
    tmp_path: Path, provider: str, memory_mode: str
) -> subprocess.CompletedProcess:
    """Build the real runtime of a Matrix frontend and report its child env."""

    project_root = tmp_path / "project"
    project_env = project_root / ".telegram_bot" / ".env"
    project_env.parent.mkdir(parents=True)
    project_env.write_text(
        "TELEGRAM_BOT_TOKEN=123456:test\n"
        "ALLOWED_USER_IDS=1\n"
        f"CCC_AGENT_PROVIDER={provider}\n"
        f"CCC_BRIDGE_MEMORY_MODE={memory_mode}\n"
        # audience-scoped Codex refuses file credentials (#581).
        "CCC_CODEX_AUDIENCE_AUTH_MODE=keyring\n",
        encoding="utf-8",
    )
    script = r"""
import os
from telegram_bot.__main__ import build_context, load_runtime_settings

settings = load_runtime_settings()
assert settings.channel == "matrix"
runtime = build_context(settings).agent_runtime
pool_factory = getattr(runtime, "_runtime_factory", None)
if pool_factory is not None:
    runtime = pool_factory(dict(runtime._shared_environment))
environment = runtime._process_environment
# The frontend itself keeps its selection.
assert os.environ["CCC_CHANNEL"] == "matrix"
assert not environment.get("CCC_CHANNEL"), environment.get("CCC_CHANNEL")
assert not environment.get("CCC_MATRIX_CONFIG_PATH")
assert environment["BOT_DATA_DIR"] == os.environ["BOT_DATA_DIR"]
print("CHILD-ENV-OK", settings.agent_provider)
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
            "CCC_BOT_ENV_FILE": str(tmp_path / "missing.env"),
            "CCC_CHANNEL": "matrix",
            "CCC_MATRIX_CONFIG_PATH": str(tmp_path / "matrix.json"),
            "BOT_DATA_DIR": str(tmp_path / ".ccc-matrix"),
        },
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize(
    ("provider", "memory_mode"),
    [("piri", "off"), ("codex", "off"), ("codex", "audience-scoped")],
)
def test_matrix_frontend_provider_child_withholds_selection(tmp_path, provider, memory_mode):
    # audience-scoped Codex builds each child through CodexRuntimePool.
    result = _composed_child_environment(tmp_path, provider, memory_mode)
    assert result.returncode == 0, result.stderr
    assert f"CHILD-ENV-OK {provider}" in result.stdout
