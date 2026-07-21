"""Regression tests: ``Settings.load`` with explicit arguments is hermetic.

Incident (2026-07-19): a test calling ``Settings.load(project_root=tmp,
environ={...}, bot_env_file=missing)`` on a live node still picked up
``CCC_CLAUDE_RUNTIME_ADAPTER=1`` from the node's real project ``.env``
(that flag retired with #584 slice C-2; ``CCC_WIKI_MEMORY_ENABLED`` is the
boolean probe now).
Root cause: pydantic-core invokes the custom ``BaseSettings.__init__`` even
from ``model_validate``, so the pydantic-settings env/dotenv sources
(``os.environ`` plus the import-time ``model_config.env_file`` baked from the
ambient ``PROJECT_ROOT``) silently backfilled every field the explicit call
did not provide.

These tests plant a fake "live node" env file, wire it into the module
constants and ``model_config.env_file`` exactly the way an exported
``PROJECT_ROOT`` would at import time, prove the ambient state is genuinely
live (implicit ``Config()`` construction sees it), and then assert that
explicit-args ``Settings.load`` never does.
"""

import importlib
import sys

import pytest


@pytest.fixture()
def config_module():
    # Use a freshly imported REAL config module: sibling tests may have left a
    # (contained) fake in sys.modules, and monkeypatched constants must never
    # touch a module object other tests hold.
    sys.modules.pop("telegram_bot.utils.config", None)
    module = importlib.import_module("telegram_bot.utils.config")
    assert hasattr(module.Settings, "load")
    return module


def _plant_ambient_machine_state(config_module, tmp_path, monkeypatch):
    """Simulate a live node: project env file + exported operator process env."""
    ambient_root = tmp_path / "ambient-project-root"
    ambient_env = ambient_root / ".telegram_bot" / ".env"
    ambient_env.parent.mkdir(parents=True)
    ambient_env.write_text(
        "CCC_WIKI_MEMORY_ENABLED=0\n"
        "CCC_BRIDGE_EXECUTION_PROFILE=owner-operator\n"
        "ALLOWED_USER_IDS=4242\n",
        encoding="utf-8",
    )
    # What importing config with PROJECT_ROOT exported would have produced:
    monkeypatch.setattr(config_module, "PROJECT_ROOT", ambient_root)
    monkeypatch.setattr(config_module, "ENV_FILE_PATH", ambient_env)
    monkeypatch.setitem(
        config_module.Config.model_config, "env_file", [str(ambient_env)]
    )
    # An operator shell exporting its own profile:
    monkeypatch.setenv("CCC_WIKI_MEMORY_ENABLED", "0")
    monkeypatch.setenv("CCC_BRIDGE_EXECUTION_PROFILE", "owner-operator")
    monkeypatch.setenv("ALLOWED_USER_IDS", "4242")
    monkeypatch.delenv("CCC_BOT_ENV_FILE", raising=False)
    return ambient_env


def test_explicit_load_never_reads_ambient_env_file_or_process_env(
    config_module, tmp_path, monkeypatch
):
    _plant_ambient_machine_state(config_module, tmp_path, monkeypatch)

    # Control: the planted machine state IS live for implicit construction,
    # proving the simulation is wired into the leak path under test.
    control = config_module.Config(telegram_bot_token="123456:test")
    assert control.wiki_memory_enabled is False
    assert control.execution_profile == "owner-operator"
    assert control.allowed_user_ids == [4242]

    # The hermetic contract: explicit environ/project_root/bot_env_file means
    # nothing outside those roots is consulted — every planted value must
    # resolve to its documented default instead.
    settings = config_module.Settings.load(
        project_root=tmp_path / "project",
        environ={"TELEGRAM_BOT_TOKEN": "123456:test"},
        bot_env_file=tmp_path / "missing-package.env",
    )
    assert settings.wiki_memory_enabled is True
    assert settings.execution_profile == "strict-project"
    assert settings.allowed_user_ids == []


def test_default_load_still_reads_process_env_project_env_and_fallback(
    config_module, tmp_path, monkeypatch
):
    """Production ``load()`` (no explicit environ) keeps its three sources."""
    project = tmp_path / "project"
    (project / ".telegram_bot").mkdir(parents=True)
    (project / ".telegram_bot" / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=123456:test\n"
        "CCC_BRIDGE_BASH_POLICY=approve-each\n"
        "CCC_TELEGRAM_SESSION_SCOPE=shared-groups\n",
        encoding="utf-8",
    )
    fallback = tmp_path / "package.env"
    fallback.write_text(
        "CCC_TELEGRAM_SESSION_SCOPE=shared-all\n"
        "LOG_LEVEL=WARNING\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROJECT_ROOT", str(project))
    monkeypatch.setenv("CCC_BRIDGE_BASH_POLICY", "auto-review")
    monkeypatch.delenv("CCC_TELEGRAM_SESSION_SCOPE", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("CCC_BOT_ENV_FILE", raising=False)

    settings = config_module.Settings.load(bot_env_file=fallback)

    # Process env beats the project file:
    assert settings.bash_policy == "auto-review"
    # The project file beats the package fallback:
    assert settings.telegram_session_scope == "shared-groups"
    # The package fallback fills whatever remains:
    assert settings.log_level == "WARNING"


def test_ccc_bot_env_file_redirects_package_fallback(config_module, tmp_path):
    """The isolation knob used by subprocess tests: explicit env wins over the
    baked-in package fallback location, and a passed bot_env_file still wins."""
    redirected = tmp_path / "redirected-package.env"
    redirected.write_text("CCC_BRIDGE_BASH_POLICY=approve-each\n", encoding="utf-8")

    settings = config_module.Settings.load(
        project_root=tmp_path / "project",
        environ={
            "TELEGRAM_BOT_TOKEN": "123456:test",
            "CCC_BOT_ENV_FILE": str(redirected),
        },
    )
    assert settings.bash_policy == "approve-each"

    explicit = tmp_path / "explicit-package.env"
    explicit.write_text("CCC_BRIDGE_BASH_POLICY=disabled\n", encoding="utf-8")
    settings = config_module.Settings.load(
        project_root=tmp_path / "project",
        environ={
            "TELEGRAM_BOT_TOKEN": "123456:test",
            "CCC_BOT_ENV_FILE": str(redirected),
        },
        bot_env_file=explicit,
    )
    assert settings.bash_policy == "disabled"
