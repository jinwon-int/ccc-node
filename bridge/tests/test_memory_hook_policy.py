import importlib
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError


def _fresh_config_module():
    sys.modules.pop("telegram_bot.utils.config", None)
    return importlib.import_module("telegram_bot.utils.config")


def _load(tmp_path: Path, values: dict[str, str]):
    project = tmp_path / "project"
    env_dir = project / ".telegram_bot"
    env_dir.mkdir(parents=True)
    lines = ["TELEGRAM_BOT_TOKEN=123456:test"]
    lines.extend(f"{key}={value}" for key, value in values.items())
    (env_dir / ".env").write_text("\n".join(lines) + "\n")
    module = _fresh_config_module()
    return module.Settings.load(
        project_root=project,
        environ={"HOME": str(tmp_path / "home")},
        bot_env_file=tmp_path / "missing.env",
    )


def test_external_policy_forces_wiki_off_and_exports_only_validated_fields(tmp_path):
    settings = _load(
        tmp_path,
        {
            "CCC_NODE_ISOLATION_PROFILE": "external",
            "CCC_WIKI_MEMORY_ENABLED": "1",
            "CCC_MEMORY_USER_LABEL": "Etter   Ahn",
            "CCC_MEMORY_ASSISTANT_LABEL": "Karellen",
        },
    )

    exported = settings.hook_policy_environment()

    assert exported == {
        "CCC_NODE_ISOLATION_PROFILE": "external",
        "CCC_WIKI_MEMORY_ENABLED": "0",
        "CCC_MEMORY_USER_LABEL": "Etter Ahn",
        "CCC_MEMORY_ASSISTANT_LABEL": "Karellen",
    }
    assert "TELEGRAM_BOT_TOKEN" not in exported


def test_fleet_policy_preserves_explicit_wiki_disable(tmp_path):
    settings = _load(
        tmp_path,
        {
            "CCC_NODE_ISOLATION_PROFILE": "fleet",
            "CCC_WIKI_MEMORY_ENABLED": "0",
        },
    )
    assert settings.hook_policy_environment()["CCC_WIKI_MEMORY_ENABLED"] == "0"


def test_unknown_isolation_profile_fails_closed_at_config_validation(tmp_path):
    with pytest.raises(ValidationError):
        _load(tmp_path, {"CCC_NODE_ISOLATION_PROFILE": "unknown"})
