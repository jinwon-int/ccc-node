import importlib
import io
import json
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
    # Pin contract-compliant perms under any umask (#779): the lifecycle
    # audit ledger fail-closes (via ensure_private_directory) when the bot
    # data dir is group/other-writable.
    project.chmod(0o700)
    env_dir.chmod(0o700)
    lines = ["TELEGRAM_BOT_TOKEN=123456:test"]
    lines.extend(f"{key}={value}" for key, value in values.items())
    (env_dir / ".env").write_text("\n".join(lines) + "\n")
    (env_dir / ".env").chmod(0o600)
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
        "CCC_HONCHO_MEMORY_ENABLED": "1",
        "CCC_MEMORY_USER_LABEL": "Etter Ahn",
        "CCC_MEMORY_ASSISTANT_LABEL": "Karellen",
        "CCC_LIFECYCLE_AUDIT": "0",
        "CCC_LIFECYCLE_AUDIT_DIR": str(settings.bot_data_dir / "lifecycle-audit"),
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


def test_hook_policy_exports_validated_lifecycle_gate_and_shared_ledger(tmp_path):
    settings = _load(
        tmp_path,
        {
            "CCC_LIFECYCLE_AUDIT": "1",
        },
    )
    exported = settings.hook_policy_environment()
    assert exported["CCC_LIFECYCLE_AUDIT"] == "1"
    assert exported["CCC_LIFECYCLE_AUDIT_DIR"] == str(
        settings.bot_data_dir / "lifecycle-audit"
    )


def test_exported_hook_env_drives_python_cli_to_live_observer_ledger(
    tmp_path, monkeypatch
):
    from telegram_bot.core import lifecycle_hook

    settings = _load(tmp_path, {"CCC_LIFECYCLE_AUDIT": "1"})
    exported = settings.hook_policy_environment()
    for key, value in exported.items():
        monkeypatch.setenv(key, value)

    payload = {
        "session_id": "shared-ledger-session",
        "tool_name": "Write",
        "tool_input": {"file_path": "/never-persist-this"},
    }
    rc = lifecycle_hook.main(
        ["lifecycle_hook", "PostToolUse"],
        stdin=io.StringIO(json.dumps(payload)),
    )
    ledger = settings.bot_data_dir / "lifecycle-audit" / "lifecycle-audit.jsonl"
    assert rc == 0 and ledger.is_file()
    record = json.loads(ledger.read_text().strip())
    assert record["event"] == "tool_completed"
    assert "never-persist-this" not in json.dumps(record)


def test_unknown_isolation_profile_fails_closed_at_config_validation(tmp_path):
    with pytest.raises(ValidationError):
        _load(tmp_path, {"CCC_NODE_ISOLATION_PROFILE": "unknown"})
