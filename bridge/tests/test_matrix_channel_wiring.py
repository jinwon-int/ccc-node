"""CCC_CHANNEL wiring for the Matrix frontend (#1780).

The Telegram path stays the default and byte-identical; the Matrix path is
opt-in and fails closed without its private config path.
"""

from __future__ import annotations

from pathlib import Path
import sys
import types

import pytest

from telegram_bot.__main__ import build_context, create_app
from telegram_bot.utils.config import Settings


def _settings(root: Path, **overrides) -> Settings:
    env = {
        "HOME": str(root),
        "TELEGRAM_BOT_TOKEN": "123456:synthetic-token",
        "CCC_AGENT_PROVIDER": "claude",
        **overrides,
    }
    return Settings.load(project_root=root, environ=env, bot_env_file=root / "absent.env")


def test_channel_defaults_to_telegram(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.channel == "telegram"
    assert settings.matrix_config_path is None


def test_matrix_channel_requires_config_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="CCC_MATRIX_CONFIG_PATH"):
        _settings(tmp_path, CCC_CHANNEL="matrix")
    settings = _settings(tmp_path, CCC_CHANNEL="matrix", CCC_MATRIX_CONFIG_PATH=str(tmp_path / "matrix.json"))
    assert settings.channel == "matrix"
    assert settings.matrix_config_path == tmp_path / "matrix.json"


def test_unknown_channel_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _settings(tmp_path, CCC_CHANNEL="irc")


def test_build_context_routes_memory_by_channel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    telegram = build_context(_settings(tmp_path))
    assert telegram.project_chat._memory_route == "telegram"
    matrix = build_context(_settings(tmp_path, CCC_CHANNEL="matrix", CCC_MATRIX_CONFIG_PATH=str(tmp_path / "m.json")))
    assert matrix.project_chat._memory_route == "matrix"


def test_create_app_selects_matrix_bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, object] = {}

    class FakeMatrixBot:
        def __init__(self, settings, *, project_chat, session_manager, clock=None):
            created.update(settings=settings, project_chat=project_chat, session_manager=session_manager, clock=clock)

    module = types.ModuleType("telegram_bot.core.matrix.bot")
    module.MatrixBot = FakeMatrixBot  # type: ignore[attr-defined]
    package = types.ModuleType("telegram_bot.core.matrix")
    package.__path__ = []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "telegram_bot.core.matrix", package)
    monkeypatch.setitem(sys.modules, "telegram_bot.core.matrix.bot", module)

    context = build_context(_settings(tmp_path, CCC_CHANNEL="matrix", CCC_MATRIX_CONFIG_PATH=str(tmp_path / "m.json")))
    bot = create_app(context)
    assert isinstance(bot, FakeMatrixBot)
    assert created["project_chat"] is context.project_chat
    assert created["session_manager"] is context.session_manager

    # Telegram stays the default frontend.
    telegram_bot = create_app(build_context(_settings(tmp_path)))
    assert type(telegram_bot).__name__ == "TelegramBot"
