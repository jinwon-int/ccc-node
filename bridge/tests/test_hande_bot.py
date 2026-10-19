"""``HandeBot`` = ``MatrixBot`` with the hande config and transport (#2380 PR-2)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_bot.core.hande import state
from telegram_bot.core.hande.bot import HandeBot
from telegram_bot.core.hande.state import HandeConfigError, load_config, validate_config
from telegram_bot.core.matrix.bot import MatrixBot
from telegram_bot.utils.channel_environment import CHANNEL_SELECTION_KEYS, without_channel_selection


def _config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "account": "@node-a:hande",
        "owner": "@owner:hande",
        "rooms": ["agent"],
        "family_rooms": [],
        "family_users": ["@kid:hande"],
        "socket_path": "/run/ccc-hande/responder.sock",
    }
    config.update(overrides)
    return config


def _write(tmp_path: Path, config: dict[str, Any], mode: int = 0o600) -> Path:
    path = tmp_path / "hande.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    os.chmod(path, mode)
    return path


def _settings(tmp_path: Path, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = dict(
        agent_provider="codex",
        project_root=tmp_path,
        execution_profile="owner-operator",
        bash_policy="approve-each",
        allowed_user_ids=[7],
        require_allowlist=True,
        claude_settings_path=tmp_path / "claude" / "settings.json",
        bot_data_dir=tmp_path / "data",
        hande_config_path=tmp_path / "hande.json",
        hande_socket_path=None,
        telegram_session_scope="per-user-chat",
        bridge_memory_mode="off",
        auto_new_session_after_hours=None,
        matrix_startup_banner=False,
        channel="hande",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


# -- state ---------------------------------------------------------------------


def test_identity_folding_matches_the_bot_actor_rule() -> None:
    assert state.actor_of_device("owner-iphone-27a879") == "owner"
    assert state.sender_identity("@owner-pc:hande") == "@owner:hande"
    assert state.sender_identity("@kid-iphone-1b89e3:hande") == "@kid:hande"
    assert state.sender_identity("@agentnode:hande") == "@agentnode:hande"
    assert state.sender_identity("@owner-pc:matrix.example") is None
    assert state.sender_identity("owner-pc") is None
    assert state.sender_identity("@bad actor:hande") is None


def test_validate_config_defaults_and_normalisation() -> None:
    config = validate_config(_config())
    assert config["approval_timeout_s"] == 120
    assert config["socket_mode"] == 0o660
    assert config["interim_frames"] is False and config["notice_frames"] is False
    assert config["family_users"] == ["@kid:hande"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"account": "node-a"},
        {"owner": "@owner:matrix.example"},
        {"rooms": []},
        {"rooms": ["agent", "agent"]},
        {"rooms": ["bad room"]},
        {"family_rooms": ["fleet"]},
        {"family_users": ["owner"]},
        {"account": "@owner:hande"},
        {"socket_mode": "0666"},
        {"socket_mode": "rw"},
        {"approval_timeout_s": 0},
        {"approval_timeout_s": "120"},
        {"notice_frames": "yes"},
        {"socket_path": ""},
    ],
)
def test_validate_config_fails_closed(overrides: dict[str, Any]) -> None:
    with pytest.raises(HandeConfigError):
        validate_config(_config(**overrides))


def test_load_config_requires_a_private_file(tmp_path: Path) -> None:
    path = _write(tmp_path, _config(), mode=0o644)
    with pytest.raises(HandeConfigError, match="private"):
        load_config(path)
    os.chmod(path, 0o600)
    assert load_config(path)["owner"] == "@owner:hande"
    with pytest.raises(HandeConfigError, match="not found"):
        load_config(tmp_path / "missing.json")


# -- bot -----------------------------------------------------------------------


def test_hande_bot_is_a_matrix_bot_with_hande_config(tmp_path: Path) -> None:
    _write(tmp_path, _config())
    bot = HandeBot(_settings(tmp_path), project_chat=object(), session_manager=object())
    assert isinstance(bot, MatrixBot)
    assert bot.config_path() == tmp_path / "hande.json"
    config = bot.load_config()
    assert config["account"] == "@node-a:hande"
    assert bot.load_config() is config  # cached
    bot.validate_runtime_paths()
    # Owner + family resolve through the same id map MatrixBot uses.
    ints = bot.allowed_user_ints()
    assert len(ints) == 2 and bot._check_user_access(ints[0]) is True
    assert bot._check_user_access(ints[1]) is False


def test_hande_bot_missing_config_path_is_a_clear_error(tmp_path: Path) -> None:
    bot = HandeBot(
        _settings(tmp_path, hande_config_path=None), project_chat=object(), session_manager=object()
    )
    with pytest.raises(HandeConfigError, match="CCC_HANDE_CONFIG_PATH"):
        bot.config_path()


def test_hande_bot_builds_the_hande_transport(tmp_path: Path) -> None:
    _write(tmp_path, _config())
    bot = HandeBot(
        _settings(tmp_path, hande_socket_path=tmp_path / "override.sock"),
        project_chat=object(),
        session_manager=object(),
    )
    transport = bot._build_transport(bot.load_config())
    from telegram_bot.core.hande.transport import HandeTransport

    assert isinstance(transport, HandeTransport)
    assert transport.socket_path == tmp_path / "override.sock"  # setting beats config
    assert transport.policy.rooms == {"agent": "direct"}


def test_transport_verdict_words_the_socket_not_a_sync_loop(tmp_path: Path) -> None:
    _write(tmp_path, _config())
    bot = HandeBot(_settings(tmp_path), project_chat=object(), session_manager=object())
    bot._health_started = 0.0  # past the startup grace
    bot._transport = SimpleNamespace(
        health_signals=lambda now: {"connected": False, "clients_seen": 1}
    )
    verdict, reason, failures = bot._transport_verdict()
    assert verdict == "error" and "no bot connected" in reason and failures == 1
    bot._transport = SimpleNamespace(health_signals=lambda now: {"connected": True})
    assert bot._transport_verdict() == ("ok", "", 0)
    bot._transport = object()
    assert bot._transport_verdict() == ("wait", "", 0)


# -- wiring ----------------------------------------------------------------------


def test_create_app_routes_hande_to_handebot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from telegram_bot import __main__ as entry

    captured: dict[str, Any] = {}

    class StubBot:
        def __init__(self, settings: Any, **kwargs: Any) -> None:
            captured["settings"] = settings
            captured["kwargs"] = kwargs

    monkeypatch.setattr("telegram_bot.core.hande.bot.HandeBot", StubBot)
    context = SimpleNamespace(
        settings=SimpleNamespace(agent_provider="codex", channel="hande"),
        project_chat="chat",
        session_manager="sessions",
        clock=None,
        distill_journal=None,
        distill_snapshot_worker=None,
        distill_extraction_worker=None,
        distill_local_sink_worker=None,
        distill_wiki_sink_worker=None,
        skill_candidate_collector_worker=None,
        memory_promoter=None,
    )
    app = entry.create_app(context)
    assert isinstance(app, StubBot)
    assert captured["kwargs"]["project_chat"] == "chat"
    with pytest.raises(ValueError, match="Grok"):
        entry.create_app(
            SimpleNamespace(settings=SimpleNamespace(agent_provider="grok", channel="hande"))
        )


def test_hande_selection_keys_are_hidden_from_provider_children() -> None:
    assert "CCC_HANDE_CONFIG_PATH" in CHANNEL_SELECTION_KEYS
    assert "CCC_HANDE_SOCKET_PATH" in CHANNEL_SELECTION_KEYS
    env = {"CCC_CHANNEL": "hande", "CCC_HANDE_CONFIG_PATH": "/x", "BOT_DATA_DIR": "/d"}
    assert without_channel_selection(env) == {"BOT_DATA_DIR": "/d"}


def test_settings_require_hande_config_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from telegram_bot.utils.config import Settings

    for key in list(os.environ):
        if key.startswith(("CCC_", "TELEGRAM_", "BOT_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABCDEF")
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("CCC_CHANNEL", "hande")
    with pytest.raises(Exception, match="CCC_HANDE_CONFIG_PATH"):
        Settings()
    monkeypatch.setenv("CCC_HANDE_CONFIG_PATH", str(tmp_path / "hande.json"))
    settings = Settings()
    assert settings.channel == "hande"
    assert settings.restart_service_unit == "ccc-hande-bridge.service"
