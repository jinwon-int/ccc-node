"""#2841: os.environ-only operational switches honour the bridge ``.env``.

The Hande / Matrix entry points start ``python -m telegram_bot`` directly (a
systemd unit, or a Termux wrapper that runs ``env -i``), so ``start.sh``'s
``.env`` export never happens. ``ExternalWaitMonitor.env_int`` / ``env_flag``
read ``os.environ`` only, so a cap written to ``.env`` was silently ignored and
the default won. These tests pin the export-free fallback that closes the gap.
"""

import os

import pytest

from telegram_bot.core.external_wait_monitor import (
    DEFAULT_RESUME_DAILY_CAP,
    ExternalWaitMonitor,
)
from telegram_bot.utils import dotenv_fallback


@pytest.fixture(autouse=True)
def _clean_fallback(monkeypatch):
    dotenv_fallback.clear()
    for name in ("CCC_EXTERNAL_WAIT_RESUME_DAILY_CAP", "CCC_EXTERNAL_WAIT_RESUME", "CCC_T2841_FLAG"):
        monkeypatch.delenv(name, raising=False)
    yield
    dotenv_fallback.clear()


def test_selection_keeps_only_non_credential_ccc_values():
    selected = dotenv_fallback.select_operational_values(
        {
            "CCC_EXTERNAL_WAIT_RESUME_DAILY_CAP": "30",
            "CCC_WEBHOOK_NUDGE_SECRET": "s3cret",
            "CCC_PUSH_FLEET_RELAY_SECRET_FILE": "/path",
            "CCC_SOME_API_KEY": "k",
            "CCC_BOT_TOKEN_HINT": "t",
            "TELEGRAM_BOT_TOKEN": "123:abc",
            "ANTHROPIC_MODEL": "claude-x",
            "CCC_BLANK": "  ",
            "CCC_NONE": None,
            "CCC_NUL": "a\x00b",
        }
    )
    assert selected == {"CCC_EXTERNAL_WAIT_RESUME_DAILY_CAP": "30"}


def test_lookup_prefers_process_then_installed_layers():
    dotenv_fallback.install({"CCC_X": "dotenv", "CCC_Y": "dotenv-y"})
    env = {"CCC_X": "process", "CCC_Z": ""}
    assert dotenv_fallback.lookup("CCC_X", env) == "process"
    assert dotenv_fallback.lookup("CCC_Y", env) == "dotenv-y"
    # A key present in the process environment wins even when blank.
    dotenv_fallback.install({"CCC_Z": "dotenv-z"})
    assert dotenv_fallback.lookup("CCC_Z", env) == ""
    assert dotenv_fallback.lookup("CCC_MISSING", env) is None


def test_install_replaces_and_installed_returns_a_copy():
    dotenv_fallback.install({"CCC_A": "1"})
    dotenv_fallback.install({"CCC_B": "2"})
    copy = dotenv_fallback.installed()
    assert copy == {"CCC_B": "2"}
    copy["CCC_B"] = "tampered"
    assert dotenv_fallback.installed() == {"CCC_B": "2"}


def test_install_from_settings_tolerates_settings_without_the_capture():
    dotenv_fallback.install({"CCC_STALE": "1"})
    dotenv_fallback.install_from_settings(object())
    assert dotenv_fallback.installed() == {}

    class _Settings:
        def dotenv_operational_environment(self):
            return {"CCC_OK": "1", "CCC_API_KEY": "dropped"}

    dotenv_fallback.install_from_settings(_Settings())
    assert dotenv_fallback.installed() == {"CCC_OK": "1"}


def test_env_int_reads_dotenv_when_process_lacks_the_key(monkeypatch):
    name = "CCC_EXTERNAL_WAIT_RESUME_DAILY_CAP"
    assert ExternalWaitMonitor.env_int(name, default=DEFAULT_RESUME_DAILY_CAP) == 30
    dotenv_fallback.install({name: "45"})
    assert ExternalWaitMonitor.env_int(name, default=DEFAULT_RESUME_DAILY_CAP) == 45
    monkeypatch.setenv(name, "12")
    assert ExternalWaitMonitor.env_int(name, default=DEFAULT_RESUME_DAILY_CAP) == 12
    # Blank process value keeps the old meaning: fall back to the default.
    monkeypatch.setenv(name, "")
    assert ExternalWaitMonitor.env_int(name, default=DEFAULT_RESUME_DAILY_CAP) == 30
    monkeypatch.delenv(name)
    dotenv_fallback.install({name: "not-a-number"})
    assert ExternalWaitMonitor.env_int(name, default=7) == 7
    dotenv_fallback.install({name: "-5"})
    assert ExternalWaitMonitor.env_int(name, default=7, minimum=0) == 0


def test_env_flag_reads_dotenv_when_process_lacks_the_key(monkeypatch):
    name = "CCC_T2841_FLAG"
    assert ExternalWaitMonitor.env_flag(name, default=True) is True
    dotenv_fallback.install({name: "off"})
    assert ExternalWaitMonitor.env_flag(name, default=True) is False
    monkeypatch.setenv(name, "1")
    assert ExternalWaitMonitor.env_flag(name, default=False) is True


def test_default_resume_cap_is_30():
    assert DEFAULT_RESUME_DAILY_CAP == 30


def test_config_load_captures_dotenv_layers_without_process_values(tmp_path):
    from telegram_bot.utils.config import Settings

    project_root = tmp_path / "project"
    project_env = project_root / ".telegram_bot" / ".env"
    project_env.parent.mkdir(parents=True)
    project_env.write_text(
        "TELEGRAM_BOT_TOKEN=123456:project\n"
        "CCC_EXTERNAL_WAIT_RESUME_DAILY_CAP=40\n"
        "CCC_WEBHOOK_NUDGE_SECRET=project-secret\n",
        encoding="utf-8",
    )
    fallback_env = tmp_path / "bridge.env"
    fallback_env.write_text(
        "CCC_EXTERNAL_WAIT_RESUME_DAILY_CAP=30\n"
        "CCC_CONTINUATION_DAILY_CAP=25\n"
        "CCC_MODEL_LABEL=label\n",
        encoding="utf-8",
    )
    before = dict(os.environ)

    settings = Settings.load(
        project_root=project_root,
        environ={"CCC_CONTINUATION_DAILY_CAP": "99"},
        bot_env_file=fallback_env,
    )

    # Project .env over the package fallback; process values are not captured
    # (lookup reads them live) and credential-looking names are dropped.
    assert settings.dotenv_operational_environment() == {
        "CCC_EXTERNAL_WAIT_RESUME_DAILY_CAP": "40",
        "CCC_CONTINUATION_DAILY_CAP": "25",
        "CCC_MODEL_LABEL": "label",
    }
    assert dict(os.environ) == before
    settings.dotenv_operational_environment()["CCC_MODEL_LABEL"] = "tampered"
    assert settings.dotenv_operational_environment()["CCC_MODEL_LABEL"] == "label"


def test_main_installs_the_dotenv_layers(monkeypatch, tmp_path):
    import telegram_bot.__main__ as entry

    class _Settings:
        def hook_policy_environment(self):
            return {}

        def dotenv_operational_environment(self):
            return {"CCC_EXTERNAL_WAIT_RESUME_DAILY_CAP": "33"}

    class _Bot:
        def validate_runtime_paths(self):
            pass

        def run(self):
            raise SystemExit(0)

    monkeypatch.setattr(entry, "load_runtime_settings", lambda: _Settings())
    monkeypatch.setattr(entry, "create_bot", lambda settings: _Bot())
    monkeypatch.setattr(entry, "setup_logging", lambda settings: None)
    monkeypatch.setattr(entry, "report_provider_environment", lambda settings: None)
    monkeypatch.setattr("sys.argv", ["telegram_bot", "--path", str(tmp_path)])
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))

    with pytest.raises(SystemExit):
        entry.main()

    assert dotenv_fallback.installed() == {"CCC_EXTERNAL_WAIT_RESUME_DAILY_CAP": "33"}
    assert ExternalWaitMonitor.env_int(
        "CCC_EXTERNAL_WAIT_RESUME_DAILY_CAP", default=DEFAULT_RESUME_DAILY_CAP
    ) == 33
